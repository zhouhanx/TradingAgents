"""Independent review of the uncommitted restart, redaction and identity changes.

The author tests are not this file. These checks are the review's own proof that
an environment overlay does not rewrite a stored restart, that the old redaction
notice and output paths still hold, and that a reused pid is not signalled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tradingagents.default_config import DEFAULT_CONFIG
from webui import service

pytestmark = pytest.mark.unit

STORED = {
    "ticker": "NVDA",
    "analysis_date": "2026-10-30",
    "asset_type": "stock",
    "analysts": ["market"],
    "research_depth": 1,
    "llm_provider": "grok",
    "quick_think_llm": "grok-4.7",
    "deep_think_llm": "grok-4.7",
    "quick_think_provider": "grok",
    "deep_think_provider": "grok",
    "output_language": "English",
    "backend_url": None,
    "save": False,
    "html": False,
    "checkpoint": False,
    "max_tokens": 4096,
    "temperature": 0.2,
}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    monkeypatch.setattr(service, "remember_run", lambda selections: None)
    yield tmp_path
    service.set_runner(None)


def _record(home: Path, selections: dict, run_id: str = "20261104_120000_review01") -> str:
    folder = home / "webui"
    (folder / "runs").mkdir(parents=True, exist_ok=True)
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    record = {
        "id": run_id,
        "kind": "analysis",
        "status": "failed",
        "ticker": selections["ticker"],
        "date": selections["analysis_date"],
        "selections": selections,
        "log": str(folder / "logs" / f"{run_id}.log"),
        "result": str(folder / "logs" / f"{run_id}.json"),
    }
    (folder / "runs" / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")
    return run_id


def test_environment_overlay_does_not_rewrite_a_stored_restart(home, monkeypatch, tmp_path):
    root = tmp_path / ".tradingagents"
    stored_cache = root / "stored-cache"
    stored_results = root / "stored-results"
    stored_memory = root / "stored-memory.md"
    env_cache = tmp_path / "env-cache"
    selections = {
        **STORED,
        "data_cache_dir": str(stored_cache),
        "results_dir": str(stored_results),
        "memory_log_path": str(stored_memory),
    }
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(env_cache))
    monkeypatch.setenv("TRADINGAGENTS_RESULTS_DIR", str(tmp_path / "env-results"))
    monkeypatch.setenv("TRADINGAGENTS_MEMORY_LOG_PATH", str(tmp_path / "env-memory.md"))
    monkeypatch.setenv("TRADINGAGENTS_MAX_TOKENS", "128")
    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "1")
    monkeypatch.setitem(DEFAULT_CONFIG, "data_cache_dir", str(env_cache))
    monkeypatch.setitem(DEFAULT_CONFIG, "results_dir", str(tmp_path / "env-results"))
    monkeypatch.setitem(DEFAULT_CONFIG, "memory_log_path", str(tmp_path / "env-memory.md"))
    monkeypatch.setitem(DEFAULT_CONFIG, "max_tokens", "128")
    monkeypatch.setitem(DEFAULT_CONFIG, "output_language", "Japanese")
    seen = {}

    def runner(job):
        seen["selections"] = dict(job["selections"])
        seen["config"] = service.build_run_config(job["selections"])
        seen["job_path"] = job["job_path"]
        seen["result_path"] = job["result_path"]
        seen["log"] = job["log"]
        return 0

    service.set_runner(runner)
    answer = service.restart_run(run_id=_record(home, selections))
    deadline = __import__("time").monotonic() + 5
    while "config" not in seen and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.02)
    assert "config" in seen, answer
    assert answer["selections"]["analysts"] == ["market"]
    assert answer["selections"]["output_language"] == "English"
    assert answer["selections"]["max_tokens"] == 4096
    assert answer["selections"]["data_cache_dir"] == str(stored_cache)
    assert seen["selections"]["data_cache_dir"] == str(stored_cache)
    assert seen["config"]["data_cache_dir"] == str(stored_cache.resolve())
    assert seen["config"]["results_dir"] == str(stored_results.resolve())
    assert seen["config"]["memory_log_path"] == str(stored_memory.resolve())
    assert seen["config"]["max_tokens"] == 4096
    job = json.loads(Path(seen["job_path"]).read_text(encoding="utf-8"))
    assert job["result_path"] == seen["result_path"]
    assert job["log"] == seen["log"]
    assert str(home) in job["result_path"]
    assert "env-results" not in job["result_path"]
    assert "sk-" not in Path(seen["job_path"]).read_text(encoding="utf-8")


def test_old_redacted_notice_survives_a_conflicting_token_environment(home, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MAX_TOKENS", "128")
    monkeypatch.setitem(DEFAULT_CONFIG, "max_tokens", "128")
    run_id = _record(home, {**STORED, "max_tokens": "[redacted]"})
    answer = service.restart_run(run_id=run_id)
    assert answer["notices"] == ["原任务的 max_tokens 已被脱敏，已改用默认值。"]
    assert "已被脱敏" in answer["zh"]
    assert answer["selections"]["max_tokens"] is None
    text = json.dumps(answer, ensure_ascii=False)
    assert "sk-" not in text
    assert answer["selections"]["max_tokens"] is None


def test_omitted_cache_still_follows_the_environment(monkeypatch, tmp_path):
    env_cache = tmp_path / "env-cache"
    monkeypatch.setenv("TRADINGAGENTS_CACHE_DIR", str(env_cache))
    monkeypatch.setitem(DEFAULT_CONFIG, "data_cache_dir", str(env_cache))
    config = service.build_run_config({
        "ticker": "NVDA", "llm_provider": "grok", "research_depth": 1, "backend_url": None,
        "quick_think_llm": "grok-4.7", "deep_think_llm": "grok-4.7",
    })
    assert config["data_cache_dir"] == str(env_cache)


def test_reused_pid_with_a_result_finishes_and_is_not_signalled(home, monkeypatch):
    import os
    import subprocess
    import sys

    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        result = home / "done.json"
        result.write_text('{"decision":"HOLD"}', encoding="utf-8")
        record = {
            "id": "reuse-review",
            "status": "running",
            "pid": other.pid,
            "proc_start": "ps:not-this-process",
            "argv": [sys.executable, "-m", "webui.worker", "missing.job"],
            "result": str(result),
        }
        monkeypatch.setattr(service, "_worker_state", lambda _record: "foreign")
        refreshed = service.refresh_run_process(record)
        assert refreshed["status"] == "finished"
        assert refreshed.get("pid_reused") is not True
        assert other.poll() is None
        os.kill(other.pid, 0)
    finally:
        other.kill()
        other.wait()
