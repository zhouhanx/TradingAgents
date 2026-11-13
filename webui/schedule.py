"""V2-SCHEDULE: local schedules and reminder receipts. No model runs.

A due slot writes an immutable trigger, then checks the current rule version.
Observe mode records that the trigger would be accepted and does not start an
analysis. Auto is not offered in the page; Eric has not confirmed that version.
A manual run is allowed under observe, but this module does not call a model
unless the caller passes a starter. Reminders are an outbox: the receipt is
saved before send, and a failure stays pending with backoff.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from webui.service import webui_home

_LOCK = threading.RLock()
TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_RULE = "sched-1"
DEFAULT_MODE = "observe"
RETRY_START = 5
RETRY_CAP = 300
SYSTEM_AFTER = 3
MODE_LABEL = {"observe": "观察", "auto": "自动", "off": "关闭"}
RECEIPT_LABEL = {"pending": "待发送", "sent": "已发送", "dropped": "已丢弃"}


class ScheduleError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now_dt(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ)
    return now.astimezone(TZ)


def _stamp(now: datetime | None = None) -> str:
    return _now_dt(now).replace(microsecond=0).isoformat()


def _path(root: Path | None) -> Path:
    base = Path(root) if root is not None else webui_home()
    base.mkdir(parents=True, exist_ok=True)
    return base / "schedule.json"


def _blank() -> dict:
    return {
        "rule_version": DEFAULT_RULE,
        "mode": DEFAULT_MODE,
        "schedules": [],
        "triggers": [],
        "checks": [],
        "rechecks": [],
        "bindings": [],
        "receipts": [],
        "last_decisions": {},
        "seen_completions": None,
    }


def _load(root: Path | None) -> dict:
    path = _path(root)
    if not path.is_file():
        return _blank()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _blank()
    if not isinstance(data, dict):
        return _blank()
    base = _blank()
    base.update({key: data.get(key, base[key]) for key in base})
    return base


def _save(root: Path | None, data: dict) -> None:
    path = _path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _symbol(raw) -> str:
    text = str(raw or "").strip().upper()
    if not text or len(text) > 32 or any(ch.isspace() for ch in text):
        raise ScheduleError("请输入标的代码。")
    return text


def _clock(raw) -> str:
    text = str(raw or "").strip()
    parts = text.split(":")
    if len(parts) < 2:
        raise ScheduleError("请选择每天的时间。")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ScheduleError("请选择每天的时间。") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ScheduleError("请选择每天的时间。")
    return f"{hour:02d}:{minute:02d}"


def _asset_label(symbol: str) -> str:
    from tradingagents.dataflows.symbols import crypto_base, normalize_symbol

    if crypto_base(symbol):
        return "加密货币"
    if str(normalize_symbol(symbol) or "").endswith("=F"):
        return "贵金属/商品期货"
    return "股票"


def _latest_due(data: dict, schedule_id: str) -> dict | None:
    rows = [
        item for item in data.get("triggers") or []
        if item.get("schedule_id") == schedule_id and item.get("kind") == "due"
    ]
    if not rows:
        return None
    return max(rows, key=lambda item: (str(item.get("slot") or ""), str(item.get("created_at") or "")))


def _latest_check(data: dict, trigger_id: str) -> dict | None:
    found = None
    for item in data.get("checks") or []:
        if item.get("trigger_id") == trigger_id:
            found = item
    return found



def _public_reminders(data: dict) -> list[dict]:
    """Recent reminders, plus every pending or dropped receipt even if it is older."""
    receipts = [item for item in data.get("receipts") or [] if isinstance(item, dict)]

    def label(item: dict) -> dict:
        label = RECEIPT_LABEL.get(item.get("status"))
        published = {**item, "status_label": label or "状态未知"}
        # A dropped or sent receipt is not waiting to retry, even if an older flag remains.
        if published.get("status") != "pending":
            published["system_problem"] = False
        return published

    sticky_ids = {item.get("id") for item in receipts if item.get("status") in {"pending", "dropped"}}
    sticky = [label(item) for item in receipts if item.get("id") in sticky_ids]
    sticky.reverse()
    others = [label(item) for item in receipts if item.get("id") not in sticky_ids][-20:]
    others.reverse()
    return sticky + others



def _known_mode(mode: str | None) -> str | None:
    if mode in {"observe", "auto", "off"}:
        return mode
    return None


def _schedule_status_label(item: dict, mode: str | None) -> str:
    """Name the rule state. Closed and observe are not 启用.

    启用 used to mean the schedule would run. Observe does not run, and a
    closed rule refuses the due check. Auto is named separately; a start still
    needs a run id, which the due note says on its own. A missing mode is not
    observe.
    """
    if item.get("status") == "paused":
        return "已暂停"
    # Only an active row may borrow the rule name. Anything else is not 观察中.
    if item.get("status") != "active":
        return "状态未知"
    name = _known_mode(mode)
    if name == "observe":
        return "观察中"
    if name == "off":
        return "规则已关闭"
    if name == "auto":
        return "自动"
    return "状态未知"


def _idle_check(mode: str | None, *, recheck: bool = False) -> tuple[str, str]:
    """A mode that must not be described as observe unless it is observe."""
    name = _known_mode(mode)
    if name == "off":
        reason = "规则已关闭，不会启动。"
        if recheck:
            reason = "已按新版本重新检查。" + reason
        return "refused", reason
    if name == "observe":
        reason = "已按新版本重新检查。观察模式不会启动分析。" if recheck else "观察模式会接受这条触发，但不会启动分析。"
        return "would_accept", reason
    if name == "auto":
        reason = "已按新版本重新检查。当前规则不会启动分析。" if recheck else "当前规则会接受这条触发，但不会启动分析。"
        return "would_accept", reason
    reason = "当前规则状态未知，不把它写成观察模式。不会启动分析。"
    if recheck:
        reason = "已按新版本重新检查。" + reason
    return "would_accept", reason


def _mode_note(mode: str | None) -> str:
    name = _known_mode(mode)
    tail = "手动立即运行只打开向导，不在这里调用模型。"
    if name == "off":
        return f"规则已关闭。到点不会启动分析。{tail}"
    if name == "auto":
        return f"到点会检查规则。没有运行编号不算已启动。{tail}"
    if name == "observe":
        return f"到点只记录触发。观察模式不会自动启动分析。{tail}"
    return f"当前规则状态未知，不把它写成观察模式或已启用。到点不会自动启动。{tail}"


def _due_when(due: dict | None) -> str:
    if not due:
        return ""
    slot = str(due.get("slot") or "")
    short = slot[5:] if len(slot) >= 7 else slot
    return f"{short} {due.get('at') or ''}".strip()


def _due_note(item: dict, due: dict | None, check: dict | None) -> str:
    """Say what the latest due check actually did. A start flag is not a run.

    The page used to collapse every result except two into 未启动. That hid a
    completed check, and it also called a bound run 未启动.
    """
    if due is None:
        if item.get("status") == "paused":
            return "已暂停。暂停期间的到点不会补记。"
        if item.get("status") != "active":
            return "定时状态未知，不把它写成观察中，也不写成可以暂停。"
        return "还没有到点触发。"
    when = _due_when(due)
    if not check:
        return f"{when} 已到点，还没有检查结果。".strip()
    result = check.get("result")
    run_id = str(check.get("run_id") or "").strip()
    started = check.get("started") is True and bool(run_id) and result in {"admitted", "manual"}
    if started:
        return f"{when} 已启动".strip()
    if result == "would_accept":
        return f"{when} 到点会接受，未启动".strip()
    if result == "discarded":
        return f"{when} 规则已变化，未按旧版本启动".strip()
    if result == "refused":
        return f"{when} 规则已关闭，未启动".strip()
    if result == "manual":
        return f"{when} 去分析只打开向导，未启动".strip()
    if result == "admitted":
        return f"{when} 已检查，未启动".strip()
    # An unrecognized result is not evidence that the due check failed to start.
    return f"{when} 检查结果未知，不把它写成未启动或已启动。".strip()


def _public(data: dict) -> dict:
    raw_mode = data.get("mode")
    mode = raw_mode if raw_mode in {"observe", "auto", "off"} else None
    schedules = []
    for item in data["schedules"]:
        if item.get("status") == "removed":
            continue
        latest = _latest_due(data, item.get("id"))
        check = _latest_check(data, latest.get("id")) if latest else None
        schedules.append({
            **item,
            "asset_label": _asset_label(item.get("symbol") or ""),
            "status_label": _schedule_status_label(item, mode),
            "latest_due": latest,
            "latest_check": check,
            "due_note": _due_note(item, latest, check),
        })
    reminders = _public_reminders(data)
    return {
        "rule_version": data.get("rule_version") or DEFAULT_RULE,
        "mode": mode,
        "mode_label": MODE_LABEL.get(mode) if mode else "状态未知",
        "schedules": schedules,
        "triggers": list(data["triggers"][-10:]),
        "checks": list(data["checks"][-10:]),
        "rechecks": [item for item in data["rechecks"] if item.get("status") == "pending"],
        "bindings": list(data["bindings"]),
        "reminders": reminders,
        "note": _mode_note(mode),
    }


def list_schedules(root: Path | None = None) -> dict:
    with _LOCK:
        return _public(_load(root))


def create_schedule(symbol, at, root: Path | None = None) -> dict:
    code = _symbol(symbol)
    clock = _clock(at)
    with _LOCK:
        data = _load(root)
        for item in data["schedules"]:
            if item.get("status") == "removed":
                continue
            if item.get("symbol") == code and item.get("at") == clock:
                raise ScheduleError(f"{code} 在 {clock} 已有定时。")
        data["schedules"].append({
            "id": "sch-" + secrets.token_hex(4),
            "symbol": code,
            "at": clock,
            "cadence": "daily",
            "status": "active",
            "created_at": _stamp(),
        })
        _save(root, data)
    return list_schedules(root)


def _find(data: dict, schedule_id: str) -> dict:
    found = next((item for item in data["schedules"] if item.get("id") == schedule_id), None)
    if found is None or found.get("status") == "removed":
        raise ScheduleError("没有这条定时。", 404)
    return found


def set_status(schedule_id: str, status: str, root: Path | None = None, *, now: datetime | None = None) -> dict:
    if status not in {"active", "paused", "removed"}:
        raise ScheduleError("不能改成这个状态。")
    stamp = _stamp(now)
    with _LOCK:
        data = _load(root)
        item = _find(data, str(schedule_id or ""))
        pauses = item.setdefault("pauses", [])
        if status == "paused" and (not pauses or pauses[-1].get("until")):
            pauses.append({"from": stamp, "until": None})
        elif status == "active" and pauses and not pauses[-1].get("until"):
            pauses[-1]["until"] = stamp
        item["status"] = status
        _save(root, data)
    return list_schedules(root)


def _trigger_id(rule: str, schedule_id: str, slot: str, at: str) -> str:
    return f"{rule}|{schedule_id}|{slot}|{at}"


def _latest_due_slot(item: dict, now: datetime) -> str | None:
    """The newest daily slot at or before now that already existed when it came due.

    Opening the page the next morning still records yesterday. A slot before the
    schedule was created is not due. Only this latest slot is written, so a long
    absence does not backfill every missed day.
    """
    try:
        hour, minute = (int(part) for part in str(item.get("at") or "").split(":")[:2])
    except (TypeError, ValueError):
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    try:
        created = datetime.fromisoformat(str(item.get("created_at") or ""))
    except (TypeError, ValueError):
        return None
    created = created.replace(tzinfo=TZ) if created.tzinfo is None else created.astimezone(TZ)
    local = _now_dt(now)
    slot = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < slot:
        slot = slot - timedelta(days=1)
    if created > slot:
        return None
    if _paused_then(item, slot):
        return None
    return slot.date().isoformat()


def _paused_then(item: dict, slot: datetime) -> bool:
    """True when this daily time fell inside a recorded pause."""
    for span in item.get("pauses") or []:
        if not isinstance(span, dict) or not span.get("from"):
            continue
        try:
            start = datetime.fromisoformat(str(span["from"]))
        except ValueError:
            continue
        start = start.replace(tzinfo=TZ) if start.tzinfo is None else start.astimezone(TZ)
        if slot < start:
            continue
        until = span.get("until")
        if not until:
            return True
        try:
            end = datetime.fromisoformat(str(until))
        except ValueError:
            return True
        end = end.replace(tzinfo=TZ) if end.tzinfo is None else end.astimezone(TZ)
        if slot < end:
            return True
    return False


def _append_trigger(data: dict, row: dict) -> dict:
    found = next((item for item in data["triggers"] if item.get("id") == row["id"]), None)
    if found:
        return found
    data["triggers"].append(row)
    return row


def _binding(data: dict, trigger_id: str) -> dict | None:
    return next((item for item in data["bindings"] if item.get("trigger_id") == trigger_id), None)


def _record_check(data: dict, trigger: dict, result: str, reason: str, *, started: bool = False,
                  run_id: str | None = None) -> dict:
    row = {
        "trigger_id": trigger["id"],
        "rule_version": trigger.get("rule_version"),
        "mode": data.get("mode"),
        "result": result,
        "reason": reason,
        "started": started,
        "run_id": run_id,
        "checked_at": _stamp(),
    }
    data["checks"].append(row)
    return row


def check_trigger(trigger_id: str, root: Path | None = None, *, version_during_check: str | None = None,
                  starter=None, now: datetime | None = None) -> dict:
    """Check one saved trigger. A version change discards this check and requeues it."""
    ident = str(trigger_id or "").strip()
    with _LOCK:
        data = _load(root)
        trigger = next((item for item in data["triggers"] if item.get("id") == ident), None)
        if trigger is None:
            raise ScheduleError("没有这条触发。", 404)
        seen = version_during_check if version_during_check is not None else data.get("rule_version")
        if seen != trigger.get("rule_version"):
            _record_check(data, trigger, "discarded", "规则版本已变化，不会按旧版本启动。")
            if not any(item.get("trigger_id") == ident and item.get("rule_version") == seen
                       and item.get("status") == "pending" for item in data["rechecks"]):
                data["rechecks"].append({
                    "trigger_id": ident,
                    "rule_version": seen,
                    "status": "pending",
                    "queued_at": _stamp(now),
                })
            _save(root, data)
            return {"started": False, "discarded": True, "requeued": True, "trigger_id": ident,
                    "rule_version": seen}
        existing = _binding(data, ident)
        if existing:
            return {"started": False, "duplicate": True, "trigger_id": ident, "run_id": existing.get("run_id"),
                    "reason": "同一触发已经检查过，不会再次启动。"}
        if trigger.get("kind") == "manual":
            return _accept_manual(data, trigger, root, starter)
        if data.get("mode") == "off":
            _record_check(data, trigger, "refused", "规则已关闭，不会启动。")
            _save(root, data)
            return {"started": False, "allowed": False, "reason": "规则已关闭，不会启动。"}
        if data.get("mode") != "auto":
            result, reason = _idle_check(data.get("mode"))
            _record_check(data, trigger, result, reason)
            _save(root, data)
            return {"started": False, "allowed": False, "would_accept": result == "would_accept",
                    "reason": reason}
        run_id = None
        if starter is not None:
            run_id = starter(dict(trigger))
        data["bindings"].append({
            "trigger_id": ident,
            "run_id": run_id,
            "bound_at": _stamp(now),
        })
        note = "规则检查通过，但本进程不调用模型。" if starter is None else "已绑定同一次启动。"
        _record_check(data, trigger, "admitted", note, started=bool(run_id), run_id=run_id)
        _save(root, data)
    return {"started": bool(run_id), "allowed": True, "duplicate": False, "trigger_id": ident,
            "run_id": run_id, "reason": note}


def _accept_manual(data: dict, trigger: dict, root: Path | None, starter) -> dict:
    run_id = starter(dict(trigger)) if starter is not None else None
    data["bindings"].append({
        "trigger_id": trigger["id"],
        "run_id": run_id,
        "bound_at": _stamp(),
    })
    reason = "手动立即运行不受观察模式限制。" if starter is not None else "手动立即运行只打开向导，不在这里调用模型。"
    _record_check(data, trigger, "manual", reason, started=bool(run_id), run_id=run_id)
    _save(root, data)
    return {"started": bool(run_id), "allowed": True, "action": "wizard", "symbol": trigger.get("symbol"),
            "trigger_id": trigger["id"], "run_id": run_id, "reason": reason}


def fire_due(now: datetime | None = None, root: Path | None = None, *, version_during_check: str | None = None,
             starter=None) -> dict:
    """Write due triggers first, then check them. Does not call a model unless starter is passed."""
    local = _now_dt(now)
    created: list[str] = []
    with _LOCK:
        data = _load(root)
        known = {item.get("id") for item in data["triggers"]}
        for item in data["schedules"]:
            due_slot = _latest_due_slot(item, local)
            if item.get("status") != "active" or due_slot is None:
                continue
            ident = _trigger_id(data["rule_version"], item["id"], due_slot, item["at"])
            if ident in known:
                continue
            _append_trigger(data, {
                "id": ident,
                "schedule_id": item["id"],
                "symbol": item["symbol"],
                "kind": "due",
                "rule_version": data["rule_version"],
                "mode": data["mode"],
                "slot": due_slot,
                "at": item["at"],
                "created_at": _stamp(local),
            })
            created.append(ident)
        _save(root, data)
    for ident in created:
        check_trigger(ident, root, version_during_check=version_during_check, starter=starter, now=local)
    recheck(root, starter=starter, now=local)
    return list_schedules(root)


def recheck(root: Path | None = None, *, starter=None, now: datetime | None = None) -> dict:
    """Evaluate pending rechecks under the version they were queued with. Never uses the old version."""
    with _LOCK:
        data = _load(root)
        pending = [item for item in data["rechecks"] if item.get("status") == "pending"]
    for item in pending:
        if item.get("rule_version") != _load(root).get("rule_version"):
            continue
        with _LOCK:
            data = _load(root)
            trigger = next((row for row in data["triggers"] if row.get("id") == item.get("trigger_id")), None)
            queued = next((row for row in data["rechecks"] if row is not None and row.get("trigger_id") == item.get("trigger_id")
                           and row.get("rule_version") == item.get("rule_version") and row.get("status") == "pending"), None)
            if trigger is None or queued is None:
                continue
            if _binding(data, trigger["id"]):
                queued["status"] = "done"
                _save(root, data)
                continue
            # The original trigger keeps its old version. This evaluation uses the queued version.
            if data.get("mode") != "auto":
                result, reason = _idle_check(data.get("mode"), recheck=True)
                _record_check(data, {**trigger, "rule_version": item["rule_version"]}, result, reason)
                queued["status"] = "done"
                _save(root, data)
                continue
            run_id = starter(dict(trigger)) if starter is not None else None
            data["bindings"].append({"trigger_id": trigger["id"], "run_id": run_id, "bound_at": _stamp(now)})
            _record_check(data, {**trigger, "rule_version": item["rule_version"]}, "admitted",
                          "已按新版本重新检查。", started=bool(run_id), run_id=run_id)
            queued["status"] = "done"
            _save(root, data)
    return list_schedules(root)


def manual_run(schedule_id: str, root: Path | None = None, *, now: datetime | None = None, starter=None) -> dict:
    """Explicit run. Observe does not block it. The default path still does not call a model."""
    local = _now_dt(now)
    with _LOCK:
        data = _load(root)
        item = _find(data, str(schedule_id or ""))
        ident = f"manual|{data['rule_version']}|{item['id']}|{local.strftime('%Y-%m-%dT%H:%M')}"
        _append_trigger(data, {
            "id": ident,
            "schedule_id": item["id"],
            "symbol": item["symbol"],
            "kind": "manual",
            "rule_version": data["rule_version"],
            "mode": data["mode"],
            "slot": local.date().isoformat(),
            "at": item["at"],
            "created_at": _stamp(local),
        })
        _save(root, data)
    result = check_trigger(ident, root, starter=starter, now=local)
    result["action"] = "wizard"
    result["symbol"] = item["symbol"]
    return result


def _receipt(data: dict, ident: str) -> dict | None:
    return next((item for item in data["receipts"] if item.get("id") == ident), None)


def enqueue_reminder(kind: str, key: str, body: str, root: Path | None = None, *, symbol: str = "",
                     now: datetime | None = None) -> dict:
    """Save a pending receipt before any send. A dropped or existing id is not queued again."""
    ident = str(key or "").strip()
    if not ident:
        raise ScheduleError("提醒缺少标识。")
    with _LOCK:
        data = _load(root)
        existing = _receipt(data, ident)
        if existing:
            return {**existing, "queued": False}
        row = {
            "id": ident,
            "kind": kind,
            "symbol": symbol,
            "body": body,
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": _stamp(now),
            "created_at": _stamp(now),
            "sent_at": None,
            "dropped_at": None,
            "drop_reason": None,
            "last_error": None,
            "system_problem": False,
        }
        data["receipts"].append(row)
        _save(root, data)
    return {**row, "queued": True}


def _backoff(attempts: int) -> int:
    delay = RETRY_START * (2 ** max(0, attempts - 1))
    return min(RETRY_CAP, delay)


def deliver(root: Path | None = None, *, now: datetime | None = None, sender=None) -> dict:
    """Send pending receipts whose retry time has arrived. Failures stay pending."""
    local = _now_dt(now)
    with _LOCK:
        data = _load(root)
        for item in data["receipts"]:
            if item.get("status") != "pending":
                continue
            due = datetime.fromisoformat(item.get("next_attempt_at") or item["created_at"])
            if due.tzinfo is None:
                due = due.replace(tzinfo=TZ)
            if due > local:
                continue
            try:
                if sender is not None:
                    sender(dict(item))
            except Exception as exc:  # noqa: BLE001 - the receipt must survive a send failure
                item["attempts"] = int(item.get("attempts") or 0) + 1
                item["last_error"] = str(exc)[:180]
                item["next_attempt_at"] = _stamp(local + timedelta(seconds=_backoff(item["attempts"])))
                item["system_problem"] = item["attempts"] >= SYSTEM_AFTER
                continue
            item["status"] = "sent"
            item["sent_at"] = _stamp(local)
            item["system_problem"] = False
        _save(root, data)
    return list_schedules(root)


def drop_reminder(receipt_id: str, reason: str, root: Path | None = None, *, now: datetime | None = None) -> dict:
    text = str(reason or "").strip() or "已丢弃"
    with _LOCK:
        data = _load(root)
        item = _receipt(data, str(receipt_id or ""))
        if item is None:
            raise ScheduleError("没有这条提醒。", 404)
        if item.get("status") != "dropped":
            item["status"] = "dropped"
            item["dropped_at"] = _stamp(now)
            item["drop_reason"] = text
            item["system_problem"] = False
            _save(root, data)
        elif item.get("system_problem"):
            item["system_problem"] = False
            _save(root, data)
    return list_schedules(root)


def _completion_body(symbol: str, rating: str, *, decided: bool | None = None) -> str:
    """A saved report can exist without a portfolio-manager conclusion.

    Only an explicit decision may be called 评级. A missing flag is not one.
    """
    if decided is True:
        tail = f"，评级 {rating}" if rating else ""
        return f"{symbol} 的分析已完成{tail}。"
    if decided is False:
        if rating:
            return f"{symbol} 的分析已完成，报告头写着 {rating}，没有组合经理结论。"
        return f"{symbol} 的分析已完成，没有组合经理结论。"
    if rating:
        return f"{symbol} 的分析已完成，记录里有 {rating}，但不把它当成结构化评级。"
    return f"{symbol} 的分析已完成，没有结构化评级。"


def _decision_body(symbol: str, old: str, new: str) -> str:
    return f"{symbol} 的决定从 {old} 变为 {new}。"


def observe_events(events: list | None, root: Path | None = None, *, now: datetime | None = None) -> dict:
    for event in events or []:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        symbol = str(event.get("symbol") or "").strip().upper()
        if kind == "completion":
            run_id = str(event.get("run_id") or "").strip()
            if not run_id:
                continue
            decided = event.get("has_decision")
            enqueue_reminder("completion", f"completion|{run_id}",
                             _completion_body(symbol or run_id, str(event.get("rating") or "").strip(),
                                              decided=decided if isinstance(decided, bool) else None),
                             root, symbol=symbol, now=now)
        elif kind == "decision":
            from webui.watchlist import canonical_symbol

            symbol = canonical_symbol(symbol)
            old, new = str(event.get("old") or "").strip(), str(event.get("new") or "").strip()
            if not symbol or not old or not new or old == new:
                continue
            # Same transition later is a new reminder. The minute keeps one tick from
            # enqueueing it twice, and a dropped receipt is still not requeued.
            when = _stamp(now)[:16]
            enqueue_reminder("decision", f"decision|{symbol}|{old}->{new}|{when}",
                             _decision_body(symbol, old, new), root, symbol=symbol, now=now)
    return list_schedules(root)


def _completion_events(data: dict) -> list[dict]:
    """New saved reports only. The first look records a baseline and does not remind."""
    try:
        from webui.watchlist import reports_for_this_console

        rows = reports_for_this_console(40)
    except Exception:
        return []
    current = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("ticker") or "").strip().upper()
        run_id = str(row.get("name") or "").strip()
        if symbol and run_id:
            current.append({
                "kind": "completion",
                "symbol": symbol,
                "run_id": run_id,
                "rating": str(row.get("rating") or "").strip(),
                "has_decision": row.get("has_decision") if isinstance(row.get("has_decision"), bool) else None,
            })
    ids = [item["run_id"] for item in current]
    seen = data.get("seen_completions")
    if not isinstance(seen, list):
        data["seen_completions"] = ids
        return []
    known = {str(item) for item in seen}
    fresh = [item for item in current if item["run_id"] not in known]
    data["seen_completions"] = list(dict.fromkeys([*ids, *[str(item) for item in seen]]))[:200]
    return fresh


def _local_events(data: dict) -> list[dict]:
    events: list[dict] = _completion_events(data)
    try:
        from webui.watchlist import local_decisions

        current = local_decisions()
    except Exception:
        current = {}
    from webui.watchlist import canonical_symbol

    seen = data.setdefault("last_decisions", {})
    folded: dict[str, str] = {}
    for symbol, rating in seen.items():
        key = canonical_symbol(symbol)
        if key and rating:
            folded[key] = str(rating)
    fresh: dict[str, str] = {}
    for symbol, rating in current.items():
        key = canonical_symbol(symbol)
        if key and rating:
            fresh[key] = str(rating)
    for symbol, rating in fresh.items():
        old = folded.get(symbol)
        if old and old != rating:
            events.append({"kind": "decision", "symbol": symbol, "old": old, "new": rating})
    data["last_decisions"] = {**folded, **fresh}
    return events


def tick(now: datetime | None = None, root: Path | None = None, *, events: list | None = None,
         observe_local: bool = False, version_during_check: str | None = None, starter=None,
         sender=None) -> dict:
    """One local pass: due triggers, reminder receipts, then delivery. No model call."""
    local = _now_dt(now)
    fire_due(local, root, version_during_check=version_during_check, starter=starter)
    if events is None and observe_local:
        with _LOCK:
            data = _load(root)
            events = _local_events(data)
            _save(root, data)
    observe_events(events or [], root, now=local)
    return deliver(root, now=local, sender=sender)
