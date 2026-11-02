"""WEBUI-006: what the 启动分析 result dialog reads from POST /api/runs.

The success dialog shows the run id, ticker and date from the response, so the
route must return the real record it just wrote. A refusal must come back as a
non-2xx answer with a readable ``error`` sentence and must not leave a run behind,
so the failure dialog can keep the form and say why.
"""

from __future__ import annotations

import json

import pytest

from tests.test_webui_security import console  # noqa: F401
from webui import service

pytestmark = pytest.mark.unit

REAL_START = service.start_validated_run
SELECTIONS = {"ticker": "BTC-USD", "analysis_date": "2026-10-30", "asset_type": "crypto", "analysts": ["market"]}


@pytest.fixture
def real_start(console, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "start_validated_run", REAL_START)
    monkeypatch.setattr(service, "validate_analysis", lambda body: {"selections": dict(SELECTIONS), "ignored": []})
    ran = []
    service.set_runner(lambda job: ran.append(job["id"]) or 0)
    yield console, ran
    service.set_runner(None)


def test_a_started_run_answers_with_its_own_run_id_ticker_and_date(real_start):
    app, ran = real_start
    status, body, _ = app["send"]("POST", "/api/runs", {"ticker": "btc-usd", "background": False})
    assert status == 200
    record = json.loads(body)
    stored = service.JOBS.current_run()
    assert record["id"] and record["id"] == stored["id"] == ran[0]
    assert record["ticker"] == "BTC-USD"
    assert record["date"] == "2026-10-30"
    assert record["started_at"]
    assert record["ignored"] == []


def test_two_launches_answer_with_different_run_ids(real_start):
    app, _ran = real_start
    ids = []
    for _ in range(2):
        status, body, _ = app["send"]("POST", "/api/runs", {"background": False})
        assert status == 200
        ids.append(json.loads(body)["id"])
    assert ids[0] != ids[1]


def test_a_refused_launch_is_a_readable_error_and_leaves_no_run(console, monkeypatch):  # noqa: F811
    sentence = "尚未登录 Grok 订阅。请先在账户页登录。"

    def refuse(body):
        raise service.ConsoleError(sentence)

    monkeypatch.setattr(service, "validate_analysis", refuse)
    monkeypatch.setattr(service, "start_validated_run", REAL_START)
    status, body, _ = console["send"]("POST", "/api/runs", {"ticker": "BTC-USD"})
    assert status == 400
    assert json.loads(body) == {"error": sentence}
    assert service.JOBS.current_run() is None


def test_a_launch_with_a_full_queue_is_refused_with_the_queue_full_sentence(real_start, monkeypatch):
    # RUN-004: a launch while runs are busy queues; only a full queue refuses it.
    import subprocess
    import sys

    app, ran = real_start
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_RUNS", "1")
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_QUEUE", "0")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        # Recorded the way _spawn records it: pid plus start time (W39).
        service.JOBS.write_run({"id": "busy", "status": "running", "pid": child.pid,
                                "proc_start": service._process_start(child.pid), "started_at": "2026-10-31T09:00:00"})
        status, body, _ = app["send"]("POST", "/api/runs", {"background": False})
    finally:
        child.kill()
        child.wait()
    assert status == 409
    error = json.loads(body)["error"]
    assert error.startswith("队列已满") and "最多同时运行 1 个，最多排队 0 个" in error
    assert ran == []
    assert [r["id"] for r in service.JOBS.list_runs()] == ["busy"]
