"""Shape checks for the local subscription sign-in files.

The login code of each provider writes one fixed JSON shape. A file that parses as
JSON but does not have that shape (a hand edit, a truncated write by another tool, a
different program's file at the same path) must not be read as "signed in" or as
"signed out": every reader asks the same validator, so status, pre-run checks and
token use agree. Local checks only; nothing here contacts a provider.
"""

from __future__ import annotations

import errno
import math
import os
import stat
from pathlib import Path
from typing import Any

_MISSING = object()
AUTH_READ_MAX = 1_000_000  # bytes; a sign-in file is a few KB


def _is_number(value: Any) -> bool:
    """A real, finite timestamp. JSON parsers accept NaN/Infinity; a NaN expiry never
    compares as due and an infinite one never expires, so both are a damaged file.

    W96: a JSON integer bigger than a float can hold makes ``math.isfinite`` raise
    OverflowError; that is a damaged file, not a crash.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _text(value: Any) -> bool:
    # W94: whitespace-only is empty — never a usable access_token / email / …
    return isinstance(value, str) and bool(value.strip())


def read_auth_file_text(path: Path) -> str:
    """Read a local sign-in file as UTF-8 (a leading BOM is fine, W93).

    W92: FIFOs, devices and directories are refused up front so status never hangs
    waiting for a writer. Symlinks to regular files are followed. Raises
    FileNotFoundError when absent; OSError with strerror set for other problems.
    """
    path = Path(path)
    try:
        info = path.stat()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise OSError(exc.errno or errno.EIO, exc.strerror or type(exc).__name__) from exc
    if stat.S_ISDIR(info.st_mode):
        raise OSError(errno.EISDIR, "is a directory")
    if not stat.S_ISREG(info.st_mode):
        raise OSError(errno.EINVAL, "is not a regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                block = os.read(fd, 65536)
            except BlockingIOError as exc:  # open FIFO that slipped past the mode check
                raise OSError(errno.EAGAIN, "read would block") from exc
            if not block:
                break
            total += len(block)
            if total > AUTH_READ_MAX:
                raise OSError(errno.EFBIG, "file is too large")
            chunks.append(block)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8-sig")


def auth_read_problem(path: Path, exc: BaseException) -> str:
    """English/Chinese reason for a failed ``read_auth_file_text``, for status lines."""
    name = Path(path).name
    if isinstance(exc, FileNotFoundError):
        return f"{name} is missing"
    if isinstance(exc, UnicodeDecodeError):
        return f"cannot read {name}: UnicodeDecodeError"
    if isinstance(exc, OSError):
        if exc.errno in (errno.EINVAL, errno.EISDIR, errno.EAGAIN) or (
            exc.strerror and "not a regular file" in exc.strerror
        ):
            # W92: clear Chinese — never hang, never a traceback
            return f"{name} 不是普通文件（例如管道、设备或目录），无法读取"
        if exc.errno == errno.EFBIG:
            return f"{name} is too large"
        return f"cannot read {name}: {type(exc).__name__}"
    return f"cannot read {name}: {type(exc).__name__}"


def _optional(data: dict, key: str, ok, label: str, what: str) -> str | None:
    value = data.get(key, _MISSING)
    if value is _MISSING or value is None or ok(value):
        return None
    return f"{label}.{key} is not {what}"


def token_problem(tokens: Any, label: str = "tokens") -> str | None:
    """Problems with a ``tokens`` record as both providers' ``_token_record`` write it."""
    if not isinstance(tokens, dict):
        return f"{label} is not an object"
    if not _text(tokens.get("access_token")):
        return f"{label}.access_token is missing or not a non-empty string"
    return (
        _optional(tokens, "refresh_token", _text, label, "a non-empty string")
        or _optional(tokens, "expires_at", _is_number, label, "a finite number")
        or _optional(tokens, "earliest_refresh_at", _is_number, label, "a finite number")
        or _optional(tokens, "scopes", lambda v: isinstance(v, list) and all(isinstance(s, str) for s in v),
                     label, "a list of strings")
    )


def account_problem(account: Any, required: tuple[str, ...] = (), label: str = "account") -> str | None:
    """Problems with an ``account`` record: an object of string labels."""
    if not isinstance(account, dict):
        return f"{label} is not an object"
    for key in required:
        if not _text(account.get(key)):
            return f"{label}.{key} is missing or not a non-empty string"
    for key in ("client_id", "email", "subject"):
        problem = _optional(account, key, lambda v: isinstance(v, str), label, "a string")
        if problem:
            return problem
    return None


def file_problem(data: Any, *, account_required_with_tokens: tuple[str, ...] | None) -> str | None:
    """Why a parsed sign-in file does not have the shape the login code writes.

    ``account_required_with_tokens`` names the account fields a signed-in file must
    carry (``None`` means the account record itself is optional).
    """
    if not isinstance(data, dict):
        return "the file is not a JSON object"
    problem = _optional(data, "host_id", _text, "file", "a non-empty string")
    if problem:
        return problem.replace("file.", "")
    if "tokens" not in data:
        account = data.get("account", _MISSING)
        if account is _MISSING or account is None:
            return None
        return account_problem(account)
    problem = token_problem(data["tokens"])
    if problem:
        return problem
    account = data.get("account", _MISSING)
    if account_required_with_tokens is None and (account is _MISSING or account is None):
        return None
    if account is _MISSING:
        return "account is missing"
    return account_problem(account, account_required_with_tokens or ())
