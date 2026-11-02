"""RUN-003c (B3): W43, W49, W51, W56, W58, W59, W64, W67, W44 UI."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from tradingagents.portfolio import CostedPortfolioContext, PortfolioContext
from webui import frameworks, service

SCRIPT = Path("webui/static/app.js").read_text(encoding="utf-8")


# ---- W51 cash
@pytest.mark.parametrize("cash", [True, False, -1, -0.01, "100", float("inf")])
def test_cash_refuses_bools_strings_negatives(cash):
    with pytest.raises(ValidationError):
        PortfolioContext.model_validate({"cash": cash})


@pytest.mark.parametrize("cash", [0, 0.0, 10, 2.5, None])
def test_cash_accepts_zero_and_positive_numbers(cash):
    assert PortfolioContext.model_validate({"cash": cash}).cash == cash


# ---- W49 Chinese portfolio errors
@pytest.mark.parametrize("book, words", [
    ({"positions": [{"ticker": "NVDA", "quantity": "many"}]}, "第 1 个持仓的数量（quantity）"),
    ({"cash": -5}, "现金（cash）需要是不小于 0 的数字"),
    ({"positions": [{"ticker": "A"}]}, "第 1 个持仓需要填写数量或成本价"),
    ({"positions": [{"ticker": "A", "average_price": 0}]}, "成本价（average_price）需要是大于 0 的数字"),
    ({"positions": {}}, "持仓列表（positions）需要是数组"),
    ([1, 2], "持仓需要是一个 JSON 对象"),
])
def test_portfolio_errors_are_chinese_without_pydantic_text(book, words):
    with pytest.raises(ValidationError) as caught:
        PortfolioContext.model_validate(book)
    text = service.portfolio_unusable(caught.value)
    assert words in text and text.startswith("持仓内容不可用：") and text.endswith("没有启动任何分析。")
    for raw in ("validation error", "input_value", "pydantic", "Input should", "type="):
        assert raw not in text


def test_form_holdings_missing_cost_says_so():
    with pytest.raises(ValidationError) as caught:
        CostedPortfolioContext.model_validate({"positions": [{"ticker": "A", "quantity": 3}]})
    assert "第 1 个持仓缺少成本价（average_price）" in service.portfolio_unusable(caught.value)


def test_validate_analysis_reports_the_chinese_problem(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    with pytest.raises(service.ConsoleError) as caught:
        service.validate_analysis({
            "ticker": "NVDA", "date": "2026-10-29", "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol",
            "portfolio": {"cash": True, "positions": []},
        })
    assert "现金（cash）" in str(caught.value) and "pydantic" not in str(caught.value)


# ---- W56 (documented lenient default)
def test_cost_rule_differs_by_source_as_documented(monkeypatch):
    monkeypatch.setattr(service, "_require_credential", lambda provider: None)
    base = {"ticker": "NVDA", "date": "2026-10-29", "llm_provider": "chatgpt",
            "quick_think_llm": "gpt-6-luna", "deep_think_llm": "gpt-6-sol",
            "portfolio": {"positions": [{"ticker": "NVDA", "quantity": 3}]}}
    service.validate_analysis({**base, "portfolio_source": "json"})
    service.validate_analysis(base)
    with pytest.raises(service.ConsoleError, match="缺少成本价"):
        service.validate_analysis({**base, "portfolio_source": "form"})
    assert "W56 成本价规则" in Path("webui/API-NOTES.md").read_text(encoding="utf-8")


# ---- W67
def test_unregistered_framework_is_named_unknown():
    assert frameworks.framework_fields({"framework": "gone-tool"})["framework_name"] == "未知工具（gone-tool）"
    assert frameworks.framework_name("x" * 100).endswith("…）")
    assert frameworks.framework_fields({})["framework_name"] != "未知工具（tradingagents）"


# ---- W58
def test_unverified_process_refusal_gives_a_next_step():
    assert "下一步：" in service.PROCESS_UNVERIFIED_SENTENCE
    assert "没有发送任何信号" in service.PROCESS_UNVERIFIED_SENTENCE


# ---- W43
def _records(n, status="finished"):
    return [{"id": f"r{i:03d}", "status": status, "started_at": f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}",
             "selections": {"ticker": "NVDA", "analysis_date": "2026-10-01"}} for i in range(n)]


def test_counts_cover_every_run_and_the_list_says_it_is_capped():
    records = _records(40) + _records(3, "failed")
    for i, r in enumerate(records[-3:]):
        r["id"] = f"f{i}"
    positions, shown, counts, listing = service._run_listing(service.order_runs(records))
    assert len(shown) == service.RECENT_RUN_LIMIT == 30
    assert counts["all"] == 43 and counts["failed"] == 3 and counts["finished"] == 40
    assert listing == {"shown": 30, "total": 43, "limit": 30, "truncated": True}


def test_a_short_list_is_not_marked_truncated():
    _, shown, counts, listing = service._run_listing(_records(5))
    assert len(shown) == 5 and counts["all"] == 5 and listing["truncated"] is False


def test_queue_positions_count_queued_runs_beyond_the_cap():
    records = _records(35, "queued")
    positions, _, counts, _ = service._run_listing(records)
    assert counts["queued"] == 35 and max(positions.values()) == 35


# ---- W64 / W59 / W44 UI (static checks; behaviour in the DOM suite)
def test_welcome_has_no_tauric_link():
    assert '">Tauric Research</a>' not in SCRIPT and "welcome.credit_url" not in SCRIPT


def test_gpt5_temperature_note_and_backtest_refusal_are_wired():
    assert "function gpt5TemperatureNote" in SCRIPT and 'id="gpt5-temperature-note"' in SCRIPT
    assert "回测不排队" in SCRIPT and 'out.setAttribute("role", "alert")' in SCRIPT
    assert "function portfolioProblems" in SCRIPT and 'id="runs-cap"' in SCRIPT
