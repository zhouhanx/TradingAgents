"""D1d / W124: long real codes under code keys stay byte-identical everywhere;
key-looking values (sk-proj + long random run, Bearer, JWT, ghp_) still redact."""
from __future__ import annotations

import json

import pytest

from tests.test_run003b import (
    SEL,
    home as home,  # noqa: F401  (fixture)
    sleepers as sleepers,  # noqa: F401  (fixture)
)
from webui import service
from webui.redaction import redact

pytestmark = pytest.mark.unit
LONG_CODES = ["XAI-USD-PERP-2026", "RT-PCR-INDEX-2026", "pk-abcdefghijkl", "SK-HYNIX"]
SECRETS = ["sk-proj-" + "A1b2C3d4E5f6G7h8J9k0L1m2", "sk-proj-" + "q8Z2x7W4v6U1t3S5r9P0", "Bearer " + "A1b2C3d4E5f6G7h8J9k0",
           "Basic " + "QWxhZGRpbjpvcGVuU2VzYW1l", "XAI-" + "a1B2c3D4e5F6g7H8", "ghp_" + "A1b2C3d4E5f6G7h8J9",
           "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"]
CODE_NAMES = ["ticker", "tickers", "symbol", "symbols", "code", "codes", "instrument", "Ticker", "SYMBOL",
              "ＴＩＣＫＥＲ", "ｓｙｍｂｏｌ", "tick\u200ber", "co\u200bde"]


def _payload(name, value):
    return {name: value, "list": {name: [value, "NVDA"]}, "deep": {"a": [{"b": {name: value}}]},
            "portfolio": {"cash": 1, "positions": [{"ticker": value, "quantity": 1, "average_price": 2,
                                                    "meta": {name: value}}]}}


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("code", LONG_CODES)
def test_long_codes_stay_in_config_and_get(home, name, code):
    payload = _payload(name, code)
    saved = service.save_config(json.loads(json.dumps(payload)))
    disk = json.loads((service.webui_home() / "config.json").read_text(encoding="utf-8"))
    get = service.public_saved_config()          # what GET /api/config returns as "saved"
    for blob in (saved, disk, get):
        assert blob[name] == code and blob["list"][name] == [code, "NVDA"]
        assert blob["deep"]["a"][0]["b"][name] == code
        assert blob["portfolio"]["positions"][0]["ticker"] == code
        assert blob["portfolio"]["positions"][0]["meta"][name] == code
    assert "[redacted" not in json.dumps(disk)
    out = redact({"runs": [{"selections": payload}]})   # run record / /api/runs path
    assert out["runs"][0]["selections"] == payload


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("secret", SECRETS)
def test_key_looking_values_redact_under_code_keys(home, name, secret):
    body = secret.split()[-1]
    service.save_config(_payload(name, secret))
    disk = (service.webui_home() / "config.json").read_text(encoding="utf-8")
    for text in (disk, json.dumps(service.public_saved_config(), ensure_ascii=False),
                 json.dumps(redact({"selections": _payload(name, secret)}), ensure_ascii=False)):
        assert body not in text
    assert "NVDA" in disk


@pytest.mark.parametrize("code", LONG_CODES)
def test_long_codes_stay_in_job_file_run_record_and_api_runs(sleepers, home, code):
    """Real start path with a sleeping stand-in worker (no model)."""
    sel = {**SEL, "ticker": code, "portfolio": {"cash": 1, "positions": [
        {"ticker": code, "quantity": 1, "average_price": 2}]}}
    record = service.start_validated_run(sel)
    found = [json.loads(p.read_text(encoding="utf-8")) for p in home.rglob("*.json")]
    job = next(d for d in found if isinstance(d, dict) and d.get("selections", {}).get("ticker") == code)
    assert job["selections"]["portfolio"]["positions"][0]["ticker"] == code
    assert record["selections"]["ticker"] == code
    assert record["selections"]["portfolio"]["positions"][0]["ticker"] == code
    api = json.dumps(service.api_runs(None), ensure_ascii=False) + json.dumps(service.api_runs(record["id"]))
    assert code in api and "[redacted" not in api



# ------------------------------------------------------------------ W124 r2
PLAIN_SECRETS = ["sk-proj-abcdefghijklmnop", "sk-proj-" + "q" * 30, "sk-1234567890123456", "XAI-ABCDEFGHIJKLMNOP",
                 "xai-abcdefghijklmnopqrstuv", "ghp_abcdefghijklmnopqr", "pk-0000111122223333", "rt-abcdefghijklmnop",
                 "sess-ABCDEFGHIJKLMNOPQRST", "sk_abcdefghijklmnop"]
COMMON_CODES = ["XAI-USD", "SK-HYNIX", "BRK-B", "600519.SS", "XAI-USD-PER6", "600519SH0600519", "0700.HK", "GC=F",
                "sk-proj-abcdefghijklmno", "sk-123456789012345", "BRK.B", "SK-HYNIX-PREF-2026", "AB-CD.EF_GH",
                "US0378331005XNAS", "600519SH0600519SSE"]


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("secret", PLAIN_SECRETS)
def test_single_case_and_digit_keys_redact_under_code_keys(home, name, secret):
    service.save_config(_payload(name, secret))
    disk = (service.webui_home() / "config.json").read_text(encoding="utf-8")
    for text in (disk, json.dumps(service.public_saved_config(), ensure_ascii=False),
                 json.dumps(redact({"selections": _payload(name, secret)}), ensure_ascii=False)):
        assert secret not in text
    assert "NVDA" in disk


@pytest.mark.parametrize(("prefix", "redacts"), [("sk-", True), ("sk-proj-", True), ("XAI-", True), ("pk-", True)])
@pytest.mark.parametrize("length", [15, 16, 17])
def test_the_body_length_boundary_is_16(home, prefix, redacts, length):
    for body in ("a" * length, "7" * length, "Q" * length):
        value = prefix + body
        out = service.save_config({"ticker": value, "codes": [value]})
        kept = out["ticker"] == value and out["codes"] == [value]
        assert kept is (length < 16), value


@pytest.mark.parametrize("code", LONG_CODES + COMMON_CODES)
def test_common_codes_stay_everywhere(home, code):
    payload = dict.fromkeys(CODE_NAMES, code) | {"portfolio": {"cash": 1, "positions": [
        {"ticker": code, "quantity": 1, "average_price": 2}]}}
    saved = service.save_config(json.loads(json.dumps(payload)))
    disk = json.loads((service.webui_home() / "config.json").read_text(encoding="utf-8"))
    for blob in (saved, disk, service.public_saved_config(), redact({"x": payload})["x"]):
        assert blob == payload


@pytest.mark.parametrize("code", COMMON_CODES[:6])
def test_common_codes_stay_in_job_file_and_api_runs(sleepers, home, code):
    test_long_codes_stay_in_job_file_run_record_and_api_runs(sleepers, home, code)


# ------------------------------------------------------------------ D1d-r2: W125 + QA matrix
Z, TAG = "\u200b", "\U000e0001"
MATRIX_REDACT = {
    "sk-proj all-lower 20": "sk-proj-" + "abcdefghij" * 2,
    "sk-proj all-lower 32": "sk-proj-" + "abcdefgh" * 4,
    "sk-proj all-digit": "sk-proj-" + "1234567890" * 2,
    "SK-PROJ all-upper": "SK-PROJ-" + "ABCDEFGHIJ" * 2,
    "sk 48 lower": "sk-" + "abcdefgh" * 6,
    "xai 40 lower": "xai-" + "abcdefgh" * 5,
    "xai 40 digit": "xai-" + "12345678" * 5,
    "ghp 36 lower": "ghp_" + "abcdef" * 6,
    "Bearer 32 lower": "Bearer " + "abcdefgh" * 4,
    "Basic 24 lower ==": "Basic " + "abcdefgh" * 3 + "==",
    "sk-proj underscore": "sk-proj-" + "A1b2C3d4E5" + "_" + "F6g7H8j9K0",
    "sk-proj hyphen": "sk-proj-" + "A1b2C3d4E5" + "-" + "F6g7H8j9K0",
    "sk-ant-api03": "sk-ant-api03-" + "Ab1Cd2Ef3G" * 4,
    "sk 16 digits (documented: redacted under code keys)": "sk-1234567890123456",
    "W125 full-width": "ｓｋ－ｐｒｏｊ－" + "ＡｂＣｄＥｆＧｈＩｊＫｌＭｎＯｐ",
    "W125 ZWSP": "sk-proj-A1b2" + Z + "C3d4E5f6G7h8J9k0",
    "W125 U+E0001": "sk-proj-A1b2" + TAG + "C3d4E5f6G7h8J9k0",
    "accepted cost: sk-hynix-longer-symbol": "sk-hynix-longer-symbol",
    "dot tail": "sk-proj-abcdefghijklmnop.x",
    "W125 ZWSP in prefix": "s" + Z + "k-proj-A1b2C3d4E5f6G7h8J9k0",
}
MATRIX_KEEP = LONG_CODES + ["XAI-USD-PER6", "600519SH0600519", "XAI-USD", "SK-HYNIX", "BRK-B", "BRK.B", "600519.SS"]


def _plain_parts(secret):
    import unicodedata
    folded = unicodedata.normalize("NFKC", secret).replace(Z, "").replace(TAG, "")
    return {secret, folded, folded.split()[-1]}


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("row", sorted(MATRIX_REDACT))
def test_qa_matrix_redacts_under_code_keys(home, name, row):
    secret = MATRIX_REDACT[row]
    service.save_config(_payload(name, secret))
    disk = (service.webui_home() / "config.json").read_text(encoding="utf-8")
    for text in (disk, json.dumps(service.public_saved_config(), ensure_ascii=False),
                 json.dumps(redact({"selections": _payload(name, secret)}), ensure_ascii=False)):
        assert not any(part in text for part in _plain_parts(secret)), row
    assert "NVDA" in disk


def test_sk_with_16_digits_is_redacted_under_code_keys(home):
    saved = service.save_config({"ticker": "sk-1234567890123456", "symbol": "sk-123456789012345"})
    assert saved["ticker"] == "[redacted-key]"
    assert saved["symbol"] == "sk-123456789012345"


@pytest.mark.parametrize("name", CODE_NAMES)
@pytest.mark.parametrize("code", MATRIX_KEEP)
def test_qa_matrix_keeps_real_codes_under_code_keys(home, name, code):
    payload = _payload(name, code)
    saved = service.save_config(json.loads(json.dumps(payload)))
    disk = json.loads((service.webui_home() / "config.json").read_text(encoding="utf-8"))
    for blob in (saved, disk, service.public_saved_config(), redact({"x": payload})["x"]):
        assert blob == payload


def test_notes_keep_their_own_rules():
    from webui.redaction import scan_value
    assert scan_value("hold XAI-USD and SK-HYNIX") == "hold XAI-USD and SK-HYNIX"
    assert "abcdefghijklmn" not in scan_value("note sk-abcdefghijklmn")   # notes still redact at 12


def test_sk_hynix_longer_symbol_is_redacted_as_accepted_cost(home):
    saved = service.save_config({"ticker": "sk-hynix-longer-symbol", "symbol": "SK-HYNIX", "code": "600519.SS"})
    assert (saved["ticker"], saved["symbol"], saved["code"]) == ("[redacted-key]", "SK-HYNIX", "600519.SS")


@pytest.mark.parametrize("value", ["AB-CD-EF-GH", "1234-5678-9012-3456", "XAI-USD-PERP-2026", "sk-abcd-efgh-ijk",
                                   "BRK.B", "SK-HYNIX", "600519.SS",
                                   "sk-abcd-efgh-ijkl-mnop",         # 18: kept by rule 1 (sk + 16 otherwise)
                                   "xai-1234-5678-9012-345",         # 18
                                   # PM final: 5-letter pieces fail rule 1, no known prefix -> kept
                                   "ABCDE-FGHIJ-KLMNO-PQRST"])
def test_short_piece_codes_stay(home, value):
    assert service.save_config({"ticker": value})["ticker"] == value


@pytest.mark.parametrize("value", ["sk-proj-" + "-".join(["abcd"] * 7), "ghp_" + "_".join(["A1b2"] * 8),
                                   "sk-proj-abcde-fghij-klmno-pqrst",              # 5-letter pieces
                                   "sk-proj-abcd-efgh-ijkl-mnop",                  # 22 -> rule 2
                                   "ghp_abcd_efgh_ijkl_mnop", "ＧＨＰ＿abcd_efgh_ijkl_mnop",  # 19 -> rule 2
                                   "sk-abcd-efgh-ijkl-mnop-q", "xai-1234-5678-9012-3456",     # 19
                                   "SK-PROJ-ABCD-EFGH-IJKL-MNOP", "sk-abcd-efgh-ijkl-mnop-qr",  # 20
                                   "sk-abcde-fghij-klmno-p",                       # 18, but 5-letter pieces
                                   "RT-PCR-INDEX-2026-SUPPLEMENTAL"])
def test_known_prefix_short_pieces_and_long_pieces_redact(home, value):
    saved = service.save_config({"ticker": value, "codes": [value]})
    assert saved["ticker"] == "[redacted-key]" and saved["codes"] == ["[redacted-key]"]
