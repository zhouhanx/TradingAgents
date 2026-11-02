"""Sign in to a Grok subscription with xAI's device-code OAuth flow.

xAI's public OpenCode integration uses RFC 8628 device authorization for
SuperGrok/X Premium accounts.  This module keeps that protocol deliberately
small and separate from the inference client:

* ``https://auth.x.ai/oauth2/device/code`` issues a device code;
* ``https://auth.x.ai/oauth2/token`` exchanges it and refreshes tokens; and
* the resulting bearer is used with ``https://api.x.ai/v1``.

Protocol sources (fetched from official OpenCode commits):

* https://raw.githubusercontent.com/anomalyco/opencode/e8fea9e63a437fb839fa925a6b63ace31b243471/packages/opencode/src/plugin/xai.ts
* https://raw.githubusercontent.com/anomalyco/opencode/fa1260d6ca484fe0fd868aa992ad81ab85343cf3/packages/ai/src/providers/xai.ts

The credentials are kept in one owner-readable file.  Refresh tokens rotate,
so every read/refresh/write is performed under the same process and file lock
used by the other subscription adapter.  xAI does not expose a revoke endpoint
in the public OpenCode adapter, therefore logout forgets the local credential
and reports ``"forgotten"`` rather than claiming that xAI revoked it.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
import uuid
import webbrowser
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests

from tradingagents.dataflows.files import locked
from tradingagents.default_config import _TRADINGAGENTS_HOME
from tradingagents.llm_clients.auth_file_schema import (
    auth_read_problem,
    file_problem,
    read_auth_file_text,
)

# These are the endpoints and client used by OpenCode's official xAI plugin.
# Keep them constants: a subscription credential must never be redirected to a
# user-configured proxy or to an endpoint that merely resembles xAI's domain.
ISSUER = "https://auth.x.ai"
GROK_LOGIN_PROMPT = "Continue with your Grok subscription: open this address and approve access:"
GROK_DEVICE_PROMPT = "If needed, open {verification_uri} and enter code: {user_code}"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
AUTHORIZE_URL = f"{ISSUER}/oauth2/authorize"
DEVICE_AUTHORIZATION_URL = f"{ISSUER}/oauth2/device/code"
TOKEN_URL = f"{ISSUER}/oauth2/token"
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
SCOPE = "openid profile email offline_access grok-cli:access api:access"
API_BASE = "https://api.x.ai/v1"
MODELS_URL = f"{API_BASE}/models"
VERIFICATION_HOSTS = frozenset({"x.ai", "auth.x.ai", "accounts.x.ai"})

AUTH_FILE = Path(_TRADINGAGENTS_HOME) / "grok-auth.json"
LOGIN_HINT = "Not signed in to Grok; run `tradingagents login grok`."

# Network calls are individually bounded.  The device code also has a hard
# deadline from xAI's expires_in, capped by the sign-in timeout supplied by the
# caller.  Intervals are bounded to prevent malformed server values from
# becoming a busy loop or an unbounded sleep.
TIMEOUT = 30
DEFAULT_LOGIN_TIMEOUT = 600
DEFAULT_DEVICE_EXPIRES = 5 * 60
DEFAULT_POLL_INTERVAL = 5.0
MIN_POLL_INTERVAL = 1.0
MAX_POLL_INTERVAL = 60.0
SLOW_DOWN_INCREMENT = 5.0
POLLING_SAFETY_MARGIN = 3.0
REFRESH_MARGIN = 120
RETRY_AFTER = 60
SIGNED_IN_FALLBACK = "your Grok account"

_KNOWN_OAUTH_CODES = frozenset(
    {
        "authorization_pending",
        "slow_down",
        "access_denied",
        "authorization_denied",
        "expired_token",
        "invalid_grant",
        "invalid_client",
        "invalid_request",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "server_error",
        "temporarily_unavailable",
        "interaction_required",
        "login_required",
        "consent_required",
    }
)

_retry_after = 0.0


class GrokAuthError(RuntimeError):
    """Signing in, renewing, or using a Grok subscription was refused."""


class GrokUnreachable(GrokAuthError):
    """xAI could not be reached or was temporarily unavailable."""


def _read() -> dict:
    """Read the credential file, treating an absent/corrupt file as signed out."""
    try:
        data = json.loads(read_auth_file_text(AUTH_FILE))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _shape_problem(data: dict) -> str | None:
    """The same shape ``sign_in`` writes: tokens with a non-empty access token; the
    account labels are optional. Local check only."""
    return file_problem(data, account_required_with_tokens=None)


def auth_file_problem() -> str | None:
    """Why the local credential file cannot be used, or ``None`` when it is absent or
    well formed. Local read only; nothing is validated online.

    ``_read`` treats a corrupt file as signed out so a new sign-in can replace it;
    status displays and every signed-in check call this so a malformed file is never
    reported as signed in or as signed out.
    """
    try:
        raw = read_auth_file_text(AUTH_FILE)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        return auth_read_problem(AUTH_FILE, exc)
    try:
        data = json.loads(raw)
    except ValueError:
        return f"{AUTH_FILE.name} is not valid JSON"
    if not isinstance(data, dict):
        return f"{AUTH_FILE.name} is not a JSON object"
    problem = _shape_problem(data)
    return f"{AUTH_FILE.name}: {problem}" if problem else None


def _well_formed() -> dict:
    """The auth file when it has the expected shape; otherwise only a usable host id."""
    data = _read()
    if not _shape_problem(data):
        return data
    host_id = data.get("host_id")
    return {"host_id": host_id} if isinstance(host_id, str) and host_id else {}


def _refuse_malformed() -> None:
    problem = auth_file_problem()
    if problem:
        raise GrokAuthError(f"The local Grok sign-in file could not be read ({problem}); "
                            "run `tradingagents login grok` again.")


def _write(data: dict) -> None:
    """Atomically replace the credential file with mode 0600."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = AUTH_FILE.with_name(f"{AUTH_FILE.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        # The temporary file is 0600 and os.replace is atomic on the same
        # filesystem; readers see either the old JSON or the new JSON.
        os.replace(temp, AUTH_FILE)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _host_id(data: Mapping[str, Any]) -> str:
    return str(data.get("host_id") or f"urn:uuid:{uuid.uuid4()}")


def signed_in_as() -> str | None:
    """Return the signed-in xAI email, or ``None`` when no well-formed credential
    exists (callers that must tell the two apart ask ``auth_file_problem``)."""
    data = _well_formed()
    tokens = data.get("tokens")
    account = data.get("account")
    if not isinstance(tokens, dict) or not (
        isinstance(tokens.get("access_token"), str) and tokens["access_token"].strip()
    ):
        return None
    email = account.get("email") if isinstance(account, dict) else None
    # A whitespace-only email is not an address (QA W14): it gets the neutral label too.
    return email.strip() if isinstance(email, str) and email.strip() else SIGNED_IN_FALLBACK


# --- OAuth response helpers ---------------------------------------------------------


def _response_json(response: Any) -> dict:
    try:
        body = response.json()
    except (ValueError, TypeError, AttributeError):
        return {}
    return body if isinstance(body, dict) else {}


def _oauth_error(response: Any) -> str:
    body = _response_json(response)
    error = body.get("error")
    if isinstance(error, dict):
        # OAuth ``error`` codes are short, stable identifiers.  Ignore the
        # free-form message/description fields: a misconfigured proxy could
        # echo a bearer or refresh token there, and it must never reach logs.
        error = error.get("code")
    if not isinstance(error, str) or error not in _KNOWN_OAUTH_CODES:
        error = f"HTTP {getattr(response, 'status_code', 'unknown')}"
    # Error codes are useful for the user and do not include token bodies.  Do
    # not include response.text: a proxy can echo a credential in it.
    return error


def _oauth_code(value: Any) -> str | None:
    """Return only a short OAuth error code; never propagate free-form text."""
    if isinstance(value, dict):
        value = value.get("code")
    return value if isinstance(value, str) and value in _KNOWN_OAUTH_CODES else None


def _request_error(task: str, exc: Exception) -> GrokUnreachable:
    return GrokUnreachable(f"Could not reach xAI to {task} ({type(exc).__name__}); try again.")


def _request_post(url: str, form: Mapping[str, Any]):
    """POST a form with the OAuth headers, wrapping network failures safely."""
    try:
        return requests.post(
            url,
            timeout=TIMEOUT,
            data=dict(form),
            allow_redirects=False,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    except requests.RequestException as exc:
        raise _request_error("contact xAI", exc) from None


def _token_body(response: Any, task: str, *, require_refresh: bool = False) -> dict:
    status = getattr(response, "status_code", 0)
    if status in (408, 429) or status >= 500:
        raise GrokUnreachable(f"xAI could not {task} now ({_oauth_error(response)}); try again.")
    if not 200 <= status < 300:
        raise GrokAuthError(
            f"xAI did not {task} ({_oauth_error(response)}); "
            "run `tradingagents login grok` again."
        )
    body = _response_json(response)
    if not isinstance(body.get("access_token"), str) or not body["access_token"]:
        raise GrokUnreachable(f"xAI's answer could not be read when asked to {task}; try again.")
    if require_refresh and (not isinstance(body.get("refresh_token"), str) or not body["refresh_token"]):
        raise GrokAuthError(
            "xAI did not return a refresh token for the Grok subscription; "
            "run `tradingagents login grok` again."
        )
    return body


def _positive_seconds(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 and number != float("inf") else default


def _bounded_interval(value: Any, default: float = DEFAULT_POLL_INTERVAL) -> float:
    return min(MAX_POLL_INTERVAL, max(MIN_POLL_INTERVAL, _positive_seconds(value, default)))


def _jwt_exp(token: str | None) -> float | None:
    """Read an access JWT's exp only for refresh timing, never for trust."""
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    try:
        value = float(exp)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _account_claims(id_token: Any) -> dict[str, str]:
    """Extract non-secret account labels from an OIDC ID token, if present."""
    if not isinstance(id_token, str):
        return {}
    parts = id_token.split(".")
    if len(parts) < 2:
        return {}
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return {}
    if not isinstance(claims, dict):
        return {}
    result = {}
    for key in ("email", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


def _token_record(response: Mapping[str, Any], now: float, previous: Mapping[str, Any] | None = None) -> dict:
    previous = previous or {}
    access = response["access_token"]
    expires_in = _positive_seconds(response.get("expires_in"), 3600)
    jwt_exp = _jwt_exp(access)
    expires_at = jwt_exp if jwt_exp is not None else now + expires_in
    refresh = response.get("refresh_token") or previous.get("refresh_token")
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "scopes": sorted(str(response.get("scope") or "").split())
        or list(previous.get("scopes") or []),
    }


def _effective_expiry(tokens: Mapping[str, Any]) -> float:
    try:
        stored = float(tokens.get("expires_at", 0))
    except (TypeError, ValueError):
        stored = 0
    jwt_exp = _jwt_exp(tokens.get("access_token"))
    return min(stored, jwt_exp) if jwt_exp is not None and stored else (jwt_exp or stored)


def _due(tokens: Mapping[str, Any], now: float) -> bool:
    return now >= _effective_expiry(tokens) - REFRESH_MARGIN


# --- device code flow ---------------------------------------------------------------


def _is_trusted_verification_url(value: Any) -> bool:
    """Accept only HTTPS URLs on the exact xAI hosts used by the flow."""
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and parsed.hostname is not None
        and parsed.hostname.lower() in VERIFICATION_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and not parsed.fragment
    )


def _request_device_code() -> dict:
    response = _request_post(
        DEVICE_AUTHORIZATION_URL,
        {"client_id": CLIENT_ID, "scope": SCOPE},
    )
    body = _response_json(response)
    if not 200 <= getattr(response, "status_code", 0) < 300:
        status = getattr(response, "status_code", 0)
        if status in (408, 429) or status >= 500:
            raise GrokUnreachable(f"xAI could not start Grok sign-in now ({_oauth_error(response)}); try again.")
        raise GrokAuthError(
            f"xAI did not start Grok sign-in ({_oauth_error(response)}); "
            "run `tradingagents login grok` again."
        )
    required = ("device_code", "user_code", "verification_uri")
    if any(not isinstance(body.get(key), str) or not body[key] for key in required):
        raise GrokUnreachable("xAI's device-code answer could not be read; try again.")
    if not _is_trusted_verification_url(body["verification_uri"]):
        raise GrokAuthError("xAI returned an untrusted Grok verification URL; run `tradingagents login grok` again.")
    complete = body.get("verification_uri_complete")
    if complete is not None and not _is_trusted_verification_url(complete):
        raise GrokAuthError("xAI returned an untrusted Grok verification URL; run `tradingagents login grok` again.")
    return body


def _poll_device_code(
    device: Mapping[str, Any],
    *,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Poll xAI until approval, refusal, expiry, or a bounded deadline."""
    expires = _positive_seconds(device.get("expires_in"), DEFAULT_DEVICE_EXPIRES)
    if timeout is not None:
        try:
            timeout_seconds = float(timeout)
        except (TypeError, ValueError):
            timeout_seconds = expires
        if timeout_seconds >= 0 and timeout_seconds != float("inf"):
            expires = min(expires, timeout_seconds)
    deadline = now() + expires
    interval = _bounded_interval(device.get("interval"))

    while now() < deadline:
        response = _request_post(
            TOKEN_URL,
            {
                "grant_type": DEVICE_CODE_GRANT_TYPE,
                "client_id": CLIENT_ID,
                "device_code": device["device_code"],
            },
        )
        status = getattr(response, "status_code", 0)
        body = _response_json(response)
        # RFC 8628 errors normally use HTTP 400, but accepting the error body
        # here as well keeps terminal handling correct if a gateway returns a
        # successful HTTP status for an OAuth error envelope.
        if 200 <= status < 300 and not body.get("error"):
            return _token_body(response, "complete your Grok sign-in", require_refresh=True)

        error = _oauth_code(body.get("error"))
        remaining = max(0.0, deadline - now())
        if error == "authorization_pending":
            delay = min(interval + POLLING_SAFETY_MARGIN, remaining)
            if delay > 0:
                sleep(delay)
            continue
        if error == "slow_down":
            interval = min(MAX_POLL_INTERVAL, interval + SLOW_DOWN_INCREMENT)
            delay = min(interval + POLLING_SAFETY_MARGIN, remaining)
            if delay > 0:
                sleep(delay)
            continue
        if error in {"access_denied", "authorization_denied"}:
            raise GrokAuthError("Grok device authorization was denied; run `tradingagents login grok` again.")
        if error in {"expired_token", "expired"}:
            raise GrokAuthError("Grok device code expired; run `tradingagents login grok` again.")
        if status in (408, 429) or status >= 500:
            raise GrokUnreachable(f"xAI could not complete Grok sign-in now ({_oauth_error(response)}); try again.")
        detail = error or f"HTTP {status}"
        raise GrokAuthError(
            f"xAI did not complete Grok sign-in ({detail}); run `tradingagents login grok` again."
        )
    raise GrokAuthError("Grok device authorization timed out; run `tradingagents login grok` again.")


def request_device_code() -> dict:
    """Request an xAI device code (public for adapter and unit-test use)."""
    return _request_device_code()


def poll_device_code_token(
    device: Mapping[str, Any],
    *,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> dict:
    """Poll a device code using xAI's RFC 8628 error/backoff contract."""
    return _poll_device_code(device, timeout=timeout, sleep=sleep, now=now)


def _notify(notify: Callable[[str], None], message: str) -> None:
    try:
        notify(message)
    except TypeError:
        # A few callers expose a no-argument notification hook.  Do not make
        # auth fail after the device code was issued just because they cannot
        # display optional progress text.
        notify()


def sign_in(
    notify: Callable[[str], None] = print,
    ask_callback: Callable[..., Any] | None = None,
    open_browser: bool | Callable[[str], Any] | None = None,
    *,
    say: Callable[[str], None] | None = None,
    read_line: Callable[[], str] | None = None,
    timeout: float = DEFAULT_LOGIN_TIMEOUT,
) -> str:
    """Sign in with an xAI Grok subscription and return the account email.

    ``notify``/``ask_callback``/``open_browser`` are the subscription adapter
    contract.  ``say`` and ``read_line`` are accepted aliases for the older
    ChatGPT adapter contract; device-code OAuth does not need a pasted redirect
    address, so ``read_line`` is intentionally unused.
    """
    if say is not None:
        notify = say
    # Preserve the familiar keyword without treating it as a redirect: the
    # device flow is explicitly designed for remote/headless hosts.
    if read_line is not None and ask_callback is None:
        ask_callback = read_line

    device = request_device_code()
    complete_url = device.get("verification_uri_complete") or device["verification_uri"]
    _notify(notify, GROK_LOGIN_PROMPT)
    _notify(notify, str(complete_url))
    _notify(notify, GROK_DEVICE_PROMPT.format(
        verification_uri=device["verification_uri"], user_code=device["user_code"]))

    if open_browser is True:
        webbrowser.open(str(complete_url))
    elif callable(open_browser):
        open_browser(str(complete_url))

    # Some hosts pass an ``ask_callback`` only to preserve the common login
    # contract.  Do not call it: doing so could block headless sign-in while
    # xAI's device endpoint already gives the user all required instructions.
    del ask_callback

    # Keep xAI's server-provided interval and RFC 8628 slow_down increments;
    # callers cannot accidentally shorten the backoff and hammer the endpoint.
    tokens = poll_device_code_token(device, timeout=timeout)
    claims = _account_claims(tokens.get("id_token"))
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    with locked(AUTH_FILE):
        data = _well_formed()
        previous_account = data.get("account") if isinstance(data.get("account"), dict) else {}
        previous_subject = previous_account.get("subject")
        if previous_subject and claims.get("sub") and previous_subject != claims["sub"]:
            raise GrokAuthError(
                f"This machine is signed in as {previous_account.get('email') or 'another account'}; "
                "run `tradingagents logout grok` first."
            )
        _write(
            {
                "host_id": _host_id(data),
                "account": {
                    "email": claims.get("email"),
                    "subject": claims.get("sub"),
                },
                "tokens": _token_record(tokens, time.time()),
            }
        )
    return claims.get("email") or "your Grok account"


# --- access tokens ------------------------------------------------------------------


def _refresh(data: Mapping[str, Any], now: float) -> dict:
    tokens = data["tokens"]
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise GrokAuthError("The Grok sign-in has no refresh token; run `tradingagents login grok` again.")
    response = _request_post(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        },
    )
    body = _token_body(response, "renew your Grok sign-in")
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    claims = _account_claims(body.get("id_token"))
    if account.get("subject") and claims.get("sub") and account["subject"] != claims["sub"]:
        raise GrokAuthError("The renewed Grok sign-in belongs to another account.")
    return {
        **data,
        "tokens": _token_record(body, now, tokens),
    }


def access_token(now: Callable[[], float] = time.time) -> str:
    """Return a usable bearer, rotating the refresh token under a file lock."""
    global _retry_after
    _refuse_malformed()
    data = _read()
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise GrokAuthError(LOGIN_HINT)
    current = now()
    if not _due(tokens, current):
        return tokens["access_token"]

    with locked(AUTH_FILE):
        _refuse_malformed()
        data = _read()
        tokens = data.get("tokens")
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            raise GrokAuthError(LOGIN_HINT)
        current = now()
        if _due(tokens, current):
            still_works = _effective_expiry(tokens) > current
            if current < _retry_after:
                if still_works:
                    return tokens["access_token"]
                raise GrokUnreachable("xAI could not renew your Grok sign-in a moment ago; try again in a minute.")
            try:
                data = _refresh(data, current)
            except GrokUnreachable:
                _retry_after = current + RETRY_AFTER
                if not still_works:
                    raise
                return tokens["access_token"]
            _write(data)
        return data["tokens"]["access_token"]


def credential() -> Callable[[], str]:
    """Return the token callable expected by the OpenAI-compatible client."""
    _refuse_malformed()
    if not isinstance(_read().get("tokens"), dict):
        raise GrokAuthError(LOGIN_HINT)
    return access_token


def list_models() -> list[tuple[str, str]]:
    """Return xAI's live ``GET /v1/models`` entries as ``(name, id)`` pairs."""
    try:
        response = requests.get(
            MODELS_URL,
            timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {access_token()}"},
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise _request_error("list Grok models", exc) from None
    status = getattr(response, "status_code", 0)
    if status in (408, 429) or status >= 500:
        raise GrokUnreachable(f"xAI could not list Grok models now ({_oauth_error(response)}); try again.")
    if status == 401 or status == 403:
        raise GrokAuthError("xAI rejected the Grok sign-in; run `tradingagents login grok` again.")
    if not 200 <= status < 300:
        raise GrokAuthError(f"xAI did not list Grok models ({_oauth_error(response)}); try again.")
    body = _response_json(response)
    models = body.get("data")
    if not isinstance(models, list):
        raise GrokUnreachable("xAI's model list could not be read; try again.")
    return [
        (item["id"], item["id"])
        for item in models
        if isinstance(item, dict) and item.get("object", "model") == "model" and isinstance(item.get("id"), str)
    ]


def sign_out() -> str | None:
    """Forget the local sign-in; return ``None`` or the ChatGPT-compatible outcome.

    xAI's official OpenCode OAuth adapter has no revoke endpoint, so a present
    sign-in always returns ``"forgotten"``.  No credential is sent over the
    network during logout.
    """
    if auth_file_problem() is None and not isinstance(_read().get("tokens"), dict):
        return None
    with locked(AUTH_FILE):
        if auth_file_problem() is not None:
            # Forget the malformed file locally, keeping only a usable host id.
            _write(_well_formed() or {"host_id": _host_id({})})
            return "forgotten"
        data = _read()
        if not isinstance(data.get("tokens"), dict):
            return None
        _write({"host_id": _host_id(data)})
    return "forgotten"


__all__ = [
    "API_BASE",
    "AUTHORIZE_URL",
    "AUTH_FILE",
    "CLIENT_ID",
    "DEVICE_AUTHORIZATION_URL",
    "DEVICE_CODE_GRANT_TYPE",
    "GrokAuthError",
    "GrokUnreachable",
    "ISSUER",
    "LOGIN_HINT",
    "MODELS_URL",
    "SCOPE",
    "SIGNED_IN_FALLBACK",
    "TOKEN_URL",
    "VERIFICATION_HOSTS",
    "access_token",
    "auth_file_problem",
    "credential",
    "list_models",
    "poll_device_code_token",
    "request_device_code",
    "sign_in",
    "sign_out",
    "signed_in_as",
]
