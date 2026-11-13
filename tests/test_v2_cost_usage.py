"""V2-INPROGRESS-2 (V2-COST-STATS, no spend): provider usage per call, by graph role.

Scripted models only (tests/test_graph_end_to_end.py); no network, no key, no model.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult

from cli.stats_handler import StatsCallbackHandler, _callback_role
from tests.test_graph_end_to_end import TRADE_DATE, ScriptedModel, _graph, offline  # noqa: F401
from webui import cost, service

ROLES = {"Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst",
         "Bull Researcher", "Bear Researcher", "Research Manager", "Trader",
         "Aggressive Analyst", "Neutral Analyst", "Conservative Analyst", "Portfolio Manager"}


class UsageModel(ScriptedModel):
    """ScriptedModel whose every answer carries provider usage, like a real API reply."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        result = super()._generate(messages, stop, run_manager, **kwargs)
        message = result.generations[0].message
        number = len(self.calls)
        message.usage_metadata = {
            "input_tokens": 100 + number, "output_tokens": 10 + number, "total_tokens": 110 + 2 * number,
            "input_token_details": {"cache_read": number},
        }
        message.response_metadata = {"model_name": "scripted-model", "id": f"resp-{number}"}
        return ChatResult(generations=[ChatGeneration(message=message)])


def _selections(analysts=("market", "social", "news", "fundamentals")):
    return {"analysts": list(analysts), "llm_provider": "deepseek", "quick_think_llm": "deepseek-chat",
            "deep_think_llm": "deepseek-reasoner"}


@pytest.mark.unit
def test_subgraph_analyst_usage_is_attributed_to_its_role_through_the_real_graph(tmp_path, monkeypatch, offline):  # noqa: F811
    handler = StatsCallbackHandler()
    model = UsageModel(callbacks=[handler])
    graph = _graph(tmp_path, monkeypatch, model)

    graph.propagate("NVDA", TRADE_DATE)

    stats = handler.get_stats()
    roles = {row["role"] for row in stats["calls"]}
    assert roles == ROLES
    assert "agent" not in roles and "" not in roles
    analysts = [row for row in stats["calls"] if row["role"].endswith("Analyst") and row["node"] == "agent"]
    assert {row["role"] for row in analysts} == {"Market Analyst", "Sentiment Analyst", "News Analyst",
                                                 "Fundamentals Analyst"}
    assert len(stats["calls"]) == len(model.calls) == stats["usage_reported"] == stats["llm_calls"]
    assert stats["usage_unknown"] == 0
    assert sum(row["input_tokens"] for row in stats["calls"]) == stats["tokens_in"]
    assert sum(row["output_tokens"] for row in stats["calls"]) == stats["tokens_out"]
    assert {key: sum(row["input_tokens"] for row in stats["calls"] if row["role"] == key) for key in ROLES} == {
        key: value["input_tokens"] for key, value in stats["by_role"].items()}
    first = stats["calls"][0]
    assert first["model"] == "scripted-model" and first["response_id"].startswith("resp-")
    assert first["input_token_details"]["cache_read"] >= 1
    assert first["total_tokens"] == first["input_tokens"] + first["output_tokens"]

    # Without a provider total the page shows the run total only (Eric's rule: no
    # per-role figures until the total is checked).
    record = {"id": "graph-run", "kind": "analysis", "ticker": "NVDA", "selections": _selections(),
              "role_models": cost.role_snapshot(_selections()), "usage": cost.usage_record(stats)}
    view = cost._view_run(record, {})
    assert view["prompt_count"] == stats["tokens_in"]
    assert view["match_text"] == "没有供应商合计，不把调用合计当成已核对。"
    # Once a matching provider total is recorded (REGRESSION-01), every call already
    # sits on a role of this run: nothing is 用量未归集 or unassigned.
    record["usage"]["reported"] = {"input_tokens": stats["tokens_in"], "output_tokens": stats["tokens_out"]}
    view = cost._view_run(record, {})
    assert view["match_text"] == "与供应商计数一致，容差 0。"
    assert {row["role"] for row in view["roles"] if row["prompt_count"] is not None} == ROLES
    assert all(row["usage_label"] != "用量未归集" for row in view["roles"])


@pytest.mark.unit
def test_memory_log_reflection_is_attributed_to_the_memory_log(tmp_path, monkeypatch, offline):  # noqa: F811
    from tradingagents.memory import settlement

    handler = StatsCallbackHandler()
    graph = _graph(tmp_path, monkeypatch, UsageModel(callbacks=[handler]))
    graph.memory_log.store_decision("NVDA", "2025-12-31", "Rating: Buy\n\nBuy NVDA.")
    closes = pd.Series([100.0 + i for i in range(10)], index=pd.bdate_range("2026-01-02", periods=10))
    monkeypatch.setattr(settlement, "get_closes", lambda *a, **k: closes)

    graph.propagate("NVDA", TRADE_DATE)

    memory = [row for row in handler.get_stats()["calls"] if row["role"] == "Memory Log"]
    assert len(memory) == 1 and memory[0]["node"] == "Memory Log"


def test_role_comes_from_the_outer_node_and_needs_start_metadata():
    nested = {"metadata": {"langgraph_node": "agent",
                           "langgraph_checkpoint_ns": "News Analyst:1a|agent:2b", "checkpoint_ns": "News Analyst:1a"}}
    assert _callback_role(nested) == "News Analyst"
    assert _callback_role({"metadata": {"langgraph_node": "Trader", "langgraph_checkpoint_ns": "Trader:9"}}) == "Trader"
    assert _callback_role({"metadata": {"langgraph_node": "Trader"}}) == "Trader"
    assert _callback_role({}) == ""
    # on_llm_end never gets metadata: the role is the one remembered at start.
    handler = StatsCallbackHandler()
    run_id = uuid.uuid4()
    handler.on_chat_model_start({}, [[]], run_id=run_id, metadata=nested["metadata"],
                                invocation_params={"model": "deepseek-chat"})
    message = AIMessage(content="ok", usage_metadata={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10})
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)
    row = handler.get_stats()["calls"][0]
    assert (row["role"], row["node"], row["model"], row["input_tokens"]) == ("News Analyst", "agent", "deepseek-chat", 7)
    assert handler.get_stats()["by_role"] == {"News Analyst": {"input_tokens": 7, "output_tokens": 3}}
    # A call without usage stays unknown and is not a row.
    other = uuid.uuid4()
    handler.on_chat_model_start({}, [[]], run_id=other, metadata=nested["metadata"])
    handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="x"))]]), run_id=other)
    assert len(handler.get_stats()["calls"]) == 1 and handler.get_stats()["usage_unknown"] == 1
    failed = uuid.uuid4()
    handler.on_chat_model_start({}, [[]], run_id=failed, metadata=nested["metadata"])
    handler.on_llm_error(RuntimeError("down"), run_id=failed)
    assert handler._started == {}


class _FakeGraph:
    """Enough of TradingAgentsGraph for default_runner; its model call reports usage."""

    fail = False
    config = {"checkpoint_enabled": False, "data_cache_dir": ""}

    def __init__(self, *args, callbacks=None, **kwargs):
        self.handler = callbacks[0]

    def _run_signature(self, *args):
        return "sig"

    def create_run_state(self, *args):
        return {}

    @property
    def propagator(self):
        return type("Propagator", (), {"get_graph_args": lambda self, callbacks=None: {}})()

    def begin_checkpoint(self, *args):
        return None

    def checkpoint_input(self, state):
        return state

    def stream_run(self, graph_input, **kwargs):
        run_id = uuid.uuid4()
        self.handler.on_chat_model_start({}, [[]], run_id=run_id, invocation_params={"model": "deepseek-chat"},
                                         metadata={"langgraph_node": "agent",
                                                   "langgraph_checkpoint_ns": "Market Analyst:1|agent:2"})
        message = AIMessage(content="r", usage_metadata={"input_tokens": 120, "output_tokens": 30, "total_tokens": 150})
        self.handler.on_llm_end(LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=run_id)
        if self.fail:
            raise RuntimeError("provider unavailable")
        yield [], {"market_report": "prices"}

    def record_decision(self, *args):
        return None

    def clear_checkpoint_on_success(self, *args):
        return None

    def end_checkpoint(self):
        return None


def _job(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    return {
        "selections": {"ticker": "NVDA", "analysis_date": "2026-10-29", "asset_type": "stock",
                       "analysts": ["market"], "research_depth": 1, "llm_provider": "deepseek",
                       "quick_think_llm": "deepseek-chat", "deep_think_llm": "deepseek-reasoner",
                       "backend_url": None, "save": False},
        "result_path": str(logs / "run-usage.json"),
    }


@pytest.mark.parametrize("fail", [False, True], ids=["finished", "failed"])
def test_a_console_run_keeps_its_usage_beside_the_result(monkeypatch, tmp_path, fail):
    import tradingagents.graph.trading_graph as graph_module

    monkeypatch.setattr(graph_module, "TradingAgentsGraph", type("G", (_FakeGraph,), {"fail": fail}))
    job = _job(tmp_path)
    if fail:
        with pytest.raises(RuntimeError, match="provider unavailable"):
            service.default_runner(job)
        assert not Path(job["result_path"]).exists()
    else:
        assert service.default_runner(job) == 0
    usage = json.loads(cost.usage_path(job["result_path"]).read_text(encoding="utf-8"))
    assert usage["schema"] == cost.USAGE_SCHEMA and "reported" not in usage
    assert [(row["role"], row["input_tokens"], row["output_tokens"], row["model"]) for row in usage["calls"]] == [
        ("Market Analyst", 120, 30, "deepseek-chat")]
    assert (usage["input_tokens"], usage["output_tokens"], usage["usage_unknown"]) == (120, 30, 0)
    assert "sk-" not in json.dumps(usage) and "api_key" not in json.dumps(usage)


def test_a_relative_result_path_writes_no_usage_file(monkeypatch, tmp_path):
    import tradingagents.graph.trading_graph as graph_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(graph_module, "TradingAgentsGraph", _FakeGraph)
    job = {**_job(tmp_path), "result_path": "unused.json"}
    assert service.default_runner(job) == 0
    assert not (tmp_path / "unused.usage.json").exists()


def _run_with_usage_file(home: Path, run_id: str, usage: dict, *, result_dir: Path | None = None) -> None:
    folder = result_dir or home / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    result = folder / f"{run_id}.json"
    cost.usage_path(result).write_text(json.dumps(usage), encoding="utf-8")
    runs = home / "runs"
    runs.mkdir(exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps({
        "id": run_id, "kind": "analysis", "ticker": "NVDA", "date": "2026-11-12", "status": "finished",
        "selections": _selections(["market"]), "role_models": cost.role_snapshot(_selections(["market"])),
        "result": str(result)}), encoding="utf-8")


def test_the_cost_page_reads_the_usage_file_and_prices(tmp_path):
    usage = cost.usage_record({"calls": [{"role": "Market Analyst", "input_tokens": 1000, "output_tokens": 200},
                                         {"role": "Portfolio Manager", "input_tokens": 500, "output_tokens": 50}],
                               "usage_unknown": 2, "tokens_in": 1500, "tokens_out": 250})
    _run_with_usage_file(tmp_path, "with-usage", usage)
    page = cost.list_cost(tmp_path)
    assert page["prices_path"] == str(tmp_path / "cost-prices.json") and page["prices_models"] == 0
    view = page["runs"][0]
    assert view["prompt_count"] == 1500 and view["completion_count"] == 250
    assert view["usage_unknown"] == 2
    # No provider total yet: no money is claimed, priced or not.
    assert view["api_text"] == "不显示 API 合计：没有供应商合计。"
    (tmp_path / "cost-prices.json").write_text(json.dumps({"models": {
        "deepseek-chat": {"input_per_million": "0.50", "output_per_million": "1.50"}}}), encoding="utf-8")
    assert cost.list_cost(tmp_path)["prices_models"] == 1
    # With a provider total recorded later, a listed model is priced and an unlisted one is 价格未知.
    reported = {**usage, "reported": {"input_tokens": 1500, "output_tokens": 250}}
    _run_with_usage_file(tmp_path, "with-usage", reported)
    view = next(item for item in cost.list_cost(tmp_path)["runs"] if item["id"] == "with-usage")
    roles = {row["role"]: row["cost_text"] for row in view["roles"]}
    assert roles["Portfolio Manager"] == "价格未知"      # deepseek-reasoner is not listed
    assert roles["Market Analyst"] == "不计入合计"       # priced, but the run total is unknown
    assert view["api_text"] == "价格未知"
    prices = json.loads((tmp_path / "cost-prices.json").read_text(encoding="utf-8"))
    prices["models"]["deepseek-reasoner"] = {"input_per_million": "1.00", "output_per_million": "2.00"}
    (tmp_path / "cost-prices.json").write_text(json.dumps(prices), encoding="utf-8")
    view = next(item for item in cost.list_cost(tmp_path)["runs"] if item["id"] == "with-usage")
    # 1000*0.5+200*1.5 + 500*1.0+50*2.0 per million = 0.0014 USD
    assert view["api_text"] == "0.0014 USD"


def test_a_usage_file_outside_the_console_logs_is_not_read(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    usage = cost.usage_record({"calls": [{"role": "Market Analyst", "input_tokens": 9, "output_tokens": 9}]})
    _run_with_usage_file(home, "outside", usage, result_dir=tmp_path / "elsewhere")
    view = cost.list_cost(home)["runs"][0]
    assert view["prompt_count"] is None
    assert view["match_text"] == "没有供应商用量，不估算。"


def test_the_shipped_price_template_is_empty_and_documented():
    template = Path("webui/cost-prices.example.json")
    data = json.loads(template.read_text(encoding="utf-8"))
    assert data["models"] == {}
    assert "input_per_million" in data["_format"] and "价格未知" in data["_format"]
    assert cost.PRICES_NAME in cost.__doc__ and "input_per_million" in cost.__doc__


@pytest.mark.unit
def test_cost_fixtures_show_pending_and_checked_runs(tmp_path, monkeypatch):
    from tests import v2_cost_fixtures

    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    v2_cost_fixtures.build(tmp_path)
    page = cost.list_cost(tmp_path)
    runs = {item["id"]: item for item in page["runs"]}
    pending, checked = runs[v2_cost_fixtures.PENDING], runs[v2_cost_fixtures.CHECKED]
    assert pending["usage_unknown"] == 1
    assert pending["api_text"] == "不显示 API 合计：没有供应商合计。"
    assert checked["match_text"] == "与供应商计数一致，容差 0。"
    roles = {row["role"]: row for row in checked["roles"]}
    assert roles["Portfolio Manager"]["cost_text"] == "价格未知"
    assert roles["Market Analyst"]["prompt_count"] == 900
    assert page["prices_models"] == 1
