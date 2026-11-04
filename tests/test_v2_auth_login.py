"""V2-AUTH-LOGIN: sign-in, sessions, rate limit, forced password change, init command.

Every password here is generated at runtime with ``secrets``; none is written to a file
outside the test's temp directory.
"""

import http.client
import json
import os
import re
import secrets
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from webui import console_auth, server

pytestmark = pytest.mark.console_signed_out

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo
EMAIL = "owner@example.invalid"
SERVER_SOURCE = Path(server.__file__).read_text(encoding="utf-8")


def _pw() -> str:
    return secrets.token_urlsafe(16)


@pytest.fixture
def console(monkeypatch, tmp_path):
    refused_connect, refused_lookup = socket.socket.connect, socket.getaddrinfo
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else refused_connect)(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else refused_lookup)(host, *args, **kwargs))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(home))
    httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def send(method, path, body=None, headers=None, cookie=None, token=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        merged = {"Content-Type": "application/json"}
        if cookie:
            merged["Cookie"] = f"{console_auth.COOKIE_NAME}={cookie}"
        if token:
            merged[server.TOKEN_HEADER] = token
        merged.update(headers or {})
        payload = None if body is None else json.dumps(body).encode("utf-8")
        connection.request(method, path, body=payload, headers=merged)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data.decode("utf-8", "replace"), {k.lower(): v for k, v in response.getheaders()}

    yield {"send": send, "port": port, "httpd": httpd, "home": home, "store": home / console_auth.USER_STORE_NAME}
    httpd.shutdown()
    httpd.server_close()


def _init(console, *, must_change=True):
    password = _pw()
    console_auth.create_user(EMAIL, password, must_change=must_change, path=console["store"])
    return password


def _login(console, password, email=EMAIL):
    status, body, headers = console["send"]("POST", "/api/account/login", {"email": email, "password": password})
    sid = None
    match = re.search(rf"{console_auth.COOKIE_NAME}=([^;]+)", headers.get("set-cookie", ""))
    if match:
        sid = match.group(1)
    return status, json.loads(body), headers, sid


def _token(console, sid, path="/"):
    status, page, _ = console["send"]("GET", path, cookie=sid)
    assert status == 200, page
    return re.search(r'name="tradingagents-token" content="([^"]*)"', page).group(1)


def _signed_in(console):
    password = _init(console, must_change=False)
    status, _, _, sid = _login(console, password)
    assert status == 200
    return password, sid, _token(console, sid)


# --- route audit --------------------------------------------------------------------

def _routes():
    get = set(re.findall(r'parsed\.path == "(/[^"]*)"', SERVER_SOURCE))
    get |= {"/api/info-crawl/sources", "/api/info-crawl/jobs", "/api/info-crawl/items", "/index.html",
            "/vendor/lightweight-charts.js", "/app.js", "/app.css", "/nonexistent", "/api/nonexistent"}
    post = set(re.findall(r'^\s+"(/api/[^"]+)": lambda', SERVER_SOURCE, re.M)) | {"/api/info-crawl/jobs"}
    return sorted(get), sorted(post)


GET_ROUTES, POST_ROUTES = _routes()
PUBLIC_GET = server._PUBLIC_GET
PUBLIC_POST = server._PUBLIC_POST


def test_route_audit_found_the_routes():
    assert {"/api/runs", "/api/config", "/api/reports/file", "/api/kline", "/api/catalog"} <= set(GET_ROUTES)
    assert {"/api/runs", "/api/keys", "/api/config", "/api/runs/stop", "/api/auth/login"} <= set(POST_ROUTES)
    assert {"/login", "/login.js", "/login.css", "/console.css", "/api/account/state"} == PUBLIC_GET
    assert {"/api/account/login", "/api/account/logout"} == PUBLIC_POST


@pytest.mark.parametrize("initialized", [False, True])
def test_every_protected_route_is_closed_when_signed_out(console, initialized):
    if initialized:
        _init(console)
    for path in GET_ROUTES:
        if path in PUBLIC_GET:
            continue
        status, body, headers = console["send"]("GET", path)
        if path.startswith("/api/"):
            assert status == 401, (path, status, body)
            assert json.loads(body)["login_required"] is True
        else:
            assert status == 303 and headers["location"] == "/login", (path, status)
    # A guessed or stale cookie is the same as none.
    status, _, _ = console["send"]("GET", "/api/runs", cookie=secrets.token_urlsafe(32))
    assert status == 401
    for path in POST_ROUTES:
        status, body, _ = console["send"]("POST", path, {})
        assert status == 401, (path, status, body)


def test_the_old_process_token_and_link_key_do_not_bypass_login(console):
    _init(console)
    httpd = console["httpd"]
    assert not getattr(httpd, "session_token", "")          # there is no per-process API token any more
    for guess in ("", secrets.token_urlsafe(32)):
        status, _, _ = console["send"]("GET", "/api/runs", token=guess or None)
        assert status == 401
        status, _, _ = console["send"]("POST", "/api/runs", {"ticker": "NVDA"}, token=guess or None)
        assert status == 401
    status, _, _ = console["send"]("GET", f"/api/reports/file?path=x&k={httpd.link_key}")
    assert status == 401


def test_token_of_one_session_does_not_work_with_another_or_without_cookie(console):
    password, sid, token = _signed_in(console)
    status, _, _ = console["send"]("GET", "/api/session", token=token)          # token without cookie
    assert status == 401
    status, _, _, other = _login(console, password)
    assert status == 200 and other != sid
    status, body, _ = console["send"]("GET", "/api/session", cookie=other, token=token)
    assert status == 403 and "会话令牌" in body
    status, _, _ = console["send"]("GET", "/api/session", cookie=sid, token=token)
    assert status == 200


def test_public_routes_and_login_page_assets(console):
    status, page, headers = console["send"]("GET", "/login")
    assert status == 200 and "text/html" in headers["content-type"]
    assert 'content="init"' in page                          # no user yet
    for path in ("/login.js", "/login.css", "/console.css", "/icons/logo.svg"):
        assert console["send"]("GET", path)[0] == 200, path
    status, body, _ = console["send"]("GET", "/api/account/state")
    assert json.loads(body) == {"initialized": False, "signed_in": False, "must_change": False}


# --- no user ------------------------------------------------------------------------

def test_no_user_shows_init_page_and_keeps_api_locked(console):
    status, page, _ = console["send"]("GET", "/login")
    assert "tradingagents-launcher auth init --email" in page and 'content="init"' in page
    status, _, headers = console["send"]("GET", "/")
    assert status == 303 and headers["location"] == "/login"
    status, body, _ = console["send"]("GET", "/api/runs")
    assert status == 401 and "auth init" in json.loads(body)["error"]
    status, body, _ = console["send"]("POST", "/api/account/login", {"email": EMAIL, "password": _pw()})
    assert status == 503 and json.loads(body)["not_initialized"] is True


# --- login ---------------------------------------------------------------------------

def test_login_flow_cookie_flags_and_page_token(console):
    password = _init(console, must_change=False)
    status, page, _ = console["send"]("GET", "/login")
    assert 'content="login"' in page and "下一步" in page and "通过密码验证" in page and "返回登录页" in page
    assert "此字段不能为空" in Path("webui/static/login.js").read_text(encoding="utf-8")
    status, body, headers, sid = _login(console, password)
    assert status == 200 and body == {"ok": True, "must_change": False}
    cookie = headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    assert len(sid) >= 40 and sid not in json.dumps(body)
    token = _token(console, sid)
    assert len(token) >= 40 and token != sid
    status, page, _ = console["send"]("GET", "/", cookie=sid)
    assert 'name="tradingagents-account" content="owner@example.invalid"' in page
    assert "修改密码" in page and "退出登录" in page
    assert console["send"]("GET", "/api/session", cookie=sid, token=token)[0] == 200
    # signed in: /login goes to the console
    status, _, headers = console["send"]("GET", "/login", cookie=sid)
    assert status == 303 and headers["location"] == "/"
    # email is case/space-insensitive
    assert _login(console, password, email="  OWNER@example.INVALID ")[0] == 200


def test_empty_fields_are_refused(console):
    _init(console)
    for body in ({"email": "", "password": _pw()}, {"email": EMAIL, "password": ""}, {}):
        status, data, _ = console["send"]("POST", "/api/account/login", body)
        assert status == 400 and json.loads(data)["error"] == "此字段不能为空"


def test_wrong_email_and_wrong_password_get_the_same_answer(console):
    password = _init(console)
    a = _login(console, password + "x")
    b = _login(console, password, email="nobody@example.invalid")
    assert a[0] == b[0] == 401
    assert a[1] == b[1] == {"error": "邮箱或密码错误"}
    assert a[3] is None and b[3] is None


def test_rate_limit_per_account_and_per_ip(console, monkeypatch):
    password = _init(console)
    for _ in range(console_auth.FAIL_LIMIT):
        assert _login(console, _pw())[0] == 401
    status, body, _, sid = _login(console, password)            # right password, but locked
    assert status == 429 and "尝试次数过多" in body["error"] and sid is None
    # W205 ruling: from loopback the lock is per account, so another account is not locked
    status, body, _, _ = _login(console, _pw(), email="other@example.invalid")
    assert status == 401
    # the lock ends after the window
    import time as time_module
    real = time_module.time
    monkeypatch.setattr(console_auth.time, "time", lambda: real() + console_auth.LOCKOUT_SECONDS + 1)
    assert _login(console, password)[0] == 200


def test_rate_limit_counts_per_account_from_different_addresses():
    state = console_auth.AuthState(Path(os.environ.get("TMPDIR", "/tmp")) / f"ta-auth-{secrets.token_hex(4)}.json")
    password = _pw()
    console_auth.create_user(EMAIL, password, path=state.path)
    try:
        for n in range(console_auth.FAIL_LIMIT):
            with pytest.raises(console_auth.AuthError):
                state.login(EMAIL, _pw(), f"10.0.0.{n}")
        with pytest.raises(console_auth.AuthError) as caught:
            state.login(EMAIL, password, "10.0.0.99")
        assert caught.value.status == 429
    finally:
        state.path.unlink()


def test_login_post_is_csrf_checked(console):
    password = _init(console)
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}):
        status, _, _ = console["send"]("POST", "/api/account/login", {"email": EMAIL, "password": password},
                                       headers=headers)
        assert status == 403


# --- first login / change password / logout -----------------------------------------

def test_first_login_forces_a_password_change(console):
    initial = _init(console, must_change=True)
    status, body, _, sid = _login(console, initial)
    assert status == 200 and body["must_change"] is True
    status, _, headers = console["send"]("GET", "/", cookie=sid)
    assert status == 303 and headers["location"] == "/login"
    status, page, _ = console["send"]("GET", "/login", cookie=sid)
    assert 'content="force"' in page and "首次登录请修改密码" in page
    token = re.search(r'name="tradingagents-token" content="([^"]+)"', page).group(1)
    for path in ("/api/runs", "/api/config", "/api/session"):
        status, data, _ = console["send"]("GET", path, cookie=sid, token=token)
        assert status == 403 and json.loads(data)["must_change"] is True, path
    status, data, _ = console["send"]("POST", "/api/runs", {"ticker": "NVDA"}, cookie=sid, token=token)
    assert status == 403
    # change without the token is refused (CSRF)
    new = _pw()
    assert console["send"]("POST", "/api/account/password", {"new_password": new}, cookie=sid)[0] == 403
    # same as old / too short
    status, data, _ = console["send"]("POST", "/api/account/password", {"new_password": initial}, cookie=sid, token=token)
    assert status == 400 and "不能和旧密码相同" in data
    status, data, _ = console["send"]("POST", "/api/account/password", {"new_password": "short"}, cookie=sid, token=token)
    assert status == 400 and "至少" in data
    status, data, headers = console["send"]("POST", "/api/account/password", {"new_password": new}, cookie=sid, token=token)
    assert status == 200
    new_sid = re.search(rf"{console_auth.COOKIE_NAME}=([^;]+)", headers["set-cookie"]).group(1)
    assert new_sid != sid
    assert console["send"]("GET", "/", cookie=sid)[0] == 303            # old session ended
    new_token = _token(console, new_sid)
    assert console["send"]("GET", "/api/session", cookie=new_sid, token=new_token)[0] == 200
    assert _login(console, initial)[0] == 401
    assert _login(console, new)[1]["must_change"] is False
    stored = console["store"].read_text(encoding="utf-8")
    assert initial not in stored and new not in stored and "scrypt$" in stored


def test_change_password_needs_the_old_one(console):
    old, sid, token = _signed_in(console)
    new = _pw()
    status, page, _ = console["send"]("GET", "/login?change=1", cookie=sid)
    assert status == 200 and 'content="change"' in page
    status, data, _ = console["send"]("POST", "/api/account/password",
                                      {"old_password": _pw(), "new_password": new}, cookie=sid, token=token)
    assert status == 400 and "旧密码不正确" in data
    status, _, _ = console["send"]("POST", "/api/account/password",
                                   {"old_password": old, "new_password": new}, cookie=sid, token=token)
    assert status == 200
    assert _login(console, old)[0] == 401 and _login(console, new)[0] == 200


def test_logout_ends_the_session(console):
    _, sid, token = _signed_in(console)
    status, _, headers = console["send"]("POST", "/api/account/logout", {}, cookie=sid, token=token)
    assert status == 200 and "Max-Age=0" in headers["set-cookie"]
    assert console["send"]("GET", "/api/session", cookie=sid, token=token)[0] == 401
    assert console["send"]("GET", "/", cookie=sid)[0] == 303


def test_session_expires_server_side(console, monkeypatch):
    _, sid, token = _signed_in(console)
    import time as time_module
    real = time_module.time
    monkeypatch.setattr(console_auth.time, "time", lambda: real() + console_auth.SESSION_IDLE_SECONDS + 5)
    assert console["send"]("GET", "/api/session", cookie=sid, token=token)[0] == 401


def test_no_password_or_session_id_in_request_log(console, capsys, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_REQUEST_LOG", "1")
    password, sid, _ = _signed_in(console)
    out = capsys.readouterr()
    assert password not in out.out + out.err and sid not in out.out + out.err


# --- store and init command ---------------------------------------------------------

def test_store_is_a_0600_salted_scrypt_hash(tmp_path):
    path = tmp_path / "users.json"
    password = _pw()
    console_auth.create_user(EMAIL, password, path=path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    assert password not in text
    user = json.loads(text)["users"][EMAIL]
    assert user["must_change"] is True and user["hash"].startswith("scrypt$")
    assert console_auth.hash_password(password) != console_auth.hash_password(password)      # salted
    assert console_auth.verify_password(password, user["hash"]) and not console_auth.verify_password(_pw(), user["hash"])


def _init_cmd(home, *args, stdin=""):
    env = {**os.environ, "TRADINGAGENTS_WEBUI_HOME": str(home), "PYTHONPATH": str(Path.cwd())}
    return subprocess.run([sys.executable, "-m", "cli.launcher", "auth", "init", *args], input=stdin,
                          capture_output=True, text=True, env=env, timeout=60)


def test_init_command_reads_stdin_and_refuses_to_overwrite(tmp_path):
    password = _pw()
    done = _init_cmd(tmp_path, "--email", EMAIL, "--password-stdin", stdin=password + "\n")
    assert done.returncode == 0, done.stderr
    assert password not in done.stdout + done.stderr
    store = tmp_path / console_auth.USER_STORE_NAME
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert json.loads(store.read_text())["users"][EMAIL]["must_change"] is True
    again = _init_cmd(tmp_path, "--email", EMAIL, "--password-stdin", stdin=_pw() + "\n")
    assert again.returncode == 1 and "--force" in again.stderr
    forced = _init_cmd(tmp_path, "--email", EMAIL, "--force", "--password-stdin", stdin=_pw() + "\n")
    assert forced.returncode == 0
    short = _init_cmd(tmp_path / "x", "--email", EMAIL, "--password-stdin", stdin="abc\n")
    assert short.returncode == 2


def test_init_command_has_no_password_argument():
    parser_text = Path(console_auth.__file__).read_text(encoding="utf-8")
    assert 'add_argument("--password"' not in parser_text
    assert "getpass.getpass" in parser_text


# --- QA W203-W206 --------------------------------------------------------------------

def test_w203_concurrent_wrong_logins_cannot_overrun_the_limit(console):
    _init(console)
    statuses = []
    barrier = threading.Barrier(20)

    def attempt():
        barrier.wait()
        statuses.append(_login(console, _pw())[0])

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(statuses) == 20
    assert 429 in statuses
    assert statuses.count(401) == console_auth.FAIL_LIMIT     # attempts are reserved before scrypt
    assert set(statuses) == {401, 429}


@pytest.mark.parametrize("form", ["--password", "--password=", "--pass", "--passw="])
def test_w204_password_argument_is_refused_without_echo(tmp_path, form):
    secret = _pw()
    args = ["--email", EMAIL, form + secret] if form.endswith("=") else ["--email", EMAIL, form, secret]
    done = _init_cmd(tmp_path, *args)
    assert done.returncode == 2
    assert done.stderr.strip() == "不支持在参数里传密码，请用交互输入或 --password-stdin"
    assert secret not in done.stdout + done.stderr
    assert not (tmp_path / console_auth.USER_STORE_NAME).exists()


def test_w204_argparse_errors_do_not_echo_values(tmp_path):
    secret = _pw()
    done = _init_cmd(tmp_path, "--email", EMAIL, "--password-stdin", secret, stdin=_pw() + "\n")
    assert done.returncode == 2
    assert secret not in done.stdout + done.stderr
    assert "用法" in done.stderr


def test_w205_failure_records_are_bounded(monkeypatch, tmp_path):
    state = console_auth.AuthState(tmp_path / "users.json")
    console_auth.create_user(EMAIL, _pw(), path=state.path)
    monkeypatch.setattr(console_auth, "verify_password", lambda *_: False)
    monkeypatch.setattr(console_auth, "MAX_FAILURE_KEYS", 50)
    # W208: each unknown email has its own record (one IP record + three account records)
    for n in range(3):
        with pytest.raises(console_auth.AuthError):
            state.login(f"nobody{n}@example.invalid", _pw(), "10.1.0.1")
    assert state.failure_keys() == 4
    # many addresses: the table stays capped
    for n in range(200):
        with pytest.raises(console_auth.AuthError):
            state.login(f"x{n}@example.invalid", _pw(), f"10.2.{n // 250}.{n % 250}")
    assert state.failure_keys() <= 2 * 50 + 2
    # after the window everything expired is pruned
    real = console_auth.time.time
    monkeypatch.setattr(console_auth.time, "time", lambda: real() + console_auth.FAIL_WINDOW_SECONDS
                        + console_auth.LOCKOUT_SECONDS + 1)
    with pytest.raises(console_auth.AuthError):
        state.login(EMAIL, _pw(), "10.3.0.1")
    assert state.failure_keys() == 2


def test_w205_per_ip_lockout_still_applies_to_unknown_accounts(monkeypatch, tmp_path):
    state = console_auth.AuthState(tmp_path / "users.json")
    console_auth.create_user(EMAIL, _pw(), path=state.path)
    monkeypatch.setattr(console_auth, "verify_password", lambda *_: False)
    for n in range(console_auth.FAIL_LIMIT):
        with pytest.raises(console_auth.AuthError):
            state.login(f"u{n}@example.invalid", _pw(), "10.4.0.1")
    with pytest.raises(console_auth.AuthError) as caught:
        state.login(EMAIL, _pw(), "10.4.0.1")
    assert caught.value.status == 429


def test_w206_lan_warning_recommends_https():
    ok, message = server.lan_bind_check("0.0.0.0", allow_lan=True)
    assert ok and "建议走 HTTPS" in message


def test_w206_cookie_is_secure_only_over_https(console):
    password = _init(console, must_change=False)
    status, _, headers = console["send"]("POST", "/api/account/login", {"email": EMAIL, "password": password})
    assert status == 200 and "Secure" not in headers["set-cookie"]
    status, _, headers = console["send"]("POST", "/api/account/login", {"email": EMAIL, "password": password},
                                         headers={"X-Forwarded-Proto": "https"})
    assert status == 200 and headers["set-cookie"].endswith("; Secure")
    assert console_auth.session_cookie("x", secure=True).endswith("; Secure")
    assert console_auth.clear_cookie(secure=True).endswith("; Secure")


def test_w205_loopback_locks_per_account_only(tmp_path):
    # The store holds one account, so the "other account" is any other email name.
    state = console_auth.AuthState(tmp_path / "users.json")
    password = _pw()
    console_auth.create_user(EMAIL, password, path=state.path)
    for loopback in ("127.0.0.1", "::1"):
        for _ in range(console_auth.FAIL_LIMIT):           # lock a different account name
            with pytest.raises(console_auth.AuthError):
                state.login("someone@example.invalid", _pw(), loopback)
        with pytest.raises(console_auth.AuthError) as caught:
            state.login("someone@example.invalid", _pw(), loopback)
        assert caught.value.status == 429
        sid, _ = state.login(EMAIL, password, loopback)     # the real account still signs in
        assert sid
        state = console_auth.AuthState(state.path)
    assert console_auth.AuthState._limit_keys("10.0.0.5", "a") == ("ip:10.0.0.5", "acct:a")
    assert console_auth.AuthState._limit_keys("::ffff:127.0.0.1", "a") == ("acct:a",)


# --- W208: unknown emails behave exactly like real ones --------------------------------

def _statuses(state, email, tries=7, ip="127.0.0.1"):
    out = []
    for _ in range(tries):
        try:
            state.login(email, _pw(), ip)
            out.append(200)
        except console_auth.AuthError as exc:
            out.append(exc.status)
    return out


def test_w208_unknown_and_real_email_answer_identically(tmp_path):
    state = console_auth.AuthState(tmp_path / "users.json")
    console_auth.create_user(EMAIL, _pw(), path=state.path)
    real = _statuses(state, EMAIL)
    unknown = _statuses(state, "Nobody@Example.invalid")
    expected = [401] * console_auth.FAIL_LIMIT + [429] * (7 - console_auth.FAIL_LIMIT)
    assert real == unknown == expected
    # the same email in another case is the same (normalized) counter
    assert _statuses(state, "nobody@example.INVALID", tries=1) == [429]


def test_w208_unknown_emails_run_the_dummy_hash(monkeypatch, tmp_path):
    state = console_auth.AuthState(tmp_path / "users.json")
    console_auth.create_user(EMAIL, _pw(), path=state.path)
    seen = []
    real_verify = console_auth.verify_password
    monkeypatch.setattr(console_auth, "verify_password", lambda pw, stored: seen.append(stored) or real_verify(pw, stored))
    _statuses(state, "ghost@example.invalid", tries=1)
    assert seen == [console_auth._dummy_hash()]


def test_w208_locking_one_unknown_email_leaves_others_alone(tmp_path):
    state = console_auth.AuthState(tmp_path / "users.json")
    password = _pw()
    console_auth.create_user(EMAIL, password, path=state.path)
    assert _statuses(state, "a@example.invalid", tries=6)[-1] == 429
    assert _statuses(state, "b@example.invalid", tries=1) == [401]
    sid, _ = state.login(EMAIL, password, "127.0.0.1")
    assert sid


def test_w208_cap_holds_with_many_unknown_emails(monkeypatch, tmp_path):
    state = console_auth.AuthState(tmp_path / "users.json")
    console_auth.create_user(EMAIL, _pw(), path=state.path)
    monkeypatch.setattr(console_auth, "verify_password", lambda *_: False)
    monkeypatch.setattr(console_auth, "MAX_FAILURE_KEYS", 50)
    for n in range(500):
        with pytest.raises(console_auth.AuthError):
            state.login(f"ghost{n}@example.invalid", _pw(), "127.0.0.1")
    assert state.failure_keys() <= 2 * 50 + 1
