"""Shared pytest fixtures that prevent CI hangs when API keys are absent."""

import os
import socket

import pytest


def _blank_settings_overlay():
    """Blank every TRADINGAGENTS_* setting before the package is imported.

    The package loads .env on import and folds these variables into
    DEFAULT_CONFIG, so a contributor's own settings would become the defaults
    the suite asserts on. A blank value is still present, so load_dotenv leaves
    it alone, and the overlay reads it as unset. Tests of the overlay set their own.
    """
    from dotenv import dotenv_values, find_dotenv

    names = set(os.environ)
    for filename in (".env", ".env.enterprise"):
        names |= set(dotenv_values(find_dotenv(filename, usecwd=True)))
    for name in names:
        if name.startswith("TRADINGAGENTS_"):
            os.environ[name] = ""


_blank_settings_overlay()


def _own_file_locations():
    """Keep the suite's results, cache and memory log in a directory of its own.

    Set before the package is imported, whose defaults put them in the user's
    home: a test reading the user's cache would see what their runs fetched.
    """
    import tempfile

    home = tempfile.mkdtemp(prefix="tradingagents-tests-")
    os.environ["TRADINGAGENTS_RESULTS_DIR"] = os.path.join(home, "logs")
    os.environ["TRADINGAGENTS_CACHE_DIR"] = os.path.join(home, "cache")
    os.environ["TRADINGAGENTS_MEMORY_LOG_PATH"] = os.path.join(home, "memory", "trading_memory.md")


_own_file_locations()


# One fixed terminal for the whole suite, whatever the caller's shell has (Eric's has
# COLUMNS=0; agents run with TERM=dumb, FORCE_COLOR=0, NO_COLOR=1). Several tests
# compare text captured in-process (Rich/typer, sized when the CLI is imported) with
# the output of a ``python -m cli.main`` subprocess or a CliRunner, and Rich/typer
# wrap, box-split or colour that text by COLUMNS/LINES/TERM/FORCE_COLOR. Pinned here,
# before the package is imported, and again around every test (``_pinned_terminal``)
# so subprocesses and CliRunner inherit the same values. A test of terminal handling
# sets its own with monkeypatch.
PINNED_TERMINAL = {"COLUMNS": "220", "LINES": "50", "TERM": "xterm-256color"}
UNPINNED_TERMINAL = ("FORCE_COLOR", "NO_COLOR", "TERMINAL_WIDTH", "CLICOLOR_FORCE", "PY_COLORS",
                     "TTY_COMPATIBLE", "TTY_INTERACTIVE", "JUPYTER_COLUMNS", "JUPYTER_LINES",
                     "_TYPER_FORCE_DISABLE_TERMINAL", "TYPER_USE_RICH")


def _pin_terminal(setenv=os.environ.__setitem__, delenv=lambda name: os.environ.pop(name, None)):
    for name, value in PINNED_TERMINAL.items():
        setenv(name, value)
    for name in UNPINNED_TERMINAL:
        delenv(name)


_pin_terminal()


def pytest_configure(config):
    for marker in ("unit", "integration", "smoke"):
        config.addinivalue_line("markers", f"{marker}: {marker}-level tests")
    # yfinance caches each symbol's time zone on disk, in the home directory
    # by default; the suite keeps it in a directory of its own.
    import tempfile

    import yfinance

    yfinance.set_tz_cache_location(tempfile.mkdtemp(prefix="tradingagents-tests-yf-"))


@pytest.fixture(autouse=True)
def _pinned_terminal(monkeypatch):
    """Re-pin the terminal around every test, so one that changed os.environ directly
    cannot leak its COLUMNS/TERM into the next test's subprocesses."""
    _pin_terminal(monkeypatch.setenv, lambda name: monkeypatch.delenv(name, raising=False))


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Tests do not reach the network; one that must is marked integration."""
    if request.node.get_closest_marker("integration"):
        return

    def refuse(*args, **kwargs):
        raise OSError(f"test tried to reach the network: {args[1:] or kwargs}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: refuse(None, *a))
    # yfinance's HTTP client is libcurl, which never touches Python's sockets.
    from curl_cffi import requests as curl_requests

    monkeypatch.setattr(curl_requests.Session, "request", refuse)
    monkeypatch.setattr(curl_requests.AsyncSession, "request", refuse)


@pytest.fixture(autouse=True)
def _at_a_terminal(monkeypatch):
    """Tests of the interactive steps run as if at a terminal; pytest's stdin is
    not one. A test of an unattended run sets isatty to False itself."""
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


@pytest.fixture(autouse=True)
def _own_cli_prefs(tmp_path, monkeypatch):
    """The CLI keeps the last run's selections in the user's home; tests keep theirs apart."""
    monkeypatch.setattr("cli.prefs._PREFS_PATH", tmp_path / "cli_prefs.json")


_API_KEY_ENV_VARS = (
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_CN_API_KEY",
    "ZHIPU_API_KEY",
    "ZHIPU_CN_API_KEY",
    "MINIMAX_API_KEY",
    "MINIMAX_CN_API_KEY",
    "OPENROUTER_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "ALPHA_VANTAGE_API_KEY",
)


@pytest.fixture(autouse=True)
def _dummy_api_keys(monkeypatch):
    for env_var in _API_KEY_ENV_VARS:
        # `or` not a .get default: an env var present but empty (e.g. a key left
        # blank in a .env copied from .env.example) must still get the placeholder.
        monkeypatch.setenv(env_var, os.environ.get(env_var) or "placeholder")


@pytest.fixture(autouse=True)
def _isolate_config():
    """Reset the global dataflows config before and after each test.

    ``set_config`` merges (it never clears keys absent from the override), so a
    test that sets e.g. ``tool_vendors`` would otherwise leak into later tests
    and make routing behavior order-dependent. Replace the global outright so
    every test starts from a clean DEFAULT_CONFIG.
    """
    import copy

    import tradingagents.dataflows.config as config_module
    import tradingagents.default_config as default_config

    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)
    yield
    config_module._config = copy.deepcopy(default_config.DEFAULT_CONFIG)


# ---- Real dotenv protection (AUDIT-FIX-02) -------------------------------
#
# ``find_dotenv(usecwd=True)`` walks up from the cwd. When pytest's tmp_path sits
# inside the project (e.g. TMPDIR under .awr/), a test that chdir'd into tmp_path
# found and rewrote the real project .env. Every config-writing module now looks
# only in the cwd, and set_key refuses the real files outright.

_PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
_REAL_DOTENV_NAMES = (".env", ".env.enterprise")


def _real_dotenv_paths():
    from pathlib import Path

    roots = {_PROJECT_ROOT, Path(os.path.expanduser("~")).resolve()}
    return {(root / name).resolve() for root in roots for name in _REAL_DOTENV_NAMES}


def _cwd_only_find_dotenv(filename=".env", raise_error_if_not_found=False, usecwd=False):
    from pathlib import Path

    candidate = Path.cwd() / filename
    if candidate.is_file():
        return str(candidate)
    if raise_error_if_not_found:
        raise OSError("File not found")
    return ""


def _guarded_set_key(original):
    def set_key(dotenv_path, *args, **kwargs):
        from pathlib import Path

        if Path(dotenv_path).resolve() in _real_dotenv_paths():
            raise AssertionError(f"test tried to write a real config file: {dotenv_path}")
        return original(dotenv_path, *args, **kwargs)

    return set_key


def _snapshot_real_dotenvs():
    import hashlib

    snap = {}
    for path in sorted(_real_dotenv_paths()):
        if path.exists():
            st = path.stat()
            snap[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest(), st.st_mode)
        else:
            snap[str(path)] = None
    return snap


@pytest.fixture(autouse=True, scope="session")
def _real_dotenv_unchanged():
    """Fail the session if any real .env changed bytes or mode (values never printed)."""
    before = _snapshot_real_dotenvs()
    yield
    after = _snapshot_real_dotenvs()
    changed = [p for p in before if before[p] != after.get(p)]
    assert not changed, f"real config files changed during the test run: {changed}"


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch):
    import dotenv

    import cli.prompts
    import webui.service

    for module in (cli.prompts, webui.service):
        monkeypatch.setattr(module, "find_dotenv", _cwd_only_find_dotenv)
        monkeypatch.setattr(module, "set_key", _guarded_set_key(dotenv.set_key))
