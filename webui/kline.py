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
from datetime import UTC, datetime, timedelta

import requests

from tradingagents.dataflows import ohlc_check
from tradingagents.dataflows.symbols import crypto_base, market_code, normalize_symbol
from tradingagents.dataflows.vendors import sina

TIMEFRAMES = ("1D", "1W", "1M")
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
    if tf == "1D":
        return list(bars)
    out: list[dict] = []
    key_of = (lambda d: d.isocalendar()[:2]) if tf == "1W" else (lambda d: (d.year, d.month))
    current_key = None
    for bar in bars:
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


def kline(symbol: str, tf: str | None = None) -> dict:
    tf = (tf or DEFAULT_TIMEFRAME).strip().upper()
    if tf not in TIMEFRAMES:
        raise KlineError("周期只支持 1D / 1W / 1M。")
    info = route(symbol)
    key = (info["asset_type"], info["canonical"])
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    cached = bool(hit and now - hit[0] < CACHE_TTL_SECONDS)
    if cached:
        daily = hit[1]
    else:
        provider, ident, bars, errors = _daily(info)
        daily = {"provider": provider, "provider_symbol": ident, "bars": bars, "fallback_errors": errors, "fetched_at": int(now)}
        with _lock:
            _cache[key] = (now, daily)
    return {
        "symbol": (symbol or "").strip().upper(),
        "canonical": info["canonical"],
        "asset_type": info["asset_type"],
        "tf": tf,
        "provider": daily["provider"],
        "provider_symbol": daily["provider_symbol"],
        "fallback_errors": daily["fallback_errors"],
        "is_future": info["asset_type"] == "commodity",
        "adjusted": daily["provider"] != "sina",
        "source_label": "新浪（非官方，不复权）" if daily["provider"] == "sina" else None,
        "fallback": daily["provider"] == "sina" and not info["canonical"].endswith(".BJ"),
        "cached": cached,
        "ohlc_fixed": sum(1 for b in daily["bars"] if b.get("ohlc_fixed")),
        "ohlc_note": ohlc_check.NOTE_ZH,
        "freshness_note": MAYBE_NOT_UPDATED if MAYBE_NOT_UPDATED in daily["fallback_errors"] else None,
        "fetched_at": daily["fetched_at"],
        "bars": resample(daily["bars"], tf),
    }


# ------------------------------------------------------------------ V2-KLINE-RT

_live_cache: dict[tuple[str, str], tuple[float, dict]] = {}


def clear_live_cache() -> None:
    with _lock:
        _live_cache.clear()


def live_okx(base: str) -> dict | None:
    resp = requests.get(OKX_LIVE_URL, params={"instId": f"{base}-USDT", "bar": "1Dutc", "limit": "1"}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    body = resp.json()
    if str(body.get("code")) != "0":
        raise RuntimeError(f"OKX: {body.get('msg') or body.get('code')}")
    rows = body.get("data") or []
    if not rows:
        return None
    row = rows[0]
    return _bar(int(row[0]) // 1000, row[1], row[2], row[3], row[4], row[5])


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
    t, low, high, op, close, vol = max(rows, key=lambda r: r[0])
    return _bar(t, op, high, low, close, vol)


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
    return _bar(day.timestamp(), row["Open"], row["High"], row["Low"], row["Close"], row.get("Volume", 0))


def _live_fetcher(name: str) -> Callable[[str], dict | None]:
    return {"okx": live_okx, "coinbase": live_coinbase, "yahoo": live_yahoo, "sina": live_sina}[name]


def live_sina(ident: str) -> dict | None:
    rows = sina.daily_rows(ident, 1)
    if not rows:
        return None
    r = rows[-1]
    ts = int(datetime.strptime(r["day"], "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
    return _bar(ts, r["open"], r["high"], r["low"], r["close"], r["volume"])


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


def latest(symbol: str) -> dict:
    """The newest daily bar and how often the page should ask again."""
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
        errors, answer = [], None
        for name, ident in info["providers"]:
            try:
                bar = _live_fetcher(name)(info["base"] if name in ("okx", "coinbase") else ident)
            except Exception as exc:  # noqa: BLE001 - the next provider is the fallback
                errors.append(f"{name}: {type(exc).__name__}")
                continue
            if bar:
                note = None
                if name == "yahoo" and _is_yahoo_sina(info):
                    name, ident, bar, note = _fresher_live(info, ident, bar, errors)
                ohlc_check.fix_bars([bar], info["canonical"])
                answer = {"provider": name, "provider_symbol": ident, "bar": bar, "fallback_errors": errors,
                          "freshness_note": note, "fetched_at": int(now)}
                break
            errors.append(f"{name}: 无数据")
        if answer is None:
            raise KlineError("暂时拿不到最新行情，稍后会自动重试。")
        with _lock:
            _live_cache[key] = (now, answer)
    return {
        "symbol": (symbol or "").strip().upper(),
        "canonical": info["canonical"],
        "asset_type": asset,
        "mode": "poll",
        "poll_seconds": RT_POLL_SECONDS[asset],
        "delayed": asset != "crypto",
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
