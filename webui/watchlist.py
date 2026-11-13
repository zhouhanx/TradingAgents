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
import secrets
import threading
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from webui.service import webui_home

_LOCK = threading.Lock()
DEFAULT_RULE = "watch-1"
DEFAULT_MODE = "observe"
MODE_LABEL = {"observe": "观察", "auto": "自动"}
BEIJING = ZoneInfo("Asia/Shanghai")


class WatchlistError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


# V2-WATCHLIST rules (R2, R11). Four kinds per symbol, at most five rules per symbol.
RULE_KINDS = ("above", "below", "pct", "win")
RULE_CAP = 5
RULE_CAP_TEXT = "每个代码最多 5 条"
PRICE_TEXT = "请输入大于 0 的价格。"
PCT_TEXT = "涨跌幅填 0.1 到 50 之间。"
POLL_CHOICES = (0, 1, 5, 15)
POLL_DEFAULT_WITH_RULES = 5


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
            # Files written before V2-WATCHLIST rules have no rules; they load with none.
            rules = item.get("rules")
            item["rules"] = [rule for rule in rules if isinstance(rule, dict)] if isinstance(rules, list) else []
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


def _market(symbol: str) -> str:
    """Where the symbol trades, for the page's close and pre-open labels (R3)."""
    from tradingagents.dataflows.symbols import normalize_symbol

    kind = _asset_type(symbol)
    if kind != "stock":
        return kind
    canonical = str(normalize_symbol(symbol) or symbol).upper()
    if canonical.endswith((".SS", ".SZ", ".BJ")):
        return "cn"
    if canonical.endswith(".HK"):
        return "hk"
    return "us"


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
        return f"规则命中或决定变化只记录触发。观察模式不会自动启动分析。{tail}"
    if name == "auto":
        return f"规则命中或决定变化只记录触发。自动模式本页也不调用模型。{tail}"
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
        "market": _market(symbol),
        # A stored 0 or negative is not a price. The page must not show it as the latest close.
        "last_price": _price(item.get("last_price")),
        "rules": [_public_rule(rule) for rule in item.get("rules") or [] if isinstance(rule, dict)],
        "rule_cap": RULE_CAP,
        "at_cap": len(item.get("rules") or []) >= RULE_CAP,
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
    """Newest first. Checked triggers stay even when they are older than the last twenty (R6)."""
    recent = rows[-TRIGGER_VISIBLE:]
    recent_ids = {item.get("id") for item in recent}
    sticky = [item for item in rows if item.get("checked") and item.get("id") not in recent_ids]
    return list(reversed(sticky + recent))


def list_watch(root: Path | None = None) -> dict:
    with _LOCK:
        data = _load(root)
    mode = _known_mode(data.get("mode") if isinstance(data.get("mode"), str) else None)
    admissions = _rows(data.get("admissions"))
    checks = _rows(data.get("checks"))
    seen_at = _when(data.get("alerts_seen_at"))
    triggers = []
    for item in data["triggers"]:
        if not isinstance(item, dict):
            continue
        row = _public_trigger(item, admissions, checks)
        made = _when(item.get("created_at"))
        row["unseen"] = item.get("kind") == "rule" and made is not None and (seen_at is None or made > seen_at)
        triggers.append(row)
    return {
        "rule_version": data["rule_version"],
        "mode": mode,
        "mode_label": MODE_LABEL.get(mode) if mode else "状态未知",
        "items": [_public_item(item) for item in data["items"] if isinstance(item, dict)],
        "triggers": triggers,
        "visible_triggers": _visible_triggers(triggers),
        "note": _watch_note(mode),
        "poll_minutes": _poll_minutes(data),
        "poll_stored": isinstance(data.get("poll_minutes"), int) and data.get("poll_minutes") in POLL_CHOICES,
        "has_rules": _has_enabled_rule(data),
        "rule_cap": RULE_CAP,
        "trigger_total": len(triggers),
        "unseen_count": sum(1 for row in triggers if row["unseen"]),
        "alerts_seen_at": data.get("alerts_seen_at"),
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
            "rules": [],
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


def _number_text(value: float) -> str:
    """3 -> 3, 1.5 -> 1.5: the way the page prints a percentage the user typed."""
    return f"{float(value):g}"


def price_text(value: float) -> str:
    """The page's price format (>= 1000 one decimal, >= 1 two, else four significant).

    A typed threshold keeps its own digits when that format would round it away.
    """
    number = float(value)
    size = abs(number)
    if size >= 1000:
        text = f"{number:.1f}"
    elif size >= 1:
        text = f"{number:.2f}"
    else:
        text = f"{number:#.4g}"
    if float(text) != number:
        text = f"{number:.10g}"
    return text


def rule_label(kind: str, value: float) -> str:
    return {
        "above": f"高于 {price_text(value)}",
        "below": f"低于 {price_text(value)}",
        "pct": f"涨跌 ≥ {_number_text(value)}%（较昨收）",
        "win": f"一个刷新间隔内涨跌 ≥ {_number_text(value)}%",
    }.get(kind, "")


def rule_chip(kind: str, value: float) -> str:
    if kind == "pct":
        return f"涨跌 ≥ {_number_text(value)}%"
    if kind == "win":
        return f"间隔内涨跌 ≥ {_number_text(value)}%"
    return rule_label(kind, value)


def beijing_text(stamp, fmt: str = "%m-%d %H:%M") -> str:
    """A stored UTC ISO time in Beijing time. Empty when unreadable."""
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(BEIJING).strftime(fmt)


def _rule_state_label(rule: dict) -> tuple[str, str]:
    """(text, state) for the drawer: 生效中 / 已触发 with the re-arm condition / 已停用."""
    if rule.get("enabled") is False:
        return "已停用。", "off"
    kind, value = rule.get("kind"), rule.get("value")
    hit_at = beijing_text(rule.get("fired_at"))
    if rule.get("state") == "hit" and kind in {"above", "below"}:
        side = "以下" if kind == "above" else "以上"
        return f"已触发（北京 {hit_at}）。价格回到 {price_text(value)} {side}后重新生效。", "hit"
    if rule.get("state") == "today" and kind == "pct":
        return f"已触发（北京 {hit_at}）。同一方向每个交易日只提醒一次，下个交易日重新生效。", "today"
    return "生效中。", "armed"


def _public_rule(rule: dict) -> dict:
    kind = str(rule.get("kind") or "")
    value = rule.get("value")
    usable = kind in RULE_KINDS and isinstance(value, (int, float)) and not isinstance(value, bool)
    text, state = _rule_state_label(rule) if usable else ("规则无法读取，不会判断。", "off")
    return {
        "id": rule.get("id"),
        "kind": kind,
        "value": value,
        "enabled": rule.get("enabled") is not False,
        "state": state,
        "state_label": text,
        "label": rule_label(kind, value) if usable else "",
        "chip": rule_chip(kind, value) if usable else "",
        "fired_at": rule.get("fired_at"),
    }


def _rule_value(kind, raw) -> float:
    if kind not in RULE_KINDS:
        raise WatchlistError("请选择规则类型。")
    if kind in {"above", "below"}:
        number = _price(raw)
        if number is None:
            raise WatchlistError(PRICE_TEXT, code="bad_price")
        return number
    number = _price(raw)
    if number is None or number < 0.1 or number > 50:
        raise WatchlistError(PCT_TEXT, code="bad_pct")
    return number


def _same_rule(rule: dict, kind: str, value: float) -> bool:
    other = rule.get("value")
    return (
        rule.get("kind") == kind
        and isinstance(other, (int, float))
        and math.isclose(float(other), value, rel_tol=1e-9, abs_tol=1e-12)
    )


def _arm(rule: dict) -> None:
    """A fresh arm: a new trigger id, and only a quote fetched after now may fire it (R2)."""
    rule["arm_seq"] = int(rule.get("arm_seq") or 0) + 1
    rule["armed_at"] = _now()
    rule["state"] = "armed"
    rule["fired_at"] = None


def _find_item(data: dict, symbol: str) -> dict:
    item = next((row for row in data["items"] if isinstance(row, dict) and row.get("symbol") == symbol), None)
    if item is None:
        raise WatchlistError(f"自选里没有 {symbol}。", 404)
    return item


def _find_rule(item: dict, rule_id) -> dict:
    rule = next((row for row in item["rules"] if row.get("id") == str(rule_id or "")), None)
    if rule is None:
        raise WatchlistError("这条规则已经不在了。", 404)
    return rule


def change_rule(body: dict, root: Path | None = None) -> dict:
    """POST /api/watchlist/rules: op add | update | toggle | delete. Never calls a model."""
    body = body if isinstance(body, dict) else {}
    symbol = _symbol(body.get("symbol"))
    op = str(body.get("op") or "")
    if op not in {"add", "update", "toggle", "delete"}:
        raise WatchlistError("不支持的规则操作。")
    with _LOCK:
        data = _load(root)
        item = _find_item(data, symbol)
        rules = item["rules"]
        if op == "add":
            if len(rules) >= RULE_CAP:
                raise WatchlistError(f"{RULE_CAP_TEXT}。", code="rule_cap")
            kind = str(body.get("kind") or "")
            value = _rule_value(kind, body.get("value"))
            for rule in rules:
                if _same_rule(rule, kind, value):
                    raise WatchlistError(f"已有一条「{rule_label(kind, value)}」，不用重复加。", code="duplicate")
            rule = {
                "id": f"r{secrets.token_hex(4)}",
                "kind": kind,
                "value": value,
                "enabled": body.get("enabled") is not False,
                "created_at": _now(),
                "arm_seq": 0,
                "fired_days": {},
            }
            _arm(rule)
            rules.append(rule)
        else:
            rule = _find_rule(item, body.get("rule_id"))
            if op == "delete":
                item["rules"] = [row for row in rules if row is not rule]
            elif op == "toggle":
                enabled = body.get("enabled")
                if not isinstance(enabled, bool):
                    enabled = rule.get("enabled") is False
                if enabled and rule.get("enabled") is False:
                    _arm(rule)
                rule["enabled"] = enabled
            else:
                kind = str(rule.get("kind") or "")
                value = _rule_value(kind, body.get("value"))
                for other in rules:
                    if other is not rule and _same_rule(other, kind, value):
                        raise WatchlistError(f"已有一条「{rule_label(kind, value)}」，不用重复加。", code="duplicate")
                if not _same_rule(rule, kind, value):
                    rule["value"] = value
                    # A new threshold is a new rule for the once-a-day count too.
                    rule["fired_days"] = {}
                    _arm(rule)
        _save(root, data)
    return list_watch(root)


def set_poll(minutes, root: Path | None = None) -> dict:
    """POST /api/watchlist/poll: 0 (关), 1, 5 or 15 minutes. Stored so every tab agrees."""
    if isinstance(minutes, bool) or minutes not in POLL_CHOICES:
        raise WatchlistError("自动刷新只能选关、1 分钟、5 分钟或 15 分钟。")
    with _LOCK:
        data = _load(root)
        data["poll_minutes"] = int(minutes)
        _save(root, data)
    return list_watch(root)


def _poll_minutes(data: dict) -> int:
    """R2: a stored choice wins; otherwise 5 minutes when an enabled rule exists, else off."""
    stored = data.get("poll_minutes")
    if isinstance(stored, int) and not isinstance(stored, bool) and stored in POLL_CHOICES:
        return stored
    return POLL_DEFAULT_WITH_RULES if _has_enabled_rule(data) else 0


def _has_enabled_rule(data: dict) -> bool:
    return any(
        isinstance(item, dict) and _enabled(item)
        and any(rule.get("enabled") is not False for rule in item.get("rules") or [])
        for item in data["items"]
    )


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


TRIGGER_VISIBLE = 20
TRIGGER_STORE_CAP = 500
WINDOW_MAX_GAP = 20 * 60  # 间隔内涨跌 compares with a quote at most 20 minutes older


def _quote(raw) -> dict | None:
    """One quote from the page: a bare number (older pages, tests) or the latest answer.

    {price, prev_close, trading_day, fetched_at, delayed}. fetched_at is the server's
    fetch time (epoch seconds or ISO); a bare number counts as fetched now.
    """
    if isinstance(raw, dict):
        price = _price(raw.get("price"))
        if price is None:
            return None
        return {
            "price": price,
            "prev_close": _price(raw.get("prev_close")),
            "trading_day": str(raw.get("trading_day") or "")[:10] or None,
            "fetched_at": _stamp_of(raw.get("fetched_at")) or _now(),
            "delayed": raw.get("delayed") is True,
        }
    price = _price(raw)
    if price is None:
        return None
    return {"price": price, "prev_close": None, "trading_day": None, "fetched_at": _now(), "delayed": False}


def _stamp_of(value) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return datetime.fromtimestamp(float(value), UTC).replace(microsecond=0).isoformat()
    when = _when(value)
    return when.replace(microsecond=0).isoformat() if when else None


def _when(value) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _change_pct(price: float, base) -> float | None:
    base = _price(base)
    if base is None:
        return None
    return (price / base - 1.0) * 100.0


def _signed(value: float) -> str:
    return f"{'+' if value > 0 else ''}{value:.2f}%"


def change_label(symbol: str, *, pre_open: bool = False) -> str:
    """R1/R3: crypto 较昨收（北京 08:00）; stocks and metals 较昨收; before the open 较前一交易日收盘."""
    if pre_open:
        return "较前一交易日收盘"
    return "较昨收（北京 08:00）" if _asset_type(symbol) == "crypto" else "较昨收"


def _fire(data: dict, item: dict, rule: dict, quote: dict, detail: str, extra: dict) -> dict:
    """Record one rule trigger. The id is rule id + which arm, so a repeat poll adds nothing."""
    symbol = item["symbol"]
    ident = f"rule|{symbol}|{rule['id']}#{int(rule.get('arm_seq') or 1)}"
    found = next((row for row in data["triggers"] if row.get("id") == ident), None)
    if found:
        return {}
    label = rule_label(rule["kind"], rule["value"])
    price = quote["price"]
    when = beijing_text(quote["fetched_at"])
    row = {
        "id": ident,
        "symbol": symbol,
        "kind": "rule",
        "rule_version": data["rule_version"],
        "rule_id": rule["id"],
        "rule_kind": rule["kind"],
        "rule_value": rule["value"],
        "rule_label": label,
        "old_value": None,
        "new_value": price,
        "price": price,
        "fetched_at": quote["fetched_at"],
        "text": f"{rule_chip(rule['kind'], rule['value'])} · {detail}",
        "created_at": _now(),
        "started": False,
        "run_id": None,
        **extra,
    }
    data["triggers"].append(row)
    rule["fired_at"] = _now()
    return {
        "id": ident,
        "symbol": symbol,
        "label": label,
        "body": f"{symbol} {label}，{detail.replace(' · ', '，')}（北京 {when}）",
        "toast": f"{symbol} {label}",
    }


def _evaluate(data: dict, item: dict, quote: dict, before: dict) -> list[dict]:
    """R2. Only a quote fetched after a rule was armed may fire it; the old price never does."""
    fired: list[dict] = []
    fetched = _when(quote["fetched_at"])
    price = quote["price"]
    symbol = item["symbol"]
    for rule in item["rules"]:
        kind, value = rule.get("kind"), rule.get("value")
        if rule.get("enabled") is False or kind not in RULE_KINDS:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        armed = _when(rule.get("armed_at"))
        fresh = fetched is not None and (armed is None or fetched > armed)
        if kind in {"above", "below"}:
            holds = price >= value if kind == "above" else price <= value
            if rule.get("state") == "hit":
                if not holds:  # crossed back: armed again, the next crossing reminds again
                    _arm(rule)
                    rule["armed_at"] = quote["fetched_at"]
                continue
            if holds and fresh:
                hit = _fire(data, item, rule, quote, f"现价 {price_text(price)}", {})
                rule["state"] = "hit"
                if hit:
                    fired.append(hit)
        elif kind == "pct":
            day = quote.get("trading_day")
            change = _change_pct(price, quote.get("prev_close"))
            days = rule.get("fired_days") if isinstance(rule.get("fired_days"), dict) else {}
            rule["fired_days"] = days
            rule["state"] = "today" if day and day in days.values() else "armed"
            if change is None or not day or not fresh:
                continue
            side = "up" if change >= value else "down" if change <= -value else None
            if side is None or days.get(side) == day:
                continue
            text = f"{change_label(symbol)} {_signed(change)}"
            hit = _fire(data, item, rule, quote, text, {"change_pct": round(change, 4), "side": side,
                                                        "trading_day": day})
            days[side] = day
            rule["state"] = "today"
            rule["arm_seq"] = int(rule.get("arm_seq") or 1) + 1
            if hit:
                fired.append(hit)
        elif kind == "win":
            last = _price(before.get("last_price"))
            last_at = _when(before.get("quote_at"))
            if last is None or last_at is None or fetched is None or not fresh:
                continue
            if armed is not None and last_at < armed:
                continue  # the earlier quote predates this rule
            if (fetched - last_at).total_seconds() > WINDOW_MAX_GAP:
                continue
            change = _change_pct(price, last)
            if change is None or abs(change) < value:
                continue
            text = f"{_signed(change)}（上次 {price_text(last)}）"
            hit = _fire(data, item, rule, quote, text, {"change_pct": round(change, 4), "base_price": last})
            rule["arm_seq"] = int(rule.get("arm_seq") or 1) + 1
            if hit:
                fired.append(hit)
    return fired


def _referenced(data: dict) -> set[str]:
    return {
        str(row.get("trigger_id"))
        for key in ("checks", "admissions")
        for row in data.get(key) or []
        if isinstance(row, dict)
    }


def _prune_triggers(data: dict) -> None:
    """R6: keep at most 500. Drop the oldest unchecked ones; a checked trigger is kept."""
    rows = data["triggers"]
    if len(rows) <= TRIGGER_STORE_CAP:
        return
    keep = _referenced(data)
    excess = len(rows) - TRIGGER_STORE_CAP
    kept = []
    for row in rows:
        if excess > 0 and str(row.get("id")) not in keep:
            excess -= 1
            continue
        kept.append(row)
    data["triggers"] = kept


def _remind(fired: list[dict], root: Path | None) -> None:
    """R5: after the watchlist lock is released, queue each hit in the 定时 outbox, then deliver.

    With no sender, deliver marks it 已发送 (shown in the page only). A file first made
    here keeps decision-change and run-complete reminders as they were (off).
    """
    if not fired:
        return
    from webui import schedule

    for hit in fired:
        schedule.enqueue_reminder("price", hit["id"], hit["body"], root, symbol=hit["symbol"], quiet_create=True)
    schedule.deliver(root)


def refresh(quotes: dict, root: Path | None = None, decisions: dict | None = None, *,
            observe_local: bool = False) -> dict:
    """Store fetched prices, check rules, record decision changes. Never starts a model run.

    R4: a price change alone is no longer a trigger; only a rule that fires records one,
    whether the refresh was automatic or manual. Stored price triggers still display.
    """
    if decisions is None and observe_local:
        decisions = local_decisions()
    price_index = _indexed(quotes if isinstance(quotes, dict) else {})
    decision_index = _indexed(decisions if isinstance(decisions, dict) else {})
    fired: list[dict] = []
    with _LOCK:
        data = _load(root)
        for item in data["items"]:
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            symbol = item["symbol"]
            quote = _quote(_lookup(symbol, price_index))
            if quote is not None:
                before = {"last_price": item.get("last_price"), "quote_at": item.get("quote_at")}
                same = before["quote_at"] == quote["fetched_at"] and _price(before["last_price"]) == quote["price"]
                item["last_price"] = quote["price"]
                item["priced_at"] = _now()
                item["quote_at"] = quote["fetched_at"]
                item["prev_close"] = quote["prev_close"]
                item["trading_day"] = quote["trading_day"]
                item["delayed"] = quote["delayed"]
                change = _change_pct(quote["price"], quote["prev_close"])
                item["change_pct"] = round(change, 4) if change is not None else None
                if not same:  # the same cached answer twice is one quote, judged once
                    fired.extend(_evaluate(data, item, quote, before))
            decision = _decision(_lookup(symbol, decision_index))
            if decision is not None:
                old_decision = item.get("last_decision")
                item["last_decision"] = decision
                item["decided_at"] = _now()
                if old_decision and old_decision != decision:
                    _add_trigger(data, symbol, "decision", old_decision, decision)
        _prune_triggers(data)
        _save(root, data)
    _remind(fired, root)
    view = list_watch(root)
    view["fired"] = [{"id": hit["id"], "symbol": hit["symbol"], "label": hit["label"], "text": hit["toast"]}
                     for hit in fired]
    return view


def mark_seen(root: Path | None = None) -> dict:
    """R8: the 自选 page was opened. The unseen count is stored, so every tab agrees."""
    with _LOCK:
        data = _load(root)
        data["alerts_seen_at"] = _now()
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
    if kind == "rule":
        rule = next((row for row in item.get("rules") or [] if row.get("id") == trigger.get("rule_id")), None)
        price = _price(item.get("last_price"))
        if rule is None or rule.get("enabled") is False or price is None:
            return False
        value = rule.get("value")
        if rule.get("kind") == "above":
            return price >= value
        if rule.get("kind") == "below":
            return price <= value
        change = item.get("change_pct")
        if rule.get("kind") == "pct" and isinstance(change, (int, float)):
            return abs(change) >= value
        return False
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
