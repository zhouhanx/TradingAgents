"""Grok subscription OAuth: protocol, private storage, refresh, and polling."""

import base64
import inspect
import json
import os
import stat
import threading
import time

import pytest

from tradingagents.llm_clients import grok_auth as auth


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def json(self):
        return self.body


def _jwt(**claims):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part(claims)}.signature"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "AUTH_FILE", tmp_path / "home" / "grok-auth.json")
    monkeypatch.setattr(auth, "_retry_after", 0.0)
    return tmp_path


def _signed_in(expires_at, refresh_token="r1", access_token="a1"):
    auth.AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    auth._write(
        {
            "host_id": "urn:uuid:test",
            "account": {"email": "a@example.com", "subject": "subject-1"},
            "tokens": {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
            },
        }
    )


@pytest.mark.unit
def test_protocol_is_pinned_to_xai_and_uses_the_open_code_device_contract():
    assert auth.DEVICE_AUTHORIZATION_URL == "https://auth.x.ai/oauth2/device/code"
    assert auth.AUTHORIZE_URL == "https://auth.x.ai/oauth2/authorize"
    assert auth.TOKEN_URL == "https://auth.x.ai/oauth2/token"
    assert auth.API_BASE == "https://api.x.ai/v1"
    assert auth.CLIENT_ID == "b1a00492-073a-47ea-816f-4c329264a828"
    assert auth.DEVICE_CODE_GRANT_TYPE == "urn:ietf:params:oauth:grant-type:device_code"
    assert auth.SCOPE == "openid profile email offline_access grok-cli:access api:access"


@pytest.mark.unit
def test_oauth_requests_do_not_follow_redirects(home, monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(("post", url, kwargs))
        return Response(
            {
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://x.ai/device",
            }
        )

    monkeypatch.setattr(auth.requests, "post", post)
    auth.request_device_code()
    assert calls[0][2]["allow_redirects"] is False

    _signed_in(time.time() + 3600)

    def get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return Response({"object": "list", "data": []})

    monkeypatch.setattr(auth.requests, "get", get)
    auth.list_models()
    assert calls[-1][2]["allow_redirects"] is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "field, value",
    [
        ("verification_uri", "http://x.ai/device"),
        ("verification_uri", "https://evil.x.ai/device"),
        ("verification_uri", "https://x.ai.evil.example/device"),
        ("verification_uri", "https://x.ai:444/device"),
        ("verification_uri", "https://x.ai@evil.example/device"),
        ("verification_uri_complete", "https://evil.x.ai/device?user_code=ABCD"),
        ("verification_uri_complete", "https://x.ai/device#user_code=ABCD"),
    ],
)
def test_device_verification_urls_must_be_https_exact_xai_hosts(home, monkeypatch, field, value):
    body = {
        "device_code": "device-secret",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://x.ai/device",
        "verification_uri_complete": "https://x.ai/device?user_code=ABCD-EFGH",
    }
    body[field] = value
    monkeypatch.setattr(auth.requests, "post", lambda *args, **kwargs: Response(body))
    with pytest.raises(auth.GrokAuthError, match="untrusted"):
        auth.request_device_code()


@pytest.mark.unit
def test_device_verification_urls_accept_the_exact_xai_hosts(home, monkeypatch):
    for host in auth.VERIFICATION_HOSTS:
        body = {
            "device_code": "device-secret",
            "user_code": "ABCD-EFGH",
            "verification_uri": f"https://{host}/device",
        }
        monkeypatch.setattr(auth.requests, "post", lambda *args, body=body, **kwargs: Response(body))
        assert auth.request_device_code()["verification_uri"].startswith(f"https://{host}/")


@pytest.mark.unit
def test_missing_sign_in_fails_without_a_network_request(home, monkeypatch):
    called = []
    monkeypatch.setattr(auth.requests, "post", lambda *args, **kwargs: called.append(args))
    with pytest.raises(auth.GrokAuthError, match="login grok"):
        auth.credential()
    with pytest.raises(auth.GrokAuthError, match="login grok"):
        auth.access_token()
    assert called == []


@pytest.mark.unit
def test_oauth_errors_do_not_echo_token_like_descriptions(home, monkeypatch):
    secret = "refresh-token-that-must-not-be-printed"
    body = {"error": "invalid_grant", "error_description": secret}

    def post(url, **kwargs):
        return Response(body, 400)

    monkeypatch.setattr(auth.requests, "post", post)
    with pytest.raises(auth.GrokAuthError) as raised:
        auth.request_device_code()
    assert secret not in str(raised.value)

    arbitrary = "refresh-token-that-must-not-be-a-code"
    assert auth._oauth_error(Response({"error": arbitrary}, 500)) == "HTTP 500"


@pytest.mark.unit
def test_device_poll_handles_pending_and_slow_down_with_bounded_backoff(monkeypatch):
    device = {
        "device_code": "device-secret",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://x.ai/activate",
        "expires_in": 30,
        "interval": 1,
    }
    answers = iter(
        [
            Response({"error": "authorization_pending"}, 400),
            Response({"error": "slow_down"}, 400),
            Response({"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}, 200),
        ]
    )
    clock = [100.0]
    delays = []

    def post(url, form):
        assert url == auth.TOKEN_URL
        assert form == {
            "grant_type": auth.DEVICE_CODE_GRANT_TYPE,
            "client_id": auth.CLIENT_ID,
            "device_code": "device-secret",
        }
        return next(answers)

    def sleep(seconds):
        delays.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(auth, "_request_post", post)
    result = auth.poll_device_code_token(device, sleep=sleep, now=lambda: clock[0])
    assert result["access_token"] == "access"
    assert delays == [4.0, 9.0]


@pytest.mark.unit
@pytest.mark.parametrize(
    "error, message",
    [
        ("access_denied", "denied"),
        ("authorization_denied", "denied"),
        ("expired_token", "expired"),
    ],
)
def test_device_poll_reports_terminal_errors(home, monkeypatch, error, message):
    device = {
        "device_code": "device-secret",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://x.ai/activate",
        "expires_in": 30,
    }
    monkeypatch.setattr(auth, "_request_post", lambda *args, **kwargs: Response({"error": error}, 400))
    with pytest.raises(auth.GrokAuthError, match=message):
        auth.poll_device_code_token(device, sleep=lambda _: None)


@pytest.mark.unit
def test_device_poll_has_a_hard_timeout_before_any_poll(home):
    device = {
        "device_code": "device-secret",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://x.ai/activate",
        "expires_in": 30,
    }
    with pytest.raises(auth.GrokAuthError, match="timed out"):
        auth.poll_device_code_token(device, timeout=0, sleep=lambda _: None)


@pytest.mark.unit
def test_sign_in_prints_only_the_device_instructions_and_writes_private_tokens(home, monkeypatch):
    printed = []
    opened = []
    device = {
        "device_code": "device-secret-not-for-display",
        "user_code": "ABCD-EFGH",
        "verification_uri": "https://x.ai/activate",
        "verification_uri_complete": "https://x.ai/activate?user_code=ABCD-EFGH",
        "expires_in": 30,
    }
    tokens = {
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "expires_in": 3600,
        "id_token": _jwt(email="a@example.com", sub="subject-1"),
    }
    monkeypatch.setattr(auth, "request_device_code", lambda: device)
    monkeypatch.setattr(auth, "poll_device_code_token", lambda *args, **kwargs: tokens)

    email = auth.sign_in(notify=printed.append, open_browser=opened.append)

    assert email == "a@example.com"
    assert opened == [device["verification_uri_complete"]]
    assert any("ABCD-EFGH" in line for line in printed)
    assert all(secret not in "\n".join(printed) for secret in ("device-secret-not-for-display", "access-secret", "refresh-secret"))
    stored = json.loads(auth.AUTH_FILE.read_text())
    assert stored["account"] == {"email": "a@example.com", "subject": "subject-1"}
    assert stored["tokens"]["refresh_token"] == "refresh-secret"
    if os.name != "nt":
        assert stat.S_IMODE(auth.AUTH_FILE.stat().st_mode) == 0o600


@pytest.mark.unit
def test_signed_in_status_has_a_generic_fallback_without_email_claims(home):
    _signed_in(time.time() + 3600)
    auth._write({"tokens": json.loads(auth.AUTH_FILE.read_text())["tokens"]})
    assert auth.signed_in_as() == auth.SIGNED_IN_FALLBACK


@pytest.mark.unit
def test_sign_in_does_not_expose_a_poll_interval_override():
    assert "poll_interval" not in inspect.signature(auth.sign_in).parameters


@pytest.mark.unit
def test_refresh_rotation_is_single_flight_across_threads(home, monkeypatch):
    _signed_in(time.time() + 30)
    calls = []
    lock = threading.Lock()

    def post(url, form):
        with lock:
            calls.append((url, dict(form)))
        time.sleep(0.03)
        return Response({"access_token": "a2", "refresh_token": "r2", "expires_in": 3600})

    monkeypatch.setattr(auth, "_request_post", post)
    values = []
    threads = [threading.Thread(target=lambda: values.append(auth.access_token())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert values == ["a2"] * 4
    assert len(calls) == 1
    assert calls[0][1]["refresh_token"] == "r1"
    stored = json.loads(auth.AUTH_FILE.read_text())
    assert stored["tokens"]["refresh_token"] == "r2"
    if os.name != "nt":
        assert stat.S_IMODE(auth.AUTH_FILE.stat().st_mode) == 0o600


@pytest.mark.unit
def test_temporary_refresh_failure_keeps_a_live_token_then_is_bounded(home, monkeypatch):
    clock = [time.time()]
    _signed_in(clock[0] + 150)
    calls = []

    def unavailable(url, **kwargs):
        calls.append(1)
        raise auth.requests.ConnectionError("reset")

    monkeypatch.setattr(auth.requests, "post", unavailable)
    assert auth.access_token(now=lambda: clock[0]) == "a1"
    assert auth.access_token(now=lambda: clock[0] + 100) == "a1"
    assert auth.access_token(now=lambda: clock[0] + 110) == "a1"
    assert len(calls) == 1

    _signed_in(clock[0] - 1)
    with pytest.raises(auth.GrokUnreachable, match="try again"):
        auth.access_token(now=lambda: clock[0])
    assert len(calls) == 1  # retry suppression is shared by this process


@pytest.mark.unit
def test_list_models_uses_xai_data_contract_and_bearer(home, monkeypatch):
    _signed_in(time.time() + 3600)
    seen = []

    def get(url, timeout, headers, allow_redirects):
        seen.append((url, timeout, headers, allow_redirects))
        return Response(
            {
                "object": "list",
                "data": [
                    {"object": "model", "id": "grok-4.6"},
                    {"object": "model", "id": "grok-build-0.1"},
                    {"object": "image", "id": "ignore-me"},
                ],
            }
        )

    monkeypatch.setattr(auth.requests, "get", get)
    assert auth.list_models() == [("grok-4.6", "grok-4.6"), ("grok-build-0.1", "grok-build-0.1")]
    assert seen == [(auth.MODELS_URL, auth.TIMEOUT, {"Authorization": "Bearer a1"}, False)]


@pytest.mark.unit
def test_logout_forgets_locally_without_claiming_xai_revocation(home, monkeypatch):
    _signed_in(time.time() + 3600)
    calls = []
    monkeypatch.setattr(auth.requests, "post", lambda *args, **kwargs: calls.append(args))
    assert auth.sign_out() == "forgotten"
    assert json.loads(auth.AUTH_FILE.read_text()) == {"host_id": "urn:uuid:test"}
    assert auth.signed_in_as() is None
    assert auth.sign_out() is None
    assert calls == []
