"""Local process manager for the TradingAgents CLI.

The launcher manages one checkout at a time.  ``start`` runs the normal CLI,
``--background`` only detaches that same CLI, and a private Unix-domain socket
is used by ``status`` and ``stop``.  The public entry point is::

    python -m cli.launcher [start|stop|restart|status|logs|login|logout|auth-status]

With no command, ``start`` is implied.  Authentication commands are exec'd
into ``cli.main`` so their arguments and exit codes remain unchanged.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

try:  # The launcher is Unix-only; keep the failure explicit on Windows.
    import fcntl
except ImportError as exc:  # pragma: no cover - Windows only
    raise RuntimeError("TradingAgents launcher requires a Unix-like platform") from exc


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_HOME = ".tradingagents"
STARTUP_TIMEOUT = 10.0
STOP_TIMEOUT = 15.0
TERM_GRACE = 5.0
POLL_INTERVAL = 0.05


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    directory: Path
    lock: Path
    state: Path
    last: Path
    socket: Path
    log: Path



def could_not_start_sentence(exc) -> str:
    return f"TradingAgents could not start the CLI: {exc}"


def background_failed_sentence(log_path) -> str:
    return f"TradingAgents background startup failed; see {log_path}"


def background_started_sentence(log_path) -> str:
    return f"TradingAgents started in background; log: {log_path}"


def could_not_read_log_sentence(exc) -> str:
    return f"Could not read log: {exc}"


LOGS_LINES_INTEGER = "logs: --lines requires an integer"
# Canonical prog for catalog/Proto; runtime --help rewrites via launcher_prog().
_CANONICAL_PROG = "python -m cli.launcher"
LOGS_USAGE = f"usage: {_CANONICAL_PROG} logs [--lines N]"
LOGS_HELP = (
    f"usage: {_CANONICAL_PROG} logs [--lines N]\n\n"
    "Show the background log tail and its path."
)
STOP_USAGE = f"usage: {_CANONICAL_PROG} stop"
STATUS_USAGE = f"usage: {_CANONICAL_PROG} status"


def launcher_prog() -> str:
    """User-facing name for this process in --help and usage errors (W103)."""
    if not sys.argv:
        return _CANONICAL_PROG
    base = Path(sys.argv[0]).name
    if base == "tradingagents-launcher":
        return "tradingagents-launcher"
    if base == "launcher.py":
        return _CANONICAL_PROG
    # ``python -m cli`` → cli/__main__.py redirects here
    if base == "__main__.py" and "cli" in Path(sys.argv[0]).parts:
        return "python -m cli"
    # pytest / direct main([...]) calls: keep the canonical string so catalog
    # rows and existing Proto assertions stay stable.
    return _CANONICAL_PROG


def format_usage(text: str) -> str:
    """Rewrite canonical ``python -m cli.launcher`` to the live invocation name."""
    prog = launcher_prog()
    if prog == _CANONICAL_PROG:
        return text
    return text.replace(_CANONICAL_PROG, prog)


def help_text() -> str:
    """Launcher --help body for the current argv0."""
    return format_usage(HELP)


def not_analysis_argument_sentence(command: str) -> str:
    """Stderr when start or restart is given a login command instead of analysis args."""
    prog = launcher_prog()
    return (
        f"launcher: '{command}' is not an analysis argument; use "
        f"'{prog} {command}' (or 'cli {command}')."
    )
LOG_PREFIX = "Log:"
NOT_RUNNING = "TradingAgents: not running"
NOT_RUNNING_STALE = "TradingAgents: not running (stale runtime metadata cleaned)"
STOP_NOT_RUNNING = "TradingAgents is not running."
ALREADY_RUNNING = "TradingAgents is already running."
ALREADY_RUNNING_LOCK = "TradingAgents is already running"
STILL_STARTING = "TradingAgents is still starting; try stop again shortly."
STOPPING = "Stopping TradingAgents…"
STOP_TIMEOUT_SENTENCE = "TradingAgents did not stop within the timeout."


def status_sentence(phase: str, child_pid=None) -> str:
    """The status line ``TradingAgents: <phase>`` with the child pid when known."""
    suffix = f" (child pid {child_pid})" if child_pid else ""
    return f"TradingAgents: {phase}{suffix}"


def status_argv_line(argv) -> str:
    """The second status line. Printed only when a supervisor response has argv."""
    return "argv: " + json.dumps(list(argv), ensure_ascii=False)


def log_path_sentence(log_path) -> str:
    return f"{LOG_PREFIX} {log_path}"




def _project_root() -> Path:
    override = os.environ.get("TRADINGAGENTS_LAUNCHER_ROOT")
    if override:
        return Path(override).resolve()
    return PROJECT_ROOT


def _runtime_home() -> Path:
    """Home used for runtime files. Tests isolate this without touching production."""

    override = os.environ.get("TRADINGAGENTS_RUNTIME_HOME")
    if override:
        return Path(override).resolve()
    return Path.home()


def runtime_paths(root: Path | str | None = None) -> RuntimePaths:
    """Return private runtime paths for *root*."""

    resolved = Path(root if root is not None else _project_root()).resolve()
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
    directory = _runtime_home() / RUNTIME_HOME / "runtime" / digest
    socket_path = directory / "control.sock"
    # AF_UNIX pathname limits are roughly 104 bytes on macOS and 108 on
    # Linux.  Test sandboxes and unusually long home directories can exceed
    # that even though the production layout is short, so retain all runtime
    # metadata in the requested directory and use a deterministic owner-only
    # fallback for the socket itself.
    if len(os.fsencode(str(socket_path))) >= 96:
        uid = getattr(os, "getuid", lambda: 0)()
        socket_path = Path(tempfile.gettempdir()) / f"tradingagents-{uid}-{digest}.sock"
    return RuntimePaths(
        root=resolved,
        directory=directory,
        lock=directory / "launcher.lock",
        state=directory / "state.json",
        last=directory / "last.json",
        socket=socket_path,
        log=directory / "launcher.log",
    )


def _ensure_runtime_directory(paths: RuntimePaths) -> None:
    paths.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(paths.directory, 0o700)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically write owner-private JSON metadata."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _acquire_lock(paths: RuntimePaths) -> Any | None:
    """Acquire the checkout lock without blocking a second invocation."""

    _ensure_runtime_directory(paths)
    fd = os.open(paths.lock, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        if isinstance(exc, BlockingIOError) or exc.errno in (errno.EACCES, errno.EAGAIN):
            os.close(fd)
            return None
        os.close(fd)
        raise
    return os.fdopen(fd, "a+b", buffering=0)


def _release_lock(handle: Any | None) -> None:
    if handle is None:
        return
    with contextlib.suppress(OSError, ValueError):
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _close_lock_without_unlock(handle: Any | None) -> None:
    """Close a descriptor after its flock has been handed to a child.

    ``flock`` is attached to the open file description.  The detached
    supervisor inherits that description, so an explicit ``LOCK_UN`` in the
    parent would release the lock for both processes.
    """

    if handle is None:
        return
    with contextlib.suppress(OSError, ValueError):
        handle.close()
    with contextlib.suppress(OSError, ValueError):
        handle.close()


def _save_last(paths: RuntimePaths, argv: Sequence[str], background: bool) -> None:
    _write_json(paths.last, {"argv": list(argv), "background": bool(background), "updated_at": time.time()})


def _load_last(paths: RuntimePaths) -> tuple[list[str], bool] | None:
    value = _read_json(paths.last)
    if not value or not isinstance(value.get("argv"), list):
        return None
    argv = value["argv"]
    if not all(isinstance(item, str) for item in argv):
        return None
    return list(argv), bool(value.get("background", False))


def _validate_analysis_args(argv: Sequence[str]) -> bool:
    """Reject auth commands accidentally placed behind a managed start.

    Authentication is an immediate CLI operation, not an analysis child.  In
    particular, rejecting these first tokens keeps browser/login output out of
    a detached analysis log; callers should use the top-level command or the
    explicit ``cli`` passthrough instead.
    """

    if argv and argv[0] in {"login", "logout", "auth-status"}:
        print(not_analysis_argument_sentence(argv[0]), file=sys.stderr)
        return False
    return True


def _sibling_console_script(name: str) -> str | None:
    """Path to an installed ``tradingagents*`` console script next to argv0.

    When this process *is* an installed entry point (editable or wheel), analysis
    and re-exec should use the sibling script so the child loads the same
    distribution — site-packages for ``pip install .``, the project tree for
    ``pip install -e`` — instead of ``python -m cli.main`` which prefers a
    ``cli/`` package in cwd (W103). ``python -m cli.launcher`` from a checkout
    returns None so tests and source runs keep ``-m cli.main``.
    """
    if not sys.argv:
        return None
    argv0 = Path(sys.argv[0])
    if not argv0.name.startswith("tradingagents"):
        return None
    candidate = argv0.with_name(name)
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _launcher_reexec_command(*extra: str) -> list[str]:
    """Re-enter the launcher: installed script when available, else ``-m``."""
    sibling = _sibling_console_script("tradingagents-launcher")
    if sibling is not None:
        return [sibling, *extra]
    return [sys.executable, "-m", "cli.launcher", *extra]


def analysis_command(argv: Sequence[str]) -> list[str]:
    """Build the command run by a supervisor (separate for test injection).

    ``TRADINGAGENTS_LAUNCHER_ANALYSIS_COMMAND`` is an internal JSON string list
    used so a detached supervisor can run a fake child. It is not a user setting.

    W103: an installed launcher prefers the sibling ``tradingagents`` entry point
    so non-editable installs do not pick up a cwd ``cli/`` shadow; editable
    installs still resolve to the project via the editable .pth. Source
    ``python -m cli.launcher`` keeps ``python -m cli.main``.
    """

    override = os.environ.get("TRADINGAGENTS_LAUNCHER_ANALYSIS_COMMAND")
    if not override:
        sibling = _sibling_console_script("tradingagents")
        if sibling is not None:
            return [sibling, *argv]
        return [sys.executable, "-m", "cli.main", *argv]
    try:
        command = json.loads(override)
    except ValueError as exc:
        raise RuntimeError(
            "TRADINGAGENTS_LAUNCHER_ANALYSIS_COMMAND must be a JSON string list"
        ) from exc
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(item, str) and item for item in command)
    ):
        raise RuntimeError("TRADINGAGENTS_LAUNCHER_ANALYSIS_COMMAND must be a JSON string list")
    return [*command, *argv]


def _safe_unlink_stale_socket(paths: RuntimePaths) -> None:
    if not paths.socket.exists():
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.25)
        probe.connect(str(paths.socket))
    except OSError:
        _unlink(paths.socket)
    else:
        raise RuntimeError(ALREADY_RUNNING_LOCK)
    finally:
        probe.close()


def _bind_control_socket(paths: RuntimePaths) -> socket.socket:
    _ensure_runtime_directory(paths)
    _safe_unlink_stale_socket(paths)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(paths.socket))
        os.chmod(paths.socket, 0o600)
        server.listen(8)
        server.settimeout(0.5)
    except BaseException:
        server.close()
        _unlink(paths.socket)
        raise
    return server


def _signal_owned_group(child: subprocess.Popen[Any] | None, sig: int) -> bool:
    """Signal a live child group owned by this supervisor only."""

    if child is None or child.poll() is not None:
        return False
    try:
        pgid = os.getpgid(child.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    if pgid != child.pid:
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def _normal_exit_code(returncode: int | None) -> int:
    if returncode is None:
        return 1
    return 128 + (-returncode) if returncode < 0 else returncode


class _Supervisor:
    """Own one analysis child and its control socket."""

    def __init__(self, paths: RuntimePaths, argv: Sequence[str], background: bool, lock_handle: Any) -> None:
        self.paths = paths
        self.argv = list(argv)
        self.background = background
        self.lock_handle = lock_handle
        self.server: socket.socket | None = None
        self.thread: threading.Thread | None = None
        self.child: subprocess.Popen[Any] | None = None
        self.child_lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.shutdown_requested = threading.Event()
        self.pending_signal: int | None = None
        self.term_sent = False
        self.term_deadline: float | None = None
        self.phase = "starting"
        self.exit_code: int | None = None
        self.started_at = time.time()

    def _snapshot(self) -> dict[str, Any]:
        with self.child_lock:
            child = self.child
            child_pid = child.pid if child is not None else None
            child_running = child is not None and child.poll() is None
            phase = self.phase
            if phase == "running" and not child_running:
                phase = "completed"
            return {
                "ok": True,
                "status": phase,
                "supervisor_pid": os.getpid(),
                "child_pid": child_pid,
                "argv": list(self.argv),
                "background": self.background,
                "started_at": self.started_at,
                "exit_code": self.exit_code,
            }

    def _write_state(self) -> None:
        snapshot = self._snapshot()
        snapshot["state_version"] = 1
        _write_json(self.paths.state, snapshot)

    def request_stop(self, sig: int = signal.SIGTERM) -> None:
        self.stop_requested.set()
        with self.child_lock:
            if self.phase not in {"completed", "stopping"}:
                self.phase = "stopping"
            if self.child is not None and self.child.poll() is None and not self.term_sent:
                _signal_owned_group(self.child, sig)
                self.term_sent = True
                self.term_deadline = time.monotonic() + TERM_GRACE
        with contextlib.suppress(OSError):
            self._write_state()

    def _control_response(self, command: str) -> dict[str, Any]:
        if command == "status":
            return self._snapshot()
        if command == "stop":
            self.request_stop()
            return {"ok": True, "status": "stopping"}
        return {"ok": False, "error": "unknown command"}

    def _serve_control(self) -> None:
        assert self.server is not None
        while not self.shutdown_requested.is_set():
            try:
                connection, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with connection:
                try:
                    connection.settimeout(1.0)
                    raw = connection.recv(128).decode("utf-8", "replace")
                    command = raw.strip().splitlines()[0] if raw.strip() else ""
                    response = self._control_response(command)
                    connection.sendall((json.dumps(response, separators=(",", ":")) + "\n").encode())
                except (OSError, ValueError):
                    pass

    def _launch_child(self) -> None:
        try:
            command = analysis_command(self.argv)
        except RuntimeError as exc:
            print(could_not_start_sentence(exc), file=sys.stderr, flush=True)
            self.exit_code = 127
            with self.child_lock:
                self.phase = "completed"
            return
        try:
            child = subprocess.Popen(
                command,
                cwd=str(self.paths.root),
                env=os.environ.copy(),
                start_new_session=True,
            )
        except OSError as exc:
            print(could_not_start_sentence(exc), file=sys.stderr, flush=True)
            self.exit_code = 127
            with self.child_lock:
                self.phase = "completed"
            return
        with self.child_lock:
            self.child = child
            self.phase = "stopping" if self.stop_requested.is_set() else "running"
        with contextlib.suppress(OSError):
            self._write_state()
        if self.stop_requested.is_set():
            self.request_stop()

    def _wait_for_child(self) -> int:
        while True:
            with self.child_lock:
                child = self.child
                if child is not None and self.stop_requested.is_set() and not self.term_sent:
                    self.phase = "stopping"
                    _signal_owned_group(child, self.pending_signal or signal.SIGTERM)
                    self.term_sent = True
                    self.term_deadline = time.monotonic() + TERM_GRACE
                returncode = child.poll() if child is not None else None
                deadline = self.term_deadline
                term_sent = self.term_sent
            if child is None:
                return self.exit_code if self.exit_code is not None else 127
            if returncode is not None:
                self.exit_code = _normal_exit_code(returncode)
                return self.exit_code
            if (
                self.stop_requested.is_set()
                and term_sent
                and deadline is not None
                and time.monotonic() >= deadline
            ):
                with self.child_lock:
                    _signal_owned_group(child, signal.SIGKILL)
                    try:
                        returncode = child.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        returncode = child.poll()
                self.exit_code = _normal_exit_code(returncode)
                return self.exit_code
            time.sleep(POLL_INTERVAL)

    def _cleanup_child(self) -> None:
        """Terminate and reap a still-live owned child during exceptional exit."""

        with self.child_lock:
            child = self.child
            if child is None or child.poll() is not None:
                return
            _signal_owned_group(child, signal.SIGTERM)
            try:
                child.wait(timeout=TERM_GRACE)
            except subprocess.TimeoutExpired:
                _signal_owned_group(child, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=2.0)

    def run(self) -> int:
        previous_handlers: dict[int, Any] = {}

        def on_signal(signum: int, _frame: Any) -> None:
            # Signal handlers must not lock, perform I/O, or call waitpid:
            # the main loop will forward this signal under child_lock.
            self.pending_signal = signum
            self.stop_requested.set()

        try:
            self.server = _bind_control_socket(self.paths)
            self.phase = "starting"
            self._write_state()
            self.thread = threading.Thread(target=self._serve_control, name="launcher-control", daemon=True)
            self.thread.start()
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, on_signal)
            self._launch_child()
            code = self._wait_for_child()
            with self.child_lock:
                self.phase = "completed"
            with contextlib.suppress(OSError):
                self._write_state()
            return code
        finally:
            self.shutdown_requested.set()
            if self.server is not None:
                with contextlib.suppress(OSError):
                    self.server.close()
            if self.thread is not None:
                self.thread.join(timeout=1.0)
            self._cleanup_child()
            if self.server is not None:
                _unlink(self.paths.socket)
            _unlink(self.paths.state)
            _release_lock(self.lock_handle)
            for signum, handler in previous_handlers.items():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(signum, handler)


def _run_supervisor(paths: RuntimePaths, argv: Sequence[str], background: bool, lock_handle: Any) -> int:
    return _Supervisor(paths, argv, background, lock_handle).run()


def _connect(paths: RuntimePaths, timeout: float = 0.5) -> socket.socket | None:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(timeout)
        connection.connect(str(paths.socket))
        return connection
    except OSError:
        connection.close()
        return None


def _request(paths: RuntimePaths, command: str, timeout: float = 1.0) -> dict[str, Any] | None:
    connection = _connect(paths, timeout)
    if connection is None:
        return None
    try:
        connection.settimeout(timeout)
        connection.sendall((command + "\n").encode("ascii"))
        data = connection.recv(65536)
    except OSError:
        return None
    finally:
        connection.close()
    try:
        value = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _wait_for_socket(paths: RuntimePaths, process: subprocess.Popen[Any], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        response = _request(paths, "status", timeout=0.25)
        if response and response.get("ok") and response.get("status") == "running" and response.get("child_pid"):
            return True
        time.sleep(POLL_INTERVAL)
    return False


def _terminate_owned_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=TERM_GRACE)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2.0)


def _start_background(paths: RuntimePaths, argv: Sequence[str]) -> int:
    lock_handle = _acquire_lock(paths)
    if lock_handle is None:
        print(ALREADY_RUNNING, file=sys.stderr)
        return 1
    log_fd: int | None = None
    process: subprocess.Popen[Any] | None = None
    handed_off = False
    try:
        _save_last(paths, argv, True)
        if paths.log.exists():
            os.chmod(paths.log, 0o600)
        log_fd = os.open(paths.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.fchmod(log_fd, 0o600)
        child_env = os.environ.copy()
        child_env["TRADINGAGENTS_LAUNCHER_LOCK_FD"] = str(lock_handle.fileno())
        process = subprocess.Popen(
            _launcher_reexec_command("--_supervise", *argv),
            cwd=str(paths.root),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            pass_fds=(lock_handle.fileno(),),
        )
        if not _wait_for_socket(paths, process, STARTUP_TIMEOUT):
            _terminate_owned_process(process)
            print(background_failed_sentence(paths.log), file=sys.stderr)
            return 1
        handed_off = True
        print(background_started_sentence(paths.log))
        return 0
    finally:
        if log_fd is not None:
            with contextlib.suppress(OSError):
                os.close(log_fd)
        if handed_off:
            _close_lock_without_unlock(lock_handle)
        else:
            _release_lock(lock_handle)


def _start_foreground(paths: RuntimePaths, argv: Sequence[str]) -> int:
    lock_handle = _acquire_lock(paths)
    if lock_handle is None:
        print(ALREADY_RUNNING, file=sys.stderr)
        return 1
    try:
        _save_last(paths, argv, False)
        return _run_supervisor(paths, argv, False, lock_handle)
    except RuntimeError as exc:
        _release_lock(lock_handle)
        print(str(exc), file=sys.stderr)
        return 1


def _status(paths: RuntimePaths) -> int:
    response = _request(paths, "status")
    if response and response.get("ok"):
        phase = str(response.get("status", "unknown"))
        print(status_sentence(phase, response.get("child_pid")))
        if response.get("argv") is not None:
            print(status_argv_line(response["argv"]))
        return 0 if phase in {"starting", "running", "stopping"} else 1
    stale = _read_json(paths.state)
    lock_handle = _acquire_lock(paths)
    if lock_handle is not None:
        _unlink(paths.state)
        _unlink(paths.socket)
        _release_lock(lock_handle)
    if stale:
        print(NOT_RUNNING_STALE)
    else:
        print(NOT_RUNNING)
    return 1


def _wait_for_exit(paths: RuntimePaths, deadline: float) -> bool:
    """Wait until the supervisor has released the checkout lock.

    The control socket is removed before the lock, so returning as soon as the
    socket disappears can make restart fail with "already running".
    """

    while time.monotonic() < deadline:
        lock_handle = _acquire_lock(paths)
        if lock_handle is not None:
            _unlink(paths.socket)
            _unlink(paths.state)
            _release_lock(lock_handle)
            return True
        time.sleep(POLL_INTERVAL)
    return False


def _stop(paths: RuntimePaths, *, quiet_if_missing: bool = False) -> int:
    deadline = time.monotonic() + STOP_TIMEOUT
    response = _request(paths, "stop")
    while response is None and time.monotonic() < deadline:
        lock_probe = _acquire_lock(paths)
        if lock_probe is not None:
            _release_lock(lock_probe)
            break
        time.sleep(POLL_INTERVAL)
        response = _request(paths, "stop")
    if response is None:
        lock_handle = _acquire_lock(paths)
        if lock_handle is not None:
            _unlink(paths.socket)
            _unlink(paths.state)
            _release_lock(lock_handle)
            if quiet_if_missing:
                return 0
            print(STOP_NOT_RUNNING)
            return 1
        if quiet_if_missing:
            return 1
        print(STILL_STARTING, file=sys.stderr)
        return 1

    print(STOPPING)
    if _wait_for_exit(paths, deadline):
        return 0
    print(STOP_TIMEOUT_SENTENCE, file=sys.stderr)
    return 1


def _logs(paths: RuntimePaths, argv: Sequence[str]) -> int:
    lines = 40
    remaining = list(argv)
    if "--help" in remaining or "-h" in remaining:
        print(format_usage(LOGS_HELP))
        return 0
    if "--lines" in remaining:
        index = remaining.index("--lines")
        if index + 1 >= len(remaining):
            print(LOGS_LINES_INTEGER, file=sys.stderr)
            return 2
        try:
            lines = int(remaining[index + 1])
        except ValueError:
            print(LOGS_LINES_INTEGER, file=sys.stderr)
            return 2
        del remaining[index : index + 2]
    if remaining or lines < 0:
        print(format_usage(LOGS_USAGE), file=sys.stderr)
        return 2
    print(log_path_sentence(paths.log))
    if not paths.log.exists():
        return 0
    try:
        with paths.log.open(encoding="utf-8", errors="replace") as handle:
            tail = handle.readlines()[-lines:] if lines else []
    except OSError as exc:
        print(could_not_read_log_sentence(exc), file=sys.stderr)
        return 1
    for line in tail:
        print(line, end="")
    return 0


HELP = """usage: python -m cli.launcher [COMMAND] [ARGS...]

Manage one TradingAgents CLI run for this checkout. With no command, start is
used. Analysis arguments are passed unchanged to cli.main.

commands:
  start [--background] [ARGS...]  Run the interactive CLI (or detach it)
  stop                            Ask the owned analysis process to stop
  restart [--background] [ARGS...] Stop, then start with saved/new arguments
  status                          Show the local process state
  logs [--lines N]                Show the background log tail and path
  login/logout/auth-status ...    Exec the corresponding cli.main command
  cli ...                         Raw passthrough to cli.main
  web [--host H] [--port N] [--allow-lan]  Open the local Chinese console

Runtime files live under ~/.tradingagents/runtime/<checkout-hash>.
"""


def _exec_cli_main(argv: Sequence[str]) -> NoReturn:
    sibling = _sibling_console_script("tradingagents")
    if sibling is not None:
        os.execv(sibling, [sibling, *argv])
    else:
        os.execv(sys.executable, [sys.executable, "-m", "cli.main", *argv])
    raise AssertionError("os.execv returned")  # pragma: no cover


def _parse_manager_command(argv: Sequence[str]) -> tuple[str, list[str]]:
    if not argv:
        return "start", []
    first = argv[0]
    known = {"start", "stop", "restart", "status", "logs", "login", "logout", "auth-status", "cli", "web"}
    if first in known:
        return first, list(argv[1:])
    if first.startswith("-"):
        return "start", list(argv)
    return first, list(argv[1:])


def _strip_background(argv: Sequence[str]) -> tuple[list[str], bool]:
    result: list[str] = []
    background = False
    for item in argv:
        if item == "--background":
            background = True
        else:
            result.append(item)
    return result, background



def unknown_command_sentence(command: str) -> str:
    """Stderr for a token that is not a launcher command, including the help text."""
    return f"unknown launcher command: {command}\n\n{help_text()}"


MISSING_INHERITED_LOCK = "launcher: missing inherited lock"


def invalid_inherited_lock_sentence(exc: BaseException) -> str:
    """Stderr when ``--_supervise`` is given a lock descriptor that cannot be used."""
    return f"launcher: invalid inherited lock: {exc}"


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in {"--help", "-h"}:
        print(help_text())
        return 0
    if args and args[0] == "--_supervise":
        lock_fd = os.environ.pop("TRADINGAGENTS_LAUNCHER_LOCK_FD", None)
        if lock_fd is None:
            print(MISSING_INHERITED_LOCK, file=sys.stderr)
            return 2
        try:
            inherited_fd = int(lock_fd)
            lock_handle = os.fdopen(inherited_fd, "a+b", buffering=0)
        except (OSError, ValueError) as exc:
            print(invalid_inherited_lock_sentence(exc), file=sys.stderr)
            return 2
        return _run_supervisor(runtime_paths(), args[1:], True, lock_handle)

    command, command_args = _parse_manager_command(args)
    paths = runtime_paths()
    if command == "start":
        if any(item in {"--help", "-h"} for item in command_args) and len(command_args) == 1:
            print(help_text())
            return 0
        analysis_args, background = _strip_background(command_args)
        if not _validate_analysis_args(analysis_args):
            return 2
        return _start_background(paths, analysis_args) if background else _start_foreground(paths, analysis_args)
    if command == "stop":
        if command_args:
            print(format_usage(STOP_USAGE), file=sys.stderr)
            return 2
        return _stop(paths)
    if command == "status":
        if command_args:
            print(format_usage(STATUS_USAGE), file=sys.stderr)
            return 2
        return _status(paths)
    if command == "logs":
        return _logs(paths, command_args)
    if command == "restart":
        if any(item in {"--help", "-h"} for item in command_args) and len(command_args) == 1:
            print(help_text())
            return 0
        analysis_args, requested_background = _strip_background(command_args)
        explicit_args = bool(analysis_args)
        saved = _load_last(paths)
        if not explicit_args and saved is not None:
            analysis_args, saved_background = saved
            background = requested_background or saved_background
        else:
            background = requested_background
        if not _validate_analysis_args(analysis_args):
            return 2
        stop_result = _stop(paths, quiet_if_missing=True)
        if stop_result != 0:
            return stop_result
        return _start_background(paths, analysis_args) if background else _start_foreground(paths, analysis_args)
    if command in {"login", "logout", "auth-status"}:
        _exec_cli_main([command, *command_args])
    if command == "cli":
        _exec_cli_main(command_args)
    if command == "web":
        from webui.server import main as web_main
        return web_main(command_args)
    print(unknown_command_sentence(command), file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:  # W109: clean exit if Ctrl-C lands after imports
        print("已取消。", file=sys.stderr, flush=True)
        raise SystemExit(130) from None
