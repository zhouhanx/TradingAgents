"""RUN-002: a console run that fails says which role failed and why, redacted,
and a restart resumes from the failed role without re-calling finished ones.

The graph is the real one with scripted models (tests/test_graph_end_to_end.py);
the worker entry point is the real ``webui.worker.main``. No network.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import openai
import pytest
from pydantic import Field

from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _Client, offline  # noqa: F401
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph import trading_graph
from tradingagents.llm_clients import call_policy
from tradingagents.llm_clients.call_policy import CallPolicy, split_timeout
from webui import run_failure, service, worker

pytestmark = pytest.mark.unit

TOKEN = "xai-PLANTEDtoken0123456789abcdef"
KEY = "sk-PLANTEDkey9876543210"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJlcmljIn0.c2lnbmF0dXJlUExBTlRFRA"
SECRET_PATH = "/home/eric/.tradingagents/auth/grok.json"
EMAIL = "eric@example.com"
PLANTED = (TOKEN, KEY, JWT, "/home/eric", EMAIL, "PLANTED")
ANALYST_NODES = {"agent", "tools", "wrap_up"}


class NodeModel(ScriptedModel):
    """ScriptedModel that records which graph node called it and can fail one node once."""

    plan: dict = Field(default_factory=dict)   # shared across bound copies

    def _count(self) -> None:
        from langgraph.config import get_config

        node = (get_config().get("metadata") or {}).get("langgraph_node")
        if node == self.plan.get("fail_node") and self.plan.get("error") is not None:
            error, self.plan["error"] = self.plan["error"], None
            raise error
        self.calls.append(1)
        self.plan.setdefault("nodes", []).append(node)


def grok_timeout() -> BaseException:
    """What the call policy raises after a Grok call timed out on every attempt."""
    import httpx2

    def call():
        raise openai.APITimeoutError(request=httpx2.Request("POST", "https://api.x.ai/v1/chat/completions"))

    try:
        call_policy.run_with_policy(call, policy=CallPolicy(retries=3, budget=900),
                                    timeout=split_timeout(300), model="grok-4.7", provider="grok",
                                    sleep=lambda seconds: None)
    except openai.APITimeoutError as exc:
        return exc
    raise AssertionError("the call should have failed")


def leaky_error() -> BaseException:
    return RuntimeError(f"upstream refused Authorization: Bearer {TOKEN}; api_key={KEY}; "
                        f"id_token {JWT}; read {SECRET_PATH} for {EMAIL}")


@pytest.fixture
def console(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path / "webui"))
    monkeypatch.delenv("TRADINGAGENTS_CHECKPOINT_ENABLED", raising=False)
    monkeypatch.setattr("cli.prefs._PREFS_PATH", tmp_path / "cli_prefs.json")
    for key, folder in (("data_cache_dir", "cache"), ("results_dir", "results")):
        monkeypatch.setitem(DEFAULT_CONFIG, key, str(tmp_path / folder))
    monkeypatch.setitem(DEFAULT_CONFIG, "memory_log_path", str(tmp_path / "memory.md"))
    monkeypatch.setitem(DEFAULT_CONFIG, "checkpoint_enabled", False)
    return tmp_path


def selections(**extra) -> dict:
    return {
        "ticker": "NVDA", "analysis_date": TRADE_DATE, "asset_type": "stock",
        "analysts": ["market"], "research_depth": 1,
        "llm_provider": "grok", "quick_think_llm": "grok-4.7", "deep_think_llm": "grok-4.7",
        "backend_url": None, "output_language": "English",
        "save": False, "html": False, **extra,
    }


def run_worker(tmp_path, capsys, model, name, monkeypatch, **extra):
    """Run one console job through webui.worker.main; return (code, job, log text)."""
    monkeypatch.setattr(trading_graph, "create_tier_client", lambda config, tier, **k: _Client(model))
    logs = tmp_path / "webui" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    job = {"id": name, "kind": "analysis", "selections": selections(**extra),
           "result_path": str(logs / f"{name}.json"), "log": str(logs / f"{name}.log"),
           "job_path": str(logs / f"{name}.job.json")}
    Path(job["job_path"]).write_text(json.dumps(job), encoding="utf-8")
    capsys.readouterr()
    code = worker.main([job["job_path"]])
    captured = capsys.readouterr()
    log = captured.out + captured.err
    Path(job["log"]).write_text(log, encoding="utf-8")
    return code, job, log


def dead_pid() -> int:
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def record_for(job) -> dict:
    record = service._record_for(job["id"], job["selections"], Path(job["log"]), Path(job["result_path"]),
                                 kind="analysis")
    record["pid"] = dead_pid()
    service.JOBS.write_run(record)
    return record


# ------------------------------------------------------------------ the failure file

def test_a_timed_out_role_is_named_with_provider_model_timeout_and_retries(console, monkeypatch, capsys, offline):  # noqa: F811
    model = NodeModel(plan={"fail_node": "Bull Researcher", "error": grok_timeout()})
    code, job, log = run_worker(console, capsys, model, "run-timeout", monkeypatch)
    assert code == 1
    failure = json.loads(run_failure.failure_path(job["result_path"]).read_text(encoding="utf-8"))
    assert failure | {"message": None, "run_id": None} == {
        "role": "Bull Researcher", "role_zh": "多头研究员", "kind": "timeout", "kind_zh": "调用超时",
        "error_type": "APITimeoutError", "message": None,
        "provider": "grok", "provider_label": "Grok 订阅", "model": "grok-4.7",
        "timeout": 300.0, "budget": 900.0, "attempts": 4, "max_attempts": 4, "retries": 3,
        "budget_exhausted": False, "checkpoint": {"enabled": True, "saved": True},
        "run_id": None, "summary": "多头研究员 · Grok 订阅 grok-4.7 调用超时（300 秒，已重试 3 次）",
    }
    assert "STATUS\tBull Researcher\terror" in log
    assert "FAILURE\t" in log

    record = service.refresh_run_process(record_for(job))
    assert record["status"] == "failed"
    assert record["detail"].startswith("多头研究员 · Grok 订阅 grok-4.7 调用超时（300 秒，已重试 3 次）。")
    assert "已保存断点" in record["detail"]
    assert record["checkpoint"] == {"enabled": True, "saved": True}
    assert record["failure"]["role"] == "Bull Researcher"
    rows = {row["agent"]: row["status"] for row in service.run_progress()["agents"]}
    assert rows["Bull Researcher"] == "error"
    assert rows["Market Analyst"] == "completed"


def test_without_a_failure_file_the_generic_detail_stays(console, monkeypatch):
    logs = console / "webui" / "logs"
    logs.mkdir(parents=True)
    job = {"id": "run-x", "selections": selections(), "result_path": str(logs / "run-x.json"),
           "log": str(logs / "run-x.log")}
    record = service.refresh_run_process(record_for(job))
    assert record["detail"] == "分析进程已退出，但没有写出结果。进程退出不等于分析完成。"
    assert "failure" not in record


# ------------------------------------------------------------------ redaction

def test_planted_secrets_and_paths_never_leave_the_worker(console, monkeypatch, capsys, offline):  # noqa: F811
    model = NodeModel(plan={"fail_node": "Bull Researcher", "error": leaky_error()})
    code, job, log = run_worker(console, capsys, model, "run-leak", monkeypatch)
    assert code == 1
    record = service.refresh_run_process(record_for(job))
    failure_text = run_failure.failure_path(job["result_path"]).read_text(encoding="utf-8")
    state_text = (console / "webui" / "runs" / f"{job['id']}.json").read_text(encoding="utf-8")
    status = json.dumps({"run": record, "progress": service.run_progress(),
                         "sentence": service.console_status_sentence(record)}, ensure_ascii=False)
    for surface, text in (("failure file", failure_text), ("run state", state_text),
                          ("status payload", status), ("log", log)):
        for planted in PLANTED:
            assert planted not in text, (surface, planted)
    assert "[redacted]" in failure_text and "[path]/grok.json" in failure_text
    traceback_part = log[log.index("Traceback"):]
    assert not re.search(r"(?<![\w.:/~-])/(?:workspace|home|tmp|Users)/", traceback_part)
    assert record["detail"].startswith("多头研究员 · 调用失败：RuntimeError: upstream refused")


def test_an_in_process_runner_failure_is_redacted_too(console, monkeypatch):
    def runner(job):
        raise leaky_error()

    monkeypatch.setattr(service, "_runner", runner)
    record = service.start_validated_run(selections(), background=False)
    stored = (console / "webui" / "runs" / f"{record['id']}.json").read_text(encoding="utf-8")
    log = Path(record["log"]).read_text(encoding="utf-8")
    for text in (json.dumps(record, ensure_ascii=False), stored, log):
        for planted in PLANTED:
            assert planted not in text
    assert record["status"] == "failed" and record["failure"]["kind"] == "other"


@pytest.mark.parametrize("raw, gone, kept", [
    ("Authorization: Bearer abc.def-ghi", "abc.def-ghi", "Bearer [redacted]"),
    ("refresh_token=rt_ABCDEFGH12345&x=1", "rt_ABCDEFGH12345", "refresh_token=[redacted]"),
    ('{"api_key": "sk-proj-abcdef123456"}', "sk-proj-abcdef123456", "api_key"),
    ("file /Users/eric/Library/tradingagents/auth.json", "/Users/eric", "[path]/auth.json"),
    (r"file C:\Users\eric\auth.json", "eric", r"[path]\auth.json"),
    ("mail eric@example.com", "eric@example.com", "[redacted-email]"),
])
def test_redaction_patterns(raw, gone, kept):
    text = run_failure.redact_text(raw)
    assert gone not in text and kept in text


def test_urls_and_ordinary_words_survive_redaction():
    text = "POST https://api.x.ai/v1/chat/completions failed after 3 tokens: monkey business"
    assert run_failure.redact_text(text) == text


def test_summaries_for_other_kinds():
    base = {"role_zh": "交易员", "provider": "chatgpt", "provider_label": "ChatGPT 订阅", "model": "gpt-6"}
    assert run_failure.summary({**base, "kind": "auth", "kind_zh": "登录失效或无权限", "retries": 0}) == \
        "交易员 · ChatGPT 订阅 gpt-6 登录失效或无权限（未重试）"
    assert run_failure.summary({**base, "kind": "rate_limit", "kind_zh": "被限流", "retries": 1,
                                "budget_exhausted": True, "budget": 900}) == \
        "交易员 · ChatGPT 订阅 gpt-6 被限流（已重试 1 次，已到单次调用总时限 900 秒）"


# ------------------------------------------------------------------ resume

def test_a_restart_resumes_at_the_failed_role_without_re_calling_finished_ones(console, monkeypatch, capsys, offline):  # noqa: F811
    failing = NodeModel(plan={"fail_node": "Bull Researcher", "error": grok_timeout()})
    code, _, _ = run_worker(console, capsys, failing, "run-1", monkeypatch)
    assert code == 1
    done_before = len(failing.calls)
    assert set(failing.plan["nodes"]) <= ANALYST_NODES          # only the analyst had answered

    # Restarted with a longer bound, as a user would after a timeout: the
    # timeout is not part of the checkpoint signature, so the run still resumes.
    resumed = NodeModel()
    code, _, log = run_worker(console, capsys, resumed, "run-2", monkeypatch, timeout=600.0,
                              llm_call_budget=1800.0)
    assert code == 0
    from cli.run import checkpoint_resume_message
    assert checkpoint_resume_message("NVDA", TRADE_DATE) in log
    assert not set(resumed.plan["nodes"]) & ANALYST_NODES      # the analyst is not asked again
    assert resumed.plan["nodes"][0] == "Bull Researcher"

    fresh = NodeModel()
    code, _, _ = run_worker(console / "fresh", capsys, fresh, "run-3", monkeypatch, checkpoint=False,
                            ticker="NVDA")
    assert code == 0
    assert len(resumed.calls) == len(fresh.calls) - done_before


# ------------------------------------------------------------------ console defaults

class _CapturingGraph:
    seen: dict = {}

    def __init__(self, analysts, config, **kwargs):
        type(self).seen = dict(config)
        raise RuntimeError("stop after config")


def _config_for(monkeypatch, **extra) -> dict:
    monkeypatch.setattr(trading_graph, "TradingAgentsGraph", _CapturingGraph)
    with pytest.raises(RuntimeError, match="stop after config"):
        service.default_runner({"selections": selections(**extra), "result_path": "unused.json"})
    return _CapturingGraph.seen


def test_console_runs_get_split_timeouts_a_budget_retries_and_checkpoints(console, monkeypatch):
    config = _config_for(monkeypatch)
    assert config["timeout"] == service.CONSOLE_MODEL_TIMEOUT == 300
    assert config["llm_call_budget"] == service.CONSOLE_CALL_BUDGET == 900
    assert config["checkpoint_enabled"] is True

    from tradingagents.llm_clients import openai_client
    from tradingagents.llm_clients.factory import create_tier_client

    spec = openai_client.OPENAI_COMPATIBLE_PROVIDERS["grok"]
    monkeypatch.setitem(openai_client.OPENAI_COMPATIBLE_PROVIDERS, "grok",
                        spec.__class__(**{**spec.__dict__, "credential": lambda: (lambda: "stub")}))
    llm = create_tier_client(config, "quick").get_llm()
    assert llm.streaming is True
    assert llm.call_policy == CallPolicy(retries=3, budget=900)
    assert (llm.request_timeout.connect, llm.request_timeout.read,
            llm.request_timeout.write, llm.request_timeout.pool) == (15, 300, 60, 15)
    worst = call_policy.worst_case_seconds(llm.call_policy, llm.request_timeout)
    assert worst == 901 < 1290 < 3 * 3 * 300   # budget + 1 s cutoff slack (r2: hard wall clock)


def test_page_values_win_over_the_console_defaults(console, monkeypatch):
    config = _config_for(monkeypatch, timeout=45.0, llm_call_budget=200.0, llm_max_retries=1, checkpoint=False)
    assert (config["timeout"], config["llm_call_budget"], config["llm_max_retries"]) == (45.0, 200.0, 1)
    assert config["checkpoint_enabled"] is False


def test_the_checkpoint_environment_variable_still_wins(console, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_CHECKPOINT_ENABLED", "false")
    assert _config_for(monkeypatch)["checkpoint_enabled"] is False


def test_the_run_state_says_whether_the_run_checkpoints(console, monkeypatch):
    monkeypatch.setattr(service, "_runner", lambda job: 0)
    record = service.start_validated_run(selections(), background=False)
    # RUN-004: the run also names its own checkpoint scope (its run id).
    assert record["checkpoint"] == {"enabled": True, "saved": None, "scope": record["id"]}


def test_the_call_budget_is_validated_and_saved(console):
    assert service.save_config({"llm_call_budget": "600"})["llm_call_budget"] == 600.0
    assert service.save_config({"llm_call_budget": ""})["llm_call_budget"] is None
    for bad in ("0", "-5", "soon", True):
        with pytest.raises(service.ConsoleError, match="单次调用总时限"):
            service.save_config({"llm_call_budget": bad})


def test_the_page_shows_the_failure_and_the_checkpoint_default():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    # The detail now goes through shownRunDetail so a non-analysis run is not given the analysis sentence.
    assert 'run.status === "failed" && run.failure && shownRunDetail(run)' in script
    assert 'id="run-failure">${punctBreakHtml(shownRunDetail(run))}' in script
    assert 'id="run-failure"' in script
    assert "run.checkpoint?.saved" in script
    # WEBUI-005: the checkpoint switch lives in the 保存与续跑 drawer and is on unless turned off.
    assert "checkpoint: state.form.checkpoint !== false," in script
    assert 'toggleRow("checkpoint", "失败后从出错的角色继续", flags.checkpoint)' in script
    assert '"timeout", "llm_call_budget"]' in script
