"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call fails to produce a usable result
   (malformed JSON from a weak model, no parsed result), fall back to a
   plain ``llm.invoke`` so the pipeline never blocks. So does a bounded
   call the model refused with 400/422 (no structured-output support). Any
   other model call that failed under the call policy (already retried
   within its budget) is raised instead of being run again.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Schema-only structured output binds exactly one tool (the schema itself), so a
# model that reaches for a search tool emits an unknown tool call and the whole
# structured attempt is discarded for a free-text retry. Agents on this path
# state the constraint explicitly rather than relying on the binding alone
# (#1130).
NO_EXTERNAL_TOOLS = (
    "Use only the evidence provided in this prompt. Do not call external tools "
    "or search the web; if something is missing, say so explicitly."
)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured(structured_llm: Any | None, prompt: Any, agent_name: str) -> T | None:
    """Run the structured call; ``None`` when there is none or it fails.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape), so a caller can forward the same value to its free-text fallback.
    """
    if structured_llm is None:
        return None
    try:
        result = structured_llm.invoke(prompt)
        if result is None:
            # A thinking model can answer in plain text instead of calling
            # the tool, leaving the parser with nothing to return. Treat it
            # as a structured miss and fall back, with a clear reason.
            raise ValueError("structured output returned no parsed result")
        return result
    except Exception as exc:
        if _model_call_failed(exc):
            # The model call itself failed under the call policy, which has
            # already retried it within its budget. A free-text rerun would
            # repeat every retry and double the budget (RUN-002 MF1).
            raise
        logger.warning(
            "%s: structured-output invocation failed (%s); retrying once as free text",
            agent_name, exc,
        )
        return None


# A model that rejects the structured request itself (tools / response_format
# not supported) answers 400 or 422 at once; one free-text request is the
# pre-RUN-002 behaviour and costs no retries or budget (W25).
_STRUCTURED_REJECTED = (400, 422)


def _model_call_failed(exc: BaseException) -> bool:
    """Whether ``exc`` is a policy-managed model call that already gave up.

    A parse or schema failure (no call annotation) earns the free-text
    fallback, and so does a bounded call the server refused with 400/422,
    which was not retried. A timeout, connection, rate-limit, 5xx, auth or
    other failure of a bounded call is raised as it is (MF1).
    """
    from tradingagents.llm_clients.call_policy import KIND_OTHER, call_info, http_status

    info = call_info(exc)
    if info is None:
        return False
    return not (info.get("kind") == KIND_OTHER and http_status(exc) in _STRUCTURED_REJECTED)


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure."""
    result = invoke_structured(structured_llm, prompt, agent_name)
    if result is not None:
        return render(result)
    return plain_llm.invoke(prompt).content
