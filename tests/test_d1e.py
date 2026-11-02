"""D1d-r6 / W126: keys wrapped in punctuation, glued to a label, or split by "/",
NBSP or U+2010 redact under code keys; rule 1 (< 19) and real codes are unchanged."""
from __future__ import annotations

import json
import re
import time

import pytest

from tests.test_d1d import CODE_NAMES, LONG_CODES, _payload
from tests.test_run003b import (
    SEL,
    home as home,  # noqa: F401  (fixture)
    sleepers as sleepers,  # noqa: F401  (fixture)
)
from webui import service
from webui.redaction import _scan_code, redact, scrub_config

pytestmark = pytest.mark.unit
BODY = "A1b2C3d4E5f6G7h8J9k0L1m2"
KEYS = ["sk-proj-" + BODY, "ghp_" + BODY, "sk-proj_" + BODY]
WRAPS = {"comma": "{},", "dquote": '"{}"', "squote": "'{}'", "paren": "({})", "bracket": "[{}]",
         "brace": "{{{}}}", "key=": "key={}", "=": "={}", "colon-label": "api:{}", "semicolon": "{};",
         "in text": "hold ({}), then", "fullwidth paren": "（{}）"}
INNER = {"colon": "sk-proj:" + BODY, "slash": "sk-proj/" + BODY, "slash body": "sk-proj-A1b2C3d4/E5f6G7h8J9k0L1m2",
         "nbsp": "sk-proj\u00a0" + BODY, "nbsp body": "sk-proj-A1b2C3d4\u00a0E5f6G7h8J9k0L1m2",
         "u2010": "sk\u2010proj\u2010" + BODY, "u2011": "sk\u2011proj\u2011" + BODY,
         "ghp slash": "ghp/" + BODY, "ghp u2010": "ghp\u2010" + BODY, "quoted nbsp": '"sk-proj\u00a0' + BODY + '",'}
FORMS = {f"{k} {w}": t.format(k) for k in KEYS for w, t in WRAPS.items()} | INNER
KEEP = LONG_CODES + ["ABCDE-FGHIJ-KLMNO-PQRST", "BRK.B", "600519.SS", "XAI-USD-PER6", "600519SH0600519", "XAI-USD",
                     "BRK-B", "0700.HK", "GC=F", "NVDA", "AAPL", "XAI-USD-PERP-2026,", "(SK-HYNIX)", '"BRK.B"',
                     "ticker=600519.SS", "BRK/B", "NASDAQ:AAPL", "SK:HYNIX", "XAI:USD:PERP:2026", '"SK-HYNIX"',
                     "NVDA/BRK.B", "NVDA/sk-abcd-efgh-ijkl-mnop",
                     "sk/abcd/efgh/ijkl/mnop", "sk\u2010abcd\u2010efgh\u2010ijkl\u2010mnop", "sk:abcd:efgh:ijkl:mnop",
                     "sk\u00a0abcd\u00a0efgh\u00a0ijkl\u00a0mnop", "(NVDA/xai/1234/5678/9012/345)", "XAI\u2010USD", "sk-abcd-efgh-ijkl-mnop", "(sk-abcd-efgh-ijkl-mnop)"]


def _leaks(text):
    plain = re.sub(r"[^A-Za-z0-9]", "", text)
    return BODY in plain or "A1b2C3d4" in text or "E5f6G7h8J9k0L1m2" in text


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("form", sorted(FORMS))
def test_w126_forms_redact_everywhere(home, name, form):
    value = FORMS[form]
    service.save_config(_payload(name, value))
    disk = (service.webui_home() / "config.json").read_text(encoding="utf-8")
    for text in (disk, json.dumps(service.public_saved_config(), ensure_ascii=False),
                 json.dumps(redact({"runs": [{"selections": _payload(name, value)}]}), ensure_ascii=False)):
        assert not _leaks(text), text
        assert "[redacted-key]" in text
    assert "NVDA" in disk


@pytest.mark.parametrize("form", ["comma", "dquote", "paren", "key=", "colon-label"])
def test_w126_forms_redact_in_job_file_and_api_runs(sleepers, home, form):
    value = WRAPS[form].format(KEYS[0])
    sel = {**SEL, "ticker": "NVDA", "symbol": value, "portfolio": {"cash": 1, "positions": [
        {"ticker": value, "quantity": 1, "average_price": 2}]}}
    try:
        record = service.start_validated_run(sel)
    except Exception:  # the start path may refuse the value; nothing on disk may leak then either
        record = None
    blobs = [p.read_text(encoding="utf-8") for p in home.rglob("*.json")]
    if record is not None:
        blobs += [json.dumps(record, ensure_ascii=False), json.dumps(service.api_runs(None), ensure_ascii=False),
                  json.dumps(service.api_runs(record["id"]), ensure_ascii=False)]
    for text in blobs:
        assert not _leaks(text)


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("code", KEEP)
def test_codes_stay_byte_identical(home, name, code):
    payload = _payload(name, code)
    saved = service.save_config(json.loads(json.dumps(payload)))
    disk = json.loads((service.webui_home() / "config.json").read_text(encoding="utf-8"))
    for blob in (saved, disk, service.public_saved_config()):
        assert blob[name] == code and blob["deep"]["a"][0]["b"][name] == code
        assert blob["portfolio"]["positions"][0]["ticker"] == code
    assert redact({"runs": [{"selections": payload}]})["runs"][0]["selections"] == payload


@pytest.mark.parametrize("value", ["ghp_abcd_efgh_ijkl_mnop", "sk-proj-abcd-efgh-ijkl-mnop", '"ghp_abcd_efgh_ijkl_mnop",',
                                   "(sk-proj-abcd-efgh-ijkl-mnop)", "ghp/abcd/efgh/ijkl/mnop", "ghp\u00a0abcd\u00a0efgh\u00a0ijkl\u00a0mnop",
                                   "xai\u20101234\u20105678\u20109012\u20103456", "ＳＫ－ＰＲＯＪ－" + BODY, "sk-proj-\u200b" + BODY,
                                   "sk-proj-" + BODY + "\U000e0001", "key=ｓｋ－ｐｒｏｊ－" + BODY])
def test_boundaries_and_w125_variants_still_redact(home, value):
    out = redact({"symbol": value})["symbol"]
    assert "[redacted-key]" in out and "mnop" not in out and "3456" not in out and "L1m2" not in out


def test_notes_unchanged():
    note = "hold XAI-USD, key=sk-abc"
    assert redact({"notes": note})["notes"] == note


# ------------------------------------------------------------------ W127 / W128 (D1d-r7)
GLUE = {"NVDA/sk-proj": "NVDA/sk-proj-" + BODY, "NVDA/ghp": "NVDA/ghp_" + BODY, "NVDA/sk-proj_": "NVDA/sk-proj_" + BODY,
        "NVDA/ghp19": "NVDA/ghp_abcd_efgh_ijkl_mnop", "NVDA/sk-proj22": "NVDA/sk-proj-abcd-efgh-ijkl-mnop",
        "NVDAsk-proj": "NVDAsk-proj-" + BODY, "NVDAghp": "NVDAghp_" + BODY, "NVDA.sk": "NVDA.sk-proj-" + BODY,
        "NVDA\u2010ghp": "NVDA\u2010ghp_" + BODY, "NVDA nbsp": "NVDA\u00a0sk-proj-" + BODY,
        "BRK/B/sk": "BRK/B/sk-proj/" + BODY}
GLUE_FORMS = {f"{g} {w}": t.format(v) for g, v in GLUE.items() for w, t in WRAPS.items()} | GLUE


def _no_tail(text):
    return not _leaks(text) and "mnop" not in text


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("form", sorted(GLUE_FORMS))
def test_w127_glued_prefix_forms_redact_everywhere(home, name, form):
    value = GLUE_FORMS[form]
    service.save_config(_payload(name, value))
    disk = (service.webui_home() / "config.json").read_text(encoding="utf-8")
    for text in (disk, json.dumps(service.public_saved_config(), ensure_ascii=False),
                 json.dumps(redact({"runs": [{"selections": _payload(name, value)}]}), ensure_ascii=False)):
        assert _no_tail(text), text
        assert "[redacted-key]" in text
    assert "NVDA" in disk


@pytest.mark.parametrize("form", ["NVDA/sk-proj", "NVDAghp", "NVDA/ghp19"])
def test_w127_forms_redact_in_job_file_and_api_runs(sleepers, home, form):
    value = "(" + GLUE[form] + "),"
    sel = {**SEL, "ticker": "NVDA", "symbol": value, "portfolio": {"cash": 1, "positions": [
        {"ticker": value, "quantity": 1, "average_price": 2}]}}
    try:
        record = service.start_validated_run(sel)
    except Exception:
        record = None
    blobs = [p.read_text(encoding="utf-8") for p in home.rglob("*.json")]
    if record is not None:
        blobs += [json.dumps(record, ensure_ascii=False), json.dumps(service.api_runs(None), ensure_ascii=False),
                  json.dumps(service.api_runs(record["id"]), ensure_ascii=False)]
    for text in blobs:
        assert _no_tail(text)


@pytest.mark.parametrize(("value", "expected"), [
    ("NVDA/sk-proj-" + BODY, "NVDA/[redacted-key]"), ("NVDAghp_" + BODY, "NVDA[redacted-key]"),
    ('("NVDA/ghp_abcd_efgh_ijkl_mnop"),', '("NVDA/[redacted-key]"),'), ("key=NVDA/sk-proj_" + BODY, "key=NVDA/[redacted-key]"),
    ("sk-proj-" + BODY + ",", "[redacted-key],")])
def test_only_the_key_is_replaced(value, expected):
    assert _scan_code(value) == expected


@pytest.mark.parametrize("value", ["a:" * 2000, "sk:" * 2000, "sk" * 5000, "a/" * 5000, "NVDA:" * 2000 + "BRK.B"])
def test_w128_colon_scan_is_fast(value):
    start = time.perf_counter()
    out = scrub_config({"ticker": value, "symbols": [value, value]})
    assert time.perf_counter() - start < 1.0
    assert out["ticker"] == value or not value.startswith(("a", "NVDA"))


def test_w128_colon_scan_scales_linearly():
    def cost(n):
        start = time.perf_counter()
        scrub_config({"ticker": "a:" * n})
        return time.perf_counter() - start
    cost(100)
    small, big = cost(2000), cost(20000)
    assert big < 1.0 and big < max(small, 1e-3) * 40


# ------------------------------------------------------------------ B1: W129 + provider keys
DASHES = ["\u2012", "\u2013", "\u2014", "\u2015", "\u2212", "+", "~", "\u00b7", "\u30fb", "\uff65"]
AIZA = "AIza" + "SyA1b2C3d4E5f6G7h8J9k0L1m2N3o4P5q6x"
AKIA = "AKIA" + "Q2W3E4R5T6Y7U8I9"
HEX40 = "0123456789abcdef" * 2 + "01234567"
HEX64 = "0123456789ABCDEF" * 4


@pytest.mark.parametrize("dash", DASHES)
@pytest.mark.parametrize("name", ["ticker", "notes"])
def test_b1_dash_like_separators_redact(home, dash, name):
    for value in ("sk" + dash + "proj" + dash + BODY, "ghp" + dash + BODY, "NVDA/sk" + dash + BODY):
        saved = json.dumps(service.save_config({"portfolio": {name: value}, name: value}), ensure_ascii=False)
        assert not _leaks(saved) and "[redacted-key]" in saved, value


@pytest.mark.parametrize("dash", DASHES)
def test_b1_dash_like_separators_keep_short_codes(home, dash):
    for code in ("XAI" + dash + "USD", "SK" + dash + "HYNIX", "sk" + dash + "abcd" + dash + "efgh" + dash + "ijkl" + dash + "mnop"):
        assert service.save_config({"ticker": code})["ticker"] == code


@pytest.mark.parametrize("secret", [AIZA, AKIA, HEX40, HEX64, "ASIA" + "Q2W3E4R5T6Y7U8I9"])
def test_b1_provider_keys_redact_everywhere(home, secret):
    from tradingagents.redaction import redact_secrets, redact_text
    service.save_config({"ticker": secret, "portfolio": {"note": "key " + secret}})
    disk = (service.webui_home() / "config.json").read_text(encoding="utf-8")
    for text in (disk, redact_secrets("n " + secret), redact_text("e " + secret),
                 json.dumps(redact({"runs": [{"selections": {"symbol": secret, "note": secret}}]}))):
        assert secret not in text


@pytest.mark.parametrize("plain", ["AIzawesome-plan", "AKIA-ish", "deadbeef", "2026-10-30", "abcdef" * 6, "1234567890" * 4])
def test_b1_short_or_single_kind_hex_like_words_stay(plain):
    from tradingagents.redaction import redact_secrets
    assert redact_secrets("note " + plain) == "note " + plain


# ------------------------------------------------------------------ B2: W132
@pytest.mark.parametrize("code", ["ARTEMIS-GLOBAL-FUND-2026", "PARTNERRE-PREFERRED-2026", "ASKARI-BANK-LIMITED-PK",
                                  "SPORTSMAN-HOLDINGS-INC", "MARKETAXESS-HOLDINGS", "DESKTOP-METAL-CLASS-A",
                                  "UPSTART-HOLDINGS-INC", "SMARTSHEET-INC-CLASS-A", "SESSIONM-HOLDINGS-INC",
                                  "BOOKINGHOLDINGS2026Q4", "NVDA/BRK.B", "(ARTEMIS-GLOBAL-FUND-2026),"])
@pytest.mark.parametrize("name", ["ticker", "symbols"])
def test_b2_words_containing_a_prefix_stay(home, name, code):
    assert service.save_config({name: code})[name] == code
    assert redact({"runs": [{"selections": {name: code}}]})["runs"][0]["selections"][name] == code


@pytest.mark.parametrize("value", ["NVDASK-PROJ-" + BODY, "NVDAsk-proj-" + BODY, "NVDAghp_" + BODY, "2026sk-proj-" + BODY])
def test_b2_glued_keys_still_redact(value):
    out = _scan_code(value)
    assert out.endswith("[redacted-key]") and not _leaks(out)


# ------------------------------------------------------------------ B3: W131 / W123
@pytest.mark.parametrize(("value", "expected"), [("NVDA/sk-proj-" + BODY + ".", "NVDA/[redacted-key]."),
                                                  ("sk-proj-" + BODY + "..", "[redacted-key].."),
                                                  ("sk-proj-" + BODY + ":", "[redacted-key]:")])
def test_b3_w131_trailing_separator_stays(value, expected):
    assert _scan_code(value) == expected


@pytest.mark.parametrize("value", ["sk-[redacted-key]Ab3dEf9hIjKlMnOp", "[redacted]Ab3dEf9hIjKlMnOp",
                                   "x [redacted-token]Ab3dEf9hIjKlMnOp"])
def test_b3_w123_tail_after_a_marker_is_dropped(home, value):
    from tradingagents.redaction import redact_secrets, redact_text
    for out in (_scan_code(value), redact_secrets(value), redact_text(value), service.save_config({"note": value})["note"]):
        assert "Ab3dEf9hIjKlMnOp" not in out
