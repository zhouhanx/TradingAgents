"""Focused lifecycle tests for the local TradingAgents process manager."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

fcntl = pytest.importorskip("fcntl")
import cli.launcher as launcher  # noqa: E402

# Captured before the suite's autouse network block replaces socket.socket.connect.
_ORIGINAL_CONNECT = socket.socket.connect


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_LAUNCHER_ROOT", str(root))
    monkeypatch.setenv("TRADINGAGENTS_RUNTIME_HOME", str(home))
    monkeypatch.setattr(launcher.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(launcher, "PROJECT_ROOT", root)
    return launcher.runtime_paths(root)


@pytest.fixture
def allow_unix_socket(monkeypatch):
    """The suite's no-network fixture blocks all socket.connect calls."""

    def connect(sock, address):
        if sock.family == socket.AF_UNIX:
            return _ORIGINAL_CONNECT(sock, address)
        raise OSError("network disabled in launcher test")

    monkeypatch.setattr(socket.socket, "connect", connect)


def _sleeping_command(monkeypatch, marker: Path, seconds: int = 30):
    script = (
        "import pathlib,sys,time; "
        f"pathlib.Path({str(marker)!r}).write_text(__import__('json').dumps(sys.argv[1:])); "
        f"time.sleep({seconds})"
    )
    monkeypatch.setattr(
        launcher,
        "analysis_command",
        lambda argv: [sys.executable, "-c", script, *argv],
    )


@pytest.mark.unit
def test_supervise_without_a_lock_does_not_start(tmp_path):
    env = os.environ.copy()
    env.pop("TRADINGAGENTS_LAUNCHER_LOCK_FD", None)
    env["TRADINGAGENTS_RUNTIME_HOME"] = str(tmp_path)
    missing = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "--_supervise"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stderr == launcher.MISSING_INHERITED_LOCK + "\n"
    assert missing.stdout == ""
    env["TRADINGAGENTS_LAUNCHER_LOCK_FD"] = "nope"
    nope = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "--_supervise"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert nope.returncode == 2
    try:
        int("nope")
    except ValueError as exc:
        expected = launcher.invalid_inherited_lock_sentence(exc)
    assert nope.stderr == expected + "\n"
    assert "nope" in nope.stderr
    env["TRADINGAGENTS_LAUNCHER_LOCK_FD"] = ""
    blank = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "--_supervise"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert blank.returncode == 2
    assert launcher.MISSING_INHERITED_LOCK not in blank.stderr
    assert "invalid inherited lock" in blank.stderr


def test_live_socket_prints_the_sentence_without_a_period(runtime, allow_unix_socket, capsys):
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    runtime.socket.parent.mkdir(parents=True, exist_ok=True)
    server.bind(str(runtime.socket))
    server.listen(1)
    try:
        code = launcher._start_foreground(runtime, ["--ticker", "NVDA"])
    finally:
        server.close()
    assert code == 1
    err = capsys.readouterr().err.strip()
    assert err == launcher.ALREADY_RUNNING_LOCK
    assert err != launcher.ALREADY_RUNNING
    assert not err.endswith(".")


def test_foreground_start_status_stop_preserves_argv_and_cleans_up(
    runtime, monkeypatch, tmp_path, allow_unix_socket
):
    marker = tmp_path / "argv.json"
    _sleeping_command(monkeypatch, marker)
    requested = ["--ticker", "AAPL", "--date", "2026-10-28"]
    observed: dict[str, object] = {}

    def controller():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not runtime.socket.exists():
            time.sleep(0.01)
        observed["status"] = launcher._request(runtime, "status", timeout=1)
        while time.monotonic() < deadline and not marker.exists():
            time.sleep(0.01)
        observed["stop"] = launcher._request(runtime, "stop", timeout=1)

    thread = threading.Thread(target=controller)
    thread.start()
    result = launcher.main(["start", *requested])
    thread.join(timeout=5)

    assert result == 143
    assert observed["status"]["status"] in {"starting", "running"}
    assert observed["status"]["argv"] == requested
    assert observed["stop"]["status"] == "stopping"
    assert json.loads(marker.read_text()) == requested
    assert not runtime.socket.exists()
    assert not runtime.state.exists()
    assert launcher.main(["status"]) == 1


@pytest.mark.unit
def test_duplicate_lock_does_not_start_second_process(runtime):
    first = launcher._acquire_lock(runtime)
    assert first is not None
    try:
        assert launcher._acquire_lock(runtime) is None
    finally:
        launcher._release_lock(first)


@pytest.mark.unit
def test_stale_state_never_kills_unrelated_pid(runtime):
    unrelated = subprocess.Popen(["sleep", "30"])
    try:
        launcher._write_json(
            runtime.state,
            {"supervisor_pid": unrelated.pid, "child_pid": unrelated.pid, "status": "running"},
        )
        assert launcher._stop(runtime) == 1
        assert unrelated.poll() is None
        assert not runtime.state.exists()
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=3)


@pytest.mark.unit
def test_natural_failure_cleans_runtime_and_keeps_last_args(runtime, monkeypatch):
    monkeypatch.setattr(
        launcher,
        "analysis_command",
        lambda argv: [sys.executable, "-c", "raise SystemExit(7)", *argv],
    )
    args = ["--ticker", "FAIL"]
    assert launcher.main(["start", *args]) == 7
    assert not runtime.socket.exists()
    assert not runtime.state.exists()
    saved = launcher._load_last(runtime)
    assert saved == (args, False)


@pytest.mark.unit
def test_restart_reuses_saved_argv_and_mode(runtime, monkeypatch):
    launcher._save_last(runtime, ["--ticker", "NVDA", "--show"], True)
    called: dict[str, object] = {}
    monkeypatch.setattr(launcher, "_stop", lambda paths, quiet_if_missing=False: 0)
    monkeypatch.setattr(
        launcher,
        "_start_background",
        lambda paths, argv: called.update(paths=paths, argv=list(argv)) or 23,
    )

    assert launcher.main(["restart"]) == 23
    assert called["argv"] == ["--ticker", "NVDA", "--show"]


@pytest.mark.unit
def test_auth_commands_exec_verbatim_and_managed_auth_is_rejected(monkeypatch):
    captured: list[object] = []

    def fake_execv(executable, argv):
        captured.extend([executable, argv])
        raise SystemExit(0)

    monkeypatch.setattr(launcher.os, "execv", fake_execv)
    with pytest.raises(SystemExit):
        launcher.main(["login", "chatgpt", "--extra"])
    assert captured == [sys.executable, [sys.executable, "-m", "cli.main", "login", "chatgpt", "--extra"]]


@pytest.mark.unit
def test_start_rejects_auth_token_before_background_start(runtime, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(launcher, "_start_background", lambda *args: called.append(args) or 99)
    assert launcher.main(["start", "--background", "login", "grok"]) == 2
    assert called == []
    assert "use 'python -m cli.launcher login'" in capsys.readouterr().err


def _fake_analysis_env(monkeypatch, marker: Path) -> None:
    script = (
        "import json, os, pathlib, sys, time\n"
        f"path = pathlib.Path({str(marker)!r})\n"
        "print('LAUNCHER_CHILD_READY', flush=True)\n"
        "path.write_text(json.dumps({'pid': os.getpid(), 'argv': sys.argv[1:]}))\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setenv("PYTHONUNBUFFERED", "1")
    monkeypatch.setenv(
        "TRADINGAGENTS_LAUNCHER_ANALYSIS_COMMAND",
        json.dumps([sys.executable, "-c", script]),
    )


def _wait_for_marker(marker: Path, previous: int | None = None, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        if marker.exists():
            last = json.loads(marker.read_text())
            if previous is None or last.get("pid") != previous:
                return last
        time.sleep(0.05)
    raise AssertionError(f"child marker was not updated: {last}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.unit
def test_background_lifecycle_owns_only_its_child(runtime, monkeypatch, tmp_path, allow_unix_socket, capsys):
    marker = tmp_path / "child.json"
    _fake_analysis_env(monkeypatch, marker)
    unrelated = subprocess.Popen(["sleep", "30"])
    args = ["--ticker", "NVDA", "--date", "2026-10-28"]
    try:
        assert launcher.main(["start", "--background", *args]) == 0
        first = _wait_for_marker(marker)
        assert first["argv"] == args
        assert launcher.main(["status"]) == 0
        assert "running" in capsys.readouterr().out
        assert launcher.main(["start", "--background", "--ticker", "AAPL"]) == 1
        assert "already running" in capsys.readouterr().err
        assert _pid_alive(first["pid"])
        assert launcher.main(["logs", "--lines", "20"]) == 0
        assert "LAUNCHER_CHILD_READY" in capsys.readouterr().out

        assert launcher.main(["restart"]) == 0
        second = _wait_for_marker(marker, previous=first["pid"])
        assert second["argv"] == args
        assert second["pid"] != first["pid"]
        assert not _pid_alive(first["pid"])
        assert _pid_alive(unrelated.pid)
        assert launcher.main(["stop"]) == 0
        assert not _pid_alive(second["pid"])
    finally:
        launcher.main(["stop"])
        if unrelated.poll() is None:
            unrelated.terminate()
            unrelated.wait(timeout=3)

    assert launcher.main(["status"]) == 1
    assert not runtime.socket.exists()
    assert not runtime.state.exists()
    assert _pid_alive(unrelated.pid) is False
    assert unrelated.poll() is not None
