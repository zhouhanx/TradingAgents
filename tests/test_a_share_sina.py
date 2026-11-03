"""V2-UI-FIX-1: analysis OHLCV paths share the A-share rule (BJ -> Sina; SH/SZ -> Yahoo then Sina)."""

import pandas as pd
import pytest

from tradingagents.dataflows.vendors import sina
from tradingagents.dataflows.vendors.yahoo import market, ohlcv


def _rows(n=30, start="2026-09-01"):
    days = pd.bdate_range(start, periods=n)
    return [{"day": d.strftime("%Y-%m-%d"), "open": 10.0 + i, "high": 11.0 + i, "low": 9.0 + i,
             "close": 10.5 + i, "volume": 1000.0} for i, d in enumerate(days)]


class _EmptyTicker:
    calls = []

    def __init__(self, sym):
        _EmptyTicker.calls.append(sym)

    def history(self, **kwargs):
        return pd.DataFrame()


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    sina.clear_cache()
    sina.last_source.clear()
    _EmptyTicker.calls = []
    from tradingagents.dataflows import config as cfg

    current = dict(cfg.get_config())
    current["data_cache_dir"] = str(tmp_path)
    monkeypatch.setattr(ohlcv, "get_config", lambda: current)
    yield
    sina.clear_cache()
    sina.last_source.clear()


@pytest.mark.parametrize("canonical,chain", [
    ("920002.BJ", ["sina"]), ("600519.SS", ["yahoo", "sina"]), ("002323.SZ", ["yahoo", "sina"]),
    ("200002.SZ", ["yahoo", "sina"]), ("AAPL", ["yahoo"]), ("0700.HK", ["yahoo"]),
])
def test_chain(canonical, chain):
    assert sina.chain(canonical) == chain


def test_bj_stock_data_is_sina_only(monkeypatch):
    seen = []
    monkeypatch.setattr(sina, "daily_rows", lambda ident, count=0: seen.append(ident) or _rows())
    monkeypatch.setattr(market.yf, "Ticker", _EmptyTicker)
    out = market.get_YFin_data_online("920002", "2026-09-01", "2026-10-12")
    assert seen == ["bj920002"] and _EmptyTicker.calls == []
    assert "# Data source: Sina Finance (unofficial, unadjusted" in out
    assert "920002.BJ (from 920002)" in out


def test_sh_falls_back_to_sina(monkeypatch):
    monkeypatch.setattr(sina, "daily_rows", lambda ident, count=0: _rows())
    monkeypatch.setattr(market.yf, "Ticker", _EmptyTicker)
    out = market.get_YFin_data_online("600519", "2026-09-01", "2026-10-12")
    assert _EmptyTicker.calls == ["600519.SS"]
    assert "unadjusted" in out


def test_load_ohlcv_bj_and_indicator_label(monkeypatch):
    monkeypatch.setattr(sina, "daily_rows", lambda ident, count=0: _rows(80, "2026-06-01"))
    monkeypatch.setattr(ohlcv.yf, "Ticker", _EmptyTicker)
    frame = ohlcv.load_ohlcv("920002", "2026-09-15")
    assert not frame.empty and _EmptyTicker.calls == []
    assert sina.last_source["920002.BJ"] == "sina"
    text = market.get_stock_stats_indicators_window("920002", "close_10_ema", "2026-09-15", 5)
    assert "Data source: Sina Finance (unofficial, unadjusted" in text


def test_us_unchanged(monkeypatch):
    seen = []
    monkeypatch.setattr(sina, "daily_rows", lambda ident, count=0: seen.append(ident) or _rows())
    monkeypatch.setattr(market.yf, "Ticker", _EmptyTicker)
    from tradingagents.dataflows.errors import VendorError

    with pytest.raises(VendorError):
        market.get_YFin_data_online("AAPL", "2026-09-01", "2026-10-12")
    assert seen == []
