"""V2-WATCHLIST W1: previous close on /api/kline/latest, and a rate limit the page can tell apart.

Fake vendors only: no network. R1: the crypto previous close is the vendor's UTC daily
candle, the same candle the daily K-line shows before the newest one.
"""

import http.client
import json
import socket
import threading

import pandas as pd
import pytest
import requests

from webui import kline, server

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo
DAY = 86400


@pytest.fixture(autouse=True)
def _fresh():
    kline.clear_live_cache()
    kline.clear_cache()
    yield
    kline.clear_live_cache()
    kline.clear_cache()


class _Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self.body


def _okx_rows(now: int, count: int) -> list[list[str]]:
    """Newest first, like OKX: day i closes at 100 + i (so each close is different)."""
    start = now - now % DAY
    return [[str((start - i * DAY) * 1000), "1", "300", "0.5", str(100 + i), "10"] for i in range(count)]


@pytest.mark.unit
def test_okx_previous_close_equals_the_daily_kline_close_before_the_newest(monkeypatch):
    now = 1_794_000_000
    rows = _okx_rows(now, 30)

    def fake_get(url, params, timeout, **kwargs):
        if url == kline.OKX_LIVE_URL:
            assert params["bar"] == "1Dutc" and params["limit"] == "2"
            return _Resp({"code": "0", "data": rows[: int(params["limit"])]})
        if "after" in params:
            return _Resp({"code": "0", "data": []})
        return _Resp({"code": "0", "data": rows})

    monkeypatch.setattr(kline.requests, "get", fake_get)
    monkeypatch.setattr(kline, "_since", lambda: now - 60 * DAY)
    live = kline.latest("BTC-USDT")
    daily = kline.kline("BTC-USDT", "1D")["bars"]
    assert live["prev_close"] == daily[-2]["close"] == 101.0
    assert live["bar"]["close"] == daily[-1]["close"] == 100.0
    assert "prev_close" not in live["bar"] and "prev_time" not in live["bar"]
    assert live["prev_day"] < live["trading_day"]


@pytest.mark.unit
def test_coinbase_yahoo_and_sina_return_the_candle_before_the_newest(monkeypatch):
    now = 1_794_000_000 - 1_794_000_000 % DAY
    rows = [[now - DAY, 1, 3, 2, 90.0, 5], [now, 1, 3, 2, 95.0, 5]]
    monkeypatch.setattr(kline.requests, "get", lambda *a, **k: _Resp(rows))
    bar = kline.live_coinbase("BTC")
    assert bar["close"] == 95.0 and bar["prev_close"] == 90.0 and bar["prev_time"] == now - DAY

    import yfinance as yf

    frame = pd.DataFrame({"Open": [1.0, 1.0, 1.0], "High": [3.0, 3.0, 3.0], "Low": [0.5, 0.5, 0.5],
                          "Close": [10.0, 11.0, 12.0], "Volume": [1, 1, 1]},
                         index=pd.to_datetime(["2026-11-11", "2026-11-12", "2026-11-13"]))

    class Ticker:
        def __init__(self, symbol):
            pass

        def history(self, **kwargs):
            return frame

    monkeypatch.setattr(yf, "Ticker", Ticker)
    bar = kline.live_yahoo("NVDA")
    assert bar["close"] == 12.0 and bar["prev_close"] == 11.0

    monkeypatch.setattr(kline.sina, "daily_rows", lambda ident, n: [
        {"day": "2026-11-12", "open": 1, "high": 2, "low": 1, "close": 1450.0, "volume": 1},
        {"day": "2026-11-13", "open": 1, "high": 2, "low": 1, "close": 1452.0, "volume": 1}][-n:])
    bar = kline.live_sina("sh600519")
    assert bar["close"] == 1452.0 and bar["prev_close"] == 1450.0


@pytest.mark.unit
def test_a_single_candle_has_no_previous_close(monkeypatch):
    monkeypatch.setattr(kline, "live_okx", lambda base: kline._bar(1_794_000_000, 1, 2, 0.5, 1.5, 3))
    out = kline.latest("BTC-USDT")
    assert out["prev_close"] is None and out["prev_day"] is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", [0, -1, float("nan"), "x", None])
def test_an_unusable_previous_close_is_dropped(bad):
    assert "prev_close" not in kline._with_prev({"close": 1.0}, 1, bad)


def _http_429():
    response = _Resp({}, 429)
    return requests.HTTPError("429 Too Many Requests", response=response)


def _yahoo_limited():
    from yfinance.exceptions import YFRateLimitError

    from tradingagents.dataflows.vendors.yahoo.common import VendorUnavailableError

    try:
        try:
            raise YFRateLimitError()
        except YFRateLimitError as inner:
            raise VendorUnavailableError("Yahoo Finance rate limited after 3 retries") from inner
    except VendorUnavailableError as outer:
        return outer


def _boom(exc):
    def fetch(*args, **kwargs):
        raise exc
    return fetch


@pytest.mark.unit
def test_a_rate_limit_is_its_own_error(monkeypatch):
    monkeypatch.setattr(kline, "live_okx", _boom(_http_429()))
    monkeypatch.setattr(kline, "live_coinbase", _boom(RuntimeError("down")))
    monkeypatch.setattr(kline, "live_yahoo", _boom(RuntimeError("down")))
    with pytest.raises(kline.KlineRateLimited) as caught:
        kline.latest("BTC-USDT")
    assert caught.value.code == "rate_limited" and str(caught.value) == "行情源限流，稍后重试。"


@pytest.mark.unit
def test_a_yahoo_rate_limit_behind_vendor_unavailable_is_found(monkeypatch):
    monkeypatch.setattr(kline, "live_yahoo", _boom(_yahoo_limited()))
    with pytest.raises(kline.KlineRateLimited):
        kline.latest("NVDA")


@pytest.mark.unit
def test_okx_code_50011_is_a_rate_limit(monkeypatch):
    monkeypatch.setattr(kline.requests, "get", lambda *a, **k: _Resp({"code": "50011", "msg": "Too Many Requests"}))
    with pytest.raises(RuntimeError) as caught:
        kline.live_okx("BTC")
    assert kline._rate_limited(caught.value)
    assert not kline._rate_limited(RuntimeError("OKX 51001: Instrument ID does not exist"))


@pytest.mark.unit
def test_other_failures_stay_a_plain_error(monkeypatch):
    for name in ("live_okx", "live_coinbase", "live_yahoo"):
        monkeypatch.setattr(kline, name, _boom(RuntimeError("down")))
    with pytest.raises(kline.KlineError) as caught:
        kline.latest("BTC-USDT")
    assert not isinstance(caught.value, kline.KlineRateLimited)


@pytest.mark.unit
def test_a_rate_limit_on_one_source_does_not_hide_another_that_answers(monkeypatch):
    monkeypatch.setattr(kline, "live_okx", _boom(_http_429()))
    monkeypatch.setattr(kline, "live_coinbase", lambda base: kline._bar(1_794_000_000, 1, 2, 0.5, 1.5, 3))
    assert kline.latest("BTC-USDT")["provider"] == "coinbase"


@pytest.mark.unit
def test_intraday_latest_also_reports_a_rate_limit(monkeypatch):
    monkeypatch.setattr(kline, "_fetch_intraday", _boom(_http_429()))
    with pytest.raises(kline.KlineRateLimited):
        kline.latest("BTC-USDT", "5m")


@pytest.fixture
def console(monkeypatch, tmp_path):
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def get(path):  # tests/conftest.py signs the console in; the page token is httpd.session_token
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request("GET", path, headers={server.TOKEN_HEADER: httpd.session_token})
        response = connection.getresponse()
        data = response.read().decode("utf-8", "replace")
        connection.close()
        return response.status, json.loads(data or "{}")

    yield get
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.unit
def test_api_latest_answers_429_with_a_code_and_400_otherwise(console, monkeypatch):
    monkeypatch.setattr(kline, "live_okx", _boom(_http_429()))
    monkeypatch.setattr(kline, "live_coinbase", _boom(RuntimeError("down")))
    monkeypatch.setattr(kline, "live_yahoo", _boom(RuntimeError("down")))
    status, body = console("/api/kline/latest?symbol=BTC-USDT")
    assert status == 429 and body == {"error": "行情源限流，稍后重试。", "code": "rate_limited"}
    monkeypatch.setattr(kline, "live_okx", _boom(RuntimeError("down")))
    kline.clear_live_cache()
    status, body = console("/api/kline/latest?symbol=BTC-USDT")
    assert status == 400 and "code" not in body
    monkeypatch.setattr(kline, "live_okx", lambda base: kline._with_prev(kline._bar(1_794_000_000, 1, 2, 0.5, 1.5, 3), 1_793_913_600, 1.4))
    status, body = console("/api/kline/latest?symbol=BTC-USDT")
    assert status == 200 and body["prev_close"] == 1.4 and body["prev_day"] == "2026-11-05"  # 1_793_913_600 is 2026-11-05 00:00 UTC
