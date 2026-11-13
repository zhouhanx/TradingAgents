"""V2-COST-PRICING-2: exact run total (W251), cache_read fallback (W252), docs (W254).

Fake prices and fake usage only. The DeepSeek adapter runs through a mock HTTP
transport (tests/test_v2_cost_pricing.py): no network, no key.
"""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli import stats_handler
from tests.test_v2_cost_pricing import CURRENT, LEGACY, _deepseek_call
from webui import cost

SELECTIONS = {"analysts": ["market", "social", "news", "fundamentals"], "llm_provider": "deepseek",
              "quick_think_llm": "m", "deep_think_llm": "m"}
PRICE = {"input_per_million": "0.27", "cache_read_per_million": "0.07", "output_per_million": "1.1",
         "currency": "CNY"}
INVALID = stats_handler.CACHE_SOURCE_HIT_TOKENS_AFTER_INVALID


def _run(calls: list[dict]) -> dict:
    return {"id": "r", "kind": "analysis", "ticker": "X", "selections": SELECTIONS,
            "role_models": cost.role_snapshot(SELECTIONS),
            "usage": {"calls": calls, "reported": {"input_tokens": sum(c["input_tokens"] for c in calls),
                                                   "output_tokens": sum(c["output_tokens"] for c in calls)}}}


def _nine_calls() -> list[dict]:
    roles = [r["role"] for r in cost.role_snapshot(SELECTIONS)["roles"] if r["role"] != "Memory Log"][:9]
    assert len(roles) == 9
    # each role's exact amount ends in 0.00000027 (1201 uncached tokens x 0.27), rounded away per role
    return [{"role": role, "input_tokens": 1201 + 100 * n, "output_tokens": 300 + 10 * n,
             "input_token_details": {"cache_read": 100 * n}} for n, role in enumerate(roles)]


def _exact(call: dict, price: dict) -> Decimal:
    hits = call["input_token_details"]["cache_read"]
    return (Decimal(call["input_tokens"] - hits) * Decimal(price["input_per_million"])
            + Decimal(hits) * Decimal(price["cache_read_per_million"])
            + Decimal(call["output_tokens"]) * Decimal(price["output_per_million"])) / Decimal(1_000_000)


def _text(amount: Decimal) -> str:
    return cost._money_text(amount.quantize(cost.TOLERANCE_COST), "CNY")


@pytest.mark.unit
def test_w251_nine_roles_total_is_the_exact_sum_rounded_once():
    calls = _nine_calls()
    view = cost._view_run(_run(calls), {"m": PRICE})
    exact = sum((_exact(call, PRICE) for call in calls), Decimal(0))
    old = sum((_exact(call, PRICE).quantize(cost.TOLERANCE_COST) for call in calls), Decimal(0))
    assert old != exact.quantize(cost.TOLERANCE_COST)      # the old method drifted on this run
    assert exact - old == Decimal("0.00000243")              # 9 x 0.00000027, like QA's 0.00000276
    assert view["api_text"] == _text(exact)
    # each role is still shown rounded on its own
    rows = {row["role"]: row for row in view["roles"]}
    for call in calls:
        assert rows[call["role"]]["cost_text"] == _text(_exact(call, PRICE))


@pytest.mark.unit
def test_w251_sub_unit_amounts_add_up_instead_of_vanishing():
    calls = [{**call, "input_tokens": 1, "output_tokens": 0, "input_token_details": {}} for call in _nine_calls()]
    view = cost._view_run(_run(calls), {"m": {**PRICE, "input_per_million": "0.4"}})
    # each role 0.0000004 -> shows 0; the old total said 0, the exact total is 0.0000036 -> 0.000004
    assert {row["cost_text"] for row in view["roles"] if row["prompt_count"]} == {"0 CNY"}
    assert view["api_text"] == "0.000004 CNY"


@pytest.mark.unit
def test_w251_money_still_rounds_one_role():
    assert cost._money(1, 0, {"input_per_million": Decimal("0.4"), "output_per_million": Decimal(0)}) == 0
    assert cost._exact_money(1, 0, {"input_per_million": Decimal("0.4"),
                                    "output_per_million": Decimal(0)}) == Decimal("0.0000004")


@pytest.mark.unit
@pytest.mark.parametrize("cached", [-5, 101, 500, 2.5])
def test_w252_invalid_cached_tokens_fall_back_to_hit_tokens(cached):
    row = _deepseek_call({**CURRENT, "prompt_tokens_details": {"cached_tokens": cached}})
    assert row["input_token_details"] == {"cache_read": 64}       # one count, not cached + hits
    assert row["cache_read_source"] == INVALID
    assert row["input_tokens"] == 100


@pytest.mark.unit
def test_w252_a_valid_cached_tokens_still_wins_and_is_not_marked():
    row = _deepseek_call({**CURRENT, "prompt_tokens_details": {"cached_tokens": 30}})
    assert row["input_token_details"] == {"cache_read": 30}
    assert "cache_read_source" not in row
    row = _deepseek_call({**CURRENT, "prompt_tokens_details": {"cached_tokens": 0}})
    assert row["input_token_details"] == {"cache_read": 0} and "cache_read_source" not in row


@pytest.mark.unit
def test_w252_hit_tokens_alone_keep_the_plain_source():
    assert _deepseek_call(LEGACY)["cache_read_source"] == stats_handler.CACHE_SOURCE_HIT_TOKENS


@pytest.mark.unit
@pytest.mark.parametrize("hits", [-1, 101, 2.5, None])
def test_w252_no_valid_fallback_keeps_what_the_provider_sent(hits):
    usage = {**CURRENT, "prompt_tokens_details": {"cached_tokens": -5}, "prompt_cache_hit_tokens": hits}
    row = _deepseek_call(usage)
    assert row["input_token_details"] == {"cache_read": -5}
    assert "cache_read_source" not in row
    # the cost page prices that as 0 hits: all input at the input price (W254)
    call = {"role": "Market Analyst", "input_tokens": 100, "output_tokens": 10,
            "input_token_details": row["input_token_details"]}
    view = cost._view_run(_run([call]), {"m": {**PRICE, "input_per_million": "1", "cache_read_per_million": "0.1",
                                               "output_per_million": "2"}})
    assert view["api_text"] == "0.00012 CNY"


def _details_for(input_details, raw_usage, input_tokens=100):
    usage = {"input_tokens": input_tokens, "output_tokens": 1, "total_tokens": input_tokens + 1}
    if input_details is not None:
        usage["input_token_details"] = input_details
    message = SimpleNamespace(response_metadata={"token_usage": raw_usage})
    return stats_handler._input_details(usage, message, SimpleNamespace(llm_output=None))


@pytest.mark.unit
@pytest.mark.parametrize("cached", ["64", True, 2.5, -1, 101, [64], {"n": 64}])
def test_w252_non_integer_or_out_of_range_cache_read_falls_back(cached):
    details, source = _details_for({"cache_read": cached, "audio": 3}, {"prompt_cache_hit_tokens": 40})
    assert details == {"cache_read": 40, "audio": 3}
    assert source == INVALID


@pytest.mark.unit
def test_w252_edges_of_the_valid_range():
    assert _details_for({"cache_read": 100}, {"prompt_cache_hit_tokens": 40}) == ({"cache_read": 100}, "")
    assert _details_for({"cache_read": -1}, {"prompt_cache_hit_tokens": 100}) == ({"cache_read": 100}, INVALID)
    assert _details_for({"cache_read": -1}, {"prompt_cache_hit_tokens": 0}) == ({"cache_read": 0}, INVALID)
    assert _details_for({"cache_read": None}, {"prompt_cache_hit_tokens": 7}) == (
        {"cache_read": 7}, stats_handler.CACHE_SOURCE_HIT_TOKENS)
    assert _details_for(None, {"prompt_cache_hit_tokens": 7}) == ({"cache_read": 7}, stats_handler.CACHE_SOURCE_HIT_TOKENS)


@pytest.mark.unit
def test_w254_a_null_cache_price_is_the_same_as_none():
    call = {"role": "Market Analyst", "input_tokens": 1000, "output_tokens": 100,
            "input_token_details": {"cache_read": 600}}
    base = {"input_per_million": "1", "output_per_million": "2", "currency": "USD"}
    with_null = cost._view_run(_run([call]), {"m": {**base, "cache_read_per_million": None}})
    without = cost._view_run(_run([call]), {"m": base})
    assert cost._price("m", {"m": {**base, "cache_read_per_million": None}}) == cost._price("m", {"m": base})
    assert with_null["api_text"] == without["api_text"] == "0.0012 USD"
    row = next(r for r in with_null["roles"] if r["role"] == "Market Analyst")
    assert row["price_note"] == "缓存命中按输入单价计"


@pytest.mark.unit
def test_w254_the_docs_say_so():
    notes = Path("webui/API-NOTES.md").read_text(encoding="utf-8")
    assert "`cache_read_per_million: null` 等同于没写" in notes
    assert "按 0 个命中计价" in notes and INVALID in notes and "只舍入一次" in notes
    assert '"cache_read_per_million": null' in cost.__doc__ and "priced as 0 hits" in cost.__doc__
