"""V2-COST-STATS fixtures (no spend): usage files and a price file for the cost page.

Fake numbers only: the token counts and the per-million prices below are test data,
not any provider's usage or price. Used by tests and the screenshot harness.
"""

from __future__ import annotations

import json
from pathlib import Path

PENDING = "cost_usage_pending"     # usage recorded, no provider total yet (today's state)
CHECKED = "cost_usage_checked"     # same, after a matching provider total is written (REGRESSION-01)
SELECTIONS = {"analysts": ["market", "social", "news", "fundamentals"], "llm_provider": "deepseek",
              "quick_think_llm": "deepseek-chat", "deep_think_llm": "deepseek-reasoner", "show": False}
FIXTURE_PRICES = {"models": {"deepseek-chat": {"input_per_million": "0.50", "output_per_million": "1.50",
                                               "currency": "USD"}},
                  "_note": "fixture prices for tests and screenshots, not a provider price"}


def _stats(base: int, unknown: int) -> dict:
    from webui import cost

    calls = []
    for number, role in enumerate(entry["role"] for entry in cost.role_snapshot(SELECTIONS)["roles"]):
        if role == "Memory Log":
            continue
        tier_model = "deepseek-reasoner" if role in cost.DEEP_ROLES else "deepseek-chat"
        calls.append({"role": role, "node": "agent" if role.endswith("Analyst") and number < 4 else role,
                      "model": tier_model, "response_id": f"fixture-{base}-{number}",
                      "input_tokens": base + 10 * number, "output_tokens": base // 10 + number,
                      "total_tokens": base + 10 * number + base // 10 + number,
                      "input_token_details": {"cache_read": number}, "output_token_details": {}})
    return {"calls": calls, "llm_calls": len(calls) + unknown, "usage_reported": len(calls),
            "usage_unknown": unknown, "tokens_in": sum(c["input_tokens"] for c in calls),
            "tokens_out": sum(c["output_tokens"] for c in calls)}


def build(home: Path, prices: bool = True) -> dict:
    """Two finished runs with usage files under ``home`` (TRADINGAGENTS_WEBUI_HOME)."""
    from webui import cost, service

    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    for run_id, ticker, date, base, unknown, checked in (
        (PENDING, "NVDA", "2026-11-13", 1200, 1, False),
        (CHECKED, "AAPL", "2026-11-12", 900, 0, True),
    ):
        selections = {**SELECTIONS, "ticker": ticker, "analysis_date": date}
        result = logs / f"{run_id}.result.json"
        result.write_text(json.dumps({"decision": "Hold"}), encoding="utf-8")
        log = logs / f"{run_id}.log"
        log.write_text("fixture\n", encoding="utf-8")
        stats = _stats(base, unknown)
        path = cost.write_usage(result, stats)
        if checked:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["reported"] = {"input_tokens": stats["tokens_in"], "output_tokens": stats["tokens_out"]}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        record = service._record_for(run_id, selections, log, result, kind="analysis")
        record.update(status="finished", returncode=0, started_at=f"{date}T11:00:00")
        cost.freeze_role_models(record, selections)
        service.JOBS.write_run(record)
    if prices:
        (home / cost.PRICES_NAME).write_text(json.dumps(FIXTURE_PRICES, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    return {"pending": PENDING, "checked": CHECKED}


# V2-COST-PRICING: a cache-priced CNY run and a run that mixes USD and CNY (fake prices).
CACHE_PRICED = "cost_cache_priced"
MIXED = "cost_mixed_currency"
PRICING_PRICES = {
    "_note": "fixture prices for tests and screenshots, not a provider price",
    "models": {
        "deepseek-chat": {"input_per_million": "2", "cache_read_per_million": "0.5",
                          "output_per_million": "8", "currency": "CNY"},
        "deepseek-reasoner": {"input_per_million": "4", "output_per_million": "16", "currency": "CNY"},
        "gpt-4.1-mini": {"input_per_million": "0.40", "output_per_million": "1.60", "currency": "USD"},
    },
}


def _pricing_stats(selections: dict) -> dict:
    """Every role called once: 2000 input tokens (1500 cache hits), 200 output."""
    from webui import cost

    calls = []
    for entry in cost.role_snapshot(selections)["roles"]:
        if entry["role"] == "Memory Log":
            continue
        calls.append({"role": entry["role"], "node": entry["role"], "model": entry["model"],
                      "response_id": f"fixture-{entry['role']}", "input_tokens": 2000, "output_tokens": 200,
                      "total_tokens": 2200, "input_token_details": {"cache_read": 1500},
                      "output_token_details": {}})
    return {"calls": calls, "llm_calls": len(calls), "usage_reported": len(calls), "usage_unknown": 0,
            "tokens_in": sum(c["input_tokens"] for c in calls), "tokens_out": sum(c["output_tokens"] for c in calls)}


def build_pricing(home: Path, prices: bool = True) -> dict:
    """Two reconciled runs under ``home``: all-CNY with cache prices, and USD + CNY."""
    from webui import cost, service

    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    base = {"analysts": ["market"], "show": False}
    runs = (
        (CACHE_PRICED, "600519", "2026-11-14", {**base, "llm_provider": "deepseek",
                                                "quick_think_llm": "deepseek-chat", "deep_think_llm": "deepseek-reasoner"}),
        (MIXED, "NVDA", "2026-11-14", {**base, "llm_provider": "deepseek", "quick_think_provider": "openai",
                                       "quick_think_llm": "gpt-4.1-mini", "deep_think_llm": "deepseek-reasoner"}),
    )
    for number, (run_id, ticker, date, selections) in enumerate(runs):
        selections = {**selections, "ticker": ticker, "analysis_date": date}
        result = logs / f"{run_id}.result.json"
        result.write_text(json.dumps({"decision": "Hold"}), encoding="utf-8")
        log = logs / f"{run_id}.log"
        log.write_text("fixture\n", encoding="utf-8")
        stats = _pricing_stats(selections)
        path = cost.write_usage(result, stats)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["reported"] = {"input_tokens": stats["tokens_in"], "output_tokens": stats["tokens_out"]}
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        record = service._record_for(run_id, selections, log, result, kind="analysis")
        record.update(status="finished", returncode=0, started_at=f"{date}T1{2 - number}:00:00")
        cost.freeze_role_models(record, selections)
        service.JOBS.write_run(record)
    if prices:
        (home / cost.PRICES_NAME).write_text(json.dumps(PRICING_PRICES, ensure_ascii=False, indent=2),
                                             encoding="utf-8")
    return {"cache_priced": CACHE_PRICED, "mixed": MIXED}
