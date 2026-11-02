"""RUN-004: several console runs at once, a queue, and checkpoints isolated per run.

Unit tests drive ``webui.service`` in-process; the ``harness`` tests run the real
console as a separate process with a fake worker (tests/run004_harness.py), so
the queue dispatcher and the console-restart recovery run exactly as shipped.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests import run004_harness as harness
from tradingagents.graph import checkpointer
from webui import service

pytestmark = pytest.mark.unit

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo
SEL = {"ticker": "NVDA", "analysis_date": "2026-10-30", "asset_type": "stock", "analysts": ["market"]}


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_MAX_RUNS", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_MAX_QUEUE", raising=False)
    monkeypatch.setattr(service, "remember_run", lambda selections: None)
    yield tmp_path / "webui"
    service.set_runner(None)
    service.set_spawn_argv(None)


@pytest.fixture
def sleepers(home):
    """Runs are real sleeping child processes, so they count as running until killed."""
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "import time; time.sleep(60)"])
    yield
    for record in service.JOBS.list_runs():
        if isinstance(record.get("pid"), int):
            with contextlib.suppress(OSError):
                os.killpg(record["pid"], 9)


def _start(ticker="NVDA", **extra):
    return service.start_validated_run({**SEL, "ticker": ticker, **extra})


def _kill(record):
    os.killpg(record["pid"], 9)
    deadline = time.monotonic() + 5
    while service._alive(record["pid"]) and time.monotonic() < deadline:
        time.sleep(0.02)


# ------------------------------------------------------------------ limits & order

def test_limits_default_to_two_running_and_five_queued(home, monkeypatch):
    assert service.run_limits() == {"max_running": 2, "max_queued": 5}
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_RUNS", "3")
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_QUEUE", "0")
    assert service.run_limits() == {"max_running": 3, "max_queued": 0}
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_RUNS", "0")
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_QUEUE", "lots")
    assert service.run_limits() == {"max_running": 1, "max_queued": 5}


def test_limits_can_come_from_the_console_config(home, monkeypatch):
    monkeypatch.setattr(service, "load_saved_config", lambda: {"max_concurrent_runs": 4, "max_queued_runs": 9})
    assert service.run_limits() == {"max_running": 4, "max_queued": 9}


def test_the_list_puts_running_first_then_the_queue_in_order_then_newest():
    records = [
        {"id": "done-old", "status": "finished", "started_at": "2026-10-31T08:00:00"},
        {"id": "q2", "status": "queued", "queue_seq": 20},
        {"id": "run-late", "status": "running", "started_at": "2026-10-31T10:00:00"},
        {"id": "failed-new", "status": "failed", "started_at": "2026-10-31T09:30:00"},
        {"id": "q1", "status": "queued", "queue_seq": 10},
        {"id": "run-early", "status": "running", "started_at": "2026-10-31T09:00:00"},
        {"id": "cancelled", "status": "cancelled", "queued_at": "2026-10-31T09:45:00"},
    ]
    assert [r["id"] for r in service.order_runs(records)] == [
        "run-early", "run-late", "q1", "q2", "cancelled", "failed-new", "done-old"]


# ------------------------------------------------------------------ admission

def test_two_run_and_the_third_is_queued_with_the_real_queue_position(sleepers):
    a, b = _start("AAA"), _start("BBB")
    assert a["status"] == b["status"] == "running"
    c = _start("CCC")
    d = _start("DDD")
    assert c["status"] == d["status"] == "queued"
    assert (c["queue_ahead"], d["queue_ahead"]) == (0, 1)
    assert c["limits"] == {"max_running": 2, "max_queued": 5}
    assert "前面还有 1 个" in d["detail"]
    assert c.get("pid") is None and not Path(c["log"]).exists()
    data = service.api_runs()
    assert [r["ticker"] for r in data["runs"]] == ["AAA", "BBB", "CCC", "DDD"]
    assert data["counts"] == {"all": 4, "running": 2, "queued": 2, "failed": 0, "finished": 0}
    assert [r["position"] for r in data["runs"]] == [None, None, 1, 2]
    assert data["runs"][2]["team_zh"] == "等待开始" and data["runs"][2]["done"] == 0


def test_a_queued_run_starts_by_itself_when_a_running_one_ends(sleepers):
    a, _b = _start("AAA"), _start("BBB")
    c, d = _start("CCC"), _start("DDD")
    _kill(a)
    started = service.dispatch_queue()
    assert [r["id"] for r in started] == [c["id"]]
    assert service.JOBS.read_run(c["id"])["status"] == "running"
    assert service.JOBS.read_run(d["id"])["status"] == "queued"
    assert service.JOBS.read_run(a["id"])["status"] == "failed"
    assert service.dispatch_queue() == []


def test_an_in_process_run_that_ends_starts_the_next_queued_run(home, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_RUNS", "1")
    ran = []
    service.set_runner(lambda job: ran.append(job["selections"]["ticker"]) or 0)
    # occupy the one slot with a record whose process is alive
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        service.JOBS.write_run({"id": "busy", "status": "running", "pid": child.pid, "started_at": "2026-10-31T09:00:00",
                                "proc_start": service._process_start(child.pid)})
        queued = service.start_validated_run({**SEL, "ticker": "QQQ"}, background=False)
        assert queued["status"] == "queued" and ran == []
    finally:
        child.kill()
        child.wait()
    service.dispatch_queue()
    deadline = time.monotonic() + 5
    while service.JOBS.read_run(queued["id"])["status"] != "finished" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ran == ["QQQ"] and service.JOBS.read_run(queued["id"])["status"] == "finished"


def test_the_eighth_start_with_two_running_and_five_queued_is_refused(sleepers):
    statuses = [_start(f"T{i}")["status"] for i in range(7)]
    assert statuses == ["running", "running", "queued", "queued", "queued", "queued", "queued"]
    with pytest.raises(service.ConsoleError) as refused:
        _start("EIGHT")
    assert refused.value.status == 409
    assert str(refused.value).startswith("队列已满")
    assert "最多同时运行 2 个，最多排队 5 个" in str(refused.value)
    assert str(refused.value).startswith("队列已满：2 个任务运行中，5 个在排队"), "the sentence counts the real runs"
    assert len(service.JOBS.list_runs()) == 7


def test_the_queue_full_sentence_uses_the_real_counts():
    sentence = service.queue_full_sentence({"max_running": 3, "max_queued": 4}, 1, 4)
    assert sentence.startswith("队列已满：1 个任务运行中，4 个在排队（最多同时运行 3 个，最多排队 4 个）。")
    assert "等排队的任务开始后再启动" in sentence


def test_a_failed_runs_list_row_does_not_count_the_failed_role_as_done(home, tmp_path):
    log = tmp_path / "f.log"
    log.write_text("STATUS\tMarket Analyst\tcompleted\t21:14:01\nSTATUS\tBull Researcher\tcompleted\t21:14:02\n", encoding="utf-8")
    record = {"id": "f1", "status": "failed", "log": str(log), "selections": {"analysts": ["market"]},
              "failure": {"role": "Bull Researcher"}}
    counts = service._progress_counts(record)
    assert counts["done"] == 1
    assert counts["team_zh"] == "研究团队"


def test_a_new_start_waits_behind_the_queue_even_if_a_slot_just_freed(sleepers):
    a, _b = _start("AAA"), _start("BBB")
    c = _start("CCC")
    _kill(a)  # not dispatched yet: the next start must not jump the queue
    d = _start("DDD")
    assert service.JOBS.read_run(c["id"])["status"] == "running"
    assert d["status"] in ("queued", "running")
    order = [r["ticker"] for r in service.JOBS.list_runs() if r["status"] in ("running", "queued")]
    assert order.index("CCC") < order.index("DDD")


# ------------------------------------------------------------------ cancel

def test_a_queued_run_can_be_cancelled_without_side_effects(sleepers, tmp_path):
    a, _b = _start("AAA"), _start("BBB")
    c = _start("CCC")
    with pytest.raises(service.ConsoleError) as refused:
        service.cancel_run(a["id"])
    assert refused.value.status == 409 and "只有排队中的任务" in str(refused.value)
    cancelled = service.cancel_run(c["id"])
    assert cancelled["status"] == "cancelled" and "没有启动" in cancelled["detail"]
    assert not Path(c["log"]).exists()
    _kill(a)
    assert service.dispatch_queue() == []
    assert service.JOBS.read_run(c["id"])["status"] == "cancelled"


def test_stopping_a_queued_run_cancels_it(sleepers):
    _start("AAA")
    _start("BBB")
    c = _start("CCC")
    assert service.stop_run(c["id"])["status"] == "cancelled"


def test_an_unknown_run_id_is_a_404(home):
    for bad in ("nope", "../x", "a/b"):
        with pytest.raises(service.ConsoleError) as missing:
            service.cancel_run(bad)
        assert missing.value.status == 404


# ------------------------------------------------------------------ backtest

def test_a_running_backtest_takes_one_slot_not_all(sleepers):
    """W44: a live backtest counts as one run; an analysis still starts beside it."""
    records = [{"id": "bt", "kind": "backtest", "status": "running"}]
    assert service._slots_used(records, service.run_limits()) == 1
    assert _start("AAA")["status"] == "running"


# ------------------------------------------------------------------ migration

def test_an_old_current_run_json_is_moved_into_the_run_list(home):
    home.mkdir(parents=True, exist_ok=True)
    old = {"id": "20261029_101010_abcdef01", "status": "failed", "ticker": "OLD", "started_at": "2026-10-29T10:10:10",
           "checkpoint": {"enabled": True, "saved": True}}
    (home / "current-run.json").write_text(json.dumps(old), encoding="utf-8")
    assert service.JOBS.current_run()["id"] == old["id"]
    assert [r["id"] for r in service.JOBS.list_runs()] == [old["id"]]
    assert (home / "runs" / f"{old['id']}.json").is_file()
    assert not (home / "current-run.json").exists() and len(list(home.glob("current-run.json.migrated-*"))) == 1


def test_an_old_record_without_an_id_gets_one(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "current-run.json").write_text(json.dumps({"status": "finished", "ticker": "OLD"}), encoding="utf-8")
    [record] = service.JOBS.list_runs()
    assert record["id"].startswith("legacy-") and record["legacy"] is True


def test_a_damaged_old_current_run_json_is_left_alone(home):
    home.mkdir(parents=True, exist_ok=True)
    (home / "current-run.json").write_text("{not json", encoding="utf-8")
    assert service.JOBS.list_runs() == []
    assert (home / "current-run.json").read_text(encoding="utf-8") == "{not json"


# ------------------------------------------------------------------ restart recovery

def test_restart_marks_a_dead_running_run_failed_and_keeps_the_queue(sleepers, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_selections_cache_dir", lambda selections: str(tmp_path / "cache"))
    a, b = _start("AAA"), _start("BBB")
    c, d = _start("CCC"), _start("DDD")
    # AAA saved a step in its own checkpoint before its process went away
    checkpointer._db_path(tmp_path / "cache", "AAA", a["id"]).write_bytes(b"x" * 10)
    _kill(a)
    starting = service.JOBS.read_run(b["id"])
    service.JOBS.write_run({**starting, "id": "20261031_000000_deadbeef", "status": "starting", "pid": None})
    result = service.recover_runs_on_start()
    assert set(result["interrupted"]) == {a["id"], "20261031_000000_deadbeef"}
    dead = service.JOBS.read_run(a["id"])
    assert dead["status"] == "failed" and dead["interrupted"] is True
    assert "控制台重启时这个任务的进程已经不在" in dead["detail"]
    assert dead["checkpoint"]["saved"] is True
    never = service.JOBS.read_run("20261031_000000_deadbeef")
    assert never["status"] == "failed" and "还没启动完成" in never["detail"]
    assert service.JOBS.read_run(b["id"])["status"] == "running"
    assert result["started"] == [c["id"]]
    assert service.JOBS.read_run(d["id"])["status"] == "queued"


def test_restart_without_a_saved_checkpoint_offers_only_a_fresh_start(sleepers, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_selections_cache_dir", lambda selections: str(tmp_path / "cache"))
    a = _start("AAA")
    _kill(a)
    service.recover_runs_on_start()
    assert service.JOBS.read_run(a["id"])["checkpoint"]["saved"] is False
    with pytest.raises(service.ConsoleError, match="本次未保存断点"):
        service.restart_run(resume=True, run_id=a["id"])


# ------------------------------------------------------------------ checkpoints

def _saved_db(data_dir, ticker, scope=None):
    from tests.run004_fake_worker import _write_checkpoint
    _write_checkpoint(str(data_dir), ticker, "2026-10-30", scope)
    return checkpointer.db_file(data_dir, ticker, scope)


def test_two_runs_of_one_ticker_get_their_own_databases(tmp_path):
    one = _saved_db(tmp_path, "SAME", "run_one")
    two = checkpointer.db_file(tmp_path, "SAME", "run_two")
    assert one == tmp_path / "checkpoints" / "runs" / "run_one" / "SAME.db"
    assert not two.exists() and not (tmp_path / "checkpoints" / "SAME.db").exists()
    assert checkpointer.checkpoint_step(tmp_path, "SAME", "2026-10-30", "", "run_one") is not None
    assert checkpointer.checkpoint_step(tmp_path, "SAME", "2026-10-30", "", "run_two") is None
    assert checkpointer.checkpoint_step(tmp_path, "SAME", "2026-10-30") is None


def test_a_scope_must_be_one_plain_path_component(tmp_path):
    for bad in ("../x", "a/b", "x" * 65, "a b", ".."):
        with pytest.raises(ValueError):
            checkpointer.db_file(tmp_path, "AAA", bad)


def test_an_old_per_ticker_checkpoint_seeds_a_new_run_and_is_only_read(tmp_path):
    legacy = _saved_db(tmp_path, "OLD")
    before = legacy.read_bytes()
    job = {"selections": {"ticker": "OLD"}, "checkpoint_scope": "new_run"}
    assert service.seed_run_checkpoint(str(tmp_path), job) is True
    assert checkpointer.checkpoint_step(tmp_path, "OLD", "2026-10-30", "", "new_run") is not None
    assert legacy.read_bytes() == before
    assert checkpointer.copy_checkpoint_db(legacy, tmp_path, "OLD", "new_run") is False


def test_resume_copies_the_failed_runs_own_database(tmp_path):
    _saved_db(tmp_path, "AAA", "failed_run")
    _saved_db(tmp_path, "AAA")  # a per-ticker one exists too; the run's own one wins
    job = {"selections": {"ticker": "AAA"}, "checkpoint_scope": "resumed",
           "resume_from": {"run": "failed_run", "scope": "failed_run"}}
    assert service.seed_run_checkpoint(str(tmp_path), job) is False
    def rows(scope):
        import sqlite3
        with sqlite3.connect(checkpointer.db_file(tmp_path, "AAA", scope)) as db:
            return db.execute("select thread_id, checkpoint_id from checkpoints order by checkpoint_id").fetchall()

    assert rows("resumed") == rows("failed_run") != rows(None)


def test_resume_of_an_old_run_without_a_scope_reads_the_per_ticker_database(tmp_path):
    _saved_db(tmp_path, "OLD")
    job = {"selections": {"ticker": "OLD"}, "checkpoint_scope": "resumed", "resume_from": {"run": "old", "scope": None}}
    assert service.seed_run_checkpoint(str(tmp_path), job) is True
    assert checkpointer.checkpoint_step(tmp_path, "OLD", "2026-10-30", "", "resumed") is not None


def test_restart_resume_passes_the_failed_runs_scope(home, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "start_validated_run",
                        lambda selections, background=True, resume_from=None, framework="tradingagents": seen.append(resume_from) or {})
    service.JOBS.write_run({"id": "r1", "status": "failed", "selections": dict(SEL),
                            "checkpoint": {"enabled": True, "saved": True, "scope": "r1"}})
    service.restart_run(resume=True, run_id="r1")
    service.restart_run(resume=False, run_id="r1")
    assert seen == [{"run": "r1", "scope": "r1"}, None]


def test_clear_keeps_running_runs_databases(home, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "effective_cache_dir", lambda: str(tmp_path / "cache"))
    _saved_db(tmp_path / "cache", "OLD")
    _saved_db(tmp_path / "cache", "AAA", "live_run")
    _saved_db(tmp_path / "cache", "BBB", "old_run")
    service.JOBS.write_run({"id": "live_run", "status": "running", "pid": None})
    listed = {(f["ticker"], f.get("run_id")) for f in service.list_checkpoints()["files"]}
    assert listed == {("OLD", None), ("AAA", "live_run"), ("BBB", "old_run")}
    service.clear_checkpoints()
    assert checkpointer.db_file(tmp_path / "cache", "AAA", "live_run").is_file()
    assert not checkpointer.db_file(tmp_path / "cache", "BBB", "old_run").exists()
    assert not checkpointer.db_file(tmp_path / "cache", "OLD").exists()


def test_the_runner_gives_each_run_its_own_checkpoint_scope():
    script = Path("webui/service.py").read_text(encoding="utf-8")
    assert 'config["checkpoint_run_scope"] = job["checkpoint_scope"]' in script
    graph = Path("tradingagents/graph/trading_graph.py").read_text(encoding="utf-8")
    assert graph.count('self.config.get("checkpoint_run_scope") or None') == 2


# ------------------------------------------------------------------ routes

def test_the_routes_take_a_run_id(home, monkeypatch):
    from webui import server
    seen = []
    monkeypatch.setattr(service, "cancel_run", lambda run_id: seen.append(("cancel", run_id)) or {})
    monkeypatch.setattr(service, "stop_run", lambda run_id=None: seen.append(("stop", run_id)) or {})
    monkeypatch.setattr(service, "restart_run", lambda resume=False, run_id=None: seen.append(("restart", run_id, resume)) or {})
    assert server._run_id({"id": "abc"}) == "abc"
    for bad in ({}, {"resume": True}, {"id": 5}, {"id": ""}, {"id": "  "}, {"id": None}):
        with pytest.raises(service.ConsoleError) as refused:  # W36 (PM): missing counts as empty -> 400, nothing done
            server._run_id(bad)
        assert refused.value.status == 400
    script = Path("webui/server.py").read_text(encoding="utf-8")
    assert '"/api/runs/cancel": lambda: service.cancel_run(_run_id(body))' in script
    assert '"/api/runs/stop": lambda: service.stop_run(_run_id(body))' in script
    assert 'run_id=_run_id(body))' in script
    assert seen == []
    assert "service.api_runs(run_id)" in script


# ------------------------------------------------------------------ fake-server harness

@pytest.fixture
def loopback(monkeypatch):
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))


@pytest.mark.parametrize("name", list(harness.SCENARIOS))
def test_fake_server_harness(name, loopback, tmp_path):
    lines = harness.run_scenario(name, tmp_path)
    assert lines


# ------------------------------------------------------------------ WEBUI-006 item 4: 持仓价格 without 持有数量

def test_a_holding_may_give_only_its_cost():
    from tradingagents.portfolio import PortfolioContext
    book = PortfolioContext.model_validate({"positions": [{"ticker": "BTC-USD", "average_price": 62000}]})
    text = book.render("BTC-USD")
    assert "held (size not given), average price 62,000.00" in text
    both = PortfolioContext.model_validate({"positions": [{"ticker": "BTC-USD", "average_price": 62000, "quantity": 0.5}]})
    assert "0.5 units, average price 62,000.00" in both.render("BTC-USD")
    others = PortfolioContext.model_validate({"positions": [{"ticker": "ETH-USD", "average_price": 3000}]})
    assert "ETH-USD (size not given)" in others.render("BTC-USD")


# ------------------------------------------------------------------ QA r1: W36-W39, W41

def _unrelated_sleeper():
    """A live process that is not a worker, leading its own group (so a stray killpg would reach it)."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)


def test_a_reused_pid_after_restart_fails_the_run_frees_the_slot_and_is_never_signalled(sleepers):
    a, b, c = _start("AAA"), _start("BBB"), _start("CCC")
    assert service.JOBS.read_run(c["id"])["status"] == "queued"
    _kill(a)
    other = _unrelated_sleeper()
    try:
        record = service.JOBS.read_run(a["id"])
        assert record["proc_start"]  # recorded at spawn
        service.JOBS.write_run({**record, "pid": other.pid})  # the old pid now belongs to `other`
        assert service._worker_state(service.JOBS.read_run(a["id"])) == "foreign"
        result = service.recover_runs_on_start()
        dead = service.JOBS.read_run(a["id"])
        assert a["id"] in result["interrupted"]
        assert dead["status"] == "failed" and dead["interrupted"] is True and dead["pid_reused"] is True
        assert dead["detail"] == service.PID_REUSED_SENTENCE
        assert result["started"] == [c["id"]]            # the slot went to the queue
        service.stop_run(a["id"])
        assert other.poll() is None                        # never signalled
        assert service.JOBS.read_run(b["id"])["status"] == "running"
    finally:
        other.kill()
        other.wait()


def test_stop_never_signals_a_process_that_only_shares_the_pid(sleepers):
    a = _start("AAA")
    _kill(a)
    other = _unrelated_sleeper()
    try:
        service.JOBS.write_run({**service.JOBS.read_run(a["id"]), "pid": other.pid})
        answer = service.stop_run(a["id"])
        assert answer["status"] == "failed" and answer["pid_reused"] is True
        time.sleep(0.2)
        assert other.poll() is None
        assert service._slots_used(service._refresh_all(), service.run_limits()) == 0   # it holds no slot
    finally:
        other.kill()
        other.wait()


def test_a_running_run_whose_pid_was_reused_is_failed_by_the_status_read(sleepers):
    a = _start("AAA")
    _kill(a)
    other = _unrelated_sleeper()
    try:
        service.JOBS.write_run({**service.JOBS.read_run(a["id"]), "pid": other.pid})
        row = next(r for r in service.api_runs()["runs"] if r["id"] == a["id"])
        assert row["status"] == "failed"
        assert service.api_runs()["counts"]["running"] == 0
        record = service.JOBS.read_run(a["id"])
        assert record.get("pid_reused") is True and record.get("interrupted") is True, record
        assert record["detail"] == service.PID_REUSED_SENTENCE, "the reason names the reused pid, not a plain exit"
        assert other.poll() is None
    finally:
        other.kill()
        other.wait()


def test_a_record_without_a_start_time_is_ours_only_when_its_command_line_names_the_run(home):
    run_id = "20261031_120000_0123abcd"
    mine = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"/x/{run_id}.job.json"], start_new_session=True)
    other = _unrelated_sleeper()
    try:
        argv = [sys.executable, "-c", "import time; time.sleep(30)", f"/x/{run_id}.job.json"]
        # W48/W57 (RUN-003a): an id substring alone no longer proves it; the recorded argv must match exactly.
        assert service._worker_state({"id": run_id, "pid": mine.pid}) == "foreign"
        assert service._worker_state({"id": run_id, "pid": mine.pid, "argv": argv}) == "ours"
        assert service._worker_state({"id": run_id, "pid": other.pid, "argv": argv}) == "foreign"
        assert service._worker_state({"id": run_id, "pid": mine.pid, "proc_start": "proc:1"}) == "foreign"
    finally:
        for proc in (mine, other):
            proc.kill()
            proc.wait()
    assert service._worker_state({"id": run_id, "pid": mine.pid}) == "gone"


@pytest.mark.parametrize("run_id", ["", None, "   "])
def test_cancel_without_an_id_is_refused_and_cancels_nothing(sleepers, run_id):
    _start("AAA"), _start("BBB")
    queued = _start("CCC")
    with pytest.raises(service.ConsoleError) as refused:
        service.cancel_run(run_id)
    assert refused.value.status == 400 and "没有取消任何任务" in str(refused.value)
    assert service.JOBS.read_run(queued["id"])["status"] == "queued"


def test_a_queued_resume_whose_checkpoint_was_cleared_fails_instead_of_starting_fresh(sleepers, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_selections_cache_dir", lambda selections: str(tmp_path / "cache"))
    monkeypatch.setattr(service, "effective_cache_dir", lambda: str(tmp_path / "cache"))
    failed_id = "20261031_080000_feedf00d"
    _saved_db(tmp_path / "cache", "RES", failed_id)
    a, _b = _start("AAA"), _start("BBB")
    queued = service.start_validated_run({**SEL, "ticker": "RES", "checkpoint": True},
                                         resume_from={"run": failed_id, "scope": failed_id})
    assert queued["status"] == "queued"
    service.clear_checkpoints()                      # 立即清除断点 while it waits
    assert not checkpointer.db_file(tmp_path / "cache", "RES", failed_id).exists()
    _kill(a)
    service.dispatch_queue()
    record = service.JOBS.read_run(queued["id"])
    assert record["status"] == "failed" and record["resume_source_gone"] is True
    assert record["detail"] == service.RESUME_SOURCE_GONE and record.get("pid") is None


def test_a_resume_whose_checkpoint_is_gone_is_refused_at_start(home, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_selections_cache_dir", lambda selections: str(tmp_path / "cache"))
    with pytest.raises(service.ConsoleError, match="要续跑的断点已经被清除"):
        service.start_validated_run({**SEL, "ticker": "RES"}, resume_from={"run": "x", "scope": "20261031_080000_feedf00d"})
    assert service.JOBS.list_runs() == []


def test_a_second_migration_keeps_the_first_backup(home):
    home.mkdir(parents=True, exist_ok=True)
    first = {"id": "20261029_101010_abcdef01", "status": "finished", "ticker": "ONE"}
    second = {"id": "20261030_101010_abcdef02", "status": "finished", "ticker": "TWO"}
    (home / "current-run.json").write_text(json.dumps(first), encoding="utf-8")
    service.JOBS.list_runs()
    (home / "current-run.json").write_text(json.dumps(second), encoding="utf-8")
    service.JOBS.list_runs()
    backups = sorted(home.glob("current-run.json.migrated-*"))
    assert len(backups) == 2
    assert {json.loads(p.read_text(encoding="utf-8"))["ticker"] for p in backups} == {"ONE", "TWO"}
    assert {r["id"] for r in service.JOBS.list_runs()} == {first["id"], second["id"]}


@pytest.mark.parametrize("position", [
    {"ticker": "NVDA", "quantity": 0, "average_price": 120},
    {"ticker": "NVDA", "quantity": 5, "average_price": 0},
    {"ticker": "NVDA", "quantity": 5, "average_price": -1},
    {"ticker": "NVDA", "quantity": 5},
    {"ticker": "NVDA", "quantity": True, "average_price": 120},
    {"ticker": "NVDA", "quantity": "5", "average_price": 120},
])
def test_the_console_refuses_holdings_the_form_refuses(monkeypatch, position):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="持仓内容不可用："):
        service.validate_analysis({"ticker": "NVDA", "date": "2026-10-29", "llm_provider": "chatgpt",
                                   "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol",
                                   "portfolio": {"positions": [position]}, "portfolio_source": "form"})


_ANALYSIS = {"ticker": "NVDA", "date": "2026-10-29", "llm_provider": "chatgpt",
             "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol"}


@pytest.mark.parametrize("source", [{"portfolio_source": "json"}, {}])   # the WebUI import, and an old client that does not say
def test_an_imported_json_position_without_a_cost_is_accepted_as_on_b4617f1(monkeypatch, source):
    """W50: only the step-1 form (and the CLI) need the cost; an imported JSON is read as before."""
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    book = {"cash": 10000, "currency": "USD", "positions": [{"ticker": "NVDA", "quantity": 5}, {"ticker": "AAPL", "quantity": -2}]}
    selections = service.validate_analysis({**_ANALYSIS, "portfolio": book, **source})
    assert selections["selections"]["portfolio"] == book
    assert "portfolio_source" not in selections["selections"], "the marker is not stored with the run"


@pytest.mark.parametrize("position", [
    {"ticker": "NVDA"},                                   # neither size nor cost (refused on b4617f1 too)
    {"ticker": "NVDA", "quantity": 0},
    {"ticker": "NVDA", "quantity": 5, "average_price": 0},
    {"ticker": "NVDA", "quantity": "5"},
])
def test_an_imported_json_still_refuses_a_broken_position(monkeypatch, position):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="持仓内容不可用："):
        service.validate_analysis({**_ANALYSIS, "portfolio": {"positions": [position]}, "portfolio_source": "json"})


def test_the_console_accepts_a_cost_with_or_without_a_size(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    for position in ({"ticker": "NVDA", "average_price": 120}, {"ticker": "NVDA", "quantity": -2, "average_price": 120}):
        selections = service.validate_analysis({"ticker": "NVDA", "date": "2026-10-29", "llm_provider": "chatgpt",
                                                "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol",
                                                "portfolio": {"positions": [position]}})
        assert selections["selections"]["portfolio"] == {"positions": [position]}


def test_an_unreadable_start_time_keeps_the_run_and_its_slot_and_never_signals(sleepers, monkeypatch):
    """W47: when neither /proc nor ps can give the start time, a live worker is not
    declared dead (no 失败, slot kept, queue does not overrun) and is never signalled."""
    a, b, c = _start("AAA"), _start("BBB"), _start("CCC")
    assert service.JOBS.read_run(c["id"])["status"] == "queued"
    pid = service.JOBS.read_run(a["id"])["pid"]
    readable = service._process_start
    monkeypatch.setattr(service, "_process_start", lambda p: None if p == pid else readable(p))   # only a's is unreadable
    record = service.JOBS.read_run(a["id"])
    assert service._worker_state(record) == "unknown"
    result = service.recover_runs_on_start()                  # console restart
    assert a["id"] not in result["interrupted"] and result["started"] == []
    assert service.JOBS.read_run(a["id"])["status"] == "running"
    data = service.api_runs()                                 # status read
    assert next(r for r in data["runs"] if r["id"] == a["id"])["status"] == "running"
    assert data["counts"]["running"] == 2 and service.dispatch_queue() == []
    assert service.JOBS.read_run(c["id"])["status"] == "queued", "the slot is still held"
    before = len(service.JOBS.list_runs())
    for act in (lambda: service.stop_run(a["id"]), lambda: service.restart_run(run_id=a["id"])):
        with pytest.raises(service.ConsoleError) as refused:
            act()
        assert refused.value.status == 409 and str(refused.value) == service.PROCESS_UNVERIFIED_SENTENCE
    time.sleep(0.2)
    assert service._alive(pid), "never signalled"
    assert service.JOBS.read_run(a["id"])["status"] == "running" and len(service.JOBS.list_runs()) == before
    service.cancel_run(c["id"])
    service.stop_run(b["id"])                                 # b is verifiably ours: stopped as before
    assert service.JOBS.read_run(b["id"])["status"] != "running"
    held = service._live_runs(service._refresh_all())         # a alone still holds its slot
    assert [r["id"] for r in held] == [a["id"]]
    monkeypatch.setattr(service, "_process_start", readable)
    assert service._worker_state(service.JOBS.read_run(a["id"])) == "ours"   # readable again: W39 as before
