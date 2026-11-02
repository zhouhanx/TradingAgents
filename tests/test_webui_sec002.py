"""SEC-002 (B4): link key on report links, LAN bind refusal, results_dir limits,
unreadable reports, redaction gaps, value scanning and confusable key names.

Loopback servers on ephemeral ports only; no model is called.
"""

import json
import os
from pathlib import Path

import pytest

from tests import test_webui_report_download, test_webui_security
from tradingagents.redaction import redact_secrets, redact_text
from webui import redaction, server, service

reports = test_webui_report_download.reports    # shared fixtures
console = test_webui_security.console

KEY = "sk-abcdefghijklmnopqrstuvwx"


# ---------------------------------------------------------------- W1 link key
def _file(console, query, **kwargs):
    return console["send"]("GET", f"/api/reports/file?path=missing.md{query}", token=False, **kwargs)


def test_w1_report_file_without_sec_fetch_headers_needs_the_link_key(console):
    # http.client sends no Sec-Fetch-* headers: the old Sec-Fetch-only guard let this in.
    status, data, _ = _file(console, "")
    assert status == 403
    assert server.LINK_KEY_REFUSED in data.decode("utf-8")
    assert _file(console, "&k=wrong")[0] == 403
    assert _file(console, "&k=")[0] == 403
    assert _file(console, f"&k={console['httpd'].session_token}")[0] == 403, "the API token is not a link key"
    assert _file(console, f"&k={console['httpd'].link_key}")[0] == 404
    # the token header (fetch from the page itself) is also enough
    assert console["send"]("GET", "/api/reports/file?path=missing.md")[0] == 404


def test_w1_a_valid_key_is_still_refused_cross_site(console):
    status, _, _ = _file(console, f"&k={console['httpd'].link_key}", headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403


def test_w1_the_page_carries_a_link_key_different_from_the_token(console):
    status, data, _ = console["send"]("GET", "/", token=False)
    page = data.decode("utf-8")
    assert status == 200
    httpd = console["httpd"]
    assert f'<meta name="{server.LINK_KEY_META}" content="{httpd.link_key}">' in page
    assert httpd.link_key and httpd.link_key != httpd.session_token
    assert server.serve("127.0.0.1", 0).link_key != httpd.link_key


def test_w1_a_served_report_sends_no_referrer(reports):
    status, _, headers = reports["get"](reports["run"] / "complete_report.html")
    assert status == 200
    assert headers["referrer-policy"] == "no-referrer"


def test_w1_frontend_puts_the_link_key_on_report_links():
    js = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "tradingagents-link-key" in js
    assert "/api/reports/file?path=" in js
    assert js.count("/api/reports/file?path=") == 1, "every report href is built by reportFileHref"


# ---------------------------------------------------------------- W2 LAN bind
@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.1", "localhost", "LOCALHOST", "::1", "[::1]"])
def test_w2_loopback_hosts_start_quietly(host):
    assert server.lan_bind_check(host, False) == (True, "")
    assert server.lan_bind_check(host, True) == (True, "")


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "example.com", ""])
def test_w2_other_hosts_are_refused_without_allow_lan_and_warned_with_it(host):
    ok, message = server.lan_bind_check(host, False)
    assert not ok and "拒绝启动" in message and "--allow-lan" in message
    ok, message = server.lan_bind_check(host, True)
    assert ok and message.startswith("警告")


def test_w2_main_refuses_a_lan_host_before_binding(monkeypatch, capsys):
    monkeypatch.setattr(service, "acquire_console_lock", lambda: pytest.fail("must refuse before the lock"))
    monkeypatch.setattr(server, "serve", lambda *a, **k: pytest.fail("must refuse before binding"))
    assert server.main(["--host", "0.0.0.0"]) == 2
    err = capsys.readouterr().err
    assert "拒绝启动" in err and "0.0.0.0" in err


def test_w2_main_with_allow_lan_warns_and_goes_on(monkeypatch, capsys):
    class Stop(Exception):
        pass

    def lock():
        raise Stop

    monkeypatch.setattr(service, "acquire_console_lock", lock)
    with pytest.raises(Stop):
        server.main(["--host", "0.0.0.0", "--allow-lan"])
    assert "警告" in capsys.readouterr().err


def test_w2_help_lists_allow_lan():
    assert "--allow-lan" in server.console_help_text()


# ------------------------------------------------- W13 / W15 / W16 results_dir
@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    (tmp_path / ".tradingagents").mkdir()
    return tmp_path / ".tradingagents"


def test_w15_segment_of_255_bytes_is_accepted_and_256_rejected(home):
    assert service.resolve_results_dir(str(home / ("a" * 255))) == str(home / ("a" * 255))
    with pytest.raises(service.ConsoleError) as info:
        service.resolve_results_dir(str(home / ("a" * 256)))
    assert info.value.status == 400
    # bytes, not characters: 85 x 3-byte = 255 ok, 86 = 258 refused
    service.resolve_results_dir(str(home / ("报" * 85)))
    with pytest.raises(service.ConsoleError):
        service.resolve_results_dir(str(home / ("报" * 86)))


@pytest.mark.parametrize("raw", ["x" * 300, "/".join(["y" * 200] * 25), "a\x00b"])
def test_w13_w16_bad_results_dir_is_400_with_a_clear_message(home, raw):
    with pytest.raises(service.ConsoleError) as info:
        service.save_config({"results_dir": str(home / raw)})
    assert info.value.status == 400
    assert str(info.value) == service.RESULTS_DIR_INVALID
    assert "results_dir" not in service.load_saved_config()


def test_w13_post_config_with_a_long_segment_is_400_and_saves_nothing(console):
    status, data, _ = console["send"]("POST", "/api/config", {"results_dir": "a" * 256})
    assert status == 400
    text = data.decode("utf-8")
    assert "255 字节" in text and "Errno" not in text and "too long" not in text


@pytest.mark.parametrize("name", ["list_reports", "list_live_sections", "list_backtest_sweeps"])
def test_w13_an_os_error_while_listing_is_a_409_not_a_dead_request(monkeypatch, name):
    def boom(*args, **kwargs):
        raise OSError(36, "File name too long", "/home/eric/" + "x" * 300)

    monkeypatch.setattr(service, "_" + name, boom)
    with pytest.raises(service.ConsoleError) as info:
        getattr(service, name)()
    assert info.value.status == 409
    assert str(info.value) == service.RESULTS_DIR_UNREADABLE


def test_w13_api_reports_and_backtests_answer_json_on_os_error(console, monkeypatch):
    monkeypatch.setattr(service, "_list_reports", lambda limit: (_ for _ in ()).throw(OSError(36, "File name too long")))
    monkeypatch.setattr(service, "_list_backtest_sweeps", lambda limit: (_ for _ in ()).throw(OSError(36, "File name too long")))
    for path in ("/api/reports", "/api/backtests"):
        status, data, _ = console["send"]("GET", path)
        assert status == 409, (path, data)
        body = json.loads(data)
        assert body["error"] == service.RESULTS_DIR_UNREADABLE
        assert "Errno" not in data.decode("utf-8")


def test_w13_a_saved_bad_results_dir_falls_back_instead_of_failing(home):
    path = service._config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"results_dir": str(home / ("z" * 300))}), encoding="utf-8")
    assert service.effective_results_dir() == service.console_default_results_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_w16_an_unreadable_report_is_404_without_errno(reports):
    target = reports["run"] / "complete_report.md"
    target.chmod(0)
    try:
        status, body, _ = reports["get"](target)
    finally:
        target.chmod(0o644)
    assert status == 404
    text = body.decode("utf-8")
    assert server.REPORT_UNREADABLE in text
    for leak in ("Errno", "Permission", "denied", str(reports["root"])):
        assert leak not in text


def test_w16_an_os_error_while_reading_is_the_same_404(reports, monkeypatch):
    monkeypatch.setattr(server.Handler, "_file", lambda self, path, download=False: (_ for _ in ()).throw(PermissionError(13, "Permission denied", str(path))))
    status, body, _ = reports["get"](reports["run"] / "complete_report.md")
    assert status == 404
    assert json.loads(body) == {"error": server.REPORT_UNREADABLE}


def test_w16_nul_in_results_dir_over_http_is_400(console):
    status, data, _ = console["send"]("POST", "/api/config", {"results_dir": "a\x00b"})
    assert status == 400
    assert "Errno" not in data.decode("utf-8") and "null" not in data.decode("utf-8")


# ---------------------------------------------------------------- W29 redaction
@pytest.mark.parametrize("raw,expected", [
    ("key：sk-abcdefghijklmnopqrstuv", "key：[redacted]"),
    ("api_key＝abcdef123456", "api_key＝[redacted]"),
    ("my_" + KEY, "my_[redacted-key]"),
    ("s\u200bk-abcdefghijklmnopqrstuvwx", "[redacted-key]"),
    (r"C:\Users\eric\a b\c.txt D:\e f\g.txt", r"[path]\c.txt [path]\g.txt"),
    (r"\\server\share\x.txt", r"[path]\x.txt"),
    ("~eric/secret.txt", "[path]/secret.txt"),
    ("~/x.txt", "~/x.txt"),
    ("%2Fhome%2Feric%2Fa.txt", "[path]/a.txt"),
    ("C%3A%5CUsers%5Ceric", "[path]"),
    ("file:///home/eric/x.md", "[path]/x.md"),
    ("「原因：」网络错误", "「原因：」网络错误"),
])
def test_w29_redaction_gaps(raw, expected):
    assert redact_text(raw) == expected


def test_w29_redact_secrets_only_touches_credentials():
    assert redact_secrets("NVDA 持仓 100 股 /home/eric") == "NVDA 持仓 100 股 /home/eric"
    assert redact_secrets(f"备注 {KEY}") == "备注 [redacted-key]"
    assert KEY not in redact_secrets("Bearer abcdefghijklmnopqrstu " + KEY)
    # a value with nothing to hide is returned exactly as typed (zero-width included)
    assert redact_secrets("备\u200b注 NVDA") == "备\u200b注 NVDA"


# ---------------------------------------------------------------- W60 values
def test_w60_holdings_values_are_scanned_in_records_but_not_outside_selections():
    record = {"selections": {"ticker": "NVDA", "holdings": {"note": f"我的 key {KEY}", "shares": 100, "items": [KEY]}},
              "status": "running"}
    clean = redaction.redact(record)
    assert KEY not in json.dumps(clean)
    assert clean["selections"]["ticker"] == "NVDA"
    assert clean["selections"]["holdings"]["shares"] == 100
    assert clean["status"] == "running"


def test_w60_save_config_does_not_store_a_pasted_key(home):
    saved = service.save_config({"portfolio_note": f"备注 {KEY}", "temperature": "0.5"})
    assert KEY not in json.dumps(saved)
    assert KEY not in service._config_path().read_text(encoding="utf-8")
    assert saved["temperature"] == "0.5"


# ---------------------------------------------------------------- W63 key names
@pytest.mark.parametrize("key", ["аpi_key", "ＴＯＫＥＮ", "pass\u200bword", "Sеcret", "ΤOKEN".replace("Τ", "τ"), "ＡＵＴＨＯＲＩＺＡＴＩＯＮ"])
def test_w63_look_alike_key_names_are_secret(key):
    assert redaction._secret_key_name(key)
    assert redaction.redact({"selections": {"holdings": {key: "plain-value"}}})["selections"]["holdings"][key] == "[redacted]"


@pytest.mark.parametrize("key", ["ticker", "shares", "备注", "max_tokens"])
def test_w63_ordinary_names_are_not(key):
    assert not redaction._secret_key_name(key) or key == "max_tokens"


def test_w63_save_config_drops_look_alike_key_names(home):
    saved = service.save_config({"аpi_key": "x", "ＴＯＫＥＮ": "y", "temperature": "0.4"})
    assert set(saved) == {"temperature"}


