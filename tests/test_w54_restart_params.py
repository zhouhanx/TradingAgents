"""W54 (QA r3, PM ruling): 重新启动 works again, and no secret is written down.

Before W54 the run record on disk was redacted by key name, so ``max_tokens``
(its key contains "token") was stored as "[redacted]" and every 重新启动 built
a run that ended at ``int("[redacted]")`` before any model was called.

Now a token-count parameter holding a number is kept everywhere (record, job
file, API answers); real secret keys are still redacted in all of them. A
record written before W54 restarts with the default and says so in one line.

The restart tests run the real worker process (``python -m webui.worker``
entry point, real ``default_runner`` and ``build_run_config``) up to the model
boundary: ``create_tier_client`` is replaced in that process by a stub that
writes down the config it got and raises, so no model is called and the
network is blocked.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from webui import service

pytestmark = pytest.mark.unit

SEL = {
    "ticker": "NVDA", "analysis_date": "2026-10-30", "asset_type": "stock", "analysts": ["market"],
    "research_depth": 1, "llm_provider": "grok", "quick_think_llm": "grok-4.7", "deep_think_llm": "grok-4.7",
    "output_language": "English", "save": False, "html": False, "checkpoint": False,
    "backend_url": None, "quick_think_provider": "grok", "deep_think_provider": "grok",
}
FAKE_KEY = "sk-W54FAKEkey0123456789abcdef"
FAKE_TOKEN = "xai-W54FAKEtoken9876543210zyx"
FAKE_REFRESH = "rt_W54FAKErefresh5555aaaa"
FAKES = (FAKE_KEY, FAKE_TOKEN, FAKE_REFRESH, "W54FAKE")
BOUNDARY = "ModelBoundaryReached"

WORKER = textwrap.dedent("""
    import json, os, socket, sys
    def _blocked(*a, **k):
        raise OSError("network blocked in the W54 test")
    socket.socket.connect = _blocked
    socket.getaddrinfo = _blocked
    socket.create_connection = _blocked
    from tradingagents.graph import trading_graph
    class ModelBoundaryReached(RuntimeError):
        pass
    job_path, message = sys.argv[1], os.environ.get("W54_BOUNDARY_MESSAGE", "stopped at the model boundary")
    def boundary(config, tier, **kwargs):
        keys = ("max_tokens", "temperature", "llm_max_retries", "max_tool_rounds")
        with open(job_path + ".boundary.json", "w", encoding="utf-8") as handle:
            json.dump({key: config.get(key) for key in keys}, handle)
        raise ModelBoundaryReached(message)
    trading_graph.create_tier_client = boundary
    from webui import worker
    sys.exit(worker.main([job_path]))
""")


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    for env, key, folder in (("TRADINGAGENTS_CACHE_DIR", "data_cache_dir", "cache"),
                             ("TRADINGAGENTS_RESULTS_DIR", "results_dir", "results")):
        monkeypatch.setenv(env, str(tmp_path / folder))
        monkeypatch.setitem(DEFAULT_CONFIG, key, str(tmp_path / folder))
    monkeypatch.setenv("TRADINGAGENTS_MEMORY_LOG_PATH", str(tmp_path / "memory.md"))
    monkeypatch.setitem(DEFAULT_CONFIG, "memory_log_path", str(tmp_path / "memory.md"))
    for env in ("TRADINGAGENTS_WEBUI_MAX_RUNS", "TRADINGAGENTS_WEBUI_MAX_QUEUE", "TRADINGAGENTS_MAX_TOKENS",
                "TRADINGAGENTS_CHECKPOINT_ENABLED"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(service, "remember_run", lambda selections: None)
    yield tmp_path / "webui"
    for record in service.JOBS.list_runs():
        if isinstance(record.get("pid"), int):
            with contextlib.suppress(OSError):
                os.killpg(record["pid"], 9)
    service.set_runner(None)
    service.set_spawn_argv(None)


def real_worker() -> None:
    """Runs start the real worker entry point; only the model client is stubbed.

    The worker's argv is stored on the run record, so the boundary's error text
    comes from the environment (W54_BOUNDARY_MESSAGE), never from argv.
    """
    service.set_spawn_argv(lambda job: [sys.executable, "-c", WORKER, job["job_path"]])


def wait_done(run_id: str) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        record = service.JOBS.read_run(run_id)
        service.refresh_run_process(record)
        record = service.JOBS.read_run(run_id)
        if record.get("status") not in service.LIVE_STATUSES:
            return record
        time.sleep(0.1)
    raise AssertionError(f"{run_id} did not end: {record}")


def stored(home: Path, run_id: str) -> dict:
    return json.loads((home / "runs" / f"{run_id}.json").read_text(encoding="utf-8"))


def job_of(home: Path, run_id: str) -> dict:
    return json.loads((home / "logs" / f"{run_id}.job.json").read_text(encoding="utf-8"))


def boundary_config(home: Path, run_id: str) -> dict:
    return json.loads((home / "logs" / f"{run_id}.job.json.boundary.json").read_text(encoding="utf-8"))


def failure_of(home: Path, run_id: str) -> dict:
    return json.loads((home / "logs" / f"{run_id}.failure.json").read_text(encoding="utf-8"))


def old_record(home: Path, run_id: str = "20261031_090000_0ld0ld00", **selections) -> str:
    """A failed run as 3cca40a wrote it: max_tokens was redacted on disk."""
    (home / "runs").mkdir(parents=True, exist_ok=True)
    record = {
        "id": run_id, "kind": "analysis", "status": "failed", "ticker": "NVDA", "date": "2026-10-30",
        "selections": {**SEL, "max_tokens": "[redacted]", **selections},
        "log": str(home / "logs" / f"{run_id}.log"), "result": str(home / "logs" / f"{run_id}.json"),
        "started_at": "2026-10-31T09:00:00", "finished_at": "2026-10-31T09:00:05",
        "pid": None, "returncode": 1, "checkpoint": {"enabled": False, "saved": None, "scope": run_id},
    }
    (home / "runs" / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return run_id


# ------------------------------------------------------------------ redaction rule

def in_selections(params: dict) -> dict:
    """``params`` as a run record holds them; returns what is left after redaction."""
    return service.redact({"id": "r", "selections": params})["selections"]


@pytest.mark.parametrize("key", ["max_tokens", "max_output_tokens"])
@pytest.mark.parametrize("value", [4096, 0.5, "800", None, ""])
def test_the_runs_own_token_counts_with_a_number_are_kept(key, value):
    assert in_selections({key: value}) == {key: value}
    # the same in a job file and in an API answer listing runs
    assert service.redact({"id": "j", "kind": "analysis", "selections": {key: value}})["selections"] == {key: value}
    assert service.redact({"runs": [{"selections": {key: value}}]}) == {"runs": [{"selections": {key: value}}]}


# MF-1 (QA r4, PM): exact, case-sensitive keys only, and only directly in selections.
@pytest.mark.parametrize("key", ["MAX_TOKENS", "Max_Tokens", "max_Tokens", "MAX_OUTPUT_TOKENS", "Max_Output_Tokens",
                                 "max_refresh_tokens", "max_access_tokens", "min_x_tokens", "min_tokens",
                                 "max_completion_tokens", "max_tokens_", "_max_tokens", "max_tokens2",
                                 "max_output_tokens_secret", " max_tokens"])
@pytest.mark.parametrize("value", [123456789, "123456789", 0.5])
def test_case_variants_and_other_token_names_are_redacted_even_when_numeric(key, value):
    assert in_selections({key: value}) == {key: "[redacted]"}


@pytest.mark.parametrize("value", [2048, "2048"])
def test_the_same_keys_nested_anywhere_else_are_redacted(value):
    portfolio = {"max_tokens": value, "max_output_tokens": value,
                 "positions": [{"ticker": "NVDA", "quantity": 1, "max_tokens": value,
                                "selections": {"max_tokens": value}}],
                 "broker": {"max_tokens": value, "deep": [{"max_output_tokens": value}]},
                 "selections": {"max_tokens": value}}
    params = {"max_tokens": value, "portfolio": portfolio, "data_vendors": {"max_tokens": value},
              "global_news_queries": [{"max_tokens": value}]}
    kept = in_selections(params)
    assert kept["max_tokens"] == value                                   # the run's own parameter
    held = kept["portfolio"]
    assert held["max_tokens"] == held["max_output_tokens"] == "[redacted]"
    assert held["positions"][0]["max_tokens"] == "[redacted]"
    assert held["positions"][0]["selections"]["max_tokens"] == "[redacted]"   # a list does not reset the scope
    assert held["broker"]["max_tokens"] == held["broker"]["deep"][0]["max_output_tokens"] == "[redacted]"
    assert held["selections"]["max_tokens"] == "[redacted]"
    assert kept["data_vendors"]["max_tokens"] == kept["global_news_queries"][0]["max_tokens"] == "[redacted]"
    # outside any selections (top level of a record, a job file or an answer)
    assert service.redact({"max_tokens": value}) == {"max_tokens": "[redacted]"}
    assert service.redact({"run": {"max_tokens": value, "selections": {}}})["run"]["max_tokens"] == "[redacted]"


@pytest.mark.parametrize("key", ["token", "api_key", "access_token", "refresh_token", "id_token", "secret",
                                 "client_secret", "password", "authorization", "OPENAI_API_KEY", "session_token"])
def test_real_secret_keys_are_still_redacted(key):
    assert service.redact({key: FAKE_KEY, "nested": [{key: FAKE_KEY}]}) == {
        key: "[redacted]", "nested": [{key: "[redacted]"}]}
    assert in_selections({key: FAKE_KEY, "portfolio": {key: FAKE_KEY}}) == {
        key: "[redacted]", "portfolio": {key: "[redacted]"}}


@pytest.mark.parametrize("value", [FAKE_KEY, {"key": FAKE_KEY}, [FAKE_KEY], True])
def test_a_token_count_key_holding_something_else_is_redacted(value):
    assert in_selections({"max_tokens": value}) == {"max_tokens": "[redacted]"}
    assert in_selections({"max_output_tokens": value}) == {"max_output_tokens": "[redacted]"}


def test_saved_settings_still_never_store_secret_looking_keys(home):
    saved = service.save_config({"api_key": FAKE_KEY, "access_token": FAKE_TOKEN, "max_tokens": 4096})
    assert "api_key" not in saved and "access_token" not in saved and "max_tokens" not in saved
    assert FAKE_KEY not in (home / "config.json").read_text(encoding="utf-8")


# ------------------------------------------------------------------ persistence

def test_the_run_record_and_job_file_keep_max_tokens(home):
    service.set_runner(lambda job: 0)
    answer = service.start_validated_run({**SEL, "max_tokens": 4096, "temperature": 0.2}, background=False)
    assert answer["selections"]["max_tokens"] == 4096                 # API answer shows the number
    record = stored(home, answer["id"])
    assert record["selections"]["max_tokens"] == 4096
    assert record["selections"]["temperature"] == 0.2
    assert job_of(home, answer["id"])["selections"]["max_tokens"] == 4096
    assert service.build_run_config(record["selections"])["max_tokens"] == 4096


def test_a_restart_of_a_new_record_keeps_max_tokens_through_the_real_worker(home):
    real_worker()
    first = service.start_validated_run({**SEL, "max_tokens": 4096})
    assert wait_done(first["id"])["status"] == "failed"
    assert boundary_config(home, first["id"])["max_tokens"] == 4096
    again = service.restart_run(run_id=first["id"])
    assert "notices" not in again and "已改用默认值" not in again["zh"]
    record = wait_done(again["id"])
    failure = failure_of(home, again["id"])
    assert failure["error_type"] == BOUNDARY, failure            # reached the model, not int()
    assert boundary_config(home, again["id"])["max_tokens"] == 4096
    assert record["selections"]["max_tokens"] == 4096


# ------------------------------------------------------------------ old records

def test_an_old_redacted_record_crashed_the_real_worker_before_the_model(home):
    """The W54 repro: the job a pre-W54 restart built ends at int('[redacted]')."""
    run_id = old_record(home)
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    job = {"id": "repro", "kind": "analysis", "selections": stored(home, run_id)["selections"],
           "result_path": str(logs / "repro.json"), "log": str(logs / "repro.log"),
           "job_path": str(logs / "repro.job.json"), "checkpoint_scope": "repro"}
    Path(job["job_path"]).write_text(json.dumps(job), encoding="utf-8")
    import subprocess
    done = subprocess.run([sys.executable, "-c", WORKER, job["job_path"]], capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 1
    assert failure_of(home, "repro")["error_type"] == "ValueError"
    assert not Path(job["job_path"] + ".boundary.json").exists()


def test_an_old_redacted_record_restarts_with_the_default_and_says_so(home):
    real_worker()
    run_id = old_record(home)
    answer = service.restart_run(run_id=run_id)
    notice = "原任务的 max_tokens 已被脱敏，已改用默认值。"
    assert answer["zh"].startswith(notice)
    assert answer["notices"] == [notice]
    record = wait_done(answer["id"])
    assert record["notices"] == [notice]
    assert record["selections"]["max_tokens"] is None
    assert job_of(home, answer["id"])["selections"]["max_tokens"] is None
    failure = failure_of(home, answer["id"])
    assert failure["error_type"] == BOUNDARY, failure            # no int() crash any more
    assert boundary_config(home, answer["id"])["max_tokens"] == DEFAULT_CONFIG.get("max_tokens")


def test_other_junk_numbers_fall_back_too_and_good_ones_stay():
    clean, notices = service.restart_selections(
        {**SEL, "max_tokens": "[redacted]", "temperature": "warm", "llm_max_retries": "3", "max_tool_rounds": 7})
    assert (clean["max_tokens"], clean["temperature"], clean["llm_max_retries"], clean["max_tool_rounds"]) == (
        None, None, "3", 7)
    assert notices == ["原任务的 max_tokens 已被脱敏，已改用默认值。", "原任务的 temperature 不是数字，已改用默认值。"]
    assert service.restart_selections({**SEL, "max_tokens": 4096}) == ({**SEL, "max_tokens": 4096}, [])


def test_a_queued_restart_keeps_the_notice(home, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_RUNS", "1")
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "import time; time.sleep(60)"])
    service.start_validated_run(dict(SEL))
    answer = service.restart_run(run_id=old_record(home))
    assert answer["status"] == "queued"
    assert answer["zh"].startswith("原任务的 max_tokens 已被脱敏，已改用默认值。 已加入队列")
    assert stored(home, answer["id"])["notices"] == ["原任务的 max_tokens 已被脱敏，已改用默认值。"]
    assert job_of(home, answer["id"])["selections"]["max_tokens"] is None


# ------------------------------------------------------------------ planted secret (QA r3 add-on)

def test_a_planted_secret_never_reaches_the_record_the_failure_file_or_the_log(home, monkeypatch):
    """A fake key and token planted in the run's inputs and in the model error text."""
    monkeypatch.setenv("W54_BOUNDARY_MESSAGE", f"provider said: api_key={FAKE_KEY} "
                       f"Authorization: Bearer {FAKE_TOKEN} refresh_token={FAKE_REFRESH}")
    real_worker()
    portfolio = {"positions": [{"ticker": "NVDA", "quantity": 1, "average_price": 100, "api_key": FAKE_KEY}],
                 "cash": 1000, "currency": "USD", "access_token": FAKE_TOKEN,
                 "broker": {"refresh_token": FAKE_REFRESH, "secret": FAKE_KEY}}
    first = service.start_validated_run({**SEL, "max_tokens": 2048, "portfolio": portfolio,
                                         "api_key": FAKE_KEY, "token": FAKE_TOKEN})
    assert not any(fake in json.dumps(first) for fake in FAKES)          # API answer
    wait_done(first["id"])
    again = service.restart_run(run_id=first["id"])
    assert not any(fake in json.dumps(again) for fake in FAKES)
    wait_done(again["id"])
    for run_id in (first["id"], again["id"]):
        assert failure_of(home, run_id)["error_type"] == BOUNDARY
        files = {
            "record": home / "runs" / f"{run_id}.json",
            "failure": home / "logs" / f"{run_id}.failure.json",
            "log": home / "logs" / f"{run_id}.log",
            "job": home / "logs" / f"{run_id}.job.json",
        }
        for name, path in files.items():
            text = path.read_text(encoding="utf-8")
            leaked = [fake for fake in FAKES if fake in text]
            assert not leaked, f"{name} {path.name} holds {leaked}"
        record = json.loads(files["record"].read_text(encoding="utf-8"))
        assert record["selections"]["max_tokens"] == 2048              # the number is kept
        assert record["selections"]["portfolio"]["access_token"] == "[redacted]"
        assert record["selections"]["portfolio"]["positions"][0]["average_price"] == 100
        assert BOUNDARY in files["log"].read_text(encoding="utf-8")   # the log is the real worker's
    everything = "".join(path.read_text(encoding="utf-8", errors="replace")
                         for path in home.rglob("*") if path.is_file())
    assert not [fake for fake in FAKES if fake in everything]


# ------------------------------------------------------------------ MF-1 end to end (QA r4)

NUMERIC_SECRETS = ("918273645", "564738291", "736251849", "192837465")


def test_only_selections_max_tokens_survives_a_first_run_and_a_restart(home):
    """MF-1: 2048 is kept through the real worker; look-alike numeric keys never reach disk."""
    real_worker()
    portfolio = {"positions": [{"ticker": "NVDA", "quantity": 1, "average_price": 100, "max_tokens": 918273645}],
                 "cash": 1000, "currency": "USD", "max_output_tokens": 564738291}
    first = service.start_validated_run({**SEL, "max_tokens": 2048, "portfolio": portfolio,
                                         "MAX_TOKENS": 736251849, "max_refresh_tokens": 192837465})
    assert first["selections"]["max_tokens"] == 2048
    assert not any(number in json.dumps(first) for number in NUMERIC_SECRETS)
    wait_done(first["id"])
    again = service.restart_run(run_id=first["id"])
    assert "已改用默认值" not in again["zh"]
    assert not any(number in json.dumps(again) for number in NUMERIC_SECRETS)
    wait_done(again["id"])
    for run_id in (first["id"], again["id"]):
        assert failure_of(home, run_id)["error_type"] == BOUNDARY
        assert boundary_config(home, run_id)["max_tokens"] == 2048     # the worker got the number
        for path in (home / "runs" / f"{run_id}.json", home / "logs" / f"{run_id}.job.json",
                     home / "logs" / f"{run_id}.failure.json", home / "logs" / f"{run_id}.log"):
            text = path.read_text(encoding="utf-8")
            assert not [number for number in NUMERIC_SECRETS if number in text], path.name
        record = stored(home, run_id)
        assert record["selections"]["max_tokens"] == 2048
        assert record["selections"]["MAX_TOKENS"] == record["selections"]["max_refresh_tokens"] == "[redacted]"
        assert record["selections"]["portfolio"]["max_output_tokens"] == "[redacted]"
        assert record["selections"]["portfolio"]["positions"][0]["max_tokens"] == "[redacted]"
