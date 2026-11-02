"""D1 (v2 backlog): W82–W88, W90, W91, W112. No model calls, no .env, no auth."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.test_run003b import home as home  # noqa: F401  (fixture)
from tradingagents.portfolio import Position
from tradingagents.redaction import redact_secrets, redact_text
from webui import server, service
from webui.redaction import _secret_key_name, fold_key, scrub_config

pytestmark = pytest.mark.unit
SCRIPT = Path("webui/static/app.js").read_text(encoding="utf-8")
KEY = "sk-live" + "A1b2C3d4E5f6G7h8"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"


def _plain(blob) -> str:
    return json.dumps(blob, ensure_ascii=False)


# ------------------------------------------------------------------ W84
NESTED = {
    "portfolio": {
        "cash": 1000,
        "note": f"broker key {KEY}",
        "positions": [{"ticker": "AAPL", "quantity": 1, "average_price": 10,
                       "note": f"Bearer {JWT}", "api_token": "plain-value"}],
    },
    "extra": {"deep": [{"x": [f"xai-{'q' * 20}"]}], "password": "hunter2"},
    "holdings_note": KEY,
}


def test_save_config_redacts_at_every_depth(home):
    saved = service.save_config(NESTED)
    on_disk = (service.webui_home() / "config.json").read_text(encoding="utf-8")
    for text in (on_disk, _plain(saved)):
        assert KEY not in text and JWT not in text and "q" * 20 not in text
        assert "hunter2" not in text and "plain-value" not in text
    assert saved["portfolio"]["cash"] == 1000
    assert saved["portfolio"]["positions"][0]["ticker"] == "AAPL"


def test_get_config_redacts_an_old_plaintext_config(home):
    path = service.webui_home() / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(NESTED), encoding="utf-8")
    shown = _plain(service.public_saved_config())
    assert KEY not in shown and JWT not in shown and "hunter2" not in shown
    # the next save rewrites the old file without the plaintext
    service.save_config({"theme": "dark"})
    assert KEY not in path.read_text(encoding="utf-8")


def test_get_config_route_uses_the_redacted_view():
    src = Path("webui/server.py").read_text(encoding="utf-8")
    assert '"saved": service.public_saved_config()' in src


def test_scrub_config_keeps_ordinary_values():
    data = {"a": [1, "AAPL", {"b": None, "max_tokens": 5}], "c": True}
    out = scrub_config(data)
    assert out["a"][0] == 1 and out["a"][1] == "AAPL" and out["c"] is True
    assert out["a"][2]["max_tokens"] == "[redacted]"   # nested: redacted, not dropped


# ------------------------------------------------------------------ W85
@pytest.mark.parametrize("mark", ["\u2066", "\u2067", "\u180b", "\u3164", "\u2069", "\ufe0f"])
def test_error_text_joins_keys_split_by_invisible_characters(mark):
    text = f"failed sk-abc{mark}def123456789"
    assert "def123456789" not in redact_text(text)
    assert "def123456789" not in redact_secrets(text)


# ------------------------------------------------------------------ W86
@pytest.mark.parametrize("key", ["ｓｋ-abcdef1234567890", "sk－abcdef1234567890", "ＳＫ－ａｂｃｄｅｆ１２３４５６７８９０",
                                 "SK-ABCDEF1234567890", "XAI-abcdef1234567890", "Xai-abcdef1234567890"])
def test_full_width_and_upper_case_keys(key):
    assert redact_text(f"bad key {key} here") == "bad key [redacted-key] here"
    assert redact_secrets(f"note {key}") == "note [redacted-key]"


def test_other_prefixes_stay_lower_case_only():
    assert redact_secrets("RT-PCR test") == "RT-PCR test"


# ------------------------------------------------------------------ W87
@pytest.mark.parametrize("text", [
    "open %252Fhome%252Fbob%252Fsecret.txt",
    "open %25252Fhome%25252Fbob%25252Fsecret.txt",
    "open /home%2Fbob%2Fsecret.txt",
    "open /home/bob%2Fsecret.txt",
    "open C:%255CUsers%255Cbob%255Csecret.txt",
])
def test_double_and_partly_encoded_paths(text):
    out = redact_text(text)
    assert "bob" not in out and "secret.txt" in out and "[path]" in out


# ------------------------------------------------------------------ W88
@pytest.mark.parametrize("name", ["ΤΟΚΕΝ", "τοκεν", "ᴛoken", "ᴛᴏᴋᴇɴ", "ＴＯＫＥΝ", "apikey", "api-key",
                                  "ApiKey", "access_key", "AccessKey", "private_key", "cookie",
                                  "Set-Cookie", "my\u2066token"])
def test_sensitive_key_names(name):
    assert _secret_key_name(name)


@pytest.mark.parametrize("name", ["ticker", "note", "quantity", "theme", "results_dir"])
def test_ordinary_key_names(name):
    assert not _secret_key_name(name)


def test_toкen_still_folds():
    assert fold_key("Тоken") == "token"


def test_greek_token_key_is_not_saved(home):
    saved = service.save_config({"ΤΟΚΕΝ": "abc", "theme": "x"})
    assert "ΤΟΚΕΝ" not in saved
    assert "abc" not in (service.webui_home() / "config.json").read_text(encoding="utf-8")


# ------------------------------------------------------------------ W90
def test_request_log_hides_the_link_key():
    path = "/api/reports/file?path=%2Fx%2Freport.html&k=SECRETLINKKEY123&raw=1"
    logged = server._log_path(path)
    assert "SECRETLINKKEY123" not in logged and "k=[redacted]" in logged and "raw=1" in logged
    assert "abc" not in server._log_path("/r?K=abc")
    assert server._log_path("/api/runs?id=7") == "/api/runs?id=7"


def test_log_message_prints_the_redacted_path(monkeypatch, capsys):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_REQUEST_LOG", "1")

    class Fake:
        command = "GET"
        path = "/report?k=TOPSECRETKEY"

    server.Handler.log_message(Fake(), "%s", "x", "200")
    out = capsys.readouterr().out
    assert "TOPSECRETKEY" not in out and "k=[redacted]" in out


# ------------------------------------------------------------------ W91
@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions, non-root")
def test_unreadable_reports_dir_is_409(home, monkeypatch, tmp_path):
    results = tmp_path / "results"
    reports = results / "reports"
    reports.mkdir(parents=True)
    monkeypatch.setattr(service, "effective_results_dir", lambda: str(results))
    reports.chmod(0)
    try:
        with pytest.raises(service.ConsoleError) as caught:
            service.list_reports()
        assert caught.value.status == 409 and "结果目录无法读取" in str(caught.value)
    finally:
        reports.chmod(0o700)
    assert service.list_reports() == []


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions, non-root")
def test_unreadable_results_dir_live_sections_is_409(home, monkeypatch, tmp_path):
    results = tmp_path / "results"
    results.mkdir()
    monkeypatch.setattr(service, "effective_results_dir", lambda: str(results))
    results.chmod(0o100)
    try:
        with pytest.raises(service.ConsoleError) as caught:
            service.list_live_sections()
        assert caught.value.status == 409
    finally:
        results.chmod(0o700)


# ------------------------------------------------------------------ W112
@pytest.mark.parametrize("given", ["127.0.0.1\u200b", "\ufeff127.0.0.1", "127.0.\u200b0.1", "[::1\u200b]",
                                   "\u200b[::1]\ufeff", "localhost\u2060"])
def test_bind_host_drops_format_characters(given):
    name = server.normalize_bind_host(given)
    assert name in ("127.0.0.1", "::1", "localhost")
    assert server.is_loopback_host(name)


def test_format_characters_do_not_make_a_lan_host_loopback():
    assert server.normalize_bind_host("0.0.0.0\u200b") == "0.0.0.0"
    assert not server.is_loopback_host(server.normalize_bind_host("0.0.0.0\u200b"))
    assert not server.is_loopback_host(server.normalize_bind_host("192.168.1.5\ufeff"))
    with pytest.raises(ValueError):
        server.normalize_bind_host("\u200b\ufeff")


# ------------------------------------------------------------------ W83
@pytest.mark.parametrize("ticker", ["\u200b", "\u200b\u200b", "\ufeff", " \u200b ", "\u2060"])
def test_invisible_holding_ticker_is_refused(ticker):
    with pytest.raises(ValidationError):
        Position(ticker=ticker, quantity=1, average_price=1)


def test_invisible_holding_ticker_is_refused_by_config(home):
    with pytest.raises(service.ConsoleError):
        service.save_config({"portfolio": {"cash": 1, "positions": [
            {"ticker": "\u200b", "quantity": 1, "average_price": 1}]}})


@pytest.mark.parametrize("ticker", ["\u200b", "\ufeff\u200b"])
def test_invisible_run_ticker_is_refused_by_the_api(ticker):
    from cli.prompts import parse_ticker
    with pytest.raises(ValueError):
        parse_ticker(ticker)


def test_ui_uses_visible_text_for_the_ticker():
    assert "function visibleText(" in SCRIPT
    assert 'visibleText(document.querySelector("#ticker").value)' in SCRIPT
    assert "!visibleText(item.ticker)" in SCRIPT


# ------------------------------------------------------------------ W82
def test_stop_refusal_follows_the_run_status():
    body = re.search(r"function stopRefusedHtml\(run\) \{.*?\n\}", SCRIPT, re.S).group(0)
    assert "STOP_REFUSAL_LIVE.has(run.status)" in body and "state.stopRefused = null" in body
