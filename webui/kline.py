"""V2-KLINE: K 线行情 data for the console's chart page.

``kline(symbol, tf)`` answers ``/api/kline``: about one year of daily bars,
resampled to weekly or monthly candles on request. Stocks and metals/commodity
futures read the project's existing Yahoo source (the same canonical symbol the
analysis uses, e.g. XAUUSD -> GC=F). Crypto reads OKX first, then Coinbase, then
Yahoo. Answers are cached in memory per (provider chain, symbol) for a short TTL.
Realtime updates are a separate batch (V2-KLINE-RT); nothing here streams.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import requests

from tradingagents.dataflows.symbols import crypto_base, normalize_symbol

TIMEFRAMES = ("1D", "1W", "1M")
DEFAULT_TIMEFRAME = "1D"
HISTORY_DAYS = 366
CACHE_TTL_SECONDS = 300
HTTP_TIMEOUT = 8.0

OKX_URL = "https://www.okx.com/api/v5/market/history-candles"
COINBASE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"

_cache: dict[tuple[str, str], tuple[float, dict]] = {}
_lock = threading.Lock()


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


def route(symbol: str) -> dict:
    """Which asset type and provider chain a symbol reads from."""
    raw = (symbol or "").strip()
    if not raw:
        raise KlineError("请输入标的代码。")
    if len(raw) > 32 or any(ch.isspace() for ch in raw):
        raise KlineError("标的代码格式不对。")
    canonical = normalize_symbol(raw)
    base = crypto_base(raw) or crypto_base(canonical)
    if base:
        return {"asset_type": "crypto", "canonical": f"{base}-USD", "base": base,
                "providers": [("okx", f"{base}-USDT"), ("coinbase", f"{base}-USD"), ("yahoo", f"{base}-USD")]}
    asset = "commodity" if canonical.endswith("=F") else "stock"
    return {"asset_type": asset, "canonical": canonical, "base": None, "providers": [("yahoo", canonical)]}


def _fetcher(name: str) -> Callable[[str], list[dict]]:
    return {"okx": fetch_okx, "coinbase": fetch_coinbase, "yahoo": fetch_yahoo}[name]


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
    return out


def _daily(info: dict) -> tuple[str, str, list[dict], list[str]]:
    errors = []
    for name, ident in info["providers"]:
        try:
            bars = _fetcher(name)(info["base"] if name in ("okx", "coinbase") else ident)
        except Exception as exc:  # noqa: BLE001 - the next provider is the fallback
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        if bars:
            return name, ident, bars, errors
        errors.append(f"{name}: 无数据")
    raise KlineError("没有拿到这个标的的 K 线数据（" + "；".join(errors) + "）。")


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
        "cached": cached,
        "fetched_at": daily["fetched_at"],
        "bars": resample(daily["bars"], tf),
    }
