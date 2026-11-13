"""AUDIT batch B: AUDIT-FIX-04 (+W200/W201), AUDIT-FIX-05, AUDIT-CHECK-02, W214, W218.

Merged onto Eric's local code (PM ruling 2026-11-13): the behaviour B fixed is tested
through Eric's interfaces -- the crawl store and the worker both follow the console's
effective cache dir (``<data_cache_dir>/info-crawl``), missing rows are refused by
``_validate_info_crawl_attachment`` only while the news analyst is selected, an
attachment without news is reported by ``_info_crawl_notices`` and never reaches the
worker, Yahoo is not named in ``source_label``, and weekly/monthly live volume comes
from ``daily_bars`` (+ ``refresh=1``) rather than ``last_daily``/``fresh=1``.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from tradingagents.dataflows import config as df_config
from tradingagents.dataflows.info_crawl import jobs
from tradingagents.dataflows.info_crawl.base import InfoItem
from tradingagents.dataflows.info_crawl.store import JsonlItemStore
from webui import info_crawl as info_crawl_api, kline, service

DAY = 86400
JOB = "ic-a-000001"
MISSING_ID = "fedcba9876543210"  # valid format, never saved


def _bars(n, start=1_700_006_400, vol=100):
    return [kline._bar(start + i * DAY, 10 + i, 11 + i, 9 + i, 10.5 + i, vol + i) for i in range(n)]


@pytest.fixture(autouse=True)
def _clean():
    kline.clear_cache()
    info_crawl_api.set_store(None)
    yield
    kline.clear_cache()
    info_crawl_api.set_store(None)


def _row(title="Held"):
    return InfoItem(source="fed_press", title=title, url=f"https://www.federalreserve.gov/{title}",
                    published_at="2026-10-29T18:00:00Z")


def _save(target, rows, status="done"):
    target.save_job({"id": JOB, "status": status, "created_at": "2026-10-29T18:00:00Z"})
    target.save_items(JOB, rows)


@pytest.fixture
def cache_dir(monkeypatch, tmp_path):
    """The console's 「数据缓存目录」, switchable mid-test like the settings drawer."""
    current = [tmp_path / "a"]
    monkeypatch.delenv("TRADINGAGENTS_CACHE_DIR", raising=False)
    monkeypatch.setattr(service, "effective_cache_dir", lambda: str(current[0]))
    return current


# ---------------------------------------------------------------- W200 / W201

def test_w200_no_ohlc_note_when_nothing_fixed(monkeypatch):
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: _bars(5))
    out = kline.kline("NVDA", "1D")
    assert out["ohlc_fixed"] == 0 and "ohlc_note" not in out


def test_w200_note_kept_when_fixed(monkeypatch):
    bars = _bars(5)
    bars[2]["ohlc_fixed"] = True
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: bars)
    out = kline.kline("NVDA", "1D")
    assert out["ohlc_fixed"] == 1 and out["ohlc_note"]


def test_w201_yahoo_is_not_named_in_source_label(monkeypatch):
    # Eric's rule (PM ruling): no 「Yahoo」 in the label; the pill reads 「数据源：股票日线 NVDA」.
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: _bars(3))
    out = kline.kline("NVDA")
    assert out["source_label"] is None
    assert "Yahoo" not in str(out.get("source_label") or "")


# ---------------------------------------------------------------- AUDIT-FIX-04

def test_store_follows_saved_cache_dir(cache_dir, tmp_path):
    first = info_crawl_api.store()
    assert Path(first.root) == tmp_path / "a" / "info-crawl"
    cache_dir[0] = tmp_path / "b"
    assert Path(info_crawl_api.store().root) == tmp_path / "b" / "info-crawl"


def test_cache_dir_change_console_and_worker_share_one_directory(cache_dir, tmp_path):
    """PM: after the cache dir changes, console and worker read/write the same directory."""
    selections = {"analysts": ["market", "news"], "research_depth": 1, "quick_think_llm": "quick",
                  "deep_think_llm": "deep", "backend_url": None, "llm_provider": "chatgpt",
                  "output_language": "Chinese"}
    for name in ("a", "b"):
        cache_dir[0] = tmp_path / name
        row = _row(f"Held-{name}")
        console = info_crawl_api.store()  # the console writes the crawl here ...
        _save(console, [row])
        sel = {**selections, "info_crawl": {"job": JOB, "items": [row.id]}}
        config = service.build_run_config(sel)  # ... and the worker gets this run config
        assert Path(config["data_cache_dir"]) / "info-crawl" == Path(console.root) == tmp_path / name / "info-crawl"
        service._validate_info_crawl_attachment(sel, config)
        with df_config.run_config(config):
            text = jobs.get_info_items(config["info_crawl_job"], config["info_crawl_items"],
                                       as_of="2026-10-30T00:00:00Z")
        assert f"Held-{name}" in text
        assert service.info_attach_run_line(config) == "已带入 1 条信息抓取资讯（由新闻分析师使用）。"
    # A row ticked under the old dir is not silently read from the new one.
    old = _row("Held-a")
    sel = {**selections, "info_crawl": {"job": JOB, "items": [old.id]}}
    with pytest.raises(service.ConsoleError, match="附件已失效"):
        service._validate_info_crawl_attachment(sel, service.build_run_config(sel))


def test_worker_info_crawl_dir_is_optional_override(tmp_path):
    """jobs.py from B: info_crawl_dir is only an optional pin; without it default_store()."""
    pinned, default = tmp_path / "pinned" / "info-crawl", tmp_path / "cache"
    _save(JsonlItemStore(pinned), [_row("Pinned")])
    _save(JsonlItemStore(default / "info-crawl"), [_row("Default")])
    saved = dict(df_config.get_config())
    try:
        df_config.set_config({**saved, "data_cache_dir": str(default), "info_crawl_dir": None})
        ids = [_row("Default").id]
        assert "Default" in jobs.get_info_items(JOB, ids, as_of="2026-10-30T00:00:00Z")
        df_config.set_config({**saved, "data_cache_dir": str(default), "info_crawl_dir": str(pinned)})
        assert "Pinned" in jobs.get_info_items(JOB, [_row("Pinned").id], as_of="2026-10-30T00:00:00Z")
    finally:
        df_config.set_config(saved)


def test_validate_refuses_missing_rows(tmp_path):
    cache = tmp_path / "cache"
    row = _row()
    _save(JsonlItemStore(cache / "info-crawl"), [row])
    base = {"analysts": ["news"]}
    with pytest.raises(service.ConsoleError, match="附件已失效") as err:
        service._validate_info_crawl_attachment(
            {**base, "info_crawl": {"job": JOB, "items": [row.id, MISSING_ID]}}, {"data_cache_dir": str(cache)})
    assert err.value.status == 409
    with pytest.raises(service.ConsoleError, match="附件已失效"):  # unknown job (dir switched / purged)
        service._validate_info_crawl_attachment(
            {**base, "info_crawl": {"job": "ic-a-000002", "items": [row.id]}}, {"data_cache_dir": str(cache)})


def test_run_line_found_and_missing(tmp_path):
    root = tmp_path / "info-crawl"
    row = _row()
    JsonlItemStore(root).save_items(JOB, [row])
    cfg = {"info_crawl_job": JOB, "info_crawl_items": [row.id], "info_crawl_dir": str(root)}
    assert service.info_attach_run_line(cfg) == "已带入 1 条信息抓取资讯（由新闻分析师使用）。"
    cfg["info_crawl_items"] = [row.id, MISSING_ID]
    assert "只找到 1 条" in service.info_attach_run_line(cfg)
    # Eric's run config carries data_cache_dir, not info_crawl_dir: same answer.
    cfg = {"info_crawl_job": JOB, "info_crawl_items": [row.id, MISSING_ID], "data_cache_dir": str(tmp_path)}
    assert "只找到 1 条" in service.info_attach_run_line(cfg)
    assert service.info_attach_run_line({}) is None


# ---------------------------------------------------------------- AUDIT-CHECK-02

PICK = {"job": JOB, "items": ["0123456789abcdef"]}
BASE = {"research_depth": 1, "llm_provider": "chatgpt", "quick_think_llm": "quick", "deep_think_llm": "deep",
        "backend_url": None, "output_language": "Chinese"}


@pytest.mark.parametrize("analysts", [["market"], ["social"], ["market", "social", "fundamentals"]])
def test_attach_without_news_starts_without_items(analysts):
    sel = {"info_crawl": dict(PICK), "analysts": analysts}
    assert service._info_crawl_has_consumer(sel) is False
    assert service._info_crawl_notices(sel) == ["未选新闻分析师，附件不会被使用。"]
    service._validate_info_crawl_attachment(sel, {"data_cache_dir": "/nonexistent"})  # PM: never refused
    config = service.build_run_config({**sel, **BASE})
    assert "info_crawl_job" not in config and "info_crawl_items" not in config
    # PM: no extra 「本次未带入 N 条」 clause; the run log carries Eric's notice instead.
    assert service.info_attach_run_line(config) is None


@pytest.mark.parametrize("analysts", [["news"], ["market", "news"], ["market", "social", "news", "fundamentals"]])
def test_attach_with_news_kept(analysts):
    sel = {"info_crawl": dict(PICK), "analysts": analysts, **BASE}
    assert service._info_crawl_has_consumer(sel) is True and service._info_crawl_notices(sel) == []
    config = service.build_run_config(sel)
    assert config["info_crawl_job"] == JOB and config["info_crawl_items"] == PICK["items"]


def test_no_attach_any_analysts_fine():
    sel = {"info_crawl": None, "analysts": ["market"]}
    assert service._info_crawl_notices(sel) == []
    service._validate_info_crawl_attachment(sel, {"data_cache_dir": "/nonexistent"})


# ---------------------------------------------------------------- AUDIT-FIX-05

def test_weekly_monthly_ohlcv_match_daily_resample(monkeypatch):
    daily = _bars(90)
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: daily)
    for tf, key in (("1W", lambda d: d.isocalendar()[:2]), ("1M", lambda d: (d.year, d.month))):
        out = kline.kline("NVDA", tf)
        groups = {}
        for b in daily:
            groups.setdefault(key(datetime.fromtimestamp(b["time"], UTC)), []).append(b)
        assert len(out["bars"]) == len(groups)
        for agg, members in zip(out["bars"], groups.values(), strict=True):
            assert agg["open"] == members[0]["open"] and agg["close"] == members[-1]["close"]
            assert agg["high"] == max(m["high"] for m in members)
            assert agg["low"] == min(m["low"] for m in members)
            assert agg["volume"] == sum(m["volume"] for m in members)
        # Eric's way: the daily grain travels with 1W/1M so the browser recomputes the
        # period (a re-polled day replaces its volume) instead of B's last_daily.
        assert "last_daily" not in out
        assert [(b["time"], b["volume"]) for b in out["daily_bars"]] == [(b["time"], b["volume"]) for b in daily]


def test_refresh_refetches_for_backfill(monkeypatch):
    # Replaces B's fresh=1 / 30 s TTL test (cut by PM): refresh=1 always refetches.
    calls = []
    monkeypatch.setattr(kline, "fetch_yahoo", lambda sym: calls.append(sym) or _bars(4))
    kline.kline("NVDA", "1W")
    assert kline.kline("NVDA", "1W")["cached"] and len(calls) == 1
    assert not kline.kline("NVDA", "1W", refresh=True)["cached"] and len(calls) == 2
    assert not hasattr(kline, "FRESH_MIN_AGE_SECONDS")


# ---------------------------------------------------------------- W214 / W218

def _validate(monkeypatch, page_analysts, env_analysts=None, *, saved_ids=None, inject=None):
    """Start-path validate (same payload 配置检查 sends) against the console cache dir."""
    if saved_ids is not None:
        _save(info_crawl_api.store(), saved_ids)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    if env_analysts is not None:
        real = service.env_skipped_values
        monkeypatch.setattr(service, "env_skipped_values", lambda: {**real(), "analysts": env_analysts})
    payload = {"ticker": "NVDA", "date": "2026-10-30", "analysts": page_analysts, "llm_provider": "openai",
               "quick_think_llm": "gpt-4o-mini", "deep_think_llm": "gpt-4o",
               "info_crawl_job": JOB, "info_inject": inject}
    return service._validate_analysis(payload)


def _unused(result):
    sel = result["selections"]
    assert service._info_crawl_has_consumer(sel) is False
    assert "未选新闻分析师，附件不会被使用。" in result["notices"]
    config = service.build_run_config(sel)
    assert "info_crawl_items" not in config and service.info_attach_run_line(config) is None


def test_w214_env_override_adds_news_keeps_items(monkeypatch, cache_dir):
    row = _row()
    result = _validate(monkeypatch, ["market"], ["market", "news"], saved_ids=[row], inject=[row.id])
    sel = result["selections"]
    assert sel["analysts"] == ["market", "news"] and result["notices"] == []
    assert service.build_run_config(sel)["info_crawl_items"] == [row.id]


def test_w214_env_override_removes_news_drops_items(monkeypatch, cache_dir):
    row = _row()
    result = _validate(monkeypatch, ["market", "news"], ["market"], saved_ids=[row], inject=[row.id])
    assert result["selections"]["analysts"] == ["market"]
    _unused(result)


def test_w218_no_news_missing_items_allowed(monkeypatch, cache_dir):
    # PM: without news the attachment is unused — do not refuse for missing/expired ticks.
    result = _validate(monkeypatch, ["market"], inject=[MISSING_ID])
    assert result["selections"]["analysts"] == ["market"]
    _unused(result)


def test_w218_with_news_missing_items_refused(monkeypatch, cache_dir):
    with pytest.raises(service.ConsoleError, match="附件已失效"):
        _validate(monkeypatch, ["market", "news"], inject=[MISSING_ID])


def test_w218_env_removes_news_missing_allowed(monkeypatch, cache_dir):
    result = _validate(monkeypatch, ["market", "news"], ["market"], inject=[MISSING_ID])
    assert result["selections"]["analysts"] == ["market"]
    _unused(result)


def test_w218_env_adds_news_missing_refused(monkeypatch, cache_dir):
    with pytest.raises(service.ConsoleError, match="附件已失效"):
        _validate(monkeypatch, ["market"], ["market", "news"], inject=[MISSING_ID])


def test_w218_no_news_partial_missing_still_allowed(monkeypatch, cache_dir):
    # One row present, one gone — without news, still start (neither reaches the worker).
    row = _row()
    _unused(_validate(monkeypatch, ["market"], saved_ids=[row], inject=[row.id, MISSING_ID]))


def test_w218_with_news_partial_missing_refused(monkeypatch, cache_dir):
    row = _row()
    with pytest.raises(service.ConsoleError, match="附件已失效"):
        _validate(monkeypatch, ["market", "news"], saved_ids=[row], inject=[row.id, MISSING_ID])


def test_run_log_says_how_many_rows_the_worker_found(monkeypatch, tmp_path):
    """(a) ported: the line is emitted right after the analysts sentence in the run log."""
    import inspect

    src = inspect.getsource(service._stream_like_cli)
    analysts_at = src.index("selected_analysts_sentence(selections[\"analysts\"])")
    assert analysts_at < src.index("info_attach_run_line(") < src.index("_emit_status(buffer, seen)")
