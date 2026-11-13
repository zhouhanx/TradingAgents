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
