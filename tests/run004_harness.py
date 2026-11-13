"""RUN-004 fake-server concurrency harness.

Runs the real console (``webui.server.main`` via ``tests.run004_fake_console``)
as a separate process in a throw-away HOME, with ``tests.run004_fake_worker``
standing in for the analysis worker. No model, subscription or network call is
made: the worker only prints progress and writes a small LangGraph checkpoint.

    python -m tests.run004_harness                # every scenario, prints PASS/FAIL
    python -m tests.run004_harness queue_full     # one scenario
    python -m tests.run004_harness --keep /tmp/x  # keep the HOME for inspection

``tests/test_run004_concurrency.py`` runs the same scenarios under pytest.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASE = {"date": "2026-10-30", "llm_provider": "chatgpt", "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol", "analysts": ["market", "news"], "checkpoint": True}


class Console:
    """One console process. ``restart`` kills it and starts a new one on the same HOME."""

    def __init__(self, root: Path, max_running: int = 2, max_queued: int = 5):
        self.root = root
        self.home = root / "home"
        self.control = root / "control"
        self.home.mkdir(parents=True, exist_ok=True)
        self.control.mkdir(parents=True, exist_ok=True)
        self.limits = (max_running, max_queued)
        self.process = None
        self.port = None
        self.token = None
        self.password = None
        self.cookie = None

    # -- process ---------------------------------------------------------------
    def env(self) -> dict:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("TRADINGAGENTS_", "XDG_")) and "PROXY" not in key.upper()}
        env.update({
            "HOME": str(self.home),
            "RUN004_CONTROL": str(self.control),
            "TRADINGAGENTS_WEBUI_MAX_RUNS": str(self.limits[0]),
            "TRADINGAGENTS_WEBUI_MAX_QUEUE": str(self.limits[1]),
            # Nothing here should go online; an accidental HTTP(S) call fails fast.
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
            "PYTHONUNBUFFERED": "1",
        })
        return env

    def start(self) -> Console:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.run004_fake_console", "--port", "0"],
            cwd=REPO, env=self.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            match = re.search(r"http://127\.0\.0\.1:(\d+)", line or "")
            if match:
                self.port = int(match.group(1))
                break
            if not line and self.process.poll() is not None:
                raise RuntimeError("console did not start")
        self._sign_in()
        page = self._request("GET", "/", token=False)[1]
        self.token = re.search(r'name="tradingagents-token" content="([^"]+)"', page).group(1)
        return self

    def _sign_in(self) -> None:
        """V2-AUTH-LOGIN: a throwaway account (random password, made once per HOME) and a session."""
        from webui import console_auth

        store = self.home / ".tradingagents" / "webui" / console_auth.USER_STORE_NAME
        if self.password is None:
            self.password = secrets.token_urlsafe(18)
            console_auth.create_user("harness@example.invalid", self.password, must_change=False, path=store,
                                     force=True)
        self.cookie = None
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        connection.request("POST", "/api/account/login", headers={"Content-Type": "application/json"},
                           body=json.dumps({"email": "harness@example.invalid", "password": self.password}))
        response = connection.getresponse()
        response.read()
        connection.close()
        assert response.status == 200, response.status
        self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]

    def kill(self) -> None:
        """Stop the console the hard way (a crash or a closed terminal). Workers keep running."""
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGKILL)
            self.process.wait(10)

    def restart(self) -> Console:
        self.kill()
        return self.start()

    def close(self) -> None:
        (self.control / "release-all").touch()
        pids = []
        with contextlib.suppress(Exception):
            pids = [r.get("pid") for r in self.records()]
        self.kill()
        for pid in pids:
            if isinstance(pid, int):
                with contextlib.suppress(OSError):
                    os.killpg(pid, signal.SIGKILL)

    # -- http ------------------------------------------------------------------
    def _request(self, method: str, path: str, body=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if token:
            headers["X-TradingAgents-Token"] = self.token
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        data = response.read().decode("utf-8")
        connection.close()
        return response.status, data

    def get(self, path: str):
        status, data = self._request("GET", path)
        return status, json.loads(data)

    def post(self, path: str, body: dict):
        status, data = self._request("POST", path, body)
        return status, json.loads(data)

    # -- helpers ---------------------------------------------------------------
    def plan(self, ticker: str, outcome: str = "finish", done: int = 2) -> None:
        (self.control / f"{ticker}.json").write_text(json.dumps({"outcome": outcome, "done": done}))

    def launch(self, ticker: str, **extra):
        return self.post("/api/runs", {**BASE, "ticker": ticker, **extra})

    def release(self, run_id: str) -> None:
        (self.control / f"release-{run_id}").touch()

    def runs(self, run_id: str | None = None) -> dict:
        status, data = self.get("/api/runs" + (f"?id={run_id}" if run_id else ""))
        assert status == 200, data
        return data

    def records(self) -> list[dict]:
        folder = self.home / ".tradingagents" / "webui" / "runs"
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]

    def record(self, run_id: str) -> dict:
        return json.loads((self.home / ".tradingagents" / "webui" / "runs" / f"{run_id}.json").read_text())

    def log(self, run_id: str) -> str:
        path = self.home / ".tradingagents" / "webui" / "logs" / f"{run_id}.log"
        return path.read_text() if path.is_file() else ""

    def wait(self, what, timeout: float = 20.0, note: str = ""):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = what()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(f"timed out waiting: {note}")

    def status(self, run_id: str) -> str:
        return next(r["status"] for r in self.runs()["runs"] if r["id"] == run_id)

    def wait_status(self, run_id: str, status: str, timeout: float = 20.0) -> None:
        self.wait(lambda: self.status(run_id) == status, timeout, f"{run_id} -> {status}")

    def cache(self) -> Path:
        return self.home / ".tradingagents" / "cache"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------ scenarios
# Each takes a started Console and returns the lines it checked.

def two_running_then_queue_and_auto_start(c: Console) -> list[str]:
    for t in ("AAA", "BBB", "CCC", "DDD"):
        c.plan(t)
    ids = {}
    for t in ("AAA", "BBB"):
        status, rec = c.launch(t)
        assert status == 200 and rec["status"] == "running", rec
        ids[t] = rec["id"]
    status, rec = c.launch("CCC")
    assert status == 200 and rec["status"] == "queued" and rec["queue_ahead"] == 0, rec
    ids["CCC"] = rec["id"]
    status, rec = c.launch("DDD")
    assert status == 200 and rec["status"] == "queued" and rec["queue_ahead"] == 1, rec
    ids["DDD"] = rec["id"]
    data = c.runs()
    assert [r["ticker"] for r in data["runs"]] == ["AAA", "BBB", "CCC", "DDD"], data["runs"]
    assert data["counts"]["running"] == 2 and data["counts"]["queued"] == 2
    assert data["limits"] == {"max_running": 2, "max_queued": 5}
    queued = [r for r in data["runs"] if r["status"] == "queued"]
    assert [r["position"] for r in queued] == [1, 2]
    assert all(r["done"] == 0 and r["team_zh"] == "等待开始" for r in queued)
    assert not c.log(ids["CCC"]), "a queued run must not have started"
    c.release(ids["AAA"])
    c.wait_status(ids["AAA"], "finished")
    c.wait_status(ids["CCC"], "running")
    assert c.status(ids["DDD"]) == "queued"
    data = c.runs()
    assert [r["ticker"] for r in data["runs"][:3]] == ["BBB", "CCC", "DDD"]
    c.release(ids["BBB"])
    c.wait_status(ids["DDD"], "running")
    return ["2 running, 3rd/4th queued (ahead 0, 1)", "list: running first, queue in order",
            "AAA finished -> CCC auto-started", "BBB finished -> DDD auto-started"]


def cancel_a_queued_run(c: Console) -> list[str]:
    for t in ("AAA", "BBB", "CCC"):
        c.plan(t)
    a = c.launch("AAA")[1]["id"]
    c.launch("BBB")
    status, queued = c.launch("CCC")
    assert queued["status"] == "queued"
    status, body = c.post("/api/runs/cancel", {"id": a})
    assert status == 409 and "只有排队中的任务" in body["error"], body
    status, body = c.post("/api/runs/cancel", {"id": queued["id"]})
    assert status == 200 and body["status"] == "cancelled", body
    assert c.status(queued["id"]) == "cancelled"
    scope = c.cache() / "checkpoints" / "runs" / queued["id"]
    assert not scope.exists(), "a cancelled queued run must not have a checkpoint"
    assert not c.log(queued["id"]), "a cancelled queued run must not have started"
    c.release(a)
    c.wait_status(a, "finished")
    time.sleep(2.5)  # a dispatcher tick: the cancelled run must stay cancelled
    assert c.status(queued["id"]) == "cancelled"
    assert c.runs()["counts"]["queued"] == 0
    return ["cancel of a running run refused (409)", "queued run cancelled: no log, no checkpoint dir",
            "a freed slot does not start a cancelled run"]


def queue_full(c: Console) -> list[str]:
    started = []
    for i in range(7):
        t = f"Q{i}X"
        c.plan(t)
        status, rec = c.launch(t)
        assert status == 200, rec
        started.append(rec["status"])
    assert started == ["running"] * 2 + ["queued"] * 5, started
    status, body = c.launch("EIGHT")
    assert status == 409, body
    assert body["error"].startswith("队列已满"), body
    assert "最多同时运行 2 个，最多排队 5 个" in body["error"]
    assert len(c.records()) == 7, "a refused start leaves no run behind"
    return ["2 running + 5 queued", "8th submission -> 409 「队列已满…」", "no record for the refused start"]


def same_ticker_isolated(c: Console) -> list[str]:
    c.plan("SAME")
    a = c.launch("SAME")[1]
    b = c.launch("SAME")[1]
    assert a["status"] == b["status"] == "running"
    for run in (a, b):
        c.wait(lambda run=run: "CHECKPOINT\tsaved" in c.log(run["id"]), note="checkpoint saved")
    da = c.cache() / "checkpoints" / "runs" / a["id"] / "SAME.db"
    db = c.cache() / "checkpoints" / "runs" / b["id"] / "SAME.db"
    assert da.is_file() and db.is_file() and da != db
    assert not (c.cache() / "checkpoints" / "SAME.db").exists(), "no shared per-ticker database"
    sys.path.insert(0, str(REPO))
    from tradingagents.graph import checkpointer
    for run in (a, b):
        assert checkpointer.checkpoint_step(str(c.cache()), "SAME", "2026-10-30", "", run["id"]) is not None
    assert "CHECKPOINT\tfresh" in c.log(a["id"]) and "CHECKPOINT\tfresh" in c.log(b["id"])
    c.release(a["id"])
    c.wait_status(a["id"], "finished")
    assert db.is_file() and checkpointer.checkpoint_step(str(c.cache()), "SAME", "2026-10-30", "", b["id"]) is not None
    return ["two SAME/2026-10-30 runs at once", f"own DBs: runs/{a['id']}/SAME.db, runs/{b['id']}/SAME.db",
            "both start fresh; no checkpoints/SAME.db", "one finishing leaves the other's DB intact"]


def legacy_checkpoint_resumes(c: Console) -> list[str]:
    """Before RUN-004 a checkpoint lived at checkpoints/<TICKER>.db and current-run.json held one run."""
    sys.path.insert(0, str(REPO))
    from tests.run004_fake_worker import _write_checkpoint
    c.kill()
    _write_checkpoint(str(c.cache()), "OLD", "2026-10-30", None)  # the pre-RUN-004 location
    legacy = c.cache() / "checkpoints" / "OLD.db"
    assert legacy.is_file()
    before = _sha(legacy)
    webui = c.home / ".tradingagents" / "webui"
    (webui / "logs").mkdir(parents=True, exist_ok=True)
    old_record = {"id": "20261029_101010_0ld0ld00", "kind": "analysis", "status": "failed", "ticker": "OLD",
                  "date": "2026-10-30", "selections": {**BASE, "ticker": "OLD", "analysis_date": "2026-10-30",
                                                       "asset_type": "stock", "analysts": ["market", "news"]},
                  "log": str(webui / "logs" / "old.log"), "result": str(webui / "logs" / "old.json"),
                  "started_at": "2026-10-29T10:10:10", "pid": None,
                  "checkpoint": {"enabled": True, "saved": True}}
    (webui / "current-run.json").write_text(json.dumps(old_record, ensure_ascii=False))
    c.start()
    data = c.runs()
    assert [r["id"] for r in data["runs"]] == [old_record["id"]], data["runs"]
    assert not (webui / "current-run.json").exists() and len(list(webui.glob("current-run.json.migrated-*"))) == 1
    c.plan("OLD")
    status, resumed = c.post("/api/runs/restart", {"id": old_record["id"], "resume": True})
    assert status == 200 and resumed["status"] == "running", resumed
    c.wait(lambda: "CHECKPOINT\tresume" in c.log(resumed["id"]), note="resume from the legacy DB")
    assert "seeded=True" in c.log(resumed["id"])
    status, fresh = c.launch("OLD")
    c.wait(lambda: "CHECKPOINT\t" in c.log(fresh["id"]), note="fresh OLD run read the legacy DB")
    assert "CHECKPOINT\tresume" in c.log(fresh["id"]), "a new run still resumes an old per-ticker checkpoint"
    assert _sha(legacy) == before, "the legacy database is only read"
    return ["old current-run.json migrated to runs/<id>.json (.migrated-<timestamp> kept)",
            "从断点重新启动 of the old run seeds from checkpoints/OLD.db and resumes",
            "a new OLD run also resumes it, as before", "checkpoints/OLD.db unchanged (read-only copy)"]


def restart_keeps_running_and_queued(c: Console) -> list[str]:
    for t in ("RUNA", "RUNB", "QUEA", "QUEB"):
        c.plan(t, done=3)
    ra = c.launch("RUNA")[1]
    rb = c.launch("RUNB")[1]
    qa = c.launch("QUEA")[1]
    qb = c.launch("QUEB")[1]
    assert [ra["status"], rb["status"], qa["status"], qb["status"]] == ["running", "running", "queued", "queued"]
    c.wait(lambda: "CHECKPOINT\tsaved" in c.log(ra["id"]), note="RUNA saved a checkpoint")
    pid_a = c.record(ra["id"])["pid"]
    c.kill()                          # the console dies...
    os.killpg(pid_a, signal.SIGKILL)  # ...and RUNA's worker with it; RUNB's worker survives
    c.wait(lambda: not _pid_alive(pid_a), note="RUNA worker gone")
    assert c.record(qa["id"])["status"] == "queued"
    c.start()                         # the console comes back
    a = c.runs(ra["id"])["run"]
    assert a["status"] == "failed" and a.get("interrupted") is True, a
    assert "控制台重启时这个任务的进程已经不在" in a["detail"]
    assert a["checkpoint"]["saved"] is True, "RUNA saved steps: 从断点重新启动 is offered"
    assert c.status(rb["id"]) == "running", "a worker that outlived the console keeps running"
    c.wait_status(qa["id"], "running")  # RUNA's slot went to the first queued run
    data = c.runs()
    assert c.status(qb["id"]) == "queued"
    assert next(r for r in data["runs"] if r["id"] == qb["id"])["position"] == 1
    status, resumed = c.post("/api/runs/restart", {"id": ra["id"], "resume": True})
    assert status == 200 and resumed["status"] == "queued" and resumed["queue_ahead"] == 1, resumed
    c.release(rb["id"])
    c.wait_status(rb["id"], "finished")
    c.wait_status(qb["id"], "running")
    c.release(qa["id"])
    c.wait_status(resumed["id"], "running")
    c.wait(lambda: "CHECKPOINT\tresume" in c.log(resumed["id"]), note="resumed from RUNA's own checkpoint")
    return ["console killed; RUNA worker killed, RUNB worker alive",
            "after restart: RUNA 失败 (interrupted, reason, checkpoint saved -> resumable)",
            "RUNB still running; QUEA auto-started into the free slot; QUEB still queued (#1)",
            "从断点重新启动 RUNA queues behind QUEB and later resumes from its own checkpoint"]


def pid_reused_after_restart(c: Console) -> list[str]:
    """W39: after a restart the old pid belongs to an unrelated program; W36: cancel needs an id."""
    for t in ("PIDA", "PIDB", "PIDQ"):
        c.plan(t, done=3)
    ra, rb, rq = c.launch("PIDA")[1], c.launch("PIDB")[1], c.launch("PIDQ")[1]
    assert [ra["status"], rb["status"], rq["status"]] == ["running", "running", "queued"]
    for body in ({}, {"id": ""}, {"id": "  "}, {"id": 5}):
        status, answer = c.post("/api/runs/cancel", body)
        assert status == 400, (body, status, answer)
    assert c.status(rq["id"]) == "queued", "a cancel without an id cancelled nothing"
    before = len(c.records())
    for path in ("/api/runs/stop", "/api/runs/restart"):
        for body in ({}, {"resume": True}, {"id": ""}, {"id": "   "}, {"id": 5}):  # PM: missing id = empty
            status, answer = c.post(path, body)
            assert status == 400 and "没有对任何任务执行操作" in answer["error"], (path, body, status, answer)
    assert [c.status(ra["id"]), c.status(rb["id"]), c.status(rq["id"])] == ["running", "running", "queued"]
    assert len(c.records()) == before, "an empty id restarted nothing"
    pid_a = c.wait(lambda: c.record(ra["id"]).get("pid"), note="PIDA has a pid")
    assert c.record(ra["id"]).get("proc_start"), "the worker's start time is recorded at spawn"
    c.kill()
    os.killpg(pid_a, signal.SIGKILL)
    c.wait(lambda: not _pid_alive(pid_a), note="PIDA worker gone")
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True)
    try:
        path = c.home / ".tradingagents" / "webui" / "runs" / f"{ra['id']}.json"
        record = json.loads(path.read_text())
        record["pid"] = other.pid            # the pid now belongs to an unrelated live program
        path.write_text(json.dumps(record))
        c.start()
        a = c.runs(ra["id"])["run"]
        assert a["status"] == "failed" and a.get("pid_reused") is True and a.get("interrupted") is True, a
        assert "属于另一个程序" in a["detail"] and "没有向那个程序发送任何信号" in a["detail"], a["detail"]
        c.wait_status(rq["id"], "running")   # the slot was released to the queue
        status, _ = c.post("/api/runs/stop", {"id": ra["id"]})
        time.sleep(0.5)
        assert other.poll() is None, "the unrelated program was never signalled"
        assert c.status(rb["id"]) == "running"
    finally:
        other.kill()
        other.wait()
    for run in (rb, rq):
        c.release(run["id"])
        c.wait_status(run["id"], "finished", timeout=30)
    return ["cancel with {}, empty or non-string id -> 400, the queued run stays queued",
            "stop / restart with {}, {resume:true}, an empty or non-string id -> 400; nothing stopped, nothing started",
            "console killed + PIDA worker killed; PIDA's pid handed to an unrelated live program",
            "after restart: PIDA 失败 (pid reused, interrupted), slot released -> PIDQ starts",
            "停止任务 on PIDA never signals the unrelated program; PIDB keeps running"]


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a live process. A zombie counts as gone (Linux /proc and macOS ps)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # Linux: field 3 of /proc/<pid>/stat is the state. macOS has no /proc.
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return True
    stat = out.stdout.strip()
    if out.returncode != 0 or not stat:
        return False
    return not stat.startswith("Z")


def holdings_run_to_completion(c: Console) -> list[str]:
    """What step 1 sends for 已持仓 without a quantity, for 未持仓, and a legacy holdings JSON import
    whose held position has no average_price (W50: accepted as on b4617f1)."""
    cost_only = {"positions": [{"ticker": "HCOST", "average_price": 62000}]}   # 已持仓, 持有数量 left empty
    legacy_json = {"cash": 10000, "currency": "USD",                           # the old drawer JSON import
                   "positions": [{"ticker": "HJSON", "quantity": 0.5},                # no average_price (W50)
                                 {"ticker": "AAPL", "quantity": 10, "average_price": 150}]}
    for t in ("HCOST", "HNONE", "HJSON"):
        c.plan(t)
    before = len(c.records())
    status, answer = c.launch("HFORM", portfolio={"positions": [{"ticker": "HFORM", "quantity": 1}]}, portfolio_source="form")
    assert status == 400 and "average_price" in json.dumps(answer, ensure_ascii=False), (status, answer)
    assert len(c.records()) == before, "a step-1 holding without its cost starts nothing"
    ids = {}
    for t, extra in (("HCOST", {"portfolio": cost_only, "portfolio_source": "form"}), ("HNONE", {}),
                     ("HJSON", {"portfolio": legacy_json, "portfolio_source": "json"})):
        status, rec = c.launch(t, **extra)
        assert status == 200 and rec["status"] in ("running", "queued"), rec
        ids[t] = rec["id"]
    assert c.status(ids["HJSON"]) == "queued", "the third run waits for a slot"
    assert c.record(ids["HCOST"])["selections"]["portfolio"] == cost_only
    assert not c.record(ids["HNONE"])["selections"].get("portfolio"), "未持仓 sends no holding"
    assert c.record(ids["HJSON"])["selections"]["portfolio"] == legacy_json
    for t in ("HCOST", "HNONE", "HJSON"):
        c.wait(lambda t=t: "PORTFOLIO\t" in c.log(ids[t]), timeout=30, note=f"{t} started")
        c.release(ids[t])
        c.wait_status(ids[t], "finished", timeout=30)
    results = {t: json.loads(Path(c.record(ids[t])["result"]).read_text()) for t in ids}
    cost = results["HCOST"]["portfolio_context"]
    assert "Current position in HCOST: held (size not given), average price 62,000.00" in cost, cost
    assert results["HNONE"]["portfolio_context"] == "" and "PORTFOLIO\tnone" in c.log(ids["HNONE"])
    legacy = results["HJSON"]["portfolio_context"]
    assert "- Current position in HJSON: 0.5 units\n" in legacy, legacy     # no cost given, none invented
    assert "Cash available: 10,000.00 USD" in legacy and "Other positions: AAPL 10" in legacy, legacy
    assert all(c.status(ids[t]) == "finished" for t in ids)
    return ["已持仓 with only 持仓价格 -> runs to 已完成; agents see 'held (size not given), average price 62,000.00'",
            "未持仓 -> no portfolio in the run, runs to 已完成, no portfolio block",
            "step-1 holding without 持仓价格 (portfolio_source=form) -> 400, nothing started",
            "legacy JSON import (cash, currency, HJSON 0.5 with NO average_price, AAPL 10 @150) -> runs to 已完成, rendered as before (W50)",
            "third run queued and auto-started (limit 2)"]


SCENARIOS = {
    "two_running_then_queue_and_auto_start": two_running_then_queue_and_auto_start,
    "cancel_a_queued_run": cancel_a_queued_run,
    "queue_full": queue_full,
    "same_ticker_isolated": same_ticker_isolated,
    "legacy_checkpoint_resumes": legacy_checkpoint_resumes,
    "restart_keeps_running_and_queued": restart_keeps_running_and_queued,
    "holdings_run_to_completion": holdings_run_to_completion,
    "pid_reused_after_restart": pid_reused_after_restart,
}


def run_scenario(name: str, root: Path) -> list[str]:
    console = Console(root).start()
    try:
        return SCENARIOS[name](console)
    finally:
        console.close()


def main(argv: list[str]) -> int:
    keep = None
    if "--keep" in argv:
        keep = Path(argv[argv.index("--keep") + 1])
        argv = [a for a in argv if a not in ("--keep", str(keep))]
    names = argv or list(SCENARIOS)
    failed = 0
    for name in names:
        root = (keep / name) if keep else Path(tempfile.mkdtemp(prefix=f"run004-{name}-"))
        started = time.monotonic()
        try:
            lines = run_scenario(name, root)
        except Exception as exc:  # noqa: BLE001 - report and carry on
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            continue
        print(f"PASS {name} ({time.monotonic() - started:.1f}s)")
        for line in lines:
            print(f"     - {line}")
    print(f"{len(names) - failed}/{len(names)} scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
