"""RUN-002 QA round 2 fixes for the console: R2-MF1, W28 and W30.

R2-MF1  a lone surrogate in a failure file never breaks /api/runs or empties
        the run record; the record (runs/<id>.json since RUN-004) is replaced atomically
W28     failure text is cut before it is redacted, redaction is linear, and
        the failure file is read only up to a bounded size
W30     QA's surviving mutants of the failure schema and the redaction rules
"""

from __future__ import annotations

import json
import os
import time

import pytest

from tests import test_run002_r2_console as r2
from tradingagents import redaction
from tradingagents.graph import checkpointer
from tradingagents.redaction import redact_text
from webui import run_failure, service

pytestmark = pytest.mark.unit

console = r2.console                 # the round-2 fixtures
http_console = r2.http_console
failed_run = r2.failed_run

GENERIC = "分析进程已退出，但没有写出结果。进程退出不等于分析完成。"
SURROGATE = '{"kind": "timeout", "message": "x \\udfff y"}'


def _current_run_file(console, run_id="run-bad"):
    # RUN-004: one record per run under runs/<id>.json (was current-run.json).
    return console / "webui" / "runs" / f"{run_id}.json"


# ------------------------------------------------------------------ R2-MF1

def test_a_lone_surrogate_in_the_failure_file_never_breaks_api_runs(console, http_console):
    failed_run(console, SURROGATE)
    for _ in range(3):
        status, body = http_console("/api/runs")
        assert status == 200, body[:300]
        run = json.loads(body)["run"]
        assert run["status"] == "failed" and run["detail"]
    stored = json.loads(_current_run_file(console).read_bytes().decode("utf-8"))
    assert stored["status"] == "failed" and stored["id"] == "run-bad"
    assert stored["failure"]["message"] == "x ? y"


@pytest.mark.parametrize("field", ["role", "role_zh", "error_type", "message", "provider", "provider_label",
                                   "model", "summary", "run_id"])
def test_a_lone_surrogate_in_any_text_field_is_made_safe(console, field):
    data = {"kind": "timeout", field: "a \udfff b \ud800"}
    clean = run_failure.clean_failure(data)
    assert clean[field] == "a ? b ?"
    json.dumps(clean, ensure_ascii=False).encode("utf-8")
    record = service.refresh_run_process(failed_run(console, json.dumps(data)))
    assert record["status"] == "failed"
    assert json.loads(_current_run_file(console).read_text(encoding="utf-8"))["status"] == "failed"


def test_safe_text_keeps_good_text_and_replaces_surrogates():
    assert run_failure.safe_text("交易员 ok") == "交易员 ok"
    assert run_failure.safe_text("\udfff") == "?"


def test_a_failure_build_from_an_exception_with_a_surrogate_is_safe():
    failure = run_failure.build_failure(RuntimeError("bad \udfff text"))
    json.dumps(failure, ensure_ascii=False).encode("utf-8")
    assert "\udfff" not in failure["message"]
    run_failure.redacted_traceback(RuntimeError("bad \udfff text")).encode("utf-8")


def test_a_record_that_cannot_be_encoded_leaves_the_old_file_whole(console):
    record = failed_run(console, SURROGATE)
    before = _current_run_file(console).read_bytes()
    with pytest.raises(UnicodeEncodeError):
        service.JOBS.write_run({**record, "detail": "\udfff"})
    assert _current_run_file(console).read_bytes() == before
    assert [p.name for p in _current_run_file(console).parent.iterdir() if p.name.endswith(".tmp")] == []


def test_a_write_that_fails_midway_leaves_the_old_file_whole(console, monkeypatch):
    record = failed_run(console, SURROGATE)
    before = _current_run_file(console).read_bytes()

    def broken(fd):
        raise OSError("disk full")

    monkeypatch.setattr(service.os, "fsync", broken)
    with pytest.raises(OSError):
        service.JOBS.write_run({**record, "detail": "new"})
    assert _current_run_file(console).read_bytes() == before
    assert [p.name for p in _current_run_file(console).parent.iterdir() if p.name.endswith(".tmp")] == []


def test_the_run_record_is_synced_before_it_replaces_the_old_one(console, monkeypatch):
    record = failed_run(console, SURROGATE)
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(service.os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(service.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    service.JOBS.write_run({**record, "detail": "new"})
    assert order == ["fsync", "replace"]
    assert json.loads(_current_run_file(console).read_text(encoding="utf-8"))["detail"] == "new"


def test_a_failure_that_cannot_be_stored_still_leaves_a_failed_record(console, monkeypatch):
    record = failed_run(console, json.dumps({"kind": "timeout"}))

    def poisoned(record, failure):
        record["failure"] = {"message": "\udfff"}
        record["detail"] = "\udfff"
        return record

    monkeypatch.setattr(service, "attach_failure", poisoned)
    result = service.refresh_run_process(record)
    assert result["status"] == "failed" and result["detail"] == GENERIC and "failure" not in result
    stored = json.loads(_current_run_file(console).read_text(encoding="utf-8"))
    assert stored["status"] == "failed" and stored["detail"] == GENERIC and "failure" not in stored


def test_api_runs_answers_even_when_the_record_cannot_be_stored_at_all(console, http_console, monkeypatch):
    failed_run(console, SURROGATE)
    before = _current_run_file(console).read_bytes()

    def broken(self, record):
        raise OSError("read-only file system")

    monkeypatch.setattr(type(service.JOBS), "write_run", broken)
    status, body = http_console("/api/runs")
    assert status == 200
    assert json.loads(body)["run"]["status"] == "failed"
    assert _current_run_file(console).read_bytes() == before


def test_a_surrogate_already_in_current_run_json_is_answered(console, http_console):
    failed_run(console, SURROGATE)
    path = _current_run_file(console)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["status"] = "finished"
    path.write_text(json.dumps(record).replace('"run-bad"', '"run-\\udfff"', 1), encoding="utf-8")
    status, body = http_console("/api/runs")
    assert status == 200
    assert json.loads(body)["run"]["id"] == "run-?"


def test_an_in_process_run_error_is_cut_and_made_safe(monkeypatch):
    text = run_failure.safe_text("boom \udfff " + "word " * 5000)
    assert "\udfff" not in text
    assert len(redact_text(text, run_failure.ERROR_LIMIT)) <= run_failure.ERROR_LIMIT


# ------------------------------------------------------------------ W28

def _timed(fn, *args, **kwargs):
    started = time.monotonic()
    value = fn(*args, **kwargs)
    return value, time.monotonic() - started


@pytest.mark.parametrize("text", ["a." * 50_000, "a" * 100_000, "x-" * 50_000, "/a" * 50_000,
                                  "%" * 100_000, "a@" * 50_000, ("/x y" * 25_000)])
def test_redacting_100kb_is_fast_without_a_limit(text):
    _, elapsed = _timed(redact_text, text)
    assert elapsed < 0.5


def test_a_100kb_failure_message_does_not_stall_api_runs(console, http_console):
    failed_run(console, json.dumps({"kind": "timeout", "message": "a." * 50_000, "summary": "b." * 50_000}))
    started = time.monotonic()
    status, body = http_console("/api/runs")
    assert status == 200 and time.monotonic() - started < 1.0
    failure = json.loads(body)["run"]["failure"]
    assert len(failure["message"]) <= 600 and len(failure["summary"]) <= 1000


def test_a_huge_failure_file_is_not_read(console, http_console):
    failed_run(console, "")
    path = run_failure.failure_path(console / "webui" / "logs" / "run-bad.json")
    with path.open("w", encoding="utf-8") as handle:
        handle.write('{"kind": "timeout", "message": "')
        handle.write("x" * (50 * 1024 * 1024))
        handle.write('"}')
    started = time.monotonic()
    status, body = http_console("/api/runs")
    assert status == 200 and time.monotonic() - started < 1.0
    assert json.loads(body)["run"]["detail"] == GENERIC


def test_a_failure_file_at_the_size_bound_is_still_read(tmp_path):
    path = tmp_path / "f.json"
    head = '{"kind": "timeout", "message": "'
    path.write_text(head + "x" * (run_failure.MAX_FAILURE_BYTES - len(head) - 2) + '"}', encoding="utf-8")
    assert path.stat().st_size == run_failure.MAX_FAILURE_BYTES
    assert run_failure.read_failure(path)["kind"] == "timeout"
    path.write_text(head + "x" * (run_failure.MAX_FAILURE_BYTES - len(head) - 1) + '"}', encoding="utf-8")
    assert run_failure.read_failure(path) is None


def test_the_failure_file_is_read_with_a_bounded_size(tmp_path, monkeypatch):
    path = tmp_path / "f.json"
    path.write_text('{"kind": "timeout"}', encoding="utf-8")
    asked = []
    real_open = type(path).open

    def spy_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        real_read = handle.read

        def read(size=-1):
            asked.append(size)
            return real_read(size)

        handle.read = read
        return handle

    monkeypatch.setattr(type(path), "open", spy_open)
    assert run_failure.read_failure(path)["kind"] == "timeout"
    assert asked and all(0 < size <= run_failure.MAX_FAILURE_BYTES + 1 for size in asked)


def test_text_is_cut_before_the_rules_run(monkeypatch):
    seen = []
    real = redaction._FILE_URL

    class Spy:
        def sub(self, repl, value):
            seen.append(len(value))
            return real.sub(repl, value)

    monkeypatch.setattr(redaction, "_FILE_URL", Spy())
    redact_text("word " * 100_000, 600)
    assert seen and max(seen) <= 600 * redaction._PRE_CUT


def test_a_key_the_pre_cut_runs_through_is_dropped_not_kept_as_a_fragment():
    limit = 100
    long_path = "/" + "/".join(["d" * 8] * 20) + "/f.txt"         # 186 characters, redacts to 14
    head = f"{long_path} {long_path} "
    pad = "z" * (limit * redaction._PRE_CUT - 20 - len(head) - 1) + " "
    text = head + pad + "AIzaSyFRAGMENT0123456789abcdefghijklmnopqrstuvwxyz rest"
    assert len(head + pad) + 20 == limit * redaction._PRE_CUT           # the cut lands inside the key
    out = redact_text(text, limit)
    assert "AIza" not in out and "FRAGMENT" not in out
    assert len(out) <= limit


def test_the_tail_cut_drops_the_token_it_runs_through_too():
    limit = 60
    long_path = "/" + "/".join(["d" * 8] * 11) + "/f.txt"         # 105 characters, redacts to 12
    tail = f" {long_path}  {long_path}       "
    key = "AIzaSy" + "k" * 24 + "TAILPART0123456789xy"            # the last 20 are kept by the cut
    assert len(tail) + 20 == limit * redaction._PRE_CUT
    out = redact_text("start " + key + tail, limit, tail=True)
    assert "TAILPART" not in out and "kkk" not in out
    assert out.endswith("[path]/f.txt       ") and len(out) <= limit


def test_the_traceback_keeps_its_end_within_its_limit():
    try:
        raise ValueError("the last line " + "x " * 50_000 + "END-MARK")
    except ValueError as exc:
        text, elapsed = _timed(run_failure.redacted_traceback, exc)
    assert len(text) <= run_failure.TRACEBACK_LIMIT and "END-MARK" in text and elapsed < 0.5


def test_a_built_failure_message_is_bounded():
    failure, elapsed = _timed(run_failure.build_failure, RuntimeError("a." * 50_000))
    assert len(failure["message"]) <= 600 and elapsed < 0.5


def test_a_checkpoint_error_is_bounded():
    text, elapsed = _timed(checkpointer.redact_error, RuntimeError("a." * 50_000))
    assert len(text) <= checkpointer.ERROR_LIMIT and elapsed < 0.5


# ------------------------------------------------------------------ W30 the failure schema

@pytest.mark.parametrize("field, limit", sorted(run_failure._TEXT_FIELDS.items()))
def test_every_text_field_is_cut_to_its_length(field, limit):
    clean = run_failure.clean_failure({"kind": "timeout", field: "word " * 2000})
    if field in ("kind", "kind_zh"):
        return
    assert 0 < len(clean[field]) <= limit
    assert limit - len(clean[field]) <= 5


@pytest.mark.parametrize("value, kept", [(True, True), (False, False), ("yes", False), (1, False),
                                         ("true", False), (None, False)])
def test_budget_exhausted_is_only_ever_a_real_true(value, kept):
    assert run_failure.clean_failure({"budget_exhausted": value})["budget_exhausted"] is kept


@pytest.mark.parametrize("value, kept", [(0, True), (1000, True), (1001, False), (-1, False), (10**9, False)])
def test_counts_stay_in_their_range(value, kept):
    clean = run_failure.clean_failure({"attempts": value, "max_attempts": value, "retries": value})
    for key in ("attempts", "max_attempts", "retries"):
        assert (clean.get(key) == value) is kept


@pytest.mark.parametrize("data", [[1, 2], "failed", 42, None, 1.5, True])
def test_a_failure_that_is_not_a_dict_is_none(data):
    assert run_failure.clean_failure(data) is None


# ------------------------------------------------------------------ W30 the redaction rules

def test_the_home_directory_becomes_a_tilde(monkeypatch):
    monkeypatch.setenv("HOME", "/opt/qa-odd-home")
    assert redact_text("open /opt/qa-odd-home/notes.txt now") == "open ~/notes.txt now"


@pytest.mark.parametrize("home", ["/", "~"])
def test_a_root_or_unknown_home_is_not_replaced(monkeypatch, home):
    monkeypatch.setattr(redaction.os.path, "expanduser", lambda path: home)
    assert redact_text("ratio 3/4 at ~ x") == "ratio 3/4 at ~ x"


def test_a_long_key_without_a_known_prefix_is_redacted():
    key = "AIzaSy" + "D0123456789abcdefghijklmnopqrstu"          # 38 characters
    assert redact_text(f"key {key} end") == "key [redacted] end"
    assert redact_text("ok " + "A1b2C3d4" * 3 + "x" * 7 + " end") == "ok " + "A1b2C3d4" * 3 + "x" * 7 + " end"
    assert redact_text("ok " + "A1b2C3d4" * 4 + " end") == "ok [redacted] end"


def test_an_email_is_still_redacted_after_the_linear_rule():
    assert redact_text("mail eric.r3+qa@example.com now") == "mail [redacted-email] now"
    assert redact_text("a" * 64 + "@example.com") == "[redacted-email]"
    assert "@example.com" not in redact_text("x" * 70 + "@example.com")


def test_one_huge_token_is_cut_but_not_dropped():
    out = redact_text("a." * 50_000, 600)
    assert out == ("a." * 300)
    assert redact_text("Z9" * 50_000, 600) == "[redacted]"
