import asyncio
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar
from urllib.parse import urlparse

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from . import call_policy as _policy
from .api_key_env import get_api_key_env
from .base_client import BaseLLMClient, normalize_content
from .capabilities import get_capabilities
from .subscriptions import SUBSCRIPTIONS
from .validators import validate_model

logger = logging.getLogger(__name__)



def is_loopback_url(url) -> bool:
    """Whether ``url`` points at this machine: localhost (and *.localhost),
    127.0.0.0/8 or ::1. Anything else, including 0.0.0.0, is not loopback.

    W98: a trailing dot (``localhost.``, the DNS absolute form) is stripped before
    the check, so it is loopback and skips the system proxy like plain localhost.
    """
    import ipaddress
    from urllib.parse import urlsplit

    if not isinstance(url, str) or not url.strip():
        return False
    try:
        host = urlsplit(url.strip()).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

class NormalizedChatOpenAI(ChatOpenAI):
    """ChatOpenAI with normalized content output and capability-aware binding.

    The Responses API returns content as a list of typed blocks
    (reasoning, text, etc.). ``invoke`` normalizes to string for
    consistent downstream handling.

    ``with_structured_output`` consults the per-model capability table
    (``capabilities.get_capabilities``) to pick the method and to decide
    whether ``tool_choice`` may be sent. Models that reject ``tool_choice``
    (e.g. DeepSeek V4 and reasoner — per their official tool-calling
    guide) still bind the schema as a tool, but no ``tool_choice``
    parameter is sent.

    Provider-specific quirks beyond structured-output (e.g. DeepSeek's
    reasoning_content roundtrip) live in subclasses so this base class
    stays small.
    """

    # A CallPolicy makes this class the only retry layer (the SDK's retries are
    # switched off) and bounds every call by a wall-clock budget; see
    # ``call_policy``. None keeps the plain SDK behaviour.
    call_policy: Any = None
    # Registry key of the provider, for failure summaries ("grok", "xai", ...).
    provider_key: str | None = None
    # Whether a stream that ends with ``data: [DONE]`` but never names a
    # finish_reason is complete. Only the Grok plan relaxes this (W26); every
    # other stream must say how it finished.
    done_marker_completes: ClassVar[bool] = False

    def __init__(self, **kwargs):
        managed = kwargs.get("call_policy") is not None
        if managed:
            kwargs["max_retries"] = 0
            # Split bounds; an empty field gets connect 15 / read 300 / write 60
            # / pool 15 rather than letting a connect wait the whole budget.
            kwargs["timeout"] = _policy.default_timeout(kwargs.get("timeout"))
        super().__init__(**kwargs)
        if managed:
            # Every socket operation of an attempt ends at its deadline (MF2).
            _policy.install_deadline_transport(getattr(self.root_client, "_client", None))
            _watch_done_marker(self.root_client)

    def invoke(self, input, config=None, **kwargs):
        return normalize_content(super().invoke(input, config, **kwargs))

    def _run_policy(self, call):
        return _policy.run_with_policy(
            call, policy=self.call_policy, timeout=self.request_timeout,
            model=self.model_name, provider=self.provider_key,
        )

    @staticmethod
    def _with_attempt_timeout(kwargs):
        # The request payload takes call kwargs as-is, so the per-attempt
        # timeout (capped to what is left of the budget) rides along there;
        # no payload override, which stays a DeepSeek/MiniMax subclass concern.
        capped = _policy.attempt_timeout()
        return kwargs if capped is None else {**kwargs, "timeout": capped}

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.call_policy is None:
            return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        parent = super()._generate
        return self._run_policy(lambda: parent(
            messages, stop=stop, run_manager=run_manager, **self._with_attempt_timeout(kwargs)))

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        if self.call_policy is None:
            yield from super()._stream(messages, stop=stop, run_manager=run_manager, **kwargs)
            return
        parent = super()._stream

        def attempt(kwargs=kwargs):
            # Collect the whole attempt: a stream that breaks halfway is retried
            # from the start rather than leaving half an answer behind.
            chunks = []
            stream = parent(messages, stop=stop, run_manager=run_manager,
                            **self._with_attempt_timeout(kwargs))
            try:
                for chunk in stream:
                    _policy.check_deadline()
                    chunks.append(chunk)
            finally:
                close = getattr(stream, "close", None)
                if close is not None:
                    close()
            outcome = _stream_outcome(chunks, done=_policy.stream_done(),
                                      done_completes=self.done_marker_completes)
            if outcome == UNFINISHED:
                # The connection closed cleanly but the answer never said it was
                # done (no finish_reason / final status): half an answer.
                raise _policy.IncompleteResponse(
                    f"the stream ended after {len(chunks)} chunks without finishing")
            if outcome != "complete":
                # The model stopped early (length, content filter, incomplete):
                # not a clean answer, and the same request would stop again.
                raise _policy.TruncatedResponse(f"the answer was cut off ({outcome})")
            return chunks

        if (kwargs.get("stream_usage") is None and self.stream_usage
                and (self.openai_api_base or "") in _NO_STREAM_USAGE):
            # W73: another client of this endpoint already learned that it refuses
            # the option after this one was built; do not ask (and be refused) again.
            self.stream_usage = False
        asked = self._asks_stream_usage(kwargs)
        try:
            chunks = self._run_policy(attempt)
        except Exception as exc:
            if not (asked and rejects_stream_usage(exc)):
                raise
            # A 400/422 on a request carrying stream_options.include_usage: ask
            # again, once, without it. If that works, remember it for this client
            # and every later client of the same endpoint in this process.
            # If that fails too, the ORIGINAL refusal is raised (W72), with the
            # resend's error chained as its cause: no further retries, and the
            # endpoint is not marked as refusing the option.
            try:
                chunks = self._run_policy(lambda: attempt({**kwargs, "stream_usage": False}))
            except Exception as resend_exc:
                raise exc from resend_exc
            self._forget_stream_usage()
            logger.warning("%s refused a request asking for stream usage and accepted it without; "
                           "token usage for its calls is unknown", self.openai_api_base or "the endpoint")
            if chunks:
                chunks[-1].message.response_metadata[USAGE_UNKNOWN] = True
        yield from chunks

    def _asks_stream_usage(self, kwargs) -> bool:
        chosen = kwargs.get("stream_usage")
        return bool(self.stream_usage if chosen is None else chosen)

    def _forget_stream_usage(self) -> None:
        self.stream_usage = False
        _NO_STREAM_USAGE.add(self.openai_api_base or "")

    # W21: async calls get the same policy as sync ones. ChatOpenAI's own
    # ``_agenerate``/``_astream`` would go through the async SDK client, which has
    # no deadline transport, keeps the SDK's retries off without a replacement,
    # and is not bounded by the budget. Under a policy the async path runs the
    # sync policy path in a worker thread instead, so retries, deadlines, stream
    # completeness checks and failure summaries are identical.
    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        if self.call_policy is None:
            return await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        sync_manager = run_manager.get_sync() if run_manager is not None else None
        return await asyncio.to_thread(
            lambda: self._generate(messages, stop=stop, run_manager=sync_manager, **kwargs))

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        if self.call_policy is None:
            async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
                yield chunk
            return
        sync_manager = run_manager.get_sync() if run_manager is not None else None
        # The sync path collects a whole attempt before yielding anyway (a broken
        # stream is retried from the start), so nothing is lost by collecting here.
        chunks = await asyncio.to_thread(
            lambda: list(self._stream(messages, stop=stop, run_manager=sync_manager, **kwargs)))
        for chunk in chunks:
            yield chunk

    def with_structured_output(self, schema, *, method=None, **kwargs):
        caps = get_capabilities(self.model_name)
        if caps.preferred_structured_method == "none":
            raise NotImplementedError(
                f"{self.model_name} has no structured-output method available; "
                f"agent factories will fall back to free-text generation."
            )
        method = method or caps.preferred_structured_method
        # When the model rejects tool_choice, suppress langchain's hardcoded
        # value. The schema is still bound as a tool — exactly what
        # DeepSeek's official tool-calling examples do.
        if method == "function_calling" and not caps.supports_tool_choice:
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


class LocalCompatibleChatOpenAI(NormalizedChatOpenAI):
    """OpenAI-compatible client for arbitrary local servers (LM Studio, vLLM,
    llama.cpp via the generic ``openai_compatible`` provider).

    Their tool-calling support varies, and many reject the object-form
    ``tool_choice`` langchain sends for function-calling structured output. Bind
    the schema as a tool but don't force tool_choice, so structured output works
    across local servers regardless of the model ID's capabilities (#1057).
    """

    def with_structured_output(self, schema, *, method=None, **kwargs):
        resolved = method or get_capabilities(self.model_name).preferred_structured_method
        if resolved == "function_calling":
            kwargs.setdefault("tool_choice", None)
        return super().with_structured_output(schema, method=method, **kwargs)


def _input_to_messages(input_: Any) -> list:
    """Normalise a langchain LLM input to a list of message objects.

    Accepts a list of messages, a ``ChatPromptValue`` (from a
    ChatPromptTemplate), or anything else (treated as no messages).
    Used by providers that need to walk the outgoing message history;
    in particular DeepSeek thinking-mode propagation must work for
    both bare-list invocations and ChatPromptTemplate-driven ones, so
    treating only ``list`` here would silently skip half the call sites.
    """
    if isinstance(input_, list):
        return input_
    if hasattr(input_, "to_messages"):
        return input_.to_messages()
    return []


class DeepSeekChatOpenAI(NormalizedChatOpenAI):
    """DeepSeek-specific overrides on top of the OpenAI-compatible client.

    Thinking-mode round-trip is the only DeepSeek-specific behavior that
    stays here. When DeepSeek's thinking models return a response with
    ``reasoning_content``, that field must be echoed back as part of the
    assistant message on the next turn or the API fails with HTTP 400.
    ``_create_chat_result`` captures it on receive and
    ``_get_request_payload`` re-attaches it on send.

    Tool-choice handling for V4 and reasoner — those models reject the
    ``tool_choice`` parameter — is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        outgoing = payload.get("messages", [])
        for message_dict, message in zip(outgoing, _input_to_messages(input_), strict=False):
            if not isinstance(message, AIMessage):
                continue
            reasoning = message.additional_kwargs.get("reasoning_content")
            if reasoning is not None:
                message_dict["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        chat_result = super()._create_chat_result(response, generation_info)
        response_dict = (
            response
            if isinstance(response, dict)
            else response.model_dump(
                exclude={"choices": {"__all__": {"message": {"parsed"}}}}
            )
        )
        for generation, choice in zip(
            chat_result.generations, response_dict.get("choices", []), strict=False
        ):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning is not None:
                generation.message.additional_kwargs["reasoning_content"] = reasoning
        return chat_result


class MinimaxChatOpenAI(NormalizedChatOpenAI):
    """MiniMax-specific overrides on top of the OpenAI-compatible client.

    M2.x reasoning models embed ``<think>...</think>`` blocks directly in
    ``message.content`` by default, which would pollute saved reports.
    Per platform.minimax.io/docs/api-reference/text-openai-api,
    ``reasoning_split=True`` redirects the thinking block into
    ``reasoning_details`` so ``content`` stays clean. It is sent via
    ``extra_body`` (not a top-level kwarg) because the openai SDK validates
    top-level params and rejects unknown ones like reasoning_split (#826).

    The flag is gated by ``ModelCapabilities.requires_reasoning_split`` so
    only M2.x reasoning models receive it; non-reasoning MiniMax endpoints
    (Coding Plan, MiniMax-Text-01) never see it.

    Tool-choice handling for M2.x — those models accept only the string
    enum ``{"none", "auto"}`` and reject langchain's function-spec dict —
    is handled by the capability dispatch in
    ``NormalizedChatOpenAI.with_structured_output``, not here.
    """

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if get_capabilities(self.model_name).requires_reasoning_split:
            # Pass via extra_body, not as a top-level kwarg: the openai SDK
            # (>=1.56) validates top-level params against Completions.create
            # and rejects unknown ones like reasoning_split (#826). extra_body
            # is forwarded into the request body untouched.
            extra_body = payload.setdefault("extra_body", {})
            extra_body.setdefault("reasoning_split", True)
        return payload


# Settings a ChatGPT plan refuses.
_PLAN_REFUSED = ("temperature", "top_p", "max_tokens")



def chatgpt_plan_refusal(name: str) -> str:
    return f"A ChatGPT plan does not accept {name}; it is not sent."


class ChatGPTPlanChatOpenAI(NormalizedChatOpenAI):
    """The person's ChatGPT plan through OpenAI's Responses API.

    Plan usage accepts no stored responses and no ``system`` role, and refuses
    sampling and length settings. Responses stream and are not stored; system
    instructions are sent as ``developer`` messages, OpenAI's name for the same
    role; refused settings are dropped with a warning; no API organization or
    project is sent.
    """

    def __init__(self, **kwargs):
        for name in _PLAN_REFUSED:
            if kwargs.pop(name, None) is not None:
                logger.warning("%s", chatgpt_plan_refusal(name))
        kwargs.setdefault("streaming", True)
        kwargs.setdefault("store", False)
        super().__init__(**kwargs)
        # The sign-in names the account; an organization or project set for API
        # keys (OPENAI_ORG_ID, OPENAI_PROJECT_ID) must not travel with it.
        self.openai_organization = None
        for client in (self.root_client, self.root_async_client):
            client.organization = None
            client.project = None

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        for item in payload.get("input") or []:
            if isinstance(item, dict) and item.get("role") == "system":
                item["role"] = "developer"
        return payload


# How a stream may end. A clean finish_reason or Responses API status is a
# complete answer; these mean the model stopped before the answer was done.
_CUT_SHORT = frozenset({
    "length", "content_filter", "max_tokens", "max_output_tokens",
    "incomplete", "failed", "cancelled",
})
# Responses API statuses that are not an ending at all.
_NOT_FINAL = frozenset({"in_progress", "queued"})


def _endings(chunk) -> list[str]:
    info = getattr(chunk, "generation_info", None) or {}
    meta = getattr(getattr(chunk, "message", None), "response_metadata", None) or {}
    found = []
    for value in (info.get("finish_reason"), meta.get("finish_reason"), meta.get("status")):
        if isinstance(value, str) and value and value not in _NOT_FINAL:
            found.append(value)
    return found


UNFINISHED = "<no ending>"


# Endpoints (base URLs) that refused stream_options.include_usage in this process.
_NO_STREAM_USAGE: set[str] = set()
# Set on a streamed answer's response_metadata when its token usage is unknown.
USAGE_UNKNOWN = "usage_unknown"


def _status_of(exc: BaseException) -> int | None:
    item, seen = exc, set()
    while item is not None and id(item) not in seen:
        seen.add(id(item))
        status = getattr(item, "status_code", None)
        if status is None:
            status = getattr(getattr(item, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        item = item.__cause__ or item.__context__
    return None


def rejects_stream_usage(exc: BaseException) -> bool:
    """Whether ``exc`` may be the endpoint refusing the stream usage option.

    Any HTTP 400 or 422, whatever its text (an endpoint may say only "invalid
    request"). Only consulted for a request that carried the option. Auth,
    timeout, rate-limit and server errors (401/403/408/429/5xx) never count.
    """
    return _status_of(exc) in (400, 422)


def _stream_outcome(chunks, *, done: bool, done_completes: bool) -> str:
    """``"complete"``, ``UNFINISHED`` or the reason a stream was cut short.

    A finish_reason or final status decides: ``stop`` / ``completed`` (or any
    other clean reason) is complete, ``length`` / ``incomplete`` and the like
    are cut short. With none, the stream is unfinished, except that the Grok
    plan (``done_completes``) accepts a closing ``[DONE]`` (W26).
    """
    endings = [ending for chunk in chunks for ending in _endings(chunk)]
    for ending in endings:
        if ending in _CUT_SHORT:
            return ending
    if endings:
        return "complete"
    return "complete" if done and done_completes else UNFINISHED


class _DoneWatchingDecoder:
    """An SSE decoder that tells the attempt in flight it saw ``data: [DONE]``."""

    def __init__(self, inner):
        self._inner = inner

    def iter_bytes(self, iterator):
        for event in self._inner.iter_bytes(iterator):
            if isinstance(getattr(event, "data", None), str) and event.data.startswith("[DONE]"):
                _policy.note_stream_done()
            yield event

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _watch_done_marker(client) -> None:
    """Make ``client``'s streams report a ``[DONE]`` marker (this client only)."""
    make = getattr(client, "_make_sse_decoder", None)
    if make is None or getattr(make, "tradingagents_done", False):
        return

    def watching():
        return _DoneWatchingDecoder(make())

    watching.tradingagents_done = True
    try:
        client._make_sse_decoder = watching
    except Exception:  # noqa: BLE001 - without it a stream needs a finish_reason
        logger.debug("could not watch for [DONE] on %r", client, exc_info=True)


def apply_subscription_defaults(kwargs: dict) -> dict:
    """Streamed calls for a signed-in plan, only when the call is bounded.

    A caller that configured a call policy (the console always does; the CLI
    only with ``llm_call_budget``) gets a streamed answer: the read timeout is
    then the longest silence between chunks, not the time to the whole answer,
    which a long debate turn can exceed. A streamed call also asks for its token
    usage, so the stats line counts it. Without a policy nothing changes from
    before RUN-002 (W17).
    """
    if kwargs.get("call_policy") is not None:
        kwargs.setdefault("streaming", True)
        # langchain-openai asks for usage in a stream (stream_options.include_usage)
        # only for api.openai.com; the plan's own base URL got none, so streamed
        # calls logged no tokens (Eric's 47-min run showed ~1.1k output tokens).
        if (kwargs.get("base_url") or kwargs.get("openai_api_base") or "") not in _NO_STREAM_USAGE:
            kwargs.setdefault("stream_usage", True)
    return kwargs


class GrokPlanChatOpenAI(NormalizedChatOpenAI):
    """Grok subscription inference, isolated from API organization/project settings.

    Bounded (console) calls stream, see ``apply_subscription_defaults``: before
    RUN-002 a long Bull Researcher turn waited for the whole answer before the
    first byte and hit the read timeout three times in a row. A stream that
    closes with ``[DONE]`` but no finish_reason is accepted as complete (W26).
    """

    done_marker_completes: ClassVar[bool] = True

    def __init__(self, **kwargs):
        apply_subscription_defaults(kwargs)
        super().__init__(**kwargs)
        self.openai_organization = None
        for client in (self.root_client, self.root_async_client):
            client.organization = None
            client.project = None


# Kwargs forwarded from user config to ChatOpenAI
_PASSTHROUGH_KWARGS = (
    "timeout", "max_retries", "reasoning_effort", "temperature", "max_tokens",
    "api_key", "callbacks", "http_client", "http_async_client",
)

# OpenAI's ``reasoning_effort`` is only accepted by reasoning models — GPT-5 and
# later, and the o-series. Non-reasoning models (gpt-4.1, gpt-4o, ...) 400 with
# "Unsupported parameter: 'reasoning.effort' is not supported with this model".
# Drop the kwarg for those rather than crash the run.
_OPENAI_REASONING_MODEL = re.compile(r"^(?:gpt-(?:[5-9]|[1-9]\d)|o[1-9])(?:[.-]|$)")


def _supports_reasoning_effort(model: str) -> bool:
    """Whether the (native OpenAI) model accepts ``reasoning_effort``."""
    return bool(_OPENAI_REASONING_MODEL.match(model.lower().strip()))


@dataclass(frozen=True)
class ProviderSpec:
    """Declarative config for one OpenAI-compatible provider.

    The OpenAI-compatible family (OpenAI, xAI, DeepSeek, Qwen, GLM, MiniMax,
    OpenRouter, Ollama, and any user endpoint) all speak the same Chat
    Completions API and differ only by these fields — so one row here replaces
    the former per-provider base-URL dict, auth handling, and client-class
    branches. Native Anthropic / Google use their own clients (genuinely
    different APIs) and are intentionally NOT in this registry.

    The API-key env var stays in ``api_key_env.PROVIDER_API_KEY_ENV`` (the single
    source consulted by both this client and the CLI prompt); only behavior that
    is provider-specific (base URL, key optionality, wire-format quirks via
    ``chat_class``) lives here.
    """

    chat_class: type = NormalizedChatOpenAI   # provider quirks live in the subclass
    base_url: str | None = None            # default endpoint (None -> SDK default)
    base_url_env: str | None = None        # env var that overrides base_url (e.g. OLLAMA_BASE_URL)
    key_optional: bool = False                # don't require/prompt; send a placeholder if unset
    placeholder_key: str = "EMPTY"            # sent when no key is available (keyless local servers)
    require_base_url: bool = False            # error if no base_url is resolved (generic endpoint)
    use_responses_api: bool = False           # native OpenAI Responses API
    # A provider the user signs in to: ``credential()`` returns the api_key callable
    # (a fresh token per request) or raises when nobody is signed in. Its token goes
    # only to its pinned base_url, and never alongside an API key.
    credential: Callable[[], Callable[[], str]] | None = None
    pinned_base_url: bool = False             # refuse any other endpoint for this provider


# Single source of truth for the OpenAI-compatible provider family. Dual-region
# providers (qwen/glm/minimax) keep separate endpoints because international and
# China accounts cannot share credentials (#758).
OPENAI_COMPATIBLE_PROVIDERS: dict[str, ProviderSpec] = {
    "openai":     ProviderSpec(use_responses_api=True),
    "xai":        ProviderSpec(base_url="https://api.x.ai/v1"),
    "deepseek":   ProviderSpec(base_url="https://api.deepseek.com", chat_class=DeepSeekChatOpenAI),
    "qwen":       ProviderSpec(base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    "qwen-cn":    ProviderSpec(base_url="https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "glm":        ProviderSpec(base_url="https://api.z.ai/api/paas/v4/"),
    "glm-cn":     ProviderSpec(base_url="https://open.bigmodel.cn/api/paas/v4/"),
    "minimax":    ProviderSpec(base_url="https://api.minimax.io/v1", chat_class=MinimaxChatOpenAI),
    "minimax-cn": ProviderSpec(base_url="https://api.minimaxi.com/v1", chat_class=MinimaxChatOpenAI),
    "openrouter": ProviderSpec(base_url="https://openrouter.ai/api/v1"),
    "mistral":    ProviderSpec(base_url="https://api.mistral.ai/v1"),
    "kimi":       ProviderSpec(base_url="https://api.moonshot.ai/v1"),
    "groq":       ProviderSpec(base_url="https://api.groq.com/openai/v1"),
    "nvidia":     ProviderSpec(base_url="https://integrate.api.nvidia.com/v1"),
    "ollama":     ProviderSpec(base_url="http://localhost:11434/v1", base_url_env="OLLAMA_BASE_URL",
                               key_optional=True, placeholder_key="ollama",
                               chat_class=LocalCompatibleChatOpenAI),
    # Generic endpoint: user supplies base_url; key optional (keyless local).
    "openai_compatible": ProviderSpec(
        require_base_url=True, key_optional=True, chat_class=LocalCompatibleChatOpenAI
    ),
}


# Auth lifecycle and inference transport are independent extension points.
_SUBSCRIPTION_TRANSPORTS = {
    "chatgpt_responses": (ChatGPTPlanChatOpenAI, True),
    "grok_chat": (GrokPlanChatOpenAI, False),
}
for _key, _subscription in SUBSCRIPTIONS.items():
    _chat_class, _responses = _SUBSCRIPTION_TRANSPORTS[_subscription.transport]
    OPENAI_COMPATIBLE_PROVIDERS[_key] = ProviderSpec(
        base_url=_subscription.base_url, chat_class=_chat_class,
        use_responses_api=_responses, credential=_subscription.credential,
        pinned_base_url=True,
    )

# Kept as a public compatibility alias; the subscription registry owns membership.
SIGNED_IN_PROVIDERS = tuple(SUBSCRIPTIONS)


def check_endpoint(provider: str, base_url: str | None) -> None:
    """Raise ValueError when a provider pinned to its own endpoint is pointed elsewhere."""
    spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider.lower())
    if spec is not None and spec.pinned_base_url and base_url and base_url != spec.base_url:
        raise ValueError(f"Provider '{provider}' only connects to {spec.base_url}; "
                         "unset backend_url / TRADINGAGENTS_LLM_BACKEND_URL for it.")


def is_openai_compatible(provider: str) -> bool:
    """Whether ``provider`` is served by the OpenAI-compatible registry."""
    return provider.lower() in OPENAI_COMPATIBLE_PROVIDERS


def _is_native_openai_base_url(base_url: str | None) -> bool:
    """True when ``base_url`` is unset or points at api.openai.com.

    The Responses API (/v1/responses) only exists on native OpenAI. A custom
    base_url on the ``openai`` provider (a proxy, gateway, or local server)
    speaks only Chat Completions, so the Responses API must stay off there even
    though the provider spec enables it (#1024).
    """
    if not base_url:
        return True
    if "://" not in base_url:
        base_url = "https://" + base_url
    host = urlparse(base_url).hostname or ""
    return host == "api.openai.com" or host.endswith(".openai.com")


class OpenAIClient(BaseLLMClient):
    """Client for OpenAI, Ollama, OpenRouter, and xAI providers.

    For native OpenAI models, uses the Responses API (/v1/responses) which
    supports reasoning_effort with function tools across all model families
    (GPT-4.1, GPT-5). Third-party compatible providers (xAI, OpenRouter,
    Ollama) use standard Chat Completions.
    """

    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        provider: str = "openai",
        **kwargs,
    ):
        super().__init__(model, base_url, **kwargs)
        self.provider = provider.lower()

    def get_llm(self) -> Any:
        """Return a configured ChatOpenAI instance, driven by the provider registry."""
        self.warn_if_unknown_model()
        llm_kwargs = {"model": self.model}
        spec = OPENAI_COMPATIBLE_PROVIDERS.get(self.provider)
        chat_cls = NormalizedChatOpenAI

        if spec is not None:
            chat_cls = spec.chat_class

            # base_url precedence: explicit client base_url (carries the config /
            # TRADINGAGENTS_LLM_BACKEND_URL value) > provider env override (e.g.
            # OLLAMA_BASE_URL) > provider default. None means use the SDK default.
            env_base_url = os.environ.get(spec.base_url_env) if spec.base_url_env else None
            base_url = self.base_url or env_base_url or spec.base_url
            check_endpoint(self.provider, base_url)
            if spec.require_base_url and not base_url:
                raise ValueError(
                    f"Provider '{self.provider}' requires a base_url. Set it via "
                    "backend_url / TRADINGAGENTS_LLM_BACKEND_URL to your endpoint, "
                    "e.g. http://localhost:8000/v1 (vLLM) or http://localhost:1234/v1 "
                    "(LM Studio)."
                )
            if base_url:
                llm_kwargs["base_url"] = base_url

            # API key: required unless key_optional; keyless local servers get a
            # placeholder. The env-var name is the single source in api_key_env.
            # A signed-in provider sends its own credential, and only that.
            api_key_env = get_api_key_env(self.provider)
            api_key = os.environ.get(api_key_env) if api_key_env else None
            if spec.credential is not None:
                if "api_key" in self.kwargs:
                    raise ValueError(f"Provider '{self.provider}' uses its sign-in, not an api_key.")
                llm_kwargs["api_key"] = spec.credential()
            elif api_key:
                llm_kwargs["api_key"] = api_key
            elif spec.key_optional:
                llm_kwargs["api_key"] = spec.placeholder_key
            elif api_key_env:
                raise ValueError(
                    f"API key for provider '{self.provider}' is not set. "
                    f"Please set the {api_key_env} environment variable "
                    f"(e.g. add {api_key_env}=your_key to your .env file)."
                )

            # The Responses API only exists on native OpenAI; if the user points
            # the openai provider at a custom base_url (proxy/gateway/local), it
            # only speaks Chat Completions, so keep Responses off there (#1024).
            if spec.use_responses_api and (spec.credential is not None or _is_native_openai_base_url(base_url)):
                llm_kwargs["use_responses_api"] = True
        elif self.base_url:
            llm_kwargs["base_url"] = self.base_url

        # Forward user-provided kwargs
        for key in _PASSTHROUGH_KWARGS:
            if key not in self.kwargs:
                continue
            if key == "reasoning_effort" and not _supports_reasoning_effort(self.model):
                continue
            llm_kwargs[key] = self.kwargs[key]

        # CLI-001: a local model server on loopback (Ollama on localhost:11434, vLLM,
        # LM Studio) is reached directly, never through HTTP(S)_PROXY/ALL_PROXY: a
        # system proxy cannot reach this machine's localhost, and NO_PROXY is often
        # unset. A caller's own http clients are kept as given.
        if is_loopback_url(llm_kwargs.get("base_url")):
            import openai

            if "http_client" not in llm_kwargs:
                llm_kwargs["http_client"] = openai.DefaultHttpxClient(trust_env=False)
            if "http_async_client" not in llm_kwargs:
                llm_kwargs["http_async_client"] = openai.DefaultAsyncHttpxClient(trust_env=False)

        # One retry layer under a hard wall-clock budget, only once a call
        # budget is configured: the console always sets one; the CLI keeps the
        # SDK's behaviour unless its config names ``llm_call_budget`` (W17).
        budget = self.kwargs.get("call_budget")
        if budget not in (None, ""):
            llm_kwargs["call_policy"] = _policy.policy_for(self.kwargs.get("max_retries"), budget)
        llm_kwargs["provider_key"] = self.provider

        # The subclass (provider quirks) comes from the registry spec.
        return chat_cls(**llm_kwargs)

    def validate_model(self) -> bool:
        """Validate model for the provider."""
        return validate_model(self.provider, self.model)
