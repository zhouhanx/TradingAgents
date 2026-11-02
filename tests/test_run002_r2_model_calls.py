"""RUN-002 QA round 1: the fixes for MF1, MF2, MF4, W17, W20 and W22 (model calls).

Every test talks to a scripted local server (tests/_model_server.py); nothing
leaves the machine and no sign-in is read.
"""

from __future__ import annotations

import dataclasses
import email.utils
import math
import time

import httpx
import openai
import pytest
from pydantic import BaseModel

from tests._model_server import ModelServer, allow_loopback_only, completion, text_chunks
from tradingagents.agents.structured import invoke_structured, invoke_structured_or_freetext
from tradingagents.llm_clients import call_policy, openai_client
from tradingagents.llm_clients.call_policy import (
    CUTOFF_SLACK,
    CallBudgetExceeded,
    CallPolicy,
    IncompleteResponse,
    call_info,
    classify,
    retry_after,
    worst_case_seconds,
)
from tradingagents.llm_clients.factory import build_llm_kwargs, create_tier_client
from tradingagents.llm_clients.openai_client import (
    ChatGPTPlanChatOpenAI,
    GrokPlanChatOpenAI,
    NormalizedChatOpenAI,
)

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore:Model .* is not in the known model list")]


@pytest.fixture(autouse=True)
def loopback(monkeypatch):
    allow_loopback_only(monkeypatch)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def no_wait(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(call_policy, "_sleep", slept.append)
    return slept


def grok(server, **kwargs):
    kwargs.setdefault("timeout", 1.0)
    kwargs.setdefault("call_policy", CallPolicy())
    return GrokPlanChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, **kwargs)


def api(server, **kwargs):
    """The openai/xai API path of the console: not streamed, bounded."""
    kwargs.setdefault("timeout", 1.0)
    kwargs.setdefault("call_policy", CallPolicy())
    return NormalizedChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, **kwargs)


def error_body(message="nope"):
    return {"error": {"message": message, "type": "invalid_request_error"}}


def wait_for_disconnect(server, index, until):
    while index not in server.disconnects and time.monotonic() < until:
        time.sleep(0.05)
    return server.disconnects.get(index)


# ------------------------------------------------------------------ MF1 structured fallback

class Plan(BaseModel):
    action: str


def _render(plan):
    return plan.action


@pytest.mark.parametrize("status, hits", [(500, 4), (503, 4), (401, 1), (404, 1), (403, 1)])
def test_a_failed_structured_call_is_not_run_again_as_free_text(no_wait, status, hits):
    with ModelServer([{"status": status, "body": error_body()}]) as server:
        llm = api(server)
        structured = llm.with_structured_output(Plan)
        with pytest.raises(openai.APIStatusError) as caught:
            invoke_structured_or_freetext(structured, llm, "x", _render, "Trader")
    assert len(server.hits) == hits                  # one policy run, never two
    assert call_info(caught.value)["attempts"] == hits


def test_a_hung_structured_call_stays_inside_one_budget():
    with ModelServer([{"delay": 5.0, "body": completion()}]) as server:
        llm = api(server, timeout=30, call_policy=CallPolicy(retries=3, budget=1.0))
        structured = llm.with_structured_output(Plan)
        started = time.monotonic()
        with pytest.raises((openai.APITimeoutError, CallBudgetExceeded)):
            invoke_structured_or_freetext(structured, llm, "x", _render, "Research Manager")
        elapsed = time.monotonic() - started
    assert elapsed < 1.0 + CUTOFF_SLACK
    assert len(server.hits) == 1


def test_the_portfolio_manager_path_raises_a_failed_call_too(no_wait):
    with ModelServer([{"status": 500, "body": error_body()}]) as server:
        llm = api(server, call_policy=CallPolicy(retries=1))
        with pytest.raises(openai.InternalServerError):
            invoke_structured(llm.with_structured_output(Plan), "x", "Portfolio Manager")
    assert len(server.hits) == 2


class _Stub:
    def __init__(self, error=None, result=None, content="free text"):
        self.error, self.result, self.content, self.calls = error, result, content, 0

    def invoke(self, prompt):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result if self.result is not None else type("M", (), {"content": self.content})()


@pytest.mark.parametrize("error", [ValueError("bad json"), KeyError("action"),
                                   openai.APITimeoutError(request=httpx.Request("POST", "http://x"))])
def test_a_parse_failure_or_an_unbounded_client_error_still_falls_back_to_free_text(error):
    """No call annotation: a parse/schema miss, or a client without a policy (CLI)."""
    plain = _Stub()
    assert invoke_structured_or_freetext(_Stub(error=error), plain, "x", _render, "Trader") == "free text"
    assert plain.calls == 1


def test_no_parsed_result_falls_back_to_free_text():
    plain = _Stub()
    structured = _Stub(result=None)
    structured.invoke = lambda prompt: None
    assert invoke_structured_or_freetext(structured, plain, "x", _render, "Sentiment Analyst") == "free text"


# ------------------------------------------------------------------ MF2 a hard wall clock

def test_a_stream_of_keep_alives_only_is_cut_at_the_budget_and_the_socket_closed():
    with ModelServer([{"chunks": [], "keepalive": (0.2, 10.0)}]) as server:
        llm = grok(server, timeout=5.0, call_policy=CallPolicy(retries=3, budget=1.5))
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            llm.invoke("x")
        elapsed = time.monotonic() - started
        gone = wait_for_disconnect(server, 0, time.monotonic() + 2.0)
    assert 1.4 <= elapsed < 1.5 + CUTOFF_SLACK
    assert len(server.hits) == 1                      # the budget is spent: no retry
    info = call_info(caught.value)
    assert info["kind"] == "timeout" and info["budget_exhausted"] is True
    assert gone is not None and gone < 1.5 + CUTOFF_SLACK + 0.5


def test_a_trickling_plain_body_is_cut_at_the_budget_not_returned():
    """The openai/xai API path: headers fast, then one byte every 50 ms (~13 s)."""
    with ModelServer([{"body": completion("x" * 200), "trickle": 0.05}]) as server:
        llm = api(server, timeout=1.0, call_policy=CallPolicy(retries=3, budget=2.0))
        started = time.monotonic()
        with pytest.raises((openai.APITimeoutError, CallBudgetExceeded)) as caught:
            llm.invoke("x")
        elapsed = time.monotonic() - started
        gone = wait_for_disconnect(server, 0, time.monotonic() + 2.0)
    assert elapsed < 2.0 + CUTOFF_SLACK
    assert len(server.hits) == 1
    assert call_info(caught.value)["budget_exhausted"] is True
    assert gone is not None


def test_silence_after_the_headers_ends_at_the_budget_even_with_a_long_read_timeout():
    with ModelServer([{"chunks": [], "stall": 10.0}]) as server:
        llm = grok(server, timeout=30.0, call_policy=CallPolicy(retries=3, budget=1.0))
        started = time.monotonic()
        with pytest.raises((openai.APITimeoutError, CallBudgetExceeded)):
            llm.invoke("x")
    assert time.monotonic() - started < 1.0 + CUTOFF_SLACK


def test_the_watchdog_shuts_the_socket_down_when_the_clamp_is_bypassed(monkeypatch):
    """Backstop: even an operation that ignores the deadline is woken by the watchdog."""
    monkeypatch.setattr(call_policy, "io_timeout", lambda timeout, expired: timeout)
    monkeypatch.setattr(call_policy, "_capped", lambda timeout, remaining: timeout)
    with ModelServer([{"chunks": [], "stall": 10.0}]) as server:
        llm = grok(server, timeout=30.0, call_policy=CallPolicy(retries=3, budget=1.0))
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            llm.invoke("x")
        elapsed = time.monotonic() - started
    assert 1.0 + call_policy.CUTOFF_GRACE - 0.1 <= elapsed < 1.0 + CUTOFF_SLACK
    assert call_info(caught.value)["kind"] == "timeout"
    assert len(server.hits) == 1


def test_a_per_attempt_cap_cuts_and_retries_within_the_budget(no_wait):
    with ModelServer([{"chunks": [], "keepalive": (0.1, 10.0)}]) as server:
        llm = grok(server, timeout=5.0, call_policy=CallPolicy(retries=1, budget=10.0, attempt_cap=0.6))
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            llm.invoke("x")
        elapsed = time.monotonic() - started
    assert len(server.hits) == 2                      # the cap is retried, the budget is not spent
    assert elapsed < 2 * 0.6 + CUTOFF_SLACK
    assert call_info(caught.value)["kind"] == "timeout"


def test_an_attempt_cut_at_the_budget_is_not_retried_and_keeps_its_own_error():
    # The attempt ran into the call deadline itself: no back-off, no new
    # attempt, and the caller sees the attempt's own timeout marked as
    # budget_exhausted (not a later CallBudgetExceeded after a pointless sleep).
    now = [0.0]
    slept: list[float] = []
    calls: list[int] = []

    def call():
        calls.append(1)
        now[0] = 5.0
        raise openai.APITimeoutError(request=httpx.Request("POST", "http://x"))

    with pytest.raises(openai.APITimeoutError) as caught:
        call_policy.run_with_policy(call, policy=CallPolicy(retries=3, budget=5.0), timeout=None,
                                    clock=lambda: now[0], sleep=slept.append)
    assert not isinstance(caught.value, CallBudgetExceeded)
    assert calls == [1] and slept == []
    info = call_info(caught.value)
    assert info["kind"] == "timeout" and info["attempts"] == 1 and info["budget_exhausted"] is True


def test_the_deadline_transport_is_a_no_op_outside_a_bounded_call():
    with ModelServer([{"body": completion("plain")}]) as server:
        llm = api(server)
        assert llm.invoke("x").content == "plain"
        assert call_policy.io_timeout(3.0, TimeoutError) == 3.0      # no attempt in flight


@pytest.mark.parametrize("budget", [900, 60, 1.5])
def test_the_worst_case_is_the_budget_plus_the_cutoff_slack(budget):
    assert worst_case_seconds(CallPolicy(budget=budget)) == budget + CUTOFF_SLACK


# ------------------------------------------------------------------ MF4 malformed Retry-After

def _rate_limited(headers):
    response = httpx.Response(429, headers=headers, request=httpx.Request("POST", "http://x"))
    return openai.RateLimitError("slow down", response=response, body=None)


@pytest.mark.parametrize("headers, expected", [
    ({"retry-after": "soon"}, None),
    ({"retry-after": "nan"}, None),
    ({"retry-after": "inf"}, math.inf),
    ({"retry-after": "-inf"}, 0.0),
    ({"retry-after": "1e400"}, math.inf),
    ({"retry-after": ""}, None),
    ({"retry-after-ms": "nan"}, None),
    ({"retry-after-ms": "soon", "retry-after": "4"}, 4.0),
    ({"retry-after": "-5"}, 0.0),
    ({"retry-after-ms": "-100"}, 0.0),
    ({"retry-after": "Mon, 99 Foo 2026 99:99:99 GMT"}, None),
])
def test_a_malformed_retry_after_is_ignored_and_never_raises(headers, expected):
    assert retry_after(_rate_limited(headers)) == expected


def test_an_http_date_retry_after_is_honoured():
    when = email.utils.formatdate(time.time() + 30, usegmt=True)
    assert retry_after(_rate_limited({"retry-after": when})) == pytest.approx(30, abs=2)
    past = email.utils.formatdate(time.time() - 30, usegmt=True)
    assert retry_after(_rate_limited({"retry-after": past})) == 0.0


@pytest.mark.parametrize("value", ["soon", "nan", "", "Mon, 99 Foo 2026 99:99:99 GMT"])
def test_a_429_with_a_malformed_retry_after_uses_the_normal_back_off(no_wait, value):
    script = [{"status": 429, "headers": {"retry-after": value}, "body": error_body("slow down")},
              {"chunks": text_chunks(["ok"])}]
    with ModelServer(script) as server:
        llm = grok(server, call_policy=CallPolicy(retries=3, backoff_base=2.0))
        assert llm.invoke("x").content == "ok"
    assert len(server.hits) == 2
    assert len(no_wait) == 1 and math.isfinite(no_wait[0]) and 1.0 <= no_wait[0] <= 2.0


@pytest.mark.parametrize("value", ["-5", "-inf"])
def test_a_negative_retry_after_retries_at_once(no_wait, value):
    script = [{"status": 429, "headers": {"retry-after": value}, "body": error_body("slow down")},
              {"chunks": text_chunks(["ok"])}]
    with ModelServer(script) as server:
        assert grok(server).invoke("x").content == "ok"
    assert no_wait == [0.0]


@pytest.mark.parametrize("value", ["1e308", "86400", "inf", "1e400"])
def test_a_huge_retry_after_gives_up_at_once_with_the_original_429(no_wait, value):
    script = [{"status": 429, "headers": {"retry-after": value}, "body": error_body("slow down")}]
    with ModelServer(script) as server, pytest.raises(openai.RateLimitError) as caught:
        grok(server, call_policy=CallPolicy(retries=3, budget=60)).invoke("x")
    assert len(server.hits) == 1 and no_wait == []
    info = call_info(caught.value)
    assert info["kind"] == "rate_limit" and info["budget_exhausted"] is True


def test_giving_up_on_a_malformed_429_keeps_the_original_error(no_wait):
    script = [{"status": 429, "headers": {"retry-after": "soon"}, "body": error_body("slow down")}]
    with ModelServer(script) as server, pytest.raises(openai.RateLimitError) as caught:
        grok(server, call_policy=CallPolicy(retries=0)).invoke("x")
    assert call_info(caught.value)["kind"] == "rate_limit"


def test_a_broken_retry_after_parser_cannot_replace_the_429(no_wait, monkeypatch):
    def broken(exc):
        raise ValueError("parser bug")

    monkeypatch.setattr(call_policy, "retry_after", broken)
    script = [{"status": 429, "body": error_body("slow down")}, {"chunks": text_chunks(["ok"])}]
    with ModelServer(script) as server:
        assert grok(server).invoke("x").content == "ok"
    assert len(no_wait) == 1 and math.isfinite(no_wait[0])


# ------------------------------------------------------------------ W17 the CLI is unchanged

@pytest.fixture
def signed_in(monkeypatch):
    for name in ("grok", "chatgpt"):
        spec = openai_client.OPENAI_COMPATIBLE_PROVIDERS[name]
        monkeypatch.setitem(openai_client.OPENAI_COMPATIBLE_PROVIDERS, name,
                            dataclasses.replace(spec, credential=lambda: (lambda: "stub")))
    monkeypatch.setenv("OPENAI_API_KEY", "stub")
    monkeypatch.setenv("XAI_API_KEY", "stub")


# Measured on 7b27f2c (before RUN-002) with the CLI's default config.
_BEFORE_RUN002 = {
    "openai": ("gpt-5", NormalizedChatOpenAI, False),
    "xai": ("grok-4.7", NormalizedChatOpenAI, False),
    "grok": ("grok-4.7", GrokPlanChatOpenAI, False),
    "chatgpt": ("gpt-5", ChatGPTPlanChatOpenAI, True),
}


@pytest.mark.parametrize("provider", sorted(_BEFORE_RUN002))
def test_the_cli_default_config_builds_the_same_client_as_before_run002(signed_in, provider):
    from tradingagents.default_config import DEFAULT_CONFIG

    model, cls, streaming = _BEFORE_RUN002[provider]
    config = dict(DEFAULT_CONFIG, llm_provider=provider, quick_think_llm=model, deep_think_llm=model)
    assert "call_budget" not in build_llm_kwargs(config)
    llm = create_tier_client(config, "quick").get_llm()
    assert type(llm) is cls
    assert llm.call_policy is None
    assert llm.streaming is streaming
    assert llm.request_timeout is None
    assert llm.max_retries is None and llm.root_client.max_retries == 2   # the SDK default
    pool = llm.root_client._client._transport._pool
    assert not getattr(pool._network_backend, "tradingagents_deadline", False)


def test_a_cli_config_that_names_a_budget_opts_in(signed_in):
    llm = create_tier_client({"llm_provider": "grok", "quick_think_llm": "grok-4.7",
                              "deep_think_llm": "grok-4.7", "llm_call_budget": 120}, "quick").get_llm()
    assert llm.call_policy == CallPolicy(retries=3, budget=120.0)
    assert llm.streaming is True and llm.max_retries == 0
    assert llm.request_timeout.connect == 15.0


def test_an_empty_timeout_never_lets_a_connect_wait_the_whole_budget():
    seen = []
    call_policy.run_with_policy(lambda: seen.append(call_policy.attempt_timeout()),
                                policy=CallPolicy(budget=900), timeout=None)
    timeout = seen[0]
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (15.0, 300.0, 60.0, 15.0)


# ------------------------------------------------------------------ W20 an unfinished stream

def test_a_stream_that_ends_cleanly_without_finishing_is_retried(no_wait):
    script = [{"chunks": text_chunks(["Par"]), "unfinished": True},
              {"chunks": text_chunks(["Partial ", "no more"])}]
    with ModelServer(script) as server:
        assert grok(server).invoke("x").content == "Partial no more"
    assert len(server.hits) == 2


def test_an_unfinished_stream_is_never_returned_as_an_answer(no_wait):
    with ModelServer([{"chunks": text_chunks(["Par"]), "unfinished": True}]) as server, \
            pytest.raises(IncompleteResponse) as caught:
        grok(server, call_policy=CallPolicy(retries=1)).invoke("x")
    assert len(server.hits) == 2
    assert classify(caught.value) == ("connection", True)
    assert call_info(caught.value)["kind"] == "connection"


# ------------------------------------------------------------------ W22 surviving mutations

def test_a_408_is_retried(no_wait):
    script = [{"status": 408, "body": error_body("timeout")}, {"chunks": text_chunks(["ok"])}]
    with ModelServer(script) as server:
        assert grok(server).invoke("x").content == "ok"
    assert len(server.hits) == 2


@pytest.mark.parametrize("status, retried", [(499, False), (500, True), (599, True)])
def test_the_edges_of_the_5xx_range(no_wait, status, retried):
    script = [{"status": status, "body": error_body()}, {"chunks": text_chunks(["ok"])}]
    with ModelServer(script) as server:
        llm = grok(server)
        if retried:
            assert llm.invoke("x").content == "ok"
        else:
            with pytest.raises(openai.APIStatusError):
                llm.invoke("x")
    assert len(server.hits) == (2 if retried else 1)


def test_classify_puts_600_outside_the_server_errors():
    response = httpx.Response(600, request=httpx.Request("POST", "http://x"))
    assert classify(openai.APIStatusError("odd", response=response, body=None)) == ("other", False)


# ------------------------------------------------------------------ each MF2/MF4/W17 layer on its own

def test_content_chunks_alone_stop_at_the_budget_without_the_deadline_transport(monkeypatch):
    """The per-chunk deadline check bounds a stream that keeps sending text."""
    monkeypatch.setattr(call_policy, "install_deadline_transport", lambda client: None)
    with ModelServer([{"chunks": text_chunks(["x"] * 100, gap=0.1)}]) as server:
        llm = grok(server, timeout=5.0, call_policy=CallPolicy(retries=3, budget=1.5))
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            llm.invoke("x")
        elapsed = time.monotonic() - started
    assert elapsed < 1.5 + CUTOFF_SLACK
    assert call_info(caught.value)["kind"] == "timeout"


def test_keep_alives_stop_at_the_budget_through_the_socket_clamp_alone(monkeypatch):
    """Without the watchdog, the clamp on every socket read still ends the attempt:
    keep-alives for 1 s, then silence; the read that starts at 1 s gets 0.5 s, not 1.5 s."""
    monkeypatch.setattr(call_policy, "_start_watchdog", lambda current, seconds: None)
    with ModelServer([{"chunks": [], "keepalive": (0.2, 1.0), "stall": 10.0}]) as server:
        llm = grok(server, timeout=5.0, call_policy=CallPolicy(retries=3, budget=1.5))
        started = time.monotonic()
        with pytest.raises(Exception) as caught:
            llm.invoke("x")
        elapsed = time.monotonic() - started
    assert elapsed < 1.5 + 0.2
    assert call_info(caught.value)["budget_exhausted"] is True


def test_a_socket_operation_after_the_deadline_raises_instead_of_waiting():
    def late():
        time.sleep(0.1)
        return call_policy.io_timeout(5.0, TimeoutError)

    with pytest.raises(TimeoutError):
        call_policy.run_with_policy(late, policy=CallPolicy(retries=0, budget=0.05), timeout=None)


@pytest.mark.parametrize("asked", [math.nan, -1.0, -math.inf])
def test_a_retry_after_reader_returning_nan_or_negative_never_sleeps_on_it(no_wait, monkeypatch, asked):
    monkeypatch.setattr(call_policy, "retry_after", lambda exc: asked)
    script = [{"status": 429, "body": error_body("slow down")}, {"chunks": text_chunks(["ok"])}]
    with ModelServer(script) as server:
        assert grok(server).invoke("x").content == "ok"
    assert len(no_wait) == 1 and math.isfinite(no_wait[0]) and no_wait[0] >= 0


@pytest.mark.parametrize("empty", [None, ""])
def test_an_empty_timeout_field_becomes_the_split_defaults(empty):
    timeout = call_policy.default_timeout(empty)
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (15.0, 300.0, 60.0, 15.0)
