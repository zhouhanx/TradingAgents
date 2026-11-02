"""RUN-003d (B3b): W72–W81, the English busy tail, and Proto's W44/W58/W59/W67/W76 UI.

No model is called: model tests talk to the scripted loopback server only.
"""

from __future__ import annotations

import json
import os
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path

import openai
import pytest
from pydantic import ValidationError

from cli.stats_handler import StatsCallbackHandler
from tests._model_server import ModelServer, allow_loopback_only, text_chunks
from tests.test_run003b import (
    _record,
    _scope,
    _shared,
    home as home,  # noqa: F401  (fixture)
)
from tests.test_run003b_model_calls import GENERIC_400, REFUSED, _footer, grok
from tradingagents.llm_clients import call_policy
from tradingagents.llm_clients.call_policy import CallPolicy
from tradingagents.portfolio import CostedPortfolioContext, PortfolioContext, Position
from webui import server, service

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore:Model .* is not in the known model list")]

SCRIPT = Path("webui/static/app.js").read_text(encoding="utf-8")
STYLE = Path("webui/static/app.css").read_text(encoding="utf-8")
BUSY = {"status": 503, "body": {"error": {"message": "busy"}}}
BASE = {"ticker": "NVDA", "date": "2026-10-29", "llm_provider": "chatgpt",
        "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol"}


@pytest.fixture
def loop(monkeypatch):
    allow_loopback_only(monkeypatch)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    from tradingagents.llm_clients import openai_client
    monkeypatch.setattr(openai_client, "_NO_STREAM_USAGE", set())
    monkeypatch.setattr(call_policy, "_sleep", lambda s: None)


# ------------------------------------------------------------------ W72
def test_a_resend_that_fails_raises_the_original_refusal(loop):
    with ModelServer([REFUSED, BUSY]) as srv, pytest.raises(openai.BadRequestError) as caught:
        grok(srv, call_policy=CallPolicy(retries=0)).invoke("x")
    assert "stream_options" in str(caught.value)
    assert isinstance(caught.value.__cause__, openai.APIStatusError)
    assert caught.value.__cause__.status_code == 503


def test_a_resend_that_fails_async_raises_the_original_too(loop):
    import asyncio
    with ModelServer([REFUSED, BUSY]) as srv, pytest.raises(openai.BadRequestError):
        asyncio.run(grok(srv, call_policy=CallPolicy(retries=0)).ainvoke("x"))


# ------------------------------------------------------------------ W73
def test_a_client_made_before_the_refusal_learns_it(loop):
    with ModelServer([REFUSED, {"chunks": text_chunks(["ok"])}]) as srv:
        first, second = grok(srv), grok(srv)
        first.invoke("x")
        assert second.stream_usage is True
        second.invoke("y")
    assert len(srv.hits) == 3 and "stream_options" not in srv.hits[2]["body"]
    assert second.stream_usage is False


def test_a_generic_400_after_learning_is_not_resent(loop):
    with ModelServer([REFUSED, {"chunks": text_chunks(["ok"])}, GENERIC_400]) as srv:
        old = grok(srv, call_policy=CallPolicy(retries=0))
        grok(srv).invoke("x")
        with pytest.raises(openai.BadRequestError):
            old.invoke("y")
    assert len(srv.hits) == 3


# ------------------------------------------------------------------ W81
def _stats_with(response):
    stats = StatsCallbackHandler()
    stats.on_llm_start({}, ["x"])
    stats.on_llm_end(response)
    return stats


def test_a_zero_usage_report_shows_numbers_not_unknown():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from cli.display import TOKENS_EMPTY, TOKENS_UNKNOWN
    message = AIMessage(content="", usage_metadata={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0})
    stats = _stats_with(LLMResult(generations=[[ChatGeneration(message=message)]]))
    assert stats.get_stats()["usage_reported"] == 1 and stats.usage_unknown == 0
    line = _footer(stats)
    assert TOKENS_EMPTY not in line and TOKENS_UNKNOWN not in line and "Tokens:" in line


def test_an_empty_generation_counts_as_unknown():
    from langchain_core.outputs import LLMResult

    from cli.display import TOKENS_UNKNOWN
    stats = _stats_with(LLMResult(generations=[]))
    assert stats.usage_unknown == 1 and TOKENS_UNKNOWN in _footer(stats)


def test_a_failed_call_counts_as_unknown():
    from cli.display import TOKENS_EMPTY, TOKENS_UNKNOWN
    stats = StatsCallbackHandler()
    stats.on_llm_start({}, ["x"])
    stats.on_llm_error(RuntimeError("boom"))
    assert stats.usage_unknown == 1
    line = _footer(stats)
    assert TOKENS_UNKNOWN in line and TOKENS_EMPTY not in line


# ------------------------------------------------------------------ W74
class _Stub:
    server_address = ("127.0.0.1", 1)

    def __init__(self, action):
        self.action, self.closed = action, False

    def serve_forever(self):
        self.action()

    def server_close(self):
        self.closed = True


def _main_with(monkeypatch, tmp_path, action):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr(service, "_CONSOLE_LOCK", None)
    stub = _Stub(action)
    monkeypatch.setattr(server, "serve", lambda host, port: stub)
    monkeypatch.setattr(server, "start_run_dispatcher", lambda: None)
    before = {sig: signal.getsignal(sig) for sig in server._STOP_SIGNALS}
    assert server.main(["--port", "0"]) == 0
    assert not (tmp_path / service.CONSOLE_LOCK_NAME).exists()
    assert service._CONSOLE_LOCK is None and stub.closed
    assert {sig: signal.getsignal(sig) for sig in server._STOP_SIGNALS} == before


@pytest.mark.parametrize("name", ["SIGTERM", "SIGHUP"])
def test_a_signal_stops_the_console_and_clears_the_lock(monkeypatch, tmp_path, name):
    sig = getattr(signal, name)

    def killed():
        os.kill(os.getpid(), sig)
        time.sleep(5)
        raise AssertionError("the signal did not stop the console")

    _main_with(monkeypatch, tmp_path, killed)


def test_ctrl_c_still_stops_the_console(monkeypatch, tmp_path):
    def interrupt():
        raise KeyboardInterrupt
    _main_with(monkeypatch, tmp_path, interrupt)


# ------------------------------------------------------------------ W77
@pytest.mark.parametrize("ticker", ["", " ", "\t\n"])
def test_a_blank_ticker_is_refused(ticker):
    with pytest.raises(ValidationError):
        Position(ticker=ticker, quantity=1)
    for model in (PortfolioContext, CostedPortfolioContext):
        with pytest.raises(ValidationError):
            model.model_validate({"positions": [{"ticker": ticker, "quantity": 1, "average_price": 1}]})


def test_a_real_ticker_still_passes():
    assert Position(ticker="NVDA", quantity=1).ticker == "NVDA"


def test_the_blank_ticker_message_is_chinese(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError) as caught:
        service.validate_analysis({**BASE, "portfolio": {"positions": [{"ticker": " ", "quantity": 1}]}})
    text = str(caught.value)
    assert "代码（ticker）需要是不为空的文字" in text and "没有启动任何分析" in text
    assert "pydantic" not in text.lower()


# ------------------------------------------------------------------ W78
BT = {"tickers": "NVDA", "start": "2026-10-01", "end": "2026-10-01", "every": 7,
      "llm_provider": "chatgpt", "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol"}


@pytest.mark.parametrize("source, positions", [
    ("json", [{"ticker": " ", "quantity": 1}]),
    ("form", [{"ticker": "NVDA", "quantity": 1}]),          # the form needs the cost
])
def test_a_backtest_checks_the_holdings(home, source, positions):  # noqa: F811
    with pytest.raises(service.ConsoleError, match="没有启动回测"):
        service.validate_backtest({**BT, "portfolio_source": source, "portfolio": {"positions": positions}})
    assert service.JOBS.list_runs() == []


def test_a_backtest_with_good_holdings_is_accepted(home):  # noqa: F811
    service.validate_backtest({**BT, "portfolio": {"positions": [{"ticker": "NVDA", "quantity": 1}]}})


# ------------------------------------------------------------------ W79
def test_bad_holdings_are_not_saved(home):  # noqa: F811
    with pytest.raises(service.ConsoleError, match="设置没有保存"):
        service.save_config({"portfolio": {"cash": -1}})
    assert not service._config_path().exists()


def test_good_settings_still_save(home):  # noqa: F811
    saved = service.save_config({"portfolio": {"cash": 10, "positions": []}})
    assert saved["portfolio"]["cash"] == 10


# ------------------------------------------------------------------ W80
def _setup(monkeypatch, tmp_path):
    cache = tmp_path / "cache"
    monkeypatch.setattr(service, "_selections_cache_dir", lambda selections: selections["data_cache_dir"])
    return cache, datetime(2026, 11, 2, 12, 0, 0)


def test_an_unreadable_job_file_keeps_shared_scopes(home, monkeypatch, tmp_path):  # noqa: F811
    cache, now = _setup(monkeypatch, tmp_path)
    _shared("done", "finished", cache, now - timedelta(days=1))
    _record("broken", "failed", cache, now - timedelta(hours=1))
    (service.webui_home() / "runs" / "broken.job.json").write_text("{not json")
    path = _scope(cache, "backtest-x")
    assert service.prune_run_checkpoints(now.timestamp()) == []
    assert path.exists()


def test_an_unreadable_record_keeps_shared_scopes_and_its_own(home, monkeypatch, tmp_path):  # noqa: F811
    cache, now = _setup(monkeypatch, tmp_path)
    _shared("done", "finished", cache, now - timedelta(days=1))
    (service.webui_home() / "runs" / "lost.json").write_text("{half")
    shared, own = _scope(cache, "backtest-x"), _scope(cache, "lost")
    assert service.prune_run_checkpoints(now.timestamp()) == []
    assert shared.exists() and own.exists()


def test_a_missing_job_file_is_not_unreadable(home, monkeypatch, tmp_path):  # noqa: F811
    cache, now = _setup(monkeypatch, tmp_path)
    _shared("done", "finished", cache, now - timedelta(days=1))
    _record("plain", "finished", cache, now - timedelta(days=1))       # no job file at all
    path = _scope(cache, "backtest-x")
    assert service.prune_run_checkpoints(now.timestamp()) == [str(path)]
    assert service._read_record_job({"id": "plain", "result": str(service.webui_home() / "runs" / "plain.json")}) == {}


def test_a_job_file_that_is_not_an_object_is_unreadable(home):  # noqa: F811
    runs = service.webui_home() / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "odd.job.json").write_text(json.dumps([1]))
    assert service._read_record_job({"id": "odd", "result": str(runs / "odd.json")}) is None
    (runs / "odd2.json").write_text(json.dumps([1]))
    assert "odd2" in service._unreadable_run_ids()
    assert not any(name.endswith(".job") for name in service._unreadable_run_ids()), "job files are judged apart"


# ------------------------------------------------------------------ English busy tail
def test_the_busy_sentence_has_no_english_tail():
    from cli.launcher import ALREADY_RUNNING
    text = service.backtest_busy_sentence({"max_running": 2}, 2, 1)
    assert ALREADY_RUNNING not in text and "TradingAgents is already running" not in text
    assert "回测没有启动" in text


# ------------------------------------------------------------------ W58
def test_next_step_is_on_its_own_line():
    assert "\n下一步：" in service.PROCESS_UNVERIFIED_SENTENCE
    assert "function multilineHtml" in SCRIPT and 'replace(/\\n/g, "<br>")' in SCRIPT


# ------------------------------------------------------------------ W59 / W67 / W44 / W76 (source checks; behaviour in DOM tests)
def test_an_empty_save_status_takes_no_space():
    assert "#model-advanced-feedback:empty" in STYLE and "display: none" in STYLE


def test_ui_hooks_are_present():
    assert "function frameworkTitle" in SCRIPT
    assert "out.scrollIntoView" in SCRIPT
    assert "state.stopRefused = {id, text: error.message}" in SCRIPT and "stopRefusedHtml(run)" in SCRIPT
