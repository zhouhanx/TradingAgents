"""RUN-004 harness: a stand-in for ``python -m webui.worker`` that calls no model.

The console spawns it exactly like the real worker (own session, log file as
stdout). It uses the real checkpoint code: it seeds its run's checkpoint with
``service.seed_run_checkpoint`` (what ``default_runner`` does) and writes a
small LangGraph checkpoint into its own run database through
``checkpointer.get_checkpointer(..., scope)``.

Behaviour per ticker comes from ``$RUN004_CONTROL/<TICKER>.json``::

    {"outcome": "finish" | "fail", "done": 3}

The worker prints its progress, then waits until ``$RUN004_CONTROL/release-<run id>``
or ``$RUN004_CONTROL/release-all`` exists (at most 120 s), then finishes or fails.

A submitted holding goes through what the real runner does with it
(``PortfolioContext.model_validate``, ``fingerprint`` for the checkpoint
signature, ``render`` for the agents' prompt); the rendered block is written into
the result so the harness can check what the agents would have seen.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import TypedDict

AGENTS = ["Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst", "Bull Researcher"]


class _State(TypedDict):
    n: int


def _write_checkpoint(data_dir: str, ticker: str, date: str, scope: str) -> None:
    from langgraph.graph import END, START, StateGraph

    from tradingagents.graph import checkpointer

    graph = StateGraph(_State)
    graph.add_node("first", lambda state: {"n": state["n"] + 1})
    graph.add_node("second", lambda state: {"n": state["n"] + 1})
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)
    with checkpointer.get_checkpointer(data_dir, ticker, scope) as saver:
        app = graph.compile(checkpointer=saver)
        app.invoke({"n": 0}, {"configurable": {"thread_id": checkpointer.thread_id(ticker, date)}})


def main(argv: list[str]) -> int:
    from tradingagents.graph import checkpointer
    from webui import service

    job = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    selections = job["selections"]
    ticker, date = selections["ticker"], selections["analysis_date"]
    control = Path(os.environ["RUN004_CONTROL"])
    plan_file = control / f"{ticker}.json"
    plan = json.loads(plan_file.read_text(encoding="utf-8")) if plan_file.is_file() else {}
    scope = job.get("checkpoint_scope")
    print(f"FAKE\tstart\t{job['id']}\t{ticker}", flush=True)
    from tradingagents.portfolio import PortfolioContext

    book = PortfolioContext.model_validate(selections["portfolio"]) if selections.get("portfolio") else None
    portfolio_context = book.render(ticker) if book is not None else ""
    print(f"PORTFOLIO\t{book.fingerprint() if book is not None else 'none'}", flush=True)
    if scope and service.console_checkpoint_enabled(selections):
        data_dir = service._selections_cache_dir(selections)
        seeded = service.seed_run_checkpoint(data_dir, job)
        before = checkpointer.checkpoint_step(data_dir, ticker, date, "", scope)
        print(f"CHECKPOINT\t{'resume' if before is not None else 'fresh'}\t{before}\tseeded={seeded}", flush=True)
        _write_checkpoint(data_dir, ticker, date, scope)
        print(f"CHECKPOINT\tsaved\t{checkpointer.db_file(data_dir, ticker, scope)}", flush=True)
    done = int(plan.get("done", 2))
    stamp = time.strftime("%H:%M:%S")
    for agent in AGENTS[:done]:
        print(f"STATUS\t{agent}\tin_progress\t{stamp}", flush=True)
        print(f"STATUS\t{agent}\tcompleted\t{stamp}", flush=True)
    if done < len(AGENTS):
        print(f"STATUS\t{AGENTS[done]}\tin_progress\t{stamp}", flush=True)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if (control / f"release-{job['id']}").exists() or (control / "release-all").exists():
            break
        time.sleep(0.05)
    if plan.get("outcome", "finish") == "fail":
        print("FAKE\tfail", flush=True)
        return 1
    Path(job["result_path"]).write_text(json.dumps({"decision": "HOLD", "rating": "Hold", "portfolio_context": portfolio_context}), encoding="utf-8")
    print("FAKE\tfinish", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
