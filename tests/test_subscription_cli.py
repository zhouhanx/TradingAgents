"""Generic subscription plumbing in the CLI.

These tests use small auth doubles so they exercise dispatch and prompt wiring
without starting an OAuth flow or making a model/status request.
"""

from types import SimpleNamespace
from unittest import mock

import pytest
import typer
from typer.testing import CliRunner

import cli.main as main
import cli.prompts as prompts


@pytest.fixture
def runner():
    return CliRunner()


def _spec(name="Grok", *, auth=None):
    return SimpleNamespace(
        key=name.lower(),
        display_name=name,
        base_url="https://subscription.example/v1",
        auth=auth or SimpleNamespace(),
        auth_error=RuntimeError,
        unreachable_error=ConnectionError,
    )


@pytest.mark.unit
def test_provider_table_uses_subscription_registry_rows():
    rows = {key: (display, url) for display, key, url in prompts._llm_provider_table()}
    assert rows["chatgpt"] == ("ChatGPT (Plus or Pro plan)", "https://api.openai.com/v1")
    assert rows["grok"] == ("Grok (subscription)", "https://api.x.ai/v1")


@pytest.mark.unit
def test_login_dispatches_to_the_selected_subscription(runner, monkeypatch):
    calls = []

    def sign_in(*, say):
        calls.append(say)
        say("paste this redirect")
        return "grok@example.com"

    fake = _spec(auth=SimpleNamespace(sign_in=sign_in))
    monkeypatch.setattr(main, "get_subscription", lambda key: fake)

    result = runner.invoke(main.app, ["login", "chatgpt"])

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert "paste this redirect" in result.output
    assert "Signed in to Grok as grok@example.com" in result.output


@pytest.mark.unit
def test_auth_status_reads_only_local_identity(runner, monkeypatch):

    def access_token():
        raise AssertionError("status must not validate or refresh a token")

    fake = _spec(
        name="Grok",
        auth=SimpleNamespace(
            signed_in_as=lambda: "grok@example.com",
            access_token=access_token,
        ),
    )
    monkeypatch.setattr(main, "get_subscription", lambda key: fake)
    monkeypatch.setattr(main, "SIGNED_IN_PROVIDERS", ("chatgpt",))

    result = runner.invoke(main.app, ["auth-status", "chatgpt"])

    assert result.exit_code == 0, result.output
    assert "signed in as grok@example.com" in result.output
    assert "local status only" in result.output
    assert "not validated live" in " ".join(result.output.split())
    assert "access_token" not in result.output


@pytest.mark.unit
def test_subscription_model_picker_uses_auth_adapter(monkeypatch):
    listed = [("Grok 4", "grok-4")]
    fake = _spec(
        auth=SimpleNamespace(list_models=lambda: listed),
    )
    monkeypatch.setattr(prompts, "get_subscription", lambda key: fake)
    shown = {}

    def select(message, choices, **kwargs):
        shown["message"] = message
        shown["values"] = [choice.value for choice in choices]
        return SimpleNamespace(ask=lambda: "grok-4")

    with mock.patch.object(prompts.questionary, "select", select):
        assert prompts.select_subscription_model("grok", "deep") == "grok-4"

    assert "Grok" in shown["message"]
    assert shown["values"] == ["grok-4", "custom"]


@pytest.mark.unit
def test_subscription_menu_lists_every_signed_in_provider(monkeypatch):
    monkeypatch.setattr(prompts, "SIGNED_IN_PROVIDERS", ("chatgpt", "grok"))
    shown = []
    answers = [
        lambda choices: next(choice.value for choice in choices if choice.value == ("subscription", None)),
        lambda choices: choices[-1].value,
    ]

    def select(message, choices, **kwargs):
        shown.append([choice.title for choice in choices])
        return SimpleNamespace(ask=lambda: answers.pop(0)(choices))

    with mock.patch.object(prompts.questionary, "select", select):
        provider, url = prompts.select_llm_provider()

    assert shown[1] == ["ChatGPT (Plus or Pro plan)", "Grok (subscription)"]
    assert (provider, url) == ("grok", "https://api.x.ai/v1")


@pytest.mark.unit
def test_subscription_sign_in_does_not_prompt_for_an_api_key(monkeypatch):
    monkeypatch.setattr(
        prompts.questionary,
        "password",
        lambda *args, **kwargs: pytest.fail("subscription credentials are not API keys"),
    )
    assert prompts.ensure_api_key("chatgpt") is None
    assert prompts.ensure_api_key("grok") is None


@pytest.mark.unit
def test_transient_subscription_unreachable_never_offers_login(monkeypatch):
    class Unreachable(Exception):
        pass

    def login(*args, **kwargs):
        pytest.fail("transient outage must not start login")
    fake = _spec(
        auth=SimpleNamespace(
            access_token=lambda: (_ for _ in ()).throw(Unreachable("try again")),
        ),
    )
    fake.auth_error = RuntimeError
    fake.unreachable_error = Unreachable
    monkeypatch.setattr(prompts, "get_subscription", lambda key: fake)
    monkeypatch.setattr(prompts, "SIGNED_IN_PROVIDERS", ("grok",))
    monkeypatch.setattr(prompts.subprocess, "run", login)

    with pytest.raises(typer.Exit):
        prompts.ensure_signed_in("grok")
