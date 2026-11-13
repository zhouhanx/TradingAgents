"""Two finished analysis runs for the 对比 end-to-end tests. No model is called.

The result files come from the real ``service.analysis_result_payload`` with
``show`` off, so they carry exactly what a new console run saves. The left run's
log has more steps than one replay page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, decision_record

LEFT, RIGHT = "cmp_e2e_left", "cmp_e2e_right"
TEAM = ("Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst")
DEBATE = ("Bull Researcher", "Bear Researcher")
TAIL = ("Research Manager", "Trader", "Aggressive Analyst", "Neutral Analyst",
        "Conservative Analyst", "Portfolio Manager")


def _clock(index: int) -> str:
    seconds = 10 * 3600 + index * 7
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def long_log(rounds: int = 18) -> list[str]:
    """Analysts, many debate turns, then the rest: more than one 40-step page."""
    order = [*TEAM, *(DEBATE * rounds), *TAIL]
    lines, index = [], 0
    for agent in order:
        for status in ("in_progress", "completed"):
            lines.append(f"STATUS\t{agent}\t{status}\t{_clock(index)}")
            index += 1
    return lines


def _state(ticker: str, rating: PortfolioRating, summary: str, evidence: list[str]) -> dict:
    decision = PortfolioDecision(rating=rating, executive_summary=summary,
                                 investment_thesis="见证据。", evidence=evidence)
    return {
        "market_report": f"{ticker} 行情：均线多头排列。",
        "news_report": f"{ticker} 新闻：本周无重大公告。",
        "investment_debate_state": {"bull_history": f"{ticker} 多头观点", "bear_history": f"{ticker} 空头观点",
                                    "judge_decision": f"{ticker} 研究经理结论"},
        "trader_investment_plan": f"{ticker} 交易计划：分批建仓。",
        "final_trade_decision": f"{ticker} 组合经理：{rating.value}",
        "final_rating": rating.value,
        "decision_record": decision_record(decision, rating.value),
    }


def build(home: Path) -> dict:
    """Write both runs under ``home`` (TRADINGAGENTS_WEBUI_HOME must point there)."""
    from webui import service
    from webui.compare import freeze_prompt_versions

    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    notice = {"report": None, "saved_to": None, "html": None, "save_error": None, "saved": False, "save_lines": []}
    runs = (
        (LEFT, "NVDA", "2026-11-12", PortfolioRating.BUY, "逢回调加仓。", ["ATR 4.2", "成交量放大 35%"], long_log()),
        (RIGHT, "600519", "2026-11-11", PortfolioRating.HOLD, "维持仓位。", ["PE 位于五年中位"], long_log(rounds=1)),
    )
    for run_id, ticker, date, rating, summary, evidence, lines in runs:
        selections = {"ticker": ticker, "analysis_date": date, "show": False,
                      "analysts": ["market", "social", "news", "fundamentals"]}
        state = _state(ticker, rating, summary, evidence)
        payload = service.analysis_result_payload(state, rating.value, notice, selections, logs / "none.log")
        result = logs / f"{run_id}.result.json"
        result.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log = logs / f"{run_id}.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        record = service._record_for(run_id, selections, log, result, kind="analysis")
        record["status"] = "finished"
        record["returncode"] = 0
        freeze_prompt_versions(record)
        service.JOBS.write_run(record)
    return {"left": LEFT, "right": RIGHT, "left_steps": len(long_log()), "right_steps": len(long_log(rounds=1))}


def dump(home: Path) -> dict:
    """The JSON the page would get from /api/compare for these runs."""
    from webui import compare

    build(home)
    replay = {}
    first = compare.replay_run(LEFT)
    for page in range(1, first["pages"] + 1):
        replay[str(page)] = compare.replay_run(LEFT, page=page)
    return {
        "list": compare.list_compare(),
        "pair": compare.compare_pair(LEFT, RIGHT),
        "replay": replay,
    }


if __name__ == "__main__":  # used by the DOM test: python -m tests.v2_compare_fixtures HOME
    print(json.dumps(dump(Path(sys.argv[1])), ensure_ascii=False))
