"""Redaction for anything an error message carries to disk or to a page.

Used for the console's failure file, run state and log (``webui.run_failure``)
and for the ``__error__`` writes LangGraph keeps in the checkpoint database
(``tradingagents.graph.checkpointer``). Tokens, keys, cookies, e-mail addresses,
absolute paths and the user name in a home directory never get through.
"""

from __future__ import annotations

import os
import re

_COOKIE_NAMES = (
    r"[\w.-]*sess(?:ion)?(?:[_-]?id|[_-]?token|[_-]?key)?|[\w.-]*sid|[\w.-]*csrf[\w-]*|"
    r"[\w.-]*xsrf[\w-]*|[\w.-]*auth[\w-]*|jwt|remember[_-]?(?:me|token)|cf_clearance|__cf\w*"
)

_SECRET_PATTERNS = (
    # Whole Cookie / Set-Cookie header values, to the end of the line.
    (re.compile(r"(?im)\b(set-cookie|cookie)(\"?\s*[:=：＝]\s*)[^\r\n]+"), r"\1\2[redacted-cookie]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+"), r"\1 [redacted]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?"), "[redacted-token]"),
    # Prefixed keys of any length: xai-abc123, sk-..., ghp_...
    # Not after a letter or digit, but after "_" too (my_sk-..., W29).
    (re.compile(r"(?<![A-Za-z0-9])(?:sk|xai|sess|rt|pk|ghp|gho|ya29)[-_][A-Za-z0-9_-]{3,}"), "[redacted-key]"),
    # W86/W113: SK-/XAI- in another case only with a key-length body (12+), so a
    # ticker such as XAI-USD or SK-HYNIX is never taken for a key.
    (re.compile(r"(?<![A-Za-z0-9])(?i:sk|xai)[-_][A-Za-z0-9_-]{12,}"), "[redacted-key]"),
    (re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|password|"
        r"authorization|client[_-]?secret|key)[\"']?\s*[:=：＝]\s*[\"']?)(?!(?:bearer|basic)\s)[^\s\"'&,;}，；]+"
    ), r"\1[redacted]"),
    # name=value session cookies outside a Cookie header: sessionid=..., connect.sid=...
    (re.compile(rf"(?i)(?<![\w.-])((?:{_COOKIE_NAMES})\s*[=＝]\s*)[^\s;,&\"']+"), r"\1[redacted]"),
    # The local part is only tried where a run of its characters starts, so a
    # long run without an @ is scanned once: linear time, not quadratic (W28).
    (re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[redacted-email]"),
    (re.compile(r"\b[A-Za-z0-9_+/=-]{32,}"), "[redacted]"),
)

# W60: the rules for a credential inside an ordinary value (a holdings note):
# Bearer/Basic, JWTs and prefixed keys. Paths, e-mail and long ids stay as typed.
# W118/W113: in a value only key-length credentials count (12+ characters after
# Bearer / the prefix), so "sk-learn", "SK-Hynix stake", "Bearer market",
# XAI-USD and rt-/pk-/ghp_ tickers stay as typed.
_VALUE_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|basic)\s+(?=[A-Za-z0-9._~+/=-]{16,})[A-Za-z0-9._~+/=-]+"), r"\1 [redacted]"),
    _SECRET_PATTERNS[2],
    (re.compile(r"(?<![A-Za-z0-9])(?i:sk|xai|sess|rt|pk|ghp|gho|ya29)[-_][A-Za-z0-9_-]{12,}"), "[redacted-key]"),
)
# W29/W85: zero-width / invisible characters a key or key name can be split with.
# One set for error text, values and key-name folding (webui.redaction uses it too):
# bidi isolates U+2066-2069, Mongolian selectors U+180B-180F, Hangul fillers.
INVISIBLE_CLASS = ("[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
                   "\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0"
                   # W115: tag characters and musical-symbol format characters
                   "\U000e0000-\U000e007f\U0001d173-\U0001d17a\U000e0100-\U000e01ef]")
_INVISIBLE = re.compile(INVISIBLE_CLASS)
# The case-insensitive, key-length prefixed rule (W86/W113), found by its replacement.
_PREFIXED = next(pattern for pattern, replacement in _VALUE_PATTERNS if replacement == "[redacted-key]")
# Full-width ASCII U+FF01-FF5E -> U+0021-007E, one character to one.
_FULLWIDTH = {code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)}
_HAS_FULLWIDTH = re.compile("[\uff01-\uff5e]")


def _shadow(value: str) -> str:
    """W86: ``value`` with full-width ASCII (``ｓｋ－``) folded one character to one,
    so a match in the shadow is the same span in ``value``."""
    return value.translate(_FULLWIDTH)


def _fullwidth_keys(value: str) -> tuple[str, int]:
    """Prefixed keys written partly or wholly in full-width letters."""
    if not _HAS_FULLWIDTH.search(value):
        return value, 0
    spans = [m.span() for m in _PREFIXED.finditer(_shadow(value))]
    for start, end in reversed(spans):
        value = value[:start] + "[redacted-key]" + value[end:]
    return value, len(spans)


def redact_secrets(text) -> str:
    """Only the credential rules, for values that are not error text (W60)."""
    original = "" if text is None else str(text)
    value = _INVISIBLE.sub("", original)
    value, count = _fullwidth_keys(value)
    found = count > 0
    for pattern, replacement in _VALUE_PATTERNS:
        value, count = pattern.subn(replacement, value)
        found = found or count > 0
    return value if found else original


_SEGMENT = r"[^\s/\"'<>()\[\],;]+"
# file:// URLs, then absolute POSIX paths (not the path part of an http URL).
# Inner directories may contain single spaces (``/Users/Zhou Han/...``); the
# last part may not, so prose after a path is left alone.
_FILE_URL = re.compile(r"(?i)\bfile:(?://[^\s/\"'<>]*)?/[^\s\"'<>]*")
_POSIX_PATH = re.compile(
    # A bare home directory (``/home/bob``, ``/Users/Zhou Han/``): no name kept.
    rf"(?<![\w.:/~-])/(?:home|Users)/{_SEGMENT}(?: {_SEGMENT})*+/?(?=[\s\"'<>()\[\],;]|$)"
    rf"|(?<![\w.:/~-])/(?:{_SEGMENT}(?: {_SEGMENT})*/)+[^\s/\"'<>()\[\],;]*"
)
# W29: one backslash, two (a Python repr), or a forward slash (C:/Users/...).
_WSEP = r"(?:\\\\|\\|/)"
_WINDOWS_SEGMENT = r"[^\s\\/\"'<>:|?*]+"
# A space may sit inside a directory name. A segment never holds ":", so the part
# after a space never starts a new drive (``C:\a b\c.txt D:\e\f.txt`` is two paths).
_WINDOWS_DIR = rf"{_WINDOWS_SEGMENT}(?: {_WINDOWS_SEGMENT})*"
_WINDOWS_PATH = re.compile(
    rf"\b[A-Za-z]:{_WSEP}Users{_WSEP}{_WINDOWS_DIR}+{_WSEP}?(?=[\s\"'<>]|$)"
    rf"|\b[A-Za-z]:{_WSEP}(?:{_WINDOWS_DIR}{_WSEP})*[^\s\\/\"'<>]*"
    # UNC: \\server\share\... (or \\\\server\\share in a repr)
    rf"|(?<![\w\\])(?:\\\\\\\\|\\\\)(?=[^\s\\/])(?:{_WINDOWS_DIR}{_WSEP})+[^\s\\/\"'<>]*"
    rf"|(?<![\w\\])(?:\\\\\\\\|\\\\)[^\s\\/\"'<>]+(?=[\s\"'<>]|$)"
)
# ~user and ~user/... (another account's home); ``~/...`` names no one and stays.
_TILDE_USER = re.compile(
    rf"(?<![\w~/.-])~[A-Za-z_][\w.-]*(?:/{_SEGMENT}(?: {_SEGMENT})*)*/?(?=[\s\"'<>()\[\],;:]|$)"
)
# URL-encoded paths: %2Fhome%2Fbob%2Fx.txt, C:%5CUsers%5C...
_ENCODED_PATH = re.compile(
    r"(?i)(?<![\w%])(?:[a-z](?::|%3A))?(?:%2F|%5C)(?:[^\s\"'<>%]|%[0-9a-f]{2})*(?:%2F|%5C)(?:[^\s\"'<>%]|%[0-9a-f]{2})*"
)
_HOME_PARENTS = {"home", "users"}
# W87: ``%25`` before two hex digits is one more encoding layer (``%252F`` -> ``%2F``).
_DOUBLE = re.compile(r"(?i)%25(?=[0-9a-f]{2})")
# An encoded separator left after the encoded-path rule (``/home%2Fbob%2Fx``):
# turned back into a plain one, so the POSIX / Windows rules see the whole path.
_LOOSE_SEP = re.compile(r"(?i)%2F|%5C")


def _decode_layers(value: str) -> str:
    for _ in range(8):
        value, count = _DOUBLE.subn("%", value)
        if not count:
            break
    return value


def _keep_name(text: str, sep: str) -> str:
    """``[path]`` plus the last name, unless that name is a home-directory user."""
    parts = [part for part in text.rstrip(sep).split(sep) if part]
    if not parts:
        return "[path]"
    if len(parts) >= 2 and parts[-2].lower() in _HOME_PARENTS:
        return "[path]"
    return f"[path]{sep}{parts[-1]}"


def _keep_last(text: str) -> str:
    """``[path]`` plus the last name, for a path with any mix of separators."""
    windows = "\\" in text
    parts = [part for part in re.split(r"[\\/]+", text) if part]
    sep = "\\" if windows else "/"
    if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
        parts = parts[1:]
    if not parts or (len(parts) >= 2 and parts[-2].lower() in _HOME_PARENTS):
        return "[path]"
    if len(parts) == 1 and (text.lstrip("\\").startswith(parts[0]) and text.startswith("\\")):
        return "[path]"                                   # a bare \\server
    return f"[path]{sep}{parts[-1]}"


def _tilde_user(match: re.Match) -> str:
    parts = [part for part in match.group(0).split("/") if part]
    return "[path]" if len(parts) == 1 else f"[path]/{parts[-1]}"


def _encoded_path(match: re.Match) -> str:
    from urllib.parse import unquote

    return _keep_last(re.sub(r"^[A-Za-z]:", "", unquote(match.group(0))))


def _file_url(match: re.Match) -> str:
    rest = re.sub(r"(?i)^file:(?://[^/]*)?", "", match.group(0))
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    return _keep_name(rest.replace("%20", " "), "/")


# Text is cut to this many times the caller's limit before it is redacted, so
# a huge message costs no more than a short one (W28).
_PRE_CUT = 4


def _cut(value: str, limit: int, tail: bool) -> str:
    """At most ``limit * _PRE_CUT`` characters, without a token cut in half.

    The token the cut runs through is dropped: half a key no longer looks like
    a key, so it must not reach the redaction rules as a stray fragment. Text
    with no space at all is one token longer than any key and is kept whole.
    """
    size = max(int(limit), 1) * _PRE_CUT
    if len(value) <= size:
        return value
    part = value[-size:] if tail else value[:size]
    if tail:
        index = 0
        while index < len(part) and not part[index].isspace():
            index += 1
        return part[index:] if index < len(part) else part
    index = len(part)
    while index > 0 and not part[index - 1].isspace():
        index -= 1
    return part[:index] if index else part


def redact_text(text, limit: int | None = None, *, tail: bool = False) -> str:
    """``text`` without tokens, keys, cookies, e-mail addresses or absolute paths.

    With ``limit``, the text is cut before it is redacted (see ``_cut``) and
    the result is at most ``limit`` characters: the start, or the end with
    ``tail`` (a traceback's last lines matter most).
    """
    value = "" if text is None else str(text)
    if limit is not None:
        value = _cut(value, limit, tail)
    home = os.path.expanduser("~")
    if home and home not in ("/", "~"):
        value = value.replace(home, "~")
    value = _INVISIBLE.sub("", value)                       # W29: split keys / key names
    value, _ = _fullwidth_keys(value)                      # W86
    value = _decode_layers(value)                          # W87: %252F, /home%2Fbob
    value = _ENCODED_PATH.sub(_encoded_path, value)
    value = _LOOSE_SEP.sub(lambda m: "/" if m.group(0)[-1] in "fF" else "\\", value)
    value = _FILE_URL.sub(_file_url, value)
    value = _WINDOWS_PATH.sub(lambda match: _keep_last(match.group(0)), value)
    value = _TILDE_USER.sub(_tilde_user, value)
    value = _POSIX_PATH.sub(lambda match: _keep_name(match.group(0), "/"), value)
    for pattern, replacement in _SECRET_PATTERNS:
        value = pattern.sub(replacement, value)
    if limit is not None:
        value = value[-limit:] if tail else value[:limit]
    return value
