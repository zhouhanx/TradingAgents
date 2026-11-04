"""OHLC sanity check shared by the K-line page and the analysis pipeline (W187).

A vendor bar whose open or close lies outside its own high/low is internally
inconsistent. It is never shown silently: the high becomes max(open, high,
close), the low becomes min(open, low, close), the original values are logged,
and the caller is told how many bars were corrected so it can say so.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

NOTE_ZH = "源数据不一致，已修正高低价"
NOTE_EN = "source data inconsistent: high/low corrected to cover open/close"

# canonical -> number of corrected bars in the frame most recently loaded for it.
last_fixed: dict[str, int] = {}


def _bad(o, h, l, c) -> bool:  # noqa: E741
    return h < max(o, c) or l > min(o, c) or h < l


def fix_bars(bars: list[dict], symbol: str) -> int:
    """Correct dict bars (open/high/low/close) in place; mark each with ohlc_fixed."""
    count = 0
    for bar in bars:
        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]  # noqa: E741
        if not _bad(o, h, l, c):
            continue
        bar["high"], bar["low"] = max(o, h, l, c), min(o, h, l, c)
        bar["ohlc_fixed"] = True
        count += 1
        logger.warning("%s: OHLC inconsistent at %s (open=%s high=%s low=%s close=%s); high/low corrected.",
                       symbol, bar.get("time"), o, h, l, c)
    return count


def fix_frame(frame, symbol: str, date_col: str | None = "Date"):
    """Correct a pandas frame with Open/High/Low/Close columns; returns (frame, count)."""
    cols = ("Open", "High", "Low", "Close")
    if frame is None or frame.empty or not all(k in frame.columns for k in cols):
        last_fixed[symbol] = 0
        return frame, 0
    o, h, l, c = (frame[k] for k in cols)  # noqa: E741
    mask = ((h < o.combine(c, max)) | (l > o.combine(c, min)) | (h < l)).fillna(False)
    count = int(mask.sum())
    if count:
        frame = frame.copy()
        sub = frame.loc[mask, list(cols)]
        for idx, row in sub.iterrows():
            when = frame.at[idx, date_col] if date_col and date_col in frame.columns else idx
            logger.warning("%s: OHLC inconsistent at %s (open=%s high=%s low=%s close=%s); high/low corrected.",
                           symbol, when, row["Open"], row["High"], row["Low"], row["Close"])
        frame.loc[mask, "High"] = sub.max(axis=1)
        frame.loc[mask, "Low"] = sub.min(axis=1)
    last_fixed[symbol] = count
    return frame, count


def note_for(symbol: str) -> str | None:
    n = last_fixed.get(symbol, 0)
    return f"{n} bar(s): {NOTE_EN} / {NOTE_ZH}" if n else None
