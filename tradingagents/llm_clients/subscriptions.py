"""Subscription metadata and a lazy authentication adapter boundary.

Add an adapter here to expose its login, logout, status and model discovery in
the CLI. Authentication stays separate from the inference transport; API-key
providers retain their own names and are never used as a fallback for a plan.
"""

from dataclasses import dataclass
from importlib import import_module
from types import ModuleType
from typing import Literal


@dataclass(frozen=True)
class SubscriptionSpec:
    key: str
    display_name: str
    base_url: str
    auth_module: str
    transport: Literal["chatgpt_responses", "grok_chat"]
    auth_error_name: str
    unreachable_error_name: str

    @property
    def auth(self) -> ModuleType:
        return import_module(self.auth_module)

    @property
    def auth_error(self) -> type[Exception]:
        return getattr(self.auth, self.auth_error_name)

    @property
    def unreachable_error(self) -> type[Exception]:
        return getattr(self.auth, self.unreachable_error_name)

    def credential(self):
        return self.auth.credential()


SUBSCRIPTIONS = {
    "chatgpt": SubscriptionSpec(
        key="chatgpt", display_name="ChatGPT (Plus or Pro plan)",
        base_url="https://api.openai.com/v1",
        auth_module="tradingagents.llm_clients.chatgpt_auth",
        transport="chatgpt_responses", auth_error_name="ChatGPTAuthError",
        unreachable_error_name="ChatGPTUnreachable",
    ),
    "grok": SubscriptionSpec(
        key="grok", display_name="Grok (subscription)",
        base_url="https://api.x.ai/v1",
        auth_module="tradingagents.llm_clients.grok_auth",
        transport="grok_chat", auth_error_name="GrokAuthError",
        unreachable_error_name="GrokUnreachable",
    ),
}


def get_subscription(provider: str) -> SubscriptionSpec:
    try:
        return SUBSCRIPTIONS[provider.lower()]
    except KeyError:
        raise ValueError(f"Unknown subscription {provider!r}; choose {', '.join(SUBSCRIPTIONS)}.") from None
