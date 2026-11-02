"""SEC-001: the local console must not be drivable by other web pages.

Covers DNS rebinding (Host check on every request), cross-origin POSTs (Origin /
Referer / Sec-Fetch-Site) and the per-process session token on API calls.
Everything runs against a loopback server on an ephemeral port; nothing leaves the box.
"""

import http.client
import json
import socket
import threading
from pathlib import Path

import pytest

from webui import server, service

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo

SENTINEL_ENV = "OPENAI_API_KEY=sentinel-original\n"


@pytest.fixture
def console(monkeypatch, tmp_path):
    # Loopback only: the suite's network guard stays in force for every other address.
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))
    home = tmp_path / "home"
    home.mkdir()
    work = tmp_path / "repo"
    work.mkdir()
    (work / ".env").write_text(SENTINEL_ENV, encoding="utf-8")
    monkeypatch.chdir(work)
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(home))
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel-original")
    calls = []
    monkeypatch.setattr(service, "logout", lambda provider: calls.append(("logout", provider)) or {"provider": provider})
    monkeypatch.setattr(service, "start_validated_run", lambda selections, background=True, framework="tradingagents": calls.append(("run", selections)) or {"status": "running"})
    monkeypatch.setattr(service, "validate_analysis", lambda body: {"selections": dict(body), "ignored": []})
    monkeypatch.setattr(service, "auth_status", lambda plan=None: {"accounts": [], "live_validation": False})
    httpd = server.serve("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    def send(method, path, body=None, headers=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        merged = {"Content-Type": "application/json"}
        if token:
            merged[server.TOKEN_HEADER] = httpd.session_token
        merged.update(headers or {})
        payload = None if body is None else json.dumps(body).encode("utf-8")
        connection.request(method, path, body=payload, headers=merged)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, data, dict(response.getheaders()))
        connection.close()
        return result

    yield {"send": send, "port": port, "httpd": httpd, "calls": calls, "env": work / ".env", "home": home}
    httpd.shutdown()
    httpd.server_close()


def _state(console):
    return console["env"].read_text(encoding="utf-8"), sorted(p.name for p in console["home"].iterdir())


STATE_CHANGING = [
    ("/api/keys", {"provider": "openai", "key": "sk-attacker"}),
    ("/api/config", {"backend_url": "http://attacker.example/v1"}),
    ("/api/auth/logout", {"provider": "chatgpt"}),
    ("/api/runs", {"ticker": "NVDA"}),
]


@pytest.mark.parametrize("path,body", STATE_CHANGING)
def test_cross_origin_post_is_refused_and_changes_nothing(console, path, body):
    before = _state(console)
    # What a no-cors fetch from https://evil.example sends: an Origin, no token.
    for headers, token in (
        ({"Origin": "https://evil.example"}, False),
        ({"Origin": "https://evil.example"}, True),
        ({"Origin": "null"}, True),
        ({"Origin": f"http://localhost:{console['port'] + 1}"}, True),
        ({"Referer": "https://evil.example/page"}, True),
        ({"Sec-Fetch-Site": "cross-site"}, True),
        ({"Sec-Fetch-Site": "same-site"}, True),
    ):
        status, data, _ = console["send"]("POST", path, body, headers=headers, token=token)
        assert status == 403, (headers, data)
    assert _state(console) == before
    assert console["calls"] == []
    assert service.os.environ["OPENAI_API_KEY"] == "sentinel-original"


@pytest.mark.parametrize("path,body", STATE_CHANGING)
def test_missing_or_wrong_token_is_refused(console, path, body):
    before = _state(console)
    same = {"Origin": f"http://127.0.0.1:{console['port']}"}
    status, data, _ = console["send"]("POST", path, body, headers=same, token=False)
    assert status == 403 and "会话令牌" in data.decode("utf-8")
    status, data, _ = console["send"]("POST", path, body, headers={**same, server.TOKEN_HEADER: "wrong"}, token=False)
    assert status == 403
    status, data, _ = console["send"]("POST", path, body, headers={server.TOKEN_HEADER: ""}, token=False)
    assert status == 403
    assert _state(console) == before
    assert console["calls"] == []


@pytest.mark.parametrize("host", ["evil.example", "evil.example:{port}", "127.0.0.1:1", "127.0.0.1", "localhost.evil.example:{port}", "", "0.0.0.0:{port}"])
def test_forged_host_is_refused_for_get_and_post(console, host):
    before = _state(console)
    forged = {"Host": host.format(port=console["port"])}
    for method, path, body in (
        ("GET", "/", None),
        ("GET", "/api/config", None),
        ("GET", "/api/auth/status", None),
        ("GET", "/api/reports/file?path=x.md", None),
        ("POST", "/api/keys", {"provider": "openai", "key": "sk-attacker"}),
        ("POST", "/api/auth/logout", {"provider": "grok"}),
    ):
        status, data, _ = console["send"](method, path, body, headers=forged)
        assert status == 421, (method, path, host, data)
        assert server.TOKEN_META.encode() not in data
    assert _state(console) == before
    assert console["calls"] == []


def test_page_carries_the_token_and_same_origin_calls_work(console):
    port = console["port"]
    token = console["httpd"].session_token
    assert len(token) >= 32
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}"):
        status, page, headers = console["send"]("GET", "/", headers={"Host": host}, token=False)
        assert status == 200
        assert f'<meta name="{server.TOKEN_META}" content="{token}">' in page.decode("utf-8")
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    origin = {"Origin": f"http://127.0.0.1:{port}", "Sec-Fetch-Site": "same-origin"}
    status, data, _ = console["send"]("POST", "/api/keys", {"provider": "openai", "key": "sk-from-page"}, headers=origin)
    assert status == 200, data
    assert "sk-from-page" in console["env"].read_text(encoding="utf-8")
    status, data, _ = console["send"]("POST", "/api/auth/logout", {"provider": "chatgpt"},
                                      headers={"Host": f"localhost:{port}", "Origin": f"http://localhost:{port}"})
    assert status == 200, data
    status, data, _ = console["send"]("POST", "/api/runs", {"ticker": "NVDA"},
                                      headers={"Referer": f"http://127.0.0.1:{port}/"})
    assert status == 200, data
    # No Origin and no Referer (curl with the token) is allowed: the token is the proof.
    status, data, _ = console["send"]("POST", "/api/config", {"temperature": "0.5"})
    assert status == 200, data
    assert [item[0] for item in console["calls"]] == ["logout", "run"]


def test_api_get_needs_the_token_but_static_and_downloads_do_not(console):
    status, _, _ = console["send"]("GET", "/api/config", token=False)
    assert status == 403
    status, _, _ = console["send"]("GET", "/api/config", headers={server.TOKEN_HEADER: "nope"}, token=False)
    assert status == 403
    status, data, _ = console["send"]("GET", "/api/config")
    assert status == 200 and "saved" in json.loads(data)
    status, _, _ = console["send"]("GET", "/api/config", headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    for path in ("/app.js", "/app.css", "/icons/tauric.svg"):
        status, _, _ = console["send"]("GET", path, token=False)
        assert status == 200, path
    # Downloads are navigations (no custom header); they carry the link key k= instead
    # (SEC-002 W1) and are refused when a cross-site page triggers them.
    key = console["httpd"].link_key
    status, _, _ = console["send"]("GET", f"/api/reports/file?path=missing.md&k={key}", token=False)
    assert status == 404
    status, _, _ = console["send"]("GET", f"/api/reports/file?path=missing.md&k={key}", token=False,
                                   headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403


def test_logo_and_favicons_are_served_as_images_that_cannot_run_script(console):
    """WEBUI-007 item 2: logo.svg and its PNG fallbacks need no token and are never sniffed."""
    status, data, headers = console["send"]("GET", "/icons/logo.svg", token=False)
    assert status == 200 and data.startswith(b"<svg") and b"<script" not in data.lower()
    assert headers["Content-Type"] == "image/svg+xml; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in headers["Content-Security-Policy"] and "sandbox" in headers["Content-Security-Policy"]
    for size in (16, 32, 180):
        status, data, headers = console["send"]("GET", f"/icons/logo-{size}.png", token=False)
        assert status == 200 and data.startswith(b"\x89PNG"), size
        assert headers["Content-Type"] == "image/png"
        assert headers["X-Content-Type-Options"] == "nosniff"
    # Only files inside webui/static/icons are reachable through /icons/.
    for path in ("/icons/../server.py", "/icons/%2e%2e/server.py", "/icons/logo.svg/../../app.js", "/logo.svg"):
        status, data, _ = console["send"]("GET", path, token=False)
        assert status == 404 or not data.startswith(b"import"), path
    status, _, headers = console["send"]("GET", "/app.css", token=False)
    assert status == 200 and headers["Content-Type"] == "text/css; charset=utf-8" and headers["X-Content-Type-Options"] == "nosniff"


def test_tokens_differ_per_process():
    first = server.serve("127.0.0.1", 0)
    second = server.serve("127.0.0.1", 0)
    try:
        assert first.session_token != second.session_token
        assert f"127.0.0.1:{first.server_address[1]}" in first.allowed_hosts
        assert f"localhost:{first.server_address[1]}" in first.allowed_hosts
        assert f"[::1]:{first.server_address[1]}" in first.allowed_hosts
    finally:
        first.server_close()
        second.server_close()


def test_allowed_hosts_add_only_an_explicit_bind_address():
    assert server.allowed_hosts("0.0.0.0", 8765) == {"127.0.0.1:8765", "localhost:8765", "[::1]:8765"}
    assert "192.168.1.5:8765" in server.allowed_hosts("192.168.1.5", 8765)
    assert "[fe80::1]:8765" in server.allowed_hosts("fe80::1", 8765)


def test_frontend_sends_the_token_on_every_api_call():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert script.count("fetch(") == 1, "all API calls must go through api()"
    api_body = script[script.index("async function api("):script.index("function escapeHtml")]
    assert '"X-TradingAgents-Token": SESSION_TOKEN' in api_body
    assert 'meta[name="tradingagents-token"]' in script
    markup = Path("webui/static/index.html").read_text(encoding="utf-8")
    assert '<meta charset="utf-8">' in markup  # the server injects the token after it
