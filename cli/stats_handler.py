
import threading
from datetime import datetime
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult


def _outer_node(namespace) -> str:
    """The top-level graph node from a LangGraph checkpoint namespace.

    ``"Market Analyst:<id>|agent:<id>"`` → ``"Market Analyst"``: an analyst's model
    runs inside its own subgraph, where ``langgraph_node`` is only ``"agent"``.
    """
    if not isinstance(namespace, str) or not namespace.strip():
        return ""
    return namespace.split("|", 1)[0].split(":", 1)[0].strip()


def _callback_role(kwargs: dict) -> str:
    """The graph role a model call belongs to. Empty when the call is unattributed.

    LangChain passes ``metadata`` to the start callbacks only, never to
    ``on_llm_end``; the handler remembers the role by ``run_id`` at start.
    """
    meta = kwargs.get("metadata") if isinstance(kwargs, dict) else None
    if not isinstance(meta, dict):
        return ""
    role = (_outer_node(meta.get("langgraph_checkpoint_ns")) or _outer_node(meta.get("checkpoint_ns"))
            or meta.get("langgraph_node") or meta.get("role"))
    return role.strip() if isinstance(role, str) else ""


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _details(value) -> dict:
    """Provider token breakdowns (e.g. cache_read, reasoning), integers only."""
    if not isinstance(value, dict):
        return {}
    return {str(key): number for key, value in value.items() if (number := _count(value)) is not None}


def _model_name(message, invocation: dict) -> str:
    meta = getattr(message, "response_metadata", None)
    for value in ((meta or {}).get("model_name"), (meta or {}).get("model"),
                  invocation.get("model"), invocation.get("model_name")):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _response_id(message) -> str:
    meta = getattr(message, "response_metadata", None) or {}
    for value in (meta.get("id"), getattr(message, "id", None)):
        if isinstance(value, str) and value.strip() and not value.startswith(("run-", "lc_run-")):
            return value.strip()
    return ""


class StatsCallbackHandler(BaseCallbackHandler):
    """Callback handler that tracks LLM calls, tool calls, and token usage."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.llm_calls = 0
        self.tool_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        # Calls whose token usage is unknown (the endpoint would not report it).
        self.usage_unknown = 0
        # Calls that did report usage, even 0/0 (W81: then the footer shows 0, not --).
        self.usage_reported = 0
        # V2-COST-STATS: tokens by graph role when the start callback named one.
        self.by_role = {}
        # V2-COST-STATS: one row per finished call that reported usage, as the provider sent it.
        self.calls = []
        self._started: dict = {}   # run_id -> (role, node, invocation params)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """Increment LLM call counter when an LLM starts."""
        with self._lock:
            self.llm_calls += 1
            self._remember(kwargs)

    def _remember(self, kwargs: dict) -> None:
        run_id = kwargs.get("run_id")
        if run_id is None:
            return
        meta = kwargs.get("metadata") if isinstance(kwargs.get("metadata"), dict) else {}
        node = meta.get("langgraph_node") if isinstance(meta.get("langgraph_node"), str) else ""
        params = kwargs.get("invocation_params") if isinstance(kwargs.get("invocation_params"), dict) else {}
        self._started[run_id] = (_callback_role(kwargs), node, params)

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        """Increment LLM call counter when a chat model starts."""
        with self._lock:
            self.llm_calls += 1
            self._remember(kwargs)

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Extract token usage from LLM response."""
        with self._lock:
            role, node, params = self._started.pop(kwargs.get("run_id"), ("", "", {}))
        role = role or _callback_role(kwargs)
        try:
            generation = response.generations[0][0]
        except (IndexError, TypeError):
            with self._lock:                 # W81: a finished call with no answer
                self.usage_unknown += 1
            return

        usage_metadata = None
        message = None
        if hasattr(generation, "message"):
            message = generation.message
            if isinstance(message, AIMessage) and hasattr(message, "usage_metadata"):
                usage_metadata = message.usage_metadata

        # W75: a call that ended without usage is unknown, whether the endpoint
        # refused the usage option (marked usage_unknown) or accepted/ignored it and
        # sent none. "Tokens: --" stays only for a run with no finished model call.
        if not usage_metadata:
            with self._lock:
                self.usage_unknown += 1
        if usage_metadata:
            with self._lock:
                self.usage_reported += 1
                incoming = int(usage_metadata.get("input_tokens") or 0)
                outgoing = int(usage_metadata.get("output_tokens") or 0)
                self.tokens_in += incoming
                self.tokens_out += outgoing
                if role:
                    bucket = self.by_role.setdefault(role, {"input_tokens": 0, "output_tokens": 0})
                    bucket["input_tokens"] += incoming
                    bucket["output_tokens"] += outgoing
                self.calls.append({
                    "role": role,
                    "node": node,
                    "model": _model_name(message, params),
                    "response_id": _response_id(message),
                    "input_tokens": incoming,
                    "output_tokens": outgoing,
                    "total_tokens": _count(usage_metadata.get("total_tokens")),
                    "input_token_details": _details(usage_metadata.get("input_token_details")),
                    "output_token_details": _details(usage_metadata.get("output_token_details")),
                    "ended_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                })

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """W81: a call that failed was still a call; its usage is unknown."""
        with self._lock:
            self._started.pop(kwargs.get("run_id"), None)
            self.usage_unknown += 1

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        """Increment tool call counter when a tool starts."""
        with self._lock:
            self.tool_calls += 1

    def get_stats(self) -> dict[str, Any]:
        """Return current statistics."""
        with self._lock:
            return {
                "llm_calls": self.llm_calls,
                "tool_calls": self.tool_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "usage_unknown": self.usage_unknown,
                "usage_reported": self.usage_reported,
                "by_role": {key: dict(value) for key, value in self.by_role.items()},
                "calls": [
                    {**row, "input_token_details": dict(row["input_token_details"]),
                     "output_token_details": dict(row["output_token_details"])}
                    for row in self.calls
                ],
            }
