"""RUN-002: model calls are streamed, bounded and retried in one layer.

Every test talks to a scripted local server (tests/_model_server.py); nothing
leaves the machine and no sign-in is read.
"""

from __future__ import annotations

import dataclasses
import time

import httpx
import openai
import pytest
from langchain_core.tools import tool

from tests._model_server import ModelServer, allow_loopback_only, completion, text_chunks
from tradingagents.llm_clients import call_policy, openai_client
from tradingagents.llm_clients.call_policy import (
    CallBudgetExceeded,
    CallPolicy,
    call_info,
    classify,
    split_timeout,
    worst_case_seconds,
)
from tradingagents.llm_clients.factory import build_llm_kwargs, create_tier_client
from tradingagents.llm_clients.grok_auth import GrokAuthError
from tradingagents.llm_clients.openai_client import GrokPlanChatOpenAI, NormalizedChatOpenAI

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore:Model .* is not in the known model list")]


@pytest.fixture(autouse=True)
def loopback(monkeypatch):
    allow_loopback_only(monkeypatch)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def no_wait(monkeypatch):
    """Record back-off sleeps instead of sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(call_policy, "_sleep", slept.append)
    return slept


def grok(server, **kwargs):
    """A bounded Grok plan client, as the console builds it (a policy is set)."""
    kwargs.setdefault("timeout", 1.0)
    kwargs.setdefault("call_policy", CallPolicy())
    return GrokPlanChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, **kwargs)


def error_body(message="nope"):
    return {"error": {"message": message, "type": "invalid_request_error"}}


# ------------------------------------------------------------------ streaming

def test_a_non_streamed_answer_slower_than_the_read_timeout_times_out():
    """The incident: no headers until the whole answer is ready."""
    with ModelServer([{"delay": 2.0, "body": completion()}]) as server:
        llm = NormalizedChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url,
                                   timeout=0.5, max_retries=0)
        started = time.monotonic()
        with pytest.raises(openai.APITimeoutError):
            llm.invoke("x" * 36000)
        assert time.monotonic() - started < 1.8
        assert [hit["stream"] for hit in server.hits] == [False]


def test_bounded_grok_streams_by_default_and_returns_one_plain_message():
    with ModelServer([{"chunks": text_chunks(["Bull ", "case."])}]) as server:
        llm = grok(server)
        assert llm.streaming is True
        message = llm.invoke("x")
    assert message.content == "Bull case."
    assert type(message).__name__ == "AIMessage"
    assert server.hits[0]["stream"] is True


def test_a_slow_but_steady_stream_is_not_cut():
    """Read timeout 2s, chunks every 0.6s for ~4.2s in total: far longer than one read
    timeout, while each gap leaves a wide margin on a loaded machine (W30)."""
    parts = [f"p{i} " for i in range(6)]
    with ModelServer([{"chunks": text_chunks(parts, gap=0.6)}]) as server:
        message = grok(server, timeout=2.0, max_retries=0).invoke("x")
    assert message.content == "".join(parts)
    assert len(server.hits) == 1


def test_a_stream_that_stalls_midway_is_cut_at_the_read_timeout(no_wait):
    with ModelServer([{"chunks": text_chunks(["a", "b"]), "stall": 4.0}]) as server:
        llm = grok(server, timeout=0.5, call_policy=CallPolicy(retries=0))
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            llm.invoke("x")
        elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert classify(caught.value) == ("timeout", True)
    assert call_info(caught.value)["kind"] == "timeout"
    assert call_info(caught.value)["attempts"] == 1


def test_a_stream_still_trickling_when_the_budget_runs_out_is_cut(no_wait):
    parts = [f"p{i}" for i in range(30)]
    policy = CallPolicy(retries=3, budget=1.0)
    with ModelServer([{"chunks": text_chunks(parts, gap=0.15)}]) as server:
        llm = grok(server, timeout=0.5, call_policy=policy)
        started = time.monotonic()
        with pytest.raises((CallBudgetExceeded, openai.APITimeoutError)) as caught:
            llm.invoke("x")
        elapsed = time.monotonic() - started
    assert elapsed <= worst_case_seconds(policy, split_timeout(0.5))
    assert elapsed < 1.5
    assert len(server.hits) == 1                      # never retried
    info = call_info(caught.value)
    assert info["kind"] == "timeout" and info["budget_exhausted"] is True


def test_streamed_tool_calls_come_back_as_tool_calls():
    @tool
    def get_news(ticker: str) -> str:
        """News for a ticker."""
        return ticker

    deltas = [
        (0, {"role": "assistant", "content": None, "tool_calls": [
            {"index": 0, "id": "call_1", "type": "function",
             "function": {"name": "get_news", "arguments": ""}}]}),
        (0, {"tool_calls": [{"index": 0, "function": {"arguments": '{"ticker": '}}]}),
        (0, {"tool_calls": [{"index": 0, "function": {"arguments": '"BTC-USD"}'}}]}),
    ]
    with ModelServer([{"chunks": deltas, "finish": "tool_calls"}]) as server:
        message = grok(server).bind_tools([get_news]).invoke("x")
    assert message.tool_calls == [{"name": "get_news", "args": {"ticker": "BTC-USD"},
                                   "id": "call_1", "type": "tool_call"}]


# ------------------------------------------------------------------ configuration

@pytest.fixture
def signed_in_grok(monkeypatch):
    spec = openai_client.OPENAI_COMPATIBLE_PROVIDERS["grok"]
    monkeypatch.setitem(openai_client.OPENAI_COMPATIBLE_PROVIDERS, "grok",
                        dataclasses.replace(spec, credential=lambda: (lambda: "stub")))


def test_timeout_retries_and_budget_from_config_reach_the_grok_client(signed_in_grok):
    config = {"llm_provider": "grok", "quick_think_llm": "grok-4.7", "deep_think_llm": "grok-4.7",
              "timeout": 300, "llm_max_retries": 5, "llm_call_budget": 600}
    llm = create_tier_client(config, "quick").get_llm()
    assert isinstance(llm, GrokPlanChatOpenAI)
    assert llm.streaming is True
    assert llm.call_policy == CallPolicy(retries=5, budget=600.0)
    assert llm.max_retries == 0                       # no second retry layer in the SDK
    assert llm.root_client.max_retries == 0
    timeout = llm.request_timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (15.0, 300.0, 60.0, 15.0)
    assert llm.provider_key == "grok"


def test_a_budget_alone_gives_grok_three_retries_and_split_default_timeouts(signed_in_grok):
    llm = create_tier_client({"llm_provider": "grok", "quick_think_llm": "grok-4.7",
                              "deep_think_llm": "grok-4.7", "llm_call_budget": 900}, "quick").get_llm()
    assert llm.call_policy == CallPolicy(retries=3, budget=900.0)
    assert llm.max_retries == 0
    assert llm.streaming is True
    # An empty timeout field: connect 15 s, never the whole budget (W17).
    timeout = llm.request_timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (15.0, 300.0, 60.0, 15.0)


def test_an_api_key_provider_keeps_sdk_retries_unless_a_budget_is_set(monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "stub")
    plain = create_tier_client({"llm_provider": "xai", "quick_think_llm": "grok-4", "deep_think_llm": "grok-4",
                                "llm_max_retries": 4}, "quick").get_llm()
    assert plain.call_policy is None and plain.max_retries == 4
    bounded = create_tier_client({"llm_provider": "xai", "quick_think_llm": "grok-4", "deep_think_llm": "grok-4",
                                  "llm_max_retries": 4, "llm_call_budget": 120, "timeout": 60}, "quick").get_llm()
    assert bounded.call_policy == CallPolicy(retries=4, budget=120.0)
    assert bounded.max_retries == 0
    assert bounded.request_timeout.read == 60.0


def test_a_bad_call_budget_is_refused():
    for bad in (0, -1, "soon", True):
        with pytest.raises(ValueError, match="llm_call_budget"):
            build_llm_kwargs({"llm_provider": "grok", "llm_call_budget": bad})


# ------------------------------------------------------------------ retries

def test_one_timeout_then_an_answer_completes(no_wait):
    script = [{"delay": 2.0, "body": completion("late")},
              {"chunks": text_chunks(["second ", "try"])}]
    with ModelServer(script) as server:
        message = grok(server, timeout=0.5).invoke("x")
    assert message.content == "second try"
    assert len(server.hits) == 2
    assert len(no_wait) == 1


def test_a_stream_that_drops_midway_is_retried_without_duplicating_text(no_wait):
    script = [{"chunks": text_chunks(["half ", "an "]), "drop": True},
              {"chunks": text_chunks(["whole ", "answer"])}]
    with ModelServer(script) as server:
        message = grok(server).invoke("x")
    assert message.content == "whole answer"
    assert len(server.hits) == 2


def test_a_server_error_is_retried(no_wait):
    script = [{"status": 503, "body": error_body("busy")}, {"chunks": text_chunks(["ok"])}]
    with ModelServer(script) as server:
        assert grok(server).invoke("x").content == "ok"
    assert len(server.hits) == 2


@pytest.mark.parametrize("headers, expected", [
    ({"retry-after": "7"}, 7.0),
    ({"retry-after-ms": "2500"}, 2.5),
])
def test_a_rate_limit_waits_as_long_as_the_server_asks(no_wait, headers, expected):
    script = [{"status": 429, "headers": headers, "body": error_body("slow down")},
              {"chunks": text_chunks(["ok"])}]
    with ModelServer(script) as server:
        assert grok(server).invoke("x").content == "ok"
    assert no_wait == [expected]
    assert len(server.hits) == 2


def test_a_rate_limit_longer_than_the_budget_gives_up_at_once(no_wait):
    script = [{"status": 429, "headers": {"retry-after": "3600"}, "body": error_body("slow down")}]
    with ModelServer(script) as server, pytest.raises(openai.RateLimitError) as caught:
        grok(server, call_policy=CallPolicy(retries=3, budget=60)).invoke("x")
    assert len(server.hits) == 1
    assert no_wait == []
    info = call_info(caught.value)
    assert info["kind"] == "rate_limit" and info["budget_exhausted"] is True


@pytest.mark.parametrize("status, error", [
    (400, openai.BadRequestError),
    (401, openai.AuthenticationError),
    (403, openai.PermissionDeniedError),
    (404, openai.NotFoundError),
])
def test_client_errors_are_never_retried(no_wait, status, error):
    with ModelServer([{"status": status, "body": error_body()}]) as server, pytest.raises(error) as caught:
        grok(server).invoke("x")
    # The policy never retries a client error. A 400 on a request that asked for
    # stream usage gets the one separate try without that option (RUN-003b).
    assert len(server.hits) == (2 if status == 400 else 1)
    assert "stream_options" not in server.hits[-1]["body"] or status != 400
    assert no_wait == []
    assert call_info(caught.value)["attempts"] == 1
    assert call_info(caught.value)["kind"] == ("auth" if status in (401, 403) else "other")


def test_an_expired_sign_in_is_not_retried(no_wait):
    calls = []

    def expired():
        calls.append(1)
        raise GrokAuthError("Grok sign-in expired; run tradingagents auth login grok.")

    with ModelServer([{"chunks": text_chunks(["never"])}]) as server:
        llm = GrokPlanChatOpenAI(model="grok-4.7", api_key=expired, base_url=server.url, timeout=1.0,
                                 call_policy=CallPolicy())
        with pytest.raises(Exception) as caught:
            llm.invoke("x")
    assert server.hits == []
    assert len(calls) == 1
    assert no_wait == []
    assert classify(caught.value) == ("auth", False)


def test_retries_do_not_stack_across_layers(no_wait):
    """Retries=2 means exactly three requests, not (SDK retries + 1) x 3."""
    with ModelServer([{"delay": 1.5, "body": completion()}]) as server:
        llm = grok(server, timeout=0.3, call_policy=CallPolicy(retries=2, budget=60))
        with pytest.raises(openai.APITimeoutError) as caught:
            llm.invoke("x")
    assert len(server.hits) == 3
    assert call_info(caught.value) | {"waited": 0} == {
        "kind": "timeout", "attempts": 3, "max_attempts": 3, "timeout": 0.3, "budget": 60,
        "model": "grok-4.7", "provider": None, "waited": 0,
    }


# ------------------------------------------------------------------ the budget

def test_the_worst_case_of_the_console_defaults_is_bounded():
    policy = CallPolicy(retries=3, budget=900)
    # A hard wall clock: every socket operation ends at the deadline, the
    # watchdog backs it up; only CUTOFF_SLACK (1 s) on top of the budget.
    assert worst_case_seconds(policy, split_timeout(300)) == 901
    assert worst_case_seconds(policy, None) == 901
    # the r1 bound (budget + one connect + read + write + pool) and the old,
    # stacked shape (3 node attempts x 3 SDK attempts x 300 s)
    assert worst_case_seconds(policy) < 1290 < 3 * 3 * 300


def test_no_attempt_or_back_off_runs_past_the_budget():
    """Fake clock: each attempt burns 400s of a 900s budget."""
    now = [0.0]
    calls = []

    def call():
        calls.append(now[0])
        now[0] += 400
        raise openai.APITimeoutError(request=httpx.Request("POST", "http://x"))

    def sleep(seconds):
        now[0] += seconds

    policy = CallPolicy(retries=10, budget=900)
    with pytest.raises(openai.APITimeoutError) as caught:
        call_policy.run_with_policy(call, policy=policy, timeout=split_timeout(300),
                                    sleep=sleep, clock=lambda: now[0])
    assert len(calls) == 3
    assert all(start < 900 for start in calls)
    assert call_info(caught.value)["attempts"] == 3


def test_a_back_off_that_oversleeps_the_budget_starts_no_new_attempt():
    """The sleep came back late (suspended laptop, busy box): no attempt at 0 s left."""
    now = [0.0]
    calls = []

    def call():
        calls.append(now[0])
        now[0] += 100
        raise openai.APITimeoutError(request=httpx.Request("POST", "http://x"))

    def late_sleep(seconds):
        now[0] += seconds + 1000

    with pytest.raises(call_policy.CallBudgetExceeded) as caught:
        call_policy.run_with_policy(call, policy=CallPolicy(retries=3, budget=900, backoff_base=1),
                                    timeout=split_timeout(300), sleep=late_sleep, clock=lambda: now[0])
    assert calls == [0.0]
    info = call_info(caught.value)
    assert info["kind"] == "timeout" and info["attempts"] == 1


def test_each_attempt_timeout_is_capped_to_the_budget_left():
    now = [0.0]
    seen = []

    def call():
        seen.append(call_policy.attempt_timeout().read)
        now[0] += 700
        raise openai.APITimeoutError(request=httpx.Request("POST", "http://x"))

    with pytest.raises(openai.APITimeoutError):
        call_policy.run_with_policy(call, policy=CallPolicy(retries=3, budget=900, backoff_base=0.001),
                                    timeout=split_timeout(300), sleep=lambda s: None,
                                    clock=lambda: now[0])
    assert seen == [300.0, pytest.approx(200.0, abs=0.01)]


def test_the_capped_timeout_goes_out_with_the_request():
    """The per-attempt cap must reach the HTTP layer, not only our bookkeeping."""
    with ModelServer([{"delay": 1.5, "body": completion()}]) as server:
        llm = grok(server, timeout=30, call_policy=CallPolicy(retries=0, budget=0.5))
        started = time.monotonic()
        with pytest.raises(openai.APITimeoutError):
            llm.invoke("x")
    assert time.monotonic() - started < 1.4
