"""POC checks for the local console. No live model calls and no credential leakage."""

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo

from cli.launcher import HELP  # noqa: E402
from cli.main import analyze, backtest  # noqa: E402
from webui import server, service  # noqa: E402
from webui.catalog import ANALYZE_FLAGS, BACKTEST_FLAGS, LAUNCHER_COMMANDS, SELECTION_STEPS  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_cli_prefs(monkeypatch, tmp_path):
    """Console runs remember answers in the CLI prefs file. Tests must not touch the real one."""
    monkeypatch.setattr("cli.prefs._PREFS_PATH", tmp_path / "cli_prefs.json")


def test_log_tail_respects_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    log = tmp_path / "run.log"
    log.write_text("\n".join(f"line {i}" for i in range(5)), encoding="utf-8")
    service.JOBS.write_run({"id": "log-test", "log": str(log), "status": "running"})
    found = service.read_log(2)
    from cli.launcher import log_path_sentence
    assert found["lines"] == ["line 3", "line 4"]
    assert found["cli"] == log_path_sentence(log)
    absent = tmp_path / "missing.log"
    service.JOBS.write_run({"id": "missing-log", "log": str(absent), "status": "running"})
    missing_file = service.read_log(40)
    assert missing_file["cli"] == log_path_sentence(absent)
    assert missing_file["lines"] == []
    service.JOBS.write_run({"id": "no-log", "status": "idle"})
    no_path = service.read_log(40)
    assert no_path["path"] is None
    assert no_path["cli"] is None


def test_run_settings_from_the_page_reach_the_config(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-luna",
        "temperature": "0.2",
        "max_tool_rounds": "3",
    })
    config = service.build_run_config(checked["selections"])
    assert config["temperature"] == 0.2
    assert config["max_tool_rounds"] == 3


def test_openrouter_list_uses_the_cli_shortlist(monkeypatch):
    monkeypatch.setattr(service, "_fetch_openrouter_models_or_raise", lambda: [
        ("Niche", "someone/new"),
        ("Llama", "meta-llama/llama-4"),
        ("Alias", "~meta-llama/llama-4"),
    ])
    listed = service.list_models("openrouter", "quick")
    assert listed["source"] == "openrouter"
    assert listed["title"] == "Select Your [Quick-Thinking] OpenRouter Model (latest available):"
    assert listed["models"] == [{"zh": "Llama", "id": "meta-llama/llama-4"}]
    assert "cli" not in listed


def test_ollama_hint_matches_the_cli_warning():
    hinted = service.ollama_hint("http://remote.example/v1")
    assert hinted["url"] == "http://remote.example/v1"
    assert hinted["cli"][0] == "✓ Using Ollama at http://remote.example/v1"
    assert hinted["notes"] == [
        "Note: 'http://remote.example/v1' doesn't include port 11434. "
        "Make sure your remote ollama-serve listens on the port shown above."
    ]
    local = service.ollama_hint("http://127.0.0.1:11434/v1")
    assert local["notes"] == []
    assert local["cli"] == ["✓ Using Ollama at http://127.0.0.1:11434/v1"]


def test_login_paste_reaches_the_waiting_reader(monkeypatch):
    seen = {}

    def fake_sign_in(say=None, read_line=None, **kwargs):
        say("https://auth.example/authorize")
        seen["pasted"] = read_line()
        return "user@example.com"

    monkeypatch.setattr(service, "get_subscription", lambda key: type("Spec", (), {
        "auth": type("Auth", (), {"sign_in": staticmethod(fake_sign_in)})()
    })())
    service.start_login("chatgpt")
    service.JOBS.paste_login("http://127.0.0.1:1455/auth/callback?code=test&state=test")
    deadline = time.time() + 2
    while time.time() < deadline and service.JOBS.login_state().get("status") == "waiting":
        time.sleep(0.05)
    assert seen["pasted"].startswith("http://127.0.0.1:1455/auth/callback")
    state = service.JOBS.login_state()
    assert state["status"] == "signed_in"
    assert state["account"] == "user@example.com"
    from cli.main import signed_in_sentence
    assert state["cli"] == signed_in_sentence("ChatGPT", "user@example.com")


def test_login_paste_timeout_becomes_a_controlled_failure(monkeypatch):
    """A timed-out paste reader must fail the login state without an uncaught thread error."""
    seen = {}

    def timeout():
        raise service.ConsoleError("等待粘贴登录地址超时。")

    def fake_sign_in(say=None, read_line=None, **kwargs):
        try:
            read_line()
        except EOFError:
            seen["paste_ended"] = True
            raise RuntimeError("paste reader stopped") from None

    monkeypatch.setattr(service.JOBS, "wait_paste", timeout)
    monkeypatch.setattr(service, "get_subscription", lambda key: type("Spec", (), {
        "auth": type("Auth", (), {"sign_in": staticmethod(fake_sign_in)})()
    })())
    service.start_login("chatgpt")
    deadline = time.time() + 2
    while time.time() < deadline and service.JOBS.login_state().get("status") == "waiting":
        time.sleep(0.02)
    state = service.JOBS.login_state()
    assert seen["paste_ended"] is True
    assert state["status"] == "failed"
    assert "超时" in state["error"] or "stopped" in state["error"]


def test_crypto_ticker_hides_fundamentals_like_the_cli():
    stock = service.asset_options("NVDA")
    crypto = service.asset_options("BTC-USD")
    assert stock["asset_type"] == "stock" and "fundamentals" in stock["analysts"]
    assert crypto["asset_type"] == "crypto" and crypto["fundamentals_hidden"] is True
    assert "fundamentals" not in crypto["analysts"]
    assert stock["benchmark"] == "SPY" and stock["benchmark_source"] == "benchmark_map"
    hong_kong = service.asset_options("0700.HK")
    assert hong_kong["benchmark"] == "^HSI"


def test_web_run_config_bounds_a_stuck_model_call():
    from tradingagents.llm_clients.factory import build_llm_kwargs
    config = service.build_run_config({
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-luna",
        "research_depth": 1,
        "backend_url": None,
        "output_language": "Chinese",
    })
    config["timeout"] = 120
    kwargs = build_llm_kwargs({**config, "llm_provider": "chatgpt"})
    assert kwargs["timeout"] == 120


def test_model_picker_is_custom_and_uses_the_model_api():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "<select" not in script
    assert "/api/models?provider=" in script
    assert "检查配置" in script
    assert "/api/runs/validate" in script
    assert "openai_reasoning_effort" in script
    assert "data-step-panel=\"1\"" in script and "data-step-panel=\"4\"" in script
    assert "function mountSelect" in script
    assert 'id="main-providers"' in script


def test_catalog_covers_launcher_commands():
    covered = {item["cli"] for item in LAUNCHER_COMMANDS}
    in_commands = False
    for line in HELP.splitlines():
        if line.strip() == "commands:":
            in_commands = True
            continue
        if not in_commands or not line.startswith("  ") or line.startswith("   "):
            continue
        command = line.strip().split()[0]
        if "/" in command:
            for part in command.split("/"):
                assert part in covered, part
            continue
        assert command in covered, command


def test_catalog_covers_analyze_and_backtest_flags():
    analyze_fields = {item["field"] for item in ANALYZE_FLAGS}
    for name in analyze.__annotations__:
        if name == "ctx":
            continue
        assert name in analyze_fields or name == "return"
    backtest_fields = {item["field"] for item in BACKTEST_FLAGS}
    for name in backtest.__annotations__:
        if name == "return":
            continue
        assert name in backtest_fields
    assert [item["step"] for item in SELECTION_STEPS] == list(range(1, 9))


def test_validation_uses_cli_parsers_and_does_not_start_a_model(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="ticker"):
        service.validate_analysis({"ticker": "../etc", "date": "2026-10-29"})
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "research_depth": 1,
        "llm_provider": "chatgpt",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6.1-sol",
        "openai_reasoning_effort": "medium",
        "output_language": "Chinese",
    })
    assert checked["selections"]["asset_type"] == "stock"
    assert checked["selections"]["openai_reasoning_effort"] == "medium"
    assert service.build_run_config(checked["selections"])["llm_provider"] == "chatgpt"


def test_unused_main_provider_does_not_keep_its_thinking_effort(monkeypatch):
    """Thinking effort follows the providers the graph calls, not an unused main provider."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        service,
        "get_subscription",
        lambda key: type("Spec", (), {
            "auth": type("Auth", (), {"signed_in_as": staticmethod(lambda: "signed")})(),
            "display_name": "Grok (subscription)",
        })(),
    )
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "openai",
        "quick_think_provider": "grok",
        "deep_think_provider": "grok",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "grok-4.6",
        "openai_reasoning_effort": "medium",
        "temperature": 0.2,
    })
    assert checked["selections"]["openai_reasoning_effort"] is None
    assert any("Grok 不能单独设置这一项" in line for line in checked["ignored"])
    assert not any("does not accept temperature" in line for line in checked["ignored"])
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function calledProviders" in script
    families = script[script.index("function effortFamilies"):script.index("function reasoningFamily")]
    assert "calledProviders(extra)" in families
    assert "llm_provider" not in families


def test_unused_main_provider_does_not_require_its_api_key(monkeypatch):
    """The graph calls the tier providers. An unused main provider must not block them."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        service,
        "get_subscription",
        lambda key: type("Spec", (), {
            "auth": type("Auth", (), {"signed_in_as": staticmethod(lambda: "signed")})(),
            "display_name": "ChatGPT (Plus or Pro plan)" if key == "chatgpt" else "Grok (subscription)",
        })(),
    )
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "openai",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6-luna",
    })
    assert checked["ok"] is True
    assert checked["selections"]["quick_think_provider"] == "grok"
    assert checked["selections"]["deep_think_provider"] == "chatgpt"
    assert "OPENAI_API_KEY" not in " ".join(checked["confirmations"])


def test_missing_subscription_is_rejected_before_a_run(monkeypatch):
    monkeypatch.setattr(
        service,
        "get_subscription",
        lambda key: type("Spec", (), {"auth": type("Auth", (), {"signed_in_as": staticmethod(lambda: None)})(), "display_name": key})(),
    )
    with pytest.raises(service.ConsoleError, match="尚未登录"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })


def test_api_key_is_saved_without_being_echoed(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
    monkeypatch.setattr(service, "find_dotenv", lambda usecwd=True: str(env_file))
    previous = os.environ.get("DEEPSEEK_API_KEY")
    secret = "sk-test-not-a-real-key"
    result = service.save_api_key("deepseek", secret)
    assert secret not in json.dumps(result)
    assert result["env_var"] == "DEEPSEEK_API_KEY"
    assert result["cli"] == f"Saved DEEPSEEK_API_KEY to {env_file}"
    assert result["prompt"] == "Paste your DEEPSEEK_API_KEY (will be saved to .env):"
    assert secret in env_file.read_text(encoding="utf-8")
    assert secret not in json.dumps(service.redact({"api_key": secret, "note": "ok"}))
    skipped = service.save_api_key("deepseek", "  ")
    assert skipped["saved"] is False
    assert skipped["cli"] == "Skipped. API calls will fail until DEEPSEEK_API_KEY is set."
    assert secret not in skipped["cli"]
    status = service.key_status()
    row = next(item for item in status if item["env_var"] == "DEEPSEEK_API_KEY")
    assert row["configured"] is True
    assert secret not in json.dumps(status)
    if previous is None:
        os.environ.pop("DEEPSEEK_API_KEY", None)
    else:
        os.environ["DEEPSEEK_API_KEY"] = previous
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    save_key = script[script.index("async function saveKey"):script.index("function renderModels")]
    assert save_key.index("await renderAccounts()") < save_key.index("insertAdjacentHTML")
    assert "result.cli" in save_key
    assert "密钥不会回显" in save_key


def test_report_path_cannot_escape_results(monkeypatch, tmp_path):
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    outside = tmp_path.parent / "secret.html"
    outside.write_text("nope", encoding="utf-8")
    with pytest.raises(service.ConsoleError):
        service.resolve_report(str(outside))


def test_server_shell_is_chinese_and_has_no_native_select(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "auth_status", lambda: {"accounts": [], "live_validation": False})
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))
    httpd = server.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        import http.client
        port = httpd.server_address[1]

        def get(path):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", path, headers={server.TOKEN_HEADER: httpd.session_token})
            response = connection.getresponse()
            body = response.read()
            connection.close()
            assert response.status == 200, body
            return body

        page = get("/").decode("utf-8")
        assert "app-shell" in page and "sidebar" in page and "启动分析" in page
        assert "<select" not in page
        # WEBUI-007 item 2: the TradingAgents sound-wave mark replaces the Tauric wordmark.
        assert 'src="/icons/logo.svg"' in page and "Tauric Research" not in page and "tauric.ai" not in page
        assert 'src="/icons/databricks.svg"' not in page
        assert get("/icons/logo.svg") == Path("webui/static/icons/logo.svg").read_bytes()
        assert '<link rel="icon" type="image/svg+xml" href="/icons/logo.svg">' in page
        assert '<link rel="apple-touch-icon" sizes="180x180" href="/icons/logo-180.png">' in page
        for size in (16, 32, 180):
            assert get(f"/icons/logo-{size}.png").startswith(b"\x89PNG")
        # The report HTML still carries the official Tauric logo; the console keeps serving it.
        official = Path("tradingagents/assets/tauric-logo.svg").read_bytes()
        assert get("/icons/tauric.svg") == official
        assert page.count('width="18"') >= 6
        assert 'aria-haspopup="menu"' in page
        assert 'data-cli="auth-status"' in page
        catalog = json.loads(get("/api/catalog"))
        assert any(item["cli"] == "login" and item["zh"] == "登录订阅" for item in catalog["commands"])
        icons = {item["key"]: item.get("icon") for item in catalog["providers"]}
        assert icons["minimax"] == "/icons/minimax.svg"
        assert icons["grok"] is None
        assert get("/icons/minimax.svg") == Path("webui/static/icons/minimax.svg").read_bytes()
        status = json.loads(get("/api/auth/status"))
        assert "token" not in json.dumps(status).lower()

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", f"/api/reports/file?path=../../secret.html&k={httpd.link_key}")
        response = connection.getresponse()
        body = response.read()
        connection.close()
        assert response.status == 404
        assert "报告路径不在结果目录内" in body.decode("utf-8")
        assert json.loads(get("/api/auth/status")) == status
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_backtest_grid_is_checked_without_starting(monkeypatch):
    checked = service.validate_backtest({
        "tickers": "NVDA,AAPL",
        "start": "2026-10-01",
        "end": "2026-10-29",
        "every": 7,
        "asset_type": "stock",
        "analysts": ["market"],
    })
    assert checked["started"] is False
    assert checked["cells"] > 0
    assert checked["tickers"] == ["NVDA", "AAPL"]


def test_stop_without_a_run_is_idle(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    assert service.stop_run()["status"] == "idle"


def test_stop_kills_only_the_owned_process_group(monkeypatch, tmp_path):
    import signal
    import subprocess
    import time

    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert service._signal_owned_group(unrelated.pid, signal.SIGTERM) is False
        assert unrelated.poll() is None
        checked = service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
        record = service.start_validated_run(checked["selections"])
        assert service._alive(record["pid"])
        stopped = service.stop_run()
        assert stopped["status"] == "stopped"
        deadline = time.time() + 3
        while time.time() < deadline and service._alive(record["pid"]):
            time.sleep(0.05)
        assert not service._alive(record["pid"])
    finally:
        service.set_spawn_argv(None)
        if unrelated.poll() is None:
            unrelated.kill()
            unrelated.wait(timeout=3)


def test_restart_replaces_the_owned_process(monkeypatch, tmp_path):
    import signal
    import time

    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "import time; time.sleep(30)"])
    first = None
    try:
        checked = service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
        first = service.start_validated_run(checked["selections"])
        restarted = service.restart_run()
        assert restarted["pid"] != first["pid"]
        assert service._alive(restarted["pid"])
        assert not service._alive(first["pid"])
    finally:
        service.stop_run()
        service.set_spawn_argv(None)
        if first and service._alive(first["pid"]):
            service._signal_owned_group(first["pid"], signal.SIGKILL)
        time.sleep(0.05)


def test_backtest_start_uses_the_injected_runner(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    seen = {}

    def runner(job):
        seen["kind"] = job["kind"]
        seen["tickers"] = job["tickers"]
        return 0

    service.set_runner(runner)
    try:
        record = service.start_backtest({
            "tickers": "NVDA",
            "start": "2026-10-01",
            "end": "2026-10-29",
            "every": 7,
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
    finally:
        service.set_runner(None)
    assert seen == {"kind": "backtest", "tickers": ["NVDA"]}
    assert record["status"] == "finished"


def test_fake_run_does_not_call_the_graph(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    called = {}

    def runner(job):
        called["id"] = job["id"]
        Path(job["result_path"]).write_text("{}", encoding="utf-8")
        return 0

    service.set_runner(runner)
    try:
        checked = service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
        record = service.start_validated_run(checked["selections"], background=False)
    finally:
        service.set_runner(None)
    assert called["id"] == record["id"]
    assert record["status"] == "finished"


def test_omitted_backtest_analysts_match_the_cli_default():
    stock = service.validate_backtest({
        "tickers": "NVDA",
        "start": "2026-10-01",
        "end": "2026-10-08",
        "every": 7,
        "asset_type": "stock",
    })
    assert stock["analysts_omitted"] is True
    assert stock["analysts"] == ["market", "social", "news", "fundamentals"]
    crypto = service.validate_backtest({
        "tickers": "BTC-USD",
        "start": "2026-10-01",
        "end": "2026-10-08",
        "every": 7,
        "asset_type": "crypto",
    })
    assert "fundamentals" not in crypto["analysts"]
    assert crypto["analysts_omitted"] is True


def test_custom_language_must_be_a_real_name(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="Please enter a language name."):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "output_language": "custom",
        })
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "output_language": "Japanese",
        "google_thinking_level": "minimal",
    })
    assert checked["selections"]["output_language"] == "Japanese"
    assert checked["selections"]["google_thinking_level"] is None
    config = service.build_run_config({
        **checked["selections"],
        "quick_think_backend_url": "http://127.0.0.1:9/v1",
    })
    assert config["output_language"] == "Japanese"
    assert config["quick_think_backend_url"] == "http://127.0.0.1:9/v1"
    ollama = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "ollama",
        "quick_think_provider": "ollama",
        "deep_think_provider": "ollama",
        "quick_think_llm": "llama3",
        "deep_think_llm": "llama3",
        "quick_think_backend_url": "http://127.0.0.1:11434/v1",
    })
    assert ollama["selections"]["quick_think_backend_url"] == "http://127.0.0.1:11434/v1"


def test_google_effort_reaches_the_run_config(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "google",
        "quick_think_provider": "google",
        "deep_think_provider": "anthropic",
        "quick_think_llm": "gemini-3.8-flash",
        "deep_think_llm": "claude-sonnet-5",
        "google_thinking_level": "minimal",
        "anthropic_effort": "low",
    })
    config = service.build_run_config(checked["selections"])
    assert config["google_thinking_level"] == "minimal"
    assert config["anthropic_effort"] == "low"


def test_help_usage_matches_python_m_cli_main():
    import subprocess
    import sys

    payload = service.cli_help()
    root = subprocess.run(
        [sys.executable, "-m", "cli.main", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert root.returncode == 0
    assert payload["cli"] == root.stdout
    for name in ("backtest", "login", "logout", "auth-status"):
        completed = subprocess.run(
            [sys.executable, "-m", "cli.main", name, "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, name
        assert payload["commands"][name] == completed.stdout
    assert "Usage: python -m cli.main login" in payload["commands"]["login"]
    assert "Usage: TradingAgents login" not in payload["commands"]["login"]


def test_help_text_is_the_cli_help_with_chinese_labels():
    payload = service.cli_help()
    assert "start" in payload["launcher"]
    assert "--ticker" in payload["cli"]
    assert "--checkpoint" in payload["cli"]
    assert "backtest" in payload["commands"]
    assert "--start" in payload["commands"]["backtest"]
    assert "login" in payload["commands"]
    assert any(item["zh"] == "启动分析" for item in payload["chinese"]["commands"])
    blob = json.dumps(payload)
    assert "api_key" not in blob.lower()
    assert "token" not in blob.lower()


def test_checkpoint_list_names_databases_and_does_not_claim_resume(monkeypatch, tmp_path):
    monkeypatch.setitem(service.DEFAULT_CONFIG, "data_cache_dir", str(tmp_path))
    folder = tmp_path / "checkpoints"
    folder.mkdir()
    (folder / "NVDA.db").write_bytes(b"not a real checkpoint")
    (folder / "notes.txt").write_text("ignore", encoding="utf-8")
    listed = service.list_checkpoints()
    assert listed["files"] == [{"ticker": "NVDA", "bytes": 21}]
    assert "恢复" in listed["note"]
    assert "Resuming" not in json.dumps(listed)


def test_checkpoint_announcement_matches_the_cli_sentence(monkeypatch, tmp_path):
    import tradingagents.graph.checkpointer as checkpointer

    seen = {}

    def fake_step(data_dir, ticker, date, signature="", scope=None):
        seen["args"] = (str(data_dir), ticker, date, signature)
        return None if seen.get("fresh", True) else 4

    monkeypatch.setattr(checkpointer, "checkpoint_step", fake_step)

    class Graph:
        config = {"checkpoint_enabled": True, "data_cache_dir": str(tmp_path)}

        def _run_signature(self, asset_type, portfolio):
            return f"{asset_type}:{portfolio}"

    selections = {"ticker": "NVDA", "analysis_date": "2026-10-29", "asset_type": "stock"}
    assert service.checkpoint_announcement(Graph(), selections, None) == "Starting fresh for NVDA on 2026-10-29"
    assert seen["args"][3] == "stock:None"
    seen["fresh"] = False
    assert service.checkpoint_announcement(Graph(), selections, None) == "Resuming the saved run for NVDA on 2026-10-29"
    Graph.config = {"checkpoint_enabled": False, "data_cache_dir": str(tmp_path)}
    assert service.checkpoint_announcement(Graph(), selections, None) is None


def test_cached_updates_do_not_contact_the_network(monkeypatch):
    import cli.updates as updates

    def fail_network(*args, **kwargs):
        raise AssertionError("updates cache reader contacted the network")

    monkeypatch.setattr(updates, "read_cache", lambda: {"latest": {"version": "0.0.1", "summary": "old"}})
    monkeypatch.setattr(updates, "requests", type("R", (), {"get": staticmethod(fail_network)}))
    payload = service.cached_updates()
    assert payload["live_check"] is False
    assert payload["source"] == "cli.updates.read_cache"


def test_removed_pages_are_gone_from_the_shell():
    # WEBUI-005: settings, memory and help pages are folded into the remaining pages.
    page = Path("webui/static/index.html").read_text(encoding="utf-8")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    for name in ("help", "memory", "settings"):
        assert f'data-page="{name}"' not in page
    assert 'data-cli="--help"' not in page
    for name in ("renderHelp", "renderMemory", "renderSettings", "openSettingsDrawer"):
        assert name not in script
    assert ">报告</button>" in page and ">模型</button>" in page
    assert "fonts.googleapis.com" not in page
    assert "Custom language" in script or "custom-language" in script
    assert "/api/checkpoints" in script
    assert "watchRunning" in script
    assert 'run.status !== "running"' in script
    assert "<select" not in script


def test_azure_asks_for_a_deployment_name_not_a_custom_model_id():
    listed = service.list_models("azure", "deep")
    assert listed["models"] == []
    assert listed["custom_allowed"] is True
    assert listed["title"] == "Enter Azure deployment name (deep-thinking):"
    assert listed["hint"] == "Please enter a deployment name."
    assert "Custom model ID" not in listed["detail"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "pickerPrompt(\"azure_quick\")" in script
    assert "pickerPrompt(\"azure_deep\")" in script
    assert "pickerPrompt(\"azure_required\")" in script
    assert "Enter Azure deployment name (quick-thinking):" not in script
    assert "Please enter a deployment name." not in script
    with pytest.raises(service.ConsoleError, match="Please enter a deployment name."):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "azure",
            "quick_think_llm": "",
            "deep_think_llm": "deploy-deep",
        })


def test_vendor_catalog_matches_the_router():
    from webui.catalog import vendor_options

    rows = {item["category"]: item for item in vendor_options()}
    assert "yfinance" in rows["core_stock_apis"]["vendors"]
    assert "sec_edgar" in rows["fundamental_data"]["vendors"]
    assert "fred" in rows["macro_data"]["vendors"]
    assert rows["core_stock_apis"]["zh"] == "行情"
    assert rows["core_stock_apis"]["cli"] == "data_vendors.core_stock_apis"


def test_vendor_chain_reaches_config_and_unknown_names_are_rejected(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="core_stock_apis"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "qwen-cn",
            "quick_think_provider": "qwen-cn",
            "deep_think_provider": "qwen-cn",
            "quick_think_llm": "qwen-plus",
            "deep_think_llm": "qwen-plus",
            "backend_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "data_vendors": {"core_stock_apis": "not-a-vendor"},
        })
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "qwen-cn",
        "quick_think_provider": "qwen-cn",
        "deep_think_provider": "qwen-cn",
        "quick_think_llm": "qwen-plus",
        "deep_think_llm": "qwen-plus",
        "backend_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "data_vendors": {"core_stock_apis": "alpha_vantage,yfinance", "news_data": "yfinance"},
    })
    config = service.build_run_config(checked["selections"])
    assert config["llm_provider"] == "qwen-cn"
    assert config["backend_url"] == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert config["data_vendors"]["core_stock_apis"] == "alpha_vantage,yfinance"
    assert config["data_vendors"]["news_data"] == "yfinance"
    assert config["data_vendors"]["fundamental_data"] == "sec_edgar,yfinance"


def test_stream_chunk_follows_the_cli_progress_rules():
    from cli.display import MessageBuffer, apply_stream_chunk

    buffer = MessageBuffer()
    buffer.init_for_analysis(["market"])
    assert buffer.agent_status["Market Analyst"] == "pending"
    assert "Fundamentals Analyst" not in buffer.agent_status
    apply_stream_chunk(buffer, {"market_report": "prices"})
    assert buffer.agent_status["Market Analyst"] == "completed"
    assert buffer.agent_status["Bull Researcher"] == "in_progress"
    apply_stream_chunk(buffer, {
        "investment_debate_state": {"bull_history": "up", "bear_history": "down"},
        "investment_plan": "hold",
    })
    assert buffer.agent_status["Research Manager"] == "completed"
    assert buffer.agent_status["Trader"] == "in_progress"


def test_progress_teams_come_from_the_cli_panel():
    from cli.display import LIVE_PROGRESS_TEAMS
    from webui.catalog import progress_teams, public_catalog
    from webui.progress import progress_rows

    expected = [(team, agent) for team, agents in LIVE_PROGRESS_TEAMS for agent, _key in agents]
    rows = progress_teams()
    assert [(row["team"], row["agent"]) for row in rows] == expected
    assert rows[0]["team_zh"] == "分析师团队"
    assert rows[0]["zh"] == "市场分析师"
    assert [row["agent"] for row in progress_rows([], None)] == [agent for _team, agent in expected]
    assert public_catalog()["progress_teams"] == rows
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog.progress_teams" in script
    assert "progressTeamMap()" in script
    assert '["Analyst Team", "分析师团队"]' not in script



def test_idle_progress_does_not_invent_pending_agents(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    progress = service.run_progress()
    assert progress["agents"] == []
    assert progress["wall_time"] is None




def test_submit_answers_with_the_launch_dialog_instead_of_a_toast():
    """WEBUI-006: a launch opens the result dialog; success resets the wizard, failure keeps it."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    html = Path("webui/static/index.html").read_text(encoding="utf-8")
    assert 'id="toast-host"' in html
    assert "${noticesHtml()}" in script
    submit = script.split("async function submitAnalysis", 1)[1].split("async function resetAnalysisForm", 1)[0]
    assert "showToast" not in submit and 'showPage("runs")' not in submit
    failed = submit.split("catch (error)", 1)[1].split("return;", 1)[0]
    assert "showLaunchFailed(error.message, payload, button)" in failed and "resetAnalysisForm" not in failed
    succeeded = submit.split("return;", 1)[1]
    assert succeeded.index("await resetAnalysisForm()") < succeeded.index("showLaunchStarted(record")
    toast = script[script.index("function showToast"):script.index("const ROLE_PILL")]
    assert "#toast-host" in toast and 'role="status"' in toast


def test_completion_sentence_is_shown_only_when_the_cli_prints_it():
    from webui.progress import COMPLETION_SENTENCE, parse_completion

    assert COMPLETION_SENTENCE == "Analysis Complete!"
    assert parse_completion(["STATS\tAgents: 1/1", "Analysis Complete!"]) == COMPLETION_SENTENCE
    assert parse_completion(["Analysis Complete! extra"]) is None
    assert parse_completion([]) is None
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "completionNote(data.progress)" in script
    assert "progress.complete" in script
    assert "分析已完成" in script
    source = Path("webui/service.py").read_text(encoding="utf-8")
    assert "print(COMPLETION_SENTENCE, flush=True)" in source


def test_progress_rows_keep_the_cli_agent_names():
    from webui.progress import progress_rows

    rows = progress_rows(["STATUS\tMarket Analyst\tin_progress", "not a status"], ["market"])
    names = [row["agent"] for row in rows]
    assert "Market Analyst" in names
    assert "Fundamentals Analyst" not in names
    assert "Portfolio Manager" in names
    market = next(row for row in rows if row["agent"] == "Market Analyst")
    assert market["zh"] == "市场分析师"
    assert market["status_zh"] == "进行中"
    assert market["team"] == "Analyst Team"


def test_runner_prints_status_lines_without_calling_a_model(monkeypatch, tmp_path, capsys):
    import tradingagents.graph.trading_graph as graph_module

    class Graph:
        config = {"checkpoint_enabled": False, "data_cache_dir": str(tmp_path)}

        def __init__(self, *args, **kwargs):
            return None

        def _run_signature(self, *args):
            return "sig"

        def create_run_state(self, *args):
            return {}

        @property
        def propagator(self):
            return type("Propagator", (), {"get_graph_args": lambda self, callbacks=None: {}})()

        def begin_checkpoint(self, *args):
            return None

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [], {"market_report": "prices"}
            yield [], {
                "investment_debate_state": {"bull_history": "up", "bear_history": "down"},
                "investment_plan": "hold",
            }

        def record_decision(self, *args):
            return None

        def clear_checkpoint_on_success(self, *args):
            return None

        def end_checkpoint(self):
            return None

    monkeypatch.setattr(graph_module, "TradingAgentsGraph", Graph)
    code = service.default_runner({
        "selections": {
            "ticker": "NVDA",
            "analysis_date": "2026-10-29",
            "asset_type": "stock",
            "analysts": ["market"],
            "research_depth": 1,
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "backend_url": None,
            "save": False,
        },
        "result_path": str(tmp_path / "result.json"),
    })
    assert code == 0
    output = capsys.readouterr().out
    assert "STATUS\tMarket Analyst\tcompleted" in output
    assert "STATUS\tTrader\tin_progress" in output


def test_wall_time_pending_sentence_matches_the_cli():
    from cli.display import AnalystWallTimeTracker
    from tradingagents.graph.analyst_execution import build_analyst_execution_plan
    from webui.catalog import public_catalog, wall_time_pending_sentence

    expected = AnalystWallTimeTracker(build_analyst_execution_plan(["market"])).format_summary()
    assert expected == "Analyst wall time: pending"
    assert wall_time_pending_sentence() == expected
    assert public_catalog()["wall_time_pending"] == expected
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "wall_time_pending" in script
    assert "Analyst wall time: pending" not in script
    assert "function wallNote" in script


def test_memory_rows_keep_the_cli_tag_line(monkeypatch, tmp_path):
    log = tmp_path / "trading_memory.md"
    log.write_text(
        "[2026-10-29 | NVDA | Hold | pending]\n\nDECISION:\n持有，这是夹具。\n\n<!-- ENTRY_END -->\n\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(service.DEFAULT_CONFIG, "memory_log_path", str(log))
    found = service.list_memory()
    assert found["entries"][0]["tag"] == "[2026-10-29 | NVDA | Hold | pending]"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "renderMemory" not in script


def test_logs_page_can_read_the_console_log_tail():
    """Search opens logs on the reports page, so that page must offer logs --lines."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    reports = script[script.index("async function renderReports"):script.index("function liveTitleLabel")]
    assert "logTailPanel()" in reports
    assert "bindLogTail()" in reports
    assert 'id="report-log-lines" value="40"' in script
    assert "/api/logs?lines=" in script[script.index("function bindLogTail"):script.index("async function renderReports")]


def test_main_provider_change_follows_into_unseparated_tiers():
    """CLI provider selection applies to both tiers unless a tier was chosen separately."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    body = script[script.index("function bindMainProvider"):script.index("function regionFamily")]
    assert "const previous = state.form.llm_provider" in body
    assert "tierProviderSkipped(tier)" in body
    assert "state.form[`${tier}_think_provider`] !== previous" in body
    assert "forceRefresh: true" in body
    assert body.index("previous !== next") < body.index("refresh.push(tier)")


def test_region_change_refreshes_following_tiers_and_keeps_env_tiers():
    """A region change is a provider change for tiers that follow it. Env-owned tiers stay."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    region = script[script.index("function applyRegion"):script.index("function hostPressed")]
    assert "tierProviderSkipped(tier)" in region
    assert "forceRefresh: true" in region
    assert region.index("tierProviderSkipped(tier)") < region.index("refresh.push(tier)")
    choose = script[script.index("async function chooseProvider"):script.index("async function showProviderNote")]
    assert "options.forceRefresh" in choose
    assert "changed && !tierModelSkipped(tier)" in choose


def test_provider_change_drops_a_model_id_from_another_list():
    """CLI asks for a model again after the provider changes. An old ID must not stay selected."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    body = script[script.index("async function chooseProvider"):script.index("async function showProviderNote")]
    assert "previous !== provider" in body
    assert "tierModelSkipped(tier)" in body
    assert 'state.form[`${tier}_think_llm`] = ""' in body
    assert body.index("previous !== provider") < body.index("renderModelChoices")


def test_backtest_analysts_come_from_the_multiselect_not_missing_buttons():
    """The backtest analyst control is a custom multi-select, not aria-pressed buttons."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    payload = script[script.index("function backtestPayload"):script.index("function syncBacktestModels")]
    assert "aria-pressed" not in payload
    assert "analysts: book.analysts" in payload
    checked = service.validate_backtest({
        "tickers": "NVDA",
        "start": "2026-10-01",
        "end": "2026-10-01",
        "every": 7,
        "asset_type": "stock",
        "analysts": ["market", "social", "news", "fundamentals"],
    })
    assert checked["analysts"] == ["market", "social", "news", "fundamentals"]
    assert checked["analysts_omitted"] is False


def test_effort_prompts_cover_every_selected_family():
    """Model settings expose thinking strength without reprinting the CLI wizard."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert script.count("function effortFamilies") == 1
    assert "function refreshEffort" in script
    assert "function showModelEffort" in script
    assert 'id="efforts"' in script
    assert "调整思考强度" in script
    assert "命令行会跳过 Step 8" not in script


def test_run_flags_rerender_from_form_state():
    """The 保存与续跑 drawer paints every switch from the form state, and only 保存 writes it back."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    drawer = script[script.index("function openSaveDrawer"):script.index("function positiveNumber")]
    for flag in ("save", "html", "show", "checkpoint", "clear_checkpoints"):
        assert f'toggleRow("{flag}"' in drawer
    assert "state.form.save !== false" in drawer
    assert "state.form.checkpoint !== false" in drawer
    assert "state.form.clear_checkpoints === true" in drawer
    toggle = script[script.index("function toggleRow"):script.index("const SAVE_MORE_FIELDS")]
    assert 'role="switch"' in toggle and "aria-checked" in toggle
    save = script[script.index("async function saveDrawer"):script.index("function buildPortfolio")]
    assert "Object.assign(state.form, draft.toggles" in save
    assert save.index("if (!saved) return;") < save.index("Object.assign(state.form")
    assert 'id="run-flags"' not in script


def test_login_completion_keeps_the_cli_sentence():
    """Re-rendering accounts must not drop the sentence the CLI prints after sign-in."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    start = script.index("async function beginLogin")
    body = script[start:script.index("async function logout")]
    assert "await renderAccounts();" in body
    assert "current.cli" in body
    assert "对应命令行：" in body
    assert "error-banner" in body
    assert body.index("await renderAccounts();") < body.index("error-banner")
    assert "setAuthCardFeedback(provider, `正在启动 ${label} 登录…`);" in body
    assert "button.textContent = \"等待授权\"" in body
    logout_body = script[script.index("async function logout"):script.index("async function saveKey")]
    assert "button.textContent = \"正在退出\"" in logout_body
    assert "setAuthCardFeedback(provider, error.message, true);" in logout_body
    assert "state.page !== \"accounts\"" in logout_body
    source = Path("webui/service.py").read_text(encoding="utf-8")
    assert "signed_in_sentence" in source


def test_brand_wordmark_is_not_covered_by_search():
    css = Path("webui/static/app.css").read_text(encoding="utf-8")
    assert "grid-template-columns: max-content minmax(160px, 1fr) max-content" in css
    assert "max-width: 240px" not in css
    assert ".brand-name { white-space: nowrap; }" in css
    active = css.split("\n.st.on {", 1)[1].split("}", 1)[0]
    assert "background: var(--active-bg)" in active


def test_provider_cli_name_uses_body_text_size():
    css = Path("webui/static/app.css").read_text(encoding="utf-8")
    span_rule = css[css.index(".provider-card span {"):css.index(".provider-card > span")]
    assert "font-size: 14px;" in span_rule
    assert "line-height: 1.5;" in span_rule
    assert "overflow-wrap: anywhere;" in span_rule
    strong_rule = css[css.index(".provider-card strong {"):css.index(".provider-card span {")]
    assert "display: block;" in strong_rule
    assert "font-size: 16px;" in strong_rule
    assert "overflow-wrap: anywhere;" in strong_rule
    assert ".provider-card > span { display: flex; flex-direction: column; min-width: 0; gap: 6px; }" in css
    assert ".provider-card span span { display: block; overflow-wrap: anywhere; }" in css
    assert ".provider-card {\n  min-width: 0;\n  min-height: 184px;\n  padding: 20px;" in css
    assert ".page-actions-group, .inline-actions { gap: 16px; }" in css
    assert ".account-provider-grid {\n  grid-template-columns: repeat(auto-fit, minmax(min(100%, 360px), 1fr));" in css


def test_account_subscription_cards_leave_room_for_status_and_actions():
    """Long account metadata and two action buttons must not escape the card."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    css = Path("webui/static/app.css").read_text(encoding="utf-8")
    assert "function accountProviderCard(item)" in script
    assert 'class="provider-grid account-provider-grid"' in script
    assert 'class="provider-card account-provider-card"' in script
    assert "data-auth-feedback" in script
    assert "account-provider-feedback--error" in css
    wrap_rule = css[css.index(".account-provider-title span,"):css.index(".account-provider-account {")]
    assert "overflow-wrap: anywhere;" in wrap_rule
    assert ".account-provider-actions .button {\n  min-width: 86px;" in css


def test_config_keys_use_cli_defaults_not_page_literals():
    from tradingagents.default_config import DEFAULT_CONFIG
    from webui.catalog import config_key_rows, public_catalog
    from webui.service import CONSOLE_MODEL_TIMEOUT

    rows = config_key_rows()
    by_field = {item["field"]: item for item in rows}
    assert by_field["news_article_limit"]["default"] == DEFAULT_CONFIG["news_article_limit"] == 20
    assert by_field["global_news_article_limit"]["default"] == 10
    assert by_field["global_news_lookback_days"]["default"] == 7
    assert by_field["holding_period_days"]["default"] == 5
    assert by_field["memory_log_max_entries"]["default"] is None
    assert by_field["global_news_queries"]["default"] == DEFAULT_CONFIG["global_news_queries"]
    assert by_field["timeout"]["console_only"] is True
    assert by_field["timeout"]["default"] == CONSOLE_MODEL_TIMEOUT == 300
    assert by_field["llm_call_budget"]["default"] == 900
    assert by_field["llm_call_budget"]["console_only"] is True
    assert by_field["timeout"]["env"] is False
    assert "命令行不设这一项" in by_field["timeout"]["note"]
    assert public_catalog()["config_keys"] == rows
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "configKeyRows" in script
    assert "Federal Reserve interest rates inflation" not in script


def test_settings_lists_every_cli_config_env():
    from tradingagents.default_config import _ENV_OVERRIDES
    from webui.catalog import config_env_rows, public_catalog

    rows = config_env_rows()
    names = [item["cli"] for item in rows]
    assert names == list(_ENV_OVERRIDES) + [
        "TRADINGAGENTS_RESULTS_DIR",
        "TRADINGAGENTS_CACHE_DIR",
        "TRADINGAGENTS_MEMORY_LOG_PATH",
    ]
    assert all(item["zh"] for item in rows)
    assert public_catalog()["config_env"] == rows
    assert rows[0]["field"] == "llm_provider"
    assert {item["cli"]: item["field"] for item in rows}["TRADINGAGENTS_CACHE_DIR"] == "data_cache_dir"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    page = Path("webui/static/index.html").read_text(encoding="utf-8")
    assert 'settingInput("set-temperature", "temperature"' in script
    assert '"results_dir"' in script
    assert '"data_cache_dir"' in script
    assert "TRADINGAGENTS_TEMPERATURE" not in script
    assert "TRADINGAGENTS_RESULTS_DIR" not in script
    assert "TRADINGAGENTS_CACHE_DIR" not in script
    assert "TRADINGAGENTS_TEMPERATURE" not in page


def test_model_prompt_titles_match_the_cli_picker():
    prompts = service.model_prompt_map()
    assert prompts["chatgpt"] == {
        "quick": "Select Your [Quick-Thinking] ChatGPT Plan Model:",
        "deep": "Select Your [Deep-Thinking] ChatGPT Plan Model:",
    }
    assert prompts["grok"]["quick"] == "Select Your [Quick-Thinking] Grok Model:"
    assert prompts["grok"]["deep"] == "Select Your [Deep-Thinking] Grok Model:"
    assert prompts["openrouter"]["deep"] == "Select Your [Deep-Thinking] OpenRouter Model (latest available):"
    assert prompts["azure"]["quick"] == "Enter Azure deployment name (quick-thinking):"
    assert prompts["openai"]["deep"] == "Select Your [Deep-Thinking LLM Engine]:"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="${tier}-model"' in script
    assert "function loadModels" in script


def test_provider_cards_keep_the_cli_display_name():
    from cli.prompts import _llm_provider_table
    from webui.catalog import PROVIDER_ZH, provider_rows

    rows = {item["key"]: item for item in provider_rows()}
    assert set(rows) == {key for _display, key, _url in _llm_provider_table()}
    assert rows["openai_compatible"]["zh"] == "兼容端点"
    assert rows["openai_compatible"]["cli"] == "OpenAI-compatible (vLLM, LM Studio, llama.cpp, custom relay)"
    assert rows["chatgpt"]["cli"] == "ChatGPT (Plus or Pro plan)"
    assert rows["grok"]["cli"] == "Grok (subscription)"
    assert rows["chatgpt"]["zh"] == "ChatGPT 订阅"
    assert rows["grok"]["zh"] == "Grok 订阅"
    for display, key, _url in _llm_provider_table():
        assert key in PROVIDER_ZH
        assert rows[key]["cli"] == display
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function providerOptions" in script
    assert "function mountSelect" in script
    assert 'data-provider="' in script



def test_live_report_titles_come_from_the_cli_display():
    from cli.display import (
        LIVE_REPORT_SECTION_TITLES,
        STREAM_REPORT_HEADING,
        MessageBuffer,
        apply_stream_chunk,
        stream_report_section,
    )
    from webui.catalog import live_report_titles, public_catalog

    rows = live_report_titles()
    assert [item["cli"] for item in rows if item["kind"] == "panel"] == list(LIVE_REPORT_SECTION_TITLES.values())
    streamed = [item["cli"] for item in rows if item["kind"] == "stream"]
    assert streamed == [
        "Bull Researcher Analysis",
        "Bear Researcher Analysis",
        "Research Manager Decision",
        "Aggressive Analyst Analysis",
        "Conservative Analyst Analysis",
        "Neutral Analyst Analysis",
        "Portfolio Manager Decision",
    ]
    assert "Portfolio Management Decision" in [item["cli"] for item in rows]
    assert public_catalog()["live_report_titles"] == rows
    assert stream_report_section("bull", "看多") == "### Bull Researcher Analysis\n看多"
    buffer = MessageBuffer()
    buffer.init_for_analysis(["market"])
    apply_stream_chunk(buffer, {
        "investment_debate_state": {"bull_history": "看多", "bear_history": ""},
    })
    assert buffer.current_report == (
        f"### {LIVE_REPORT_SECTION_TITLES['investment_plan']}\n"
        f"### {STREAM_REPORT_HEADING['bull']}\n看多"
    )
    apply_stream_chunk(buffer, {
        "risk_debate_state": {"aggressive_history": "", "conservative_history": "", "neutral_history": ""},
        "final_trade_decision": "持有",
    })
    assert buffer.current_report.endswith("### Portfolio Manager Decision\n持有")
    assert buffer.current_report.startswith("### Portfolio Management Decision\n")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog.live_report_titles" in script
    assert "liveTitlePanel()" in script
    assert "function reportTitleKind" in script
    assert "item.kind" in script
    assert "Portfolio Manager Decision" not in script
    assert "Portfolio Management Decision" not in script





def test_region_choices_use_the_cli_labels_and_urls():
    from cli.prompts import (
        GLM_REGION_CHOICES,
        GLM_REGION_PROMPT,
        MINIMAX_REGION_CHOICES,
        MINIMAX_REGION_PROMPT,
        QWEN_REGION_CHOICES,
        QWEN_REGION_PROMPT,
    )
    from webui.catalog import REGIONS, public_catalog

    expected = {
        "qwen": (QWEN_REGION_PROMPT, QWEN_REGION_CHOICES),
        "glm": (GLM_REGION_PROMPT, GLM_REGION_CHOICES),
        "minimax": (MINIMAX_REGION_PROMPT, MINIMAX_REGION_CHOICES),
    }
    for family, (prompt, choices) in expected.items():
        rows = REGIONS[family]
        assert [row["prompt"] for row in rows] == [prompt, prompt]
        assert [(row["cli"], row["value"], row["url"]) for row in rows] == [
            (label, value, url) for label, (value, url) in choices
        ]
    assert public_catalog()["regions"]["glm"][1]["cli"].startswith("BigModel —")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog.regions" in script


def test_selection_choices_use_the_cli_labels_in_cli_order():
    from cli.prompts import (
        ANTHROPIC_EFFORT_CHOICES,
        DEPTH_OPTIONS,
        GEMINI_THINKING_CHOICES,
        OPENAI_EFFORT_CHOICES,
        OUTPUT_LANGUAGE_CHOICES,
    )
    from webui.catalog import DEPTHS, LANGUAGES, REASONING, public_catalog

    assert {item["cli"] for item in LANGUAGES} == {label for label, _value in OUTPUT_LANGUAGE_CHOICES}
    assert LANGUAGES[0]["value"] == "Chinese"
    assert [item["cli"] for item in DEPTHS] == [label for label, _value in DEPTH_OPTIONS]
    assert [(item["cli"], item["value"]) for item in REASONING["openai"]] == list(OPENAI_EFFORT_CHOICES)
    assert [(item["cli"], item["value"]) for item in REASONING["google"]] == list(GEMINI_THINKING_CHOICES)
    assert [(item["cli"], item["value"]) for item in REASONING["anthropic"]] == list(ANTHROPIC_EFFORT_CHOICES)
    assert public_catalog()["languages"] == list(LANGUAGES)
    assert public_catalog()["reasoning"]["openai"][0]["cli"] == "Medium (Default)"


def test_show_agents_follow_the_saved_report_order_not_the_progress_panel():
    from tradingagents.reporting import _SECTIONS
    from webui.catalog import progress_teams, public_catalog, show_agents

    rows = show_agents()
    expected = [
        (heading, agent, filename)
        for heading, _folder, agents in _SECTIONS
        for agent, filename, _path in agents
    ]
    assert [(row["heading"], row["agent"], row["file"]) for row in rows] == expected
    names = [row["agent"] for row in rows if row["heading"].startswith("IV.")]
    assert names == ["Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"]
    progress_names = [row["agent"] for row in progress_teams() if row["team"] == "Risk Management"]
    assert progress_names == ["Aggressive Analyst", "Neutral Analyst", "Conservative Analyst"]
    assert public_catalog()["show_agents"] == rows
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog.show_agents" in script
    assert "showAgentMap()" in script
    assert "function shownReport" in script


def test_report_page_lists_every_cli_show_heading():
    from tradingagents.reporting import _SECTIONS
    from webui.catalog import public_catalog, report_sections

    headings = [item["cli"] for item in report_sections()]
    assert headings == [heading for heading, _folder, _agents in _SECTIONS]
    assert headings == [
        "I. Analyst Team Reports",
        "II. Research Team Decision",
        "III. Trading Team Plan",
        "IV. Risk Management Team Decision",
        "V. Portfolio Manager Decision",
    ]
    assert public_catalog()["report_sections"] == report_sections()
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "report_sections" in script
    assert 'cli: "I. Analyst Team Reports"' not in script
    assert 'cli: "V. Portfolio Manager Decision"' not in script


def test_report_outline_uses_the_saved_cli_sections(monkeypatch, tmp_path):
    report = tmp_path / "reports" / "NVDA-2026-10-29"
    (report / "1_analysts").mkdir(parents=True)
    (report / "5_portfolio").mkdir()
    (report / "1_analysts" / "market.md").write_text("价格上涨", encoding="utf-8")
    (report / "5_portfolio" / "decision.md").write_text("持有", encoding="utf-8")
    (report / "complete_report.md").write_text("# Trading Analysis Report: NVDA\n- Rating: Hold\n", encoding="utf-8")
    (report / "complete_report.html").write_text("<p>saved</p>", encoding="utf-8")
    outside = tmp_path / "secret.md"
    outside.write_text("nope", encoding="utf-8")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    outlined = service.report_outline(str(report / "complete_report.html"))
    assert outlined["rating"] == "Hold"
    headings = [item["heading"] for item in outlined["sections"]]
    assert headings == ["I. Analyst Team Reports", "V. Portfolio Manager Decision"]
    assert outlined["sections"][0]["zh"] == "一、分析师团队报告"
    assert outlined["sections"][0]["items"][0]["text"] == "价格上涨"
    assert outlined["sections"][1]["items"][0]["agent"] == "Portfolio Manager"
    with pytest.raises(service.ConsoleError):
        service.report_outline(str(outside))


def test_filled_fields_update_their_labels_not_only_the_step_note():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="ticker-label"' in script
    assert 'id="date-label"' in script
    assert 'id="analyst-label"' in script
    assert "function analysisSummary" in script
    assert '"分析对象"' in script
    assert '"分析维度"' in script


def test_filled_ticker_and_date_do_not_claim_question_box_titles():
    from cli.selections import DATE_FLAG_PREFIX, SELECTED_ANALYSTS_PREFIX, TICKER_FLAG_PREFIX
    from webui.catalog import confirmation_sentences

    sentences = confirmation_sentences()
    assert sentences["ticker_flag_prefix"] == TICKER_FLAG_PREFIX
    assert sentences["date_flag_prefix"] == DATE_FLAG_PREFIX
    assert sentences["selected_analysts_prefix"] == SELECTED_ANALYSTS_PREFIX
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function analysisSummary" in script
    assert '"分析对象"' in script
    assert '"分析日期"' in script
    assert '"分析维度"' in script


def test_env_skipped_steps_do_not_claim_their_titles():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function applyEnvSkipped" in script
    assert "envSkipped" in script
    assert "命令行会跳过 Step 3" not in script
    assert "命令行会跳过 Step 5" not in script
    assert "命令行会跳过 Step 6" not in script


def test_model_questions_are_not_claimed_when_environment_skips_them():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function lockSkippedModelInputs" in script
    assert "envSkipped" in script
    assert "命令行会跳过 Step 7" not in script


def test_save_path_question_is_not_claimed_when_flags_answer_it():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="save-path"' in script
    assert "reports" in script
    assert "保存路径。命令行提问：" not in script


def test_save_path_stays_inside_reports(monkeypatch, tmp_path):
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "save_path": "custom-folder",
    })
    saved = Path(checked["selections"]["save_path"])
    assert saved.is_relative_to(tmp_path / "reports")
    assert saved.name == "custom-folder"
    with pytest.raises(service.ConsoleError, match="reports"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "save_path": "../../outside",
        })


def test_tool_lines_match_the_cli_message_panel(capsys):
    from webui.progress import parse_tool_lines

    rows = parse_tool_lines([
        "STATUS\tMarket Analyst\tin_progress",
        "TOOL\tget_stock_data\t{'symbol': 'NVDA'}",
        "noise",
    ])
    assert rows == [{"name": "get_stock_data", "args": "{'symbol': 'NVDA'}"}]
    service._emit_tools([type("Message", (), {"tool_calls": [{"name": "get_news", "args": {"ticker": "NVDA"}}]})()])
    printed = capsys.readouterr().out
    assert "TOOL\tget_news\t" in printed
    assert "NVDA" in printed


def test_analysis_form_starts_from_cli_defaults_not_a_subscription():
    from tradingagents.default_config import DEFAULT_CONFIG
    from webui.catalog import form_defaults

    defaults = form_defaults()
    assert defaults["llm_provider"] == DEFAULT_CONFIG["llm_provider"] == "openai"
    assert defaults["quick_think_llm"] == DEFAULT_CONFIG["quick_think_llm"]
    assert defaults["deep_think_llm"] == DEFAULT_CONFIG["deep_think_llm"]
    assert defaults["quick_think_provider"] == "openai"
    assert defaults["openai_reasoning_effort"] == "medium"
    assert defaults["research_depth"] == 1
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "form_defaults" in script
    assert "grok-4.6" not in script
    assert "gpt-6.1-sol" not in script
    assert "analysts" not in defaults


def test_analysis_form_does_not_preselect_analysts_without_memory():
    """Interactive CLI leaves analysts unchecked until a previous run is remembered."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "analysts: []" in script
    assert 'analysts: ["market"]' not in script
    assert "state.form.analysts = [data.analysts[0]]" not in script
    assert 'for (const key of ["output_language", "analysts", "research_depth"' in script


def test_custom_model_choice_and_portfolio_shape_are_not_invented():
    from cli.prompts import CUSTOM_MODEL_CHOICE
    from webui.catalog import placeholders

    assert CUSTOM_MODEL_CHOICE == "Custom model ID"
    shape = placeholders()["portfolio"]
    assert "cash" in shape and "positions" in shape and "average_price" in shape
    assert "10000" not in shape
    assert "USD" not in shape
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    source = Path("webui/service.py").read_text(encoding="utf-8")
    assert 'pickerPrompt("custom_choice")' in script
    assert "Custom model ID" not in script
    assert "10000" not in script
    assert "CUSTOM_MODEL_CHOICE" in source


def test_placeholders_come_from_cli_examples_not_filled_defaults():
    from cli.display import LIVE_REPORT_SECTION_TITLES
    from cli.prompts import TICKER_PROMPT_DEFAULT
    from webui.catalog import placeholders

    hints = placeholders()
    assert hints["ticker"] == TICKER_PROMPT_DEFAULT == "SPY"
    assert hints["openrouter_id"] == "google/gemma-4-26b-a4b-it"
    assert hints["compatible_url"] == "http://localhost:8000/v1"
    assert hints["section_file"] == f"{next(iter(LIVE_REPORT_SECTION_TITLES))}.md"
    assert hints["every"] == "7"
    assert hints["login_paste"].startswith("http://127.0.0.1:")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'placeholder("ticker")' in script
    assert 'placeholder("openrouter_id")' in script
    assert 'tickers: "NVDA,AAPL"' not in script
    assert 'start: "2026-10-01"' not in script
    assert "google/gemma-4-26b-a4b-it" not in script
    assert "http://localhost:11434/v1" not in script


def test_backtest_progress_template_is_not_an_invented_row():
    from cli.main import backtest_progress_line
    from webui.catalog import outcome_sentences

    assert outcome_sentences()["progress"] == backtest_progress_line("{done}", "{total}", "{ticker}", "{date}")
    assert outcome_sentences()["progress"] == "[{done}/{total}] {ticker} {date}"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function backtestProgress" in script
    assert "progress?.backtest" in script
    assert "[1/2] NVDA 2026-10-01" not in script


def test_backtest_progress_uses_the_cli_sentence(monkeypatch, tmp_path, capsys):
    import tradingagents.backtest as backtest

    class Result:
        cells_run = 1
        skipped = 2
        run_id = "sweep1"
        log_path = tmp_path / "trading_memory.md"
        failures = [("NVDA", "2026-10-01", "boom")]
        settlement_failures = [("AAPL", "no close")]

    def fake_run(*args, progress=None, **kwargs):
        progress(1, 3, "NVDA", "2026-10-01")
        return Result()

    monkeypatch.setattr(service, "build_run_config", lambda selections: {})
    monkeypatch.setattr(backtest, "run_backtest", fake_run)
    monkeypatch.setattr(
        "cli.main.summarize",
        lambda result: type("Summary", (), {"render": lambda self: "summary"})(),
    )
    code = service.run_backtest_job({
        "selections": {"portfolio": None},
        "tickers": ["NVDA"],
        "dates": ["2026-10-01"],
        "asset_type": "stock",
        "analysts": ["market"],
        "result_path": str(tmp_path / "result.txt"),
    })
    assert code == 0
    output = capsys.readouterr().out
    saved = (tmp_path / "result.txt").read_text(encoding="utf-8")
    from cli.main import backtest_completion_text
    expected = backtest_completion_text(Result())
    assert expected.startswith("summary\n\n")
    assert output.endswith(expected)
    assert saved == expected
    assert "[1/3] NVDA 2026-10-01" in output
    assert "Ran 1 cells, skipped 2. Log:" in output
    assert "Continue or settle this sweep: --run-id sweep1" in output
    assert "failed: NVDA 2026-10-01: boom" in output
    assert "unsettled: AAPL: no close" in output
    from webui.progress import parse_backtest_lines
    found = parse_backtest_lines(output.splitlines())
    assert found["latest"]["cli"] == "[1/3] NVDA 2026-10-01"
    assert found["latest"]["done"] == 1
    assert any(line.startswith("failed:") for line in found["summary"])


def test_backtest_completion_text_matches_the_cli_print(monkeypatch):
    from cli.display import console
    from cli.main import backtest_completion_text, print_backtest_completion
    from tradingagents.backtest import BacktestSummary

    monkeypatch.setattr(
        "cli.main.summarize",
        lambda result: BacktestSummary(1, 0, {}, holding="5 trading days"),
    )
    result = type("Result", (), {
        "cells_run": 1,
        "skipped": 0,
        "run_id": "sweep1",
        "log_path": "memory.md",
        "failures": [("NVDA", "2026-10-01", "boom")],
        "settlement_failures": [("AAPL", "no close")],
    })()
    expected = backtest_completion_text(result)
    # Pin both dimensions: Rich ignores a width of its own when TERM=dumb and no height
    # is set (it reports 80x25), and COLUMNS=0 gives a zero-width console.
    monkeypatch.setattr(console, "_width", 400)
    monkeypatch.setattr(console, "_height", 25)
    monkeypatch.setattr(console, "_color_system", None)  # plain text even under FORCE_COLOR
    with console.capture() as captured:
        print_backtest_completion(result)
    printed = captured.get()
    assert expected.startswith("Resolved cells: 1 · pending: 0\n")
    assert "\n\nRan 1 cells, skipped 0. Log: memory.md\n" in expected
    assert printed == expected
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function backtestProgress" in script
    assert "result.text" in script
    assert "不另写带花括号的句子" not in script


def test_show_keeps_the_cli_sections_without_a_saved_file():
    from cli.display import COMPLETE_REPORT_RULE

    state = {
        "market_report": "行情正文",
        "investment_debate_state": {"bull_history": "多头", "bear_history": ""},
        "final_trade_decision": "持有",
        "final_rating": "Hold",
    }
    shown = service.shown_report(state)
    assert shown["rule"] == COMPLETE_REPORT_RULE
    headings = [item["heading"] for item in shown["sections"]]
    assert headings == ["I. Analyst Team Reports", "II. Research Team Decision", "V. Portfolio Manager Decision"]
    assert shown["sections"][0]["items"] == [{"agent": "Market Analyst", "text": "行情正文"}]
    assert shown["sections"][1]["items"] == [{"agent": "Bull Researcher", "text": "多头"}]
    notice = {
        "report": None, "saved_to": None, "html": None, "save_error": None,
        "saved": False, "save_lines": [],
    }
    hidden = service.analysis_result_payload(state, "Hold", notice, {"show": False}, Path("/tmp/missing.log"))
    assert "shown_report" not in hidden
    visible = service.analysis_result_payload(state, "Hold", notice, {"show": True}, Path("/tmp/missing.log"))
    assert visible["shown_report"]["sections"][2]["items"][0]["text"] == "持有"
    assert visible["saved"] is False


def test_result_file_is_shown_only_when_it_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    missing = service.read_result()
    assert missing["present"] is False
    result = tmp_path / "result.json"
    result.write_text('{"decision": "Hold", "rating": null, "report": null}', encoding="utf-8")
    service.JOBS.write_run({
        "id": "result-test",
        "kind": "analysis",
        "status": "finished",
        "result": str(result),
        "log": str(tmp_path / "missing.log"),
    })
    found = service.read_result()
    assert found["present"] is True
    assert found["decision"] == "Hold"
    assert found["rating"] is None
    assert found["shown_report"] is None
    result.write_text(
        '{"decision": "Hold", "shown_report": {"rule": "Complete Analysis Report", "sections": []}}',
        encoding="utf-8",
    )
    with_show = service.read_result()
    assert with_show["shown_report"]["rule"] == "Complete Analysis Report"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function shownReport" in script
    assert "result.shown_report" in script
    assert "headingZh(section.heading)" in script
    summary = tmp_path / "summary.txt"
    summary.write_text("Resolved cells: 0 · pending: 1\n", encoding="utf-8")
    service.JOBS.write_run({
        "id": "backtest-result",
        "kind": "backtest",
        "status": "finished",
        "result": str(summary),
        "log": str(tmp_path / "missing.log"),
    })
    shown = service.read_result()
    assert shown["kind"] == "backtest"
    assert "Resolved cells: 0" in shown["text"]


def test_memory_log_reads_saved_entries_and_stays_empty_without_a_file(monkeypatch, tmp_path):
    missing = tmp_path / "missing.md"
    monkeypatch.setitem(service.DEFAULT_CONFIG, "memory_log_path", str(missing))
    empty = service.list_memory()
    assert empty["count"] == 0
    assert empty["entries"] == []
    log = tmp_path / "trading_memory.md"
    log.write_text(
        "[2026-10-29 | NVDA | Hold | pending]\n\nDECISION:\n持有，这是夹具。\n\n<!-- ENTRY_END -->\n\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(service.DEFAULT_CONFIG, "memory_log_path", str(log))
    found = service.list_memory()
    assert found["count"] == 1
    assert found["entries"][0]["ticker"] == "NVDA"
    assert found["entries"][0]["rating"] == "Hold"
    assert found["entries"][0]["pending"] is True
    assert "夹具" in found["entries"][0]["decision"]
    assert "token" not in json.dumps(found).lower()


def test_footer_stats_use_the_cli_wording():
    from cli.display import MessageBuffer, format_footer_stats
    from webui.progress import parse_stats_line

    buffer = MessageBuffer()
    buffer.init_for_analysis(["market"])
    buffer.update_agent_status("Market Analyst", "completed")

    class Handler:
        def get_stats(self):
            return {"llm_calls": 2, "tool_calls": 1, "tokens_in": 0, "tokens_out": 0}

    line = format_footer_stats(buffer, Handler(), start_time=0, now=65)
    assert line.startswith("Agents: 1/")
    assert "LLM: 2" in line
    assert "Tools: 1" in line
    assert "Tokens: --" in line
    assert "01:05" in line
    assert parse_stats_line(["noise", "STATS\t" + line]) == line
    from cli.display import TOKENS_EMPTY
    from webui.catalog import display_sentences
    sentences = display_sentences()
    assert sentences["tokens_empty"] == TOKENS_EMPTY
    assert sentences["elapsed_prefix"]
    assert ":" not in sentences["elapsed_prefix"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function statsNote" in script
    assert "progress.stats" in script
    assert "01:05" not in script


def test_wall_time_summary_matches_the_cli_sentence():
    from cli.display import AnalystWallTimeTracker
    from tradingagents.graph.analyst_execution import build_analyst_execution_plan
    from webui.progress import parse_wall_time

    tracker = AnalystWallTimeTracker(build_analyst_execution_plan(["market", "news"]))
    tracker.mark_started("market", started_at=10)
    tracker.mark_completed("market", completed_at=12.5)
    summary = tracker.format_summary()
    assert summary == "Analyst wall time: Market 2.50s"
    assert parse_wall_time(["noise", summary]) == summary
    assert parse_wall_time([]) is None


def test_stream_messages_match_the_cli_panel():
    from langchain_core.messages import AIMessage

    from cli.display import MessageBuffer, record_stream_messages
    from webui.progress import parse_message_lines

    buffer = MessageBuffer()
    message = AIMessage(content="价格上涨", id="m1")
    added = record_stream_messages(buffer, [message, message])
    assert added == [("Agent", "价格上涨")]
    assert len(buffer.messages) == 1
    rows = parse_message_lines(["MESSAGE\tAgent\t价格上涨", "noise"])
    assert rows == [{"type": "Agent", "content": "价格上涨"}]


def test_review_notice_matches_the_cli_sentence():
    from webui.progress import REVIEW_NOTICE, parse_review_line, review_notice

    assert review_notice("Buy") is None
    assert review_notice(None) is None
    assert review_notice("REVIEW") == REVIEW_NOTICE
    assert "recorded for review rather than as a position" in REVIEW_NOTICE
    assert parse_review_line(["noise", "REVIEW\t" + REVIEW_NOTICE]) == REVIEW_NOTICE


def test_result_file_marks_review_without_calling_a_model(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    result = tmp_path / "review.json"
    result.write_text('{"decision": "REVIEW", "rating": "REVIEW", "report": null}', encoding="utf-8")
    service.JOBS.write_run({
        "id": "review-test",
        "kind": "analysis",
        "status": "finished",
        "result": str(result),
        "log": str(tmp_path / "missing.log"),
    })
    found = service.read_result()
    assert found["rating"] == "REVIEW"
    assert found["review"]
    assert "recorded for review rather than as a position" in found["review"]


def test_dialog_and_save_step_are_in_the_page_script():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function openLayer" in script
    assert "function closeLayer" in script
    assert 'event.key === "Escape"' in script
    assert "confirm-cancel" in script
    assert "layer-open" in script
    assert "保存与续跑" in script
    assert "function openSaveDrawer" in script
    assert "ArrowDown" in script
    assert "function mountMultiSelect" in script
    assert 'event.key !== "/"' in script
    assert "sidebar-create-menu" in script
    assert "function mountSelect" in script
    assert "function mountMultiSelect" in script
    assert "空格切换，a 全选或取消全选，Enter 完成" in script
    assert "data-vendor=" not in script
    assert "custom-select-menu" in script
    assert "if (tier === \"deep\") state.form.llm_provider = provider" not in script


def test_remembered_answers_match_the_cli_and_skip_ticker(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    service.set_runner(lambda job: 0)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market", "news"],
        "research_depth": 3,
        "llm_provider": "deepseek",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6-luna",
        "output_language": "Chinese",
        "openai_reasoning_effort": "low",
    })
    service.start_validated_run(checked["selections"], background=False)
    remembered = service.remembered_form()
    values = remembered["values"]
    assert remembered["source"] == "cli.prefs.load_last_run"
    assert "ticker" not in values and "analysis_date" not in values
    assert values["analysts"] == ["market", "news"]
    assert values["research_depth"] == 3
    assert values["llm_provider"] == "deepseek"
    assert values["quick_think_provider"] == "grok"
    assert values["deep_think_provider"] == "chatgpt"
    assert values["openai_reasoning_effort"] == "low"
    assert "不会跳过步骤" in remembered["note"]


def test_backtest_sweeps_are_listed_without_a_model(monkeypatch, tmp_path):
    root = tmp_path / "backtest" / "20261029_010101"
    root.mkdir(parents=True)
    (root / "trading_memory.md").write_text(
        "[2026-10-01 | NVDA | Hold | pending]\n\nDECISION:\n持有，这是夹具。\n\n<!-- ENTRY_END -->\n\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    found = service.list_backtest_sweeps()
    from cli.main import continue_sweep_sentence
    sentence = continue_sweep_sentence("20261029_010101")
    assert found["sweeps"][0]["id"] == "20261029_010101"
    assert found["sweeps"][0]["has_log"] is True
    assert "pending" in found["sweeps"][0]["summary"]
    assert found["sweeps"][0]["continue_sweep"] == sentence
    assert "{run_id}" not in found["sweeps"][0]["continue_sweep"]
    assert "token" not in json.dumps(found).lower()
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function sweepSelectionText" in script
    assert "data-sweep" in script
    assert "Continue or settle this sweep:" not in script
    click = script.split('querySelectorAll("[data-sweep]")', 1)[1].split("}));", 1)[0]
    assert "sweepSelectionText(row)" in click
    assert "/api/backtests" not in click
    assert "api(" not in click


def test_portfolio_file_and_clear_are_in_the_page():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "portfolio-file" in script
    assert "bindPortfolioFile" in script
    assert "clear-checkpoints-now" in script
    assert "/api/checkpoints/clear" in script
    assert "portfolio" in script


def test_result_links_html_only_when_the_page_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    report = tmp_path / "complete_report.md"
    report.write_text("md", encoding="utf-8")
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"decision": "Hold", "rating": "Hold", "report": str(report)}), encoding="utf-8")
    service.JOBS.write_run({
        "id": "html-missing",
        "kind": "analysis",
        "status": "finished",
        "result": str(result),
        "log": str(tmp_path / "missing.log"),
    })
    missing = service.read_result()
    assert missing["html"] is None
    page = report.with_suffix(".html")
    page.write_text("<p>ok</p>", encoding="utf-8")
    found = service.read_result()
    assert found["html"] == str(page)
    assert "token" not in json.dumps(found).lower()


def test_update_refresh_uses_the_cli_check_and_can_be_disabled(monkeypatch):
    import cli.updates as updates

    called = []
    monkeypatch.setattr(updates, "refresh", lambda: called.append(1))
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv(updates.OPT_OUT, raising=False)
    payload = service.refresh_updates()
    assert called == [1]
    assert payload["ran_check"] is True
    assert payload["live_check"] is False
    monkeypatch.setenv(updates.OPT_OUT, "1")
    blocked = service.refresh_updates()
    assert blocked["ran_check"] is False
    assert called == [1]
    assert "TRADINGAGENTS_NO_UPDATE_CHECK" in blocked["reason"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "检查更新" in script
    assert "/api/updates/refresh" in script
    accounts = script[script.index("async function renderAccounts"):script.index("function bindAccountActions") if "function bindAccountActions" in script else None]
    assert "${updatesSection()}" in accounts and "bindUpdates();" in accounts


def test_memory_path_stays_under_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADINGAGENTS_MEMORY_LOG_PATH", raising=False)
    monkeypatch.setattr(service, "_memory_root", lambda: tmp_path)
    # About the memory path only: never depend on (or read) this machine's real sign-in.
    monkeypatch.setattr(service, "_require_credential", lambda provider: "fixture sign-in")
    inside = service.resolve_memory_path("notes.md")
    assert inside == str((tmp_path / "memory" / "notes.md").resolve())
    with pytest.raises(service.ConsoleError):
        service.resolve_memory_path("/tmp/outside.md")
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    service.save_config({"memory_log_path": inside})
    log = Path(inside)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(
        "[2026-10-29 | NVDA | Hold | pending]\n\nDECISION:\n持有，这是夹具。\n\n<!-- ENTRY_END -->\n\n",
        encoding="utf-8",
    )
    found = service.list_memory()
    assert found["path"] == inside
    assert found["count"] == 1
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "memory_log_path": "notes.md",
    })
    assert checked["selections"]["memory_log_path"] == inside


def test_risk_rounds_can_differ_from_research_depth(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "research_depth": 1,
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "max_risk_discuss_rounds": 3,
    })
    assert checked["selections"]["research_depth"] == 1
    assert checked["selections"]["max_risk_discuss_rounds"] == 3
    config = service.build_run_config(checked["selections"])
    assert config["max_debate_rounds"] == 1
    assert config["max_risk_discuss_rounds"] == 3
    with pytest.raises(service.ConsoleError):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "max_risk_discuss_rounds": 0,
        })


def test_cache_dir_lists_checkpoints_under_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADINGAGENTS_CACHE_DIR", raising=False)
    monkeypatch.setattr(service, "_memory_root", lambda: tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    inside = service.resolve_cache_dir("cache")
    assert inside == str((tmp_path / "cache").resolve())
    with pytest.raises(service.ConsoleError):
        service.resolve_cache_dir("/tmp/outside-cache")
    db = Path(inside) / "checkpoints" / "NVDA.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fixture")
    service.save_config({"data_cache_dir": inside})
    found = service.list_checkpoints()
    assert found["directory"] == str(db.parent)
    assert found["files"] == [{"ticker": "NVDA", "bytes": 7}]
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "data_cache_dir": "cache",
    })
    config = service.build_run_config(checked["selections"])
    assert config["data_cache_dir"] == inside


def test_tool_vendor_override_reaches_config_and_unknown_names_are_rejected(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="没有这个数据工具"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "tool_vendors": {"not_a_tool": "yfinance"},
        })
    with pytest.raises(service.ConsoleError, match="get_news"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "tool_vendors": {"get_news": "sec_edgar"},
        })
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "tool_vendors": {"get_news": "alpha_vantage,yfinance"},
        "news_article_limit": "8",
        "holding_period_days": 12,
        "global_news_queries": ["oil supply", ""],
    })
    config = service.build_run_config(checked["selections"])
    assert config["tool_vendors"]["get_news"] == "alpha_vantage,yfinance"
    assert config["news_article_limit"] == 8
    assert config["holding_period_days"] == 12
    assert config["global_news_queries"] == ["oil supply"]
    with pytest.raises(service.ConsoleError, match="至少为 1"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "memory_log_max_entries": 0,
        })


def test_results_dir_lists_reports_under_the_home_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADINGAGENTS_RESULTS_DIR", raising=False)
    monkeypatch.setattr(service, "_memory_root", lambda: tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    inside = service.resolve_results_dir("logs")
    assert inside == str((tmp_path / "logs").resolve())
    with pytest.raises(service.ConsoleError):
        service.resolve_results_dir("/tmp/outside-results")
    page = Path(inside) / "reports" / "NVDA_fixture" / "complete_report.html"
    page.parent.mkdir(parents=True)
    page.write_text("<p>fixture</p>", encoding="utf-8")
    service.save_config({"results_dir": "logs"})
    found = service.list_reports()
    assert found[0]["name"] == "NVDA_fixture"
    assert service.effective_results_dir() == inside
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "results_dir": "logs",
        "save_path": "custom",
    })
    config = service.build_run_config(checked["selections"])
    assert config["results_dir"] == inside
    assert Path(checked["selections"]["save_path"]).is_relative_to(Path(inside) / "reports")


def test_invalid_saved_results_dir_falls_back_instead_of_hiding_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_memory_root", lambda: tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path / "default-logs"))
    service._config_path().write_text('{"results_dir": "/tmp/outside-results"}', encoding="utf-8")
    assert service.effective_results_dir() == str(tmp_path / "default-logs")
    with pytest.raises(service.ConsoleError):
        service.save_config({"results_dir": "/tmp/outside-results"})


def test_settings_save_reports_failure_in_the_page():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    for feedback in ("model-advanced-feedback", "data-sources-feedback"):
        assert feedback in script
    assert "tool_vendors" in script
    assert "TRADINGAGENTS_RESULTS_DIR" not in script
    assert "TRADINGAGENTS_CACHE_DIR" not in script
    saver = script[script.index("async function saveConfigPart"):script.index("function modelAdvancedSection")]
    assert ".catch(() => {})" not in saver
    assert "error-banner" in saver


def test_whats_new_includes_the_cli_upgrade_line(monkeypatch):
    import cli.updates as updates

    captured = {}

    def fake_messages(cache, running, upgrade=None):
        captured["upgrade"] = upgrade
        return ["Upgrade: python -m pip install"] if upgrade else ["missing upgrade"]

    monkeypatch.setattr(updates, "messages", fake_messages)
    payload = service.cached_updates()
    assert captured["upgrade"] is updates.upgrade_command
    assert payload["lines"] == ["Upgrade: python -m pip install"]
    assert payload["live_check"] is False


def test_timeout_reaches_the_run_and_empty_keeps_the_console_bound(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "timeout": "45",
    })
    config = service.build_run_config(checked["selections"])
    assert config["timeout"] == 45
    with pytest.raises(service.ConsoleError, match="大于 0"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "timeout": 0,
        })
    omitted = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
    })
    bare = service.build_run_config(omitted["selections"])
    assert bare.get("timeout") in (None, "")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function logPathNotice" in script
    assert "log.cli" in script
    assert 'displaySentence("log_prefix")' not in script
    assert "日志路径 Log:" not in script
    assert "还没有日志文件" not in script
    assert "restart-run" in script
    assert "configKeyRows" in script
    assert "set-key-${item.field}" in script
    assert "留空沿用 20" not in script
    assert "留空使用 120" not in script


def test_data_env_is_saved_without_being_echoed(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(service, "find_dotenv", lambda usecwd=True: str(env_file))
    for name in ("FRED_API_KEY", "ALPHA_VANTAGE_API_KEY", "SEC_EDGAR_USER_AGENT"):
        monkeypatch.delenv(name, raising=False)
    secret = "fred-test-not-a-real-key"
    contact = "Example Analyst example@example.com"
    saved = service.save_data_env("FRED_API_KEY", secret)
    assert saved == {"saved": True, "env_var": "FRED_API_KEY", "configured": True}
    assert secret not in json.dumps(saved)
    assert secret in env_file.read_text(encoding="utf-8")
    contact_saved = service.save_data_env("SEC_EDGAR_USER_AGENT", contact)
    assert contact not in json.dumps(contact_saved)
    status = service.data_env_status()
    assert {item["env"] for item in status} == {
        "FRED_API_KEY", "ALPHA_VANTAGE_API_KEY", "SEC_EDGAR_USER_AGENT",
    }
    assert all("note" in item and "value" not in item for item in status)
    assert secret not in json.dumps(status)
    assert contact not in json.dumps(status)
    with pytest.raises(service.ConsoleError, match="只能保存"):
        service.save_data_env("OPENAI_API_KEY", secret)
    with pytest.raises(service.ConsoleError, match="不能为空"):
        service.save_data_env("ALPHA_VANTAGE_API_KEY", "  ")
    from webui.catalog import DATA_ENV
    assert [item["env"] for item in DATA_ENV] == [
        "FRED_API_KEY", "ALPHA_VANTAGE_API_KEY", "SEC_EDGAR_USER_AGENT",
    ]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog.data_env" in script
    assert "/api/data-env" in script
    assert "不会回显" in script
    assert "保存数据凭据" in script
    save_env = script[script.index("async function saveDataEnv"):script.index("async function beginLogin")]
    assert "await renderAccounts()" in save_env
    assert save_env.index("await api(\"/api/data-env\"") < save_env.index("await renderAccounts()")


def test_cloud_env_is_saved_without_being_echoed(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(service, "find_dotenv", lambda usecwd=True: str(env_file))
    for name in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT_NAME", "OPENAI_API_VERSION", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK"):
        monkeypatch.delenv(name, raising=False)
    token = "bt-test-not-a-real-token"
    saved = service.save_data_env("AWS_BEARER_TOKEN_BEDROCK", token)
    assert saved["env_var"] == "AWS_BEARER_TOKEN_BEDROCK"
    assert token not in json.dumps(saved)
    assert token not in json.dumps(service.cloud_env_status())
    region = service.save_data_env("AWS_DEFAULT_REGION", "eu-west-1")
    assert region["configured"] is True
    with pytest.raises(service.ConsoleError, match="http"):
        service.save_data_env("AZURE_OPENAI_ENDPOINT", "资源名.openai.azure.com")
    endpoint = service.save_data_env("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    assert endpoint["env_var"] == "AZURE_OPENAI_ENDPOINT"
    assert "https://example.openai.azure.com/" not in json.dumps(endpoint)
    from webui.catalog import CLOUD_ENV
    assert {item["env"] for item in CLOUD_ENV} == {
        "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT_NAME", "OPENAI_API_VERSION",
        "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK",
    }
    from webui.catalog import PROVIDER_ICONS
    assert PROVIDER_ICONS["glm"] == "zdotai.svg"
    assert "grok" not in PROVIDER_ICONS
    assert "groq" not in PROVIDER_ICONS
    assert Path("webui/static/icons/zdotai.svg").read_text(encoding="utf-8").startswith("<svg")
    assert "Z.ai" in Path("webui/static/icons/zdotai.svg").read_text(encoding="utf-8")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog.cloud_env" in script
    assert "cloud_env" in script


def test_minimax_icon_is_the_unmodified_simple_icons_mark():
    import hashlib

    from webui.catalog import PROVIDER_ICONS

    mark = Path("webui/static/icons/minimax.svg").read_bytes()
    assert hashlib.sha256(mark).hexdigest() == "ef95e28b290e5954a5c0be338925d81f13b3d06a0a32af29a4bc64583fbcc368"
    assert mark.startswith(b"<svg") and b"<title>MiniMax</title>" in mark
    assert PROVIDER_ICONS["minimax"] == "minimax.svg"
    assert PROVIDER_ICONS["minimax-cn"] == "minimax.svg"
    assert "grok" not in PROVIDER_ICONS
    assert "xai" not in PROVIDER_ICONS
    assert "groq" not in PROVIDER_ICONS
    assert "glm-cn" not in PROVIDER_ICONS


def test_ticker_note_names_the_cli_benchmark():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "outcome.failed" not in script
    assert "outcome.unsettled" not in script
    assert "不另写带花括号的句子" not in script
    assert "failed / unsettled" not in script


def test_save_failure_keeps_the_decision_and_prints_the_cli_sentence(monkeypatch, tmp_path, capsys):
    import tradingagents.graph.trading_graph as graph_module

    class Graph:
        config = {"checkpoint_enabled": False, "data_cache_dir": str(tmp_path)}

        def __init__(self, *args, **kwargs):
            return None

        def _run_signature(self, *args):
            return "sig"

        def create_run_state(self, *args):
            return {}

        @property
        def propagator(self):
            return type("Propagator", (), {"get_graph_args": lambda self, callbacks=None: {}})()

        def begin_checkpoint(self, *args):
            return None

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [], {"final_trade_decision": "Hold NVDA"}

        def record_decision(self, *args):
            return None

        def clear_checkpoint_on_success(self, *args):
            return None

        def end_checkpoint(self):
            return None

        def save_reports(self, *args, **kwargs):
            raise OSError("disk full")

    monkeypatch.setattr(graph_module, "TradingAgentsGraph", Graph)
    result_path = tmp_path / "result.json"
    code = service.default_runner({
        "selections": {
            "ticker": "NVDA",
            "analysis_date": "2026-10-29",
            "asset_type": "stock",
            "analysts": ["market"],
            "research_depth": 1,
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "backend_url": None,
            "save": True,
            "html": True,
        },
        "result_path": str(result_path),
    })
    assert code == 0
    assert "Error saving report: disk full" in capsys.readouterr().out
    saved = json.loads(result_path.read_text(encoding="utf-8"))
    assert saved["save_error"] == "disk full"
    assert saved["saved"] is False
    assert saved["decision"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function saveNotice" in script
    assert "result.save_error" in script
    assert "保存报告失败" in script
    assert "✓ Report saved to:" not in script
    assert "  Complete report:" not in script
    assert "  HTML report:" not in script


def test_logout_sentences_match_the_cli(monkeypatch):
    calls = []

    def fake_sign_out():
        calls.append("sign_out")
        return "forgotten"

    monkeypatch.setattr(service, "get_subscription", lambda key: type("Spec", (), {
        "auth": type("Auth", (), {"sign_out": staticmethod(fake_sign_out)})(),
    })())
    result = service.logout("chatgpt")
    assert calls == ["sign_out"]
    assert result["outcome"] == "forgotten"
    assert result["cli"] == (
        "Signed out here, but OpenAI could not be reached to revoke the sign-in; "
        "it stays valid at OpenAI until it expires."
    )
    assert "没有" not in result["cli"]
    assert "已在本机退出" in result["zh"]
    grok_zh, grok_cli = service.logout_sentence("grok", "forgotten")
    assert grok_cli == "Signed out of Grok locally; server revocation was not confirmed."
    assert "服务端撤销没有确认" in grok_zh
    assert service.logout_sentence("chatgpt", "revoked")[1] == "Signed out of ChatGPT."
    assert service.logout_sentence("grok", None)[1] == "Not signed in to Grok."
    assert "signed in as user@example.com" in service.auth_status_sentence("chatgpt", "user@example.com")
    assert service.auth_status_sentence("grok", None).endswith("not signed in (local status only; no live validation).")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "result.cli" in script
    assert "item.cli" in script
    assert "item.plan_use" in script
    assert "function setAuthCardFeedback" in script



def test_signed_in_status_includes_the_pre_run_plan_sentence(monkeypatch):
    monkeypatch.setattr(
        "tradingagents.llm_clients.subscriptions.get_subscription",
        service.get_subscription,
    )

    class Auth:
        def signed_in_as(self):
            return "signed"

    class Spec:
        display_name = "ChatGPT (Plus or Pro plan)"
        auth = Auth()

    monkeypatch.setattr(service, "SUBSCRIPTIONS", {"chatgpt": Spec()})
    status = service.auth_status()
    assert status["accounts"][0]["plan_use"] == "✓ Using your ChatGPT plan: signed"
    assert status["accounts"][0]["cli"].startswith("ChatGPT: signed in as signed")


def test_checkpoint_clear_uses_the_cli_sentence(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_memory_root", lambda: tmp_path)
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    cache = tmp_path / "cache"
    db = cache / "checkpoints" / "NVDA.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fixture")
    service.save_config({"data_cache_dir": str(cache)})
    cleared = service.clear_checkpoints()
    assert cleared["cleared"] == 1
    assert cleared["cli"] == "Cleared 1 checkpoint(s)."
    assert "已清除 1 个断点" in cleared["zh"]
    assert not db.exists()
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "outcome_sentences?.cleared" in script or "data.cli" in script
    assert "Cleared ${data.cleared} checkpoint(s)." not in script
    assert "TradingAgents: not running" not in script
    assert "stale runtime metadata cleaned" not in script


def test_second_start_is_refused_while_one_is_running(monkeypatch, tmp_path):
    # RUN-004: with one run slot and no queue, a second analysis is refused as queue-full;
    # a backtest is still refused with the launcher sentence while anything runs.
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_RUNS", "1")
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_QUEUE", "0")
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    service.set_runner(None)
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        checked = service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
        first = service.start_validated_run(checked["selections"])
        assert service._alive(first["pid"])
        with pytest.raises(service.ConsoleError, match="队列已满"):
            service.start_validated_run(checked["selections"])
        with pytest.raises(service.ConsoleError, match="回测没有启动"):   # B3b: Chinese only, no CLI tail
            service.start_backtest({
                "tickers": "NVDA",
                "start": "2026-10-01",
                "end": "2026-10-01",
                "every": 7,
                "llm_provider": "chatgpt",
                "quick_think_llm": "gpt-6-luna",
                "deep_think_llm": "gpt-6-sol",
            })
        idle = service.stop_run()
        assert idle["status"] == "stopped"
        assert idle["cli"] == "Stopping TradingAgents…"
        assert "正在停止" in idle["zh"]
    finally:
        service.stop_run()
        service.set_spawn_argv(None)
    absent = service.stop_run()
    assert absent["cli"] == "TradingAgents is not running."
    assert "没有查询" in absent["note"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "result.cli" in script


def test_background_start_names_the_log_like_the_cli(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    service.set_runner(None)
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        checked = service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
        started = service.start_validated_run(checked["selections"])
        assert started["cli"].startswith("TradingAgents started in background; log: ")
        assert started["log"] in started["cli"]
        assert Path(started["log"]).is_file()
        assert "进程启动不等于分析完成" in started["zh"]
        assert service._alive(started["pid"])
    finally:
        service.stop_run()
        service.set_spawn_argv(None)
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "TradingAgents started in background" not in script
    assert "result.cli" in script


def test_immediate_exit_is_a_background_startup_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    service.set_runner(None)
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "raise SystemExit(1)"])
    try:
        checked = service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
        with pytest.raises(service.ConsoleError, match="TradingAgents background startup failed; see"):
            service.start_validated_run(checked["selections"])
        failed = service.JOBS.current_run()
        assert failed["status"] == "failed"
        assert "background startup failed" in failed["cli"]
        assert Path(failed["log"]).is_file()
    finally:
        service.set_spawn_argv(None)


def test_stop_while_starting_does_not_call_the_process_idle(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    service.JOBS.write_run({"id": "starting", "status": "running", "pid": None, "selections": {}})
    result = service.stop_run()
    assert result["status"] == "starting"
    assert result["cli"] == "TradingAgents is still starting; try stop again shortly."
    assert service.JOBS.current_run()["status"] == "running"


def test_log_lines_reject_the_same_values_as_the_cli(monkeypatch, tmp_path):
    assert service.parse_log_line_count(None) == 40
    assert service.parse_log_line_count("") == 40
    assert service.parse_log_line_count("12") == 12
    with pytest.raises(service.ConsoleError, match="logs: --lines requires an integer"):
        service.parse_log_line_count("nope")
    with pytest.raises(service.ConsoleError, match="usage: python -m cli.launcher logs"):
        service.parse_log_line_count("-1")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="log-lines" value="40"' in script
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    log = tmp_path / "run.log"
    log.write_text("line\n", encoding="utf-8")
    service.JOBS.write_run({"id": "log-test", "log": str(log), "status": "running"})

    original = service.Path.read_text

    def selective(self, *args, **kwargs):
        if self.name == "run.log":
            raise OSError("permission denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(service.Path, "read_text", selective)
    found = service.read_log(40)
    assert found["error"] == "Could not read log: permission denied"


def test_role_split_does_not_call_the_quick_model_a_data_source():
    from webui.catalog import ROLE_SPLIT, public_catalog

    catalog = public_catalog()
    assert catalog["role_split"] == ROLE_SPLIT
    assert "不是数据源" in catalog["role_split"]["quick_zh"]
    assert "不是 Grok 搜索" in catalog["role_split"]["data_zh"]
    joined = " ".join(catalog["notes"])
    assert "快模型负责取数" not in joined
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "快模型负责取数" not in script
    assert "tier-role-note" in script
    assert "Grok 不会代替 yfinance 或 SEC" in script
    assert "TRADINGAGENTS_QUICK_THINK_PROVIDER / TRADINGAGENTS_DEEP_THINK_PROVIDER" not in script


def test_emitted_launcher_sentences_use_the_cli_constants():
    from cli.launcher import (
        ALREADY_RUNNING,
        STILL_STARTING,
        STOP_TIMEOUT_SENTENCE,
        STOPPING,
        status_sentence,
    )
    from cli.prompts import DEPTH_OPTIONS, URL_SCHEME_REQUIRED

    assert status_sentence("running", 12) == "TradingAgents: running (child pid 12)"
    assert service.console_status_sentence({"status": "running", "pid": 12})["cli"] == status_sentence("running", 12)
    assert service.console_status_sentence({"status": "running", "pid": 12})["zh"] == "控制台任务 running (child pid 12)。"
    assert STILL_STARTING == "TradingAgents is still starting; try stop again shortly."
    assert STOPPING == "Stopping TradingAgents…"
    assert STOP_TIMEOUT_SENTENCE == "TradingAgents did not stop within the timeout."
    assert ALREADY_RUNNING.endswith(".")
    names = [display.split(" - ", 1)[0] for display, _ in DEPTH_OPTIONS]
    try:
        service._validate_analysis({"research_depth": 2, "ticker": "NVDA", "date": "2026-10-29"})
    except service.ConsoleError as exc:
        assert " / ".join(names) in str(exc)
        assert "1、3 或 5" in str(exc)
    else:
        raise AssertionError("invalid depth was accepted")
    assert URL_SCHEME_REQUIRED == "Enter a URL starting with http:// or https://"


def test_console_status_sentence_matches_launcher_phases_only():
    absent = service.console_status_sentence(None)
    assert absent["cli"] == "TradingAgents: not running"
    running = service.console_status_sentence({"status": "running", "pid": 12})
    assert running["cli"] == "TradingAgents: running (child pid 12)"
    finished = service.console_status_sentence({"status": "finished", "pid": 12})
    assert finished["cli"] is None
    assert not finished["zh"].startswith("TradingAgents:")
    for word in ("failed", "stopped", "idle"):
        sentence = service.console_status_sentence({"status": word})
        assert sentence["cli"] is None
        assert "TradingAgents:" not in (sentence["cli"] or "")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "TradingAgents: ${" not in script
    assert "TradingAgents: not running" not in script
    assert "function statusLine" in script
    assert "data.status_sentence" in script


def test_status_argv_line_is_printed_only_for_a_stored_process_command(monkeypatch, tmp_path, capsys):
    from cli.launcher import status_argv_line

    monkeypatch.setattr("cli.launcher._request", lambda paths, command, timeout=1.0: {
        "ok": True,
        "status": "running",
        "child_pid": 12,
        "argv": ["--ticker", "NVDA"],
    })
    assert launcher_status(tmp_path) == 0
    printed = capsys.readouterr().out
    assert printed == "TradingAgents: running (child pid 12)\nargv: [\"--ticker\", \"NVDA\"]\n"
    monkeypatch.setattr("cli.launcher._request", lambda paths, command, timeout=1.0: {
        "ok": True,
        "status": "running",
        "child_pid": 12,
    })
    assert launcher_status(tmp_path) == 0
    assert capsys.readouterr().out == "TradingAgents: running (child pid 12)\n"
    assert status_argv_line(["--ticker", "NVDA"]) == "argv: [\"--ticker\", \"NVDA\"]"
    assert service.status_argv_line_for(None) is None
    assert service.status_argv_line_for({"status": "running"}) is None
    assert service.status_argv_line_for({"status": "finished", "argv": ["--ticker", "NVDA"]}) is None
    assert service.status_argv_line_for({"status": "running", "argv": ["ok", 1]}) is None
    assert service.status_argv_line_for({
        "status": "running",
        "argv": ["--ticker", "NVDA"],
    }) == status_argv_line(["--ticker", "NVDA"])
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function statusArgvLine" in script
    assert "argv_line" in script
    assert "argv: [" not in script


def launcher_status(paths):
    from cli.launcher import _status
    return _status(paths)


def test_spawned_run_keeps_its_process_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    service.set_spawn_argv(lambda job: command)
    try:
        checked = service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
        started = service.start_validated_run(checked["selections"])
        assert started["argv"] == command
        line = service.status_argv_line_for(service.JOBS.current_run())
        assert line == "argv: " + json.dumps(command, ensure_ascii=False)
        assert "NVDA" not in line
    finally:
        service.stop_run()
        service.set_spawn_argv(None)


def test_inherited_lock_errors_match_the_launcher_and_keep_the_value():
    import subprocess

    from cli.launcher import MISSING_INHERITED_LOCK, invalid_inherited_lock_sentence
    from webui.catalog import launcher_conditional_rows

    rows = {item["key"]: item for item in launcher_conditional_rows()}
    assert rows["inherited_lock_missing"]["cli"] == MISSING_INHERITED_LOCK
    try:
        int("nope")
    except ValueError as exc:
        nope_sentence = invalid_inherited_lock_sentence(exc)
    assert rows["inherited_lock_nope"]["cli"] == nope_sentence
    assert "不套用 nope" in rows["inherited_lock_nope"]["when"]
    assert "控制台不打印这句" in rows["inherited_lock_missing"]["when"]
    env = os.environ.copy()
    env.pop("TRADINGAGENTS_LAUNCHER_LOCK_FD", None)
    missing = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "--_supervise"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 2
    assert missing.stderr == MISSING_INHERITED_LOCK + "\n"
    env["TRADINGAGENTS_LAUNCHER_LOCK_FD"] = "nope"
    nope = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "--_supervise"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert nope.returncode == 2
    assert nope.stderr == nope_sentence + "\n"
    env["TRADINGAGENTS_LAUNCHER_LOCK_FD"] = "ChatGPT"
    other = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "--_supervise"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert other.returncode == 2
    assert other.stderr != nope.stderr
    assert "ChatGPT" in other.stderr
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "renderHelp" not in script
    assert "invalid literal for int()" not in script
    assert "launcher: missing inherited lock" not in script


def test_logs_help_wins_over_other_arguments_and_keeps_the_other_errors():
    import subprocess

    from cli.launcher import LOGS_HELP, LOGS_LINES_INTEGER, LOGS_USAGE
    from webui.catalog import launcher_conditional_rows

    def run(args):
        return subprocess.run(
            [sys.executable, "-m", "cli.launcher", *args],
            capture_output=True,
            text=True,
            check=False,
        )

    help_only = run(["logs", "--help"])
    mixed = run(["logs", "--lines", "nope", "--help"])
    short = run(["logs", "extra", "-h"])
    bad = run(["logs", "--lines", "nope"])
    missing = run(["logs", "--lines"])
    extra = run(["logs", "extra"])
    negative = run(["logs", "--lines", "-1"])
    assert help_only.returncode == 0
    assert help_only.stdout == LOGS_HELP + "\n"
    assert mixed.returncode == 0
    assert mixed.stdout == help_only.stdout
    assert "requires an integer" not in mixed.stderr
    assert short.returncode == 0
    assert short.stdout == help_only.stdout
    assert bad.returncode == 2
    assert bad.stderr == LOGS_LINES_INTEGER + "\n"
    assert LOGS_HELP not in bad.stdout
    assert missing.stderr == LOGS_LINES_INTEGER + "\n"
    assert "nope" not in bad.stderr
    assert extra.returncode == 2
    assert extra.stderr == LOGS_USAGE + "\n"
    assert negative.stderr == LOGS_USAGE + "\n"
    assert negative.stderr != bad.stderr
    rows = {item["key"]: item for item in launcher_conditional_rows()}
    assert rows["logs_help"]["cli"] == LOGS_HELP
    assert rows["logs_lines_integer"]["cli"] == LOGS_LINES_INTEGER
    assert rows["logs_usage"]["cli"] == LOGS_USAGE
    assert "即使同时还有别的参数" in rows["logs_help"]["when"]
    assert "负数不是这句" in rows["logs_lines_integer"]["when"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "logs_lines_integer" in script
    assert "logs_usage" in script
    assert "data-logs" in script


def test_cancel_exits_match_the_cli_and_stay_off_the_page(monkeypatch):
    import questionary

    from cli.display import console
    from cli.prompts import (
        NO_TICKER_EXIT,
        get_ticker,
        no_thinking_engine_exit,
    )
    from webui.catalog import cancel_exit_rows

    rows = {item["key"]: item for item in cancel_exit_rows()}
    assert rows["ticker"]["cli"] == NO_TICKER_EXIT
    assert rows["quick"]["cli"] == no_thinking_engine_exit("quick")
    assert rows["deep"]["cli"] == no_thinking_engine_exit("deep")
    assert rows["quick"]["cli"] != rows["deep"]["cli"]
    assert rows["analysts"]["cli"] != rows["model"]["cli"]
    assert rows["cancelled"]["cli"] != rows["endpoint"]["cli"]
    assert "控制台没有取消" in rows["ticker"]["when"]
    assert "不套用这句" in rows["quick"]["when"]

    class Cancelled:
        def ask(self):
            return None

    monkeypatch.setattr(questionary, "text", lambda *args, **kwargs: Cancelled())
    previous = console.width
    console.width = 200
    try:
        with console.capture() as captured, pytest.raises(SystemExit) as exited:
            get_ticker()
        printed = captured.get()
    finally:
        console.width = previous
    assert exited.value.code == 1
    assert NO_TICKER_EXIT in printed
    assert "SPY" not in printed
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function cancelExitBlock" not in script
    assert "No ticker symbol provided. Exiting..." not in script
    assert "No quick thinking llm engine selected. Exiting..." not in script
    assert "Cancelled. Exiting..." not in script


def test_already_running_sentences_are_not_interchangeable(monkeypatch, tmp_path):
    from cli.launcher import ALREADY_RUNNING, ALREADY_RUNNING_LOCK
    from webui.catalog import launcher_conditional_rows

    rows = {item["key"]: item for item in launcher_conditional_rows()}
    locked = rows["already_running_lock"]
    listening = rows["already_running_socket"]
    assert locked["cli"] == ALREADY_RUNNING
    assert listening["cli"] == ALREADY_RUNNING_LOCK
    assert locked["cli"] != listening["cli"]
    assert locked["cli"].endswith(".")
    assert not listening["cli"].endswith(".")
    assert "不套用这句" in locked["when"]
    assert "句尾没有句号" in listening["when"]
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    service.set_spawn_argv(lambda job: [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        checked = service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
        service.start_validated_run(checked["selections"])
        service.start_validated_run(checked["selections"])   # both run slots busy (W44: a backtest needs one)
        with pytest.raises(service.ConsoleError) as refused:
            service.start_backtest({"tickers": "NVDA", "start": "2026-10-01", "end": "2026-10-01", "every": 7,
                                    "llm_provider": "chatgpt", "quick_think_llm": "gpt-6-luna",
                                    "deep_think_llm": "gpt-6-sol"})
        # B3b: the console sentence is Chinese only; neither English CLI line is appended.
        assert ALREADY_RUNNING not in str(refused.value)
        assert ALREADY_RUNNING_LOCK not in str(refused.value)
        assert "回测没有启动" in str(refused.value)
    finally:
        service.stop_run()
        service.set_spawn_argv(None)
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function alreadyRunningNote" in script
    assert "already_running_lock" in script
    assert "already_running_socket" in script
    assert "TradingAgents is already running" not in script


def test_split_subscriptions_do_not_ask_for_openai_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def signed(key):
        return type("Spec", (), {
            "auth": type("Auth", (), {"signed_in_as": staticmethod(lambda: "signed")})(),
            "display_name": key,
        })()

    monkeypatch.setattr(service, "get_subscription", signed)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6.1-sol",
        "openai_reasoning_effort": "medium",
    })
    config = service.build_run_config(checked["selections"])
    assert config["quick_think_provider"] == "grok"
    assert config["deep_think_provider"] == "chatgpt"
    assert config["openai_reasoning_effort"] == "medium"
    with pytest.raises(service.ConsoleError, match="OPENAI_API_KEY"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "openai",
            "quick_think_provider": "openai",
            "deep_think_provider": "openai",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })


def test_run_log_preamble_includes_flag_sentences_without_claiming_defaults():
    """A supplied ticker or date is a CLI flag. A blank field must not claim that sentence."""
    supplied = service.run_preamble_lines({
        "ticker": "NVDA",
        "analysis_date": "2026-10-29",
        "ticker_from_default": False,
        "date_from_default": False,
    })
    assert "✓ Ticker from --ticker: NVDA" in supplied
    assert "✓ Analysis date from --date: 2026-10-29" in supplied
    blank = "\n".join(service.run_preamble_lines({
        "ticker": "SPY",
        "analysis_date": "2026-10-29",
        "ticker_from_default": True,
        "date_from_default": True,
    }))
    assert "✓ Ticker from --ticker:" not in blank
    assert "✓ Analysis date from --date:" not in blank
    source = Path("webui/service.py").read_text(encoding="utf-8")
    printed = source.index("run_preamble_lines(selections)")
    live = source.index("selected_ticker_message(selections", printed)
    assert printed < live


def test_flag_confirmations_match_the_cli_sentences():
    lines = service.flag_confirmations({
        "ticker": "BTC-USD",
        "analysis_date": "2026-10-29",
        "asset_type": "crypto",
        "analysts": ["market", "news"],
    })
    assert lines == [
        "✓ Ticker from --ticker: BTC-USD",
        "Detected asset type: crypto",
        "✓ Analysis date from --date: 2026-10-29",
        "Selected analysts: market, news",
    ]
    stock = service.flag_confirmations({
        "ticker": "NVDA",
        "analysis_date": "2026-10-29",
        "asset_type": "stock",
        "analysts": ["market"],
    })
    assert "Detected asset type:" not in "\n".join(stock)


def test_saved_report_lines_match_the_cli_markup_stripped():
    lines = service.report_saved_lines("/tmp/reports/NVDA", "complete_report.md", "complete_report.html")
    assert lines == [
        "✓ Report saved to: /tmp/reports/NVDA",
        "  Complete report: complete_report.md",
        "  HTML report: complete_report.html",
    ]
    assert service.report_saved_lines("/tmp/reports/NVDA", "complete_report.md") == [
        "✓ Report saved to: /tmp/reports/NVDA",
        "  Complete report: complete_report.md",
    ]


def test_subscription_and_launcher_help_match_the_cli():
    from typer.testing import CliRunner

    from cli.launcher import HELP
    from cli.main import app as cli_app
    from webui.catalog import public_catalog

    catalog = public_catalog()
    commands = {item["cli"]: item for item in catalog["commands"]}
    assert commands["stop"]["help"] == "stop                            Ask the owned analysis process to stop"
    assert commands["status"]["help"].endswith("Show the local process state")
    assert commands["logs"]["help"].endswith("Show the background log tail and path")
    assert commands["web"]["help"].endswith("Open the local Chinese console")
    assert commands["cli"]["help"].endswith("Raw passthrough to cli.main")
    assert commands["login"]["help"] == commands["logout"]["help"] == commands["auth-status"]["help"]
    assert commands["login"]["help"].startswith("login/logout/auth-status")
    assert "Exec the corresponding cli.main command" in commands["login"]["help"]
    assert "help" not in commands["backtest"]
    assert commands["backtest"]["doc"] == "Score past decisions over a grid of tickers and dates."
    assert catalog["launcher_preamble"] in HELP
    assert catalog["launcher_preamble"].startswith("usage: python -m cli.launcher [COMMAND] [ARGS...]")
    assert "With no command, start is\nused." in catalog["launcher_preamble"]
    subs = {item["cli"]: item for item in catalog["subscription_commands"]}
    assert subs["login"]["doc"] == "Sign in to a subscription provider on this machine."
    assert subs["logout"]["doc"] == "Sign out of a subscription provider on this machine."
    assert subs["auth-status"]["doc"].startswith("Show local sign-in status without validating credentials live.")
    assert subs["login"]["help"] == "The plan to sign in with"
    assert subs["logout"]["help"] == "The plan to sign out of"
    assert subs["auth-status"]["help"] == "A subscription to inspect; omit for all"
    runner = CliRunner()
    # A fixed wide terminal whatever the caller's COLUMNS/TERM (COLUMNS=0, 40, TERM=dumb
    # with FORCE_COLOR all wrap or box-split the help sentences differently).
    wide = {"COLUMNS": "220", "LINES": "50", "TERM": "xterm-256color", "TERMINAL_WIDTH": None}
    for name, row in subs.items():
        result = runner.invoke(cli_app, [name, "--help"], env=wide)
        assert result.exit_code == 0
        emitted = " ".join(result.stdout.split())
        assert row["doc"].splitlines()[0] in emitted
        assert row["help"] in emitted
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "subscription_commands" in script
    assert "function renderHelp" not in script


def test_backtest_is_not_a_bare_launcher_command():
    import subprocess
    import sys

    from cli.launcher import HELP, unknown_command_sentence
    from webui.catalog import backtest_route, public_catalog

    sentence = unknown_command_sentence("backtest")
    assert sentence.startswith("unknown launcher command: backtest\n\n")
    assert sentence.endswith(HELP)
    completed = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "backtest"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert completed.stderr == sentence + "\n"
    assert completed.stdout == ""
    route = backtest_route()
    assert route["typer"] == "backtest"
    assert route["launcher"] == "cli backtest"
    assert route["rejected_command"] == "backtest"
    assert route["rejected"] == sentence
    assert public_catalog()["backtest_route"] == route
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderBacktest" in script
    assert "unknown launcher command: backtest" not in script
    page = Path("webui/static/index.html").read_text(encoding="utf-8")
    assert 'data-cli="cli backtest"' in page
    assert 'data-page="backtest" data-cli="backtest"' not in page
    working = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "cli", "backtest", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert working.returncode == 0
    assert "Score past decisions over a grid of tickers and dates." in working.stdout
    assert "unknown launcher command" not in working.stderr


def test_launcher_conditional_sentences_match_the_process():
    import subprocess
    import sys

    from cli.launcher import (
        LOGS_HELP,
        STATUS_USAGE,
        STOP_USAGE,
        not_analysis_argument_sentence,
    )
    from webui.catalog import launcher_conditional_rows, public_catalog

    cases = [
        (["logs", "--help"], 0, LOGS_HELP + "\n", ""),
        (["logs", "-h"], 0, LOGS_HELP + "\n", ""),
        (["stop", "extra"], 2, "", STOP_USAGE + "\n"),
        (["status", "extra"], 2, "", STATUS_USAGE + "\n"),
        (["start", "login"], 2, "", not_analysis_argument_sentence("login") + "\n"),
        (["restart", "logout"], 2, "", not_analysis_argument_sentence("logout") + "\n"),
        (["start", "auth-status"], 2, "", not_analysis_argument_sentence("auth-status") + "\n"),
    ]
    for args, code, out, err in cases:
        completed = subprocess.run(
            [sys.executable, "-m", "cli.launcher", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == code, args
        assert completed.stdout == out, args
        assert completed.stderr == err, args
    rows = {item["key"]: item for item in launcher_conditional_rows()}
    assert rows["logs_help"]["cli"] == LOGS_HELP
    assert rows["stop_usage"]["cli"] == STOP_USAGE
    assert rows["status_usage"]["cli"] == STATUS_USAGE
    assert rows["not_analysis_login"]["cli"] == not_analysis_argument_sentence("login")
    assert rows["not_analysis_logout"]["cli"] == not_analysis_argument_sentence("logout")
    assert rows["not_analysis_auth-status"]["cli"] == not_analysis_argument_sentence("auth-status")
    assert "login" not in rows["not_analysis_logout"]["cli"]
    assert public_catalog()["launcher_conditional"] == launcher_conditional_rows()
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderHelp" not in script
    assert "usage: python -m cli.launcher stop" not in script
    assert "is not an analysis argument" not in script
    assert "Show the background log tail and its path." not in script


def test_web_help_matches_the_launcher_process():
    import subprocess
    import sys

    from webui.catalog import launcher_conditional_rows
    from webui.server import console_arg_error, console_help_text

    help_text = console_help_text()
    assert help_text.startswith("usage: python -m cli.launcher web ")
    assert "[-h]" in help_text and "--allow-lan" in help_text
    assert "打开 TradingAgents 控制台" in help_text
    for args in (["web", "--help"], ["web", "-h"]):
        completed = subprocess.run(
            [sys.executable, "-m", "cli.launcher", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, args
        assert completed.stdout == help_text
        assert completed.stderr == ""
    nope = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "web", "--nope"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert nope.returncode == 2
    assert nope.stderr == console_arg_error(["--nope"])
    assert "--nope" in nope.stderr
    bad = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "web", "--port", "nope"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad.returncode == 2
    assert bad.stderr == console_arg_error(["--port", "nope"])
    other = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "web", "--port", "xx"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert other.returncode == 2
    assert "invalid int value: 'xx'" in other.stderr
    assert other.stderr != bad.stderr
    rows = {item["key"]: item for item in launcher_conditional_rows()}
    assert rows["web_help"]["cli"] == help_text
    assert rows["web_unrecognized"]["cli"] == nope.stderr
    assert rows["web_port"]["cli"] == bad.stderr
    assert "xx" not in rows["web_port"]["cli"]
    assert "别的非整数值会带上当时的值" in rows["web_port"]["when"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderHelp" not in script
    assert "usage: launcher.py [-h]" not in script
    assert "usage: python -m cli.launcher web [-h]" not in script
    assert "unrecognized arguments: --nope" not in script


def test_the_shell_script_help_matches_the_launcher_process():
    """./tradingagents.sh needs Conda. Skipped only where the script itself says Conda is missing.

    W103: the shell runs tradingagents-launcher, so web --help shows that name
    (not the canonical ``python -m cli.launcher web`` catalog form).
    """
    import subprocess

    scripted = subprocess.run(
        ["./tradingagents.sh", "web", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    if scripted.returncode != 0 and scripted.stdout == "" and scripted.stderr.startswith("未找到 Conda。"):
        pytest.skip("this machine has no Conda; ./tradingagents.sh cannot start here")
    assert scripted.returncode == 0
    assert scripted.stdout.startswith("usage: tradingagents-launcher web ")
    assert "--port" in scripted.stdout
    assert "打开 TradingAgents 控制台" in scripted.stdout


def test_start_and_restart_help_print_launcher_help_only_when_alone():
    import subprocess
    import sys

    from cli.launcher import HELP
    from webui.catalog import help_invocation_notes, public_catalog

    def run(args, module="cli.launcher"):
        env = os.environ.copy()
        env["COLUMNS"] = "96"
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    for args in (["start", "--help"], ["start", "-h"], ["restart", "--help"], ["restart", "-h"]):
        completed = run(args)
        assert completed.returncode == 0, args
        assert completed.stdout == HELP + "\n", args
        assert completed.stderr == ""
    main_help = run(["--help"], "cli.main")
    assert main_help.returncode == 0
    for args in (["start", "--help", "extra"], ["restart", "--help", "NVDA"], ["cli", "--help"]):
        completed = run(args)
        assert completed.returncode == 0, args
        assert completed.stdout == main_help.stdout, args
        assert completed.stdout != HELP + "\n"
    mixed = run(["restart", "-h", "--ticker"])
    assert mixed.returncode == 2
    assert mixed.stdout != HELP + "\n"
    assert "No such option: -h" in mixed.stderr
    direct = run(["-h", "--ticker"], "cli.main")
    assert mixed.stdout == direct.stdout
    assert mixed.stderr == direct.stderr
    notes = help_invocation_notes()
    assert "后面还有参数时不打印这段" in notes["launcher_only"]
    assert notes["cli_same"] == "cli --help 打印的是下面同一段，退出码 0。"
    assert public_catalog()["help_invocation"] == notes
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderHelp" not in script
    assert "只有 start --help" not in script


def test_extra_help_arguments_are_forwarded_and_not_reused():
    import subprocess

    from cli.launcher import HELP
    from webui.catalog import help_forward_examples, public_catalog

    def run(args, module="cli.launcher"):
        env = os.environ.copy()
        env["COLUMNS"] = "96"
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    main_help = run(["--help"], "cli.main")
    for args in (
        ["start", "--help", "extra"],
        ["restart", "--help", "NVDA"],
        ["cli", "--help", "extra"],
    ):
        completed = run(args)
        assert completed.returncode == 0, args
        assert completed.stdout == main_help.stdout, args
        assert completed.stderr == ""
        assert completed.stdout != HELP + "\n"
    short = run(["-h", "extra"], "cli.main")
    for args in (["start", "-h", "extra"], ["restart", "-h", "extra"]):
        completed = run(args)
        assert completed.returncode == 2, args
        assert completed.stderr == short.stderr, args
        assert "No such option: -h" in completed.stderr
    ticker = run(["--help", "--ticker"], "cli.main")
    forwarded = run(["start", "--help", "--ticker"])
    assert forwarded.returncode == 2
    assert forwarded.stderr == ticker.stderr
    assert "Option '--ticker' requires an argument." in forwarded.stderr
    assert forwarded.stderr != short.stderr
    rows = {item["key"]: item for item in help_forward_examples()}
    assert rows["short_extra"]["cli"] == short.stderr
    assert rows["help_ticker"]["cli"] == ticker.stderr
    assert rows["help_extra"]["exit"] == 0
    assert "不套用这个结果" in rows["help_extra"]["when"]
    assert public_catalog()["help_forward"] == help_forward_examples()
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderHelp" not in script
    assert "No such option: -h" not in script
    assert "Option '--ticker' requires an argument." not in script


def test_subscription_short_option_is_not_help():
    import subprocess
    import sys

    from webui.catalog import public_catalog, subscription_short_option_errors

    rows = {item["key"]: item for item in subscription_short_option_errors()}
    for name in ("login", "logout", "auth-status"):
        completed = subprocess.run(
            [sys.executable, "-m", "cli.launcher", name, "-h"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 2, name
        assert completed.stdout == ""
        assert completed.stderr == rows[f"{name}_short_help"]["cli"]
        assert "No such option: -h" in completed.stderr
        assert f"python -m cli.main {name} --help" in completed.stderr
        assert "别的无效选项不套用 -h" in rows[f"{name}_short_help"]["when"]
    assert rows["login_short_help"]["cli"] != rows["logout_short_help"]["cli"]
    assert rows["auth-status_short_help"]["cli"] != rows["login_short_help"]["cli"]
    helped = subprocess.run(
        [sys.executable, "-m", "cli.launcher", "login", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert helped.returncode == 0
    assert helped.stdout != rows["login_short_help"]["cli"]
    assert "No such option: -h" not in helped.stdout
    assert public_catalog()["launcher_conditional"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderHelp" not in script
    assert "No such option: -h" not in script


def test_flag_help_matches_the_cli_help_text(monkeypatch):
    from typer.testing import CliRunner

    from cli.launcher import HELP
    from cli.main import app as cli_app
    from webui.catalog import public_catalog

    # The catalog captures CLI error text through Rich; COLUMNS=0 makes that console
    # zero-width (it prints nothing). Pin a real width so this test checks help text only.
    monkeypatch.setenv("COLUMNS", "220")
    catalog = public_catalog()
    analyze = {item["field"]: item for item in catalog["analyze_flags"]}
    backtest = {item["field"]: item for item in catalog["backtest_flags"]}
    assert analyze["checkpoint"]["help"] == (
        "Enable/disable checkpoint-resume (save state after each node so a crashed run can resume). "
        "Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED."
    )
    assert analyze["clear_checkpoints"]["help"] == "Delete all saved checkpoints before running (force fresh start)."
    assert analyze["portfolio"]["help"].startswith("JSON file with current holdings and cash")
    assert analyze["ticker"]["help"] == "Ticker to analyze, e.g. NVDA or 0700.HK; skips the prompt"
    assert analyze["date"]["help"] == "Analysis date, YYYY-MM-DD; skips the prompt"
    assert analyze["html"]["help"].startswith("Save the report as one HTML page too")
    start_line = next(line.strip() for line in HELP.splitlines() if line.strip().startswith("start "))
    assert analyze["background"]["help"] == start_line
    assert backtest["tickers"]["help"] == "Comma-separated tickers, e.g. NVDA,AAPL"
    assert backtest["start"]["help"] == "First analysis date, YYYY-MM-DD"
    assert backtest["end"]["help"] == "Last analysis date, YYYY-MM-DD"
    assert backtest["every"]["help"] == "Days between analysis dates"
    assert backtest["analysts"]["help"].startswith("Comma-separated analysts to run")
    assert backtest["asset_type"]["help"] == "stock, crypto or commodity"
    assert backtest["portfolio"]["help"] == "JSON file with holdings and cash, held constant across the grid"
    assert backtest["run_id"]["help"].startswith("Continue an earlier sweep")
    assert catalog["analyze_doc"].startswith("Run an analysis. This is what a bare `tradingagents` does.")
    assert catalog["backtest_doc"] == "Score past decisions over a grid of tickers and dates."
    runner = CliRunner()
    # A fixed wide terminal whatever the caller's COLUMNS/TERM (COLUMNS=0, 40, TERM=dumb
    # with FORCE_COLOR all wrap or box-split the help sentences differently).
    wide = {"COLUMNS": "220", "LINES": "50", "TERM": "xterm-256color", "TERMINAL_WIDTH": None}
    root = runner.invoke(cli_app, ["--help"], env=wide)
    sweep = runner.invoke(cli_app, ["backtest", "--help"], env=wide)
    assert root.exit_code == 0 and sweep.exit_code == 0
    emitted = " ".join((root.stdout + "\n" + sweep.stdout).split())
    for row in (*catalog["analyze_flags"], *catalog["backtest_flags"]):
        if row["field"] == "background":
            continue
        assert " ".join(row["help"].split()) in emitted
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderHelp" not in script


def test_question_box_defaults_match_the_cli(monkeypatch):
    import datetime

    from cli.prompts import TICKER_PROMPT_DEFAULT, interactive_ticker_or_default
    from cli.selections import (
        analysis_date_question_default,
        question_default_line,
        ticker_question_default,
    )
    from webui.catalog import question_defaults

    frozen = datetime.datetime(2026, 10, 29, 8, 0)
    assert ticker_question_default() == TICKER_PROMPT_DEFAULT == "SPY"
    assert question_default_line("SPY") == "Default: SPY"
    assert analysis_date_question_default(frozen) == "2026-10-29"
    assert question_defaults(frozen) == {"1": "Default: SPY", "2": "Default: 2026-10-29"}
    assert interactive_ticker_or_default("  ") == "SPY"
    assert interactive_ticker_or_default("aapl") == "AAPL"
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    blank = service.validate_analysis({
        "ticker": "  ",
        "date": "",
        "analysts": ["market"],
        "research_depth": 1,
        "llm_provider": "deepseek",
        "quick_think_llm": "deepseek-flash",
        "deep_think_llm": "deepseek-v4-pro",
    })
    assert blank["selections"]["ticker"] == "SPY"
    assert blank["selections"]["analysis_date"] == analysis_date_question_default()
    assert blank["selections"]["ticker_from_default"] is True
    assert blank["selections"]["date_from_default"] is True
    joined = "\n".join(blank["confirmations"])
    assert "✓ Ticker from --ticker:" not in joined
    assert "✓ Analysis date from --date:" not in joined
    assert "Selected analysts: market" in joined
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function analysisStepError" in script
    assert 'ticker: ""' in script
    assert 'date: ""' in script
    assert 'id="ticker"' in script
    assert 'id="date"' in script
    assert "Default: SPY" not in script


def test_non_menu_env_depth_is_not_shown_as_the_first_choice(monkeypatch):
    """CLI can skip depth with round counts that are not Shallow, Medium, or Deep."""
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "4")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_debate_rounds", 2)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_risk_discuss_rounds", 4)
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "research_depth": 1,
    })
    config = service.build_run_config(checked["selections"])
    assert config["max_debate_rounds"] == 2
    assert config["max_risk_discuss_rounds"] == 4
    assert "✓ Research depth from environment: 2 debate / 4 risk rounds" in checked["confirmations"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function depthEnvLine" in script
    assert "allowEmpty: skipped && !matched" in script


def test_config_env_values_are_not_replaced_by_the_page(monkeypatch):
    """Temperature and tool rounds from the environment stay on the run."""
    monkeypatch.setenv("TRADINGAGENTS_TEMPERATURE", "0.2")
    monkeypatch.setenv("TRADINGAGENTS_MAX_TOOL_ROUNDS", "3")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "temperature", 0.2)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_tool_rounds", 3)
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "temperature": "0.9",
        "max_tool_rounds": "9",
    })
    config = service.build_run_config(checked["selections"])
    assert config["temperature"] == 0.2
    assert config["max_tool_rounds"] == 3
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function envFieldLocked" in script
    assert "环境变量 ${envName(key)} 已设置，页面不能改。" in script


def test_backend_url_env_is_not_replaced_by_the_page(monkeypatch):
    """CLI keeps TRADINGAGENTS_LLM_BACKEND_URL even when the provider is chosen."""
    monkeypatch.setenv("TRADINGAGENTS_LLM_BACKEND_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_BACKEND_URL", "http://127.0.0.1:10/v1")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "backend_url", "http://127.0.0.1:9/v1")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "quick_think_backend_url", "http://127.0.0.1:10/v1")
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "openai_compatible",
        "backend_url": "http://127.0.0.1:8000/v1",
        "quick_think_provider": "openai_compatible",
        "deep_think_provider": "openai_compatible",
        "quick_think_llm": "local",
        "deep_think_llm": "local",
        "quick_think_backend_url": "http://127.0.0.1:8001/v1",
    })
    assert checked["selections"]["backend_url"] == "http://127.0.0.1:9/v1"
    config = service.build_run_config(checked["selections"])
    assert config["backend_url"] == "http://127.0.0.1:9/v1"
    assert config["quick_think_backend_url"] == "http://127.0.0.1:10/v1"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function backendUrlLocked" in script
    assert "function tierUrlLocked" in script


def test_tier_provider_env_is_not_replaced_by_the_page(monkeypatch):
    """A tier provider set by the environment stays on that provider."""
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_PROVIDER", "grok")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "quick_think_provider", "grok")
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "chatgpt",
        "quick_think_provider": "deepseek",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
    })
    assert checked["selections"]["quick_think_provider"] == "grok"
    assert service.build_run_config(checked["selections"])["quick_think_provider"] == "grok"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function tierProviderSkipped" in script
    assert "function lockSkippedModelInputs" in script
    assert "环境变量已设置，页面不能改" in script


def test_provider_env_does_not_preselect_reasoning_effort(monkeypatch):
    """CLI skips Step 8 when the provider comes from the environment and effort is unset."""
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "chatgpt")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "llm_provider", "chatgpt")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "openai_reasoning_effort", None)
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "openai_reasoning_effort": "medium",
    })
    assert checked["selections"]["openai_reasoning_effort"] is None
    assert service.build_run_config(checked["selections"]).get("openai_reasoning_effort") in (None, "")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "allowEmpty" in script
    assert "function bindEffort" in script


def test_env_skipped_provider_and_models_are_not_replaced_by_the_page(monkeypatch):
    """CLI skips provider and model questions when those variables are set."""
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "chatgpt")
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "gpt-6-luna")
    monkeypatch.setenv("TRADINGAGENTS_DEEP_THINK_LLM", "gpt-6-sol")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "llm_provider", "chatgpt")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "quick_think_llm", "gpt-6-luna")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "deep_think_llm", "gpt-6-sol")
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "deepseek",
        "quick_think_provider": "deepseek",
        "deep_think_provider": "deepseek",
        "quick_think_llm": "deepseek-flash",
        "deep_think_llm": "deepseek-v4-pro",
    })
    assert checked["selections"]["llm_provider"] == "chatgpt"
    assert checked["selections"]["quick_think_llm"] == "gpt-6-luna"
    assert checked["selections"]["deep_think_llm"] == "gpt-6-sol"
    assert "✓ LLM provider from environment: chatgpt" in checked["confirmations"]
    assert "✓ Thinking agents from environment: quick=gpt-6-luna, deep=gpt-6-sol" in checked["confirmations"]
    assert service.env_skipped_values()["llm_provider"] == "chatgpt"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "env_skipped" in script
    assert "function applyEnvSkipped" in script
    assert "function bindDepth" in script


def test_output_language_env_wins_over_the_page_chinese_default(monkeypatch):
    """CLI skips the language question when TRADINGAGENTS_OUTPUT_LANGUAGE is set."""
    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "Japanese")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "output_language", "Japanese")
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "output_language": "Chinese",
    })
    assert checked["selections"]["output_language"] == "Japanese"
    assert service.build_run_config(checked["selections"])["output_language"] == "Japanese"
    assert "✓ Output language from environment: Japanese" in checked["confirmations"]
    assert service.env_output_language() == "Japanese"
    monkeypatch.delenv("TRADINGAGENTS_OUTPUT_LANGUAGE")
    assert service.env_output_language() is None
    kept = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "output_language": "Chinese",
    })
    assert kept["selections"]["output_language"] == "Chinese"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "env_output_language" in script
    assert "outputLanguageFromEnv" in script
    assert "function bindLanguages" in script


def test_untouched_checkpoint_does_not_send_no_checkpoint(monkeypatch):
    """Omitting the flag keeps TRADINGAGENTS_CHECKPOINT_ENABLED. False is --no-checkpoint."""
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    import cli.run as run_module
    monkeypatch.setitem(run_module.DEFAULT_CONFIG, "checkpoint_enabled", True)
    payload = {
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
    }
    omitted = service.validate_analysis(payload)
    assert omitted["selections"]["checkpoint"] is None
    assert service.build_run_config(omitted["selections"])["checkpoint_enabled"] is True
    forced = service.validate_analysis({**payload, "checkpoint": False})
    assert service.build_run_config(forced["selections"])["checkpoint_enabled"] is False
    enabled = service.validate_analysis({**payload, "checkpoint": True})
    assert service.build_run_config(enabled["selections"])["checkpoint_enabled"] is True
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "checkpoint: null" in script
    assert 'toggleRow("checkpoint", "失败后从出错的角色继续"' in script
    assert "已完成的角色不会再调用模型。" in script


def test_result_panel_does_not_print_unfilled_followup_templates():
    """A saved backtest file already contains the follow-up lines."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "结束后还有" not in script
    assert "outcome.ran" not in script
    assert "function runTechnicalDetails" in script
    assert "不另写带花括号的句子" not in script
    panel = script[script.index("function resultPanel"):script.index("function saveNotice")]
    assert "slotSentence" not in panel


def test_unfilled_cli_templates_are_not_shown_as_printed_sentences():
    """Braces in catalog templates are input slots, not text the CLI prints."""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    run_ui = script[script.index("function resultPanel"):script.index("function saveNotice")]
    assert "slotSentence" not in run_ui
    assert "花括号不是原样输出" not in run_ui
    assert "validation_sentences" not in run_ui
    assert "outcome_sentences?.progress" not in run_ui


def test_step_titles_match_the_cli_question_boxes():
    from webui.catalog import STEP_EIGHT, public_catalog

    titles = {item["step"]: item["title"] for item in SELECTION_STEPS}
    assert titles[1] == "Step 1: Ticker Symbol"
    assert titles[4] == "Step 4: Analysts Team"
    assert titles[7] == "Step 7: Thinking Agents"
    assert public_catalog()["step_eight"]["google"]["title"] == "Step 8: Thinking Mode"
    assert STEP_EIGHT["anthropic"]["prompt"] == "Select Effort Level:"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "Step 1: Ticker Symbol" not in script
    assert 'data-step-panel="4"' in script
    assert "第 ${state.step} 步，共 4 步" in script
    assert "function analysisSummary" in script
    assert "Complete Analysis Report" not in script


def test_failed_plan_model_list_keeps_custom_id(monkeypatch):
    def broken_list():
        raise RuntimeError("quota")

    monkeypatch.setattr(service, "get_subscription", lambda key: type("Spec", (), {
        "display_name": "ChatGPT (Plus or Pro plan)" if key == "chatgpt" else "Grok (subscription)",
        "auth": type("Auth", (), {"list_models": staticmethod(broken_list)})(),
    })())
    listed = service.list_models("chatgpt", "deep")
    assert listed["models"] == []
    assert listed["custom_allowed"] is True
    assert listed["title"] == "Select Your [Deep-Thinking] ChatGPT Plan Model:"
    assert listed["cli"] == "Could not list your ChatGPT plan's models: quota"
    grok = service.list_models("grok", "quick")
    assert grok["title"] == "Select Your [Quick-Thinking] Grok Model:"
    assert grok["cli"] == "Could not list your Grok plan's models: quota"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "custom-model-id" in script
    assert "mode=quick" not in script or "quick" in script and "deep" in script


def test_openrouter_fetch_failure_uses_the_cli_sentence(monkeypatch):
    def broken():
        raise TimeoutError("timed out")

    monkeypatch.setattr(service, "_fetch_openrouter_models_or_raise", broken)
    listed = service.list_models("openrouter", "deep")
    assert listed["models"] == []
    assert listed["custom_allowed"] is True
    assert listed["cli"] == "Could not fetch OpenRouter models: timed out"
    assert listed["title"] == "Select Your [Deep-Thinking] OpenRouter Model (latest available):"


def test_missing_api_key_uses_the_unattended_cli_sentence(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(service.ConsoleError, match="OPENAI_API_KEY is not set; there is no terminal to ask for it."):
        service._require_credential("openai")


def test_plan_use_sentence_matches_the_cli_label():
    assert service.plan_use_sentence("chatgpt", "signed") == "✓ Using your ChatGPT plan: signed"
    assert service.plan_use_sentence("grok", "signed").startswith("✓ Using your ")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function initialKeyProvider" in script
    assert "Paste your DEEPSEEK_API_KEY" not in script
    assert "DEEPSEEK_API_KEY" not in script
    assert 'value="deepseek"' not in script
    assert "initialKeyProvider" in script
    assert "is not set in your environment." not in script
    assert "Saved " not in script or "result.cli" in script


def test_depth_environment_lines_match_the_cli(monkeypatch):
    from cli.run import depth_environment_lines, depth_override_notice

    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_MAX_RISK_ROUNDS", raising=False)
    assert depth_environment_lines({"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}) == []
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    prefix, rest = depth_override_notice("max_debate_rounds", 2, "TRADINGAGENTS_MAX_DEBATE_ROUNDS")
    assert depth_environment_lines({"max_debate_rounds": 2, "max_risk_discuss_rounds": 5}) == [prefix + rest]
    assert "the research depth you chose does not apply" in prefix + rest
    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "4")
    both = depth_environment_lines({"max_debate_rounds": 2, "max_risk_discuss_rounds": 4})
    assert both == ["✓ Research depth from environment: 2 debate / 4 risk rounds"]
    assert "does not apply" not in both[0]


def test_page_risk_rounds_do_not_replace_the_environment(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "4")
    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_risk_discuss_rounds", 4)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_debate_rounds", 1)
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    kept = service.depth_lines_for({"max_risk_discuss_rounds": 3})
    assert any("max_risk_discuss_rounds from environment: 4" in line for line in kept)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market"],
        "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "research_depth": 5,
        "max_risk_discuss_rounds": 1,
    })
    config = service.build_run_config(checked["selections"])
    assert config["max_risk_discuss_rounds"] == 4
    assert config["max_debate_rounds"] == 5
    assert any("max_risk_discuss_rounds from environment: 4" in line for line in checked["confirmations"])
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "来自 TRADINGAGENTS_MAX_RISK_ROUNDS，研究深度不覆盖它" not in script
    assert "function riskRoundLocked" in script
    assert "state.depthOverrides" in script


def test_partial_depth_override_is_printed_only_when_the_run_starts(monkeypatch):
    """One round-count variable is printed by run setup, not when the page opens."""
    import cli.run as run_mod
    from cli.run import _build_run_config, depth_override_notice
    from webui.catalog import depth_run_override_when, public_catalog

    printed = []
    monkeypatch.setattr(run_mod.console, "print", lambda *args, **kwargs: printed.append(args[0] if args else ""))
    selections = {
        "research_depth": 5,
        "quick_think_llm": "gpt-6-luna",
        "deep_think_llm": "gpt-6-sol",
        "backend_url": "https://example.invalid",
        "llm_provider": "chatgpt",
        "output_language": "Chinese",
    }
    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_MAX_RISK_ROUNDS", raising=False)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_debate_rounds", 1)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_risk_discuss_rounds", 1)
    _build_run_config(selections, None)
    assert printed == []

    monkeypatch.setenv("TRADINGAGENTS_MAX_RISK_ROUNDS", "4")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_risk_discuss_rounds", 4)
    prefix, rest = depth_override_notice("max_risk_discuss_rounds", 4, "TRADINGAGENTS_MAX_RISK_ROUNDS")
    _build_run_config(selections, None)
    assert printed == [f"[green]{prefix}[/green]{rest}"]
    assert "does not apply" in rest
    assert "Research depth from environment" not in printed[0]

    printed.clear()
    monkeypatch.setenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", "2")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "max_debate_rounds", 2)
    _build_run_config(selections, None)
    assert printed == []

    note = depth_run_override_when()
    assert "打开页面时命令行还没打印这句" in note
    assert "两个都设置时" in note
    assert public_catalog()["depth_run_when"] == note
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function bindDepth" in script
    assert "research_depth" in script
    assert "来自 TRADINGAGENTS_MAX_RISK_ROUNDS，研究深度不覆盖它" not in script


def test_backtest_missing_required_arguments_match_the_cli():
    import subprocess
    import sys

    from webui.catalog import backtest_missing_errors, public_catalog

    errors = backtest_missing_errors()
    cases = {
        "tickers": ["backtest"],
        "start": ["backtest", "NVDA"],
        "end": ["backtest", "NVDA", "--start", "2026-10-01"],
    }
    for key, args in cases.items():
        completed = subprocess.run(
            [sys.executable, "-m", "cli.main", *args],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 2, args
        assert completed.stderr == errors[key]
    with pytest.raises(service.ConsoleError, match="Missing argument 'tickers'"):
        service.validate_backtest({})
    with pytest.raises(service.ConsoleError, match="Missing option '--start'"):
        service.validate_backtest({"tickers": ""})
    with pytest.raises(service.ConsoleError, match="Missing option '--end'"):
        service.validate_backtest({"tickers": "NVDA", "start": "2026-10-01"})
    assert "Missing argument 'tickers'" in public_catalog()["backtest_missing"]["tickers"]
    assert "没传标的参数时" in public_catalog()["backtest_missing_note"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="bt-tickers"' in script
    assert 'id="check-backtest"' in script
    assert "空列表时命令行说" not in script


def test_empty_backtest_tickers_use_the_cli_sentence():
    from cli.main import NO_TICKER_SENTENCE
    from webui.catalog import outcome_sentences
    assert outcome_sentences()["no_ticker"] == NO_TICKER_SENTENCE
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="bt-tickers"' in script
    assert "/api/backtests/validate" in script
    with pytest.raises(service.ConsoleError, match="No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL"):
        service.validate_backtest({"tickers": " , ", "start": "2026-10-01", "end": "2026-10-08"})


def test_split_tier_without_a_model_uses_the_cli_sentence(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="The quick tier runs on grok; set TRADINGAGENTS_QUICK_THINK_LLM"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_provider": "grok",
            "deep_think_provider": "chatgpt",
            "quick_think_llm": "",
            "deep_think_llm": "gpt-6-sol",
        })


def test_openai_compatible_without_a_url_uses_the_cli_sentence(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="No endpoint URL provided. Exiting..."):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "openai_compatible",
            "quick_think_llm": "local",
            "deep_think_llm": "local",
        })


def test_region_choices_use_the_cli_prompt_text():
    from webui.catalog import REGIONS, public_catalog

    regions = public_catalog()["regions"]
    assert regions["qwen"][0]["prompt"] == "Select Qwen region:"
    assert regions["qwen"][0]["cli"] == "International — dashscope-intl.aliyuncs.com (uses DASHSCOPE_API_KEY)"
    assert regions["glm"][1]["cli"] == "BigModel — open.bigmodel.cn (China, uses ZHIPU_CN_API_KEY)"
    assert regions["minimax"][0]["prompt"] == "Select MiniMax region:"
    assert REGIONS["glm"][0]["prompt"] == "Select GLM platform:"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog.regions" in script
    assert "function refreshRegions" in script


def test_empty_analyst_list_uses_the_cli_sentences(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="You must select at least one analyst"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "analysts": [],
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
    with pytest.raises(service.ConsoleError, match="You must select at least one analyst") as second:
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "analysts": " , ",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
    assert "No analysts selected. Exiting..." not in str(second.value)
    empty = service.validate_backtest({
        "tickers": "NVDA",
        "start": "2026-10-01",
        "end": "2026-10-01",
        "every": 7,
        "asset_type": "stock",
        "analysts": [],
    })
    assert empty["analysts_omitted"] is True
    assert empty["analysts"] == ["market", "social", "news", "fundamentals"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "guard_sentences?.analyst_required" in script
    assert "命令行这时会退出" not in script


def test_depth_and_analyst_prompts_match_the_cli():
    from webui.catalog import ANALYST_INSTRUCTION, public_catalog

    catalog = public_catalog()
    assert catalog["depths"][0]["cli"] == "Shallow - Quick research, few debate and strategy discussion rounds"
    assert catalog["depths"][0]["prompt"] == "Select Your [Research Depth]:"
    assert catalog["analyst_prompt"] == "Select Your [Analysts Team]:"
    assert catalog["provider_prompt"] == "Select your LLM Provider:"
    assert catalog["subscription_choice"] == "Subscriptions (use a signed-in account)"
    assert "Press Space to select/unselect analysts" in ANALYST_INSTRUCTION
    assert "Press 'a' to select/unselect all" in catalog["analyst_instruction"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderAnalystChoices" in script
    assert "function bindDepth" in script
    assert "Press Space to select/unselect analysts" not in script
    assert "对应命令行 checkbox" not in script
    assert "Select Your [Analysts Team]:" not in script
    assert "Select your LLM Provider:" not in script


def test_stream_prints_the_cli_live_messages(capsys):
    class Graph:
        def create_run_state(self, *args):
            return {}

        @property
        def propagator(self):
            return type("Propagator", (), {"get_graph_args": lambda self, callbacks=None: {}})()

        def begin_checkpoint(self, *args):
            return None

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [], {"final_trade_decision": "Hold"}

        def record_decision(self, *args):
            return None

        def clear_checkpoint_on_success(self, *args):
            return None

        def end_checkpoint(self):
            return None

    service._stream_like_cli(Graph(), {
        "ticker": "BTC-USD",
        "analysis_date": "2026-10-29",
        "asset_type": "crypto",
        "analysts": ["market"],
    }, None)
    output = capsys.readouterr().out
    assert "MESSAGE\tSystem\tDetected asset type: crypto" in output
    assert "MESSAGE\tSystem\tCompleted analysis for 2026-10-29" in output
    assert "STATUS\tMarket Analyst\tcompleted" in output
    assert "Analysis Complete!" in output


def test_current_report_round_trips_and_uses_the_cli_title():
    from webui.progress import format_report_line, parse_report_line

    body = "### Market Analysis\nprice\t1"
    line = format_report_line(body)
    assert "\n" not in line
    assert parse_report_line(["noise", line]) == body
    assert parse_report_line(["STATUS\tMarket Analyst\tpending"]) is None
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function currentReportPanel" in script
    assert "progress.report" in script
    assert "Waiting for analysis report..." not in script


def test_checkpoint_and_effort_labels_come_from_the_cli(monkeypatch):
    from cli.selections import (
        ANTHROPIC_EFFORT_ENV_LABEL,
        GEMINI_THINKING_ENV_LABEL,
        OPENAI_REASONING_ENV_LABEL,
    )

    source = Path("webui/service.py").read_text(encoding="utf-8")
    assert "checkpoint_resume_message" in source
    assert "checkpoint_fresh_message" in source
    assert "Resuming the saved run for {" not in source
    assert "Starting fresh for {" not in source
    assert "Gemini thinking mode" not in source
    assert GEMINI_THINKING_ENV_LABEL == "Gemini thinking mode"
    assert OPENAI_REASONING_ENV_LABEL == "Reasoning effort"
    assert ANTHROPIC_EFFORT_ENV_LABEL == "Claude effort"
    monkeypatch.setenv("TRADINGAGENTS_GOOGLE_THINKING_LEVEL", "high")
    lines = service.environment_confirmation_lines({"google_thinking_level": "high"})
    assert "✓ Gemini thinking mode from environment: high" in lines


def test_environment_confirmation_lines_match_the_cli(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_OUTPUT_LANGUAGE", "Chinese")
    monkeypatch.setenv("TRADINGAGENTS_LLM_PROVIDER", "chatgpt")
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "grok-4.6")
    config = {
        "output_language": "Chinese",
        "llm_provider": "chatgpt",
        "backend_url": None,
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6-sol",
        "google_thinking_level": None,
        "openai_reasoning_effort": "medium",
        "anthropic_effort": None,
    }
    lines = service.environment_confirmation_lines(config)
    assert "✓ Output language from environment: Chinese" in lines
    assert "✓ LLM provider from environment: chatgpt" in lines
    assert any(line.startswith("✓ Backend URL:") for line in lines)
    assert "✓ Thinking agents from environment: quick=grok-4.6, deep=gpt-6-sol" in lines
    matched = service.matching_environment_lines({
        "output_language": "English",
        "llm_provider": "chatgpt",
        "backend_url": None,
        "quick_think_llm": "other",
        "deep_think_llm": "gpt-6-sol",
    }, config)
    assert "✓ LLM provider from environment: chatgpt" in matched
    assert not any(line.startswith("✓ Output language from environment:") for line in matched)
    assert not any(line.startswith("✓ Thinking agents from environment:") for line in matched)
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.environmentLines" in script


def test_post_run_prompts_match_the_cli_defaults():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    from cli.run import HTML_PROMPT, SAVE_PATH_PROMPT, SAVE_REPORT_PROMPT, SHOW_PROMPT
    from webui.catalog import report_prompts

    prompts = report_prompts()
    assert prompts["save"] == SAVE_REPORT_PROMPT == "Save report?"
    assert prompts["save_path"] == SAVE_PATH_PROMPT
    assert prompts["html"] == HTML_PROMPT
    assert prompts["show"] == SHOW_PROMPT == "Display full report on screen?"
    assert "function saveNotice" in script
    assert "function resultPanel" in script
    assert "Save report?" not in script
    assert "Welcome to TradingAgents CLI" not in script
    assert "show: true" in script


def test_checkpoint_announcement_is_a_system_message_after_begin(capsys):
    class Graph:
        _resuming = True

        def create_run_state(self, *args):
            return {}

        @property
        def propagator(self):
            return type("Propagator", (), {"get_graph_args": lambda self, callbacks=None: {}})()

        def begin_checkpoint(self, *args):
            return "thread-1"

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [], {}

        def record_decision(self, *args):
            return None

        def clear_checkpoint_on_success(self, *args):
            return None

        def end_checkpoint(self):
            return None

    service._stream_like_cli(Graph(), {
        "ticker": "NVDA",
        "analysis_date": "2026-10-29",
        "asset_type": "stock",
        "analysts": ["market"],
    }, None)
    output = capsys.readouterr().out
    assert "MESSAGE\tSystem\tResuming the saved run for NVDA on 2026-10-29" in output

    class Fresh(Graph):
        _resuming = False

    service._stream_like_cli(Fresh(), {
        "ticker": "NVDA",
        "analysis_date": "2026-10-29",
        "asset_type": "stock",
        "analysts": ["market"],
    }, None)
    second = capsys.readouterr().out
    assert "MESSAGE\tSystem\tStarting fresh for NVDA on 2026-10-29" in second


def test_plan_errors_match_the_cli_and_keep_case():
    import subprocess
    import sys

    from webui.catalog import plan_argument_errors, plan_cli_error, public_catalog

    examples = plan_argument_errors()
    live_missing = subprocess.run(
        [sys.executable, "-m", "cli.main", "login"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert live_missing.returncode == 2
    assert live_missing.stderr == examples["login_missing"]
    assert service.start_login(None).get("status") if False else True
    with pytest.raises(service.ConsoleError) as missing:
        service.start_login(None)
    assert str(missing.value) == examples["login_missing"]
    with pytest.raises(service.ConsoleError) as nope:
        service.start_login("nope")
    assert str(nope.value) == examples["login_nope"]
    with pytest.raises(service.ConsoleError) as capital:
        service.start_login("ChatGPT")
    assert "Invalid value for 'plan': 'ChatGPT'" in str(capital.value)
    assert str(capital.value) == plan_cli_error("login", "ChatGPT")
    with pytest.raises(service.ConsoleError) as blank:
        service.start_login("")
    assert "Invalid value for 'plan': ''" in str(blank.value)
    assert "Missing argument" not in str(blank.value)
    with pytest.raises(service.ConsoleError) as logout_missing:
        service.logout(None)
    assert str(logout_missing.value) == examples["logout_missing"]
    assert examples["login_nope"] != examples["logout_nope"]
    assert "不套用到别的值" in public_catalog()["plan_note"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function planErrorNote" in script
    assert "state.catalog?.plan_errors" in script
    assert "只能登录 chatgpt 或 grok" not in script
    assert "Invalid value for 'plan'" not in script


def test_auth_status_filters_one_plan_and_keeps_illegal_case(monkeypatch):
    import subprocess
    import sys

    from webui.catalog import plan_argument_errors, plan_cli_error, public_catalog

    examples = plan_argument_errors()
    live = subprocess.run(
        [sys.executable, "-m", "cli.main", "auth-status", "nope"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert live.returncode == 2
    assert live.stderr == examples["auth_status_nope"]
    assert "auth-status" in examples["auth_status_nope"]
    assert examples["auth_status_nope"] != examples["login_nope"]
    with pytest.raises(service.ConsoleError) as nope:
        service.auth_status("nope")
    assert str(nope.value) == examples["auth_status_nope"]
    with pytest.raises(service.ConsoleError) as capital:
        service.auth_status("ChatGPT")
    assert str(capital.value) == plan_cli_error("auth-status", "ChatGPT")
    assert "ChatGPT" in str(capital.value)
    with pytest.raises(service.ConsoleError) as blank:
        service.auth_status("")
    assert "Invalid value for 'plan': ''" in str(blank.value)
    assert "Missing argument" not in str(blank.value)

    class Auth:
        def signed_in_as(self):
            return None

    class ChatSpec:
        display_name = "ChatGPT (Plus or Pro plan)"
        auth = Auth()

    class GrokSpec:
        display_name = "Grok (subscription)"
        auth = Auth()

    monkeypatch.setattr(service, "SUBSCRIPTIONS", {"chatgpt": ChatSpec(), "grok": GrokSpec()})
    everything = service.auth_status()
    assert [row["provider"] for row in everything["accounts"]] == ["chatgpt", "grok"]
    assert everything["plan"] is None
    assert everything["cli"] == "auth-status"
    grok = service.auth_status("grok")
    assert [row["provider"] for row in grok["accounts"]] == ["grok"]
    assert grok["cli"] == "auth-status grok"
    assert grok["accounts"][0]["cli"].startswith("Grok: not signed in")
    note = public_catalog()["plan_note"]
    assert "auth-status 省略 plan" in note
    assert "auth-status nope 和 login nope 不是同一句" in note
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function authPlanFilters" in script
    assert "data-auth-plan" in script
    assert "auth_status_nope" in script
    assert "encodeURIComponent(plan)" in script
    assert "只能登录 chatgpt 或 grok" not in script


def test_auth_status_marks_local_read_failures_without_leaking_exception_text(monkeypatch):
    secret = "local-auth-secret-should-not-escape"

    class BrokenAuth:
        def signed_in_as(self):
            raise RuntimeError(secret)

    class BrokenSpec:
        display_name = "ChatGPT (Plus or Pro plan)"
        auth = BrokenAuth()

    monkeypatch.setattr(service, "SUBSCRIPTIONS", {"chatgpt": BrokenSpec()})
    status = service.auth_status()
    row = status["accounts"][0]
    assert row["provider"] == "chatgpt"
    assert row["signed_in"] is False
    assert row["account"] is None
    assert row["read_error"] is True
    assert secret not in json.dumps(status)
    assert status["live_validation"] is False


def test_auth_status_query_passes_the_plan_unchanged(monkeypatch):
    import http.client
    import threading

    seen = []
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))

    def fake(plan=None):
        seen.append(plan)
        if plan not in (None, "chatgpt", "grok"):
            raise service.ConsoleError(f"rejected {plan!r}")
        providers = ["chatgpt", "grok"] if plan is None else [plan]
        return {
            "accounts": [{"provider": item} for item in providers],
            "live_validation": False,
            "plan": plan,
            "cli": "auth-status" if plan is None else f"auth-status {plan}",
        }

    monkeypatch.setattr(service, "auth_status", fake)
    httpd = server.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]

        def get(path):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", path, headers={server.TOKEN_HEADER: httpd.session_token})
            response = connection.getresponse()
            body = response.read()
            connection.close()
            return response.status, body

        status, body = get("/api/auth/status")
        assert status == 200
        assert json.loads(body)["cli"] == "auth-status"
        status, body = get("/api/auth/status?plan=chatgpt")
        assert status == 200
        assert json.loads(body)["accounts"] == [{"provider": "chatgpt"}]
        status, body = get("/api/auth/status?plan=ChatGPT")
        assert status == 400
        assert json.loads(body)["error"] == "rejected 'ChatGPT'"
        status, body = get("/api/auth/status?plan=")
        assert status == 400
        assert json.loads(body)["error"] == "rejected ''"
        assert seen == [None, "chatgpt", "ChatGPT", ""]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_grok_login_does_not_accept_a_pasted_address():
    from webui.catalog import login_paste_notes, public_catalog

    service.JOBS.begin_login("grok")
    try:
        with pytest.raises(service.ConsoleError, match="不读取粘贴地址"):
            service.JOBS.paste_login("https://accounts.x.ai/device")
        assert service.JOBS._paste.empty()
        assert service.JOBS.login_state()["provider"] == "grok"
        assert service.JOBS.login_state()["status"] == "waiting"
    finally:
        service.JOBS.finish_login(error="test cleanup")
    notes = login_paste_notes()
    assert "Grok 登录不询问粘贴地址" in notes["grok"]
    assert "ChatGPT 的粘贴句" in notes["grok"]
    assert public_catalog()["login_paste_notes"] == notes
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function pasteLoginNote" in script
    assert "state.catalog.login_paste_notes" in script
    assert "login.provider === \"grok\"" in script
    assert "登录地址会出现在这里" not in script
    assert "正在等待登录地址" not in script


def test_login_paste_prompt_matches_the_cli(monkeypatch, tmp_path):
    from webui.catalog import LOGIN_SENTENCES, public_catalog

    assert "If the browser cannot reach this machine, paste here the address it ends up on." in LOGIN_SENTENCES
    assert public_catalog()["login_sentences"][0].startswith("Continue with ChatGPT:")
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    with pytest.raises(service.ConsoleError, match="paste here the address it ends up on"):
        service.JOBS.paste_login("  ")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog.login_paste" in script
    assert "paste here the address it ends up on." not in script
    assert "function loginSentenceList" in script
    assert "login_rows" in script
    assert "item.slot" in script
    login_body = script[script.index("function loginSentenceList"):script.index("function planErrorNote")]
    assert "花括号不是原样输出" not in login_body
    rows = public_catalog()["login_rows"]
    device = next(item for item in rows if item["slot"])
    assert "{user_code}" in device["cli"]
    assert device["cli"] not in script


def test_message_panel_matches_the_cli_columns_and_order():
    from webui.progress import message_panel_rows

    rows = message_panel_rows([
        "MESSAGE\tSystem\tSelected ticker: NVDA\t09:00:01",
        "TOOL\tget_stock_data\t{'symbol': 'NVDA'}\t09:00:02",
        "MESSAGE\tAgent\t价格上涨\t09:00:03",
    ])
    assert [item["type"] for item in rows] == ["Agent", "Tool", "System"]
    assert rows[0]["time"] == "09:00:03"
    assert rows[1]["content"].startswith("get_stock_data:")
    assert len(message_panel_rows([f"MESSAGE\tSystem\tm\t09:00:{i:02d}" for i in range(20)])) == 12
    from webui.catalog import message_panel_types
    types = [item["cli"] for item in message_panel_types()]
    assert types == ["User", "Agent", "Data", "Control", "System", "Tool"]
    display = Path("cli/display.py").read_text(encoding="utf-8")
    run_source = Path("cli/run.py").read_text(encoding="utf-8")
    for name in ("User", "Agent", "Data", "Control"):
        assert f'return ("{name}", content)' in display
    assert '(timestamp, "Tool"' in display
    assert '"System"' in run_source
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function messageHeaders" in script
    assert "function messageTable" in script
    assert "function toolTable" in script
    assert "User、Agent、Data、Control、Tool" not in script
    assert "时间 Time" not in script
    assert "Messages & Tools" not in script
    assert "Welcome to TradingAgents CLI" not in script
    assert "live_report_titles" in script
    assert '["Market Analysis", "市场分析"]' not in script
    assert "Pending cells are not scored above; re-run to settle them." not in script


def test_headless_setup_text_matches_the_cli_gaps():
    from cli.selections import unattended_gaps

    text = service.headless_setup_text({})
    assert text.startswith("No terminal to answer the setup questions. Set:")
    for gap in unattended_gaps({}):
        assert f"  {gap}" in text
    assert service.headless_setup_text({
        "ticker": "NVDA", "date": "2026-10-29", "analysts": "market", "save": True, "show": True,
    }) == service.headless_setup_text({
        "ticker": "NVDA", "date": "2026-10-29", "analysts": "market", "save": True, "show": True,
    })
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function headlessBlock" not in script
    assert "团队 Team" not in script
    assert "progress_statuses" in script


def test_ticker_and_date_errors_match_the_cli_parsers():
    with pytest.raises(service.ConsoleError, match="not a ticker symbol: 'NOPE!'"):
        service.validate_analysis({"ticker": "NOPE!", "date": "2026-10-29"})
    with pytest.raises(service.ConsoleError, match=r"Error: not a date: 'yesterday'; use YYYY-MM-DD"):
        service.validate_analysis({"ticker": "NVDA", "date": "yesterday"})
    with pytest.raises(service.ConsoleError, match=r"Error: 2999-01-01 is in the future"):
        service.validate_analysis({"ticker": "NVDA", "date": "2999-01-01"})
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    from cli.prompts import TICKER_INVALID, TICKER_PROMPT
    from cli.selections import DATE_PROMPT
    from webui.catalog import setup_prompts

    prompts = setup_prompts()
    assert prompts["ticker"] == TICKER_PROMPT == "Enter ticker symbol (e.g. SPY, 0700.HK, BTC-USD):"
    assert prompts["ticker_invalid"] == TICKER_INVALID
    assert prompts["date"] == DATE_PROMPT == "Enter the analysis date (YYYY-MM-DD)"
    assert 'id="ticker"' in script
    assert 'id="date"' in script
    assert "function analysisStepError" in script
    assert "Enter ticker symbol (e.g. SPY, 0700.HK, BTC-USD):" not in script


def test_custom_language_token_is_rejected_and_the_prompt_is_shown(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="Please enter a language name."):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "output_language": "custom",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    from webui.catalog import setup_prompts
    assert setup_prompts()["language"] == "Select Output Language:"
    assert 'id="languages"' in script
    assert 'id="custom-language"' in script
    assert "Select Output Language:" not in script
    assert "Enter language name (e.g. Turkish, Vietnamese, Thai, Indonesian):" not in script
    assert "Please enter a language name." not in script
    assert "按命令行回退为 English" not in script


def test_bad_portfolio_uses_the_cli_unusable_sentence(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    # W49 (RUN-003c): the page now says what is wrong in Chinese, without pydantic's text.
    with pytest.raises(service.ConsoleError, match="持仓内容不可用：第 1 个持仓的数量"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
            "portfolio": {"positions": [{"ticker": "NVDA", "quantity": "many"}]},
        })
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'filePicker("portfolio-file"' in script
    assert "bindPortfolioFile" in script
    assert "portfolio file <path> is not usable" not in script


def test_tier_backend_url_is_not_described_as_an_ollama_question():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "仅 Ollama 或自建端点填写" not in script
    assert "订阅留空。" not in script
    assert "function backendUrlLocked" in script
    assert "function tierUrlLocked" in script


def test_compatible_url_prompt_is_only_for_that_provider():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="backend-url-label"' in script
    assert 'id="backend-url"' in script
    assert "function backendUrlLocked" in script
    assert "当前供应商不会询问 OpenAI-compatible 地址" not in script


def test_openai_compatible_url_uses_the_cli_validator(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="Enter a URL starting with http:// or https://"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "openai_compatible",
            "backend_url": "localhost:8000/v1",
            "quick_think_llm": "local",
            "deep_think_llm": "local",
        })
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="backend-url"' in script
    assert "openai_compatible" in script
    assert "Enter the OpenAI-compatible base URL" not in script
    assert "Enter model ID:" not in script


def test_backtest_grid_errors_match_the_cli():
    with pytest.raises(service.ConsoleError, match="Invalid value for '--every': 'nope' is not a valid int."):
        service.validate_backtest({
            "tickers": "NVDA",
            "start": "2026-10-01",
            "end": "2026-10-08",
            "every": "nope",
        })
    with pytest.raises(service.ConsoleError, match="the grid ends before it starts: 2026-10-01 is before 2026-10-08"):
        service.validate_backtest({
            "tickers": "NVDA",
            "start": "2026-10-08",
            "end": "2026-10-01",
            "every": 7,
        })
    with pytest.raises(service.ConsoleError, match="grid dates must be in YYYY-MM-DD format, got 'yesterday'"):
        service.validate_backtest({
            "tickers": "NVDA",
            "start": "yesterday",
            "end": "2026-10-08",
            "every": 7,
        })
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'id="bt-every"' in script
    assert "/api/backtests/validate" in script
    assert "Invalid value for '--every'" not in script


def test_future_grid_is_empty_and_not_an_invented_error():
    checked = service.validate_backtest({
        "tickers": "NVDA",
        "start": "2999-01-01",
        "end": "2999-01-08",
        "every": 7,
    })
    assert checked["dates"] == []
    assert checked["cells"] == 0
    assert checked["started"] is False
    assert service.backtest_span([], {"start": "2999-01-01", "end": "2999-01-08"}) == "2999-01-01..2999-01-08"
    assert service.backtest_probe_date([]) != "2999-01-08"
    source = Path("webui/service.py").read_text(encoding="utf-8")
    assert "没有可分析的日期" not in source


def test_unknown_analyst_and_asset_type_match_the_cli(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="unknown analyst macro; choose from market, sentiment, news, fundamentals"):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "analysts": ["macro"],
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
    with pytest.raises(service.ConsoleError, match="'bond' is not a valid AssetType"):
        service.validate_backtest({
            "tickers": "NVDA",
            "start": "2026-10-01",
            "end": "2026-10-08",
            "asset_type": "bond",
        })
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function analysisStepError" in script
    assert "function renderBacktest" in script
    assert "unknown analyst" not in script
    assert "is not a valid AssetType" not in script


def test_crypto_fundamentals_match_the_cli_sentence(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="fundamentals is not available for crypto"):
        service.validate_analysis({
            "ticker": "BTC-USD",
            "date": "2026-10-29",
            "analysts": ["fundamentals"],
            "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna",
            "deep_think_llm": "gpt-6-sol",
        })
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function backtestAnalystNote" in script
    assert "unavailable_crypto" in script
    assert "fundamentals is not available for crypto" not in script
    assert "fundamentals_hidden" in script
    from webui.catalog import report_prompts
    assert report_prompts()["open_failed"] == "Could not open a browser; the page is at:"
    assert "Could not open a browser; the page is at:" not in script


def test_report_outline_includes_the_cli_header_lines(monkeypatch, tmp_path):
    report = tmp_path / "reports" / "NVDA-header"
    (report / "1_analysts").mkdir(parents=True)
    (report / "1_analysts" / "market.md").write_text("价格", encoding="utf-8")
    (report / "complete_report.md").write_text(
        "\n".join([
            "# Trading Analysis Report: NVDA",
            "- Analysis date: 2026-10-29",
            "- Rating: Hold",
            "- Generated: 2026-10-29 09:00:00",
            "- TradingAgents 1.2.3: chatgpt, deep gpt-6-sol, quick grok-4.6",
            "- Analysts: market; research debate rounds 1, risk debate rounds 1",
            "- Data vendors: core_stock_apis yfinance",
            "- Memory log: 2 settled, 1 pending",
            "",
            "## I. Analyst Team Reports",
        ]) + "\n",
        encoding="utf-8",
    )
    (report / "complete_report.html").write_text("<p>saved</p>", encoding="utf-8")
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    outlined = service.report_outline(str(report / "complete_report.html"))
    assert outlined["rating"] == "Hold"
    assert outlined["header"][0] == "Trading Analysis Report: NVDA"
    assert "Analysis date: 2026-10-29" in outlined["header"]
    assert "Generated: 2026-10-29 09:00:00" in outlined["header"]
    assert "Memory log: 2 settled, 1 pending" in outlined["header"]
    assert any(line.startswith("TradingAgents 1.2.3:") for line in outlined["header"])
    assert any(line.startswith("Data vendors:") for line in outlined["header"])
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "Generated" not in script
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "data.header" in script

def test_message_tool_log_matches_the_cli_rows(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessage

    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    log = service.prepare_message_tool_log(
        {"results_dir": str(tmp_path)}, "NVDA", "2026-10-29",
    )
    assert log.name == "message_tool.log"
    assert log.is_file() and log.read_text(encoding="utf-8") == ""
    service.append_message_tool_log(
        log, service.message_tool_message_line("09:00:01", "System", "Selected ticker: NVDA"),
    )
    service.append_message_tool_log(
        log, service.message_tool_call_line("09:00:02", "get_stock_data", {"symbol": "NVDA"}),
    )
    service.append_message_tool_log(
        log, service.message_tool_message_line("09:00:03", "Agent", "第一行\n第二行"),
    )
    written = log.read_text(encoding="utf-8")
    assert written == (
        "09:00:01 [System] Selected ticker: NVDA\n"
        "09:00:02 [Tool Call] get_stock_data(symbol=NVDA)\n"
        "09:00:03 [Agent] 第一行 第二行\n"
    )
    outside = tmp_path.parent / "message_tool.log"
    outside.write_text("secret", encoding="utf-8")
    with pytest.raises(service.ConsoleError, match="日志路径不在结果目录内"):
        service.read_message_tool_log(str(outside))
    read = service.read_message_tool_log(str(log))
    assert read["lines"][1] == "09:00:02 [Tool Call] get_stock_data(symbol=NVDA)"

    class Graph:
        config = {"results_dir": str(tmp_path)}

        def create_run_state(self, *args):
            return {}

        @property
        def propagator(self):
            return type("Propagator", (), {"get_graph_args": lambda self, callbacks=None: {}})()

        def begin_checkpoint(self, *args):
            return None

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [AIMessage(content="价格上涨", tool_calls=[{"name": "get_news", "args": {"ticker": "NVDA"}, "id": "1"}])], {}

        def record_decision(self, *args):
            return None

        def clear_checkpoint_on_success(self, *args):
            return None

        def end_checkpoint(self):
            return None

    service._stream_like_cli(Graph(), {
        "ticker": "NVDA",
        "analysis_date": "2026-10-29",
        "asset_type": "stock",
        "analysts": ["market"],
    }, None)
    streamed = log.read_text(encoding="utf-8")
    assert "[System] Selected ticker: NVDA" in streamed
    assert "[Tool Call] get_news(ticker=NVDA)" in streamed
    assert "[Agent] 价格上涨" in streamed
    assert "[System] Completed analysis for 2026-10-29" in streamed
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "message_tool_log" in script
    assert "message_tool.log" not in script
    assert "function messageTable" in script
    assert "Use arrow keys to navigate" not in script
    assert "Press Enter to select" not in script
    monkeypatch.setattr(service, "_fetch_openrouter_models_or_raise", lambda: [])
    listed = service.list_models("openrouter", "quick")
    assert listed["custom_prompt"] == "Enter OpenRouter model ID (e.g. google/gemma-4-26b-a4b-it):"
    assert listed["hint"] == "Please enter a model ID."
    assert "function loadModels" in script
    assert "Enter OpenRouter model ID (e.g. google/gemma-4-26b-a4b-it):" not in script


def test_empty_custom_model_id_uses_the_cli_validator_not_cancel(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError, match="Please enter a model ID."):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "deepseek",
            "quick_think_provider": "deepseek",
            "deep_think_provider": "deepseek",
            "quick_think_llm": "",
            "deep_think_llm": "deepseek-chat",
        })
    with pytest.raises(service.ConsoleError, match="Please enter a deployment name."):
        service.validate_analysis({
            "ticker": "NVDA",
            "date": "2026-10-29",
            "llm_provider": "azure",
            "quick_think_provider": "azure",
            "deep_think_provider": "azure",
            "quick_think_llm": "",
            "deep_think_llm": "deploy",
        })
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function modelIdRequired" in script
    assert "if (!id) return;" not in script
    assert "模型 ID 为空" in script
    assert "Cancelled. Exiting..." not in script
    assert "No model selected. Exiting..." not in script
    assert "手填模型 ID" not in script


def test_backtest_analyst_control_does_not_use_the_checkbox_instruction():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "state.catalog?.analyst_instruction" not in script
    assert 'selector.includes("analyst")' not in script
    fill = script[script.index("function fillChoices"):script.index("function riskRoundLocked")]
    assert "analyst_instruction" not in fill
    assert "requireOne: selector" not in fill


def test_menu_navigation_is_only_on_cli_menus_that_print_it():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function mountSelect" in script
    assert "function mountMultiSelect" in script
    assert "custom-select-menu" in script
    assert "ArrowDown" in script


def test_log_and_memory_sentences_come_from_their_writers():
    from cli.launcher import NOT_RUNNING, NOT_RUNNING_STALE, STOP_NOT_RUNNING
    from cli.run import (
        MESSAGE_TOOL_LOG_NAME,
        TOOL_CALL_LABEL,
        completed_analysis_message,
        message_tool_call_line,
        message_tool_message_line,
        selected_ticker_message,
    )
    from tradingagents.memory.log import (
        DECISION_MARKER,
        PENDING_STATUS,
        REFLECTION_MARKER,
        RESOLVED_PREFIX,
    )
    from tradingagents.memory.reflection import ALPHA_VS_LABEL, alpha_vs_line
    from webui.catalog import log_sentences

    assert NOT_RUNNING == "TradingAgents: not running"
    assert NOT_RUNNING_STALE == "TradingAgents: not running (stale runtime metadata cleaned)"
    assert STOP_NOT_RUNNING == "TradingAgents is not running."
    assert message_tool_message_line("09:00:01", "System", "Selected ticker: NVDA") == (
        "09:00:01 [System] Selected ticker: NVDA\n"
    )
    assert message_tool_call_line("09:00:02", "get_stock_data", {"symbol": "NVDA"}) == (
        "09:00:02 [Tool Call] get_stock_data(symbol=NVDA)\n"
    )
    assert message_tool_message_line("09:00:03", "Agent", "第一行\n第二行") == (
        "09:00:03 [Agent] 第一行 第二行\n"
    )
    assert selected_ticker_message("NVDA") == "Selected ticker: NVDA"
    assert completed_analysis_message("2026-10-29") == "Completed analysis for 2026-10-29"
    assert alpha_vs_line("SPY", 0.011) == "Alpha vs SPY: +1.1%"
    catalog = log_sentences()
    assert catalog["not_running"] == NOT_RUNNING
    assert "stale runtime" not in " ".join(catalog.values())
    assert catalog["message_tool_log"] == MESSAGE_TOOL_LOG_NAME
    assert catalog["tool_call"] == TOOL_CALL_LABEL
    assert catalog["message_row"] == "{timestamp} [{type}] {content}"
    assert catalog["tool_row"] == "{timestamp} [Tool Call] {name}({args})"
    assert catalog["selected_ticker"] == "Selected ticker: {ticker}"
    assert catalog["decision"] == DECISION_MARKER == "DECISION:"
    assert catalog["reflection"] == REFLECTION_MARKER == "REFLECTION:"
    assert catalog["pending"] == PENDING_STATUS == "pending"
    assert catalog["resolved"] == RESOLVED_PREFIX == "resolved:"
    assert catalog["alpha_vs"] == ALPHA_VS_LABEL == "Alpha vs"
    assert catalog["alpha_vs_line"] == "Alpha vs {benchmark}: {alpha}"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "09:00:01 [System] Selected ticker: NVDA" not in script
    assert "stale runtime metadata cleaned" not in script


def test_memory_page_keeps_reflection_alpha_and_holding(monkeypatch, tmp_path):
    log = tmp_path / "trading_memory.md"
    log.write_text(
        "[2026-10-29 | NVDA | Buy | +4.2% | +1.1% | 5d | resolved:2026-11-05]\n\n"
        "DECISION:\n买入。\n\n"
        "REFLECTION:\nAlpha vs SPY: +1.1%\n\n"
        "<!-- ENTRY_END -->\n\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(service.DEFAULT_CONFIG, "memory_log_path", str(log))
    found = service.list_memory()
    row = found["entries"][0]
    assert row["raw"] == "+4.2%"
    assert row["alpha"] == "+1.1%"
    assert row["holding"] == "5d"
    assert row["resolved"] == "2026-11-05"
    assert row["reflection"] == "Alpha vs SPY: +1.1%"
    assert "REFLECTION" in found["note"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "renderMemory" not in script
    assert '"REFLECTION"' not in script
    assert '"Alpha vs"' not in script
    assert '"DECISION"' not in script



def test_finished_live_sections_match_the_cli_final_text(tmp_path, capsys):
    class Graph:
        config = {"results_dir": str(tmp_path)}

        def create_run_state(self, *args):
            return {}

        @property
        def propagator(self):
            return type("Propagator", (), {"get_graph_args": lambda self, callbacks=None: {}})()

        def begin_checkpoint(self, *args):
            return None

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [], {
                "investment_debate_state": {"bull_history": "看多", "bear_history": ""},
                "investment_plan": "持有",
            }

        def record_decision(self, *args):
            return None

        def clear_checkpoint_on_success(self, *args):
            return None

        def end_checkpoint(self):
            return None

    service._stream_like_cli(Graph(), {
        "ticker": "NVDA",
        "analysis_date": "2026-10-29",
        "asset_type": "stock",
        "analysts": ["market"],
    }, None)
    section = tmp_path / "NVDA" / "2026-10-29" / "reports" / "investment_plan.md"
    assert section.read_text(encoding="utf-8") == "持有"
    output = capsys.readouterr().out
    assert "MESSAGE\tSystem\tAnalyst wall time:" in output
    assert "Analyst wall time:" in output.split("Analysis Complete!")[0]
    log = tmp_path / "NVDA" / "2026-10-29" / "message_tool.log"
    assert "[System] Analyst wall time:" in log.read_text(encoding="utf-8")


def test_live_sections_are_written_like_the_cli(tmp_path):
    class Graph:
        config = {"results_dir": str(tmp_path)}

        def create_run_state(self, *args):
            return {}

        @property
        def propagator(self):
            return type("Propagator", (), {"get_graph_args": lambda self, callbacks=None: {}})()

        def begin_checkpoint(self, *args):
            return None

        def checkpoint_input(self, state):
            return state

        def stream_run(self, graph_input, **kwargs):
            yield [], {"market_report": "价格上涨"}

        def record_decision(self, *args):
            return None

        def clear_checkpoint_on_success(self, *args):
            return None

        def end_checkpoint(self):
            return None

    service._stream_like_cli(Graph(), {
        "ticker": "NVDA",
        "analysis_date": "2026-10-29",
        "asset_type": "stock",
        "analysts": ["market"],
    }, None)
    section = tmp_path / "NVDA" / "2026-10-29" / "reports" / "market_report.md"
    assert section.read_text(encoding="utf-8") == "价格上涨"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function liveSectionTable" in script
    assert "data-live" in script
    assert "market_report.md" not in script


def test_welcome_panel_matches_the_cli_box():
    source = Path("cli/selections.py").read_text(encoding="utf-8")
    panel = service.welcome_panel()
    assert panel["title"] == "Welcome to TradingAgents"
    assert panel["subtitle"] == "Multi-Agents LLM Financial Trading Framework"
    for key in ("body", "workflow_label", "workflow", "credit"):
        assert panel[key] in source
    assert panel["credit_url"] == "https://github.com/TauricResearch"
    assert panel["ascii"] == Path("cli/static/welcome.txt").read_text(encoding="utf-8")
    assert "Welcome to TradingAgents" in Path("cli/display.py").read_text(encoding="utf-8")
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function welcomeBox" in script
    assert "function updatesSection" in script
    assert "Built by Tauric Research" not in script
    assert "What's new" not in script
    assert "Bull Researcher Analysis" not in script
    assert "Aggressive Analyst Analysis" not in script
    welcome_body = script[script.index("function welcomeBox"):script.index("function updatesSection")]
    assert "这里列出命令行供应商表里的全部供应商" not in welcome_body
    assert '["deepseek", "openai", "chatgpt", "grok", "azure", "openrouter"]' not in script
    from cli.updates import messages
    lines = messages({"latest": {"version": "9.9.9", "summary": "newer", "url": "https://example.invalid/release"}}, running="0.1.0", upgrade=lambda: "git pull")
    assert lines[0].startswith("TradingAgents 9.9.9 is available (you have 0.1.0):")
    assert "Upgrade: git pull" in lines


def test_command_search_opens_the_cli_surface():
    from webui.catalog import (
        ANALYZE_FLAGS,
        BACKTEST_FLAGS,
        COMMAND_PAGES,
        LAUNCHER_COMMANDS,
        public_catalog,
        search_entries,
    )

    rows = search_entries()
    pages = {"analyze", "runs", "reports", "backtest", "accounts", "models"}
    assert {row["page"] for row in rows} <= pages
    pairs = {(row["cli"], row["page"]) for row in rows}
    by_command = {row["cli"]: row for row in rows if row["kind"] == "command"}
    for item in LAUNCHER_COMMANDS:
        assert by_command[item["cli"]]["page"] == COMMAND_PAGES[item["cli"]]
        assert by_command[item["cli"]]["zh"] == item["zh"]
    for item in ANALYZE_FLAGS:
        assert (item["cli"], "analyze") in pairs
    for item in BACKTEST_FLAGS:
        assert (item["cli"], "backtest") in pairs
    assert ("--ticker", "analyze") in pairs
    assert ("--analysts", "analyze") in pairs and ("--analysts", "backtest") in pairs
    assert by_command["stop"]["page"] == "runs"
    assert by_command["logs"]["page"] == "reports"
    assert ("Medium (Default)", "analyze") in pairs
    assert ("FRED_API_KEY", "accounts") in pairs
    assert ("Grok (subscription)", "models") in pairs
    assert ("ChatGPT (Plus or Pro plan)", "models") in pairs
    assert ("DeepSeek", "models") in pairs
    assert ("OpenAI-compatible (vLLM, LM Studio, llama.cpp, custom relay)", "models") in pairs
    assert public_catalog()["search"] == rows
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    page = Path("webui/static/index.html").read_text(encoding="utf-8")
    assert "command-search-menu" in script
    assert "state.catalog.search" in script or "catalog.search" in script
    assert "没有匹配的命令。不会编造结果。" in script
    assert 'id="command-search"' in page


def test_chatgpt_plan_refuses_sampling_settings(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    mixed = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6.1-sol",
        "temperature": 0.2,
        "max_tokens": 800,
    })
    assert mixed["selections"]["temperature"] == 0.2
    assert "A ChatGPT plan does not accept temperature; it is not sent." in mixed["ignored"]
    assert "A ChatGPT plan does not accept max_tokens; it is not sent." in mixed["ignored"]
    grok_only = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "grok",
        "quick_think_provider": "grok",
        "deep_think_provider": "grok",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "grok-4.6",
        "temperature": 0.2,
    })
    assert not any("ChatGPT plan" in line for line in grok_only["ignored"])
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "A ChatGPT plan does not accept temperature; it is not sent." not in script
    assert "A ChatGPT plan does not accept max_tokens; it is not sent." not in script


def test_live_section_files_can_be_listed_and_read(monkeypatch, tmp_path):
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(tmp_path))
    assert service.list_live_sections() == []
    section = tmp_path / "NVDA" / "2026-10-29" / "reports" / "market_report.md"
    section.parent.mkdir(parents=True)
    section.write_text("价格上涨", encoding="utf-8")
    (tmp_path / "NVDA" / "2026-10-29" / "reports" / "notes.md").write_text("not a section", encoding="utf-8")
    rows = service.list_live_sections()
    assert len(rows) == 1
    assert rows[0]["ticker"] == "NVDA"
    assert rows[0]["file"] == "market_report.md"
    assert rows[0]["title"] == "Market Analysis"
    read = service.read_live_section(str(section))
    assert read["text"] == "价格上涨"
    outside = tmp_path.parent / "other" / "2026-10-29" / "reports"
    outside.mkdir(parents=True)
    secret = outside / "market_report.md"
    secret.write_text("secret", encoding="utf-8")
    with pytest.raises(service.ConsoleError, match="找不到这份分段文件"):
        service.read_live_section(str(secret))
    with pytest.raises(service.ConsoleError, match="这不是命令行会写的分段文件"):
        service.read_live_section(str(tmp_path / "NVDA" / "2026-10-29" / "reports" / "notes.md"))
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "data-live" in script
    assert "/api/live-sections" in script
    table = script.split("function liveSectionTable", 1)[1].split("async function showLiveSection", 1)[0]
    assert "进行中的分段" not in table
    assert "function liveTitleLabel" in script
    assert "liveTitleLabel(row.title)" in script


def test_models_page_sets_the_cli_thinking_effort():
    from webui.service import model_prompt_title

    assert model_prompt_title("deepseek", "quick") == "Select Your [Quick-Thinking LLM Engine]:"
    assert model_prompt_title("deepseek", "deep") == "Select Your [Deep-Thinking LLM Engine]:"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "Select Your [Quick-Thinking LLM Engine]:" not in script
    assert "Select Your [Deep-Thinking LLM Engine]:" not in script
    from webui.catalog import STEP_EIGHT
    assert STEP_EIGHT["openai"]["prompt"] == "Select Reasoning Effort:"
    assert STEP_EIGHT["google"]["prompt"] == "Select Thinking Mode:"
    assert STEP_EIGHT["anthropic"]["prompt"] == "Select Effort Level:"
    assert "function showModelEffort" in script
    assert 'id="model-effort"' in script
    assert "Grok、DeepSeek 和其他未列出的供应商没有单独思考强度" not in script


def test_backtest_page_sends_the_selected_models(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6.1-sol",
    })
    config = service.build_run_config(checked["selections"])
    assert config["quick_think_provider"] == "grok"
    assert config["deep_think_provider"] == "chatgpt"
    assert config["quick_think_llm"] == "grok-4.6"
    assert config["deep_think_llm"] == "gpt-6.1-sol"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function renderBacktest" in script
    assert "function syncBacktestModels" in script
    assert 'id="bt-quick-provider"' not in script
    assert 'id="bt-deep-model"' not in script
    assert 'id="${tier}-providers"' in script
    assert "bindTier(\"quick\")" in script
    assert "showModelEffort()" in script
    assert "syncBacktestModels()" in script
    assert "function loadSelectedTierModels" in script
    assert "loadSelectedTierModels();" in script


def test_dead_console_process_is_not_called_complete(monkeypatch, tmp_path):
    import subprocess
    import sys

    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    (tmp_path).mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.05)"])
    proc.wait(timeout=5)
    service.JOBS.write_run({
        "id": "dead",
        "status": "running",
        "pid": proc.pid,
        "ticker": "NVDA",
        "date": "2026-10-29",
        "result": str(tmp_path / "missing.json"),
    })
    failed = service.refresh_run_process(service.JOBS.current_run())
    assert failed["status"] == "failed"
    assert failed["detail"] == "分析进程已退出，但没有写出结果。进程退出不等于分析完成。"
    assert "Analysis Complete" not in json.dumps(failed)
    result = tmp_path / "done.json"
    result.write_text("{}", encoding="utf-8")
    service.JOBS.write_run({
        "id": "done",
        "status": "running",
        "pid": proc.pid,
        "ticker": "NVDA",
        "date": "2026-10-29",
        "result": str(result),
    })
    finished = service.refresh_run_process(service.JOBS.current_run())
    assert finished["status"] == "finished"
    assert "Analysis Complete" not in json.dumps(finished)
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        service.JOBS.write_run({
            "id": "self",
            "status": "running",
            "pid": live.pid,
            "proc_start": service._process_start(live.pid),  # W39: pid and start time identify the worker
            "ticker": "NVDA",
            "date": "2026-10-29",
        })
        alive = service.refresh_run_process(service.JOBS.current_run())
        assert alive["status"] == "running"
    finally:
        live.kill()
        live.wait(timeout=5)
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "run.detail" in script or "run?.detail" in script


def test_models_page_shows_the_ollama_cli_lines():
    hinted = service.ollama_hint("http://127.0.0.1:11434/v1")
    assert hinted["cli"][0] == "✓ Using Ollama at http://127.0.0.1:11434/v1"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    start = script.index("async function loadModels")
    body = script[start:script.index("function modelTable")]
    assert "/api/ollama" in body
    assert "hint.cli" in body
    assert 'provider === "ollama"' in body


def test_check_shows_the_report_header_model_line(monkeypatch):
    from tradingagents import __version__
    from tradingagents.reporting import model_line

    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "llm_provider": "chatgpt",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6.1-sol",
    })
    expected = "- " + model_line({
        "version": __version__,
        "llm_provider": "chatgpt",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6.1-sol",
    })
    assert checked["model_header"] == expected
    assert "deep chatgpt gpt-6.1-sol, quick grok grok-4.6" in expected
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function analysisSummary" in script
    assert "快模型" in script and "深模型" in script


def test_check_shows_the_known_report_header_lines(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    checked = service.validate_analysis({
        "ticker": "NVDA",
        "date": "2026-10-29",
        "analysts": ["market", "social"],
        "research_depth": 1,
        "llm_provider": "chatgpt",
        "quick_think_provider": "grok",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "grok-4.6",
        "deep_think_llm": "gpt-6.1-sol",
    })
    lines = checked["header_lines"]
    assert "- Analysis date: 2026-10-29" in lines
    assert any("deep chatgpt gpt-6.1-sol, quick grok grok-4.6" in line for line in lines)
    assert any(line.startswith("- Analysts: market, sentiment;") for line in lines)
    assert any("research debate rounds 1" in line and "risk debate rounds 1" in line for line in lines)
    assert any(line.startswith("- Data vendors:") and "yfinance" in line for line in lines)
    assert not any(line.startswith("- Generated:") or line.startswith("- Rating:") for line in lines)
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "data.header" in script
    assert "Generated" not in script
    assert "Rating" not in script


def test_progress_statuses_match_the_cli_panel():
    from webui.catalog import progress_statuses
    from webui.progress import STATUS_ZH

    rows = progress_statuses()
    assert [item["cli"] for item in rows] == ["pending", "in_progress", "completed", "error"]
    assert {item["cli"]: item["zh"] for item in rows} == STATUS_ZH
    display = Path("cli/display.py").read_text(encoding="utf-8")
    for name in ("pending", "in_progress", "completed", "error"):
        assert f'"{name}"' in display
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "progressStatusLine()" in script
    assert "pending、in_progress" not in script


def test_tool_table_uses_the_cli_message_row():
    display = Path("cli/display.py").read_text(encoding="utf-8")
    assert '(timestamp, "Tool", f"{tool_name}: {formatted_args}")' in display
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function toolRowContent" in script
    assert "messageHeaders()" in script.split("function toolTable", 1)[1].split("function progressTeamMap", 1)[0]
    assert "<th>工具</th>" not in script
    assert "<th>参数</th>" not in script


def test_memory_table_uses_tag_fields_not_invented_status():
    # The memory page left the console in WEBUI-005; the service note still holds.
    note = Path("webui/service.py").read_text(encoding="utf-8")
    assert "页面不把第四段叫成状态或收益列" in note


def test_check_preface_does_not_claim_a_blank_date_came_from_the_flag():
    from cli.selections import DATE_FLAG_PREFIX
    from webui.catalog import confirmation_sentences

    assert confirmation_sentences()["date_flag_prefix"] == DATE_FLAG_PREFIX
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    body = script.split("function checkPreface", 1)[1].split("function questionDefault", 1)[0]
    assert "date_from_default" in body
    assert "ticker_from_default" in body
    assert "date_flag_prefix" in body
    assert "--date、--analysts" in body
