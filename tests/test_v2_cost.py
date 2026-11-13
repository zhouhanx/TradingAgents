"""V2-COST-STATS: frozen role models and structured budget classification."""

import copy
import json
from decimal import Decimal
from pathlib import Path

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.call_policy import CallBudgetExceeded
from webui import cost


def _selections():
    return {
        "analysts": ["market", "news"],
        "llm_provider": "openai",
        "quick_think_provider": "openai",
        "deep_think_provider": "chatgpt",
        "quick_think_llm": "gpt-4.1-mini",
        "deep_think_llm": "gpt-5",
        "openai_reasoning_effort": "medium",
        "google_thinking_level": "high",
    }


def test_snapshot_does_not_follow_or_write_global_defaults():
    before = copy.deepcopy(DEFAULT_CONFIG)
    selections = _selections()
    snap = cost.role_snapshot(selections)
    selections["quick_think_llm"] = "changed-later"
    DEFAULT_CONFIG["quick_think_llm"] = "global-changed"
    try:
        market = next(role for role in snap["roles"] if role["role"] == "Market Analyst")
        manager = next(role for role in snap["roles"] if role["role"] == "Research Manager")
        assert market["model"] == "gpt-4.1-mini"
        assert market["effort_field"] == "openai_reasoning_effort"
        assert market["effort"] == "medium"
        assert market["billing"] == "api"
        assert manager["provider"] == "chatgpt"
        assert manager["billing"] == "subscription"
        assert manager["effort"] == "medium"
        assert "News Analyst" in {role["role"] for role in snap["roles"]}
        assert "Fundamentals Analyst" not in {role["role"] for role in snap["roles"]}
        assert DEFAULT_CONFIG["llm_provider"] == before.get("llm_provider")
    finally:
        DEFAULT_CONFIG.clear()
        DEFAULT_CONFIG.update(before)


def test_started_snapshot_stays_when_selections_change():
    record = {"id": "run-1", "selections": _selections()}
    cost.freeze_role_models(record, record["selections"])
    record["selections"]["quick_think_llm"] = "other"
    assert record["role_models"]["roles"][0]["model"] == "gpt-4.1-mini"


class _Status:
    def __init__(self, status_code):
        self.status_code = status_code


class _Exc(Exception):
    def __init__(self, **kwargs):
        super().__init__(kwargs.get("message") or "Budget has been exceeded")
        for key, value in kwargs.items():
            setattr(self, key, value)


def test_rate_limit_and_text_are_not_budget():
    limited = cost.classify_failure(_Exc(status_code=429, body={"error": {"type": "budget_exceeded"}}))
    assert limited["budget"] is False
    assert limited["kind"] == "rate_limit"
    coded = cost.classify_failure(_Exc(code="RATE_LIMIT"))
    assert coded["budget"] is False
    text = cost.classify_failure(Exception("Budget has been exceeded"))
    assert text["kind"] == "unknown" and text["budget"] is False
    local = cost.classify_failure(CallBudgetExceeded("budget"))
    assert local["kind"] == "call_timeout" and local["budget"] is False


def test_structured_budget_exceeded_is_recognized():
    top = cost.classify_failure(_Exc(type="budget_exceeded", status_code=400))
    nested = cost.classify_failure(_Exc(body={"error": {"code": "budget_exceeded"}}))
    assert top["budget"] is True
    assert nested["budget"] is True and nested["kind"] == "budget_exceeded"
    google = cost.classify_failure(_Exc(code=429, details={"error": {"status": "RESOURCE_EXHAUSTED"}}))
    assert google["budget"] is False
    assert google["kind"] == "rate_limit"


def test_usage_matches_provider_counts_and_keeps_subscription_separate(tmp_path):
    calls = [
        {"role": "Market Analyst", "input_tokens": 1000, "output_tokens": 200},
        {"role": "Research Manager", "input_tokens": 50, "output_tokens": 10},
    ]
    reconciled = cost.reconcile_usage(calls, {"input_tokens": 1050, "output_tokens": 210})
    assert reconciled["matches_provider"] is True
    assert reconciled["tolerance_count"] == 0
    mismatched = cost.reconcile_usage(calls, {"input_tokens": 1, "output_tokens": 1})
    assert mismatched["matches_provider"] is False
    missing = cost.reconcile_usage(calls, None)
    assert missing["matches_provider"] is False
    assert missing["provider_total"] is False
    record = {
        "id": "no-total",
        "kind": "analysis",
        "ticker": "600519",
        "selections": _selections(),
        "usage": {"calls": calls},
    }
    view = cost._view_run(record, {})
    assert view["matches_provider"] is False
    assert view["match_text"] == "没有供应商合计，不把调用合计当成已核对。"
    assert view["api_text"] == "不显示 API 合计：没有供应商合计。"
    assert "没有 API 用量" not in view["api_text"]
    assert "USD" not in view["api_text"]
    (tmp_path / "cost-prices.json").write_text(json.dumps({
        "models": {"gpt-4.1-mini": {"input_per_million": "0.40", "output_per_million": "1.60", "currency": "USD"}}
    }), encoding="utf-8")
    record = {
        "id": "run-1",
        "kind": "analysis",
        "ticker": "600519",
        "date": "2026-11-11",
        "status": "finished",
        "selections": _selections(),
        "role_models": cost.role_snapshot(_selections()),
        "usage": {"calls": calls, "reported": {"input_tokens": 1050, "output_tokens": 210}},
    }
    view = cost._view_run(record, cost.load_prices(tmp_path))
    assert view["matches_provider"] is True
    assert view["api_text"] == "0.00072 USD"
    assert view["subscription_text"] == "订阅，不按次计费"
    assert Decimal("0.00072") == cost._money(1000, 200, {
        "input_per_million": Decimal("0.40"),
        "output_per_million": Decimal("1.60"),
        "currency": "USD",
    })


def test_missing_price_is_unknown_not_zero(tmp_path):
    record = {
        "id": "run-2",
        "kind": "analysis",
        "ticker": "AAPL",
        "selections": _selections(),
        "usage": {"calls": [{"role": "Market Analyst", "input_tokens": 1, "output_tokens": 1}],
                  "reported": {"input_tokens": 1, "output_tokens": 1}},
    }
    view = cost._view_run(record, cost.load_prices(tmp_path))
    assert view["api_text"] == "价格未知"


def test_page_has_cost_entry():
    html = Path("webui/static/index.html").read_text(encoding="utf-8")
    service = Path("webui/service.py").read_text(encoding="utf-8")
    assert 'data-page="cost"' in html
    assert "freeze_role_models(record, selections)" in service


def test_cost_page_names_prompt_and_completion_counts():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "提示 ${row.prompt_count} · 完成 ${row.completion_count}" in script
    assert "prompt_count}↑" not in script
    assert "429 记为限流，不是预算超限" in script
    assert "budget_exceeded" not in script


def test_run_health_uses_structured_fields_not_sentences(tmp_path):
    limited = {
        "id": "limited",
        "kind": "analysis",
        "ticker": "600519",
        "failure": {"provider_health": {"kind": "rate_limit", "budget": False}, "message": "Budget has been exceeded"},
    }
    overrun = {
        "id": "over",
        "kind": "analysis",
        "ticker": "AAPL",
        "failure": {"provider_health": {"kind": "budget_exceeded", "budget": True}},
    }
    text_only = {
        "id": "text",
        "kind": "analysis",
        "ticker": "XAUUSD",
        "failure": {"kind": "other", "message": "Budget has been exceeded"},
    }
    prices = {}
    limited_view = cost._view_run(limited, prices, tmp_path)
    overrun_view = cost._view_run(overrun, prices, tmp_path)
    text_view = cost._view_run(text_only, prices, tmp_path)
    assert limited_view["health_text"] == "被限流，不是预算超限。"
    assert overrun_view["health_text"] == "结构化字段为预算超限。"
    assert text_view["health_text"] == "没有结构化预算字段，不记为预算超限。"
    assert "结构化字段为预算超限" not in text_view["health_text"]


def test_mismatched_budget_flag_is_not_trusted():
    from webui.run_failure import clean_failure, provider_health

    assert provider_health("rate_limit", True) is None
    assert provider_health("budget_exceeded", False) is None
    clean = clean_failure({"kind": "rate_limit", "provider_health": {"kind": "rate_limit", "budget": True}})
    assert "provider_health" not in clean
    kept = clean_failure({"kind": "other", "provider_health": {"kind": "budget_exceeded", "budget": True}})
    assert kept["provider_health"] == {"kind": "budget_exceeded", "budget": True}


def test_failure_file_outside_the_console_home_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = outside / "run.json"
    result.write_text("{}", encoding="utf-8")
    (outside / "run.failure.json").write_text(json.dumps({
        "kind": "other",
        "provider_health": {"kind": "budget_exceeded", "budget": True},
    }), encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    record = {"id": "escaped", "kind": "analysis", "ticker": "600519", "result": str(result)}
    assert cost._view_run(record, {}, home)["health_text"] == ""


def test_failure_file_outside_logs_is_not_read_even_inside_the_home(tmp_path):
    home = tmp_path / "home"
    stray = home / "notes"
    stray.mkdir(parents=True)
    result = stray / "run.json"
    result.write_text("{}", encoding="utf-8")
    (stray / "run.failure.json").write_text(json.dumps({
        "provider_health": {"kind": "budget_exceeded", "budget": True},
    }), encoding="utf-8")
    logs = home / "logs"
    logs.mkdir()
    kept = logs / "ok.json"
    kept.write_text("{}", encoding="utf-8")
    (logs / "ok.failure.json").write_text(json.dumps({
        "provider_health": {"kind": "rate_limit", "budget": False},
    }), encoding="utf-8")
    assert cost._view_run({"id": "stray", "kind": "analysis", "ticker": "600519", "result": str(result)}, {}, home)["health_text"] == ""
    assert cost._view_run({"id": "ok", "kind": "analysis", "ticker": "AAPL", "result": str(kept)}, {}, home)["health_text"] == "被限流，不是预算超限。"


def test_build_failure_stores_structured_health():
    from webui.run_failure import build_failure

    limited = build_failure(_Exc(status_code=429, body={"error": {"type": "budget_exceeded"}}))
    assert limited["provider_health"] == {"kind": "rate_limit", "budget": False}
    overrun = build_failure(_Exc(type="budget_exceeded", status_code=400))
    assert overrun["provider_health"] == {"kind": "budget_exceeded", "budget": True}
    text = build_failure(Exception("Budget has been exceeded"))
    assert "provider_health" not in text


def test_unassigned_usage_is_not_priced_as_a_full_match(tmp_path):
    (tmp_path / "cost-prices.json").write_text(json.dumps({
        "models": {"gpt-4.1-mini": {"input_per_million": "0.40", "output_per_million": "1.60", "currency": "USD"}}
    }), encoding="utf-8")
    calls = [
        {"role": "Market Analyst", "input_tokens": 100, "output_tokens": 20},
        {"role": "", "input_tokens": 50, "output_tokens": 10},
    ]
    record = {
        "id": "partial",
        "kind": "analysis",
        "ticker": "600519",
        "selections": _selections(),
        "role_models": cost.role_snapshot(_selections()),
        "usage": {"calls": calls, "reported": {"input_tokens": 150, "output_tokens": 30}},
    }
    view = cost._view_run(record, cost.load_prices(tmp_path))
    assert view["matches_provider"] is True
    assert "没有归到" in view["match_text"]
    assert view["api_text"] == "不显示 API 合计：用量未全部归集。"
    assert "没有 API 用量" not in view["api_text"]
    assert "USD" not in view["api_text"]
    priced = [role for role in view["roles"] if role.get("prompt_count")]
    assert priced
    assert all(role["cost_text"] == "不计入合计" for role in priced)
    assert "USD" not in json.dumps(priced, ensure_ascii=False)
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "role.cost_text" in script

def test_mismatched_count_does_not_claim_there_is_no_api_usage():
    record = {
        "id": "mismatch",
        "kind": "analysis",
        "ticker": "600519",
        "selections": _selections(),
        "usage": {
            "calls": [{"role": "Market Analyst", "input_tokens": 10, "output_tokens": 2}],
            "reported": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    view = cost._view_run(record, {})
    assert view["matches_provider"] is False
    assert view["api_text"] == "不显示 API 合计：与供应商计数不一致。"
    assert "没有 API 用量" not in view["api_text"]
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function costApiLine" in script
    assert "costApiLine(run.api_text)" in script
    assert "API ${escapeHtml(run.api_text" not in script

def test_subscription_only_usage_is_not_an_api_total():
    record = {
        "id": "sub-only",
        "kind": "analysis",
        "ticker": "600519",
        "selections": _selections(),
        "usage": {
            "calls": [{"role": "Research Manager", "input_tokens": 50, "output_tokens": 10}],
            "reported": {"input_tokens": 50, "output_tokens": 10},
        },
    }
    view = cost._view_run(record, {})
    assert view["api_text"] == "没有 API 用量"
    assert view["subscription_text"] == "订阅，不按次计费"



def test_unknown_billing_is_not_called_api_usage():
    record = {
        "id": "unknown-bill",
        "kind": "analysis",
        "ticker": "AAPL",
        "role_models": {"roles": [{"role": "Market Analyst", "zh": "市场分析师", "tier": "quick", "provider": "openai", "model": "gpt-4.1-mini"}]},
        "usage": {
            "calls": [{"role": "Market Analyst", "input_tokens": 20, "output_tokens": 4}],
            "reported": {"input_tokens": 20, "output_tokens": 4},
        },
    }
    view = cost._view_run(record, {"gpt-4.1-mini": {"input": 1, "output": 2, "currency": "USD"}})
    assert view["api_text"] == "计费方式未知，不把它当成 API 用量。"
    assert "没有 API 用量" not in view["api_text"]
    role = view["roles"][0]
    assert role["cost_text"] == "计费方式未知"
    assert role["billing"] is None


def test_provider_without_an_effort_field_is_not_called_unset():
    grok = {
        "analysts": ["market"],
        "llm_provider": "grok",
        "quick_think_provider": "grok",
        "deep_think_provider": "grok",
        "quick_think_llm": "grok-4",
        "deep_think_llm": "grok-4",
    }
    snap = cost.role_snapshot(grok)
    market = next(role for role in snap["roles"] if role["role"] == "Market Analyst")
    assert market["effort"] == ""
    assert cost.effort_label(market["provider"], market["effort"], market["effort_field"]) == "无独立强度"
    assert cost.effort_label("openai", "", "openai_reasoning_effort") == "强度未设置"
    assert cost.effort_label("openai", "medium", "openai_reasoning_effort") == "medium"
    record = {"id": "grok-run", "kind": "analysis", "ticker": "600519", "selections": grok, "role_models": snap}
    view = cost._view_run(record, {})
    shown = next(role for role in view["roles"] if role["role"] == "Market Analyst")
    assert shown["effort_label"] == "无独立强度"
    assert shown["effort_label"] != "强度未设置"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function costEffortLabel" in script
    assert "role.effort_label" in script
    assert 'role.effort ? " · " + role.effort : " · 强度未设置"' not in script
    assert "强度未知，不写成无独立强度" in script


def test_roles_without_calls_are_not_called_unassigned():
    selections = {
        "analysts": ["market"],
        "llm_provider": "grok",
        "quick_think_provider": "grok",
        "deep_think_provider": "grok",
        "quick_think_llm": "grok-4",
        "deep_think_llm": "grok-4",
    }
    record = {
        "id": "no-calls",
        "kind": "analysis",
        "ticker": "600519",
        "selections": selections,
        "role_models": cost.role_snapshot(selections),
    }
    view = cost._view_run(record, {})
    assert all(role["usage_label"] == "未调用" for role in view["roles"])
    assert all(role["cost_text"] == "未调用" for role in view["roles"])
    assert all("未归集" not in role["cost_text"] for role in view["roles"])
    mismatched = {
        "id": "mismatch-label",
        "kind": "analysis",
        "ticker": "AAPL",
        "selections": _selections(),
        "usage": {
            "calls": [{"role": "Market Analyst", "input_tokens": 10, "output_tokens": 2}],
            "reported": {"input_tokens": 1, "output_tokens": 1},
        },
    }
    bad = cost._view_run(mismatched, {})
    idle = next(role for role in bad["roles"] if role["role"] == "Trader")
    assert idle["usage_label"] == "用量未归集"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function costMissingLabel" in script
    assert "role.usage_label" in script
    assert "用量未知，不写成未调用" in script
    assert '|| "未调用"' not in script
    assert "用量未归集</span>" not in script
