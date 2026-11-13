"""V2-UI-FIX-2: OHLC check (W187), invalid vs not-found vs unreachable codes (W188),
Pandas4Warning filter (W189)."""

import logging
import warnings
from pathlib import Path

import pandas as pd
import pytest
import requests

from tradingagents.dataflows import ohlc_check
from tradingagents.dataflows.errors import VendorUnavailableError
from webui import kline

STATIC = Path(__file__).resolve().parents[1] / "webui" / "static"
DAY = 86400


@pytest.fixture(autouse=True)
def _fresh():
    kline.clear_cache()
    yield
    kline.clear_cache()


def _bad_bars():
    # Mirrors the 200625 case: open 3.16 above high 3.01.
    return [kline._bar(1_700_006_400, 3.0, 3.05, 2.95, 3.0, 10),
            kline._bar(1_700_006_400 + DAY, 3.16, 3.01, 2.98, 3.00, 10)]


def test_fix_bars_corrects_and_logs(caplog):
    bars = _bad_bars()
    with caplog.at_level(logging.WARNING, logger="tradingagents.dataflows.ohlc_check"):
        assert ohlc_check.fix_bars(bars, "200625.SZ") == 1
    assert "ohlc_fixed" not in bars[0]
    assert bars[1]["high"] == 3.16 and bars[1]["low"] == 2.98 and bars[1]["ohlc_fixed"]
    assert bars[1]["open"] == 3.16 and bars[1]["close"] == 3.00
    assert "open=3.16 high=3.01" in caplog.text


def test_fix_frame_and_note():
    frame = pd.DataFrame({"Date": ["2026-11-02", "2026-11-03"], "Open": [3.0, 3.16], "High": [3.05, 3.01],
                          "Low": [2.95, 2.98], "Close": [3.0, 2.90]})
    out, n = ohlc_check.fix_frame(frame, "200625.SZ")
    assert n == 1 and out["High"].tolist() == [3.05, 3.16] and out["Low"].tolist() == [2.95, 2.90]
    assert frame["High"].tolist() == [3.05, 3.01]  # input frame untouched
    assert "已修正高低价" in ohlc_check.note_text(n)
    clean, n = ohlc_check.fix_frame(out, "200625.SZ")
    assert n == 0 and ohlc_check.note_text(n) is None


def test_kline_marks_fixed_bars(monkeypatch):
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: _bad_bars())
    out = kline.kline("200625")
    assert out["ohlc_fixed"] == 1 and out["ohlc_note"] == ohlc_check.NOTE_ZH
    assert out["bars"][-1]["high"] == 3.16 and out["bars"][-1]["ohlc_fixed"]
    week = kline.kline("200625", "1W")
    assert any(b.get("ohlc_fixed") for b in week["bars"])


@pytest.mark.parametrize("raw", ["ab$c", "60051!", "<script>", "茅台x", "..", "-ABC"])
def test_format_invalid_before_any_source(monkeypatch, raw):
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: pytest.fail("source must not be asked"))
    with pytest.raises(kline.KlineError, match="代码格式无效|没有找到这个名称"):
        kline.kline(raw)


@pytest.mark.parametrize("raw", ["ABC", "abc", "BRK-B", "BRK.B", "GC=F", "^GSPC"])
def test_letter_codes_pass_format(raw):
    kline.route(raw)


def test_not_found_says_not_found(monkeypatch):
    class YFPricesMissingError(Exception):
        pass

    def gone(sym):
        try:
            raise YFPricesMissingError("possibly delisted")
        except YFPricesMissingError as exc:
            raise VendorUnavailableError("Yahoo Finance request failed: YFPricesMissingError") from exc

    monkeypatch.setattr(kline, "fetch_yahoo", gone)
    with pytest.raises(kline.KlineError, match="没有找到这个代码") as err:
        kline.kline("abc")
    assert "连不上" not in str(err.value)


@pytest.mark.parametrize("exc", [requests.ConnectionError("x"), requests.Timeout("x"), TimeoutError("x")])
def test_unreachable_says_unreachable(monkeypatch, exc):
    def down(sym):
        try:
            raise exc
        except Exception as inner:
            raise VendorUnavailableError("Yahoo Finance request failed") from inner

    monkeypatch.setattr(kline, "fetch_yahoo", down)
    with pytest.raises(kline.KlineError, match="行情源暂时连不上"):
        kline.kline("ABC")


def test_http_5xx_counts_as_unreachable(monkeypatch):
    resp = requests.Response()
    resp.status_code = 503

    def down(sym):
        raise requests.HTTPError("503", response=resp)

    monkeypatch.setattr(kline, "fetch_yahoo", down)
    with pytest.raises(kline.KlineError, match="行情源暂时连不上"):
        kline.kline("ABC")


def test_pandas4warning_filtered_only_from_yfinance():
    from tradingagents.dataflows.vendors.yahoo import common

    cat = common.Pandas4Warning
    if cat is None:
        pytest.skip("pandas has no Pandas4Warning")
    with warnings.catch_warnings(record=True) as seen:
        warnings.resetwarnings()
        warnings.simplefilter("always")
        common.silence_yfinance_pandas4()
        warnings.warn_explicit("x", cat, "history.py", 1, module="yfinance.scrapers.history", registry={})
        warnings.warn_explicit("y", cat, "mine.py", 1, module="tradingagents.other", registry={})
        warnings.warn_explicit("z", UserWarning, "base.py", 1, module="yfinance.base", registry={})
    assert sorted(str(w.message) for w in seen) == ["y", "z"]


def test_404_counts_as_not_found(monkeypatch):
    resp = requests.Response()
    resp.status_code = 404

    def gone(sym):
        raise requests.HTTPError("404", response=resp)

    monkeypatch.setattr(kline, "fetch_yahoo", gone)
    with pytest.raises(kline.KlineError, match="没有找到这个代码"):
        kline.kline("ZZZZ")


def test_ui_shows_fixed_note():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "源数据不一致，已修正高低价" in js and "kline-ohlc--fixed" in js
    assert ".kline-ohlc--fixed" in (STATIC / "app.css").read_text(encoding="utf-8")


def test_yahoo_stock_label_is_not_yahoo_pill():
    """W201: Yahoo rows are not labeled 数据源：Yahoo on the K-line page."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "数据源：股票${period}" in js
    assert "数据源：股票日线 ${" not in js
    body = js.split("function klineSourceLabel", 1)[1].split("function klineTickLabel", 1)[0]
    assert 'row.asset_type === "stock"' in body
    assert "不把它写成股票" in body
    assert "周期未知，不把它写成日线" in js
    assert '|| [, "日线"]' not in js
    assert 'if (row.asset_type === "stock") return `数据源：股票${period} ${symbol}`;' in body
    assert "数据源：Yahoo" not in js
    assert "Yahoo Finance" not in js.split("function klineSourceLabel")[1].split("function drawKline")[0]
