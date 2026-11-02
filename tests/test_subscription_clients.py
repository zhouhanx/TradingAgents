"""Subscription routing must never fall through to API-key credentials."""

import json

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.factory import create_llm_client, create_tier_client
from tradingagents.llm_clients.model_catalog import get_model_options
from tradingagents.llm_clients.openai_client import (
    OPENAI_COMPATIBLE_PROVIDERS,
    SIGNED_IN_PROVIDERS,
    ChatGPTPlanChatOpenAI,
    GrokPlanChatOpenAI,
)
from tradingagents.llm_clients.subscriptions import SUBSCRIPTIONS, get_subscription


@pytest.fixture
def signed_plans(monkeypatch):
    for key, spec in SUBSCRIPTIONS.items():
        monkeypatch.setattr(spec.auth, "credential", lambda key=key: lambda: f"{key}-test-token")


def test_registry_drives_auth_and_catalog():
    assert set(SIGNED_IN_PROVIDERS) == set(SUBSCRIPTIONS) == {"chatgpt", "grok"}
    assert get_subscription("GROK") is SUBSCRIPTIONS["grok"]
    for key, spec in SUBSCRIPTIONS.items():
        assert get_api_key_env(key) is None
        assert get_model_options(key, "quick") == [("Custom model ID", "custom")]
        assert OPENAI_COMPATIBLE_PROVIDERS[key].base_url == spec.base_url
    with pytest.raises(ValueError, match="Unknown subscription"):
        get_subscription("unregistered")


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
def test_subscription_rejects_custom_endpoint_and_explicit_key(provider, signed_plans):
    with pytest.raises(ValueError, match="only connects"):
        create_llm_client(provider, "model", base_url="https://relay.example/v1").get_llm()
    with pytest.raises(ValueError, match="not an api_key"):
        create_llm_client(provider, "model", api_key="wrong-key").get_llm()


def test_two_subscription_tiers_keep_their_own_credentials(signed_plans, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "api-key-not-for-plans")
    monkeypatch.setenv("XAI_API_KEY", "other-api-key-not-for-plans")
    monkeypatch.setenv("OPENAI_ORG_ID", "api-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "api-project")
    config = {
        **DEFAULT_CONFIG,
        "llm_provider": "chatgpt", "backend_url": "https://api.openai.com/v1",
        "quick_think_provider": "grok", "quick_think_llm": "grok-build",
        "deep_think_provider": "chatgpt", "deep_think_llm": "gpt-5.5",
    }
    quick = create_tier_client(config, "quick").get_llm()
    deep = create_tier_client(config, "deep").get_llm()
    assert isinstance(quick, GrokPlanChatOpenAI)
    assert isinstance(deep, ChatGPTPlanChatOpenAI)
    assert quick.openai_api_base == SUBSCRIPTIONS["grok"].base_url
    assert quick.openai_api_key() == "grok-test-token"
    assert deep.openai_api_key() == "chatgpt-test-token"
    for llm in (quick, deep):
        for client in (llm.root_client, llm.root_async_client):
            assert client.organization is None and client.project is None


def test_grok_key_mode_remains_separate(signed_plans, monkeypatch):
    monkeypatch.setenv("XAI_API_KEY", "xai-api-test-key")
    llm = create_llm_client("xai", "grok-4.7").get_llm()
    assert not isinstance(llm, GrokPlanChatOpenAI)
    assert llm.openai_api_key.get_secret_value() == "xai-api-test-key"


def test_grok_subscription_can_bind_tools(signed_plans):
    llm = create_llm_client("grok", "grok-build").get_llm()
    bound = llm.bind_tools([{
        "type": "function", "function": {
            "name": "lookup", "description": "Look up a price",
            "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}},
        },
    }])
    payload = llm._get_request_payload(
        [SystemMessage("Use the lookup tool."), HumanMessage("Look up NVDA")], **bound.kwargs,
    )
    assert payload["tools"][0]["function"]["name"] == "lookup"
    assert payload["model"] == "grok-build"


def test_grok_missing_sign_in_does_not_use_api_key(monkeypatch, tmp_path):
    auth = get_subscription("grok").auth
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "missing.json")
    monkeypatch.setenv("XAI_API_KEY", "available-api-key")
    with pytest.raises(auth.GrokAuthError, match="login grok"):
        create_llm_client("grok", "grok-build").get_llm()


def test_grok_tool_roundtrip_uses_fresh_subscription_tokens(monkeypatch):
    """Exercise the actual SDK serialization and LangChain tool response parser."""
    auth = get_subscription("grok").auth
    tokens = iter(["first-access-token", "refreshed-access-token"])
    monkeypatch.setattr(auth, "credential", lambda: lambda: next(tokens))
    seen = []

    def serve(request):
        assert str(request.url) == "https://api.x.ai/v1/chat/completions"
        seen.append((request.headers["authorization"], json.loads(request.content)))
        message = {"role": "assistant", "content": "Price checked."}
        if len(seen) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_price", "type": "function",
                "function": {"name": "lookup", "arguments": '{"ticker":"NVDA"}'},
            }]}
        return httpx.Response(200, json={
            "id": "response-test", "object": "chat.completion", "created": 1,
            "model": "grok-build", "choices": [{"index": 0, "message": message,
                "finish_reason": "tool_calls" if len(seen) == 1 else "stop"}],
        })

    with httpx.Client(transport=httpx.MockTransport(serve)) as client:
        llm = create_llm_client("grok", "grok-build", http_client=client, max_retries=0).get_llm()
        bound = llm.bind_tools([{"type": "function", "function": {
            "name": "lookup", "description": "Get price",
            "parameters": {"type": "object", "properties": {"ticker": {"type": "string"}}},
        }}])
        question = HumanMessage("Check NVDA price")
        first = bound.invoke([question])
        assert first.tool_calls[0]["args"] == {"ticker": "NVDA"}
        final = bound.invoke([question, first, ToolMessage("100", tool_call_id="call_price")])
    assert final.content == "Price checked."
    assert [header for header, _ in seen] == ["Bearer first-access-token", "Bearer refreshed-access-token"]
    assert seen[1][1]["messages"][-1]["tool_call_id"] == "call_price"
