"""AUDIT-CHECK-02: crawl attachments only have a news-analyst consumer."""

from __future__ import annotations

from pathlib import Path

import pytest

from webui import service

ITEMS = {"job": "ic-a-000001", "items": ["0123456789abcdef"]}


def _selection(analysts):
    return {
        "analysts": list(analysts),
        "research_depth": 1,
        "quick_think_llm": "quick",
        "deep_think_llm": "deep",
        "backend_url": None,
        "llm_provider": "chatgpt",
        "output_language": "Chinese",
        "info_crawl": dict(ITEMS),
    }


@pytest.mark.parametrize("analysts", [["market"], ["social"]])
def test_attachment_without_news_is_allowed_but_not_carried_to_worker(analysts):
    selections = _selection(analysts)
    assert service._info_crawl_has_consumer(selections) is False
    assert service._info_crawl_notices(selections) == [service.INFO_CRAWL_NO_NEWS_ANALYST]
    config = service.build_run_config(selections)
    assert "info_crawl_job" not in config and "info_crawl_items" not in config


def test_missing_attachment_without_news_does_not_block_analysis(tmp_path):
    selections = _selection(["market"])
    service._validate_info_crawl_attachment(
        selections, {"data_cache_dir": str(tmp_path / "does-not-exist")}
    )


def test_start_records_no_news_attachment_notice_and_continues(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    service.set_runner(lambda job: 0)
    try:
        record = service.start_validated_run(_selection(["market"]), background=False)
    finally:
        service.set_runner(None)
    assert record["status"] == "finished"
    assert record["notices"] == [service.INFO_CRAWL_NO_NEWS_ANALYST]
    assert service.INFO_CRAWL_NO_NEWS_ANALYST in record["detail"]


def test_attachment_with_news_is_carried_to_worker_and_has_no_warning():
    selections = _selection(["market", "social", "news"])
    assert service._info_crawl_has_consumer(selections) is True
    assert service._info_crawl_notices(selections) == []
    config = service.build_run_config(selections)
    assert config["info_crawl_job"] == ITEMS["job"]
    assert config["info_crawl_items"] == ITEMS["items"]


def test_consumer_check_uses_analysts_after_real_environment_overlay(monkeypatch):
    # The shipped product has no TRADINGAGENTS_ANALYSTS variable: analyst
    # choices remain the user-selected list, while real env overlays (here the
    # output language) are applied before the worker-consumer check.
    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "1")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "output_language", "Japanese")
    selections = _selection(["market"])
    service.apply_env_skips(selections)
    assert selections["analysts"] == ["market"]
    assert selections["output_language"] == "Japanese"
    assert service._info_crawl_has_consumer(selections) is False
    assert service._info_crawl_notices(selections) == [service.INFO_CRAWL_NO_NEWS_ANALYST]
    config = service.build_run_config(selections)
    assert "info_crawl_job" not in config


def test_finished_process_is_not_called_a_completed_analysis():
    counts = service._progress_counts({"id": "run-finished", "status": "finished"})
    assert counts["team_zh"] == "进程已结束"
    assert counts["team_zh"] != "全部完成"
    assert counts["unrecorded"] is True
    running = service._progress_counts({"id": "run-live", "status": "running"})
    assert running["unrecorded"] is False
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'finished: "已结束"' in script
    assert '{finished: "已完成", completed: "已完成"}' not in script
    assert '["finished", "已结束"]' in script


def test_failed_run_without_a_role_is_not_called_stopped():
    counts = service._progress_counts({"id": "run-failed", "status": "failed"})
    assert counts["team_zh"] == "没有定位到团队"
    assert counts["team_zh"] != "已停止"
    stopped = service._progress_counts({"id": "run-stopped", "status": "stopped"})
    assert stopped["team_zh"] == "已停止"
