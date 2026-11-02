"""RUN-003a (batch B1): W66, W40, W48/W57, W53/W61, W65, W62, W31.

W66     the worker starts the framework its job file names (registry runner);
        unknown/ill-typed ids refuse with a Chinese error, nothing runs; old jobs = TradingAgents
W40     one console per TRADINGAGENTS_WEBUI_HOME (flock); a second one exits 1 before recovery
W48/W57 without a process start time, a worker is ours only on an exact argv match
W53/W61 the listen backlog holds a burst of 64 connections without a reset
W65     /api/runs?id=<unknown> is a not-found, never the newest run
W62     selections is the params layer only at record/job top level, runs[i] and run
W31     a legal supplementary-plane char in failure.json is kept and breaks nothing
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import textwrap
import threading

import pytest

from tests import test_run002_r2_console as r2
from webui import frameworks, run_failure, server, service, worker

pytestmark = pytest.mark.unit

console = r2.console
http_console = r2.http_console
failed_run = r2.failed_run


# ------------------------------------------------------------------ W66

CALLS: list[tuple[str, dict]] = []


def demo_runner(job):
    CALLS.append(("demo", job))
    return 7


@pytest.fixture
def registry(monkeypatch):
    CALLS.clear()
    demo = frameworks.Framework(id="demo", name="Demo", description="测试用框架。", icon="/icons/logo.svg",
                                runner="tests.test_run003a:demo_runner")
    items = (*frameworks.FRAMEWORKS, demo)
    monkeypatch.setattr(frameworks, "FRAMEWORKS", items)
    monkeypatch.setattr(frameworks, "_BY_ID", {item.id: item for item in items})
    monkeypatch.setattr(service, "default_runner", lambda job: CALLS.append(("tradingagents", job)) or 0)
    return demo


def test_the_worker_runs_the_framework_named_in_the_job(registry):
    assert worker.execute({"framework": "demo", "kind": "analysis"}) == 7
    assert [name for name, _ in CALLS] == ["demo"]


@pytest.mark.parametrize("job", [{}, {"framework": None}, {"framework": "tradingagents"}])
def test_an_old_or_default_job_runs_tradingagents(registry, job):
    assert worker.execute({**job, "kind": "analysis"}) == 0
    assert [name for name, _ in CALLS] == ["tradingagents"]


@pytest.mark.parametrize("value", ["nope", "TradingAgents", " demo", "", 5, ["demo"], {"id": "demo"}, True])
def test_an_unregistered_framework_in_a_job_runs_nothing(registry, value):
    with pytest.raises(frameworks.UnknownFramework) as caught:
        worker.execute({"framework": value, "kind": "analysis"})
    assert "没有注册，没有启动分析" in str(caught.value)
    assert CALLS == []


def test_the_worker_writes_a_chinese_failure_for_an_unknown_framework(registry, tmp_path, capsys):
    result = tmp_path / "r.json"
    job_path = tmp_path / "j.job.json"
    job_path.write_text(json.dumps({"framework": "gone", "kind": "analysis", "result_path": str(result)}),
                        encoding="utf-8")
    assert worker.main([str(job_path)]) == 1
    assert CALLS == []
    failure = json.loads(run_failure.failure_path(result).read_text(encoding="utf-8"))
    assert "没有注册" in failure["message"]


def test_the_public_framework_rows_do_not_show_the_runner(registry):
    assert all("runner" not in row for row in frameworks.public_frameworks([]))


def test_runner_for_an_unknown_id_refuses():
    with pytest.raises(frameworks.UnknownFramework):
        frameworks.runner_for("nope")


def test_the_registered_tradingagents_runner_is_the_default_runner():
    assert frameworks.runner_for("tradingagents") is service.default_runner


# ------------------------------------------------------------------ W40

HOLDER = textwrap.dedent("""
    import sys, time
    from webui import service
    service.acquire_console_lock()
    print("held", flush=True)
    time.sleep(30)
""")


@pytest.fixture
def lock_home(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    service.release_console_lock()
    yield tmp_path / "webui"
    service.release_console_lock()


def _holder():
    proc = subprocess.Popen([sys.executable, "-c", HOLDER], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_a_second_console_on_the_same_home_is_refused(lock_home):
    proc = _holder()
    try:
        with pytest.raises(service.ConsoleError) as caught:
            service.acquire_console_lock()
        assert caught.value.status == 409 and "另一个控制台" in str(caught.value)
    finally:
        proc.kill()
        proc.wait()
    service.acquire_console_lock()  # the OS dropped the dead holder's lock


def test_main_exits_before_recovery_when_another_console_holds_the_home(lock_home, monkeypatch, capsys):
    touched = []
    monkeypatch.setattr(service, "recover_runs_on_start", lambda: touched.append("recover"))
    monkeypatch.setattr(server, "serve", lambda *a, **k: touched.append("serve"))
    proc = _holder()
    try:
        assert server.main(["--port", "0"]) == 1
    finally:
        proc.kill()
        proc.wait()
    assert touched == []
    assert "另一个控制台" in capsys.readouterr().err


def test_the_lock_is_reentrant_in_one_console(lock_home):
    assert service.acquire_console_lock() is service.acquire_console_lock()


# ------------------------------------------------------------------ W48 / W57

def _sleeper(*extra):
    argv = [sys.executable, "-c", "import time; time.sleep(30)", *extra]
    return argv, subprocess.Popen(argv, start_new_session=True)


def test_an_id_substring_in_another_command_line_is_not_ours():
    run_id = "20261101_120000_0123abcd"
    argv, proc = _sleeper(f"/tmp/editor-{run_id}.job.json.swp")
    try:
        assert service._worker_state({"id": run_id, "pid": proc.pid}) == "foreign"
        assert service._worker_state({"id": run_id, "pid": proc.pid, "argv": argv[:-1]}) == "foreign"
        assert service._worker_state({"id": run_id, "pid": proc.pid, "argv": argv}) == "ours"
        elsewhere = ["/other/python", *argv[1:]]  # same job file, different program: not ours
        assert service._worker_state({"id": run_id, "pid": proc.pid, "argv": elsewhere}) == "foreign"
    finally:
        proc.kill()
        proc.wait()


def test_a_legacy_record_without_argv_matches_only_its_exact_job_file(tmp_path, monkeypatch):
    """W48 + W69: without argv, the process must be exactly <python> -m webui.worker <its job>."""
    run_id = "20261101_120000_0123abcd"
    record = {"id": run_id, "result": str(tmp_path / f"{run_id}.json")}
    job = str(tmp_path / f"{run_id}.job.json")
    cases = {
        (sys.executable, "-m", "webui.worker", job): True,
        ("/usr/bin/python3.13", "-m", "webui.worker", job): True,
        (sys.executable, "-m", "webui.worker", job + "x"): False,
        (sys.executable, "-c", "pass", "-m", "webui.worker", job): False,      # W69: extra tokens before
        ("/bin/sh", "-m", "webui.worker", job): False,                          # not a Python
        ("-m", "webui.worker", job): False,
        (sys.executable, "-m", "webui.worker", job, "extra"): False,
    }
    for argv, ours in cases.items():
        monkeypatch.setattr(service, "_process_argv", lambda pid, argv=argv: list(argv))
        assert service._argv_matches(record, 1) is ours, argv
        monkeypatch.setattr(service, "_process_argv", lambda pid: None)        # ps fallback, same rule
        monkeypatch.setattr(service, "_process_cmdline", lambda pid, argv=argv: " ".join(argv) + "\n")
        assert service._argv_matches(record, 1) is ours, ("ps", argv)
    monkeypatch.setattr(service, "_process_argv", lambda pid: [sys.executable, "-m", "webui.worker", job])
    assert service._argv_matches({"id": run_id}, 1) is False                   # nothing to match against


def test_the_ps_fallback_needs_the_whole_recorded_command_line(monkeypatch):
    argv, proc = _sleeper("x")
    monkeypatch.setattr(service, "_process_argv", lambda pid: None)
    try:
        monkeypatch.setattr(service, "_process_cmdline", lambda pid: " ".join(argv) + "\n")
        assert service._argv_matches({"argv": argv}, proc.pid) is True
        monkeypatch.setattr(service, "_process_cmdline", lambda pid: "vim " + " ".join(argv))
        assert service._argv_matches({"argv": argv}, proc.pid) is False
        monkeypatch.setattr(service, "_process_cmdline", lambda pid: "")
        assert service._argv_matches({"argv": argv}, proc.pid) is False
        assert service._argv_matches({"argv": [""]}, proc.pid) is False  # an unreadable line proves nothing
    finally:
        proc.kill()
        proc.wait()


# ------------------------------------------------------------------ W53 / W61

def test_a_burst_of_64_connections_is_queued_not_reset(console, http_console):
    # http_console only lets loopback through; this test runs its own server that does not accept yet
    httpd = server.serve("127.0.0.1", 0)
    assert httpd.request_queue_size >= 64
    port = httpd.server_address[1]
    sockets = []
    thread = None
    try:
        for _ in range(64):  # all connect while nobody accepts yet: only the backlog holds them
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(3)
            sock.connect(("127.0.0.1", port))
            sockets.append(sock)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        body = b"{}"
        for sock in sockets:
            sock.sendall(b"POST /api/runs/validate HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                         b"Content-Length: 2\r\nConnection: close\r\n\r\n" % port + body)
        for sock in sockets:
            data = sock.recv(64)
            assert data.startswith(b"HTTP/1."), data
    finally:
        for sock in sockets:
            sock.close()
        if thread is not None:
            httpd.shutdown()
        httpd.server_close()


# ------------------------------------------------------------------ W65

@pytest.mark.parametrize("asked", ["20991231_000000_deadbeef", "nope", "../x", "a" * 65])
def test_an_unknown_id_is_not_found_not_the_newest_run(console, asked):
    failed_run(console, '{"kind": "timeout"}')
    answer = service.api_runs(asked)
    assert answer["run"] is None and answer["selected"] is None
    assert answer["not_found"] is True and answer["error"] == "没有这个任务。"
    assert [r["id"] for r in answer["runs"]] == ["run-bad"]
    assert answer["counts"]["all"] == 1 and answer["result"]["present"] is False


def test_no_id_still_shows_the_newest_run(console):
    failed_run(console, '{"kind": "timeout"}')
    for asked in (None, ""):
        answer = service.api_runs(asked)
        assert answer["selected"] == "run-bad" and "not_found" not in answer


def test_http_unknown_id_is_a_200_not_found(console, http_console):
    failed_run(console, '{"kind": "timeout"}')
    status, body = http_console("/api/runs?id=nope")
    assert status == 200
    data = json.loads(body)
    assert data["run"] is None and data["not_found"] is True


# ------------------------------------------------------------------ W62

def test_selections_params_only_at_fixed_positions():
    sel = {"max_tokens": 4096, "api_key": "sk-x"}
    assert service.redact({"selections": sel})["selections"] == {"max_tokens": 4096, "api_key": "[redacted]"}
    assert service.redact({"run": {"selections": sel}})["run"]["selections"]["max_tokens"] == 4096
    assert service.redact({"runs": [{"selections": sel}]})["runs"][0]["selections"]["max_tokens"] == 4096
    for nested in ({"x": {"selections": sel}}, {"selections": {"selections": sel}},
                   {"run": {"x": {"selections": sel}}}, {"runs": [{"x": {"selections": sel}}]},
                   {"runs": [[{"selections": sel}]]}, {"x": [{"selections": sel}]},
                   {"run": {"run": {"selections": sel}}}, {"run": {"runs": [{"selections": sel}]}},
                   {"x": {"runs": [{"selections": sel}]}}):
        assert "4096" not in json.dumps(service.redact(nested)), nested
    assert service.redact([{"selections": sel}]) == [{"selections": {"max_tokens": "[redacted]", "api_key": "[redacted]"}}]


# ------------------------------------------------------------------ W31

SUPPLEMENTARY = '{"kind": "timeout", "message": "a \\udbff\\udfff b"}'


def test_a_supplementary_plane_char_is_kept_and_breaks_nothing(console, http_console):
    failed_run(console, SUPPLEMENTARY)
    for _ in range(3):
        status, body = http_console("/api/runs")
        assert status == 200, body[:300]
        assert json.loads(body)["run"]["status"] == "failed"
    stored = json.loads((console / "webui" / "runs" / "run-bad.json").read_text(encoding="utf-8"))
    assert stored["status"] == "failed" and stored["failure"]["message"] == "a \U0010ffff b"
    assert run_failure.safe_text("\U0010ffff") == "\U0010ffff"


# ------------------------------------------------------------------ W70 / W71 (RUN-003b)

def test_a_framework_mismatch_is_not_labelled_a_model_call_failure():
    from webui.frameworks import UnknownFramework
    from webui.run_failure import build_failure, clean_failure

    failure = build_failure(UnknownFramework("没有这个分析框架：x。"), {"id": "r"})
    assert failure["kind"] == "framework"
    assert failure["kind_zh"] == "分析框架不可用（未调用模型）"
    assert "调用失败" not in failure["summary"] and "没有这个分析框架" in failure["summary"]
    assert clean_failure(failure)["kind_zh"] == "分析框架不可用（未调用模型）"   # survives the read-back
    wrapped = RuntimeError("outer")
    wrapped.__cause__ = UnknownFramework("x")
    assert build_failure(wrapped)["kind"] == "framework"
    assert build_failure(RuntimeError("boom"))["kind_zh"] == "调用失败"            # others unchanged


def test_a_clean_exit_clears_the_pid_in_console_lock(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_CONSOLE_LOCK", None)
    service.acquire_console_lock()
    lock = tmp_path / service.CONSOLE_LOCK_NAME
    assert lock.read_text() == str(os.getpid())
    service.release_console_lock()
    assert not lock.exists()                              # W89: removed, not just emptied
    service.acquire_console_lock()                       # and it can be taken again
    service.release_console_lock()


def test_the_console_main_releases_the_lock_on_exit(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_CONSOLE_LOCK", None)

    class Stub:
        server_address = ("127.0.0.1", 1)

        def serve_forever(self):
            raise KeyboardInterrupt

        def server_close(self):
            pass

    monkeypatch.setattr(server, "serve", lambda host, port: Stub())
    monkeypatch.setattr(server, "start_run_dispatcher", lambda: None)
    assert server.main(["--port", "0"]) == 0
    assert not (tmp_path / service.CONSOLE_LOCK_NAME).exists()
    assert service._CONSOLE_LOCK is None
