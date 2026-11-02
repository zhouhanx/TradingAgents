"""WEBUI-004 AC5: a corrupt local auth file is a read failure, not "未登录".

Status stays local-only: nothing here reaches a provider, and the page/API must not
claim an online check happened.
"""

import json
import os

import pytest

from tradingagents.llm_clients import chatgpt_auth, grok_auth
from webui import service

MODULES = {"chatgpt": chatgpt_auth, "grok": grok_auth}
FILES = {"chatgpt": "chatgpt-auth.json", "grok": "grok-auth.json"}
# What each provider's sign_in actually writes (chatgpt_auth.sign_in / grok_auth.sign_in).
_TOKENS = {"access_token": "fixture-access", "refresh_token": "fixture-refresh",
           "expires_at": 4102444800.0, "scopes": ["openid"]}
VALID_FILES = {
    "chatgpt": {"host_id": "urn:uuid:fixture",
                "account": {"client_id": "app_fixture", "email": "fixture@example.com",
                            "subject": "fixture-sub"},
                "tokens": {**_TOKENS, "earliest_refresh_at": None}},
    "grok": {"host_id": "urn:uuid:fixture",
             "account": {"email": "fixture@example.com", "subject": "fixture-sub"},
             "tokens": dict(_TOKENS)},
}
VALID = VALID_FILES["grok"]


@pytest.fixture
def auth_home(monkeypatch, tmp_path):
    for key, module in MODULES.items():
        monkeypatch.setattr(module, "AUTH_FILE", tmp_path / FILES[key])
    return tmp_path


def _row(provider):
    status = service.auth_status(provider)
    assert status["live_validation"] is False
    (row,) = status["accounts"]
    return row


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
def test_missing_file_is_signed_out(auth_home, provider):
    row = _row(provider)
    assert row["state"] == "signed_out"
    assert row["signed_in"] is False and not row.get("read_error")
    assert "not signed in" in row["cli"]
    assert MODULES[provider].auth_file_problem() is None


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
def test_signed_out_file_without_tokens_is_signed_out(auth_home, provider):
    (auth_home / FILES[provider]).write_text(json.dumps({"host_id": "urn:uuid:x"}), encoding="utf-8")
    row = _row(provider)
    assert row["state"] == "signed_out" and not row.get("read_error")


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
@pytest.mark.parametrize("content", ['{"tokens": {"access_token": "tru', "", "[]", "\udcff"])
def test_corrupt_file_is_a_read_failure(auth_home, provider, content):
    path = auth_home / FILES[provider]
    if content == "\udcff":
        path.write_bytes(b"\xff\xfe\x00garbage")
    else:
        path.write_text(content, encoding="utf-8")
    row = _row(provider)
    assert row["state"] == "read_error"
    assert row["read_error"] is True and row["signed_in"] is False and row["account"] is None
    assert "读取失败" in row["detail"]
    assert "没有联网校验" in row["detail"]
    assert "could not read local sign-in status" in row["cli"]
    assert "no live validation" in row["cli"]
    assert "not signed in" not in row["cli"]
    assert "fixture" not in json.dumps(row)  # no file contents echoed
    # A run that needs this subscription says why instead of "尚未登录".
    with pytest.raises(service.ConsoleError, match="读取失败"):
        service._require_credential(provider)
    # The file is left alone for the user to inspect; a new sign-in replaces it.
    assert path.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
def test_unreadable_file_is_a_read_failure(auth_home, provider):
    path = auth_home / FILES[provider]
    path.write_text(json.dumps(VALID), encoding="utf-8")
    path.chmod(0)
    try:
        row = _row(provider)
    finally:
        path.chmod(0o600)
    assert row["state"] == "read_error" and "PermissionError" in row["detail"]


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
def test_valid_file_is_signed_in(auth_home, provider):
    (auth_home / FILES[provider]).write_text(json.dumps(VALID_FILES[provider]), encoding="utf-8")
    row = _row(provider)
    assert row["state"] == "signed_in"
    assert row["signed_in"] is True and not row.get("read_error")
    assert row["account"] == "fixture@example.com"
    assert "未做在线校验" in row["detail"]
    assert "fixture-access" not in json.dumps(row)


def test_fake_auth_without_a_probe_still_works(monkeypatch):
    class Auth:
        def signed_in_as(self):
            return None

    class Spec:
        display_name = "Grok (subscription)"
        auth = Auth()

    monkeypatch.setattr(service, "SUBSCRIPTIONS", {"grok": Spec()})
    row = _row("grok")
    assert row["state"] == "signed_out"


# --- F-M3-1: JSON that parses but does not have the shape sign_in writes ------------

def _with(provider, **changes):
    data = json.loads(json.dumps(VALID_FILES[provider]))
    for dotted, value in changes.items():
        target, *path = dotted.split("__")
        node = data
        keys = [target, *path]
        for key in keys[:-1]:
            node = node[key]
        if value is _DROP:
            node.pop(keys[-1], None)
        else:
            node[keys[-1]] = value
    return data


_DROP = object()

# QA fix-r1 cases, verbatim, for both providers.
QA_CASES = {
    "tokens_oops": lambda p: {"tokens": "oops"},
    "tokens_string_with_email": lambda p: _with(p, tokens="oops"),
    "tokens_list_with_email": lambda p: _with(p, tokens=["a1", "r1"]),
    "refresh_without_access": lambda p: _with(p, tokens__access_token=_DROP),
    "access_token_number": lambda p: _with(p, tokens__access_token=5),
    "account_string": lambda p: _with(p, account="fixture@example.com"),
}

# A wider table of malformed shapes, applied to both providers.
MALFORMED = {
    **QA_CASES,
    "tokens_null": lambda p: _with(p, tokens=None),
    "tokens_empty_object": lambda p: _with(p, tokens={}),
    "tokens_number": lambda p: _with(p, tokens=1),
    "tokens_true": lambda p: _with(p, tokens=True),
    "access_token_empty": lambda p: _with(p, tokens__access_token=""),
    "access_token_null": lambda p: _with(p, tokens__access_token=None),
    "access_token_list": lambda p: _with(p, tokens__access_token=["a"]),
    "access_token_object": lambda p: _with(p, tokens__access_token={"v": "a"}),
    "access_token_bool": lambda p: _with(p, tokens__access_token=True),
    "refresh_token_number": lambda p: _with(p, tokens__refresh_token=7),
    "refresh_token_empty": lambda p: _with(p, tokens__refresh_token=""),
    "expires_at_string": lambda p: _with(p, tokens__expires_at="tomorrow"),
    "expires_at_bool": lambda p: _with(p, tokens__expires_at=True),
    "earliest_refresh_at_string": lambda p: _with(p, tokens__earliest_refresh_at="soon"),
    # json.dumps writes these as NaN / Infinity / -Infinity, and json.loads reads them back.
    **{f"{field}_{name}": (lambda f, v: lambda p: _with(p, **{f"tokens__{f}": v}))(field, value)
       for field in ("expires_at", "earliest_refresh_at")
       for name, value in (("nan", float("nan")), ("inf", float("inf")), ("minus_inf", float("-inf")))},
    "scopes_string": lambda p: _with(p, tokens__scopes="openid email"),
    "scopes_numbers": lambda p: _with(p, tokens__scopes=[1, 2]),
    "account_list": lambda p: _with(p, account=["fixture@example.com"]),
    "account_number": lambda p: _with(p, account=3),
    "email_number": lambda p: _with(p, account__email=5),
    "email_object": lambda p: _with(p, account__email={"a": "b"}),
    "subject_number": lambda p: _with(p, account__subject=9),
    "host_id_number": lambda p: _with(p, host_id=12),
    "host_id_empty": lambda p: _with(p, host_id=""),
    "signed_out_account_string": lambda p: {"host_id": "urn:uuid:x", "account": "x"},
}

# ChatGPT's refresh needs account.client_id, which its sign_in always writes.
CHATGPT_ONLY = {
    "chatgpt_account_missing": lambda p: _with(p, account=_DROP),
    "chatgpt_account_null": lambda p: _with(p, account=None),
    "chatgpt_client_id_missing": lambda p: _with(p, account__client_id=_DROP),
    "chatgpt_client_id_number": lambda p: _with(p, account__client_id=42),
}


def _assert_read_error(auth_home, provider, data):
    path = auth_home / FILES[provider]
    path.write_text(json.dumps(data), encoding="utf-8")
    module = MODULES[provider]
    assert module.auth_file_problem(), data
    # The module's own signed-in check uses the same validator.
    assert module.signed_in_as() is None
    row = _row(provider)
    assert row["state"] == "read_error", (data, row)
    assert row["read_error"] is True and row["signed_in"] is False and row["account"] is None
    assert "读取失败" in row["detail"] and "没有联网校验" in row["detail"]
    assert "could not read local sign-in status" in row["cli"]
    assert "not signed in" not in row["cli"]
    for secret in ("fixture-access", "fixture-refresh", "fixture@example.com"):
        assert secret not in json.dumps(row)
    with pytest.raises(service.ConsoleError, match="登录文件读取失败"):
        service._require_credential(provider)
    # Token use refuses with a read error, never a Python AttributeError/TypeError.
    error = (chatgpt_auth.ChatGPTAuthError if provider == "chatgpt" else grok_auth.GrokAuthError)
    with pytest.raises(error, match="could not be read"):
        module.credential()
    with pytest.raises(error, match="could not be read"):
        module.access_token(now=lambda: 0.0)
    assert path.exists()


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
@pytest.mark.parametrize("case", sorted(QA_CASES))
def test_qa_reported_shapes_are_read_failures(auth_home, provider, case):
    _assert_read_error(auth_home, provider, QA_CASES[case](provider))


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
@pytest.mark.parametrize("case", sorted(set(MALFORMED) - set(QA_CASES)))
def test_malformed_shapes_are_read_failures(auth_home, provider, case):
    _assert_read_error(auth_home, provider, MALFORMED[case](provider))


@pytest.mark.parametrize("case", sorted(CHATGPT_ONLY))
def test_chatgpt_file_without_a_refreshable_account_is_a_read_failure(auth_home, case):
    _assert_read_error(auth_home, "chatgpt", CHATGPT_ONLY[case]("chatgpt"))


def test_grok_account_is_optional_like_its_sign_in_allows(auth_home):
    for data in (_with("grok", account=_DROP), _with("grok", account=None),
                 _with("grok", account__email=None)):
        (auth_home / FILES["grok"]).write_text(json.dumps(data), encoding="utf-8")
        assert grok_auth.auth_file_problem() is None
        assert _row("grok")["state"] == "signed_in"


def test_non_finite_timestamps_are_written_as_json_literals_the_parser_accepts(auth_home):
    """The NaN cases above really reach the validator as floats, not as a JSON error."""
    data = MALFORMED["expires_at_nan"]("chatgpt")
    text = json.dumps(data)
    assert "NaN" in text
    (auth_home / FILES["chatgpt"]).write_text(text, encoding="utf-8")
    assert "expires_at is not a finite number" in chatgpt_auth.auth_file_problem()


@pytest.mark.parametrize("email", [None, "", _DROP])
def test_chatgpt_sign_in_without_an_email_is_signed_in_with_a_neutral_label(auth_home, monkeypatch, email):
    """sign_in writes ``"email": null`` when the ID token has no email claim; that is
    a valid sign-in (W10), labelled like sign_in labels it, as Grok already does."""
    monkeypatch.setattr(chatgpt_auth.requests, "post", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(chatgpt_auth.requests, "get", lambda *a, **k: pytest.fail("network"))
    data = _with("chatgpt", account__email=email)
    (auth_home / FILES["chatgpt"]).write_text(json.dumps(data), encoding="utf-8")
    assert chatgpt_auth.auth_file_problem() is None
    assert chatgpt_auth.signed_in_as() == chatgpt_auth.SIGNED_IN_FALLBACK == "your ChatGPT account"
    row = _row("chatgpt")
    assert row["state"] == "signed_in" and row["signed_in"] is True and not row.get("read_error")
    assert row["account"] == chatgpt_auth.SIGNED_IN_FALLBACK
    assert service._require_credential("chatgpt")
    assert chatgpt_auth.access_token(now=lambda: 0.0) == "fixture-access"


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
def test_a_real_email_is_still_the_account_label(auth_home, provider):
    (auth_home / FILES[provider]).write_text(json.dumps(VALID_FILES[provider]), encoding="utf-8")
    assert MODULES[provider].signed_in_as() == "fixture@example.com"
    assert _row(provider)["account"] == "fixture@example.com"


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
def test_optional_token_fields_may_be_null_or_absent(auth_home, provider):
    data = _with(provider, tokens__refresh_token=None, tokens__expires_at=_DROP, tokens__scopes=_DROP,
                 host_id=_DROP)
    (auth_home / FILES[provider]).write_text(json.dumps(data), encoding="utf-8")
    assert MODULES[provider].auth_file_problem() is None
    assert _row(provider)["state"] == "signed_in"
    assert service._require_credential(provider)


@pytest.mark.parametrize("provider", ["chatgpt", "grok"])
def test_logout_forgets_a_malformed_file_without_crashing(auth_home, provider, monkeypatch):
    module = MODULES[provider]
    # Nothing may reach a provider while forgetting a malformed file.
    if provider == "chatgpt":
        monkeypatch.setattr(module.requests, "post", lambda *a, **k: pytest.fail("network"))
    path = auth_home / FILES[provider]
    path.write_text(json.dumps(_with(provider, tokens="oops")), encoding="utf-8")
    assert module.sign_out() == "forgotten"
    assert json.loads(path.read_text(encoding="utf-8")) == {"host_id": "urn:uuid:fixture"}
    assert _row(provider)["state"] == "signed_out"
