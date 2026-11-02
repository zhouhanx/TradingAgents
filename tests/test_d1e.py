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
