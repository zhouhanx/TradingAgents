
import threading
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult


def _callback_role(kwargs: dict) -> str:
    """Graph node name when the caller passed it. Empty when the call is unattributed."""
    meta = kwargs.get("metadata") if isinstance(kwargs, dict) else None
    if not isinstance(meta, dict):
        return ""
    role = meta.get("langgraph_node") or meta.get("role")
    return role.strip() if isinstance(role, str) else ""


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
        # V2-COST-STATS: tokens by graph node when the callback metadata names one.
        self.by_role = {}

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        **kwargs: Any,
    ) -> None:
        """Increment LLM call counter when an LLM starts."""
        with self._lock:
            self.llm_calls += 1

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        **kwargs: Any,
    ) -> None:
        """Increment LLM call counter when a chat model starts."""
        with self._lock:
            self.llm_calls += 1

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        """Extract token usage from LLM response."""
        try:
            generation = response.generations[0][0]
        except (IndexError, TypeError):
            with self._lock:                 # W81: a finished call with no answer
                self.usage_unknown += 1
            return

        usage_metadata = None
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
                role = _callback_role(kwargs)
                if role:
                    bucket = self.by_role.setdefault(role, {"input_tokens": 0, "output_tokens": 0})
                    bucket["input_tokens"] += incoming
                    bucket["output_tokens"] += outgoing

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        """W81: a call that failed was still a call; its usage is unknown."""
        with self._lock:
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
            }
