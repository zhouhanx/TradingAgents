"""RUN-002 QA round 2 fixes for the model calls: W25, W26, W27 and W30.

Every test talks to a scripted local server (tests/_model_server.py) or to
fakes; nothing leaves the machine and no sign-in is read.
"""

from __future__ import annotations

import threading
import time
import types

import openai
import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk
from pydantic import BaseModel

from tests._model_server import ModelServer, allow_loopback_only, completion, text_chunks
from tradingagents.agents.structured import invoke_structured_or_freetext
from tradingagents.llm_clients import call_policy, openai_client
from tradingagents.llm_clients.call_policy import (
    CallPolicy,
    IncompleteResponse,
    TruncatedResponse,
    call_info,
    classify,
    retry_after,
)
from tradingagents.llm_clients.openai_client import (
    UNFINISHED,
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
    kwargs.setdefault("call_policy", CallPolicy(retries=2))
    return GrokPlanChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, **kwargs)


def chatgpt(server, **kwargs):
    kwargs.setdefault("timeout", 1.0)
    kwargs.setdefault("call_policy", CallPolicy(retries=2))
    return ChatGPTPlanChatOpenAI(model="gpt-5", api_key="stub", base_url=server.url,
                                 use_responses_api=True, **kwargs)


def api(server, **kwargs):
    kwargs.setdefault("timeout", 1.0)
    kwargs.setdefault("call_policy", CallPolicy())
    return NormalizedChatOpenAI(model="grok-4.7", api_key="stub", base_url=server.url, **kwargs)


def error_body(message="nope"):
    return {"error": {"message": message, "type": "invalid_request_error"}}


# ------------------------------------------------------------------ W25 structured -> free text

class Plan(BaseModel):
    action: str


def _render(plan):
    return plan.action


@pytest.mark.parametrize("status", [400, 422])
def test_a_rejected_structured_request_falls_back_to_free_text_once(no_wait, status):
    script = [{"status": status, "body": error_body("response_format is not supported")},
              {"body": completion("BUY, the free text answer")}]
    with ModelServer(script) as server:
        llm = api(server, call_policy=CallPolicy(retries=3))
        text = invoke_structured_or_freetext(llm.with_structured_output(Plan), llm, "x", _render, "Trader")
    assert text == "BUY, the free text answer"
    assert len(server.hits) == 2                         # the rejected call, then one free-text call
    asked = server.hits[0]["body"]
    assert "tools" in asked or "response_format" in asked
    assert "tools" not in server.hits[1]["body"] and "response_format" not in server.hits[1]["body"]
    assert no_wait == []                                 # a 400/422 is never retried itself


@pytest.mark.parametrize("status", [400, 422])
def test_a_free_text_fallback_that_is_rejected_too_is_not_tried_again(no_wait, status):
    with ModelServer([{"status": status, "body": error_body()}]) as server:
        llm = api(server, call_policy=CallPolicy(retries=3))
        with pytest.raises(openai.APIStatusError) as caught:
            invoke_structured_or_freetext(llm.with_structured_output(Plan), llm, "x", _render, "Trader")
    assert caught.value.status_code == status
    assert len(server.hits) == 2                         # exactly one fallback


@pytest.mark.parametrize("status, hits", [(500, 4), (401, 1), (403, 1), (404, 1), (429, 4)])
def test_other_failed_structured_calls_still_get_no_free_text_run(no_wait, status, hits):
    with ModelServer([{"status": status, "body": error_body()}]) as server:
        llm = api(server, call_policy=CallPolicy(retries=3))
        with pytest.raises(openai.APIStatusError):
            invoke_structured_or_freetext(llm.with_structured_output(Plan), llm, "x", _render, "Trader")
    assert len(server.hits) == hits


def test_a_timed_out_structured_call_gets_no_free_text_run():
    with ModelServer([{"delay": 3.0, "body": completion()}]) as server:
        llm = api(server, call_policy=CallPolicy(retries=1, budget=0.6))
        with pytest.raises(Exception) as caught:
            invoke_structured_or_freetext(llm.with_structured_output(Plan), llm, "x", _render, "Trader")
    assert call_info(caught.value)["kind"] == "timeout"
    assert len(server.hits) == 1


def test_http_status_reads_the_status_through_the_exception_chain():
    with ModelServer([{"status": 422, "body": error_body()}]) as server, pytest.raises(openai.APIStatusError) as caught:
        api(server, call_policy=CallPolicy(retries=0)).invoke("x")
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = caught.value
    assert call_policy.http_status(wrapped) == 422
    assert call_policy.http_status(RuntimeError("no status")) is None


# ------------------------------------------------------------------ W26 the end of a stream

def test_grok_plan_a_done_marker_without_finish_reason_is_a_complete_answer(no_wait):
    with ModelServer([{"chunks": text_chunks(["Hel", "lo"]), "finish": None}]) as server:
        message = grok(server).invoke("x")
    assert message.content == "Hello"
    assert len(server.hits) == 1


def test_grok_plan_a_done_marker_after_a_usage_only_chunk_is_complete(no_wait):
    usage = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": "grok-4.7", "choices": [],
             "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}
    with ModelServer([{"chunks": text_chunks(["ok"]), "finish": None, "after": [usage]}]) as server:
        assert grok(server).invoke("x").content == "ok"
    assert len(server.hits) == 1


def test_grok_plan_no_done_marker_and_no_finish_reason_is_still_incomplete(no_wait):
    # W20 holds: a stream that just stops is retried and then fails.
    script = [{"chunks": text_chunks(["Hel"]), "finish": None, "done": False}]
    with ModelServer(script) as server, pytest.raises(IncompleteResponse):
        grok(server).invoke("x")
    assert len(server.hits) == 3


@pytest.mark.parametrize("done", [True, False])
@pytest.mark.parametrize("reason", ["length", "content_filter"])
def test_grok_plan_a_cut_off_answer_is_not_a_clean_success(no_wait, reason, done):
    script = [{"chunks": text_chunks(["half an ans"]), "finish": reason, "done": done}]
    with ModelServer(script) as server, pytest.raises(TruncatedResponse) as caught:
        grok(server).invoke("x")
    assert reason in str(caught.value)
    assert len(server.hits) == 1                         # the same prompt would be cut again
    assert call_info(caught.value)["kind"] == "other"
    assert classify(caught.value) == ("other", False)


def test_the_api_path_does_not_take_a_done_marker_alone_as_complete(no_wait):
    with ModelServer([{"chunks": text_chunks(["Hel"]), "finish": None}]) as server:
        llm = api(server, streaming=True, call_policy=CallPolicy(retries=1))
        with pytest.raises(IncompleteResponse):
            llm.invoke("x")
    assert len(server.hits) == 2


def test_only_the_grok_plan_accepts_the_done_marker():
    assert GrokPlanChatOpenAI.done_marker_completes is True
    assert ChatGPTPlanChatOpenAI.done_marker_completes is False
    assert NormalizedChatOpenAI.done_marker_completes is False


def _response(status, incomplete=None, text="Hi"):
    return {"id": "resp_1", "object": "response", "created_at": 0, "model": "gpt-5", "status": status,
            "incomplete_details": incomplete, "parallel_tool_calls": True, "tool_choice": "auto",
            "tools": [],
            "output": [{"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": text, "annotations": []}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
                      "input_tokens_details": {"cached_tokens": 0},
                      "output_tokens_details": {"reasoning_tokens": 0}}}


def _responses_stream(final=None, done=False):
    events = [("response.created", {"type": "response.created", "sequence_number": 0,
                                    "response": {**_response("in_progress"), "output": []}}),
              ("response.output_text.delta", {"type": "response.output_text.delta", "sequence_number": 1,
                                              "item_id": "msg_1", "output_index": 0, "content_index": 0,
                                              "delta": "Hi", "logprobs": []})]
    if final is not None:
        kind, status, incomplete = final
        events.append((kind, {"type": kind, "sequence_number": 2, "response": _response(status, incomplete)}))
    if done:
        events.append("[DONE]")
    return events


def test_chatgpt_plan_a_completed_response_is_a_clean_answer(no_wait):
    with ModelServer([{"events": _responses_stream(("response.completed", "completed", None))}]) as server:
        message = chatgpt(server).invoke("x")
    assert message.content and "Hi" in str(message.content)
    assert len(server.hits) == 1
    assert server.hits[0]["path"].endswith("/responses")


@pytest.mark.parametrize("done", [True, False])
def test_chatgpt_plan_still_needs_a_final_status_even_after_a_done_marker(no_wait, done):
    with ModelServer([{"events": _responses_stream(None, done=done)}]) as server, pytest.raises(IncompleteResponse):
        chatgpt(server).invoke("x")
    assert len(server.hits) == 3                         # unfinished: retried


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
def test_chatgpt_plan_an_incomplete_response_is_not_a_clean_success(no_wait, reason):
    final = ("response.incomplete", "incomplete", {"reason": reason})
    with ModelServer([{"events": _responses_stream(final, done=True)}]) as server, pytest.raises(TruncatedResponse):
        chatgpt(server).invoke("x")
    assert len(server.hits) == 1


def _chunk(info=None, **metadata):
    return ChatGenerationChunk(message=AIMessageChunk(content="", response_metadata=metadata),
                               generation_info=info)


@pytest.mark.parametrize("chunks, outcome", [
    ([_chunk({"finish_reason": "stop"})], "complete"),
    ([_chunk(finish_reason="stop")], "complete"),
    ([_chunk(status="completed")], "complete"),
    ([_chunk({"finish_reason": "length"})], "length"),
    ([_chunk(finish_reason="length")], "length"),
    ([_chunk(finish_reason="max_tokens")], "max_tokens"),
    ([_chunk(status="incomplete")], "incomplete"),
    ([_chunk(status="failed")], "failed"),
    ([_chunk(status="in_progress")], UNFINISHED),
    ([_chunk({"finish_reason": "stop"}), _chunk(finish_reason="length")], "length"),
    ([_chunk(status="completed"), _chunk(status="incomplete")], "incomplete"),
    ([_chunk()], UNFINISHED),
    ([], UNFINISHED),
])
@pytest.mark.parametrize("done, done_completes", [(False, False), (True, False), (False, True)])
def test_the_stream_outcome_reads_every_ending(chunks, outcome, done, done_completes):
    assert openai_client._stream_outcome(chunks, done=done, done_completes=done_completes) == outcome


@pytest.mark.parametrize("chunks, outcome", [
    ([_chunk()], "complete"),
    ([], "complete"),
    ([_chunk(status="in_progress")], "complete"),
    ([_chunk(finish_reason="length")], "length"),
    ([_chunk(status="incomplete")], "incomplete"),
])
def test_a_done_marker_completes_only_an_answer_without_an_ending(chunks, outcome):
    assert openai_client._stream_outcome(chunks, done=True, done_completes=True) == outcome


def test_the_done_marker_is_noted_for_the_attempt_in_flight_only():
    seen = []

    def call():
        call_policy.note_stream_done()
        seen.append(call_policy.stream_done())
        return "ok"

    call_policy.run_with_policy(call, policy=CallPolicy(retries=0), timeout=None)
    assert seen == [True]
    assert call_policy.stream_done() is False
    call_policy.note_stream_done()                       # outside a call: nothing to note
    assert call_policy.stream_done() is False


def test_each_retry_starts_without_the_last_attempts_done_marker(no_wait):
    # a fresh attempt record per try: a [DONE] read by a dropped attempt never completes the next
    seen = []

    def call():
        seen.append(call_policy.stream_done())
        call_policy.note_stream_done()
        if len(seen) == 1:
            raise IncompleteResponse("again")
        return "ok"

    call_policy.run_with_policy(call, policy=CallPolicy(retries=1), timeout=None)
    assert seen == [False, False]


# ------------------------------------------------------------------ W27 a watchdog cuts its own call only

def test_a_watchdog_never_cuts_the_keep_alive_connection_another_call_now_uses():
    # A: a managed call that sends one request and then keeps working in its
    # own code past its budget; its connection went back to the pool.
    # B: a streamed call that picks up that same connection. A's watchdog
    # fires while B is reading; B must finish on its single request.
    script = [{"body": completion("A")},
              {"chunks": text_chunks(["b"] * 6, gap=0.2)}]
    with ModelServer(script) as server:
        llm = grok(server, timeout=5.0, call_policy=CallPolicy(retries=2, budget=10.0))
        results = {}
        a_sent, b_done = threading.Event(), threading.Event()

        def call_a():
            llm.root_client.chat.completions.create(model="grok-4.7", messages=[{"role": "user", "content": "a"}])
            a_sent.set()                                 # A's connection is back in the pool
            b_done.wait(10)                              # A stays in call() past its budget while B reads
            return "A"

        def run_a():
            try:
                results["a"] = call_policy.run_with_policy(call_a, policy=CallPolicy(retries=0, budget=0.4),
                                                           timeout=None)
            except Exception as exc:  # noqa: BLE001
                results["a"] = exc

        a = threading.Thread(target=run_a)
        a.start()
        assert a_sent.wait(10)                           # A's request is done, A is still in call()
        try:
            results["b"] = llm.invoke("b").content
        finally:
            b_done.set()
        a.join()
    assert results["b"] == "bbbbbb"
    assert len(server.hits) == 2                         # A's request and B's, no retry of B
    assert server.hits[0]["peer"] == server.hits[1]["peer"]   # B really reused A's connection


class _Sock:
    def __init__(self):
        self.shut = 0

    def shutdown(self, how):
        self.shut += 1


def _attempt(seconds=5.0):
    return call_policy._Attempt(deadline=time.monotonic() + seconds, timeout=None, clock=time.monotonic)


def test_an_attempt_cuts_a_socket_only_while_it_owns_it():
    sock, a, b = _Sock(), _attempt(), _attempt()
    a.track(sock)
    b.track(sock)                                        # B picked the connection up
    a.cut_off()
    assert sock.shut == 0
    b.cut_off()
    assert sock.shut == 1
    a.release()
    b.release()


def test_a_socket_used_outside_any_managed_call_is_no_attempts_to_cut():
    sock, a = _Sock(), _attempt()
    a.track(sock)
    call_policy.track_socket(sock)                       # an unmanaged call reuses it
    a.cut_off()
    assert sock.shut == 0
    a.release()


def test_a_released_attempt_never_cuts_or_tracks_again():
    sock, a = _Sock(), _attempt()
    a.track(sock)
    a.release()
    a.cut_off()
    a.track(sock)
    assert sock.shut == 0 and a.sockets == [] and call_policy._owner(sock) is None


def test_a_cut_attempt_shuts_a_socket_it_meets_later():
    sock, a = _Sock(), _attempt()
    a.cut_off()
    a.track(sock)
    assert sock.shut == 1
    a.release()


def test_an_attempt_gives_up_its_sockets_when_it_ends():
    sock = _Sock()

    def call():
        call_policy.track_socket(sock)
        return call_policy._CURRENT.get()

    attempt = call_policy.run_with_policy(call, policy=CallPolicy(retries=0), timeout=None)
    assert attempt.released and attempt.sockets == []
    assert call_policy._owner(sock) is None


def test_a_late_watchdog_does_nothing_to_an_attempt_that_has_ended():
    sock = _Sock()

    def call():
        call_policy.track_socket(sock)
        return call_policy._CURRENT.get()

    attempt = call_policy.run_with_policy(call, policy=CallPolicy(retries=0), timeout=None)
    attempt.cut_off()                       # a timer that fires just after the call returned
    assert not attempt.cut
    assert not sock.shut


# ------------------------------------------------------------------ W30 QA's surviving mutants

def test_no_watchdog_outlives_a_successful_call(monkeypatch):
    timers = []
    real = call_policy._start_watchdog

    def spy(current, seconds):
        timer = real(current, seconds)
        timers.append(timer)
        return timer

    monkeypatch.setattr(call_policy, "_start_watchdog", spy)
    assert call_policy.run_with_policy(lambda: "ok", policy=CallPolicy(budget=30.0), timeout=None) == "ok"
    assert len(timers) == 1
    timers[0].join(1.0)
    assert timers[0].finished.is_set() and not timers[0].is_alive()


class _FakeStream:
    def __init__(self, sock=None, log=None):
        self.sock, self.log = sock, log if log is not None else []

    def get_extra_info(self, info):
        return self.sock if info == "socket" else None

    def read(self, max_bytes, timeout=None):
        self.log.append(("read", timeout))
        return b"x"

    def write(self, buffer, timeout=None):
        self.log.append(("write", timeout))

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.log.append(("tls", timeout))
        return _FakeStream(self.sock, self.log)

    def close(self):
        pass


class _FakeBackend:
    def __init__(self, sock=None):
        self.log, self.sock = [], sock

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.log.append(("connect", timeout))
        return _FakeStream(self.sock, self.log)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        self.log.append(("unix", timeout))
        return _FakeStream(self.sock, self.log)


_CORE = types.SimpleNamespace(ConnectTimeout=type("ConnectTimeout", (Exception,), {}),
                              ReadTimeout=type("ReadTimeout", (Exception,), {}),
                              WriteTimeout=type("WriteTimeout", (Exception,), {}))


def _inside(attempt, fn):
    token = call_policy._CURRENT.set(attempt)
    try:
        return fn()
    finally:
        call_policy._CURRENT.reset(token)
        attempt.release()


def test_every_socket_operation_is_clamped_to_the_time_left():
    backend = call_policy._DeadlineBackend(_FakeBackend(), _CORE)

    def work():
        stream = backend.connect_tcp("h", 1, timeout=15.0)
        stream.write(b"x", timeout=60.0)
        stream.read(10, timeout=300.0)
        stream.start_tls(None, "h", timeout=15.0)
        backend.connect_unix_socket("/s", timeout=15.0)
        return backend._inner.log

    log = _inside(_attempt(0.5), work)
    assert [name for name, _ in log] == ["connect", "write", "read", "tls", "unix"]
    assert all(timeout is not None and 0 < timeout <= 0.5 for _, timeout in log)


def test_socket_operations_outside_a_managed_call_keep_their_timeouts():
    backend = call_policy._DeadlineBackend(_FakeBackend(), _CORE)
    stream = backend.connect_tcp("h", 1, timeout=15.0)
    stream.write(b"x", timeout=60.0)
    stream.read(10, timeout=300.0)
    assert backend._inner.log == [("connect", 15.0), ("write", 60.0), ("read", 300.0)]


def test_no_socket_operation_runs_after_the_attempt_was_cut():
    backend = call_policy._DeadlineBackend(_FakeBackend(), _CORE)
    attempt = _attempt(5.0)

    def work():
        stream = backend.connect_tcp("h", 1, timeout=15.0)
        attempt.cut_off()
        with pytest.raises(_CORE.ReadTimeout):
            stream.read(10, timeout=300.0)
        with pytest.raises(_CORE.WriteTimeout):
            stream.write(b"x", timeout=60.0)
        with pytest.raises(_CORE.ConnectTimeout):
            backend.connect_tcp("h", 1, timeout=15.0)
        return backend._inner.log

    assert [name for name, _ in _inside(attempt, work)] == ["connect"]   # nothing after the cut


def test_a_pooled_stream_is_claimed_by_the_attempt_reading_it_now():
    sock = _Sock()
    backend = call_policy._DeadlineBackend(_FakeBackend(sock), _CORE)
    first, second = _attempt(), _attempt()
    stream = _inside_keep(first, lambda: backend.connect_tcp("h", 1, timeout=15.0))
    token = call_policy._CURRENT.set(second)
    try:
        stream.read(10)
        assert call_policy._owner(sock) is second
        token2 = call_policy._CURRENT.set(_attempt())
        stream.write(b"x")
        assert call_policy._owner(sock) is not second
        call_policy._CURRENT.reset(token2)
    finally:
        call_policy._CURRENT.reset(token)
    first.cut_off()
    assert sock.shut == 0
    first.release()
    second.release()


def _inside_keep(attempt, fn):
    token = call_policy._CURRENT.set(attempt)
    try:
        return fn()
    finally:
        call_policy._CURRENT.reset(token)


def test_an_http_date_without_a_zone_is_read_as_utc(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        when = time.strftime("%a, %d %b %Y %H:%M:%S -0000", time.gmtime(time.time() + 30))
        exc = types.SimpleNamespace(response=types.SimpleNamespace(headers={"retry-after": when}))
        exc = _as_exception(exc)
        seconds = retry_after(exc)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()
    assert seconds is not None and 25 <= seconds <= 31


def _as_exception(namespace):
    exc = RuntimeError("429")
    exc.response = namespace.response
    return exc


