"""RUN-003b: W21 (async calls use the call policy) and streamed token usage.

Every test talks to the scripted local server (tests/_model_server.py); nothing
leaves the machine and no sign-in is read.
"""

from __future__ import annotations

import asyncio

import openai
import pytest

from cli.stats_handler import StatsCallbackHandler
from tests._model_server import ModelServer, allow_loopback_only, completion, text_chunks
from tradingagents.llm_clients import call_policy
from tradingagents.llm_clients.call_policy import CallPolicy, IncompleteResponse, call_info
from tradingagents.llm_clients.openai_client import GrokPlanChatOpenAI, NormalizedChatOpenAI

pytestmark = [pytest.mark.unit, pytest.mark.filterwarnings("ignore:Model .* is not in the known model list")]

USAGE = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "grok-4.7", "choices": [],
         "usage": {"prompt_tokens": 1200, "completion_tokens": 345, "total_tokens": 1545}}


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


# ------------------------------------------------------------------ W21

def test_an_async_call_is_retried_by_the_policy(no_wait):
    script = [{"status": 503, "body": {"error": {"message": "busy"}}}, {"chunks": text_chunks(["o", "k"])}]
    with ModelServer(script) as server:
        answer = asyncio.run(grok(server).ainvoke("x"))
    assert answer.content == "ok"
    assert len(server.hits) == 2
    assert no_wait                                      # the policy's backoff, not the SDK's


def test_an_async_unfinished_stream_is_never_an_answer(no_wait):
    with ModelServer([{"chunks": text_chunks(["Par"]), "unfinished": True}]) as server, \
            pytest.raises(IncompleteResponse) as caught:
        asyncio.run(grok(server, call_policy=CallPolicy(retries=1)).ainvoke("x"))
    assert len(server.hits) == 2
    assert call_info(caught.value)["kind"] == "connection"


def test_async_astream_goes_through_the_policy(no_wait):
    script = [{"chunks": text_chunks(["Par"]), "drop": True}, {"chunks": text_chunks(["a", "b"])}]

    async def collect(llm):
        return "".join([c.content async for c in llm.astream("x")])

    with ModelServer(script) as server:
        assert asyncio.run(collect(grok(server))) == "ab"
    assert len(server.hits) == 2


def test_a_managed_non_streamed_async_call_is_retried_by_the_policy(no_wait):
    script = [{"status": 503, "body": {"error": {"message": "busy"}}}, {"body": completion("ok")}]
    with ModelServer(script) as server:
        llm = NormalizedChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, timeout=1.0,
                                   call_policy=CallPolicy())
        assert asyncio.run(llm.ainvoke("x")).content == "ok"
    assert len(server.hits) == 2 and server.hits[0]["stream"] is False
    assert no_wait


def test_async_without_a_policy_is_unchanged():
    with ModelServer([{"body": completion("plain")}]) as server:
        llm = NormalizedChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, timeout=1.0,
                                   max_retries=0)
        assert asyncio.run(llm.ainvoke("x")).content == "plain"
    assert server.hits[0]["stream"] is False


# ------------------------------------------------------------------ streamed token usage

def test_a_bounded_grok_stream_asks_for_usage_and_counts_it(no_wait):
    stats = StatsCallbackHandler()
    with ModelServer([{"chunks": text_chunks(["o", "k"]), "after": [USAGE]}]) as server:
        answer = grok(server, callbacks=[stats]).invoke("x")
    assert answer.content == "ok"
    assert server.hits[0]["body"]["stream"] is True
    assert server.hits[0]["body"]["stream_options"] == {"include_usage": True}
    assert (stats.tokens_in, stats.tokens_out) == (1200, 345)


def test_async_stream_usage_is_counted_too(no_wait):
    stats = StatsCallbackHandler()
    with ModelServer([{"chunks": text_chunks(["ok"]), "after": [USAGE]}]) as server:
        asyncio.run(grok(server, callbacks=[stats]).ainvoke("x"))
    assert stats.tokens_out == 345


def test_an_unbounded_grok_call_is_unchanged():
    with ModelServer([{"body": completion("ok")}]) as server:
        GrokPlanChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, timeout=1.0,
                           max_retries=0).invoke("x")
    body = server.hits[0]["body"]
    assert not body.get("stream") and "stream_options" not in body


def test_an_unbounded_streamed_grok_call_is_unchanged():
    with ModelServer([{"chunks": text_chunks(["ok"])}]) as server:
        GrokPlanChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, timeout=1.0,
                           max_retries=0, streaming=True).invoke("x")
    assert server.hits[0]["body"]["stream"] is True and "stream_options" not in server.hits[0]["body"]


def test_a_caller_can_still_turn_stream_usage_off(no_wait):
    with ModelServer([{"chunks": text_chunks(["ok"])}]) as server:
        grok(server, stream_usage=False).invoke("x")
    assert "stream_options" not in server.hits[0]["body"]


# ------------------------------------------------------------------ an endpoint that refuses stream usage

REFUSED = {"status": 400, "body": {"error": {"message": "Unrecognized request argument: stream_options",
                                             "type": "invalid_request_error"}}}


@pytest.fixture(autouse=True)
def fresh_endpoints(monkeypatch):
    from tradingagents.llm_clients import openai_client
    monkeypatch.setattr(openai_client, "_NO_STREAM_USAGE", set())


def test_a_refused_usage_option_is_dropped_once_and_usage_is_unknown(no_wait):
    stats = StatsCallbackHandler()
    with ModelServer([REFUSED, {"chunks": text_chunks(["o", "k"])}]) as server:
        llm = grok(server, callbacks=[stats])
        answer = llm.invoke("x")
    assert answer.content == "ok"
    assert answer.response_metadata.get("usage_unknown") is True   # the answer itself says so
    assert len(server.hits) == 2
    assert server.hits[0]["body"]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in server.hits[1]["body"]
    assert stats.usage_unknown == 1 and (stats.tokens_in, stats.tokens_out) == (0, 0)
    assert no_wait == []                                 # not a policy retry: no backoff


def test_async_refused_usage_option_too(no_wait):
    stats = StatsCallbackHandler()
    with ModelServer([REFUSED, {"chunks": text_chunks(["ok"])}]) as server:
        assert asyncio.run(grok(server, callbacks=[stats]).ainvoke("x")).content == "ok"
    assert len(server.hits) == 2 and "stream_options" not in server.hits[1]["body"]
    assert stats.usage_unknown == 1


def test_later_calls_and_clients_skip_the_option(no_wait):
    with ModelServer([REFUSED, {"chunks": text_chunks(["ok"])}]) as server:
        llm = grok(server)
        llm.invoke("x")
        llm.invoke("y")
        grok(server).invoke("z")                         # a new client of the same endpoint
    assert len(server.hits) == 4
    assert all("stream_options" not in hit["body"] for hit in server.hits[1:])


def test_it_never_loops(no_wait):
    with ModelServer([REFUSED]) as server, pytest.raises(Exception, match="stream_options"):
        grok(server, call_policy=CallPolicy(retries=3)).invoke("x")
    assert len(server.hits) == 2                          # the original and one try without it


GENERIC_400 = {"status": 400, "body": {"error": {"message": "invalid request"}}}
GENERIC_422 = {"status": 422, "body": {"error": {"message": "Unprocessable Entity"}}}


@pytest.mark.parametrize("refusal", [GENERIC_400, GENERIC_422])
def test_a_generic_400_or_422_also_drops_the_option_once(no_wait, refusal):
    stats = StatsCallbackHandler()
    with ModelServer([refusal, {"chunks": text_chunks(["ok"])}]) as server:
        llm = grok(server, callbacks=[stats])
        assert llm.invoke("x").content == "ok"
    assert len(server.hits) == 2 and "stream_options" in server.hits[0]["body"]
    assert "stream_options" not in server.hits[1]["body"]
    assert stats.usage_unknown == 1 and llm.stream_usage is False


@pytest.mark.parametrize("refusal", [GENERIC_400, GENERIC_422])
def test_async_generic_refusal(no_wait, refusal):
    with ModelServer([refusal, {"chunks": text_chunks(["ok"])}]) as server:
        assert asyncio.run(grok(server).ainvoke("x")).content == "ok"
    assert len(server.hits) == 2 and "stream_options" not in server.hits[1]["body"]


def test_400_twice_is_two_requests_then_the_error_and_nothing_is_remembered(no_wait):
    from tradingagents.llm_clients import openai_client

    with ModelServer([GENERIC_400]) as server:
        llm = grok(server, call_policy=CallPolicy(retries=3))
        with pytest.raises(openai.BadRequestError):
            llm.invoke("x")
        assert len(server.hits) == 2
        assert not openai_client._NO_STREAM_USAGE and llm.stream_usage is True
        with pytest.raises(openai.BadRequestError):
            grok(server).invoke("y")                   # still asks: nothing was learned
    assert "stream_options" in server.hits[2]["body"]


def test_async_400_twice_is_two_requests(no_wait):
    with ModelServer([GENERIC_400]) as server, pytest.raises(openai.BadRequestError):
        asyncio.run(grok(server).ainvoke("x"))
    assert len(server.hits) == 2


def test_a_request_without_the_option_is_unchanged(no_wait):
    with ModelServer([GENERIC_400, {"chunks": text_chunks(["ok"])}]) as server, \
            pytest.raises(openai.BadRequestError):
        grok(server).invoke("x", stream_usage=False)
    assert len(server.hits) == 1


@pytest.mark.parametrize("status", [401, 403, 408, 429, 500, 503])
def test_other_statuses_never_drop_the_option(no_wait, status):
    with ModelServer([{"status": status, "body": {"error": {"message": "stream_options"}}}]) as server, \
            pytest.raises(openai.APIStatusError):
        grok(server, call_policy=CallPolicy(retries=0)).invoke("x")
    assert all("stream_options" in hit["body"] for hit in server.hits)


def test_a_refused_usage_retry_that_fails_otherwise_uses_the_policy(no_wait):
    busy = {"status": 503, "body": {"error": {"message": "busy"}}}
    with ModelServer([REFUSED, busy, {"chunks": text_chunks(["ok"])}]) as server:
        assert grok(server).invoke("x").content == "ok"
    assert len(server.hits) == 3 and no_wait               # the 503 got the normal backoff


@pytest.mark.parametrize("status, text, refused", [
    (400, "stream_options is not supported", True),
    (422, "include_usage: extra fields not permitted", True),
    (400, "bad model", True),
    (422, "anything", True),
    (401, "stream_options", False),
    (429, "stream_options", False),
    (500, "stream_options", False),
    (403, "stream_options", False),
    (408, "stream_options", False),
])
def test_what_counts_as_refusing_the_option(status, text, refused):
    from tradingagents.llm_clients.openai_client import rejects_stream_usage

    exc = RuntimeError(text)
    exc.status_code = status
    assert rejects_stream_usage(exc) is refused


def test_the_footer_says_unknown():
    from cli.display import TOKENS_EMPTY, TOKENS_UNKNOWN, format_footer_stats

    class Buffer:
        agent_status, report_sections = {}, {}

        def get_completed_reports_count(self):
            return 0

    stats = StatsCallbackHandler()
    assert TOKENS_EMPTY in format_footer_stats(Buffer(), stats)
    stats.usage_unknown = 1
    assert TOKENS_UNKNOWN in format_footer_stats(Buffer(), stats)
    stats.tokens_in, stats.tokens_out = 1000, 10
    assert "(部分未知)" in format_footer_stats(Buffer(), stats)


def test_an_explicit_per_call_usage_request_is_dropped_on_the_retry_too(no_wait):
    with ModelServer([REFUSED, {"chunks": text_chunks(["ok"])}]) as server:
        assert grok(server).invoke("x", stream_usage=True).content == "ok"
    assert len(server.hits) == 2 and "stream_options" not in server.hits[1]["body"]


# ------------------------------------------------------------------ W75

def _footer(stats):
    from cli.display import format_footer_stats

    class Buffer:
        agent_status, report_sections = {}, {}

        def get_completed_reports_count(self):
            return 0

    return format_footer_stats(Buffer(), stats)


def test_an_endpoint_that_ignores_the_option_shows_unknown_not_dashes(no_wait):
    from cli.display import TOKENS_EMPTY, TOKENS_UNKNOWN

    stats = StatsCallbackHandler()
    with ModelServer([{"chunks": text_chunks(["ok"])}]) as server:
        assert grok(server, callbacks=[stats]).invoke("x").content == "ok"
    assert "stream_options" in server.hits[0]["body"]            # asked, got no usage back
    assert stats.usage_unknown == 1
    line = _footer(stats)
    assert TOKENS_UNKNOWN in line and TOKENS_EMPTY not in line


def test_async_ignored_option_shows_unknown_too(no_wait):
    from cli.display import TOKENS_UNKNOWN

    stats = StatsCallbackHandler()
    with ModelServer([{"chunks": text_chunks(["ok"])}]) as server:
        asyncio.run(grok(server, callbacks=[stats]).ainvoke("x"))
    assert TOKENS_UNKNOWN in _footer(stats)


def test_some_calls_with_usage_and_some_without_is_partly_unknown(no_wait):
    stats = StatsCallbackHandler()
    script = [{"chunks": text_chunks(["a"]), "after": [USAGE]}, {"chunks": text_chunks(["b"])}]
    with ModelServer(script) as server:
        llm = grok(server, callbacks=[stats])
        llm.invoke("x")
        llm.invoke("y")
    assert "Tokens: 1.2k" in _footer(stats) and "(部分未知)" in _footer(stats)


def test_a_run_with_no_model_call_keeps_the_dashes():
    from cli.display import TOKENS_EMPTY

    stats = StatsCallbackHandler()
    assert stats.get_stats()["llm_calls"] == 0
    assert TOKENS_EMPTY in _footer(stats)


def test_the_console_knows_the_unknown_label():
    from cli.display import TOKENS_UNKNOWN
    from webui.catalog import display_sentences

    assert display_sentences()["tokens_unknown"] == TOKENS_UNKNOWN == "Tokens: 未知"
