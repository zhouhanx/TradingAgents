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
import re
import secrets
import threading
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
PRICE_MAX = 1e12
PRICE_MAX_TEXT = "价格不能超过 1 万亿（1e12）。"
NUMBER_TEXT = "只能填半角数字和小数点，例如 1452.30。"
KIND_LOCKED_TEXT = "规则类型不能修改，请删除后重新添加。"
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
        "trigger_seq": 0,
        "alerts_seen_seq": 0,
    }


CORRUPT_CODE = "watchlist_corrupt"


def _backup_corrupt(path: Path, raw: bytes) -> str:
    """Copy a bad watchlist.json aside once. The same bad bytes are not copied again."""
    for old in sorted(path.parent.glob(f"{path.name}.corrupt-*")):
        try:
            if old.read_bytes() == raw:
                return old.name
        except OSError:
            continue
    stamp = datetime.now(BEIJING).strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    index = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{index}")
        index += 1
    target.write_bytes(raw)
    return target.name


def _corrupt(path: Path, raw: bytes | None) -> WatchlistError:
    """W261: a broken file is not an empty list. Back it up and refuse reads and writes."""
    if raw is None:
        return WatchlistError(
            f"读不到自选数据文件 {path.name}，没有按空列表处理，也不会写入。请检查文件权限后刷新页面。",
            409, code=CORRUPT_CODE)
    try:
        backup = _backup_corrupt(path, raw)
        kept = f"原文件已备份为 {backup}。"
    except OSError:
        kept = "备份没有成功，原文件保持不动。"
    return WatchlistError(
        f"自选数据文件 {path.name} 已损坏，没有按空列表读取，也不会写入。{kept}"
        f"请修复或移走 {path.name} 后刷新页面。",
        409, code=CORRUPT_CODE)


_TABLES = ("items", "triggers", "admissions", "checks")


def _text_or_missing(row: dict, key: str) -> bool:
    return key not in row or row[key] is None or isinstance(row[key], str)


def _rule_well_formed(rule) -> bool:
    """A stored rule is an object with a string id; its arm number, when present, is a whole number."""
    if not isinstance(rule, dict):
        return False
    ident, arm = rule.get("id"), rule.get("arm_seq")
    if not isinstance(ident, str) or not ident:
        return False
    return arm is None or (type(arm) is int and arm >= 0)


def _well_formed(data) -> bool:
    """W261/W264: every table is a list and every row in it is usable, or the whole file is
    damaged. A bad row is never skipped: the next write would drop it from the file.

    items: objects with a non-empty string ``symbol``; rows in a ``rules`` list are objects
    with a string ``id`` and a whole-number ``arm_seq``; ``rules`` itself, when present, is a
    list (a file from before rules has no ``rules`` key and loads with none). triggers: objects whose ``id`` and
    ``symbol`` are strings when present. checks and admissions: objects whose ``trigger_id``
    is a string when present.
    """
    if not isinstance(data, dict):
        return False
    if any(key in data and not isinstance(data[key], list) for key in _TABLES):
        return False
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            return False
        symbol = item.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            return False
        if "rules" in item and not isinstance(item["rules"], list):  # commit 8: "rules": "bad" is damage
            return False
        if not all(_rule_well_formed(rule) for rule in item.get("rules") or []):
            return False
    for row in data.get("triggers") or []:
        if not isinstance(row, dict) or not _text_or_missing(row, "id") or not _text_or_missing(row, "symbol"):
            return False
    for key in ("checks", "admissions"):
        for row in data.get(key) or []:
            if not isinstance(row, dict) or not _text_or_missing(row, "trigger_id"):
                return False
    return True


def _load(root: Path | None) -> dict:
    """Read watchlist.json. Missing means a new list; unreadable or broken raises (W261).

    A file whose rows do not fit (W264) is broken too: backed up once and refused, never 500.
    """
    path = _path(root)
    if not path.is_file():
        return _blank()
    try:
        raw = path.read_bytes()
    except OSError:
        raise _corrupt(path, None) from None
    try:
        # W266: a leading UTF-8 BOM is not damage (same as the price file); _save never writes one.
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _corrupt(path, raw) from None
    if not _well_formed(data):
        raise _corrupt(path, raw)
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
            item.setdefault("rules", [])
    _number_triggers(data)
    return data


def _seq(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _number_triggers(data: dict) -> None:
    """W262: every trigger has a sequence number that only grows; unseen is counted by it.

    Older files are numbered in table order. Their stored seen time becomes the highest
    number among rule triggers made at or before it.
    """
    rows = [row for row in data["triggers"] if isinstance(row, dict)]
    top = max([_seq(data.get("trigger_seq")) or 0] + [_seq(row.get("seq")) or 0 for row in rows])
    for row in rows:
        if _seq(row.get("seq")) is None:
            top += 1
            row["seq"] = top
    data["trigger_seq"] = top
    if _seq(data.get("alerts_seen_seq")) is None and data.get("alerts_seen_seq") != 0:
        seen_at = _when(data.get("alerts_seen_at"))
        data["alerts_seen_seq"] = 0 if seen_at is None else max(
            [0] + [row["seq"] for row in rows
                   if (made := _when(row.get("created_at"))) is not None and made <= seen_at])


def _stamp_trigger(data: dict, row: dict) -> dict:
    data["trigger_seq"] = (_seq(data.get("trigger_seq")) or 0) + 1
    row["seq"] = data["trigger_seq"]
    data["triggers"].append(row)
    return row


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


def _table_order(rows: list[dict], symbols: list[str]) -> list[dict]:
    """W-WL2: in a newest-first list, triggers recorded in the same second follow the
    watchlist table's row order (then the order they were recorded), not the reverse."""
    place = {symbol: index for index, symbol in enumerate(symbols)}
    out: list[dict] = []
    run: list[dict] = []

    def flush() -> None:
        run.sort(key=lambda row: (place.get(row.get("symbol"), len(place)), row.get("seq") or 0))
        out.extend(run)
        run.clear()

    for row in rows:
        if run and row.get("created_at") != run[0].get("created_at"):
            flush()
        run.append(row)
    flush()
    return out


def _visible_triggers(rows: list[dict], symbols: list[str] | None = None) -> list[dict]:
    """Newest first. Checked triggers stay even when they are older than the last twenty (R6)."""
    recent = rows[-TRIGGER_VISIBLE:]
    recent_ids = {item.get("id") for item in recent}
    sticky = [item for item in rows if item.get("checked") and item.get("id") not in recent_ids]
    # Sticky checked rows stay after the recent ones and are not mixed into their seconds.
    return _table_order(list(reversed(recent)), symbols or []) + list(reversed(sticky))


def list_watch(root: Path | None = None) -> dict:
    with _LOCK:
        data = _load(root)
    mode = _known_mode(data.get("mode") if isinstance(data.get("mode"), str) else None)
    admissions = _rows(data.get("admissions"))
    checks = _rows(data.get("checks"))
    seen = data["alerts_seen_seq"]
    triggers = []
    for item in data["triggers"]:
        if not isinstance(item, dict):
            continue
        row = _public_trigger(item, admissions, checks)
        row["seq"] = item["seq"]
        row["unseen"] = item.get("kind") == "rule" and item["seq"] > seen
        triggers.append(row)
    return {
        "rule_version": data["rule_version"],
        "mode": mode,
        "mode_label": MODE_LABEL.get(mode) if mode else "状态未知",
        "items": [_public_item(item) for item in data["items"] if isinstance(item, dict)],
        "triggers": triggers,
        "visible_triggers": _visible_triggers(
            triggers, [str(item.get("symbol")) for item in data["items"] if isinstance(item, dict)]),
        "note": _watch_note(mode),
        "poll_minutes": _poll_minutes(data),
        "poll_stored": isinstance(data.get("poll_minutes"), int) and data.get("poll_minutes") in POLL_CHOICES,
        "has_rules": _has_enabled_rule(data),
        "rule_cap": RULE_CAP,
        "trigger_total": len(triggers),
        "unseen_count": sum(1 for row in triggers if row["unseen"]),
        "alerts_seen_at": data.get("alerts_seen_at"),
        "alerts_seen_seq": seen,
        "trigger_seq": data["trigger_seq"],
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


def _half_up(number: float, places: int) -> Decimal:
    """Round like the page's toFixed (the exact binary value, ties away from zero)."""
    return Decimal(number).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


DECIMALS_MAX = 6
DECIMALS_TEXT = "小数最多 6 位。"
_PLAIN = re.compile(r"^\+?(\d+)(?:\.(\d*))?$|^\+?\.(\d+)$")


def typed_text(raw) -> str | None:
    """W259 (PM ruling): the number as the user typed it, tidied but never rounded.

    "1452.30" stays "1452.30"; "0190.5" -> "190.5"; ".5" -> "0.5"; "5." -> "5". A JSON number
    has no typed form: 3.0 -> "3", 1452.3 -> "1452.3". Exponent forms ("1e3") are written out.
    None when it is not a finite number.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        number = float(raw)
        if not math.isfinite(number):
            return None
        if number.is_integer():
            return str(int(number))
        text = format(Decimal(repr(number)), "f")
    elif isinstance(raw, str):
        text = raw.strip()
    else:
        return None
    match = _PLAIN.match(text)
    if not match:
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
        if not number.is_finite():
            return None
        text = format(number, "f")
        match = _PLAIN.match(text.lstrip("-")) if not text.startswith("-") else None
        if not match:
            return None
    whole, frac = (match.group(1), match.group(2)) if match.group(3) is None else ("0", match.group(3))
    whole = whole.lstrip("0") or "0"
    return f"{whole}.{frac}" if frac else whole


def _rule_text(rule: dict) -> str:
    """The stored typed text; rules saved before it existed get theirs from the number.

    W270-3: a value_text that does not stand for the stored value is ignored and the number
    itself is shown, so the label always names the threshold that is actually compared.
    "Stands for" means the text, read as a decimal, turns into exactly the stored float:
    1452.30 for 1452.3 yes, 1452.31 for 1452.3 no. (A float keeps about 17 digits, so a
    typed 999999999999.123456 is the source of its float, not a mismatch.)
    """
    text = rule.get("value_text")
    value = rule.get("value")
    if isinstance(text, str) and typed_text(text) == text and _number_of(value) is not None:
        try:
            if float(Decimal(text)) == float(value):
                return text
        except (InvalidOperation, ValueError, OverflowError):
            pass
    return typed_text(value) or ""


def _number_of(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _decimal_of(rule: dict) -> Decimal | None:
    """W268: the threshold a rule shows, as an exact decimal (from _rule_text)."""
    text = _rule_text(rule)
    try:
        number = Decimal(text) if text else None
    except InvalidOperation:
        return None
    return number if number is not None and number.is_finite() else None


def threshold_text(text: str) -> str:
    """A price threshold in a label: every typed digit, padded with zeros up to the page's
    price format (>= 1000 one decimal, >= 1 two, else four significant), never rounded.
    1452.35 -> 1452.35, 1452.30 -> 1452.30, 190 -> 190.00, 0.5 -> 0.5000."""
    whole, _, frac = text.partition(".")
    if len(whole) >= 4:
        page = 1
    elif whole != "0":
        page = 2
    else:
        lead = len(frac) - len(frac.lstrip("0"))
        page = lead + 4 if frac.strip("0") else 3
    places = max(page, len(frac))
    return f"{whole}.{frac.ljust(places, '0')}"


def price_text(value: float) -> str:
    """The page's price format (>= 1000 one decimal, >= 1 two, else four significant).

    Used for quotes. Rule thresholds use threshold_text (the typed digits).
    """
    number = float(value)
    size = abs(number)
    if not math.isfinite(number):
        return "—"
    if size >= 1000:
        return format(_half_up(number, 1), "f")
    if size >= 1:
        return format(_half_up(number, 2), "f")
    if size == 0:
        return "0.000"
    places = 3 - Decimal(number).adjusted()
    rounded = _half_up(number, places)
    if rounded.adjusted() > Decimal(number).adjusted():  # 0.99995 -> 1.000, as toPrecision(4)
        rounded = _half_up(number, places - 1)
    return format(rounded, "f")


def rule_label(kind: str, value, text: str | None = None) -> str:
    """``text`` is the typed number (W259); without it the number's own digits are used."""
    shown = text if text is not None else (typed_text(value) or "")
    price = threshold_text(shown) if shown else ""
    return {
        "above": f"高于 {price}",
        "below": f"低于 {price}",
        "pct": f"涨跌 ≥ {shown}%（较昨收）",
        "win": f"一个刷新间隔内涨跌 ≥ {shown}%",
    }.get(kind, "")


def rule_chip(kind: str, value, text: str | None = None) -> str:
    shown = text if text is not None else (typed_text(value) or "")
    if kind == "pct":
        return f"涨跌 ≥ {shown}%"
    if kind == "win":
        return f"间隔内涨跌 ≥ {shown}%"
    return rule_label(kind, value, shown)


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
    kind = rule.get("kind")
    hit_at = beijing_text(rule.get("fired_at"))
    if rule.get("state") == "hit" and kind in {"above", "below"}:
        side = "以下" if kind == "above" else "以上"
        return f"已触发（北京 {hit_at}）。价格回到 {threshold_text(_rule_text(rule))} {side}后重新生效。", "hit"
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
        "value_text": _rule_text(rule) if usable else None,
        "enabled": rule.get("enabled") is not False,
        "state": state,
        "state_label": text,
        "label": rule_label(kind, value, _rule_text(rule)) if usable else "",
        "chip": rule_chip(kind, value, _rule_text(rule)) if usable else "",
        "fired_at": rule.get("fired_at"),
    }


_ASCII_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]{1,6})?")
_ASCII_LONG = re.compile(r"[0-9]+\.[0-9]{7,}")
_ASCII_NEGATIVE = re.compile(r"-[0-9]+(?:\.[0-9]+)?")


def _input_text(raw) -> str | None:
    """The submitted number as text. A JSON number is written as Python writes it (190 -> "190",
    1452.3 -> "1452.3", 1e-07 stays "1e-07" and is refused). None for anything else."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, float):
        return repr(raw) if math.isfinite(raw) else None
    return raw if isinstance(raw, str) else None


def _rule_value(kind, raw) -> tuple[float, str]:
    r"""(number, tidied typed text). W269/W270-1/W272 (PM ruling): after the surrounding
    whitespace and one leading ``+`` are removed, only ASCII ``[0-9]+(\.[0-9]{1,6})?`` is a
    number. Full-width or other scripts' digits, ``_``, ``.5``, ``5.`` and exponent forms
    answer 400 bad_number; more than 6 decimals answers bad_decimals. The number is the
    decimal of the text; > 0, <= 1e12 (prices) and 0.1-50 (percentages) are checked on that
    decimal, so 1000000000000.000001 is over the cap.
    """
    if kind not in RULE_KINDS:
        raise WatchlistError("请选择规则类型。")
    price_kind = kind in {"above", "below"}
    range_error = (WatchlistError(PRICE_TEXT, code="bad_price") if price_kind
                   else WatchlistError(PCT_TEXT, code="bad_pct"))
    raw_text = _input_text(raw)
    if raw_text is None:
        raise range_error
    text = raw_text.strip()
    if text.startswith("+"):
        text = text[1:]
    if not _ASCII_DECIMAL.fullmatch(text):
        if _ASCII_LONG.fullmatch(text):
            raise WatchlistError(DECIMALS_TEXT, code="bad_decimals")
        if _ASCII_NEGATIVE.fullmatch(text) or not any(ch.isdecimal() for ch in text):
            raise range_error  # -5, empty, abc, inf, nan: not a positive number
        raise WatchlistError(NUMBER_TEXT, code="bad_number")
    whole, _, frac = text.partition(".")
    text = f"{whole.lstrip('0') or '0'}.{frac}" if frac else (whole.lstrip("0") or "0")
    number = Decimal(text)
    if number <= 0:
        raise range_error
    if price_kind:
        if number > PRICE_MAX_DECIMAL:
            raise WatchlistError(PRICE_MAX_TEXT, code="bad_price")
    elif number < PCT_MIN or number > PCT_MAX:
        raise range_error
    return float(number), text


PRICE_MAX_DECIMAL = Decimal("1000000000000")
PCT_MIN, PCT_MAX = Decimal("0.1"), Decimal("50")


def _same_rule(rule: dict, kind: str, text: str) -> bool:
    """W268 (PM ruling): the same kind and exactly the same decimal. 1452.3 and 1452.30 are
    the same threshold; 1452.300001 is not 1452.3. No float tolerance."""
    return rule.get("kind") == kind and _decimal_of(rule) == Decimal(text)


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
            value, text = _rule_value(kind, body.get("value"))
            for rule in rules:
                if _same_rule(rule, kind, text):
                    raise WatchlistError(f"已有一条「{rule_label(kind, rule.get('value'), _rule_text(rule))}」，不用重复加。",
                                         code="duplicate")
            rule = {
                "id": f"r{secrets.token_hex(4)}",
                "kind": kind,
                "value": value,
                "value_text": text,
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
                asked = body.get("kind")
                if asked not in (None, "") and asked != kind:  # W260: a rule keeps its type
                    raise WatchlistError(KIND_LOCKED_TEXT, code="kind_locked")
                value, text = _rule_value(kind, body.get("value"))
                for other in rules:
                    if other is not rule and _same_rule(other, kind, text):
                        raise WatchlistError(
                            f"已有一条「{rule_label(kind, other.get('value'), _rule_text(other))}」，不用重复加。",
                            code="duplicate")
                same = _same_rule(rule, kind, text)
                rule["value_text"] = text  # 1452.3 -> 1452.30 changes only how it reads
                if not same:
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
    return _stamp_trigger(data, row)


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
    label = rule_label(rule["kind"], rule["value"], _rule_text(rule))
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
        "rule_value_text": _rule_text(rule),
        "text": f"{rule_chip(rule['kind'], rule['value'], _rule_text(rule))} · {detail}",
        "created_at": _now(),
        "started": False,
        "run_id": None,
        **extra,
    }
    _stamp_trigger(data, row)
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


def _seen(row: dict, seen_seq: int) -> bool:
    """Seen as the page counts it: only rule triggers are ever unseen, until the seen mark passes them."""
    return row.get("kind") != "rule" or (_seq(row.get("seq")) or 0) <= seen_seq


def _prune_triggers(data: dict) -> None:
    """R6/W263/W265 (PM ruling): keep at most 500 in total, deleting in three tiers.

    1. the oldest seen triggers that no check or admission refers to;
    2. then the oldest unseen ones that nothing refers to;
    3. only then the oldest referenced ones (by time), seen or not.
    The newest 20 (the list on the page, which holds the hits just recorded) are never
    deleted. Check and admission records of a deleted trigger stay as history.
    """
    rows = data["triggers"]
    excess = len(rows) - TRIGGER_STORE_CAP
    if excess <= 0:
        return
    seen_seq = _seq(data.get("alerts_seen_seq")) or 0
    referenced = _referenced(data)
    older = range(max(len(rows) - TRIGGER_VISIBLE, 0))

    def tier(index: int) -> int:
        row = rows[index]
        if str(row.get("id")) in referenced:
            return 2
        return 0 if _seen(row, seen_seq) else 1

    gone = set(sorted(older, key=lambda index: (tier(index), index))[:excess])
    data["triggers"] = [row for index, row in enumerate(rows) if index not in gone]


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


def mark_seen(root: Path | None = None, upto_seq=None) -> dict:
    """R8/W262: the 自选 page showed triggers up to ``upto_seq``. The seen mark is stored,
    so every tab agrees, and it is a trigger number rather than a time: a trigger made in
    the same second by another tab's refresh stays unseen until a page has shown it.
    Without ``upto_seq`` everything recorded so far counts as seen. The mark never goes back.
    """
    # W267: a non-negative integer only. true/false, 0.0 and "3" are not trigger numbers.
    if upto_seq is not None and (type(upto_seq) is not int or upto_seq < 0):
        raise WatchlistError("upto_seq 须为不小于 0 的整数。")
    with _LOCK:
        data = _load(root)
        top = data["trigger_seq"]
        upto = top if upto_seq is None else min(upto_seq, top)
        data["alerts_seen_seq"] = max(data["alerts_seen_seq"], upto)
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
