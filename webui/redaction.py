"""Redaction of secret-looking values in console records, job files and API answers.

Split out of webui/service.py (RUN-003b, no behaviour change); ``webui.service``
re-exports ``redact``.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from tradingagents.redaction import INVISIBLE_CLASS

_SECRET_MARKERS = ("token", "api_key", "secret", "password", "authorization", "refresh",
                   # W88
                   "apikey", "api-key", "access_key", "access-key", "accesskey",
                   "private_key", "private-key", "privatekey", "cookie")
# W54 / MF-1 (PM): the run's own token-count parameters are numbers, not
# credentials. Only these exact, case-sensitive keys, only directly inside a
# run's ``selections`` (record, job file, API answers), and only while the value
# is a number (or empty) are exempt from key-name redaction. Case variants,
# other *_tokens names and the same keys anywhere else (holdings JSON, nested
# dicts) are redacted like any other secret-looking key.
_PARAM_COUNT_KEYS = frozenset({"max_tokens", "max_output_tokens"})
_NUMBER_TEXT = re.compile(r"\s*[-+]?\d+(?:\.\d+)?\s*")


# W63: letters from other scripts that look like the Latin ones in the markers.
_CONFUSABLES = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ї": "i", "ј": "j", "ѕ": "s", "ԁ": "d",
    "һ": "h", "ԛ": "q", "ԝ": "w", "ӏ": "l",
    # Greek (ν is folded to "v" here and to "n" in the second spelling, W88)
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p",
    "τ": "t", "υ": "u", "χ": "x", "ω": "w",
    # Latin look-alikes NFKC keeps
    "ı": "i", "ȷ": "j", "ɑ": "a", "ɡ": "g",
    # W88: small capitals (ᴛoken)
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ɢ": "g", "ʜ": "h", "ɪ": "i",
    "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m", "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ꞯ": "q", "ʀ": "r",
    "ꜱ": "s", "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
})
# W88/W116: the second spelling reads Greek ν as "n" and υ as "y" (ΤΟΚΕΝ, ΑΡΙ_ΚΕΥ).
_NU_AS_N = str.maketrans({"ν": "n", "υ": "y"})
# W85: the same invisible set as error-text redaction.
_INVISIBLE = re.compile(INVISIBLE_CLASS)


def _fold(text: str, extra=None) -> str:
    text = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).casefold()
    if extra:
        text = text.translate(extra)
    return unicodedata.normalize("NFKC", text.translate(_CONFUSABLES))


def fold_key(key) -> str:
    """A key name as the sensitive-name check sees it (W63): NFKC (full-width ->
    ASCII), invisible characters dropped, case folded, look-alike letters folded."""
    return _fold(str(key))


def _secret_key_name(key: str) -> bool:
    """The key's name alone looks like a credential (save_config never stores these)."""
    folded = fold_key(key)
    if any(marker in folded for marker in _SECRET_MARKERS):
        return True
    # W88/W116: ΤΟΚΕΝ, ΑΡΙ_ΚΕΥ; only worth a second fold when a ν or υ is there.
    text = str(key)
    return any(ch in "νυ" for ch in unicodedata.normalize("NFKC", text).casefold()) and any(
        marker in _fold(text, _NU_AS_N) for marker in _SECRET_MARKERS)


# W113: instrument codes are never redacted, in config.json, run records or the
# job file: XAI-USD, SK-HYNIX, sk-/rt-/pk-/ghp_ symbols reach the run as typed.
_CODE_KEYS = frozenset({"ticker", "tickers", "symbol", "symbols", "code", "codes", "instrument"})


def _is_code_field(key, value: Any) -> bool:
    if not isinstance(key, str) or fold_key(key) not in _CODE_KEYS:
        return False
    if isinstance(value, str):
        return True
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


# W124 / W125 (D1d-r5): a code field is redacted only when the value LOOKS like a key.
#   1. NFKC fold (full-width -> ASCII) and drop zero-width / tag (U+E0001...) characters;
#   2. per token: split on "-", "_", "."; at least one separator, EVERY piece 1-4
#      characters long and under 19 characters without separators -> a real code,
#      kept (XAI-USD-PERP-2026: 3, 3, 4, 4);
#   3. otherwise drop every "-", "_", "."; the longest known key prefix followed by 16 or
#      more letters/digits (any case, digits alone too) -> redact; anything else is kept
#      (SK-HYNIX, 600519.SS, BRK.B, RT-PCR-INDEX-2026: short body or no known prefix).
# Accepted cost: sk-hynix-longer-symbol (a 6-letter piece, sk + 17) is redacted.
# D1d-r5: the short-piece KEEP needs under 19 characters without separators, so
# ghp_abcd_efgh_ijkl_mnop (19) and sk-proj-abcd-efgh-ijkl-mnop (22) are redacted.
# D1d-r6 (W126): keys are cut out of quotes, brackets, key=, ":" and commas, and
# "/", NBSP and U+2010 count as separators too; rules 1 and 2 are unchanged.
# D1d-r7 (W127/W128): the known prefix is found anywhere inside a run (NVDA/sk-proj-...),
# in one linear pass; rules 1 and 2 are unchanged.
# Long Bearer/Basic tokens and JWTs keep their earlier rule.
_KEY_RUN = 16
_SHORT_PIECE = 4
_SHORT_TOTAL = 19
_KEY_PREFIXES = ("skantapi03", "skant", "skproj", "sess", "sk", "xai", "ghp", "gho", "ghs", "rt", "pk")
_SEPARATORS = re.compile(r"[-_./\u2010]")
_WIDE_SEPS = re.compile("[\u00a0\u2011\u202f]")
_RUN = re.compile(r"[A-Za-z0-9\-_./:\u2010]+")
_INVISIBLE = re.compile(INVISIBLE_CLASS)
_BEARER = re.compile(rf"(?i:\b(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]{{{_KEY_RUN},}}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")


def _fold_code(value: str) -> str:
    value = _WIDE_SEPS.sub("\u2010", value)
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", value))


def _key_start(run: str) -> int | None:
    """W126/W127/W128: where a key starts inside a run of key characters (cut out of
    quotes / brackets / key= / commas). The known prefix may sit anywhere in the run
    (NVDA/sk-proj-..., NVDAghp_..., api:sk-...); ":" counts as a separator here.
    One left-to-right pass over the run; rule 1 is only re-checked for the few start
    points whose tail is under 19 characters without separators, so the cost is linear."""
    chars, where = [], []
    for i, ch in enumerate(run):
        if ch != ":" and not _SEPARATORS.match(ch):
            chars.append(ch.lower())
            where.append(i)
    joined = "".join(chars)
    n = len(joined)
    for k in range(n - _KEY_RUN - 1):
        prefix = next((p for p in _KEY_PREFIXES if joined.startswith(p, k)), None)
        if prefix is None or n - k - len(prefix) < _KEY_RUN:
            continue
        if n - k < _SHORT_TOTAL and _short_pieces(run[where[k]:].replace(":", "-")):
            continue
        return where[k]
    return None


def _key_run(run: str) -> bool:
    return _key_start(run) is not None


def _cut_key(run: str) -> str:
    start = _key_start(run)
    return run if start is None else run[:start] + "[redacted-key]"


def _short_pieces(token: str) -> bool:
    pieces = _SEPARATORS.split(token)
    return (len(pieces) > 1 and all(1 <= len(piece) <= _SHORT_PIECE for piece in pieces)
            and len("".join(pieces)) < _SHORT_TOTAL)


def _scan_one(value: str) -> str:
    folded = _fold_code(value)
    tokens = _RUN.findall(folded)
    if not (_JWT.search(folded) or _BEARER.search(folded) or any(_key_run(t) for t in tokens)):
        return value
    text = _BEARER.sub("[redacted-key]", _JWT.sub("[redacted-key]", folded))
    return scan_value(_RUN.sub(lambda m: _cut_key(m.group()), text))


def _scan_code(value):
    """W120 + W124: codes stay byte-identical; a key-looking value is redacted."""
    if isinstance(value, list):
        return [_scan_one(item) for item in value]
    return _scan_one(value)


def scan_value(value: str) -> str:
    """W60: a string value with a credential in it, under any key name, is not
    kept in plain text: the same prefixed-key / Bearer / JWT rules as error text."""
    from tradingagents.redaction import redact_secrets

    return redact_secrets(value)


def _is_count_value(value: Any) -> bool:
    if value is None or isinstance(value, bool):
        return value is None
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and (value == "" or bool(_NUMBER_TEXT.fullmatch(value)))


def _looks_secret(key: str, value: Any = None, *, params: bool = False) -> bool:
    """Whether ``key: value`` is redacted in run records, job files and API answers.

    ``params`` is true only for the keys directly inside a run's ``selections``:
    there ``max_tokens`` / ``max_output_tokens`` holding a number stay readable,
    so a restart gets them back (W54, MF-1).
    """
    if not _secret_key_name(key):
        return False
    return not (params and isinstance(key, str) and key in _PARAM_COUNT_KEYS and _is_count_value(value))


def redact(value: Any, _scope: str = "root") -> Any:
    """Secret-looking keys become ``[redacted]``; the run's numeric parameters are kept (W54).

    Used for everything that leaves the console (API answers) and for the run
    record and job file on disk, which therefore never hold a credential while
    still holding every parameter a restart needs. ``_scope`` is "outside" (not
    within any ``selections``), "params" (the keys of a ``selections`` dict) or
    "inside" (anything nested deeper, e.g. the holdings JSON): only "params"
    gets the exemption (MF-1).
    """
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if _is_code_field(key, item):
                clean[key] = _scan_code(item)
                continue
            if _looks_secret(key, item, params=_scope == "params"):
                clean[key] = "[redacted]"
                continue
            clean[key] = redact(item, _child_scope(_scope, key, item))
        return clean
    if isinstance(value, str) and _scope in ("params", "inside"):
        # W60: the run's own answers (holdings note, ticker, ...) are scanned by value.
        return scan_value(value)
    if isinstance(value, list):
        # only the run list directly under the root holds run records (runs[i])
        child = "slot" if _scope == "runs" else ("outside" if _scope in ("root", "slot", "outside") else "inside")
        return [redact(item, child) for item in value]
    return value


def _child_scope(scope: str, key, item) -> str:
    """Where ``selections`` is a run's parameter layer (W62): only at fixed positions.

    Those are the record or job top level (``root``), each ``runs[i]`` and the
    ``run`` of an API answer (``slot``). A ``selections`` dict anywhere else is
    ordinary data with no exemption.
    """
    if scope in ("root", "slot") and key == "selections" and isinstance(item, dict):
        return "params"
    if scope == "root" and key == "run" and isinstance(item, dict):
        return "slot"
    if scope == "root" and key == "runs" and isinstance(item, list):
        return "runs"
    if scope in ("root", "slot", "outside", "runs"):
        return "outside"
    return "inside"


def scrub_config(value: Any, _top: bool = True) -> Any:
    """W84: settings with no credential in plain text at any depth.

    Secret-named keys are dropped at the top level (as before) and become
    ``[redacted]`` deeper down (holdings, portfolio fields); every string, in
    dicts and lists alike, is scanned by value (W60).
    """
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if _is_code_field(key, item):
                clean[key] = _scan_code(item)
                continue
            if _secret_key_name(key):
                if not _top:
                    clean[key] = "[redacted]"
                continue
            clean[key] = scrub_config(item, False)
        return clean
    if isinstance(value, list):
        return [scrub_config(item, False) for item in value]
    if isinstance(value, str):
        return scan_value(value)
    return value
