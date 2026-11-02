"""RUN-002 QA round 1: failure files, checkpoint database and redaction.

MF3  a failure file with fields of the wrong type never breaks /api/runs
W18  the checkpoint database never stores a raw error and is private (0600)
W19  redaction of paths with spaces, file:// URLs, home user names, cookies
W22  short xai- keys are redacted
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import sqlite3
import stat
import subprocess
import threading
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from tradingagents.graph import checkpointer
from tradingagents.redaction import redact_text
from webui import run_failure, server, service

_CONNECT, _GETADDRINFO = socket.socket.connect, socket.getaddrinfo

CANARIES = {
    "sk": "sk-R2canary0123456789abcdef",
    "xai": "xai-R2canaryABCDEF0123456789",
    "xai_short": "xai-R2short9",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJyMmNhbmFyeSJ9.R2canarySIGxyz",
    "bearer": "R2bearerTok0123456789",
    "user": "zhouhanR2canary",
    "email": "eric.r2canary@example.com",
    "cookie": "R2cookieValue0042",
}


def leaky_text() -> str:
    c = CANARIES
    return (f"refused {c['sk']} {c['xai']} {c['xai_short']} {c['jwt']} Authorization: Bearer {c['bearer']} "
            f"reading /Users/{c['user']}/Library/auth.json for {c['email']} sessionid={c['cookie']}")


# ------------------------------------------------------------------ MF3

@pytest.fixture
def console(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    monkeypatch.setattr("cli.prefs._PREFS_PATH", tmp_path / "cli_prefs.json")
    return tmp_path


def dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def failed_run(console, failure_text: str) -> dict:
    """A running record whose process is gone and whose failure file holds ``failure_text``."""
    logs = console / "webui" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    selections = {"ticker": "NVDA", "analysis_date": "2026-10-01", "asset_type": "stock", "analysts": ["market"]}
    result = logs / "run-bad.json"
    record = service._record_for("run-bad", selections, logs / "run-bad.log", result, kind="analysis")
    record["pid"] = dead_pid()
    service.JOBS.write_run(record)
    run_failure.failure_path(result).write_text(failure_text, encoding="utf-8")
    return record


GENERIC = "分析流程 · "
BAD_FILES = {
    "checkpoint_yes": {"checkpoint": "yes"},
    "checkpoint_list": {"checkpoint": [1, 2]},
    "checkpoint_bad_flags": {"checkpoint": {"enabled": "yes", "saved": 1}},
    "timeout_abc": {"kind": "timeout", "timeout": "abc"},
    "timeout_nan": {"kind": "timeout", "timeout": float("nan")},
    "timeout_inf": {"kind": "timeout", "timeout": float("inf")},
    "timeout_negative": {"kind": "timeout", "timeout": -5},
    "timeout_dict": {"kind": "timeout", "timeout": {"read": 300}},
    "budget_text": {"budget_exhausted": True, "budget": "900s"},
    "budget_exhausted_text": {"budget_exhausted": "no", "budget": 900},
    "retries_text": {"retries": "3"},
    "retries_float": {"retries": 2.5},
    "retries_bool": {"retries": True},
    "retries_negative": {"retries": -1},
    "role_list": {"role": ["Trader"], "role_zh": {"x": 1}},
    "kind_unknown": {"kind": "<script>", "kind_zh": 7},
    "provider_number": {"provider": 5, "provider_label": ["Grok"], "model": None},
    "summary_number": {"summary": 12},
    "message_dict": {"message": {"nested": {"deeper": [1, 2, {"x": None}]}}},
    "everything_wrong": {"role": 1, "role_zh": 2, "kind": 3, "kind_zh": 4, "provider": 5, "provider_label": 6,
                         "model": 7, "timeout": "x", "budget": "y", "attempts": "z", "retries": "w",
                         "budget_exhausted": "maybe", "checkpoint": "yes", "summary": [], "message": {}},
}
NOT_A_RECORD = {
    "list": "[1, 2, 3]",
    "string": '"failed"',
    "null": "null",
    "number": "42",
    "corrupt": "{not json",
    "empty": "",
    "deep": "[" * 100000 + "]" * 100000,
}
NAN_LITERAL = '{"kind": "timeout", "timeout": NaN, "retries": Infinity, "budget": -Infinity}'



def _check_safe(record):
    assert record["status"] == "failed"
    assert isinstance(record["detail"], str) and record["detail"]
    json.dumps(record, ensure_ascii=False, allow_nan=False)
    service.console_status_sentence(record)
    service.run_progress()


@pytest.mark.parametrize("name", sorted(BAD_FILES))
def test_a_failure_field_of_the_wrong_type_falls_back_to_the_generic_words(console, name):
    record = service.refresh_run_process(failed_run(console, json.dumps(BAD_FILES[name])))
    _check_safe(record)
    assert record["detail"].startswith(GENERIC), record["detail"]
    for value in record["failure"].values():
        assert value is None or isinstance(value, (str, int, float, bool, dict))
    if "checkpoint" in record["failure"]:
        assert set(record["failure"]["checkpoint"]) == {"enabled", "saved"}


@pytest.mark.parametrize("name", sorted(NOT_A_RECORD))
def test_a_failure_file_that_is_not_a_record_gives_the_generic_detail(console, name):
    record = service.refresh_run_process(failed_run(console, NOT_A_RECORD[name]))
    _check_safe(record)
    assert record["detail"] == "分析进程已退出，但没有写出结果。进程退出不等于分析完成。"
    assert "failure" not in record


def test_nan_and_infinity_literals_are_dropped(console):
    record = service.refresh_run_process(failed_run(console, NAN_LITERAL))
    _check_safe(record)
    assert record["detail"] == "分析流程 · 调用超时"
    assert set(record["failure"]) == {"kind", "kind_zh", "budget_exhausted", "summary"}
    assert record["failure"]["summary"] == "分析流程 · 调用超时"


def test_good_fields_survive_next_to_bad_ones(console):
    failure = {"role": "Trader", "role_zh": "交易员", "kind": "timeout", "kind_zh": "调用超时",
               "provider": "grok", "provider_label": "Grok 订阅", "model": "grok-4.7",
               "timeout": "abc", "retries": 2, "checkpoint": "yes"}
    record = service.refresh_run_process(failed_run(console, json.dumps(failure)))
    assert record["detail"] == "交易员 · Grok 订阅 grok-4.7 调用超时（已重试 2 次）"
    assert record["failure"]["role"] == "Trader" and "timeout" not in record["failure"]
    assert "checkpoint" not in record["failure"]


def test_a_bad_checkpoint_does_not_replace_the_runs_own(console):
    record = failed_run(console, json.dumps({"checkpoint": "yes"}))
    record["checkpoint"] = {"enabled": True, "saved": None}
    service.JOBS.write_run(record)
    refreshed = service.refresh_run_process(record)
    assert refreshed["checkpoint"] == {"enabled": True, "saved": None}


def test_summary_and_checkpoint_sentence_never_raise_on_raw_dicts():
    for data in BAD_FILES.values():
        assert run_failure.summary(data).startswith(GENERIC)
        run_failure.checkpoint_sentence(data.get("checkpoint"))
    assert run_failure.checkpoint_sentence("yes") is None
    assert run_failure.checkpoint_sentence({"enabled": "yes"}) is None
    assert run_failure.summary({"kind": "<script>", "kind_zh": "<b>x</b>"}) == "分析流程 · 调用失败"


def test_text_fields_from_the_file_are_redacted_again(console):
    failure = {"kind": "other", "message": leaky_text(), "summary": "交易员 · 调用失败：" + leaky_text()}
    record = service.refresh_run_process(failed_run(console, json.dumps(failure)))
    text = json.dumps(record, ensure_ascii=False)
    for canary in CANARIES.values():
        assert canary not in text


@pytest.fixture
def http_console(console, monkeypatch):
    monkeypatch.setattr(socket.socket, "connect", lambda sock, address: (
        _CONNECT if address[0] == "127.0.0.1" else (_ for _ in ()).throw(OSError("offline")))(sock, address))
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *args, **kwargs: (
        _GETADDRINFO if host == "127.0.0.1" else (_ for _ in ()).throw(OSError("offline")))(host, *args, **kwargs))
    work = console / "repo"
    work.mkdir()
    monkeypatch.chdir(work)
    httpd = server.serve("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def get(path):
        connection = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=10)
        connection.request("GET", path, headers={server.TOKEN_HEADER: httpd.session_token})
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, body

    yield get
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("text", [json.dumps(BAD_FILES["checkpoint_yes"]), json.dumps(BAD_FILES["timeout_abc"]),
                                  json.dumps(BAD_FILES["everything_wrong"]), NOT_A_RECORD["list"],
                                  NOT_A_RECORD["deep"], NAN_LITERAL],
                         ids=["checkpoint_yes", "timeout_abc", "everything_wrong", "list", "deep", "nan"])
def test_api_runs_answers_200_for_any_failure_file(console, http_console, text):
    failed_run(console, text)
    for _ in range(2):          # the first call notices the exit, the second reads the stored record
        status, body = http_console("/api/runs")
        assert status == 200, body[:300]
        payload = json.loads(body)
        assert payload["run"]["status"] == "failed" and payload["run"]["detail"]


def test_api_runs_answers_200_even_when_the_failure_reader_breaks(console, http_console, monkeypatch):
    failed_run(console, json.dumps({"kind": "timeout"}))
    monkeypatch.setattr(run_failure, "clean_failure", lambda data: 1 / 0)
    status, body = http_console("/api/runs")
    assert status == 200
    assert json.loads(body)["run"]["detail"] == "分析进程已退出，但没有写出结果。进程退出不等于分析完成。"


# ------------------------------------------------------------------ W18

class _State(TypedDict, total=False):
    done: list


def _graph(saver, fail: bool):
    def first(state):
        return {"done": [*(state.get("done") or []), "first"]}

    def second(state):
        if fail:
            raise RuntimeError(leaky_text())
        return {"done": [*(state.get("done") or []), "second"]}

    builder = StateGraph(_State)
    builder.add_node("first", first)
    builder.add_node("second", second)
    builder.add_edge(START, "first")
    builder.add_edge("first", "second")
    builder.add_edge("second", END)
    return builder.compile(checkpointer=saver)


def _all_bytes(directory: Path) -> bytes:
    return b"".join(path.read_bytes() for path in sorted(directory.iterdir()) if path.is_file())


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def loose_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def test_a_failed_node_error_is_redacted_in_the_checkpoint_database(tmp_path, loose_umask):
    config = {"configurable": {"thread_id": "t1"}}
    with checkpointer.get_checkpointer(tmp_path, "NVDA") as saver:
        with pytest.raises(RuntimeError):
            _graph(saver, fail=True).invoke({"done": []}, config)
        stored = saver.conn.execute(
            "SELECT count(*) FROM writes WHERE channel = '__error__'").fetchone()[0]
        directory = tmp_path / "checkpoints"
        files = sorted(directory.iterdir())
        assert stored == 1
        raw = _all_bytes(directory)
        for name, canary in CANARIES.items():
            assert canary.encode() not in raw, name
        assert b"[redacted-key]" in raw and b"RuntimeError" in raw
        for path in files:
            assert _mode(path) == 0o600, (path.name, oct(_mode(path)))
    assert _mode(directory) == 0o700
    for path in directory.iterdir():
        assert _mode(path) == 0o600, (path.name, oct(_mode(path)))

    # The run still resumes from the failed node.
    with checkpointer.get_checkpointer(tmp_path, "NVDA") as saver:
        result = _graph(saver, fail=False).invoke(None, config)
    assert result["done"] == ["first", "second"]


def test_an_old_raw_error_is_scrubbed_when_the_database_is_opened(tmp_path):
    config = {"configurable": {"thread_id": "t2"}}
    raw_saver_cls = checkpointer.RedactingSqliteSaver.__mro__[1]
    db = checkpointer._db_path(tmp_path, "AAPL")
    conn = sqlite3.connect(str(db), check_same_thread=False)
    saver = raw_saver_cls(conn)
    saver.setup()
    with pytest.raises(RuntimeError):
        _graph(saver, fail=True).invoke({"done": []}, config)
    conn.close()
    assert CANARIES["sk"].encode() in _all_bytes(db.parent)       # written raw by the old saver
    with checkpointer.get_checkpointer(tmp_path, "AAPL") as opened:
        opened.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        opened.conn.execute("VACUUM")
        row = opened.conn.execute("SELECT type, value FROM writes WHERE channel = '__error__'").fetchone()
        assert CANARIES["sk"] not in opened.serde.loads_typed(row)
    raw = _all_bytes(db.parent)
    for name, canary in CANARIES.items():
        assert canary.encode() not in raw, name
    assert _mode(db) == 0o600


def test_an_existing_loose_database_is_made_private(tmp_path):
    directory = tmp_path / "checkpoints"
    directory.mkdir(mode=0o755)
    db = directory / "MSFT.db"
    db.write_bytes(b"")
    os.chmod(db, 0o644)
    with checkpointer.get_checkpointer(tmp_path, "MSFT"):
        pass
    assert _mode(directory) == 0o700 and _mode(db) == 0o600


def test_redact_error_never_keeps_the_raw_text():
    text = checkpointer.redact_error(RuntimeError(leaky_text()))
    assert text.startswith("RuntimeError(")
    for canary in CANARIES.values():
        assert canary not in text


# ------------------------------------------------------------------ W19 / W22

@pytest.mark.parametrize("raw, gone", [
    ("open /Users/Zhou Han/Library/App Support/key.db failed", ["Zhou", "Han", "App Support"]),
    ("open /home/zhou han/.config/tradingagents/auth.json", ["zhou", "han"]),
    (r"open C:\Users\Zhou Han\AppData\auth.json failed", ["Zhou", "Han"]),
    ("cwd /Users/Zhou Han", ["Zhou", "Han"]),
    ("cwd /home/zhouhan", ["zhouhan"]),
    ("cwd /home/zhouhan/ now", ["zhouhan"]),
    ("(/Users/zhouhan)", ["zhouhan"]),
    (r"cwd C:\Users\zhouhan", ["zhouhan"]),
    (r"cwd C:\Users\Zhou Han", ["Zhou", "Han"]),
    ("file:///Users/zhouhan/secret%20dir/auth.json", ["zhouhan", "secret"]),
    ("file://localhost/home/zhouhan/x.json", ["zhouhan", "localhost"]),
    ("file:/home/zhouhan/x.json", ["zhouhan"]),
    ("file:///home/zhouhan", ["zhouhan"]),
    ("Cookie: sid=abc123; theme=dark; __Secure-next-auth.session-token=tok9", ["abc123", "dark", "tok9"]),
    ("Set-Cookie: session=XYZ987; Path=/; HttpOnly", ["XYZ987"]),
    ('{"cookie": "a=b; c=d"}', ["a=b", "c=d"]),
    ("got sessionid=abc123 and connect.sid=s%3Aabc and csrftoken=zz9 and PHPSESSID=pp7", ["abc123", "s%3Aabc", "zz9", "pp7"]),
    ("session_token=tok12 _session_id=x77 auth_token=y88", ["tok12", "x77", "y88"]),
    ("key xai-AbC123 short", ["xai-AbC123"]),
    ("key xai-abcd", ["xai-abcd"]),
    ("key xai_QWERTY0123456789", ["QWERTY0123456789"]),
    ("key XAI-AbC123", []),
])
def test_redaction_gaps_from_qa_round_1(raw, gone):
    text = redact_text(raw)
    for item in gone:
        assert item not in text, (raw, text)


def test_the_xai_prefix_rule_is_what_catches_a_short_key():
    assert redact_text("xai-Ab12") == "[redacted-key]"
    assert redact_text("xai-" + "A" * 40) == "[redacted-key]"


@pytest.mark.parametrize("text", [
    "POST https://api.x.ai/v1/chat/completions failed after 3 tokens: monkey business",
    "considered=1 with grok-4.7 on python3.13",
    "/tmp/a.txt was missing",
])
def test_ordinary_text_survives(text):
    expected = "[path]/a.txt was missing" if text.startswith("/tmp") else text
    assert redact_text(text) == expected


def test_the_last_name_of_a_path_is_kept_when_it_is_not_a_user():
    assert redact_text("/Users/Zhou Han/Library/auth.json") == "[path]/auth.json"
    assert redact_text("file:///opt/data/report.md") == "[path]/report.md"


def test_the_webui_module_uses_the_shared_redaction():
    assert run_failure.redact_text is redact_text
