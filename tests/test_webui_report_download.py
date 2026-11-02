"""WEBUI-004 AC3: report downloads keep their type and file name.

「下载 Markdown」 used to save ``file.html`` because /api/reports/file always answered
text/html with no file name. Path traversal protection must stay in place.
"""

import http.client
import socket
import threading
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

from webui import server, service

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo


@pytest.fixture
def reports(monkeypatch, tmp_path):
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "home"))
    results = tmp_path / "results"
    run = results / "reports" / "NVDA_2026-10-01"
    run.mkdir(parents=True)
    monkeypatch.setitem(service.DEFAULT_CONFIG, "results_dir", str(results))
    files = {
        "complete_report.md": "# NVDA 报告\n\n买入。\n",
        "complete_report.html": "<!DOCTYPE html><title>r</title><p>报告</p>",
        "state.json": '{"rating": "BUY"}',
        "run.log": "line 1\n",
        "notes.txt": "plain\n",
        "腾讯 报告.md": "# 中文文件名\n",
        "blob.bin": "\x00\x01",
    }
    for name, text in files.items():
        (run / name).write_text(text, encoding="utf-8")
    (tmp_path / "secret.md").write_text("outside", encoding="utf-8")
    (run / "escape.md").symlink_to(tmp_path / "secret.md")
    httpd = server.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    def get(path, download=False):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        url = f"/api/reports/file?path={quote(str(path))}" + ("&download=1" if download else "") + f"&k={httpd.link_key}"
        connection.request("GET", url)
        response = connection.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, body, headers

    def api(path):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", path, headers={server.TOKEN_HEADER: httpd.session_token})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    yield {"get": get, "api": api, "run": run, "root": tmp_path}
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("name,content_type", [
    ("complete_report.md", "text/markdown; charset=utf-8"),
    ("state.json", "application/json"),
    ("run.log", "text/plain; charset=utf-8"),
    ("notes.txt", "text/plain; charset=utf-8"),
    ("blob.bin", "application/octet-stream"),
])
def test_download_has_the_right_type_and_file_name(reports, name, content_type):
    status, body, headers = reports["get"](reports["run"] / name, download=True)
    assert status == 200
    assert body == (reports["run"] / name).read_bytes()
    assert headers["content-type"] == content_type
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-disposition"] == f'attachment; filename="NVDA_2026-10-01_{name}"'


def test_markdown_is_an_attachment_even_without_the_download_flag(reports):
    status, _, headers = reports["get"](reports["run"] / "complete_report.md")
    assert status == 200
    assert headers["content-type"].startswith("text/markdown")
    assert headers["content-disposition"].startswith("attachment;")
    assert "html" not in headers["content-disposition"]


def test_html_opens_inline_sandboxed_and_downloads_as_html(reports):
    status, _, headers = reports["get"](reports["run"] / "complete_report.html")
    assert status == 200
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert "content-disposition" not in headers
    assert headers["content-security-policy"].startswith("sandbox")
    status, _, headers = reports["get"](reports["run"] / "complete_report.html", download=True)
    assert headers["content-disposition"] == 'attachment; filename="NVDA_2026-10-01_complete_report.html"'


def test_non_ascii_name_has_an_ascii_fallback_and_rfc5987_name(reports):
    status, body, headers = reports["get"](reports["run"] / "腾讯 报告.md", download=True)
    assert status == 200 and "中文文件名" in body.decode("utf-8")
    disposition = headers["content-disposition"]
    disposition.encode("latin-1")  # headers stay ASCII-safe
    fallback = disposition.split('filename="', 1)[1].split('"', 1)[0]
    assert fallback.isascii() and fallback.endswith(".md")
    star = disposition.split("filename*=UTF-8''", 1)[1]
    assert unquote(star) == "NVDA_2026-10-01_腾讯 报告.md"


def test_content_disposition_never_breaks_the_header():
    for name in ('a"b.md', "a\\b.md", "a;b.md", "报告.md", "\r\nSet-Cookie: x.md"):
        header = server.content_disposition(name)
        assert "\r" not in header and "\n" not in header
        fallback = header.split('filename="', 1)[1].split('"', 1)[0]
        assert '"' not in fallback and "\\" not in fallback and ";" not in fallback
        assert fallback.endswith(".md")


@pytest.mark.parametrize("raw", [
    "../../secret.md",
    "{root}/secret.md",
    "{run}/../../../secret.md",
    "{run}/escape.md",  # symlink inside reports/ that points outside
    "/etc/passwd",
])
def test_path_traversal_is_still_blocked(reports, raw):
    path = raw.format(root=reports["root"], run=reports["run"])
    status, body, headers = reports["get"](path, download=True)
    assert status == 404
    assert b"outside" not in body
    assert "content-disposition" not in headers
    assert "报告路径不在结果目录内" in body.decode("utf-8")


@pytest.mark.parametrize("raw", [
    "{run}/complete_report.md\x00.html",
    "\x00",
    "{run}\x00/../../secret.md",
])
@pytest.mark.parametrize("endpoint", ["/api/reports/file", "/api/reports/outline", "/api/live-sections"])
def test_a_nul_byte_in_a_report_path_is_a_clean_client_error(reports, raw, endpoint):
    path = raw.format(run=reports["run"])
    status, body = reports["api"](f"{endpoint}?path={quote(path)}&download=1")
    assert status == 400, (status, body)
    text = body.decode("utf-8")
    assert "路径无效" in text
    for leak in ("null", "lstat", "embedded", "Error", "Traceback", str(reports["root"])):
        assert leak not in text, text
    assert b"outside" not in body and b"NVDA" not in body


PATH_ENDPOINTS = ["/api/reports/file", "/api/reports/outline", "/api/live-sections", "/api/message-log"]
LEAKS = ("Errno", "File name too long", "Traceback", "OSError", "/tmp", "/home", "/Users")


def _assert_no_leak(body, root):
    text = body.decode("utf-8")
    for leak in (*LEAKS, str(root)):
        assert leak not in text, text
    return text


@pytest.mark.parametrize("raw", [
    "{run}/" + "a" * 300 + ".md",              # one segment past NAME_MAX
    "{run}/" + "报" * 100 + ".md",             # 300 UTF-8 bytes in 101 characters
    "{run}/" + "/".join(["x" * 200] * 25),     # 5000+ characters, each segment fine
    "/" + "b" * 5000,                           # one 5000-character path
])
@pytest.mark.parametrize("endpoint", PATH_ENDPOINTS)
def test_an_overlong_path_is_a_clean_client_error(reports, raw, endpoint):
    path = raw.format(run=reports["run"])
    status, body = reports["api"](f"{endpoint}?path={quote(path)}")
    assert status == 400, (status, body)
    assert "路径无效" in _assert_no_leak(body, reports["root"])


@pytest.mark.parametrize("endpoint", PATH_ENDPOINTS)
def test_a_path_the_os_refuses_to_stat_is_not_found(reports, monkeypatch, endpoint):
    """Past the length guard, any OS refusal on stat (ENAMETOOLONG, ELOOP, EACCES ...)
    is the endpoint's own 404, never a 500 carrying the OS text and server path."""
    def refuse(self, *args, **kwargs):
        raise OSError(36, "File name too long", str(self))

    monkeypatch.setattr(service.Path, "is_file", refuse)
    target = reports["run"] / "complete_report.md"
    if endpoint == "/api/live-sections":
        target = reports["run"] / "reports" / next(iter(service.LIVE_SECTION_FILES))
    elif endpoint == "/api/message-log":
        from cli.run import MESSAGE_TOOL_LOG_NAME
        target = reports["run"] / MESSAGE_TOOL_LOG_NAME
    status, body = reports["api"](f"{endpoint}?path={quote(str(target))}")
    assert status == 404, (status, body)
    _assert_no_leak(body, reports["root"])


def test_an_unexpected_api_failure_is_generic_and_logged_server_side(reports, monkeypatch, capfd):
    def broken(_path):
        raise OSError(36, "File name too long", str(reports["root"] / "secret"))

    monkeypatch.setattr(service, "report_outline", broken)
    status, body = reports["api"](f"/api/reports/outline?path={quote(str(reports['run']))}")
    assert status == 500
    text = _assert_no_leak(body, reports["root"])
    assert "服务器内部错误" in text
    logged = capfd.readouterr().err
    assert "File name too long" in logged and "/api/reports/outline" in logged
    assert "?path=" not in logged


def test_an_unexpected_report_read_failure_does_not_echo_internals(reports, monkeypatch):
    def broken(_path):
        raise RuntimeError(f"secret internal detail at {reports['root']}")

    monkeypatch.setattr(service, "resolve_report", broken)
    status, body, _headers = reports["get"](reports["run"] / "complete_report.md")
    assert status == 500
    assert "secret internal detail" not in body.decode("utf-8")
    assert "读取报告失败" in body.decode("utf-8")


def test_frontend_download_links_ask_for_an_attachment_with_the_report_name():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function reportDownloadHref" in script and "&download=1" in script
    assert '" download>' not in script, "every download link must name its file"
    assert script.count("download=\"${escapeHtml(reportFileName(") == 4
