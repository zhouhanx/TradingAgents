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
    assert '["1H", "小时线"]' in js and '["5m", "5分钟线"]' in js
    assert '["1H", "小时"]' not in js and '["5m", "5分钟"]' not in js
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


# ---- V2-KLINE-INTRADAY mock v1 (Eric 2026-11-13): Beijing clock, trading day,
# day separators, bar-count default view, 万/亿 volume.

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

NY = ZoneInfo("America/New_York")
BJ = ZoneInfo("Asia/Shanghai")


def _at(text, zone):
    return int(datetime.fromisoformat(text).replace(tzinfo=zone).timestamp())


def _beijing(ts):
    return datetime.fromtimestamp(ts, BJ).strftime("%m-%d %H:%M")


def test_us_regular_session_crosses_beijing_midnight_but_keeps_its_trading_day():
    # 14:40 ET on Thu 11-12 is 03:40 on 11-13 in Beijing; the trading day is still 11-12.
    ts = _at("2026-11-12 14:40", NY)
    assert _beijing(ts) == "11-13 03:40"
    day, chart = kline.session_day(ts, "NVDA", "stock")
    assert day == "2026-11-12"
    # chart_time lands on the trading day's UTC date, so the axis day tick reads 11-12.
    assert datetime.fromtimestamp(chart, ZoneInfo("UTC")).date().isoformat() == "2026-11-12"


def test_us_pre_market_and_after_hours_share_the_new_york_date():
    for clock in ("04:00", "09:25", "16:00", "19:55"):
        assert kline.session_day(_at(f"2026-11-12 {clock}", NY), "AAPL", "stock")[0] == "2026-11-12", clock


def test_us_overnight_session_belongs_to_the_next_trading_day_and_skips_the_weekend():
    assert kline.session_day(_at("2026-11-12 20:00", NY), "NVDA", "stock")[0] == "2026-11-13"
    assert kline.session_day(_at("2026-11-12 23:30", NY), "NVDA", "stock")[0] == "2026-11-13"
    # A Friday-evening bar does not open a Saturday.
    assert kline.session_day(_at("2026-11-13 20:30", NY), "NVDA", "stock")[0] == "2026-11-16"
    # Sunday-evening overnight opens Monday.
    assert kline.session_day(_at("2026-11-15 20:00", NY), "NVDA", "stock")[0] == "2026-11-16"


def test_daylight_saving_does_not_move_the_trading_day():
    # 2026-11-01 is the first day of EST: 09:30 ET is 21:30 Beijing before, 22:30 after.
    before = _at("2026-10-30 09:30", NY)
    after = _at("2026-11-02 09:30", NY)
    assert _beijing(before) == "10-30 21:30" and _beijing(after) == "11-02 22:30"
    assert kline.session_day(before, "NVDA", "stock")[0] == "2026-10-30"
    assert kline.session_day(after, "NVDA", "stock")[0] == "2026-11-02"
    # Same position inside the session on both sides of the switch.
    assert kline.session_day(before, "NVDA", "stock")[1] % 86400 == kline.session_day(after, "NVDA", "stock")[1] % 86400


def test_cme_futures_trading_day_starts_at_18_et_and_sunday_opens_monday():
    assert kline.session_day(_at("2026-11-12 17:55", NY), "GC=F", "commodity")[0] == "2026-11-12"
    assert kline.session_day(_at("2026-11-12 18:00", NY), "GC=F", "commodity")[0] == "2026-11-13"
    assert kline.session_day(_at("2026-11-15 18:00", NY), "GC=F", "commodity")[0] == "2026-11-16"
    # The session start sits at 00:00 of the trading day on the chart.
    _, chart = kline.session_day(_at("2026-11-12 18:00", NY), "GC=F", "commodity")
    assert chart % 86400 == 0


def test_beijing_and_hong_kong_markets_use_the_local_date_and_crypto_the_beijing_date():
    assert kline.session_day(_at("2026-11-13 09:35", BJ), "600519.SS", "stock")[0] == "2026-11-13"
    assert kline.session_day(_at("2026-11-13 14:55", BJ), "000858.SZ", "stock")[0] == "2026-11-13"
    assert kline.session_day(_at("2026-11-13 15:55", ZoneInfo("Asia/Hong_Kong")), "0700.HK", "stock")[0] == "2026-11-13"
    # Crypto trades on Saturday; its day is the Beijing calendar date, no weekend roll.
    assert kline.session_day(_at("2026-11-14 00:05", BJ), "BTC-USD", "crypto")[0] == "2026-11-14"
    assert kline.session_day(_at("2026-11-13 23:55", BJ), "BTC-USD", "crypto")[0] == "2026-11-13"


def test_with_sessions_keeps_real_time_and_gives_strictly_increasing_chart_time():
    bars = [_bar(_at(f"2026-11-{d} {h}", NY)) for d, h in (("12", "15:00"), ("12", "15:55"), ("13", "09:30"), ("16", "09:30"))]
    out = kline.with_sessions(bars, "NVDA", "stock")
    assert [b["time"] for b in out] == [b["time"] for b in bars]
    assert [b["trading_day"] for b in out] == ["2026-11-12", "2026-11-12", "2026-11-13", "2026-11-16"]
    charts = [b["chart_time"] for b in out]
    assert charts == sorted(charts) and len(set(charts)) == len(charts)
    assert "trading_day" not in bars[0], "the source (cached) bars are not changed"


def test_trading_day_gap_names_weekend_and_holiday_breaks():
    assert kline.trading_day_gap("2026-11-11", "2026-11-12") == ""
    assert kline.trading_day_gap("2026-11-13", "2026-11-16") == "weekend"
    # Thanksgiving: Wed 11-25 to Fri 11-27 is a weekday holiday, not a weekend.
    assert kline.trading_day_gap("2026-11-25", "2026-11-27") == "closed"
    # Good Friday plus the weekend still crosses a weekend.
    assert kline.trading_day_gap("2026-04-02", "2026-04-06") == "weekend"


def test_intraday_answer_and_latest_carry_trading_day_and_chart_time(monkeypatch):
    bars = [_bar(_at("2026-11-12 15:55", NY)), _bar(_at("2026-11-13 09:30", NY), 12)]
    monkeypatch.setattr(kline, "fetch_yahoo_intraday", lambda ident, tf: [dict(b) for b in bars])
    kline.clear_cache()
    kline.clear_live_cache()
    out = kline.kline("NVDA", "5m")
    assert [b["trading_day"] for b in out["bars"]] == ["2026-11-12", "2026-11-13"]
    assert all(isinstance(b["chart_time"], int) for b in out["bars"])
    live = kline.latest("NVDA", "5m")
    assert live["bar"]["trading_day"] == "2026-11-13"
    assert live["bar"]["chart_time"] == out["bars"][-1]["chart_time"], "the poll lands on the same chart slot"


def test_page_wiring_for_the_intraday_mock():
    js = Path("webui/static/app.js").read_text(encoding="utf-8")
    css = Path("webui/static/app.css").read_text(encoding="utf-8")
    assert "const KLINE_VIEW_BARS = { wide: 160, narrow: 60 };" in js
    assert "crosshair: { horzLine: { visible: false, labelVisible: false } }" in js
    assert 'priceFormat: { type: "custom", formatter: klineVolume, minMove: 1 }' in js
    assert "时间为北京时间，日期为交易日。" in js
    assert "const KLINE_SCALE_W = 72;" in js
    assert "  .kline-vol { height: 80px; }" in css
    assert ".kline-ohlc--ro > span { white-space: nowrap; }" in css
    assert "border-left: 1px dashed" in css
