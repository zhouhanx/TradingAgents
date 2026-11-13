"""V2-KLINE: K 线行情 data for the console's chart page.

``kline(symbol, tf)`` answers ``/api/kline``: about one year of daily bars,
resampled to weekly or monthly candles on request. Stocks and metals/commodity
futures read the project's existing Yahoo source (the same canonical symbol the
analysis uses, e.g. XAUUSD -> GC=F). Crypto reads OKX first, then Coinbase, then
Yahoo. Answers are cached in memory per (provider chain, symbol) for a short TTL.
V2-KLINE-RT: ``latest(symbol)`` answers ``/api/kline/latest`` with the newest
(still forming) daily bar. The page polls it while visible: crypto every few
seconds from OKX's public REST (Coinbase, then Yahoo as fallbacks; no keys),
stocks and metals/futures every 60 s from Yahoo (a delayed quote, not ticks).
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from tradingagents.dataflows import ohlc_check
from tradingagents.dataflows.symbols import crypto_base, market_code, normalize_symbol
from tradingagents.dataflows.vendors import sina

TIMEFRAMES = ("1D", "1W", "1M", "1H", "5m")
INTRADAY = ("1H", "5m")
DEFAULT_TIMEFRAME = "1D"
HISTORY_DAYS = 366
CACHE_TTL_SECONDS = 300
HTTP_TIMEOUT = 8.0

OKX_URL = "https://www.okx.com/api/v5/market/history-candles"
OKX_LIVE_URL = "https://www.okx.com/api/v5/market/candles"
# V2-KLINE-RT: how often the page asks for the newest bar, and how long the
# server reuses one answer (several tabs share it; the vendor sees at most one
# request per window).
RT_POLL_SECONDS = {"crypto": 5, "stock": 60, "commodity": 60}
RT_CACHE_SECONDS = {"crypto": 3, "stock": 55, "commodity": 55}
COINBASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"

_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_lock = threading.Lock()


# W188: what a ticker may contain before any source is asked. Letters stay
# valid (ABC may be a real US ticker); the source decides whether it exists.
CODE_RE = re.compile(r"[A-Za-z0-9^][A-Za-z0-9.\-=^+]{0,31}")
FORMAT_INVALID = "代码格式无效：A 股用 6 位数字，港股 4-5 位数字，美股用英文代码（可含 . - = ^）。"


class KlineError(ValueError):
    """A request the page should show as a plain message."""


RATE_LIMITED = "rate_limited"
RATE_LIMITED_TEXT = "行情源限流，稍后重试。"


class KlineRateLimited(KlineError):
    """Every source failed and at least one said "too many requests" (V2-WATCHLIST R7)."""

    code = RATE_LIMITED


def _rate_limited(exc: BaseException | None) -> bool:
    """HTTP 429, yfinance's YFRateLimitError, or OKX code 50011 anywhere in the chain."""
    seen = 0
    while exc is not None and seen < 8:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429 or type(exc).__name__ == "YFRateLimitError":
            return True
        if str(exc).startswith("OKX 50011"):
            return True
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return False


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _bar(ts: int, o, h, l, c, v) -> dict:  # noqa: E741
    return {"time": int(ts), "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v or 0)}


def _since() -> int:
    return int((datetime.now(UTC) - timedelta(days=HISTORY_DAYS)).timestamp())


def fetch_okx(base: str) -> list[dict]:
    """OKX daily candles for BASE-USDT, newest-first pages of 100, walked back one year."""
    since, after, bars = _since(), None, {}
    for _ in range(6):
        params = {"instId": f"{base}-USDT", "bar": "1Dutc", "limit": "100"}
        if after:
            params["after"] = str(after)
        resp = requests.get(OKX_URL, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("code")) != "0":
            raise RuntimeError(f"OKX: {body.get('msg') or body.get('code')}")
        rows = body.get("data") or []
        if not rows:
            break
        for row in rows:
            ts = int(row[0]) // 1000
            if ts >= since:
                bars[ts] = _bar(ts, row[1], row[2], row[3], row[4], row[5])
        after = rows[-1][0]
        if int(after) // 1000 < since:
            break
    return [bars[k] for k in sorted(bars)]


def fetch_coinbase(base: str) -> list[dict]:
    """Coinbase Exchange daily candles for BASE-USD, 300 per request."""
    bars, end = {}, datetime.now(UTC)
    start_limit = end - timedelta(days=HISTORY_DAYS)
    while end > start_limit:
        start = max(start_limit, end - timedelta(days=299))
        resp = requests.get(
            COINBASE_URL.format(product=f"{base}-USD"),
            params={"granularity": 86400, "start": start.isoformat(), "end": end.isoformat()},
            headers={"User-Agent": "TradingAgents-WebUI"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        for t, low, high, op, close, vol in resp.json():
            bars[int(t)] = _bar(t, op, high, low, close, vol)
        end = start
    return [bars[k] for k in sorted(bars)]


def fetch_yahoo(canonical: str) -> list[dict]:
    """The project's existing Yahoo source: one year of daily history."""
    import yfinance as yf

    from tradingagents.dataflows.vendors.yahoo.common import yf_retry

    frame = yf_retry(lambda: yf.Ticker(canonical).history(period="1y", interval="1d", auto_adjust=True, actions=False))
    if frame is None or frame.empty:
        return []
    out = []
    for idx, row in frame.iterrows():
        if any(row.get(k) != row.get(k) for k in ("Open", "High", "Low", "Close")):  # NaN
            continue
        day = datetime(idx.year, idx.month, idx.day, tzinfo=UTC)
        out.append(_bar(day.timestamp(), row["Open"], row["High"], row["Low"], row["Close"], row.get("Volume", 0)))
    return out


def sina_symbol(canonical: str) -> str | None:
    return sina.sina_symbol(canonical)


def fetch_sina(ident: str) -> list[dict]:
    """About one year of unadjusted daily bars from Sina (shared vendor module)."""
    prefix, code = ident[:2], ident[2:]
    suffix = {"sh": ".SS", "sz": ".SZ", "bj": ".BJ"}[prefix]
    return sina.bars_epoch(code + suffix, _since())


def route(symbol: str) -> dict:
    """Which asset type and provider chain a symbol reads from."""
    raw = (symbol or "").strip()
    if not raw:
        raise KlineError("请输入标的代码。")
    if len(raw) > 32 or any(ch.isspace() for ch in raw):
        raise KlineError(FORMAT_INVALID)
    if not raw.isascii():
        hit = CN_NAMES.get(raw)
        if not hit:
            raise KlineError("没有找到这个名称，请用 6 位代码查询（港股 4-5 位，美股英文代码）。")
        raw = hit[0]
    if not CODE_RE.fullmatch(raw):
        raise KlineError(FORMAT_INVALID)
    if raw.isdigit() and len(raw) <= 3:
        raise KlineError("港股请用 4-5 位代码，例如 0700 或 00700；A 股请用 6 位代码。")
    if raw.isdigit() and len(raw) == 6 and not market_code(raw):
        raise KlineError("暂不支持这个 6 位代码（支持沪市 6/90、深市 0/2/3、北交所 4/8/92 开头）。")
    canonical = normalize_symbol(raw)
    base = crypto_base(raw) or crypto_base(canonical)
    if base:
        return {"asset_type": "crypto", "canonical": f"{base}-USD", "base": base,
                "providers": [("okx", f"{base}-USDT"), ("coinbase", f"{base}-USD"), ("yahoo", f"{base}-USD")]}
    asset = "commodity" if canonical.endswith("=F") else "stock"
    sina = sina_symbol(canonical)
    if canonical.endswith(".BJ"):
        providers = [("sina", sina)]
    elif sina:
        providers = [("yahoo", canonical), ("sina", sina)]
    else:
        providers = [("yahoo", canonical)]
    return {"asset_type": asset, "canonical": canonical, "base": None, "providers": providers}


def _fetcher(name: str) -> Callable[[str], list[dict]]:
    return {"okx": fetch_okx, "coinbase": fetch_coinbase, "yahoo": fetch_yahoo, "sina": fetch_sina}[name]


def resample(bars: list[dict], tf: str) -> list[dict]:
    # Keep one canonical daily row per timestamp before grouping.  A provider
    # can return the forming day more than once (and some fallback chains may
    # overlap at their boundary); feeding duplicate rows into the aggregate
    # would make volume depend on fetch order rather than on the daily OHLCV.
    by_time = {int(bar["time"]): dict(bar) for bar in bars}
    ordered = [by_time[t] for t in sorted(by_time)]
    if tf == "1D":
        return ordered
    out: list[dict] = []
    key_of = (lambda d: d.isocalendar()[:2]) if tf == "1W" else (lambda d: (d.year, d.month))
    current_key = None
    for bar in ordered:
        key = key_of(datetime.fromtimestamp(bar["time"], UTC))
        if key != current_key:
            out.append(dict(bar))
            current_key = key
        else:
            agg = out[-1]
            agg["high"] = max(agg["high"], bar["high"])
            agg["low"] = min(agg["low"], bar["low"])
            agg["close"] = bar["close"]
            agg["volume"] += bar["volume"]
            if bar.get("ohlc_fixed"):
                agg["ohlc_fixed"] = True
    return out


MAYBE_NOT_UPDATED = "可能未更新"


def _is_yahoo_sina(info: dict) -> bool:
    return [name for name, _ in info["providers"]] == ["yahoo", "sina"]


def _daily(info: dict) -> tuple[str, str, list[dict], list[str]]:
    """The first provider's daily bars; SH/SZ also check Yahoo against Sina (AUDIT-FIX-06)."""
    errors = []
    for name, ident in info["providers"]:
        try:
            bars = _fetcher(name)(info["base"] if name in ("okx", "coinbase") else ident)
        except Exception as exc:  # noqa: BLE001 - the next provider is the fallback
            errors.append(f"{name}: {type(exc).__name__}" + ("" if _is_network(exc) else " 无此代码"))
            continue
        if bars:
            if name == "yahoo" and _is_yahoo_sina(info):
                name, ident, bars = _fresher_of_yahoo_and_sina(info, ident, bars, errors)
            ohlc_check.fix_bars(bars, info["canonical"])
            return name, ident, bars, errors
        errors.append(f"{name}: 无数据")
    raise KlineError(_friendly(info, errors))


def _fresher_of_yahoo_and_sina(info: dict, yahoo_ident: str, yahoo_bars: list[dict], errors: list[str]):
    """PM rule: Yahoo is stale when its last bar is earlier than Sina's; then Sina is
    used. When Sina has nothing, Yahoo is used and noted 可能未更新. No day threshold."""
    sina_ident = dict(info["providers"])["sina"]
    try:
        sina_bars = fetch_sina(sina_ident)
    except Exception as exc:  # noqa: BLE001 - Yahoo stays, unverified
        sina_bars = []
        errors.append(f"sina: {type(exc).__name__}")
    if not sina_bars:
        errors.append(MAYBE_NOT_UPDATED)
        return "yahoo", yahoo_ident, yahoo_bars
    if _bar_day(yahoo_bars[-1]) < _bar_day(sina_bars[-1]):
        errors.append("yahoo: 数据过期")
        return "sina", sina_ident, sina_bars
    return "yahoo", yahoo_ident, yahoo_bars





def _quote_bar(bars: list[dict]) -> tuple[dict | None, str | None]:
    """Newest positive bar. A newer zero bar is not a quote."""
    if not bars:
        return None, None
    if _priced_bar(bars[-1]):
        return bars[-1], None
    earlier = [bar for bar in bars if _priced_bar(bar)]
    if not earlier:
        return None, "没有正价格"
    return earlier[-1], "最新一根没有正价格，显示上一根。"


def _priced_bar(bar: dict) -> bool:
    """A candle needs positive OHLC. Zero is not a price and must not stretch the chart."""
    if not isinstance(bar, dict):
        return False
    for key in ("open", "high", "low", "close"):
        value = bar.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if value != value or value <= 0:
            return False
    return True


def _priced_bars(bars: list[dict]) -> tuple[list[dict], int]:
    kept = [bar for bar in bars if _priced_bar(bar)]
    return kept, len(bars) - len(kept)


def _unpriced_note(count: int) -> str:
    return f"有{count}根K线没有正价格，未画入图表。"


def history_span_label(bars: list[dict]) -> str:
    """Label the change from the bars actually returned. Do not call a short series one year."""
    times = sorted(int(bar["time"]) for bar in bars if isinstance(bar, dict) and isinstance(bar.get("time"), int))
    if not times:
        return ""
    if len(times) == 1:
        return "仅1根"
    seconds = times[-1] - times[0]
    if seconds < 50 * 60:
        minutes = max(1, round(seconds / 60))
        return f"近{minutes}分钟"
    if seconds < 20 * 3600:
        hours = max(1, round(seconds / 3600))
        return f"近{hours}小时"
    days = max(1, round(seconds / 86400))
    if days >= 330:
        return "近1年"
    return f"近{days}天"


def _bar_day(bar: dict):
    return datetime.fromtimestamp(bar["time"], UTC).date()


# Exceptions meaning the source answered "no such symbol" (yfinance's missing
# prices / time zone / ticker answers, or HTTP 404). Anything else, a bare
# VendorUnavailableError included, is treated as the source being unreachable.
_NOT_FOUND_MARKERS = ("Missing", "Delisted", "NotFound", "NoMarketData")


def _is_network(exc: BaseException | None) -> bool:
    """Whether a fetch failed in transit rather than the source answering that it
    has no such symbol (W188)."""
    seen = 0
    while exc is not None and seen < 8:
        name = type(exc).__name__
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int) and (status >= 500 or status == 429):
            return True
        if any(m in name for m in _NOT_FOUND_MARKERS):
            # AUDIT-FIX-07: yfinance also raises YFPricesMissingError for an error
            # answer (e.g. HTTP 503); its debug_info then carries the status code.
            return _carries_error_status(exc)
        if status == 404:
            return False
        exc, seen = exc.__cause__ or exc.__context__, seen + 1
    return True


def _carries_error_status(exc: BaseException) -> bool:
    """A "missing" exception that is really a failed answer: a non-404 status in its text."""
    text = " ".join(str(part) for part in (getattr(exc, "debug_info", None), exc) if part)
    codes = [int(c) for c in re.findall(r"status_code\D{0,3}(\d{3})", text)]
    return any(c != 404 for c in codes) or ("status_code" in text and not codes)


def _friendly(info: dict, errors: list[str]) -> str:
    """Plain Chinese for a failed fetch; never raw vendor exception names."""
    code = info["canonical"]
    if code.endswith(".BJ") and code[0] in "48" and all(e.endswith("无数据") for e in errors):
        return f"行情源近一年没有 {code} 的数据：北交所 4/8 开头的老代码已切换为 920 开头的新代码，请用新代码查询。"
    if not errors or any(not (e.endswith("无数据") or e.endswith("无此代码")) for e in errors):
        return f"行情源暂时连不上（{info['canonical']}），请稍后再试。"
    empty = all(e.endswith("无数据") for e in errors)
    if sina_symbol(info["canonical"]) and empty:
        return f"行情源暂时没有这只股票的数据（代码格式正确：{info['canonical']}）。"
    if info["asset_type"] == "stock":
        return f"没有找到这个代码（{info['canonical']}），A 股请用 6 位代码，港股用 4-5 位代码，美股用英文代码。"
    return f"没有拿到 {info['canonical']} 的行情数据。"



# V2-KLINE-INTRADAY. Hour and 5-minute bars are native vendor candles, not a
# resample of the daily series. Yahoo minute history is short; Beijing is Sina
# only and labelled unadjusted. Closed sessions and the A-share lunch break are
# not filled with empty candles.
YAHOO_INTRADAY = {"1H": ("60d", "1h"), "5m": ("5d", "5m")}
SINA_INTRADAY = {"1H": ("60", 400), "5m": ("5", 240)}
OKX_INTRADAY = {"1H": ("1H", 4, 14), "5m": ("5m", 3, 1)}
COINBASE_INTRADAY = {"1H": (3600, 14), "5m": (300, 1)}
INTRADAY_SECONDS = {"1H": 3600, "5m": 300}
SPAN_NOTE = {
    ("stock", "1H"): "小时线，请求近 60 天；沪深可回退新浪，北交所仅新浪，长度以实际返回为准。",
    ("commodity", "1H"): "小时线，请求近 60 天，长度以实际返回为准。",
    ("crypto", "1H"): "小时线，加密货币请求近 14 天（OKX，其次 Coinbase），长度以实际返回为准。",
    ("stock", "5m"): "5分钟线，请求近 5 天；新浪分钟更短，长度以实际返回为准。午休和收盘后不补空K。",
    ("commodity", "5m"): "5分钟线，请求近 5 天，长度以实际返回为准。非交易时段不补空K。",
    ("crypto", "5m"): "5分钟线，加密货币请求近 24 小时，长度以实际返回为准。",
}
SESSION_NOTE = "非交易时段和午休不补空K，只显示数据源给出的K线。"
HISTORY_LABEL = {("stock", "1H"): "近60天", ("commodity", "1H"): "近60天", ("crypto", "1H"): "近14天",
                 ("stock", "5m"): "近5天", ("commodity", "5m"): "近5天", ("crypto", "5m"): "近24小时"}


def _epoch(idx) -> int:
    tz = getattr(idx, "tzinfo", None)
    if tz is None:
        return int(idx.replace(tzinfo=UTC).timestamp())
    return int(idx.timestamp())


def fetch_yahoo_intraday(canonical: str, tf: str) -> list[dict]:
    import yfinance as yf

    from tradingagents.dataflows.vendors.yahoo.common import yf_retry

    period, interval = YAHOO_INTRADAY[tf]
    frame = yf_retry(lambda: yf.Ticker(canonical).history(period=period, interval=interval, auto_adjust=True, actions=False))
    if frame is None or frame.empty:
        return []
    out = []
    for idx, row in frame.iterrows():
        if any(row.get(k) != row.get(k) for k in ("Open", "High", "Low", "Close")):
            continue
        out.append(_bar(_epoch(idx), row["Open"], row["High"], row["Low"], row["Close"], row.get("Volume", 0)))
    return out


def fetch_sina_intraday(ident: str, tf: str) -> list[dict]:
    from zoneinfo import ZoneInfo

    scale, count = SINA_INTRADAY[tf]
    clock = ZoneInfo("Asia/Shanghai")
    out = []
    for row in sina.scale_rows(ident, scale, count):
        day = row["day"]
        if len(day) <= 10:
            local = datetime.strptime(day[:10], "%Y-%m-%d").replace(tzinfo=clock)
        else:
            local = datetime.strptime(day[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=clock)
        out.append(_bar(local.timestamp(), row["open"], row["high"], row["low"], row["close"], row["volume"]))
    return out


def fetch_okx_intraday(base: str, tf: str) -> list[dict]:
    bar, pages, days = OKX_INTRADAY[tf]
    since = int((datetime.now(UTC) - timedelta(days=days)).timestamp())
    after, bars = None, {}
    for _ in range(pages):
        params = {"instId": f"{base}-USDT", "bar": bar, "limit": "100"}
        if after:
            params["after"] = str(after)
        resp = requests.get(OKX_URL, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        if str(body.get("code")) != "0":
            raise RuntimeError(f"OKX: {body.get('msg') or body.get('code')}")
        rows = body.get("data") or []
        if not rows:
            break
        for row in rows:
            ts = int(row[0]) // 1000
            if ts >= since:
                bars[ts] = _bar(ts, row[1], row[2], row[3], row[4], row[5])
        after = rows[-1][0]
        if int(after) // 1000 < since:
            break
    return [bars[k] for k in sorted(bars)]


def fetch_coinbase_intraday(base: str, tf: str) -> list[dict]:
    granularity, days = COINBASE_INTRADAY[tf]
    end = datetime.now(UTC)
    start_limit = end - timedelta(days=days)
    bars = {}
    # Coinbase returns at most 300 candles per request.
    step = timedelta(seconds=granularity * 299)
    while end > start_limit:
        start = max(start_limit, end - step)
        resp = requests.get(
            COINBASE_URL.format(product=f"{base}-USD"),
            params={"granularity": granularity, "start": start.isoformat(), "end": end.isoformat()},
            headers={"User-Agent": "TradingAgents-WebUI"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        for t, low, high, op, close, vol in resp.json():
            bars[int(t)] = _bar(t, op, high, low, close, vol)
        end = start
    return [bars[k] for k in sorted(bars)]


def _fetch_intraday(name: str, ident: str, info: dict, tf: str) -> list[dict]:
    if name == "okx":
        return fetch_okx_intraday(info["base"], tf)
    if name == "coinbase":
        return fetch_coinbase_intraday(info["base"], tf)
    if name == "yahoo":
        return fetch_yahoo_intraday(ident, tf)
    return fetch_sina_intraday(ident, tf)


def _pick_intraday(info: dict, tf: str) -> tuple[str, str, list[dict], list[str]]:
    errors: list[str] = []
    yahoo_ident = yahoo_bars = None
    for name, ident in info["providers"]:
        try:
            bars = _fetch_intraday(name, ident, info, tf)
        except Exception as exc:  # noqa: BLE001 - try the next source
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        if not bars:
            errors.append(f"{name}: 无数据")
            continue
        ohlc_check.fix_bars(bars, info["canonical"])
        if name == "yahoo" and _is_yahoo_sina(info):
            yahoo_ident, yahoo_bars = ident, bars
            continue
        if yahoo_bars and bars and bars[-1]["time"] <= yahoo_bars[-1]["time"]:
            return "yahoo", yahoo_ident, yahoo_bars, errors
        if yahoo_bars and bars and bars[-1]["time"] > yahoo_bars[-1]["time"]:
            errors.append("yahoo: 数据过期")
        return name, ident, bars, errors
    if yahoo_bars:
        return "yahoo", yahoo_ident, yahoo_bars, errors
    raise KlineError(_friendly(info, errors))


def intraday(symbol: str, tf: str, *, refresh: bool = False) -> dict:
    info = route(symbol)
    if info["canonical"].endswith(".BJ") and not sina_symbol(info["canonical"]):
        raise KlineError(f"北交所 {info['canonical']} 只有新浪来源，当前代码无法查询。")
    key = ("intraday", tf, info["canonical"])
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    cached = bool(not refresh and hit and now - hit[0] < CACHE_TTL_SECONDS)
    if cached:
        chosen = hit[1]
    else:
        provider, ident, bars, errors = _pick_intraday(info, tf)
        chosen = {"provider": provider, "provider_symbol": ident, "bars": bars, "fallback_errors": errors, "fetched_at": int(now)}
        with _lock:
            _cache[key] = (now, chosen)
    bars, omitted_unpriced = _priced_bars(chosen["bars"])
    bars = with_sessions(bars, info["canonical"], info["asset_type"])
    stale = _stale_bar(bars[-1]["time"] if bars else None, tf, now)
    ohlc_fixed = sum(1 for b in bars if b.get("ohlc_fixed"))
    payload = {
        "symbol": (symbol or "").strip().upper(),
        "canonical": info["canonical"],
        "asset_type": info["asset_type"],
        "tf": tf,
        "provider": chosen["provider"],
        "provider_symbol": chosen["provider_symbol"],
        "fallback_errors": chosen["fallback_errors"],
        "is_future": info["asset_type"] == "commodity",
        "adjusted": chosen["provider"] != "sina",
        "delayed": quote_delayed(info["asset_type"], chosen["provider"]),
        "source_label": "新浪（非官方，不复权）" if chosen["provider"] == "sina" else None,
        "fallback": chosen["provider"] == "sina" and not info["canonical"].endswith(".BJ"),
        "cached": cached,
        "ohlc_fixed": ohlc_fixed,
        "freshness_note": MAYBE_NOT_UPDATED if MAYBE_NOT_UPDATED in chosen["fallback_errors"] else None,
        "fetched_at": chosen["fetched_at"],
        "daily_bars": [],
        "bars": bars,
        "span_note": SPAN_NOTE[(info["asset_type"], tf)],
        "session_note": SESSION_NOTE,
        "history_label": history_span_label(bars) or HISTORY_LABEL[(info["asset_type"], tf)],
        "stale_bar": stale,
    }
    if omitted_unpriced:
        payload["unpriced_note"] = _unpriced_note(omitted_unpriced)
    if ohlc_fixed:
        payload["ohlc_note"] = ohlc_check.NOTE_ZH
    return payload


# V2-KLINE-INTRADAY (Eric 2026-11-13): the page shows Beijing clock times, and the
# date on the axis and in the readout is the trading day. Each intraday bar gets
# ``trading_day`` (YYYY-MM-DD) and ``chart_time``: the trading day's 00:00 UTC plus
# the time since that session's start, so the chart library's day ticks fall on
# trading-day changes. ``time`` stays the real epoch and is what the page shows.
BEIJING = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")
HONG_KONG = ZoneInfo("Asia/Hong_Kong")
# US stocks: pre-market, regular and after-hours share the New York date; a bar at
# or after 20:00 ET (overnight session) belongs to the next trading day.
US_STOCK_ROLL_HOUR = 20
# CME Globex (GC=F and other =F): the trading day starts at 18:00 ET the evening before.
CME_ROLL_HOUR = 18


def session_rule(canonical: str, asset: str) -> tuple[ZoneInfo, int | None]:
    """(exchange clock, hour the next trading day starts or None for midnight)."""
    code = (canonical or "").upper()
    if asset == "crypto":
        return BEIJING, None
    if asset == "commodity" or code.endswith("=F"):
        return NEW_YORK, CME_ROLL_HOUR
    if code.endswith(".HK"):
        return HONG_KONG, None
    if code.endswith((".SS", ".SZ", ".BJ")) or "." in code:
        return BEIJING, None
    return NEW_YORK, US_STOCK_ROLL_HOUR


def session_day(ts, canonical: str, asset: str) -> tuple[str, int]:
    """(trading_day, chart_time) for one bar start time ``ts`` (epoch seconds)."""
    clock, roll = session_rule(canonical, asset)
    local = datetime.fromtimestamp(int(ts), clock).replace(tzinfo=None)
    day = local.date()
    if roll is None:
        start = datetime(day.year, day.month, day.day)
    elif local.hour >= roll:
        start = datetime(day.year, day.month, day.day, roll)
        day = day + timedelta(days=1)
    else:
        start = datetime(day.year, day.month, day.day, roll) - timedelta(days=1)
    if roll is not None:
        # Sunday evening futures open Monday's session; nothing trades for Saturday.
        while day.weekday() >= 5:
            day = day + timedelta(days=1)
    offset = int((local - start).total_seconds())
    offset = min(max(offset, 0), 86399)
    anchor = int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())
    return day.isoformat(), anchor + offset


def with_sessions(bars: list[dict], canonical: str, asset: str) -> list[dict]:
    """Copies of ``bars`` with trading_day and a strictly increasing chart_time."""
    out, last = [], None
    for bar in bars:
        trading_day, chart_time = session_day(bar["time"], canonical, asset)
        if last is not None and chart_time <= last:
            chart_time = last + 1
        last = chart_time
        out.append({**bar, "trading_day": trading_day, "chart_time": chart_time})
    return out


def trading_day_gap(previous: str, current: str) -> str:
    """How the page names the break between two trading days.

    "" for consecutive days (an ordinary overnight close), "weekend" when the
    break spans a Saturday or Sunday, "closed" for a weekday holiday.
    """
    a, b = date.fromisoformat(previous), date.fromisoformat(current)
    if (b - a).days <= 1:
        return ""
    between = (a + timedelta(days=n) for n in range(1, (b - a).days))
    return "weekend" if any(d.weekday() >= 5 for d in between) else "closed"


def normalize_tf(tf: str | None) -> str:
    raw = (tf or DEFAULT_TIMEFRAME).strip()
    upper = raw.upper()
    if upper in ("1D", "1W", "1M", "1H"):
        return upper
    if raw.lower() == "5m":
        return "5m"
    raise KlineError("周期只支持 1D / 1W / 1M / 1H / 5m。")


def kline(symbol: str, tf: str | None = None, *, refresh: bool = False) -> dict:
    tf = normalize_tf(tf)
    if tf in INTRADAY:
        return intraday(symbol, tf, refresh=refresh)
    info = route(symbol)
    key = (info["asset_type"], info["canonical"])
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    cached = bool(not refresh and hit and now - hit[0] < CACHE_TTL_SECONDS)
    if cached:
        daily = hit[1]
    else:
        provider, ident, bars, errors = _daily(info)
        daily = {"provider": provider, "provider_symbol": ident, "bars": bars, "fallback_errors": errors, "fetched_at": int(now)}
        with _lock:
            _cache[key] = (now, daily)
    # ``daily_bars`` is intentionally included for weekly/monthly clients.
    # Realtime updates arrive as a forming *daily* bar; retaining the source
    # daily grain lets the browser replace that day's volume and recompute the
    # complete period instead of adding the same cumulative volume every poll.
    priced, omitted_unpriced = _priced_bars(daily["bars"])
    daily_bars = resample(priced, "1D")
    ohlc_fixed = sum(1 for b in priced if b.get("ohlc_fixed"))
    payload = {
        "symbol": (symbol or "").strip().upper(),
        "canonical": info["canonical"],
        "asset_type": info["asset_type"],
        "tf": tf,
        "provider": daily["provider"],
        "provider_symbol": daily["provider_symbol"],
        "fallback_errors": daily["fallback_errors"],
        "is_future": info["asset_type"] == "commodity",
        "adjusted": daily["provider"] != "sina",
        "delayed": quote_delayed(info["asset_type"], daily["provider"]),
        "source_label": "新浪（非官方，不复权）" if daily["provider"] == "sina" else None,
        "fallback": daily["provider"] == "sina" and not info["canonical"].endswith(".BJ"),
        "cached": cached,
        "ohlc_fixed": ohlc_fixed,
        "freshness_note": MAYBE_NOT_UPDATED if MAYBE_NOT_UPDATED in daily["fallback_errors"] else None,
        "fetched_at": daily["fetched_at"],
        "daily_bars": daily_bars,
        "bars": resample(daily_bars, tf),
        "history_label": history_span_label(daily_bars),
    }
    # W200: do not claim a correction when none happened. Omit the note entirely.
    if ohlc_fixed:
        payload["ohlc_note"] = ohlc_check.NOTE_ZH
    if omitted_unpriced:
        payload["unpriced_note"] = _unpriced_note(omitted_unpriced)
    return payload


# ------------------------------------------------------------------ V2-KLINE-RT

_live_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def clear_live_cache() -> None:
    with _lock:
        _live_cache.clear()


def live_okx(base: str) -> dict | None:
    # Two UTC daily candles: the newest, and the one before it (V2-WATCHLIST 昨收, R1).
    resp = requests.get(OKX_LIVE_URL, params={"instId": f"{base}-USDT", "bar": "1Dutc", "limit": "2"}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if str(body.get("code")) != "0":
        raise RuntimeError(f"OKX {body.get('code')}: {body.get('msg') or ''}".strip())
    rows = body.get("data") or []
    if not rows:
        return None
    row = rows[0]
    bar = _bar(int(row[0]) // 1000, row[1], row[2], row[3], row[4], row[5])
    if len(rows) > 1:
        prev = rows[1]
        return _with_prev(bar, int(prev[0]) // 1000, prev[4])
    return bar


def live_coinbase(base: str) -> dict | None:
    end = datetime.now(UTC)
    resp = requests.get(
        COINBASE_URL.format(product=f"{base}-USD"),
        params={"granularity": 86400, "start": (end - timedelta(days=2)).isoformat(), "end": end.isoformat()},
        headers={"User-Agent": "TradingAgents-WebUI"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    ordered = sorted(rows, key=lambda r: r[0])
    t, low, high, op, close, vol = ordered[-1]
    bar = _bar(t, op, high, low, close, vol)
    if len(ordered) > 1:
        return _with_prev(bar, ordered[-2][0], ordered[-2][4])
    return bar


def live_yahoo(canonical: str) -> dict | None:
    import yfinance as yf

    from tradingagents.dataflows.vendors.yahoo.common import yf_retry

    frame = yf_retry(lambda: yf.Ticker(canonical).history(period="5d", interval="1d", auto_adjust=True, actions=False))
    if frame is None or frame.empty:
        return None
    idx, row = frame.index[-1], frame.iloc[-1]
    if any(row.get(k) != row.get(k) for k in ("Open", "High", "Low", "Close")):
        return None
    day = datetime(idx.year, idx.month, idx.day, tzinfo=UTC)
    bar = _bar(day.timestamp(), row["Open"], row["High"], row["Low"], row["Close"], row.get("Volume", 0))
    if len(frame.index) > 1:
        pidx, prow = frame.index[-2], frame.iloc[-2]
        pday = datetime(pidx.year, pidx.month, pidx.day, tzinfo=UTC)
        return _with_prev(bar, pday.timestamp(), prow.get("Close"))
    return bar


def _with_prev(bar: dict, when, close) -> dict:
    """The daily candle before ``bar``: its close is the page's 昨收 (V2-WATCHLIST)."""
    try:
        number = float(close)
        stamp = int(float(when))
    except (TypeError, ValueError):
        return bar
    if number != number or number <= 0 or number in (float("inf"), float("-inf")):
        return bar
    return {**bar, "prev_close": number, "prev_time": stamp}


def _split_prev(bar: dict) -> tuple[dict, float | None, str | None]:
    """(bar without the previous-candle keys, previous close, previous candle's day)."""
    bar = dict(bar)
    close = bar.pop("prev_close", None)
    when = bar.pop("prev_time", None)
    day = datetime.fromtimestamp(when, UTC).date().isoformat() if close is not None and when is not None else None
    return bar, close, day


def _live_fetcher(name: str) -> Callable[[str], dict | None]:
    return {"okx": live_okx, "coinbase": live_coinbase, "yahoo": live_yahoo, "sina": live_sina}[name]


def live_sina(ident: str) -> dict | None:
    rows = sina.daily_rows(ident, 2)
    if not rows:
        return None
    r = rows[-1]
    ts = int(datetime.strptime(r["day"], "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    bar = _bar(ts, r["open"], r["high"], r["low"], r["close"], r["volume"])
    if len(rows) > 1:
        p = rows[-2]
        return _with_prev(bar, datetime.strptime(p["day"], "%Y-%m-%d").replace(tzinfo=UTC).timestamp(), p["close"])
    return bar


def _fresher_live(info: dict, yahoo_ident: str, yahoo_bar: dict, errors: list[str]):
    """The latest-bar form of the Yahoo-vs-Sina rule (AUDIT-FIX-06)."""
    sina_ident = dict(info["providers"])["sina"]
    try:
        sina_bar = live_sina(sina_ident)
    except Exception as exc:  # noqa: BLE001 - Yahoo stays, unverified
        sina_bar = None
        errors.append(f"sina: {type(exc).__name__}")
    if not sina_bar:
        return "yahoo", yahoo_ident, yahoo_bar, MAYBE_NOT_UPDATED
    if _bar_day(yahoo_bar) < _bar_day(sina_bar):
        errors.append("yahoo: 数据过期")
        return "sina", sina_ident, sina_bar, None
    return "yahoo", yahoo_ident, yahoo_bar, None




def quote_delayed(asset: str, provider: str) -> bool:
    """Yahoo is a delayed quote even when the symbol is crypto."""
    return provider == "yahoo" or asset != "crypto"


def _stale_bar(when, tf: str, now: float) -> bool:
    """An old bar is not proof that the session is closed."""
    span = INTRADAY_SECONDS.get(tf)
    if not span:
        return False
    try:
        age = now - float(when)
    except (TypeError, ValueError):
        return False
    return age > span * 3


def _latest_intraday(symbol: str, tf: str) -> dict:
    info = route(symbol)
    asset = info["asset_type"]
    key = ("live", tf, info["canonical"])
    now = time.time()
    with _lock:
        hit = _live_cache.get(key)
    cached = bool(hit and now - hit[0] < RT_CACHE_SECONDS[asset])
    if cached:
        answer = hit[1]
    else:
        errors, answer, limited = [], None, False
        for name, ident in info["providers"]:
            try:
                bars = _fetch_intraday(name, ident, info, tf)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {type(exc).__name__}")
                limited = limited or _rate_limited(exc)
                continue
            if not bars:
                errors.append(f"{name}: 无数据")
                continue
            bar, skipped = _quote_bar(bars)
            if bar is None:
                errors.append(f"{name}: 没有正价格")
                continue
            ohlc_check.fix_bars([bar], info["canonical"])
            answer = {"provider": name, "provider_symbol": ident, "bar": bar, "fallback_errors": errors,
                      "freshness_note": None, "unpriced_note": skipped, "fetched_at": int(now)}
            break
        if answer is None:
            if limited:
                raise KlineRateLimited(RATE_LIMITED_TEXT)
            raise KlineError("暂时拿不到最新行情，稍后会自动重试。")
        with _lock:
            _live_cache[key] = (now, answer)
    bar = answer["bar"]
    trading_day, chart_time = session_day(bar["time"], info["canonical"], asset)
    answer = {**answer, "bar": {**bar, "trading_day": trading_day, "chart_time": chart_time}}
    return {
        "symbol": (symbol or "").strip().upper(),
        "canonical": info["canonical"],
        "asset_type": asset,
        "tf": tf,
        "mode": "poll",
        "poll_seconds": RT_POLL_SECONDS[asset],
        "delayed": quote_delayed(asset, answer["provider"]),
        "cached": cached,
        "stale_bar": _stale_bar(answer["bar"].get("time"), tf, now),
        **answer,
    }


def latest(symbol: str, tf: str | None = None) -> dict:
    """The newest bar for ``tf`` and how often the page should ask again."""
    chosen = normalize_tf(tf) if tf else "1D"
    if chosen in INTRADAY:
        return _latest_intraday(symbol, chosen)
    info = route(symbol)
    asset = info["asset_type"]
    key = (asset, info["canonical"])
    now = time.time()
    with _lock:
        hit = _live_cache.get(key)
    cached = bool(hit and now - hit[0] < RT_CACHE_SECONDS[asset])
    if cached:
        answer = hit[1]
    else:
        errors, answer, limited = [], None, False
        for name, ident in info["providers"]:
            try:
                bar = _live_fetcher(name)(info["base"] if name in ("okx", "coinbase") else ident)
            except Exception as exc:  # noqa: BLE001 - the next provider is the fallback
                errors.append(f"{name}: {type(exc).__name__}")
                limited = limited or _rate_limited(exc)
                continue
            if bar and not _priced_bar(bar):
                errors.append(f"{name}: 没有正价格")
                continue
            if bar:
                note = None
                if name == "yahoo" and _is_yahoo_sina(info):
                    name, ident, bar, note = _fresher_live(info, ident, bar, errors)
                if not _priced_bar(bar):
                    errors.append(f"{name}: 没有正价格")
                    continue
                bar, prev_close, prev_day = _split_prev(bar)
                ohlc_check.fix_bars([bar], info["canonical"])
                answer = {"provider": name, "provider_symbol": ident, "bar": bar, "fallback_errors": errors,
                          "freshness_note": note, "fetched_at": int(now),
                          # V2-WATCHLIST: the vendor's daily candle before this one (crypto: the UTC
                          # day, 北京 08:00) and this candle's own date. None when not returned.
                          "prev_close": prev_close, "prev_day": prev_day,
                          "trading_day": _bar_day(bar).isoformat()}
                break
            errors.append(f"{name}: 无数据")
        if answer is None:
            if limited:
                raise KlineRateLimited(RATE_LIMITED_TEXT)
            raise KlineError("暂时拿不到最新行情，稍后会自动重试。")
        with _lock:
            _live_cache[key] = (now, answer)
    return {
        "symbol": (symbol or "").strip().upper(),
        "canonical": info["canonical"],
        "asset_type": asset,
        "mode": "poll",
        "poll_seconds": RT_POLL_SECONDS[asset],
        "delayed": quote_delayed(asset, answer["provider"]),
        "cached": cached,
        **answer,
    }


# ------------------------------------------------------------------ V2-UI-FIX-1 search

SEARCH_CACHE_SECONDS = 600
SEARCH_MAX = 8
_search_cache: dict[str, tuple[float, dict]] = {}
# Yahoo's search does not match Chinese names, so a short local table covers
# common A-share / HK names; anything else goes to Yahoo search (no key).
CN_NAMES = {
    "贵州茅台": ("600519.SS", "上交所"), "茅台": ("600519.SS", "上交所"), "五粮液": ("000858.SZ", "深交所"),
    "宁德时代": ("300750.SZ", "深交所"), "比亚迪": ("002594.SZ", "深交所"), "中国平安": ("601318.SS", "上交所"),
    "招商银行": ("600036.SS", "上交所"), "工商银行": ("601398.SS", "上交所"), "中国石油": ("601857.SS", "上交所"),
    "隆基绿能": ("601012.SS", "上交所"), "东方财富": ("300059.SZ", "深交所"), "中信证券": ("600030.SS", "上交所"),
    "美的集团": ("000333.SZ", "深交所"), "格力电器": ("000651.SZ", "深交所"), "海康威视": ("002415.SZ", "深交所"),
    "腾讯": ("0700.HK", "港交所"), "腾讯控股": ("0700.HK", "港交所"), "阿里巴巴": ("9988.HK", "港交所"),
    "美团": ("3690.HK", "港交所"), "小米": ("1810.HK", "港交所"), "小米集团": ("1810.HK", "港交所"),
    "京东": ("9618.HK", "港交所"), "苹果": ("AAPL", "纳斯达克"), "英伟达": ("NVDA", "纳斯达克"),
    "特斯拉": ("TSLA", "纳斯达克"), "微软": ("MSFT", "纳斯达克"), "黄金": ("XAUUSD", "COMEX"),
}
EXCHANGES = {"SHH": "上交所", "SHZ": "深交所", "HKG": "港交所", "NMS": "纳斯达克", "NGM": "纳斯达克",
             "NCM": "纳斯达克", "NYQ": "纽交所", "ASE": "美交所", "PCX": "NYSE Arca", "BTS": "Cboe"}
_ALLOWED_TYPES = {"EQUITY", "ETF", "INDEX", "FUTURE", "CRYPTOCURRENCY", "MUTUALFUND"}


def clear_search_cache() -> None:
    with _lock:
        _search_cache.clear()


def yahoo_search(query: str) -> list[dict]:
    """Yahoo symbol search through the project's yfinance dependency (public, no key)."""
    import yfinance as yf

    from tradingagents.dataflows.vendors.yahoo.common import yf_retry

    quotes = yf_retry(lambda: yf.Search(query, max_results=SEARCH_MAX, news_count=0).quotes) or []
    out = []
    for q in quotes:
        sym = str(q.get("symbol") or "")
        if not sym or (q.get("quoteType") and q.get("quoteType") not in _ALLOWED_TYPES):
            continue
        exch = str(q.get("exchange") or "")
        out.append({"symbol": sym, "name": q.get("shortname") or q.get("longname") or sym,
                    "exchange": EXCHANGES.get(exch, q.get("exchDisp") or exch)})
    return out


def search(query: str) -> dict:
    q = (query or "").strip()
    if not q:
        return {"query": "", "results": [], "cached": False}
    if len(q) > 40:
        raise KlineError("搜索词太长了。")
    key = q.upper()
    now = time.time()
    with _lock:
        hit = _search_cache.get(key)
    if hit and now - hit[0] < SEARCH_CACHE_SECONDS:
        return {**hit[1], "query": q, "results": list(hit[1]["results"]), "cached": True}
    results: list[dict] = []
    for name, (sym, exch) in CN_NAMES.items():
        if q in name and all(r["symbol"] != sym for r in results):
            results.append({"symbol": sym, "name": name, "exchange": exch})
    bare = market_code(q)
    if bare:
        exch = {".SS": "上交所", ".SZ": "深交所", ".BJ": "北交所"}.get(bare[-3:], "港交所")
        local = next((n for n, (sym, _) in CN_NAMES.items() if sym == bare), None)
        name = local or (sina.suggest_name(q) if sina_symbol(bare) else None) or q
        results.append({"symbol": bare, "name": name, "exchange": exch})
    try:
        # Pure digits: only the mapped A-share/HK candidate, never Yahoo results.
        remote = yahoo_search(q) if q.isascii() and not q.isdigit() else []
    except Exception:  # noqa: BLE001 - suggestions are best-effort
        remote = []
        if not results:
            raise KlineError("搜索暂时不可用，可以直接输入代码后点「查询」。") from None
    for r in remote:
        found = next((x for x in results if x["symbol"] == r["symbol"]), None)
        if found:
            if found["name"] == q:
                found["name"] = r["name"]
        else:
            results.append(r)
    results = results[:SEARCH_MAX]
    out = {"query": q, "results": results, "cached": False}
    if not results and not q.isascii():
        out["hint"] = "没有找到这个名称，请用 6 位代码查询。"
    elif not results and q.isdigit() and len(q) <= 3:
        out["hint"] = "港股请用 4-5 位代码，例如 0700 或 00700；A 股请用 6 位代码。"
    with _lock:
        _search_cache[key] = (now, dict(out))  # full payload, hint included (W176)
    return out
