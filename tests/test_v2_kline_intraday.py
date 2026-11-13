"""V2-KLINE-INTRADAY: hour and 5-minute bars from native vendor candles."""

from pathlib import Path

from webui import kline


def _bar(ts, close=10.0):
    return {"time": ts, "open": close - 1, "high": close + 1, "low": close - 2, "close": close, "volume": 3}


def test_normalize_keeps_five_minute_distinct_from_month():
    assert kline.normalize_tf("5m") == "5m"
    assert kline.normalize_tf("1h") == "1H"
    assert kline.normalize_tf("1M") == "1M"


def test_beijing_hour_uses_sina_only(monkeypatch):
    calls = []

    def sina(ident, tf):
        calls.append((ident, tf))
        return [_bar(1_700_000_000), _bar(1_700_003_600, 12)]

    monkeypatch.setattr(kline, "fetch_sina_intraday", sina)
    monkeypatch.setattr(kline, "fetch_yahoo_intraday", lambda *a: (_ for _ in ()).throw(AssertionError("yahoo")))
    kline.clear_cache()
    out = kline.kline("920002", "1H")
    assert calls == [("bj920002", "1H")]
    assert out["provider"] == "sina"
    assert out["source_label"] == "新浪（非官方，不复权）"
    assert out["adjusted"] is False
    assert out["daily_bars"] == []
    assert out["span_note"].startswith("小时线")
    assert "不补空K" in out["session_note"]
    assert "ohlc_note" not in out


def test_yahoo_five_minute_prefers_yahoo_when_newer(monkeypatch):
    monkeypatch.setattr(kline, "fetch_yahoo_intraday", lambda ident, tf: [_bar(200), _bar(500, 11)])
    monkeypatch.setattr(kline, "fetch_sina_intraday", lambda ident, tf: [_bar(100)])
    kline.clear_cache()
    out = kline.kline("600519", "5m")
    assert out["provider"] == "yahoo"
    assert out["source_label"] is None
    assert out["history_label"] == "近5分钟"
    assert "近5天" not in out["history_label"]
    assert out["bars"][-1]["close"] == 11


def test_stale_yahoo_hour_falls_back_to_sina(monkeypatch):
    monkeypatch.setattr(kline, "fetch_yahoo_intraday", lambda ident, tf: [_bar(100)])
    monkeypatch.setattr(kline, "fetch_sina_intraday", lambda ident, tf: [_bar(100), _bar(4600, 9)])
    kline.clear_cache()
    out = kline.kline("600519", "1H")
    assert out["provider"] == "sina"
    assert "yahoo: 数据过期" in out["fallback_errors"]
    assert out["source_label"] == "新浪（非官方，不复权）"


def test_crypto_hour_tries_okx_before_coinbase(monkeypatch):
    order = []

    def okx(base, tf):
        order.append("okx")
        return [_bar(10), _bar(3610, 8)]

    def coinbase(base, tf):
        order.append("coinbase")
        raise AssertionError("coinbase should not run after OKX bars")

    monkeypatch.setattr(kline, "fetch_okx_intraday", okx)
    monkeypatch.setattr(kline, "fetch_coinbase_intraday", coinbase)
    kline.clear_cache()
    out = kline.kline("BTC-USDT", "1H")
    assert order == ["okx"]
    assert out["provider"] == "okx"
    assert out["history_label"] == "近1小时"
    assert "近14天" not in out["history_label"]
    assert "加密货币" in out["span_note"]


def test_latest_intraday_uses_same_timeframe(monkeypatch):
    monkeypatch.setattr(kline, "fetch_okx_intraday", lambda base, tf: [_bar(10), _bar(310, 4)] if tf == "5m" else [_bar(1)])
    kline.clear_live_cache()
    out = kline.latest("BTC-USDT", "5m")
    assert out["tf"] == "5m"
    assert out["bar"]["close"] == 4
    assert out["poll_seconds"] == 5


def test_page_offers_hour_and_five_minute():
    js = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert '["1H", "小时"]' in js and '["5m", "5分钟"]' in js
    assert "不补空K" in Path("webui/kline.py").read_text(encoding="utf-8")


def test_span_notes_do_not_name_yahoo():
    notes = list(kline.SPAN_NOTE.values())
    assert notes
    assert all("Yahoo" not in note and "yahoo" not in note.lower() for note in notes)
    assert "加密货币" in kline.SPAN_NOTE[("crypto", "1H")]
    assert "OKX" in kline.SPAN_NOTE[("crypto", "1H")]
    assert "新浪" in kline.SPAN_NOTE[("stock", "5m")]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function klineSourceLabel" in script
    assert "其他行情源（备用）" in script
    assert "数据源：期货${period}" in script
    assert "数据源：加密货币${period}" in script
    assert "Yahoo Finance" not in script.split("function klineSourceLabel")[1].split("function drawKline")[0]


def test_intraday_axis_shows_clock_time_not_the_day_number():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function klineTickLabel" in script
    assert "tickMarkFormatter" in script
    assert 'timeZone: "Asia/Shanghai"' in script
    assert 'tf === "5m" || tf === "1H"' in script
    body = script.split("function klineTickLabel", 1)[1].split("function drawKline", 1)[0]
    assert 'return `${part("hour")}:${part("minute")}`' in body
    assert "日" not in body


def test_old_intraday_bar_is_not_called_a_closed_session(monkeypatch):
    now = 1_700_000_000
    monkeypatch.setattr(kline.time, "time", lambda: now)
    monkeypatch.setattr(kline, "fetch_okx_intraday", lambda base, tf: [_bar(now - 3600, 4)])
    kline.clear_live_cache()
    old = kline.latest("BTC-USDT", "5m")
    assert old["stale_bar"] is True
    assert old.get("market_state") != "closed"
    monkeypatch.setattr(kline, "fetch_okx_intraday", lambda base, tf: [_bar(now - 30, 5)])
    kline.clear_live_cache()
    fresh = kline.latest("BTC-USDT", "5m")
    assert fresh["stale_bar"] is False
    assert fresh.get("market_state") != "closed"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function klineStaleNote" in script
    assert "不据此判断是否开市" in script
    assert "非交易时段，显示最近一根。" not in script
