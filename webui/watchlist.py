"""V2-WATCHLIST: local symbols, price and decision triggers, no model runs.

A price or decision change is only a trigger. The default rule mode is
observe, so the trigger is stored and does not start an analysis. Opening the
K-line page does not consume a trigger. The same trigger id is stored once.
Admitting a start checks the current rule version and still does not call a
model; auto mode is not exposed in the page.

The page label follows that check. No check is 未检查. A stored check or
admission without a run is 已检查，未启动. 已启动 requires an admission that
names a run. A start flag on the trigger is not a start.
"""

from __future__ import annotations

import json
import math
import threading
from datetime import UTC, datetime
from pathlib import Path

from webui.service import webui_home

_LOCK = threading.Lock()
DEFAULT_RULE = "watch-1"
DEFAULT_MODE = "observe"
MODE_LABEL = {"observe": "观察", "auto": "自动"}


class WatchlistError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _path(root: Path | None) -> Path:
    base = Path(root) if root is not None else webui_home()
    base.mkdir(parents=True, exist_ok=True)
    return base / "watchlist.json"


def _blank() -> dict:
    return {
        "rule_version": DEFAULT_RULE,
        "mode": DEFAULT_MODE,
        "items": [],
        "triggers": [],
        "admissions": [],
        "checks": [],
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
    data.setdefault("rule_version", DEFAULT_RULE)
    # A missing mode is not observe. A new file still starts in observe via _blank().
    data.setdefault("items", [])
    data.setdefault("triggers", [])
    data.setdefault("admissions", [])
    data.setdefault("checks", [])
    for item in data["items"]:
        if isinstance(item, dict):
            item.setdefault("enabled", True)
            item.setdefault("last_price", None)
            item.setdefault("last_decision", None)
    return data


def _save(root: Path | None, data: dict) -> None:
    path = _path(root)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _symbol(raw) -> str:
    text = str(raw or "").strip().upper()
    if not text or len(text) > 32 or any(ch.isspace() for ch in text):
        raise WatchlistError("请输入标的代码。")
    return text


def canonical_symbol(ticker) -> str:
    """One key for aliases such as 600519 and 600519.SS. Empty when unusable."""
    text = str(ticker or "").strip().upper()
    if not text:
        return ""
    from tradingagents.dataflows.symbols import normalize_symbol

    canonical = str(normalize_symbol(text) or "").strip().upper()
    return canonical or text


def _keys(symbol: str) -> set[str]:
    text = str(symbol or "").strip().upper()
    found = {text} if text else set()
    if not text:
        return found
    from tradingagents.dataflows.symbols import crypto_base, normalize_symbol

    canonical = str(normalize_symbol(text) or "").upper()
    if canonical:
        found.add(canonical)
    base = crypto_base(text) or crypto_base(canonical)
    if base:
        found.add(base)
        found.add(f"{base}-USD")
        found.add(f"{base}-USDT")
    return found


def _asset_type(symbol: str) -> str:
    from tradingagents.dataflows.symbols import crypto_base, normalize_symbol

    if crypto_base(symbol):
        return "crypto"
    canonical = str(normalize_symbol(symbol) or "")
    if canonical.endswith("=F"):
        return "commodity"
    return "stock"


def _asset_label(symbol: str) -> str:
    return {"crypto": "加密货币", "commodity": "贵金属/商品期货"}.get(_asset_type(symbol), "股票")


def _price(value):
    """A usable quote. Zero, negative and non-finite numbers are not prices."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _decision(value) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 64:
        return None
    return text


def _id_part(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _enabled(item: dict) -> bool:
    return item.get("enabled", True) is not False


def _known_mode(mode: str | None) -> str | None:
    if mode in {"observe", "auto"}:
        return mode
    return None


def _watch_note(mode: str | None) -> str:
    """Do not call a missing or unknown rule 观察模式."""
    name = _known_mode(mode)
    tail = "打开K线不消耗触发。未检查表示还没有规则检查。已检查并不表示已启动。"
    if name == "observe":
        return f"价格或决定变化只记录触发。观察模式不会自动启动分析。{tail}"
    if name == "auto":
        return f"价格或决定变化只记录触发。自动模式本页也不调用模型。{tail}"
    return f"当前规则状态未知，不把它写成观察模式，也不会自动启动分析。{tail}"


def _public_item(item: dict) -> dict:
    symbol = str(item.get("symbol") or "")
    enabled = _enabled(item)
    return {
        **item,
        "enabled": enabled,
        "enabled_label": "" if enabled else "已停用",
        "asset_type": _asset_type(symbol),
        "asset_label": _asset_label(symbol),
        # A stored 0 or negative is not a price. The page must not show it as the latest close.
        "last_price": _price(item.get("last_price")),
    }


def _rows(value) -> list[dict]:
    return [row for row in value or [] if isinstance(row, dict)]


def _latest_for(rows: list[dict], ident: str) -> dict | None:
    found = None
    for row in rows:
        if row.get("trigger_id") == ident:
            found = row
    return found


def _run_id(value) -> str | None:
    text = str(value or "").strip()
    return text or None


def _public_trigger(trigger: dict, admissions: list[dict], checks: list[dict]) -> dict:
    """Publish the check, and call it started only when an admission names a run.

    The stored trigger flag stays false even after a check. Reading that flag
    alone would hide a completed check, or call a bare true value a start.
    """
    ident = str(trigger.get("id") or "")
    admission = _latest_for(admissions, ident)
    check = _latest_for(checks, ident)
    run_id = _run_id(admission.get("run_id")) if admission else None
    explicit = trigger.get("kind") == "explicit"
    really_started = (
        not explicit
        and admission is not None
        and admission.get("started") is True
        and run_id is not None
    )
    checked = admission is not None or check is not None or explicit
    if really_started:
        label = "已启动"
    elif explicit:
        label = "只打开向导，未启动"
    elif checked:
        label = "已检查，未启动"
    else:
        label = "未检查"
    published = dict(trigger)
    # A run id on the trigger alone is not a start. Only the admission may name one.
    # 去分析 never starts from this page, even if a stored flag names a run.
    published["run_id"] = None if explicit else (run_id if admission is not None else None)
    published["started"] = really_started
    published["checked"] = checked
    published["status_label"] = label
    return published


def _visible_triggers(rows: list[dict]) -> list[dict]:
    """Newest first. Checked triggers stay even when they are older than the last five."""
    recent = rows[-5:]
    recent_ids = {item.get("id") for item in recent}
    sticky = [item for item in rows if item.get("checked") and item.get("id") not in recent_ids]
    return list(reversed(sticky + recent))


def list_watch(root: Path | None = None) -> dict:
    with _LOCK:
        data = _load(root)
    mode = _known_mode(data.get("mode") if isinstance(data.get("mode"), str) else None)
    admissions = _rows(data.get("admissions"))
    checks = _rows(data.get("checks"))
    triggers = [
        _public_trigger(item, admissions, checks)
        for item in data["triggers"]
        if isinstance(item, dict)
    ]
    return {
        "rule_version": data["rule_version"],
        "mode": mode,
        "mode_label": MODE_LABEL.get(mode) if mode else "状态未知",
        "items": [_public_item(item) for item in data["items"] if isinstance(item, dict)],
        "triggers": triggers,
        "visible_triggers": _visible_triggers(triggers),
        "note": _watch_note(mode),
    }


def add_symbol(raw, root: Path | None = None) -> dict:
    symbol = _symbol(raw)
    with _LOCK:
        data = _load(root)
        if any(item.get("symbol") == symbol for item in data["items"]):
            raise WatchlistError(f"{symbol} 已在自选中。")
        data["items"].append({
            "symbol": symbol,
            "added_at": _now(),
            "enabled": True,
            "last_price": None,
            "last_decision": None,
        })
        _save(root, data)
    return list_watch(root)


def remove_symbol(raw, root: Path | None = None) -> dict:
    symbol = _symbol(raw)
    with _LOCK:
        data = _load(root)
        before = len(data["items"])
        data["items"] = [item for item in data["items"] if item.get("symbol") != symbol]
        if len(data["items"]) == before:
            raise WatchlistError(f"自选里没有 {symbol}。", 404)
        _save(root, data)
    return list_watch(root)


def _trigger_id(rule: str, symbol: str, kind: str, old, new) -> str:
    return f"{rule}|{symbol}|{kind}|{_id_part(old)}->{_id_part(new)}"


def _add_trigger(data: dict, symbol: str, kind: str, old, new) -> dict:
    rule = data["rule_version"]
    ident = _trigger_id(rule, symbol, kind, old, new)
    found = next((item for item in data["triggers"] if item.get("id") == ident), None)
    if found:
        return found
    row = {
        "id": ident,
        "symbol": symbol,
        "kind": kind,
        "rule_version": rule,
        "old_value": old,
        "new_value": new,
        "created_at": _now(),
        "started": False,
        "run_id": None,
    }
    data["triggers"].append(row)
    return row


def _indexed(mapping: dict | None) -> dict:
    indexed: dict = {}
    for raw, value in (mapping or {}).items():
        for key in _keys(str(raw)):
            indexed.setdefault(key, value)
    return indexed


def _lookup(symbol: str, indexed: dict):
    for key in _keys(symbol):
        if key in indexed:
            return indexed[key]
    return None


def _memory_in_this_console() -> bool:
    """A second console must not import decisions from another home's memory log."""
    import os

    override = os.environ.get("TRADINGAGENTS_WEBUI_HOME")
    if not override:
        return True
    try:
        from webui import service

        payload = service.list_memory(limit=1)
        path = Path(str(payload.get("path") or ""))
        if not str(path):
            return False
        home = Path(override).resolve()
        resolved = path.resolve()
    except Exception:
        return False
    return resolved == home or home in resolved.parents


def _decision_key(date: str, tie: float) -> tuple[str, float]:
    return (str(date or ""), float(tie))


def reports_for_this_console(limit: int = 80) -> list[dict]:
    """Saved reports this console may use. A second home does not read another directory."""
    import os

    from webui import service

    rows = [row for row in service.list_reports(limit=limit) if isinstance(row, dict)]
    override = os.environ.get("TRADINGAGENTS_WEBUI_HOME")
    if not override:
        return rows
    try:
        home = Path(override).resolve()
    except OSError:
        return []
    kept = []
    for row in rows:
        path = row.get("markdown") or row.get("html")
        if not path:
            continue
        try:
            resolved = Path(str(path)).resolve()
        except OSError:
            continue
        if resolved == home or home in resolved.parents:
            kept.append(row)
    return kept


def local_decisions() -> dict[str, str]:
    """Newest local rating per ticker. Does not call a model. Missing logs are empty.

    A later trade date wins whether it came from a saved report or the memory log.
    On the same date, the saved report wins because its file time is the tie-break.
    """
    best: dict[str, tuple[tuple[str, float], str]] = {}

    def keep(ticker, rating, key: tuple[str, float]) -> None:
        symbol = canonical_symbol(ticker)
        chosen = _decision(rating)
        if not symbol or not chosen:
            return
        current = best.get(symbol)
        if current is None or key > current[0]:
            best[symbol] = (key, chosen)

    try:
        for row in reports_for_this_console(80):
            if not isinstance(row, dict):
                continue
            # A header rating without a portfolio-manager paragraph is not a decision.
            if row.get("has_decision") is False:
                continue
            modified = row.get("modified")
            tie = float(modified) if isinstance(modified, (int, float)) and not isinstance(modified, bool) else 0.0
            date = str(row.get("date") or "")
            if not date and tie:
                from datetime import datetime
                from zoneinfo import ZoneInfo

                date = datetime.fromtimestamp(tie, ZoneInfo("Asia/Shanghai")).date().isoformat()
            keep(row.get("ticker"), row.get("rating"), _decision_key(date, tie))
    except Exception:
        pass
    if _memory_in_this_console():
        try:
            from webui import service

            payload = service.list_memory(limit=80)
            entries = [row for row in (payload.get("entries") or []) if isinstance(row, dict)]
            # list_memory returns newest write first. A later write only wins a same-date tie
            # against another memory row; a report's file time still wins that date.
            for index, row in enumerate(entries):
                keep(row.get("ticker"), row.get("rating"), _decision_key(str(row.get("date") or ""), float(len(entries) - index)))
        except Exception:
            pass
    return {symbol: rating for symbol, (_key, rating) in best.items()}


def refresh(quotes: dict, root: Path | None = None, decisions: dict | None = None, *,
            observe_local: bool = False) -> dict:
    """Record a trigger when a known price or decision changes. Never starts a model run."""
    if decisions is None and observe_local:
        decisions = local_decisions()
    price_index = _indexed(quotes if isinstance(quotes, dict) else {})
    decision_index = _indexed(decisions if isinstance(decisions, dict) else {})
    with _LOCK:
        data = _load(root)
        for item in data["items"]:
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            symbol = item["symbol"]
            price = _price(_lookup(symbol, price_index))
            if price is not None:
                old = item.get("last_price")
                item["last_price"] = price
                item["priced_at"] = _now()
                if old is not None and old != price:
                    _add_trigger(data, symbol, "price", old, price)
            decision = _decision(_lookup(symbol, decision_index))
            if decision is not None:
                old_decision = item.get("last_decision")
                item["last_decision"] = decision
                item["decided_at"] = _now()
                if old_decision and old_decision != decision:
                    _add_trigger(data, symbol, "decision", old_decision, decision)
        _save(root, data)
    return list_watch(root)


def open_kline(raw, root: Path | None = None) -> dict:
    symbol = _symbol(raw)
    with _LOCK:
        data = _load(root)
        if not any(item.get("symbol") == symbol for item in data["items"]):
            raise WatchlistError(f"自选里没有 {symbol}。", 404)
    return {"action": "kline", "symbol": symbol, "consumed_trigger": False}


def explicit_analyze(raw, root: Path | None = None) -> dict:
    """An explicit click opens the wizard. It records one trigger per rule version."""
    symbol = _symbol(raw)
    with _LOCK:
        data = _load(root)
        if not any(item.get("symbol") == symbol for item in data["items"]):
            raise WatchlistError(f"自选里没有 {symbol}。", 404)
        row = _add_trigger(data, symbol, "explicit", None, None)
        reason = "去分析只打开向导，不由这条触发启动。"
        checks = data.get("checks") if isinstance(data.get("checks"), list) else []
        already = any(
            item.get("trigger_id") == row["id"] and item.get("result") == "wizard" for item in checks
        )
        if not already:
            _append_check(data, row["id"], "wizard", reason)
        _save(root, data)
    return {
        "action": "wizard",
        "symbol": symbol,
        "trigger_id": row["id"],
        "started": False,
        "note": "已记录明确触发。请在分析向导确认后再启动，不会在这里直接调用模型。",
    }


def _condition_holds(item: dict, trigger: dict) -> bool:
    kind = trigger.get("kind")
    if kind == "price":
        return item.get("last_price") == trigger.get("new_value")
    if kind == "decision":
        return item.get("last_decision") == trigger.get("new_value")
    return False


def _append_check(data: dict, trigger_id: str, result: str, reason: str) -> None:
    """Remember a check without treating a refusal as the one allowed start."""
    checks = data.get("checks")
    if not isinstance(checks, list):
        checks = []
        data["checks"] = checks
    checks.append({
        "trigger_id": trigger_id,
        "result": result,
        "reason": reason,
        "checked_at": _now(),
        "started": False,
        "run_id": None,
    })


def admit(raw, trigger_id: str, root: Path | None = None) -> dict:
    """Check whether one trigger may start. Never calls a model, and never starts twice.

    Observe mode always refuses. Auto mode may record one admission, still with
    ``run_id`` empty, because this page is not allowed to launch an analysis.
    A refusal is stored as a check so the page can show that it happened. It does
    not occupy the one admission, so a later rule can still be checked once.
    """
    symbol = _symbol(raw)
    ident = str(trigger_id or "").strip()
    if not ident:
        raise WatchlistError("没有这条触发。", 404)
    with _LOCK:
        data = _load(root)
        item = next((row for row in data["items"] if row.get("symbol") == symbol), None)
        if item is None:
            raise WatchlistError(f"自选里没有 {symbol}。", 404)
        trigger = next((row for row in data["triggers"] if row.get("id") == ident), None)
        if trigger is None or trigger.get("symbol") != symbol:
            raise WatchlistError("没有这条触发。", 404)
        existing = next((row for row in data["admissions"] if row.get("trigger_id") == ident), None)
        if existing:
            reason = "同一触发已经检查过，不会再次启动。"
            _append_check(data, ident, "duplicate", reason)
            _save(root, data)
            return {**existing, "allowed": False, "started": False, "duplicate": True,
                    "reason": reason}
        reason = None
        if not item.get("enabled", True):
            reason = "这项自选已停用，不会启动。"
        elif trigger.get("rule_version") != data.get("rule_version"):
            reason = "规则版本已变化，不会按旧版本启动。"
        elif trigger.get("kind") == "explicit":
            reason = "去分析只打开向导，不由这条触发启动。"
        elif data.get("mode") != "auto":
            if data.get("mode") == "observe":
                reason = "当前是观察模式，不会启动分析。"
            elif not data.get("mode"):
                reason = "当前规则状态未知，不把它写成观察模式。不会启动分析。"
            else:
                reason = "当前规则不会启动分析。"
        elif not _condition_holds(item, trigger):
            reason = "条件已经变化，不会按这条触发启动。"
        if reason:
            _append_check(data, ident, "refused", reason)
            _save(root, data)
            return {"allowed": False, "started": False, "duplicate": False, "trigger_id": ident,
                    "reason": reason}
        note = "规则检查通过，但本页不调用模型。"
        receipt = {
            "trigger_id": ident,
            "rule_version": data["rule_version"],
            "admitted_at": _now(),
            "run_id": None,
            "started": False,
            "note": note,
        }
        _append_check(data, ident, "admitted", note)
        data["admissions"].append(receipt)
        _save(root, data)
    return {**receipt, "allowed": True, "duplicate": False, "reason": receipt["note"]}
