"""V2-KLINE: /api/kline routing, provider fallback, cache, resample, and UI assets."""

import re
from pathlib import Path

import pytest

from webui import kline

STATIC = Path(__file__).resolve().parents[1] / "webui" / "static"
DAY = 86400


def _bars(n, start=1_700_006_400):
    return [kline._bar(start + i * DAY, 10 + i, 11 + i, 9 + i, 10.5 + i, 100) for i in range(n)]


@pytest.fixture(autouse=True)
def _fresh_cache():
    kline.clear_cache()
    yield
    kline.clear_cache()


@pytest.mark.parametrize("raw,asset,canonical,chain", [
    ("NVDA", "stock", "NVDA", ["yahoo"]),
    ("XAUUSD", "commodity", "GC=F", ["yahoo"]),
    ("XAGUSD", "commodity", "SI=F", ["yahoo"]),
    ("GC=F", "commodity", "GC=F", ["yahoo"]),
    ("BTC-USDT", "crypto", "BTC-USD", ["okx", "coinbase", "yahoo"]),
    ("btcusd", "crypto", "BTC-USD", ["okx", "coinbase", "yahoo"]),
    ("ETH-USD", "crypto", "ETH-USD", ["okx", "coinbase", "yahoo"]),
])
def test_route(raw, asset, canonical, chain):
    info = kline.route(raw)
    assert info["asset_type"] == asset
    assert info["canonical"] == canonical
    assert [name for name, _ in info["providers"]] == chain


@pytest.mark.parametrize("raw", ["", "  ", "A B", "X" * 40])
def test_route_rejects(raw):
    with pytest.raises(kline.KlineError):
        kline.route(raw)


def test_bad_timeframe():
    with pytest.raises(kline.KlineError):
        kline.kline("NVDA", "5m")


def test_crypto_okx_first(monkeypatch):
    calls = []
    monkeypatch.setattr(kline, "fetch_okx", lambda base: calls.append(("okx", base)) or _bars(3))
    monkeypatch.setattr(kline, "fetch_coinbase", lambda base: calls.append(("coinbase", base)) or _bars(3))
    out = kline.kline("BTC-USDT")
    assert out["provider"] == "okx" and out["provider_symbol"] == "BTC-USDT"
    assert calls == [("okx", "BTC")]
    assert out["asset_type"] == "crypto" and not out["is_future"]


def test_crypto_falls_back_to_coinbase_then_yahoo(monkeypatch):
    def boom(base):
        raise ConnectionError("down")
    monkeypatch.setattr(kline, "fetch_okx", boom)
    monkeypatch.setattr(kline, "fetch_coinbase", lambda base: [])
    seen = []
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: seen.append(sym) or _bars(2))
    out = kline.kline("ETHUSDT")
    assert out["provider"] == "yahoo" and seen == ["ETH-USD"]
    assert out["fallback_errors"] == ["okx: ConnectionError", "coinbase: 无数据"]


def test_all_providers_fail(monkeypatch):
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: [])
    with pytest.raises(kline.KlineError, match="没有拿到"):
        kline.kline("NVDA")


def test_commodity_uses_existing_yahoo_canonical(monkeypatch):
    seen = []
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: seen.append(sym) or _bars(5))
    out = kline.kline("XAUUSD", "1D")
    assert seen == ["GC=F"] and out["is_future"] and out["canonical"] == "GC=F"
    assert len(out["bars"]) == 5


def test_cache_hits_within_ttl_and_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: calls.append(sym) or _bars(4))
    clock = [1000.0]
    monkeypatch.setattr(kline.time, "time", lambda: clock[0])
    first = kline.kline("NVDA", "1D")
    second = kline.kline("nvda", "1W")
    assert len(calls) == 1 and not first["cached"] and second["cached"]
    clock[0] += kline.CACHE_TTL_SECONDS + 1
    kline.kline("NVDA")
    assert len(calls) == 2


def test_resample_weekly_monthly():
    bars = _bars(60)
    weekly = kline.resample(bars, "1W")
    monthly = kline.resample(bars, "1M")
    assert 8 <= len(weekly) <= 10 and 2 <= len(monthly) <= 3
    assert weekly[0]["open"] == bars[0]["open"] and weekly[-1]["close"] == bars[-1]["close"]
    assert sum(b["volume"] for b in weekly) == sum(b["volume"] for b in bars)
    assert max(b["high"] for b in monthly) == max(b["high"] for b in bars)


def test_okx_parses_and_paginates(monkeypatch):
    now = kline._since() + 400 * DAY
    pages = [
        [[str((now - i * DAY) * 1000), "1", "2", "0.5", "1.5", "10"] for i in range(100)],
        [[str((now - i * DAY) * 1000), "1", "2", "0.5", "1.5", "10"] for i in range(100, 200)],
        [],
    ]
    seen = []

    class Resp:
        def __init__(self, rows):
            self.rows = rows

        def raise_for_status(self):
            pass

        def json(self):
            return {"code": "0", "data": self.rows}

    def fake_get(url, params, timeout):
        seen.append(params)
        return Resp(pages[len(seen) - 1])

    monkeypatch.setattr(kline.requests, "get", fake_get)
    bars = kline.fetch_okx("BTC")
    assert seen[0]["instId"] == "BTC-USDT" and "after" in seen[1]
    assert bars == sorted(bars, key=lambda b: b["time"]) and len(bars) == 200
    assert bars[0]["high"] == 2.0


def test_api_kline_endpoint(monkeypatch):
    from webui import server

    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: _bars(3))
    sent = {}

    class H:
        def _json(self, data, status=200):
            sent["data"], sent["status"] = data, status

    handler_cls = next(v for v in vars(server).values() if isinstance(v, type) and hasattr(v, "_kline"))
    handler_cls._kline(H(), "XAUUSD", "")
    assert sent["status"] == 200 and sent["data"]["tf"] == "1D" and sent["data"]["provider_symbol"] == "GC=F"
    handler_cls._kline(H(), "", "1D")
    assert sent["status"] == 400 and "标的" in sent["data"]["error"]


def test_server_routes_and_vendor_bundle():
    src = (Path(__file__).resolve().parents[1] / "webui" / "server.py").read_text(encoding="utf-8")
    assert '"/api/kline"' in src and "/vendor/lightweight-charts.js" in src
    lib = (STATIC / "vendor" / "lightweight-charts.js").read_text(encoding="utf-8")
    assert "Apache License 2.0" in lib[:400]  # upstream license header kept intact
    assert "Apache License" in (STATIC / "vendor" / "lightweight-charts.LICENSE.txt").read_text(encoding="utf-8")


def test_ui_has_entry_attribution_and_no_tradingview_brand():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    nav = re.findall(r'data-page="(\w+)"', index)
    assert nav.index("kline") == nav.index("reports") + 1
    assert "K 线行情</button>" in index
    assert "Lightweight Charts" in app and "lightweight-charts.LICENSE.txt" in app
    assert "attributionLogo: false" in app
    for text in (index, app, css):
        assert "tradingview" not in text.lower()
    for forbidden in ("charting_library", "tradingview.com/widget", "s3.tradingview.com", "TradingView.widget"):
        assert forbidden.lower() not in (index + app).lower()


# ------------------------------------------------------------------ V2-KLINE-RT

@pytest.fixture
def _fresh_live():
    kline.clear_live_cache()
    yield
    kline.clear_live_cache()


def test_latest_crypto_okx_poll_5s(monkeypatch, _fresh_live):
    seen = []
    monkeypatch.setattr(kline, "live_okx", lambda base: seen.append(base) or kline._bar(1, 1, 2, 0.5, 1.5, 3))
    out = kline.latest("BTC-USDT")
    assert seen == ["BTC"] and out["provider"] == "okx"
    assert out["poll_seconds"] == 5 and out["delayed"] is False and out["mode"] == "poll"
    assert out["bar"]["close"] == 1.5


def test_latest_crypto_falls_back(monkeypatch, _fresh_live):
    def boom(base):
        raise TimeoutError
    monkeypatch.setattr(kline, "live_okx", boom)
    monkeypatch.setattr(kline, "live_coinbase", lambda base: kline._bar(1, 1, 1, 1, 2, 0))
    out = kline.latest("ETHUSD")
    assert out["provider"] == "coinbase" and out["fallback_errors"] == ["okx: TimeoutError"]


@pytest.mark.parametrize("sym,canonical", [("XAUUSD", "GC=F"), ("NVDA", "NVDA")])
def test_latest_stock_metal_60s_delayed_and_cached(monkeypatch, _fresh_live, sym, canonical):
    calls = []
    monkeypatch.setattr(kline, "live_yahoo", lambda s: calls.append(s) or kline._bar(1, 1, 1, 1, 1, 0))
    clock = [5000.0]
    monkeypatch.setattr(kline.time, "time", lambda: clock[0])
    first = kline.latest(sym)
    assert first["poll_seconds"] == 60 and first["delayed"] is True and calls == [canonical]
    clock[0] += 30
    assert kline.latest(sym)["cached"] is True and len(calls) == 1
    clock[0] += 30
    assert kline.latest(sym)["cached"] is False and len(calls) == 2


def test_latest_all_fail_is_chinese_and_leaks_nothing(monkeypatch, _fresh_live):
    def boom(s):
        raise RuntimeError("secret-token sk-abc https://x?key=1")
    monkeypatch.setattr(kline, "live_yahoo", boom)
    with pytest.raises(kline.KlineError) as err:
        kline.latest("NVDA")
    assert "稍后会自动重试" in str(err.value) and "sk-" not in str(err.value) and "key=" not in str(err.value)


def test_latest_does_not_touch_history_cache(monkeypatch, _fresh_live):
    calls = []
    monkeypatch.setattr(kline, "fetch_yahoo", lambda s: calls.append(s) or _bars(3))
    monkeypatch.setattr(kline, "live_yahoo", lambda s: kline._bar(1, 1, 1, 1, 1, 0))
    kline.kline("NVDA")
    kline.latest("NVDA")
    assert kline.kline("NVDA")["cached"] is True and len(calls) == 1


def test_api_kline_latest_endpoint(monkeypatch, _fresh_live):
    from webui import server

    monkeypatch.setattr(kline, "live_yahoo", lambda s: kline._bar(1, 1, 1, 1, 1, 0))
    sent = {}

    class H:
        def _json(self, data, status=200):
            sent["data"], sent["status"] = data, status

    handler_cls = next(v for v in vars(server).values() if isinstance(v, type) and hasattr(v, "_kline_latest"))
    handler_cls._kline_latest(H(), "XAGUSD")
    assert sent["status"] == 200 and sent["data"]["canonical"] == "SI=F"
    handler_cls._kline_latest(H(), "")
    assert sent["status"] == 400
    src = (Path(__file__).resolve().parents[1] / "webui" / "server.py").read_text(encoding="utf-8")
    assert '"/api/kline/latest"' in src


def test_rt_has_visibility_pause_and_no_external_socket():
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "visibilitychange" in app and 'visibilityState !== "hidden"' in app
    assert "new WebSocket" not in app  # the browser only talks to this console; the server polls vendors
    assert "知情" in app and "tradingview" not in app.lower()
