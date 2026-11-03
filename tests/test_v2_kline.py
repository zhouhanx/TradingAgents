"""V2-KLINE: /api/kline routing, provider fallback, cache, resample, and UI assets."""

import re
import socket
from pathlib import Path

import pytest

from webui import kline

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo
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
    with pytest.raises(kline.KlineError, match="没有找到这个代码"):
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


# ------------------------------------------------------------------ V2-UI-FIX-1

from tradingagents.dataflows.symbols import market_code, normalize_symbol  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    ("600519", "600519.SS"), ("688981", "688981.SS"), ("002323", "002323.SZ"),
    ("000858", "000858.SZ"), ("300750", "300750.SZ"), ("0700", "0700.HK"),
    ("00700", "0700.HK"), ("09988", "9988.HK"), ("002323.SZ", "002323.SZ"),
    ("600519.SH", "600519.SS"), ("0700.HK", "0700.HK"), ("AAPL", "AAPL"), ("nvda", "NVDA"),
])
def test_shared_symbol_mapping(raw, expected):
    # The analysis pipeline (normalize_symbol) and the K 线 page share one mapping.
    assert normalize_symbol(raw) == expected
    assert kline.route(raw)["canonical"] == expected


@pytest.mark.parametrize("raw,expected", [("830799", "830799.BJ"), ("430047", "430047.BJ"), ("920002", "920002.BJ"), ("900901", "900901.SS"), ("200002", "200002.SZ")])
def test_beijing_and_b_share(raw, expected):
    assert normalize_symbol(raw) == expected
    info = kline.route(raw)
    assert info["canonical"] == expected


def test_a_share_providers():
    assert [n for n, _ in kline.route("830799")["providers"]] == ["sina"]
    assert kline.route("830799")["providers"][0][1] == "bj830799"
    assert kline.route("600519")["providers"] == [("yahoo", "600519.SS"), ("sina", "sh600519")]
    assert kline.route("AAPL")["providers"] == [("yahoo", "AAPL")]


def test_sina_fallback_and_bj(monkeypatch):
    monkeypatch.setattr(kline, "fetch_yahoo", lambda s: [])
    monkeypatch.setattr(kline, "fetch_sina", lambda s: _bars(4))
    out = kline.kline("002323")
    assert out["provider"] == "sina" and out["provider_symbol"] == "sz002323"
    kline.clear_cache()
    assert kline.kline("920002")["provider_symbol"] == "bj920002"


def test_route_hints():
    with pytest.raises(kline.KlineError, match="港股请用 4-5 位代码"):
        kline.route("700")
    with pytest.raises(kline.KlineError, match="请用 6 位代码查询"):
        kline.route("不存在的公司")
    assert kline.route("贵州茅台")["canonical"] == "600519.SS"
    assert kline.search("不存在的公司")["hint"].endswith("请用 6 位代码查询。")


@pytest.mark.parametrize("raw", ["700000", "123", "1234567", "AAPL"])
def test_market_code_none(raw):
    assert market_code(raw) is None


def test_unsupported_six_digit_friendly():
    with pytest.raises(kline.KlineError, match="暂不支持这个 6 位代码"):
        kline.route("700000")


def test_errors_are_plain_chinese(monkeypatch):
    class VendorUnavailableError(Exception):
        pass

    def boom(_):
        raise VendorUnavailableError("yahoo down")

    monkeypatch.setattr(kline, "fetch_yahoo", boom)
    monkeypatch.setattr(kline, "fetch_sina", boom)
    with pytest.raises(kline.KlineError) as err:
        kline.kline("002323")
    assert "Error" not in str(err.value) and "行情源暂时连不上（002323.SZ）" in str(err.value)
    kline.clear_cache()
    monkeypatch.setattr(kline, "fetch_yahoo", lambda _: [])
    with pytest.raises(kline.KlineError, match="没有找到这个代码.*A 股请用 6 位代码"):
        kline.kline("INTCX")


def test_search_local_names_and_cache(monkeypatch):
    kline.clear_search_cache()
    calls = []
    monkeypatch.setattr(kline, "yahoo_search", lambda q: calls.append(q) or [{"symbol": "AAPL", "name": "Apple Inc.", "exchange": "纳斯达克"}])
    out = kline.search("贵州茅台")
    assert out["results"][0] == {"symbol": "600519.SS", "name": "贵州茅台", "exchange": "上交所"}
    assert calls == [], "non-ASCII queries never hit Yahoo search"
    assert kline.search("Apple")["results"][0]["symbol"] == "AAPL"
    assert kline.search("apple")["cached"] is True
    assert calls == ["Apple"]
    code = kline.search("002323")["results"]
    assert code[0]["symbol"] == "002323.SZ" and code[0]["exchange"] == "深交所"
    kline.clear_search_cache()


def test_search_failure_friendly(monkeypatch):
    kline.clear_search_cache()

    def boom(_):
        raise RuntimeError("YFRateLimitError")

    monkeypatch.setattr(kline, "yahoo_search", boom)
    with pytest.raises(kline.KlineError, match="搜索暂时不可用"):
        kline.search("ZZZZ")
    assert kline.search("")["results"] == []
    kline.clear_search_cache()


def test_search_endpoint_requires_token(monkeypatch):
    import http.client
    import json
    import threading

    from webui import server

    kline.clear_search_cache()
    monkeypatch.setattr(kline, "yahoo_search", lambda q: [])
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *a, **k))
    httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        def get(token):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            headers = {server.TOKEN_HEADER: httpd.session_token} if token else {}
            conn.request("GET", "/api/kline/search?q=%E8%8C%85%E5%8F%B0", headers=headers)
            resp = conn.getresponse()
            body = json.loads(resp.read().decode() or "{}")
            conn.close()
            return resp.status, body
        assert get(False)[0] == 403
        status, body = get(True)
        assert status == 200 and body["results"][0]["symbol"] == "600519.SS"
    finally:
        httpd.shutdown()
        httpd.server_close()
        kline.clear_search_cache()


def test_ui_assets_ui_fix():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    assert ">查询</button>" in js and "/api/kline/search" in js and "未更新" in js
    assert ".kline-chart { position: relative; height: 520px; }" in css and ".kline-vol { height: 96px; border-top: 1px solid" in css and "--kline-h: 36px" in css
    assert re.search(r"\.ic-src input\[type=\"checkbox\"\][^}]*width: 16px", css)


def test_old_bj_code_hint(monkeypatch):
    monkeypatch.setattr(kline, "fetch_sina", lambda s: [])
    with pytest.raises(kline.KlineError, match="920 开头的新代码"):
        kline.kline("830799")


def test_digit_search_only_mapped(monkeypatch):
    kline.clear_search_cache()
    calls = []
    monkeypatch.setattr(kline, "yahoo_search", lambda q: calls.append(q) or [{"symbol": "X", "name": "x", "exchange": "y"}])
    assert [r["symbol"] for r in kline.search("002323")["results"]] == ["002323.SZ"]
    assert [r["symbol"] for r in kline.search("00700")["results"]] == ["0700.HK"]
    assert [r["symbol"] for r in kline.search("920002")["results"]] == ["920002.BJ"]
    assert kline.search("123")["results"] == []
    assert kline.search("123")["hint"].startswith("港股请用 4-5 位代码")
    assert calls == []
    kline.clear_search_cache()


def test_empty_vs_unreachable_wording(monkeypatch):
    monkeypatch.setattr(kline, "fetch_yahoo", lambda s: [])
    monkeypatch.setattr(kline, "fetch_sina", lambda s: [])
    with pytest.raises(kline.KlineError, match="行情源暂时没有这只股票的数据（代码格式正确：002323.SZ）"):
        kline.kline("002323")
    kline.clear_cache()

    def down(_):
        raise TimeoutError("t")

    monkeypatch.setattr(kline, "fetch_sina", down)
    with pytest.raises(kline.KlineError, match="行情源暂时连不上"):
        kline.kline("002323")


def test_sina_response_labels(monkeypatch):
    monkeypatch.setattr(kline, "fetch_yahoo", lambda s: [])
    monkeypatch.setattr(kline, "fetch_sina", lambda s: _bars(3))
    out = kline.kline("600519")
    assert out["source_label"] == "新浪（非官方，不复权）" and out["fallback"] is True and out["adjusted"] is False
    kline.clear_cache()
    bj = kline.kline("920002")
    assert bj["fallback"] is False and bj["source_label"] == "新浪（非官方，不复权）"
    kline.clear_cache()
    monkeypatch.setattr(kline, "fetch_yahoo", lambda s: _bars(3))
    y = kline.kline("600519")
    assert y["source_label"] is None and y["adjusted"] is True


def test_search_hint_survives_cache():
    kline.clear_search_cache()
    first = kline.search("不存在的公司")
    second = kline.search("不存在的公司")
    assert first["cached"] is False and second["cached"] is True
    assert second["hint"] == first["hint"] == "没有找到这个名称，请用 6 位代码查询。"
    assert second["results"] == []
    kline.clear_search_cache()
