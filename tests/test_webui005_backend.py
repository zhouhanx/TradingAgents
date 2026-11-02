"""WEBUI-005 backend pieces: report list safety and metadata (QA W8), neutral account
label for a blank email (QA W14), recent tickers for the wizard, timestamped role
status for the status page timeline, and resume-from-checkpoint restarts.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from tests.test_webui_security import console  # noqa: F401
from tradingagents.llm_clients import chatgpt_auth, grok_auth
from webui import progress, service

pytestmark = pytest.mark.unit


# --- W14 -------------------------------------------------------------------------------

@pytest.mark.parametrize("auth", [grok_auth, chatgpt_auth])
@pytest.mark.parametrize("email", ["", "   ", "\t\n", None])
def test_a_blank_email_falls_back_to_the_neutral_label(monkeypatch, auth, email):
    monkeypatch.setattr(auth, "_well_formed", lambda: {"tokens": {"access_token": "t"}, "account": {"email": email}})
    assert auth.signed_in_as() == auth.SIGNED_IN_FALLBACK


@pytest.mark.parametrize("auth", [grok_auth, chatgpt_auth])
def test_a_padded_email_is_shown_trimmed(monkeypatch, auth):
    monkeypatch.setattr(auth, "_well_formed", lambda: {"tokens": {"access_token": "t"}, "account": {"email": "  a@example.com "}})
    assert auth.signed_in_as() == "a@example.com"


def test_auth_status_never_shows_a_blank_account_name(monkeypatch):
    monkeypatch.setattr(grok_auth, "_well_formed", lambda: {"tokens": {"access_token": "t"}, "account": {"email": "  "}})
    monkeypatch.setattr(service, "_auth_file_problem", lambda spec: None)
    monkeypatch.setattr(grok_auth, "auth_file_problem", lambda: None)   # CLI-001: the shared reader asks the module
    rows = {row["provider"]: row for row in service.auth_status()["accounts"]}
    grok = rows["grok"]
    assert grok["signed_in"] is True
    assert grok["account"] == grok_auth.SIGNED_IN_FALLBACK


# --- W8 and report metadata ---------------------------------------------------------------

def _report(folder, ticker="NVDA", rating="Buy", date="2026-10-29"):
    folder.mkdir(parents=True)
    (folder / "complete_report.html").write_text("<html></html>", encoding="utf-8")
    (folder / "complete_report.md").write_text(
        f"# Trading Analysis Report: {ticker}\n\n- Analysis date: {date}\n- Rating: {rating}\n"
        "- Generated: 2026-10-29 21:12:00\n\n## I. Analyst Team Reports\n- Rating: Sell\n", encoding="utf-8")
    return folder


@pytest.fixture
def results(monkeypatch, tmp_path):
    root = tmp_path / "results"
    monkeypatch.setattr(service, "effective_results_dir", lambda: str(root))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    return root


def test_report_list_skips_symlinked_run_dirs_outside_the_results_dir(results, tmp_path):
    _report(results / "reports" / "NVDA_20261029_211200")
    outside = _report(tmp_path / "elsewhere" / "AAPL_20261028_100000", ticker="AAPL")
    (results / "reports" / "AAPL_20261028_100000").symlink_to(outside, target_is_directory=True)
    names = [row["name"] for row in service.list_reports()]
    assert names == ["NVDA_20261029_211200"]


def test_report_list_keeps_a_symlink_that_stays_inside_the_results_dir(results):
    real = _report(results / "reports" / "archive" / "TSLA_20261018_090000", ticker="TSLA", rating="Sell")
    (results / "reports" / "TSLA_20261018_090000").symlink_to(real, target_is_directory=True)
    assert [row["name"] for row in service.list_reports()] == ["TSLA_20261018_090000"]


def test_report_list_skips_a_markdown_symlink_that_leaves_the_results_dir(results, tmp_path):
    folder = _report(results / "reports" / "NVDA_20261029_211200")
    secret = tmp_path / "secret.md"
    secret.write_text("# Trading Analysis Report: SECRET\n", encoding="utf-8")
    (folder / "complete_report.md").unlink()
    (folder / "complete_report.md").symlink_to(secret)
    row = service.list_reports()[0]
    assert row["markdown"] is None
    assert row["ticker"] == "NVDA"


def test_report_list_reads_ticker_date_and_rating_from_the_header_only(results):
    _report(results / "reports" / "NVDA_20261029_211200", rating="Overweight")
    row = service.list_reports()[0]
    assert (row["ticker"], row["date"], row["rating"], row["generated"]) == ("NVDA", "2026-10-29", "Overweight", "2026-10-29 21:12:00")


# --- recent tickers -------------------------------------------------------------------------

def test_recent_tickers_are_newest_first_unique_and_from_real_jobs(results, tmp_path):
    logs = tmp_path / "webui" / "logs"
    logs.mkdir(parents=True)
    now = time.time()
    for index, (ticker, asset) in enumerate([("aapl", "stock"), ("BTC-USD", "crypto"), ("AAPL", "stock"), ("NVDA", "stock")]):
        path = logs / f"run{index}.job.json"
        path.write_text(json.dumps({"kind": "analysis", "selections": {"ticker": ticker, "asset_type": asset}}), encoding="utf-8")
        os.utime(path, (now + index, now + index))
    (logs / "bt.job.json").write_text(json.dumps({"kind": "backtest", "selections": {"ticker": "SPY"}}), encoding="utf-8")
    os.utime(logs / "bt.job.json", (now + 9, now + 9))
    (logs / "bad.job.json").write_text("{", encoding="utf-8")
    _report(results / "reports" / "TSLA_20261018_090000", ticker="TSLA")
    assert service.recent_tickers() == [
        {"ticker": "NVDA", "asset_type": "stock"},
        {"ticker": "AAPL", "asset_type": "stock"},
        {"ticker": "BTC-USD", "asset_type": "crypto"},
        {"ticker": "TSLA", "asset_type": None},
    ]
    assert len(service.recent_tickers(limit=2)) == 2


def test_recent_tickers_reach_the_page_through_the_tokened_config_route(console):  # noqa: F811
    status, body, _ = console["send"]("GET", "/api/config")
    assert status == 200
    assert json.loads(body)["recent"] == []
    status, _, _ = console["send"]("GET", "/api/config", token=False)
    assert status == 403


# --- timestamped status and the timeline ----------------------------------------------------

LOG = [
    "STATUS\tMarket Analyst\tin_progress\t20:35:10",
    "STATUS\tMarket Analyst\tcompleted\t20:36:52",
    "STATUS\tNews Analyst\tin_progress\t20:36:52",
    "STATUS\tNews Analyst\tcompleted\t20:39:31",
    "STATUS\tFundamentals Analyst\tcompleted\t20:39:31",
    "STATUS\tBull Researcher\tpending\t20:39:31",
    "STATUS\tBull Researcher\tin_progress\t20:39:33",
    "STATUS\tTrader\tbogus\t20:40:00",
    "STATUS\tTrader\tcompleted\tnot-a-clock",
]


def test_status_lines_with_and_without_a_clock_both_parse():
    assert progress.parse_status_lines(["STATUS\tTrader\tcompleted", "STATUS\tTrader\terror\t20:41:07"]) == {"Trader": "error"}
    assert progress.parse_status_lines(["STATUS\tTrader\tcompleted\tnot-a-clock"]) == {}


def test_progress_rows_carry_real_start_finish_and_elapsed_times():
    rows = {row["agent"]: row for row in progress.progress_rows(LOG, ["market", "news"])}
    assert "Fundamentals Analyst" not in rows
    assert rows["Market Analyst"]["elapsed"] == "1:42"
    assert rows["News Analyst"]["elapsed"] == "2:39"
    assert rows["Bull Researcher"]["status"] == "in_progress"
    assert (rows["Bull Researcher"]["started"], rows["Bull Researcher"]["elapsed"]) == ("20:39:33", "")
    assert rows["Trader"]["elapsed"] == "" and rows["Trader"]["status"] == "pending"


def test_elapsed_wraps_past_midnight():
    rows = progress.progress_rows(["STATUS\tTrader\tin_progress\t23:59:30", "STATUS\tTrader\tcompleted\t00:01:00"])
    assert next(row for row in rows if row["agent"] == "Trader")["elapsed"] == "1:30"


def test_timeline_is_newest_first_without_pending_or_unselected_roles():
    events = progress.progress_timeline(LOG, ["market", "news"])
    assert [(row["time"], row["zh"], row["status_zh"]) for row in events] == [
        ("20:39:33", "多头研究员", "进行中"),
        ("20:39:31", "新闻分析师", "完成"),
        ("20:36:52", "新闻分析师", "进行中"),
        ("20:36:52", "市场分析师", "完成"),
        ("20:35:10", "市场分析师", "进行中"),
    ]
    assert events[0]["team_zh"] == "研究团队"
    assert len(progress.progress_timeline(LOG, ["market", "news"], limit=2)) == 2


def test_the_worker_stamps_every_status_line(capsys):
    buffer = type("Buffer", (), {"agent_status": {"Trader": "in_progress"}})()
    service._emit_status(buffer, {})
    line = capsys.readouterr().out.strip()
    parts = line.split("\t")
    assert parts[:3] == ["STATUS", "Trader", "in_progress"] and progress._clock(parts[3])


def test_run_progress_puts_the_failed_role_on_top_of_the_timeline(monkeypatch):
    record = {"status": "failed", "selections": {"analysts": ["market"]}, "failure": {"role": "Bull Researcher"},
              "finished_at": "2026-10-30T20:41:07"}
    monkeypatch.setattr(service.JOBS, "current_run", lambda: record)
    monkeypatch.setattr(service, "read_log", lambda limit=40, run_id=None: {"lines": LOG[:2] + ["STATUS\tBull Researcher\tin_progress\t20:39:33"]})
    data = service.run_progress()
    assert data["timeline"][0] == {"agent": "Bull Researcher", "status": "error", "time": "20:41:07", "zh": "多头研究员",
                                   "team_zh": "研究团队", "status_zh": "错误"}
    assert next(row for row in data["agents"] if row["agent"] == "Bull Researcher")["status"] == "error"


# --- resume from checkpoint ------------------------------------------------------------------

def _restart_record(monkeypatch, saved):
    record = {"status": "failed", "pid": None, "selections": {"ticker": "BTC-USD", "checkpoint": None, "clear_checkpoints": True},
              "checkpoint": {"enabled": True, "saved": saved}}
    started = []
    monkeypatch.setattr(service.JOBS, "current_run", lambda: record)
    monkeypatch.setattr(service, "start_validated_run",
                        lambda selections, background=True, resume_from=None, framework="tradingagents": started.append(selections) or {"status": "running"})
    return started


def test_resume_turns_checkpoints_on_and_never_clears_them(monkeypatch):
    started = _restart_record(monkeypatch, saved=True)
    service.restart_run(resume=True)
    assert started[-1]["checkpoint"] is True and started[-1]["clear_checkpoints"] is False
    service.restart_run()
    assert started[-1]["checkpoint"] is None and started[-1]["clear_checkpoints"] is True


def test_resume_without_a_saved_checkpoint_is_refused(monkeypatch):
    started = _restart_record(monkeypatch, saved=None)
    with pytest.raises(service.ConsoleError) as error:
        service.restart_run(resume=True)
    assert error.value.status == 409 and "本次未保存断点" in str(error.value)
    assert started == []


def test_the_restart_route_passes_only_a_true_resume_flag(console, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(service, "restart_run", lambda resume=False, run_id=None: seen.append(resume) or {"status": "running"})
    for body in ({"id": "r1", "resume": True}, {"id": "r1"}, {"id": "r1", "resume": "yes"}):
        status, _, _ = console["send"]("POST", "/api/runs/restart", body)
        assert status == 200
    assert seen == [True, False, False]
    status, _, _ = console["send"]("POST", "/api/runs/restart", {"id": "r1", "resume": True}, token=False)
    assert status == 403
    assert seen == [True, False, False]


@pytest.mark.parametrize("path", ["/api/runs/stop", "/api/runs/restart", "/api/runs/cancel"])
def test_stop_restart_and_cancel_without_a_usable_id_are_refused_and_do_nothing(console, monkeypatch, path):  # noqa: F811
    """PM ruling (W36): a missing id counts as empty; no "newest run" fallback for any of the three."""
    seen = []
    monkeypatch.setattr(service, "stop_run", lambda run_id=None: seen.append(("stop", run_id)) or {"status": "stopped"})
    monkeypatch.setattr(service, "restart_run", lambda resume=False, run_id=None: seen.append(("restart", run_id)) or {"status": "running"})
    monkeypatch.setattr(service, "cancel_run", lambda run_id=None: seen.append(("cancel", run_id)) or {"status": "cancelled"})
    for body in ({}, {"resume": True}, {"id": None}, {"id": ""}, {"id": "   "}, {"id": 5}, {"id": ["r1"]}):
        status, payload, _ = console["send"]("POST", path, body)
        assert status == 400, (path, body, status, payload)
        assert "没有对任何任务执行操作" in payload.decode("utf-8"), payload
    assert seen == [], "nothing was stopped, restarted or cancelled"
    status, _, _ = console["send"]("POST", path, {"id": "r1"})
    assert status == 200 and seen == [(path.rsplit("/", 1)[1], "r1")]


# --- W6 --------------------------------------------------------------------------------

def test_the_session_probe_answers_only_with_the_current_token(console):  # noqa: F811
    status, body, _ = console["send"]("GET", "/api/session")
    assert status == 200
    assert json.loads(body) == {"ok": True}
    status, body, _ = console["send"]("GET", "/api/session", token=False)
    assert status == 403
    # The page looks for this phrase to show its refresh banner.
    assert "缺少或错误的控制台会话令牌" in json.loads(body)["error"]


# --- status page failure banner ---------------------------------------------------------

def test_the_failure_record_carries_the_redacted_summary_for_the_banner():
    record = {"status": "failed"}
    service.attach_failure(record, {
        "role": "Bull Researcher", "role_zh": "多头研究员", "kind": "timeout", "kind_zh": "调用超时",
        "provider": "grok", "provider_label": "Grok 订阅", "model": "grok-4.7", "timeout": 300, "retries": 2,
        "message": "boom api_key=sk-secretsecretsecret1234", "checkpoint": {"enabled": True, "saved": True},
    })
    assert record["failure"]["summary"] == "多头研究员 · Grok 订阅 grok-4.7 调用超时（300 秒，已重试 2 次）"
    assert "sk-secretsecretsecret1234" not in record["failure"]["message"]
    assert record["checkpoint"] == {"enabled": True, "saved": True}


def test_report_list_reads_models_and_debate_rounds_from_the_header(results):
    from tradingagents.reporting import analysts_line, model_line

    folder = _report(results / "reports" / "NVDA_20261029_211200")
    md = folder / "complete_report.md"
    split = model_line({"version": "0.3", "deep_think_provider": "chatgpt", "deep_think_llm": "gpt-6-sol",
                        "quick_think_provider": "grok", "quick_think_llm": "grok-4.7"})
    rounds = analysts_line({"analysts": ["market"], "max_debate_rounds": 3, "max_risk_discuss_rounds": 1})
    md.write_text(md.read_text(encoding="utf-8").replace("- Generated:", f"- {split}\n- {rounds}\n- Generated:"), encoding="utf-8")
    row = service.list_reports()[0]
    assert (row["deep_model"], row["quick_model"], row["debate_rounds"]) == ("gpt-6-sol", "grok-4.7", 3)
    same = model_line({"version": "0.3", "llm_provider": "grok", "deep_think_llm": "grok-4.7-heavy", "quick_think_llm": "?"})
    assert service._header_models(same) == {"deep": "grok-4.7-heavy"}
    _report(results / "reports" / "AAPL_20261028_100000", ticker="AAPL")
    older = [row for row in service.list_reports() if row["ticker"] == "AAPL"][0]
    assert (older["deep_model"], older["quick_model"], older["debate_rounds"]) == (None, None, None)
