"""Sina Finance public daily K-line JSON for A shares (no key, unofficial).

Yahoo has no Beijing (.BJ) bars, so Beijing reads Sina only; Shanghai/Shenzhen
read Yahoo first and fall back to Sina. Sina prices are NOT adjusted for
dividends/splits (送转/除权), unlike Yahoo's ``auto_adjust`` history, so every
consumer labels the source and whether it is adjusted.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pandas as pd
import requests

SINA_URL = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
SINA_SUGGEST_URL = "https://suggest3.sinajs.cn/suggest/type=11,12,15,21,22,23,24,25,26,31,33,41&key={key}"
HTTP_TIMEOUT = 8.0
CACHE_SECONDS = 300
MAX_BARS = 1300  # about five years of trading days
_PREFIX = {".SS": "sh", ".SZ": "sz", ".BJ": "bj"}

SOURCE_YAHOO = "Yahoo Finance (adjusted / 复权)"
SOURCE_SINA = "Sina Finance (unofficial, unadjusted / 新浪非官方，不复权)"
SOURCE_LABEL = {"yahoo": SOURCE_YAHOO, "sina": SOURCE_SINA}

_cache: dict[tuple[str, int], tuple[float, list[dict]]] = {}
_lock = threading.Lock()
# canonical -> vendor name ("yahoo"/"sina") that last served OHLCV for it.
last_source: dict[str, str] = {}


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def sina_symbol(canonical: str) -> str | None:
    """``600519.SS`` -> ``sh600519``; None for anything that is not an A share."""
    if not isinstance(canonical, str):
        return None
    for suffix, prefix in _PREFIX.items():
        if canonical.upper().endswith(suffix) and canonical[:-3].isdigit():
            return prefix + canonical[:-3]
    return None


def chain(canonical: str) -> list[str]:
    """Vendor order for daily OHLCV: BJ -> Sina; SH/SZ -> Yahoo then Sina; else Yahoo."""
    if canonical.upper().endswith(".BJ") and sina_symbol(canonical):
        return ["sina"]
    if sina_symbol(canonical):
        return ["yahoo", "sina"]
    return ["yahoo"]


def daily_rows(ident: str, count: int = MAX_BARS) -> list[dict]:
    """Newest ``count`` daily bars as ``{"day","open","high","low","close","volume"}`` floats."""
    key = (ident, count)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    resp = requests.get(SINA_URL, params={"symbol": ident, "scale": "240", "ma": "no", "datalen": str(count)},
                        headers={"User-Agent": "Mozilla/5.0 TradingAgents"}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    text = resp.text.strip()
    raw = resp.json() if text not in ("", "null") else []
    rows = [{"day": r["day"][:10], "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]),
             "close": float(r["close"]), "volume": float(r.get("volume") or 0)} for r in raw or []]
    with _lock:
        _cache[key] = (now, rows)
    return rows


def history_frame(canonical: str, start: str | None = None, end_exclusive: str | None = None) -> pd.DataFrame:
    """Yahoo-shaped frame (DatetimeIndex ``Date``; Open/High/Low/Close/Volume)."""
    ident = sina_symbol(canonical)
    if not ident:
        return pd.DataFrame()
    rows = daily_rows(ident)
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["Date"] = pd.to_datetime(frame.pop("day"))
    frame = frame.rename(columns=str.capitalize).set_index("Date").sort_index()
    if start:
        frame = frame[frame.index >= pd.Timestamp(start)]
    if end_exclusive:
        frame = frame[frame.index < pd.Timestamp(end_exclusive)]
    return frame


def suggest_name(code: str) -> str | None:
    """Best-effort Chinese name for a 6-digit A-share code from Sina suggest (GBK text)."""
    try:
        resp = requests.get(SINA_SUGGEST_URL.format(key=code), timeout=3,
                            headers={"User-Agent": "Mozilla/5.0 TradingAgents"})
        resp.raise_for_status()
        text = resp.content.decode("gbk", errors="ignore")
    except Exception:  # noqa: BLE001 - names are cosmetic
        return None
    body = text.split('"', 2)[1] if text.count('"') >= 2 else ""
    for entry in body.split(";"):
        parts = entry.split(",")
        if len(parts) > 4 and parts[2] == code:
            return parts[4] or None
    return None


def bars_epoch(canonical: str, since_ts: int) -> list[dict]:
    """K 线 page shape: ``{"time": epoch_utc, ...}`` from ``since_ts`` on."""
    ident = sina_symbol(canonical)
    out = []
    for r in daily_rows(ident, 260) if ident else []:
        ts = int(datetime.strptime(r["day"], "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
        if ts >= since_ts:
            out.append({"time": ts, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"]})
    return out
