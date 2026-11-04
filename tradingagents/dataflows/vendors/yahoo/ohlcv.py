import logging
import os

import pandas as pd
import yfinance as yf

from tradingagents.dataflows import ohlc_check
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors import sina
from tradingagents.dataflows.vendors.yahoo.common import raise_for_empty, yf_retry

logger = logging.getLogger(__name__)

# A vendor's latest OHLCV row this many calendar days before the requested date
# is treated as stale. Generous enough to span long holiday weekends, tight
# enough to catch the year-old frames yfinance occasionally returns (#1021).
MAX_OHLCV_STALE_DAYS = 10

# How long a same-day cache that does not yet reach the requested day may be
# reused before it is refetched (#1150). Short enough that an intraday run picks
# up today's close soon after it publishes, long enough that a day with no bar
# at all (weekend, holiday) cannot trigger a download on every call.
OHLCV_CACHE_TTL_SECONDS = 900


def _ensure_date_column(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize the date column to ``Date``.

    Some yfinance builds leave the index unnamed (so ``reset_index()`` yields
    ``index``) or use ``Datetime`` for intraday data. Rename the first
    date-like column so indicators don't silently drop when it isn't ``Date``.
    """
    if "Date" in data.columns:
        return data
    for candidate in ("index", "Datetime", "date"):
        if candidate in data.columns:
            return data.rename(columns={candidate: "Date"})
    return data


def _local_midnight(value) -> pd.Timestamp:
    """A single timestamp as its naive, midnight-normalized local date (or NaT)."""
    if pd.isna(value):
        return pd.NaT
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return pd.NaT
    if ts.tzinfo is not None:
        ts = ts.tz_localize(None)  # drop tz, keep the local wall-clock date
    return ts.normalize()


def _normalize_dates(dates) -> pd.Series:
    """Parse to naive, midnight-normalized dates so tz-aware or intraday
    timestamps compare correctly against the naive ``as_of_date`` cutoff (#1201).

    Normalized per element: 5 years of yfinance bars span daylight-saving
    changes (and cache CSVs round-trip the offsets as strings), so the series can
    carry mixed UTC offsets that ``pd.to_datetime`` cannot unify without
    ``utc=True`` — which would shift non-US (positive-offset) markets to the
    previous day. Keeping each bar's own local date avoids both.
    """
    return pd.to_datetime(pd.Series(dates).map(_local_midnight))


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse/normalize dates and
    coerce prices to numeric (NaN where invalid). Dropping incomplete rows and
    filling gaps is left to ``_fill_price_gaps`` so the caller can first inspect
    the latest in-range bar (#1201)."""
    data = _ensure_date_column(data)
    data["Date"] = _normalize_dates(data["Date"])
    data = data.dropna(subset=["Date"]).copy()

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    return data


def _fill_price_gaps(data: pd.DataFrame) -> pd.DataFrame:
    """Drop rows with no close and forward/back-fill remaining price gaps so
    indicators compute on a continuous series."""
    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    # copy() so a filtered (sliced) input is written to safely, not via a view.
    data = data.dropna(subset=["Close"]).copy()
    data[price_cols] = data[price_cols].ffill().bfill()
    return data


def _coerce_ohlcv_dates(data: pd.DataFrame) -> pd.Series:
    """Return parsed dates from an OHLCV frame, whether Date is a column or the index."""
    if "Date" in data.columns:
        return pd.to_datetime(data["Date"], errors="coerce").dropna()
    # yfinance keeps the dates in the index (a DatetimeIndex, sometimes unnamed).
    if isinstance(data.index, pd.DatetimeIndex):
        return pd.Series(pd.to_datetime(data.index, errors="coerce")).dropna()
    # Fallback: expose the index and look for any date-like column.
    df = data.reset_index()
    for col in ("Date", "Datetime", "date", "index"):
        if col in df.columns:
            parsed = pd.to_datetime(df[col], errors="coerce").dropna()
            if not parsed.empty:
                return parsed
    return pd.Series(dtype="datetime64[ns]")


def _assert_ohlcv_not_stale(
    data: pd.DataFrame,
    as_of_date: str,
    symbol: str,
    canonical: str | None = None,
    *,
    max_stale_days: int = MAX_OHLCV_STALE_DAYS,
) -> None:
    """Reject OHLCV whose latest row is far older than as_of_date.

    Raises NoMarketDataError (with a stale-specific detail) so the router treats
    it like any other "no usable data from this vendor" — try the next vendor,
    then emit one clear unavailable signal. Empty frames are left to the
    caller's existing no-data handling; this guards only the dangerous case of
    present-but-stale rows (a vendor returning a year-old frame that would
    otherwise feed wrong prices to the agent, #1021).
    """
    if data is None or data.empty:
        return
    requested = pd.to_datetime(as_of_date, errors="coerce")
    if pd.isna(requested):
        return
    requested = requested.normalize()
    dates = _coerce_ohlcv_dates(data)
    if dates.empty:
        return
    latest = dates.max().normalize()
    stale_days = (requested - latest).days
    if stale_days > max_stale_days:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"latest row is {latest.date()}, {stale_days} days before the "
            f"requested {requested.date()} (stale) — refusing to use it",
        )


def _cache_is_fresh(data_file, as_of_dt, now) -> bool:
    """Whether the symbol's cached download can serve this request.

    The file holds the download made on the day it was written, so it serves
    only that day. A current-day request also refetches once the file is older
    than the TTL: Yahoo publishes a partial daily candle during market hours,
    whose ``Close`` is not the closing price, and row inspection cannot tell it
    from a final one (#1150).
    """
    written = pd.Timestamp.fromtimestamp(os.path.getmtime(data_file))
    if written.date() != now.date():
        return False
    return as_of_dt.date() < now.date() or (now - written).total_seconds() <= OHLCV_CACHE_TTL_SECONDS


def load_ohlcv(symbol: str, as_of_date: str, fill_gaps: bool = True) -> pd.DataFrame:
    """Fetch OHLCV data with caching, filtered to prevent look-ahead bias.

    Downloads 5 years of data up to today and caches per symbol. On
    subsequent calls the cache is reused. Rows after as_of_date are
    filtered out so backtests never see future prices.

    ``fill_gaps`` carries prices forward over gaps so indicators compute on a
    continuous series. Pass ``False`` to read the values as the vendor reported
    them, leaving a cell that was never reported empty.
    """
    # Resolve broker/forex symbols (XAUUSD+ -> GC=F) to Yahoo's convention,
    # then reject values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    config = get_config()
    as_of_dt = pd.to_datetime(as_of_date).normalize()

    # One cache file per symbol, holding the latest 5y-to-today download.
    now = pd.Timestamp.today()
    start_date = now - pd.DateOffset(years=5)
    start_str = start_date.strftime("%Y-%m-%d")
    # yfinance ``end`` is EXCLUSIVE; request tomorrow so today's row is included
    # when as_of_date is the current day (#986). Look-ahead is still prevented by
    # the as_of_date filter below.
    end_str = (now + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    # A shares: BJ reads Sina only; SH/SZ read Yahoo first, then Sina. Each
    # source keeps its own cache file so an unadjusted Sina frame is never
    # mistaken for Yahoo's adjusted one.
    sources = sina.chain(canonical)

    def fetch(source):
        return _load_source(source, canonical, safe_symbol, config["data_cache_dir"],
                            as_of_dt, now, start_str, end_str)

    data, source, freshness_note = None, sources[0], None
    if sources == ["yahoo", "sina"]:
        data, source, freshness_note = _pick_fresh_a_share(fetch, canonical, as_of_dt)
    else:
        source = sources[0]
        data = fetch(source)
    if data is None:
        raise_for_empty(symbol, canonical, "price rows")
    sina.last_source[canonical] = source

    data = _clean_dataframe(data)

    # Filter to as_of_date to prevent look-ahead bias in backtesting, BEFORE the
    # OHLC check: a bad bar after the analysis date must not count in its note
    # (AUDIT-CHECK-04).
    data = data[data["Date"] <= as_of_dt]
    data, fixed = ohlc_check.fix_frame(data, canonical)

    # A closeless newest bar is an unsettled session, not a symbol without data.
    # _fill_price_gaps below drops it, here and mid-series alike, so the frame
    # ends at the last settled bar; only a range with no close anywhere is no
    # data (#1201, #1289).
    if not data.empty and pd.isna(data["Close"].iloc[-1]):
        settled = data["Close"].notna().to_numpy().nonzero()[0]
        if settled.size == 0:
            raise NoMarketDataError(
                symbol, canonical, "no bar in range has a closing price"
            )
        logger.warning(
            "%s: %d trailing bar(s) through %s have no closing price; using %s "
            "as the latest close.", canonical, len(data) - settled[-1] - 1,
            data["Date"].iloc[-1].date(), data["Date"].iloc[settled[-1]].date(),
        )

    # Indicators need a continuous series, so gaps are carried forward. A caller
    # that reports the numbers themselves asks for the frame as it was reported:
    # a filled cell is the previous session's price under this session's date.
    data = _fill_price_gaps(data) if fill_gaps else data.dropna(subset=["Close"]).copy()

    # Reject a stale frame (latest row far older than as_of_date) rather than
    # feeding year-old prices into indicators (#1021). SH/SZ shares are judged
    # against Sina instead of a fixed day count (AUDIT-FIX-06, PM rule).
    if sources != ["yahoo", "sina"]:
        _assert_ohlcv_not_stale(data, as_of_date, symbol, canonical)

    data.attrs["ohlc_fixed"] = fixed
    data.attrs["source"] = source
    data.attrs["freshness_note"] = freshness_note
    return data


MAYBE_NOT_UPDATED_ZH = "可能未更新"
MAYBE_NOT_UPDATED_EN = "Yahoo data could not be checked against Sina and may not be up to date"


def last_bar_date(frame: pd.DataFrame | None, as_of_dt=None):
    """The newest bar date (naive midnight) on or before ``as_of_dt``, or None."""
    if frame is None or frame.empty:
        return None
    dates = _normalize_dates(_coerce_ohlcv_dates(frame).reset_index(drop=True)).dropna()
    if as_of_dt is not None:
        dates = dates[dates <= pd.Timestamp(as_of_dt).normalize()]
    return None if dates.empty else dates.max()


def _pick_fresh_a_share(fetch, canonical: str, as_of_dt):
    """Yahoo vs Sina for an SH/SZ share (AUDIT-FIX-06, PM rule).

    Yahoo is stale when its last bar is earlier than Sina's: Sina is used. When
    Sina has nothing to compare with, Yahoo is used and marked 可能未更新. No
    fixed day threshold, so a long market holiday never rejects either source.
    Returns (frame, source, freshness_note).
    """
    yahoo_error = None
    try:
        yahoo = fetch("yahoo")
    except Exception as exc:  # noqa: BLE001 - Sina is the fallback
        yahoo, yahoo_error = None, exc
        logger.warning("%s: yahoo failed (%s); trying sina.", canonical, type(exc).__name__)
    try:
        other = fetch("sina")
    except Exception as exc:  # noqa: BLE001
        if yahoo is None:
            raise
        logger.warning("%s: sina failed (%s); yahoo used unchecked.", canonical, type(exc).__name__)
        other = None
    if yahoo is None:
        if other is None and yahoo_error is not None:
            raise yahoo_error
        return other, "sina", None
    y_last, s_last = last_bar_date(yahoo, as_of_dt), last_bar_date(other, as_of_dt)
    if s_last is None:
        return yahoo, "yahoo", MAYBE_NOT_UPDATED_ZH
    if y_last is None or y_last < s_last:
        logger.warning("%s: yahoo last bar %s is older than sina's %s; using sina.",
                       canonical, y_last and y_last.date(), s_last.date())
        return other, "sina", None
    return yahoo, "yahoo", None


def _load_source(source, canonical, safe_symbol, cache_dir, as_of_dt, now, start_str, end_str):
    """One source's 5-year frame from its same-day cache or a download; None when empty."""
    data_file = os.path.join(
        cache_dir,
        f"{safe_symbol}-YFin-data.csv" if source == "yahoo" else f"{safe_symbol}-Sina-data.csv",
    )
    # A cached file may be empty if a prior fetch failed (unknown symbol,
    # transient rate limit). Treat an empty/columnless cache as a miss and
    # re-fetch rather than serving the poisoned file forever.
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if not cached.empty and "Close" in cached.columns and _cache_is_fresh(data_file, as_of_dt, now):
            return cached
    if source == "sina":
        downloaded = sina.history_frame(canonical)
    else:
        # yf.download catches every error, a rate limit included, and returns
        # an empty frame. Ticker.history raises the rate limit, so it is retried.
        downloaded = yf_retry(lambda: yf.Ticker(canonical).history(
            start=start_str,
            end=end_str,
            auto_adjust=True,
            actions=False,
        ))
    if downloaded is None:
        return None
    downloaded = _ensure_date_column(downloaded.reset_index())
    # Only cache real data — never persist an empty frame.
    if downloaded.empty or "Close" not in downloaded.columns:
        return None
    replace_file(data_file, lambda temp, frame=downloaded: frame.to_csv(temp, index=False, encoding="utf-8"))
    return downloaded


def load_notes(symbol: str, as_of_date: str) -> dict:
    """The per-call notes of the frame load_ohlcv serves for this symbol and date."""
    frame = load_ohlcv(symbol, as_of_date)
    return {
        "ohlc_fixed": int(frame.attrs.get("ohlc_fixed", 0)),
        "source": frame.attrs.get("source"),
        "freshness_note": frame.attrs.get("freshness_note"),
    }


