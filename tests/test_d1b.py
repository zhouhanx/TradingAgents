"""D1b: W113 (codes never redacted), W114–W118. No model calls, no .env, no auth."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from tests.test_run003b import (
    SEL,
    home as home,  # noqa: F401  (fixture)
    sleepers as sleepers,  # noqa: F401  (fixture)
)
from tradingagents.portfolio import Position
from tradingagents.redaction import redact_secrets, redact_text
from webui import server, service
from webui.redaction import _secret_key_name, redact, scrub_config

pytestmark = pytest.mark.unit
CODES = ["XAI-USD", "SK-HYNIX", "sk-foo", "sk-foobarbaz", "rt-abc", "pk-xyz1", "ghp_abc", "Xai-Usd", "xai-usd",
         "SK-HYNIX-PREF", "BRK-B", "0700.HK", "GC=F"]
REAL = "sk-proj-" + "A1b2C3d4E5f6G7h8J9k0"


def _config(code):
    return {"ticker": code, "portfolio": {"cash": 10, "positions": [
        {"ticker": code, "quantity": 1, "average_price": 2, "note": f"holding {code}"}]},
        "tickers": [code, "NVDA"], "symbol": code}


# ------------------------------------------------------------------ W113
@pytest.mark.parametrize("code", CODES)
def test_codes_survive_save_and_get(home, code):
    payload = _config(code)
    saved = service.save_config(json.loads(json.dumps(payload)))
    disk = json.loads((service.webui_home() / "config.json").read_text(encoding="utf-8"))
    for blob in (saved, disk, service.public_saved_config()):
        assert blob["ticker"] == code and blob["symbol"] == code and blob["tickers"] == [code, "NVDA"]
        pos = blob["portfolio"]["positions"][0]
        assert pos["ticker"] == code
        if len(code) < 20:                       # short codes in a note are words, not keys
            assert pos["note"] == f"holding {code}"


@pytest.mark.parametrize("code", CODES)
def test_codes_survive_record_and_job_redaction(code):
    job = {"id": "j", "kind": "analysis", "selections": {"ticker": code, "portfolio": {"cash": 1, "positions": [
        {"ticker": code, "quantity": 1, "average_price": 2}]}}}
    out = redact(job)
    assert out["selections"]["ticker"] == code
    assert out["selections"]["portfolio"]["positions"][0]["ticker"] == code
    assert redact({"runs": [{"selections": {"ticker": code}}]})["runs"][0]["selections"]["ticker"] == code


@pytest.mark.parametrize("code", ["XAI-USD", "SK-HYNIX", "BTC-USD"])
def test_job_file_keeps_the_original_code(sleepers, home, code):
    """Through the real start path (a sleeping stand-in worker, no model): the
    job.json the worker reads and the run record keep the code as typed."""
    sel = {**SEL, "ticker": code, "portfolio": {"cash": 1, "positions": [
        {"ticker": code, "quantity": 1, "average_price": 2, "note": f"hold {code}"}]}}
    record = service.start_validated_run(sel)
    jobs = list(home.rglob("*.json"))
    found = [json.loads(p.read_text(encoding="utf-8")) for p in jobs]
    job = next(d for d in found if isinstance(d, dict) and d.get("selections", {}).get("ticker") == code)
    assert job["selections"]["portfolio"]["positions"][0]["ticker"] == code
    assert job["selections"]["portfolio"]["positions"][0]["note"] == f"hold {code}"
    assert "[redacted" not in json.dumps(job["selections"])
    assert record["selections"]["ticker"] == code


def test_a_real_key_is_still_redacted_everywhere(home):
    saved = service.save_config({"portfolio": {"cash": 1, "positions": [
        {"ticker": "AAPL", "quantity": 1, "average_price": 2, "note": f"key {REAL}"}]}, "memo": f"Bearer {REAL}"})
    text = (service.webui_home() / "config.json").read_text(encoding="utf-8") + json.dumps(saved)
    assert REAL not in text
    assert REAL not in json.dumps(redact({"selections": {"portfolio": {"note": REAL}}}))
    assert REAL not in redact_text(f"error {REAL}")


def test_a_key_in_a_code_field_of_another_shape_is_scanned():
    assert scrub_config({"ticker": {"x": REAL}})["ticker"]["x"] != REAL


# ------------------------------------------------------------------ W118
@pytest.mark.parametrize("text", ["uses sk-learn", "SK-Hynix stake", "Bearer market", "XAI-holdings",
                                  "rt-pcr", "pk-1 ok", "ghp_short"])
def test_ordinary_notes_stay_verbatim(text):
    assert redact_secrets(text) == text
    assert scrub_config({"portfolio": {"note": text}}) == {"portfolio": {"note": text}}


def test_cookie_keys_and_nested_secret_names_unchanged(home):
    saved = service.save_config({"cookie_consent": "yes", "portfolio": {"cash": 1, "password": "x"}})
    assert "cookie_consent" not in saved and saved["portfolio"]["password"] == "[redacted]"


# ------------------------------------------------------------------ W114
@pytest.mark.parametrize("ticker", ["\u3164", "\ufe0f", "\u034f", "\u115f", "\u17b4", "\u3164\u200b", "\uffa0"])
def test_backend_refuses_the_same_invisible_tickers_as_the_page(ticker):
    with pytest.raises(ValidationError):
        Position(ticker=ticker, quantity=1, average_price=1)


# ------------------------------------------------------------------ W115
@pytest.mark.parametrize("mark", ["\U000e0001", "\U0001d173", "\U000e0041", "\U0001d17a"])
def test_tag_and_musical_format_characters_join_keys(mark):
    text = f"sk-abc{mark}NotReal0123456789"
    assert "NotReal0123456789" not in redact_text(text)
    assert "NotReal0123456789" not in redact_secrets(text)


# ------------------------------------------------------------------ W116
@pytest.mark.parametrize("name", ["ΑΡΙ_ΚΕΥ", "αρι_κευ", "ΤΟΚΕΝ"])
def test_greek_key_names(name):
    assert _secret_key_name(name)


# ------------------------------------------------------------------ W117
@pytest.mark.parametrize("name", ["apikey", "api-key", "password", "secret", "refresh_token", "%6B", "%256B",
                                  "API_KEY", "Password", "%61pikey", "k", "key", "token"])
def test_request_log_hides_secret_params(name):
    logged = server._log_path(f"/x?id=1&{name}=TOPSECRET9&raw=1")
    assert "TOPSECRET9" not in logged and "id=1" in logged and "raw=1" in logged


def test_request_log_keeps_ordinary_params():
    assert server._log_path("/api/runs?id=7&ticker=XAI-USD") == "/api/runs?id=7&ticker=XAI-USD"


def test_request_log_stays_off_by_default(monkeypatch, capsys):
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_REQUEST_LOG", raising=False)

    class Fake:
        command = "GET"
        path = "/x?k=1"

    server.Handler.log_message(Fake(), "%s", "x", "200")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("code", ["XAI-USD", "SK-HYNIX", "Xai-Usd", "SK-HYNIX-PREF"])
def test_error_text_keeps_exchange_style_tickers(code):
    assert redact_text(f"no price data for {code} on 2026-10-30") == f"no price data for {code} on 2026-10-30"


# ------------------------------------------------------------------ W120
SECRETS = [REAL, "Bearer " + "A1b2C3d4E5f6G7h8J9k0", "XAI-" + "a1B2c3D4e5F6g7H8", "ghp_" + "A1b2C3d4E5f6G7h8J9",
           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"]
CODE_NAMES = ["ticker", "symbol", "code", "codes", "tickers", "instrument", "ＴＩＣＫＥＲ", "tick\u200ber", "Symbol"]


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("secret", SECRETS)
def test_a_key_under_a_code_field_is_redacted(home, name, secret):
    payload = {name: secret, "portfolio": {"cash": 1, "positions": [{"ticker": "AAPL", "quantity": 1,
               "average_price": 2, "extra": {name: [secret, "NVDA"]}}]}}
    saved = service.save_config(payload)
    disk = (service.webui_home() / "config.json").read_text(encoding="utf-8")
    secret_part = secret.split()[-1]
    for text in (disk, json.dumps(saved, ensure_ascii=False), json.dumps(service.public_saved_config()),
                 json.dumps(redact({"selections": {name: secret, "x": {name: [secret]}}}))):
        assert secret_part not in text
    assert "NVDA" in disk


@pytest.mark.parametrize("code", CODES)
def test_codes_stay_under_every_code_field_name(home, code):
    saved = service.save_config(dict.fromkeys(CODE_NAMES, code))
    assert all(saved[name] == code for name in CODE_NAMES)
