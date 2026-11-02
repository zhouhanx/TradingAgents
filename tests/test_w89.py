"""W89: --host ::1 / [::1] / hosts with surrounding whitespace start the console
(IPv6 loopback is local) or fail with a clear Chinese error, and console.lock is
removed on every exit path, including SIGTERM/SIGHUP (as W74) and a failed start.

The consoles run in subprocesses with a temporary HOME and TRADINGAGENTS_WEBUI_HOME;
no model is called.
"""

from __future__ import annotations

import os
import re
import signal
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from webui import server, service

pytestmark = pytest.mark.skipif(os.name != "posix", reason="signals and flock are Unix-only")
ROOT = Path(__file__).resolve().parents[1]
STARTED = re.compile(r"^控制台已启动：http://(\S+):(\d+)$")


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


needs_ipv6 = pytest.mark.skipif(not _ipv6_loopback(), reason="this machine has no IPv6 loopback")


def _env(home: Path) -> dict:
    return {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LANG": "C.UTF-8", "TERM": "dumb",
            "PYTHONPATH": str(ROOT), "TRADINGAGENTS_WEBUI_HOME": str(home / "webui"), "NO_PROXY": "*"}


def _console(home: Path, *args: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-m", "webui", *args], cwd=home, env=_env(home),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _run(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "webui", *args], cwd=home, env=_env(home),
                          capture_output=True, text=True, timeout=60)


def _get(host: str, port: str) -> str:
    """GET / from a child process (the suite's network guard stays on in this one)."""
    code = textwrap.dedent(f"""
        import urllib.request
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))
        with opener.open("http://{host}:{port}/", timeout=10) as r:
            print(r.status, b'src="/app.js"' in r.read())
    """)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout.strip()


def _lock(home: Path) -> Path:
    return home / "webui" / service.CONSOLE_LOCK_NAME


# ------------------------------------------------------------------ hosts that start
STARTS = [
    pytest.param("::1", "[::1]", marks=needs_ipv6, id="::1"),
    pytest.param("[::1]", "[::1]", marks=needs_ipv6, id="[::1]"),
    pytest.param(" [::1] ", "[::1]", marks=needs_ipv6, id="spaced [::1]"),
    pytest.param("\t::1\n", "[::1]", marks=needs_ipv6, id="tab ::1 newline"),
    pytest.param(" 127.0.0.1 ", "127.0.0.1", id="spaced 127.0.0.1"),
    pytest.param(" localhost", "localhost", id="spaced localhost"),
]


@pytest.mark.parametrize("host, shown", STARTS)
@pytest.mark.parametrize("stop", ["SIGTERM", "SIGHUP"])
def test_loopback_hosts_start_serve_and_stop_cleanly(tmp_path, host, shown, stop):
    process = _console(tmp_path, "--host", host, "--port", "0")
    try:
        line = process.stdout.readline().strip()
        started = STARTED.match(line)
        assert started, line + process.stderr.read() if process.poll() is not None else line
        assert started.group(1) == shown
        assert _lock(tmp_path).read_text() == str(process.pid)
        assert _get(shown, started.group(2)) == "200 True"
        process.send_signal(getattr(signal, stop))
        out, err = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.returncode == 0, err
    assert "控制台已停止。" in out and "Traceback" not in err
    assert not _lock(tmp_path).exists()


# ------------------------------------------------------------------ hosts that fail cleanly
def test_an_empty_host_is_refused_in_chinese(tmp_path):
    for host in ("", "   ", "[]", " [ ] "):
        result = _run(tmp_path, "--host", host, "--port", "0")
        assert result.returncode == 2, host
        assert result.stderr.strip() == server.BAD_HOST
        assert not _lock(tmp_path).exists()


@pytest.mark.parametrize("args", [["--host", "[::1"], ["--host", "fe80::zz", "--allow-lan"],
                                  ["--host", "no-such-host.invalid", "--allow-lan"]],
                         ids=["half bracket", "bad ipv6 with --allow-lan", "unresolvable with --allow-lan"])
def test_a_host_that_cannot_be_bound_fails_cleanly(tmp_path, args):
    result = _run(tmp_path, *args, "--port", "0")
    assert result.returncode == 1, result.stderr
    assert "无法启动控制台：不能在" in result.stderr and "Traceback" not in result.stderr
    assert not _lock(tmp_path).exists()


def test_a_port_in_use_fails_cleanly_and_removes_the_lock(tmp_path):
    (tmp_path / "a").mkdir()
    first = _console(tmp_path / "a", "--port", "0")
    try:
        port = STARTED.match(first.stdout.readline().strip()).group(2)
        (tmp_path / "b").mkdir()
        result = _run(tmp_path / "b", "--port", port)
        assert result.returncode == 1
        assert f"端口 {port} 上监听" in result.stderr and "Traceback" not in result.stderr
        assert not _lock(tmp_path / "b").exists()
        assert _lock(tmp_path / "a").exists()               # the running console keeps its own
    finally:
        first.terminate()
        first.wait(timeout=20)
    assert not _lock(tmp_path / "a").exists()


def test_a_second_console_on_the_same_home_leaves_the_first_ones_lock(tmp_path):
    first = _console(tmp_path, "--port", "0")
    try:
        assert STARTED.match(first.stdout.readline().strip())
        result = _run(tmp_path, "--port", "0")
        assert result.returncode == 1 and "另一个控制台在运行" in result.stderr
        assert _lock(tmp_path).read_text() == str(first.pid)
    finally:
        first.terminate()
        first.wait(timeout=20)
    assert not _lock(tmp_path).exists()


# ------------------------------------------------------------------ in-process failure paths
@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_CONSOLE_LOCK", None)
    before = {sig: signal.getsignal(sig) for sig in server._STOP_SIGNALS}
    yield tmp_path
    service.release_console_lock()
    assert {sig: signal.getsignal(sig) for sig in server._STOP_SIGNALS} == before


def test_a_failed_bind_removes_the_lock(home, monkeypatch, capsys):
    def refuse(host, port):
        assert (home / service.CONSOLE_LOCK_NAME).exists()
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(server, "serve", refuse)
    assert server.main(["--port", "1"]) == 1
    assert "Address already in use" in capsys.readouterr().err
    assert not (home / service.CONSOLE_LOCK_NAME).exists() and service._CONSOLE_LOCK is None


@pytest.mark.parametrize("stop", ["SIGTERM", "SIGHUP"])
def test_a_stop_signal_during_start_up_removes_the_lock(home, monkeypatch, stop):
    class Stub:
        server_address = ("127.0.0.1", 1)
        closed = False

        def server_close(self):
            Stub.closed = True

        def serve_forever(self):
            pytest.fail("the signal arrived before serving")

    monkeypatch.setattr(server, "serve", lambda host, port: Stub())
    monkeypatch.setattr(server, "start_run_dispatcher", lambda: os.kill(os.getpid(), getattr(signal, stop)))
    assert server.main(["--port", "0"]) == 0
    assert Stub.closed
    assert not (home / service.CONSOLE_LOCK_NAME).exists() and service._CONSOLE_LOCK is None


def test_an_unexpected_start_up_error_still_removes_the_lock(home, monkeypatch):
    class Stub:
        server_address = ("127.0.0.1", 1)

        def server_close(self):
            pass

    monkeypatch.setattr(server, "serve", lambda host, port: Stub())
    monkeypatch.setattr(server, "start_run_dispatcher", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        server.main(["--port", "0"])
    assert not (home / service.CONSOLE_LOCK_NAME).exists() and service._CONSOLE_LOCK is None


def test_a_refused_lan_host_never_takes_the_lock(home, monkeypatch):
    monkeypatch.setattr(service, "acquire_console_lock", lambda: pytest.fail("must refuse before the lock"))
    assert server.main(["--host", " 192.168.1.5 ", "--port", "0"]) == 2


# ------------------------------------------------------------------ helpers
@pytest.mark.parametrize("given, bound", [("::1", "::1"), ("[::1]", "::1"), (" [::1] ", "::1"), ("[ ::1 ]", "::1"),
                                          (" 127.0.0.1\t", "127.0.0.1"), ("localhost", "localhost"),
                                          ("[::1", "[::1"), ("0.0.0.0", "0.0.0.0"),
                                          ("[[::1]]", "::1"), ("[[[::1]]]", "::1"),  # W105
                                          ("１２７.０.０.１", "127.0.0.1"), ("［：：１］", "::1")])  # W106 NFKC
def test_normalize_bind_host(given, bound):
    assert server.normalize_bind_host(given) == bound


@pytest.mark.parametrize("given", ["", "  ", "[]", "[ ]", None])
def test_normalize_bind_host_rejects_empty(given):
    with pytest.raises(ValueError):
        server.normalize_bind_host(given)


def test_url_host():
    assert server.url_host("::1") == "[::1]" and server.url_host("127.0.0.1") == "127.0.0.1"


@needs_ipv6
def test_serve_binds_ipv6_and_answers_the_bracketed_host_header():
    httpd = server.serve(" [::1] ", 0)
    try:
        assert httpd.address_family == socket.AF_INET6 and httpd.server_address[0] == "::1"
        assert f"[::1]:{httpd.server_address[1]}" in httpd.allowed_hosts
    finally:
        httpd.server_close()


def test_the_lock_is_retaken_when_its_file_was_replaced(home, monkeypatch):
    """A lock taken on a file another console just removed is retried on the new file."""
    import fcntl

    path = home / service.CONSOLE_LOCK_NAME
    real_flock, calls = fcntl.flock, []

    def flock(fd, op):
        real_flock(fd, op)
        calls.append(fd)
        if len(calls) == 1:                 # the previous console removes the file right now
            path.unlink()
            path.write_text("")

    monkeypatch.setattr(fcntl, "flock", flock)
    handle = service.acquire_console_lock()
    assert len(calls) == 2
    assert os.path.samestat(os.fstat(handle.fileno()), os.stat(path))
    assert path.read_text() == str(os.getpid())
