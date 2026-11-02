"""RUN-003b console fixes: backtest bounds/checkpoints, W24, W44, W45. No model is called."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tradingagents.graph import checkpointer
from webui import service

pytestmark = pytest.mark.unit

SEL = {"ticker": "NVDA", "analysis_date": "2026-10-30", "asset_type": "stock", "analysts": ["market"]}
BT = {"tickers": "NVDA", "start": "2026-10-01", "end": "2026-10-01", "every": 7,
      "llm_provider": "chatgpt", "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol"}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    for name in ("TRADINGAGENTS_WEBUI_MAX_RUNS", "TRADINGAGENTS_WEBUI_MAX_QUEUE", "TRADINGAGENTS_RESULTS_DIR",
                 "TRADINGAGENTS_CHECKPOINT_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(service, "remember_run", lambda selections: None)
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    yield tmp_path / "webui"
    service.set_runner(None)
    service.set_spawn_argv(None)


@pytest.fixture
def sleepers(home):
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "import time; time.sleep(60)"])
    yield
    for record in service.JOBS.list_runs():
        if isinstance(record.get("pid"), int):
            with contextlib.suppress(OSError):
                os.killpg(record["pid"], 9)


def _selections():
    return service.validate_analysis({"ticker": "NVDA", "date": "2026-10-29", "llm_provider": "chatgpt",
                                      "quick_think_llm": "gpt-6-luna",
                                      "deep_think_llm": "gpt-6-sol"})["selections"]


def _start(ticker="NVDA"):
    return service.start_validated_run({**SEL, "ticker": ticker})


# ------------------------------------------------------------------ W44

def test_a_backtest_starts_in_a_free_slot_beside_an_analysis(sleepers):
    a = _start("AAA")
    bt = service.start_backtest(BT)
    assert a["status"] == "running" and bt["status"] == "running" and bt["kind"] == "backtest"
    c = _start("CCC")
    assert c["status"] == "queued"                      # the backtest took one slot, not both


def test_a_running_backtest_leaves_the_other_slots_free(sleepers, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_RUNS", "3")
    bt = service.start_backtest(BT)
    a, b = _start("AAA"), _start("BBB")
    assert bt["status"] == a["status"] == b["status"] == "running"
    assert _start("CCC")["status"] == "queued"


def test_a_backtest_is_refused_when_every_slot_is_busy_and_nothing_is_written(sleepers):
    _start("AAA"), _start("BBB")
    before = len(service.JOBS.list_runs())
    with pytest.raises(service.ConsoleError) as refused:
        service.start_backtest(BT)
    assert refused.value.status == 409
    assert "回测没有启动" in str(refused.value) and "上限 2 个" in str(refused.value)
    assert len(service.JOBS.list_runs()) == before


def test_a_backtest_never_jumps_the_queue(sleepers, monkeypatch):
    _start("AAA"), _start("BBB")
    _start("CCC")                                      # waits
    monkeypatch.setattr(service, "_slots_used", lambda records, limits: 0)   # a slot looks free
    monkeypatch.setattr(service, "dispatch_queue", lambda: [])                # ...and the queue keeps waiting
    with pytest.raises(service.ConsoleError, match="另有 1 个在排队"):
        service.start_backtest(BT)


# ------------------------------------------------------------------ backtest time limits + checkpoints

def _capture_backtest(monkeypatch):
    seen = {}

    def fake(tickers, dates, config, **kwargs):
        seen["config"] = dict(config)
        raise SystemExit(0)

    import tradingagents.backtest as bt
    monkeypatch.setattr(bt, "run_backtest", fake)
    return seen


def _backtest_job(home, **payload):
    jobs = []
    service.set_runner(lambda job: jobs.append(job) or 0)
    service.start_backtest({**BT, **payload})
    return jobs[0]


def test_a_backtest_gets_the_console_call_bounds_and_checkpoints(home, monkeypatch):
    job = _backtest_job(home)
    assert job["checkpoint_scope"] == job["id"]
    seen = _capture_backtest(monkeypatch)
    with pytest.raises(SystemExit):
        service.run_backtest_job(job)
    config = seen["config"]
    assert config["timeout"] == service.CONSOLE_MODEL_TIMEOUT
    assert config["llm_call_budget"] == service.CONSOLE_CALL_BUDGET
    assert config["checkpoint_enabled"] is True
    assert config["checkpoint_run_scope"] == job["id"]


def test_a_named_backtest_resumes_from_its_own_scope(home, monkeypatch):
    job = _backtest_job(home, run_id="sweep1")
    assert job["checkpoint_scope"] == "backtest-sweep1"
    assert service.backtest_checkpoint_scope("x1", "a.b") == "x1"      # not a scope name: its own


def test_the_page_can_still_turn_backtest_checkpoints_off(home, monkeypatch):
    job = _backtest_job(home)
    job["selections"]["checkpoint"] = False
    seen = _capture_backtest(monkeypatch)
    with pytest.raises(SystemExit):
        service.run_backtest_job(job)
    assert seen["config"]["checkpoint_enabled"] is False
    assert "checkpoint_run_scope" not in seen["config"] or not seen["config"]["checkpoint_run_scope"]


def test_page_bounds_win_for_a_backtest(home, monkeypatch):
    job = _backtest_job(home, timeout=42, llm_call_budget=99)
    seen = _capture_backtest(monkeypatch)
    with pytest.raises(SystemExit):
        service.run_backtest_job(job)
    assert (seen["config"]["timeout"], seen["config"]["llm_call_budget"]) == (42.0, 99.0)


# ------------------------------------------------------------------ W24

def test_results_go_under_the_console_home_when_it_is_set(home, monkeypatch):
    assert service.effective_results_dir() == str(home / "results")
    assert service.build_run_config(_selections())["results_dir"] == str(home / "results")


def test_the_results_variable_still_wins(home, monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path / "elsewhere"))
    assert service.effective_results_dir() == str(tmp_path / "elsewhere")
    assert service.build_run_config(_selections())["results_dir"] == str(tmp_path / "elsewhere")


def test_without_a_console_home_nothing_changes(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_HOME", raising=False)
    monkeypatch.setattr(service, "load_saved_config", lambda: {})
    assert service.console_default_results_dir() == service.DEFAULT_CONFIG["results_dir"]


# ------------------------------------------------------------------ W45

def _record(run_id, status, cache, ended=None, **extra):
    record = {"id": run_id, "status": status, "selections": {**SEL, "data_cache_dir": str(cache)},
              "result": str(service.webui_home() / "runs" / f"{run_id}.json"), **extra}
    if ended is not None:
        record["finished_at"] = ended.isoformat(timespec="seconds")
    service.JOBS.write_run(record)
    return record


def _scope(cache, scope):
    path = checkpointer.scope_dir(cache, scope)
    path.mkdir(parents=True, exist_ok=True)
    (path / "NVDA.db").write_bytes(b"x")
    return path


def test_checkpoint_cleanup_rule(home, monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setattr(service, "_selections_cache_dir", lambda selections: selections["data_cache_dir"])
    now = datetime(2026, 11, 2, 12, 0, 0)
    old, fresh = now - timedelta(days=service.CHECKPOINT_KEEP_DAYS + 1), now - timedelta(days=1)
    _record("r_done", "finished", cache, fresh)
    _record("r_failed_old", "failed", cache, old)
    _record("r_failed_new", "failed", cache, fresh)
    _record("r_stopped_noend", "stopped", cache)
    _record("r_running", "running", cache)
    _record("r_queued", "queued", cache)
    _record("r_done_src", "finished", cache, fresh)
    _record("r_old_src", "failed", cache, old)
    job = service.webui_home() / "runs" / "r_queued.job.json"
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(json.dumps({"checkpoint_scope": "r_queued", "resume_from": {"scope": "r_old_src"}}))
    bt_job = service.webui_home() / "runs" / "r_bt.job.json"
    bt_job.write_text(json.dumps({"checkpoint_scope": "backtest-sweep"}))
    _record("r_bt", "finished", cache, fresh)
    paths = {name: _scope(cache, name) for name in (
        "r_done", "r_failed_old", "r_failed_new", "r_stopped_noend", "r_running", "r_queued",
        "r_old_src", "backtest-sweep", "someone_else")}
    removed = service.prune_run_checkpoints(now.timestamp())
    assert sorted(Path(p).name for p in removed) == ["backtest-sweep", "r_done", "r_failed_old"]
    gone = {name for name, path in paths.items() if not path.exists()}
    assert gone == {"r_done", "r_failed_old", "backtest-sweep"}
    # kept: a recent failure, a failure with no end time, live/queued runs, a queued resume's
    # source, and a scope this home has no record of (the cache may be shared)


def test_cleanup_is_throttled_from_the_dispatcher(home, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "prune_run_checkpoints", lambda now=None: calls.append(now) or [])
    monkeypatch.setattr(service, "_last_checkpoint_prune", 1000.0)
    service.maybe_prune_run_checkpoints(1000.0 + service.CHECKPOINT_PRUNE_EVERY - 1)
    assert calls == []
    service.maybe_prune_run_checkpoints(1000.0 + service.CHECKPOINT_PRUNE_EVERY + 1)
    assert len(calls) == 1


def test_the_console_start_runs_the_cleanup(home, monkeypatch):
    # W111: recover_runs_on_start → dispatch_queue → maybe_prune, then a direct
    # prune_run_checkpoints; product code unchanged since 358aee3.
    calls = []
    monkeypatch.setattr(service, "prune_run_checkpoints", lambda now=None: calls.append(1) or [])
    service.recover_runs_on_start()
    assert calls == [1, 1]


def test_a_finished_analysis_loses_its_checkpoint_on_the_next_sweep(home, monkeypatch, tmp_path):
    monkeypatch.setattr(service, "effective_cache_dir", lambda: str(tmp_path / "c"))
    monkeypatch.setattr(service, "_selections_cache_dir", lambda selections: str(tmp_path / "c"))
    service.set_runner(lambda job: 0)
    done = service.start_validated_run(dict(SEL))
    service.JOBS.write_run({**service.JOBS.read_run(done["id"]), "status": "finished"})
    path = _scope(tmp_path / "c", done["id"])
    assert service.prune_run_checkpoints(time.time()) == [str(path)]


# ------------------------------------------------------------------ MF-1 (W45, QA r1)

def _shared(name, status, cache, ended, scope="backtest-x"):
    job = service.webui_home() / "runs" / f"{name}.job.json"
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(json.dumps({"checkpoint_scope": scope}))
    return _record(name, status, cache, ended)


def _mf1_setup(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setattr(service, "_selections_cache_dir", lambda selections: selections["data_cache_dir"])
    return cache, datetime(2026, 11, 2, 12, 0, 0)


def test_qa_repro_completed_then_failed_same_run_id_keeps_the_checkpoint(home, monkeypatch, tmp_path):
    cache, now = _mf1_setup(monkeypatch, tmp_path)
    _shared("bt_a", "finished", cache, now - timedelta(days=2))
    _shared("bt_b", "failed", cache, now - timedelta(hours=1))
    path = _scope(cache, "backtest-x")
    assert service.prune_run_checkpoints(now.timestamp()) == []
    assert path.exists()


@pytest.mark.parametrize("status", ["failed", "stopped", "cancelled", "interrupted"])
def test_any_unfinished_record_in_its_window_protects_a_shared_scope(home, monkeypatch, tmp_path, status):
    cache, now = _mf1_setup(monkeypatch, tmp_path)
    _shared("old_failed", "failed", cache, now - timedelta(days=service.CHECKPOINT_KEEP_DAYS + 3))
    _shared("done", "finished", cache, now - timedelta(days=1))
    _shared("recent", status, cache, now - timedelta(days=service.CHECKPOINT_KEEP_DAYS - 1))
    path = _scope(cache, "backtest-x")
    assert service.prune_run_checkpoints(now.timestamp()) == []
    assert path.exists()


def test_a_shared_scope_with_no_end_time_is_kept(home, monkeypatch, tmp_path):
    cache, now = _mf1_setup(monkeypatch, tmp_path)
    _shared("done", "finished", cache, now - timedelta(days=1))
    _shared("stopped", "stopped", cache, None)
    path = _scope(cache, "backtest-x")
    service.prune_run_checkpoints(now.timestamp())
    assert path.exists()


def test_a_running_record_protects_a_shared_scope(home, monkeypatch, tmp_path):
    cache, now = _mf1_setup(monkeypatch, tmp_path)
    _shared("done", "finished", cache, now - timedelta(days=1))
    _shared("live", "running", cache, None)
    path = _scope(cache, "backtest-x")
    service.prune_run_checkpoints(now.timestamp())
    assert path.exists()


def test_a_shared_scope_goes_once_every_record_is_past_its_window(home, monkeypatch, tmp_path):
    cache, now = _mf1_setup(monkeypatch, tmp_path)
    _shared("done", "finished", cache, now - timedelta(days=1))
    _shared("old", "failed", cache, now - timedelta(days=service.CHECKPOINT_KEEP_DAYS + 1))
    path = _scope(cache, "backtest-x")
    assert service.prune_run_checkpoints(now.timestamp()) == [str(path)]
    assert not path.exists()


def test_a_recent_failure_does_not_protect_another_scope(home, monkeypatch, tmp_path):
    cache, now = _mf1_setup(monkeypatch, tmp_path)
    _shared("done", "finished", cache, now - timedelta(days=1), scope="backtest-y")
    _shared("recent", "failed", cache, now - timedelta(hours=1), scope="backtest-x")
    y, x = _scope(cache, "backtest-y"), _scope(cache, "backtest-x")
    assert service.prune_run_checkpoints(now.timestamp()) == [str(y)]
    assert x.exists()
