"""V2-AUTH-LOGIN: console sign-in, server-side sessions, forced first password change.

One local account (the user store holds a dict so more could be added later). The
password is kept only as a salted ``hashlib.scrypt`` hash (stdlib; argon2-cffi and
bcrypt are not dependencies). The store is ``<webui home>/console-users.json``
(``~/.tradingagents/webui`` unless ``TRADINGAGENTS_WEBUI_HOME`` is set), mode 0600,
outside git, never ``.env``.

Sessions live in this process's memory only: a console restart signs everyone out.
Each session carries its own API token (the ``X-TradingAgents-Token`` value the
console page receives), so the token is only ever handed to a signed-in session and
is checked together with that session's cookie. Passwords and session ids are never
logged or returned.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from pathlib import Path

USER_STORE_NAME = "console-users.json"
COOKIE_NAME = "ta_session"
MIN_PASSWORD_LENGTH = 8
SESSION_IDLE_SECONDS = 8 * 3600
SESSION_MAX_SECONDS = 24 * 3600
FAIL_LIMIT = 5
FAIL_WINDOW_SECONDS = 15 * 60
LOCKOUT_SECONDS = 15 * 60
# W205/W208: bound the failure tables. Every email (known or not) has its own counter, expired
# entries are pruned
# at most every PRUNE_INTERVAL_SECONDS, and past MAX_FAILURE_KEYS the oldest entries are dropped.
MAX_FAILURE_KEYS = 4096
PRUNE_INTERVAL_SECONDS = 60

# scrypt cost: 2**15 * 8 * 128 bytes = 32 MiB per hash.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024

BAD_CREDENTIALS = "邮箱或密码错误"
EMPTY_FIELD = "此字段不能为空"
RATE_LIMITED = "尝试次数过多，已暂时锁定，请 {minutes} 分钟后再试。"
NOT_INITIALIZED = ("控制台还没有设置登录账户。请在运行控制台的电脑上执行 "
                   "`tradingagents-launcher auth init --email <你的邮箱>` 创建账户后再打开。")
LOGIN_REQUIRED = "请先登录。"
MUST_CHANGE = "首次登录请先修改密码。"
PASSWORD_TOO_SHORT = f"新密码至少 {MIN_PASSWORD_LENGTH} 位。"
PASSWORD_SAME = "新密码不能和旧密码相同。"
OLD_PASSWORD_WRONG = "旧密码不正确。"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


# --- password hashing ---------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            maxmem=_SCRYPT_MAXMEM, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=base64.b64decode(salt), n=int(n), r=int(r),
                                p=int(p), maxmem=_SCRYPT_MAXMEM, dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the email is unknown, so a wrong email costs as long as a wrong password.
_DUMMY_HASH = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


# --- user store -----------------------------------------------------------------------

def normalize_email(email: str) -> str:
    return (email or "").strip().casefold()


def store_path() -> Path:
    from webui.service import webui_home

    return webui_home() / USER_STORE_NAME


def load_users(path: Path | None = None) -> dict:
    path = path or store_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {}
    users = data.get("users") if isinstance(data, dict) else None
    return users if isinstance(users, dict) else {}


def save_users(users: dict, path: Path | None = None) -> None:
    path = path or store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "users": users}, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    os.chmod(path, 0o600)


def create_user(email: str, password: str, *, force: bool = False, must_change: bool = True,
                path: Path | None = None) -> Path:
    path = path or store_path()
    key = normalize_email(email)
    if not key or "@" not in key:
        raise AuthError("邮箱格式不对。")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LENGTH} 位。")
    users = load_users(path)
    if users and not force:
        raise AuthError(f"已经有登录账户（{path}）。确实要覆盖请加 --force。", 409)
    users = {key: {"email": email.strip(), "hash": hash_password(password), "must_change": must_change,
                   "updated": int(time.time())}}
    save_users(users, path)
    return path


# --- sessions and rate limiting -----------------------------------------------------

def _is_loopback(ip: str) -> bool:
    import ipaddress

    try:
        address = ipaddress.ip_address(str(ip).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


class AuthState:
    """Per-server sign-in state. Sessions and failure counters are in memory only."""

    def __init__(self, path: Path | None = None):
        self._path = path
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._last_prune = 0.0

    @property
    def path(self) -> Path:
        return self._path or store_path()

    def users(self) -> dict:
        return load_users(self.path)

    def initialized(self) -> bool:
        return bool(self.users())

    # sessions
    def session(self, sid: str | None) -> dict | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            data = self._sessions.get(sid)
            if data is None:
                return None
            if now - data["last"] > SESSION_IDLE_SECONDS or now - data["created"] > SESSION_MAX_SECONDS:
                self._sessions.pop(sid, None)
                return None
            user = self.users().get(data["user"])
            if user is None:                       # account removed or re-initialised elsewhere
                self._sessions.pop(sid, None)
                return None
            data["last"] = now
            data["must_change"] = bool(user.get("must_change"))
            data["email"] = user.get("email") or data["user"]
            return data

    def _new_session(self, key: str) -> tuple[str, dict]:
        sid = secrets.token_urlsafe(32)
        now = time.time()
        data = {"user": key, "token": secrets.token_urlsafe(32), "created": now, "last": now}
        self._sessions[sid] = data
        return sid, data

    def logout(self, sid: str | None) -> None:
        with self._lock:
            self._sessions.pop(sid or "", None)

    # rate limit
    def _locked(self, key: str, now: float) -> float:
        until = self._locked_until.get(key, 0)
        if until > now:
            return until - now
        self._locked_until.pop(key, None)
        return 0

    def _prune(self, now: float, *, force: bool = False) -> None:
        """Drop expired failure/lock records; cap the table size (caller holds the lock)."""
        if not force and now - self._last_prune < PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        for key in list(self._failures):
            recent = [t for t in self._failures[key] if now - t < FAIL_WINDOW_SECONDS]
            if recent:
                self._failures[key] = recent
            else:
                del self._failures[key]
        for key in [k for k, until in self._locked_until.items() if until <= now]:
            del self._locked_until[key]
        for table, last in ((self._failures, lambda v: v[-1]), (self._locked_until, lambda v: v)):
            excess = len(table) - MAX_FAILURE_KEYS
            if excess > 0:
                for key in sorted(table, key=lambda k: last(table[k]))[:excess]:
                    del table[key]

    @staticmethod
    def _limit_keys(ip: str, account: str) -> tuple[str, ...]:
        """Rate-limit buckets for one attempt.

        W208: every normalized email gets its own counter, whether or not the account exists,
        so an unknown email answers exactly like a real one (memory is bounded by the cap).

        W205 ruling: loopback clients (127.0.0.0/8, ::1) are limited per account only, so one
        locked account does not lock the others on this computer; other addresses are limited
        per IP and per account.
        """
        acct = f"acct:{account}"
        if _is_loopback(ip):
            return (acct,)
        return (f"ip:{ip}", acct)

    def failure_keys(self) -> int:
        with self._lock:
            return len(self._failures) + len(self._locked_until)

    def _fail(self, key: str, now: float) -> None:
        recent = [t for t in self._failures.get(key, []) if now - t < FAIL_WINDOW_SECONDS]
        recent.append(now)
        if len(recent) >= FAIL_LIMIT:
            self._locked_until[key] = now + LOCKOUT_SECONDS
            recent = []
        self._failures[key] = recent

    def login(self, email: str, password: str, ip: str) -> tuple[str, dict]:
        if not str(email or "").strip() or not password:
            raise AuthError(EMPTY_FIELD, 400)
        users = self.users()
        if not users:
            raise AuthError(NOT_INITIALIZED, 503, not_initialized=True)
        key = normalize_email(email)
        user = users.get(key)
        keys = self._limit_keys(ip, key)
        now = time.time()
        # W203: check and reserve in one critical section. The attempt counts as a failure
        # before the (slow, unlocked) scrypt check, so concurrent guesses cannot overrun the limit.
        with self._lock:
            self._prune(now, force=len(self._failures) + len(self._locked_until) > MAX_FAILURE_KEYS)
            wait = max(self._locked(k, now) for k in keys)
            if not wait:
                for k in keys:
                    self._fail(k, now)
        if wait:
            raise AuthError(RATE_LIMITED.format(minutes=max(1, int(wait // 60) + (wait % 60 > 0))), 429)
        ok = verify_password(password, user["hash"] if user else _dummy_hash()) and user is not None
        with self._lock:
            if not ok:
                raise AuthError(BAD_CREDENTIALS, 401)
            for k in keys:
                self._failures.pop(k, None)
                self._locked_until.pop(k, None)
            sid, data = self._new_session(key)
        data["must_change"] = bool(user.get("must_change"))
        data["email"] = user.get("email") or key
        return sid, data

    def change_password(self, sid: str, old: str | None, new: str, *, require_old: bool) -> tuple[str, dict]:
        data = self.session(sid)
        if data is None:
            raise AuthError(LOGIN_REQUIRED, 401)
        users = self.users()
        user = users.get(data["user"])
        if user is None:
            raise AuthError(LOGIN_REQUIRED, 401)
        if require_old and not verify_password(old or "", user["hash"]):
            raise AuthError(OLD_PASSWORD_WRONG, 400)
        if len(new or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(PASSWORD_TOO_SHORT, 400)
        if verify_password(new, user["hash"]):
            raise AuthError(PASSWORD_SAME, 400)
        user.update(hash=hash_password(new), must_change=False, updated=int(time.time()))
        save_users(users, self.path)
        with self._lock:
            # every other session of this account ends; this one gets a fresh id and token
            for other in [s for s, d in self._sessions.items() if d["user"] == data["user"]]:
                self._sessions.pop(other, None)
            new_sid, new_data = self._new_session(data["user"])
        new_data["must_change"] = False
        new_data["email"] = user.get("email") or data["user"]
        return new_sid, new_data


def session_cookie(sid: str, *, secure: bool = False) -> str:
    """W206: ``secure`` adds the Secure flag when the request came over HTTPS."""
    return f"{COOKIE_NAME}={sid}; Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if secure else "")


def clear_cookie(*, secure: bool = False) -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if secure else "")


def cookie_sid(header: str | None) -> str | None:
    from http.cookies import CookieError, SimpleCookie

    if not header:
        return None
    jar = SimpleCookie()
    try:
        jar.load(header)
    except CookieError:
        return None
    morsel = jar.get(COOKIE_NAME)
    return morsel.value if morsel and morsel.value else None


# --- init command -----------------------------------------------------------------

INIT_USAGE = "tradingagents-launcher auth init --email <邮箱> [--force] [--password-stdin]"


def _read_password(from_stdin: bool) -> str:
    if from_stdin or not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\r\n")
    first = getpass.getpass("初始密码（不回显）：")
    second = getpass.getpass("再输入一次：")
    if first != second:
        raise AuthError("两次输入的密码不一致。")
    return first


PASSWORD_ARG_REFUSED = "不支持在参数里传密码，请用交互输入或 --password-stdin"


def _password_in_argv(argv: list[str]) -> bool:
    """W204: any ``--password`` form (or an abbreviation of it) other than --password-stdin."""
    for token in argv:
        name = token.split("=", 1)[0]
        if name == "--password-stdin":
            continue
        if len(name) >= 3 and ("--password".startswith(name) or name.startswith("--password")):
            return True
    return False


class _QuietParser(argparse.ArgumentParser):
    """Argparse errors would echo argument values (a password, say); print usage only."""

    def error(self, message):  # noqa: ARG002 - the message may contain argument values
        self.exit(2, f"参数有误。用法：{INIT_USAGE}\n")


def cli_main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if _password_in_argv(argv):
        print(PASSWORD_ARG_REFUSED, file=sys.stderr)
        return 2
    parser = _QuietParser(prog="tradingagents-launcher auth", description="控制台登录账户",
                          allow_abbrev=False)
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="创建登录账户（首次登录必须改密码）", allow_abbrev=False)
    init.add_argument("--email", required=True)
    init.add_argument("--force", action="store_true", help="覆盖已有账户")
    init.add_argument("--password-stdin", action="store_true", help="从标准输入读一行作为密码")
    args = parser.parse_args(argv)
    try:
        password = _read_password(args.password_stdin)
        path = create_user(args.email, password, force=args.force)
    except AuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2 if exc.status != 409 else 1
    print(f"已创建登录账户，账户文件：{path}（权限 0600）。首次登录后必须修改密码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
