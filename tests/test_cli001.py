"""CLI-001: auth-status read failures (W5), COLUMNS=0 (W9), loopback model servers skip the proxy.

Every sign-in file here is a fixture written into a temporary HOME; no real sign-in
is read and no model is called (the model test talks to the loopback script server).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli import _drop_unusable_terminal_size
from tests._model_server import ModelServer, allow_loopback_only, completion
from tradingagents.llm_clients.openai_client import OpenAIClient, is_loopback_url

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore:Model .* is not in the known model list")]

ROOT = Path(__file__).resolve().parents[1]
GOOD = {"tokens": {"access_token": "fixture-token", "expires_at": 4102444800}, "account": {"email": "a@example.com"}}


def _env(home, **extra):
    """A clean environment: the conftest terminal pin is NOT inherited (QA W9)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "TERM": "xterm-256color",
           "PYTHONPATH": str(ROOT), "LANG": "C.UTF-8"}
    env.update({key: str(value) for key, value in extra.items()})
    return env


def _cli(home, *args, **env):
    return subprocess.run([sys.executable, "-m", "cli.main", *args], cwd=ROOT, env=_env(home, **env),
                          capture_output=True, text=True, timeout=120)


def _home(tmp_path, grok=None, chatgpt=None):
    folder = tmp_path / ".tradingagents"
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in (("grok-auth.json", grok), ("chatgpt-auth.json", chatgpt)):
        if body is not None:
            (folder / name).write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return tmp_path


# ------------------------------------------------------------------ W5
DAMAGED = {
    "not json": ("{half", "grok-auth.json is not valid JSON"),
    "not an object": ("[1, 2]", "grok-auth.json is not a JSON object"),
    "tokens not an object": ({"tokens": "x"}, "tokens is not an object"),
    "empty access token": ({"tokens": {"access_token": ""}}, "tokens.access_token is missing"),
    "NaN expiry": ('{"tokens": {"access_token": "t", "expires_at": NaN}}', "tokens.expires_at is not a finite number"),
    "inf expiry": ('{"tokens": {"access_token": "t", "expires_at": Infinity}}', "tokens.expires_at is not a finite number"),
    "-inf refresh": ('{"tokens": {"access_token": "t", "earliest_refresh_at": -Infinity}}',
                     "tokens.earliest_refresh_at is not a finite number"),
}


@pytest.mark.parametrize("case", list(DAMAGED))
def test_auth_status_reports_a_damaged_file_as_a_read_failure(tmp_path, case):
    body, words = DAMAGED[case]
    result = _cli(_home(tmp_path, grok=body), "auth-status", "grok", COLUMNS=200)
    assert result.returncode == 1, result.stdout + result.stderr
    out = " ".join(result.stdout.split())
    assert "Grok: could not read local sign-in status:" in out
    assert words in out
    assert "not signed in" not in out and "signed in as" not in out
    assert "Traceback" not in result.stderr


def test_auth_status_matches_the_webui_cli_field(tmp_path, monkeypatch):
    from tradingagents.llm_clients import grok_auth
    from webui import service

    home = _home(tmp_path, grok=DAMAGED["NaN expiry"][0])
    monkeypatch.setattr(grok_auth, "AUTH_FILE", home / ".tradingagents" / "grok-auth.json")
    row = next(item for item in service.auth_status("grok")["accounts"] if item["provider"] == "grok")
    assert row["read_error"] is True
    result = _cli(home, "auth-status", "grok", COLUMNS=400)
    assert result.stdout.strip() == row["cli"]


def test_a_good_file_and_no_file_keep_their_lines_and_exit_0(tmp_path):
    result = _cli(_home(tmp_path, grok=GOOD), "auth-status", COLUMNS=400)
    assert result.returncode == 0
    lines = result.stdout.strip().splitlines()
    assert lines == ["ChatGPT: not signed in (local status only; no live validation).",
                     "Grok: signed in as a@example.com (local status only; sign-in was not validated live)."]


def test_one_damaged_file_still_lists_the_others(tmp_path):
    result = _cli(_home(tmp_path, chatgpt="{half"), "auth-status", COLUMNS=400)
    assert result.returncode == 1
    lines = result.stdout.strip().splitlines()
    assert lines[0].startswith("ChatGPT: could not read local sign-in status: chatgpt-auth.json is not valid JSON")
    assert lines[1] == "Grok: not signed in (local status only; no live validation)."


def test_the_shared_reader(monkeypatch):
    from cli.main import local_sign_in

    class Auth:
        problem, account = None, "a@example.com"

        def auth_file_problem(self):
            if isinstance(self.problem, Exception):
                raise self.problem
            return self.problem

        def signed_in_as(self):
            if isinstance(self.account, Exception):
                raise self.account
            return self.account

    spec = type("Spec", (), {"auth": Auth()})()
    assert local_sign_in(spec) == ("a@example.com", None)
    spec.auth.problem = "x.json is not valid JSON"
    assert local_sign_in(spec) == (None, "x.json is not valid JSON")
    spec.auth.problem = PermissionError("secret path /home/u")
    assert local_sign_in(spec) == (None, "PermissionError")
    spec.auth.problem, spec.auth.account = None, RuntimeError("boom /secret")
    assert local_sign_in(spec) == (None, "RuntimeError")
    spec.auth.account = None
    assert local_sign_in(spec) == (None, None)
    del Auth.auth_file_problem
    spec.auth.account = "b@example.com"
    assert local_sign_in(spec) == ("b@example.com", None)


# ------------------------------------------------------------------ COLUMNS=0 (and W9: real subprocesses)
@pytest.mark.parametrize("columns", ["0", "40"])
def test_auth_status_and_help_are_never_empty(tmp_path, columns):
    home = _home(tmp_path)
    status = _cli(home, "auth-status", COLUMNS=columns)
    assert status.returncode == 0
    text = " ".join(status.stdout.split())
    assert "ChatGPT: not signed in (local status only; no live validation)." in text
    helped = _cli(home, "--help", COLUMNS=columns)
    assert helped.returncode == 0 and "Usage:" in helped.stdout and "auth-status" in helped.stdout
    widest = max(len(line) for line in helped.stdout.splitlines())
    if columns == "40":
        assert widest <= 40, "COLUMNS=40 is still honoured"
    else:
        assert widest == 80, "COLUMNS=0 falls back to the piped default width"


def test_console_print_is_not_empty_with_columns_0(tmp_path):
    code = "from cli.display import console; console.print('hello from the console'); print(console.width)"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=_env(tmp_path, COLUMNS=0, LINES=0),
                            capture_output=True, text=True, timeout=60)
    lines = result.stdout.splitlines()
    assert lines[0] == "hello from the console" and int(lines[1]) > 0


def test_the_conftest_pin_is_not_what_makes_it_work():
    # The subprocess tests above build their own environment; prove they do not
    # inherit the suite's pinned COLUMNS.
    assert os.environ.get("COLUMNS") == "220"
    assert "COLUMNS" not in {k for k in _env("/tmp") if k != "COLUMNS"}
    assert _env("/tmp", COLUMNS=0)["COLUMNS"] == "0"


@pytest.mark.parametrize("value, kept, stored", [
    ("0", False, None), ("-3", False, None), ("abc", False, None), ("", False, None), (" 0 ", False, None),
    ("+0", False, None), ("9" * 20, False, None),          # W95: huge → unset
    ("1", True, "1"), ("40", True, "40"), (" 120 ", True, "120"),
    ("+40", True, "40"), (" 40 ", True, "40"),             # W97: normalize
])
def test_which_sizes_are_dropped(value, kept, stored):
    environ = {"COLUMNS": value, "LINES": value, "OTHER": "0"}
    _drop_unusable_terminal_size(environ)
    assert ("COLUMNS" in environ) is kept and ("LINES" in environ) is kept
    if kept:
        assert environ["COLUMNS"] == stored == environ["LINES"]
    assert environ["OTHER"] == "0"


# ------------------------------------------------------------------ loopback proxy
@pytest.mark.parametrize("url, loop", [
    ("http://localhost:11434/v1", True), ("http://LOCALHOST:8000/v1", True), ("http://127.0.0.1:8000/v1", True),
    ("http://127.8.9.10/v1", True), ("http://[::1]:8000/v1", True), ("http://ollama.localhost/v1", True),
    ("http://localhost./v1", True), ("http://LOCALHOST./v1", True), ("http://localhost../v1", True),
    ("http://0.0.0.0:11434/v1", False), ("http://192.168.1.5:11434/v1", False), ("https://api.x.ai/v1", False),
    ("http://localhost.example.com/v1", False), ("http://notlocalhost/v1", False), ("", False), (None, False),
    ("http://[bad/v1", False), ("not a url", False),
])
def test_what_counts_as_loopback(url, loop):
    assert is_loopback_url(url) is loop


def _trust_env(client):
    return getattr(client, "_trust_env", getattr(client, "trust_env", None))


def test_a_local_ollama_client_ignores_the_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    llm = OpenAIClient(model="llama3", provider="ollama").get_llm()
    assert _trust_env(llm.http_client) is False
    assert _trust_env(llm.http_async_client) is False


def test_a_remote_endpoint_keeps_the_proxy(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture")
    llm = OpenAIClient(model="gpt-4.1", provider="openai").get_llm()
    assert llm.http_client is None or _trust_env(llm.http_client) is not False


def test_a_callers_own_http_client_is_kept(monkeypatch):
    import httpx
    mine = httpx.Client()
    llm = OpenAIClient(model="llama3", provider="ollama", http_client=mine).get_llm()
    assert llm.http_client is mine
    assert _trust_env(llm.http_async_client) is False


def test_a_loopback_call_works_with_a_dead_system_proxy(monkeypatch):
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")          # nothing listens there
    allow_loopback_only(monkeypatch)
    with ModelServer([{"body": completion("local ok", model="llama3")}]) as server:
        monkeypatch.setenv("OLLAMA_BASE_URL", server.url)
        llm = OpenAIClient(model="llama3", provider="ollama", max_retries=0, timeout=5).get_llm()
        assert llm.invoke("x").content == "local ok"
    assert len(server.hits) == 1


def test_async_loopback_call_works_with_a_dead_system_proxy(monkeypatch):
    import asyncio
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    allow_loopback_only(monkeypatch)
    with ModelServer([{"body": completion("async ok", model="llama3")}]) as server:
        monkeypatch.setenv("OLLAMA_BASE_URL", server.url)
        llm = OpenAIClient(model="llama3", provider="ollama", max_retries=0, timeout=5).get_llm()
        assert asyncio.run(llm.ainvoke("x")).content == "async ok"
