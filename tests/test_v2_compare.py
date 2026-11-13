"""V2-REPORT-COMPARE: saved prompt versions and structured evidence only."""

import json
from pathlib import Path

from tradingagents.agents.schemas import PortfolioDecision, PortfolioRating, decision_record
from webui import compare


def test_free_text_does_not_become_evidence():
    recorded = decision_record(None, "Hold")
    assert recorded["source"] == "free_text"
    assert recorded["evidence"] == []
    structured = decision_record(PortfolioDecision(
        rating=PortfolioRating.BUY,
        executive_summary="加仓。",
        investment_thesis="见引用。",
        evidence=["ATR 4.2", ""],
    ), "Buy")
    assert structured["evidence"] == ["ATR 4.2"]
    assert structured["source"] == "structured"


def test_replay_uses_saved_prompt_not_current(tmp_path):
    saved = {"id": "portfolio_manager", "sha256": "old", "text": "保存的提示词版本"}
    runs = tmp_path / "runs"
    logs = tmp_path / "logs"
    runs.mkdir()
    logs.mkdir()
    log = logs / "cmp-a.log"
    log.write_text("STATUS\tMarket Analyst\tcompleted\t10:01:02\nSTATUS\tPortfolio Manager\tcompleted\t10:04:08\n", encoding="utf-8")
    (runs / "cmp-a.json").write_text(json.dumps({
        "id": "cmp-a",
        "kind": "analysis",
        "ticker": "600519",
        "date": "2026-11-11",
        "log": str(log),
        "prompt_versions": [saved],
        "decision": {"rating": "Buy", "summary": "加仓。", "evidence": ["ATR 4.2"], "source": "structured"},
    }), encoding="utf-8")
    (runs / "cmp-b.json").write_text(json.dumps({
        "id": "cmp-b",
        "kind": "analysis",
        "ticker": "AAPL",
        "date": "2026-11-10",
        "decision": {"rating": "Hold", "summary": "", "evidence": [], "source": "free_text"},
    }), encoding="utf-8")
    pair = compare.compare_pair("cmp-a", "cmp-b", tmp_path)
    assert pair["left"]["evidence"] == ["ATR 4.2"]
    assert pair["right"]["evidence"] == []
    assert "不从正文猜测" in pair["right"]["evidence_note"]
    replay = compare.replay_run("cmp-a", tmp_path, current=[{"id": "portfolio_manager", "sha256": "new", "text": "当前提示词"}])
    assert replay["prompts"][0]["text"] == "保存的提示词版本"
    assert replay["prompts"][0]["changed"] is True
    missing = compare.replay_run("cmp-a", tmp_path, current=[])
    assert missing["prompts"][0]["changed"] is None
    assert missing["prompts"][0]["text"] == "保存的提示词版本"

    assert replay["uses_saved_prompts"] is True
    assert [step["agent"] for step in replay["steps"]] == ["Market Analyst", "Portfolio Manager"]


def test_start_saves_prompt_versions():
    service = Path("webui/service.py").read_text(encoding="utf-8")
    html = Path("webui/static/index.html").read_text(encoding="utf-8")
    assert "freeze_prompt_versions(record)" in service
    assert 'data-page="compare"' in html
    versions = compare.capture_prompt_versions()
    # V2-INPROGRESS-2: every role's prompt is frozen, not only the two managers.
    assert {item["id"] for item in versions} == set(compare.PROMPT_LABELS)
    assert all(len(item["sha256"]) == 64 and item["text"] for item in versions)


def test_replay_labels_known_roles_and_does_not_invent_unknown_ones(tmp_path):
    runs = tmp_path / "runs"
    logs = tmp_path / "logs"
    runs.mkdir()
    logs.mkdir()
    log = logs / "cmp-c.log"
    log.write_text("STATUS\tMarket Analyst\tcompleted\t10:01:02\n", encoding="utf-8")
    (runs / "cmp-c.json").write_text(json.dumps({
        "id": "cmp-c",
        "kind": "analysis",
        "ticker": "600519",
        "date": "2026-11-11",
        "log": str(log),
        "prompt_versions": [
            {"id": "portfolio_manager", "sha256": "old", "text": "保存的提示词版本"},
            {"id": "not_a_role", "sha256": "abc", "text": "不要翻译成角色"},
        ],
    }), encoding="utf-8")
    replay = compare.replay_run("cmp-c", tmp_path, current=[{"id": "portfolio_manager", "sha256": "old"}])
    known = next(item for item in replay["prompts"] if item["id"] == "portfolio_manager")
    unknown = next(item for item in replay["prompts"] if item["id"] == "not_a_role")
    assert known["agent"] == "Portfolio Manager"
    assert known["zh"] == "组合经理"
    assert known["text"] == "保存的提示词版本"
    assert unknown["agent"] == ""
    assert unknown["zh"] == ""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "这一步没有单独保存的提示词版本" in script
    assert "promptTitle" in script
    assert "<select" not in script


def test_result_outside_the_console_logs_is_not_read(tmp_path):
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({
        "rating": "Sell",
        "shown_report": {"sections": [{"heading": "秘密", "items": [{"text": "不应出现"}]}]},
    }), encoding="utf-8")
    inside_dir = tmp_path / "logs"
    inside_dir.mkdir()
    inside = inside_dir / "cmp-c.json"
    inside.write_text(json.dumps({
        "rating": "Buy",
        "shown_report": {"sections": [{"heading": "要点", "items": [{"text": "可以显示"}]}]},
    }), encoding="utf-8")
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "cmp-out.json").write_text(json.dumps({
        "id": "cmp-out",
        "kind": "analysis",
        "ticker": "600519",
        "date": "2026-11-11",
        "result": str(outside),
    }), encoding="utf-8")
    (runs / "cmp-in.json").write_text(json.dumps({
        "id": "cmp-in",
        "kind": "analysis",
        "ticker": "AAPL",
        "date": "2026-11-10",
        "result": str(inside),
    }), encoding="utf-8")
    pair = compare.compare_pair("cmp-out", "cmp-in", tmp_path)
    assert pair["left"]["rating"] == ""
    assert pair["left"]["sections"] == []
    assert "不应出现" not in json.dumps(pair, ensure_ascii=False)
    assert pair["right"]["rating"] == "Buy"
    assert pair["right"]["sections"][0]["text"] == "可以显示"


def test_replay_page_does_not_claim_a_match_when_the_role_is_gone():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "当前源码没有这个角色，回放仍用保存版本" in script
    assert "item.changed == null" in script


def test_empty_prompt_text_is_not_called_a_saved_replay(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "cmp-empty.json").write_text(json.dumps({
        "id": "cmp-empty",
        "kind": "analysis",
        "ticker": "600519",
        "date": "2026-11-11",
        "prompt_versions": [{"id": "portfolio_manager", "sha256": "old", "text": ""}],
    }), encoding="utf-8")
    replay = compare.replay_run("cmp-empty", tmp_path, current=[{"id": "portfolio_manager", "sha256": "old"}])
    assert replay["uses_saved_prompts"] is False
    assert "没有保存提示词正文" in replay["note"]
    assert replay["prompts"][0]["saved"] is False
    assert replay["prompts"][0]["changed"] is None
    assert replay["prompts"][0]["text"] == ""
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "没有保存提示词正文" in script
    assert "item.saved" in script

def test_same_run_is_not_two_results(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "cmp-a.json").write_text(json.dumps({
        "id": "cmp-a",
        "kind": "analysis",
        "ticker": "600519",
        "date": "2026-11-11",
        "decision": {"rating": "Buy", "summary": "加仓。", "evidence": ["ATR 4.2"], "source": "structured"},
    }), encoding="utf-8")
    (runs / "cmp-b.json").write_text(json.dumps({
        "id": "cmp-b",
        "kind": "analysis",
        "ticker": "600519",
        "date": "2026-11-10",
        "decision": {"rating": "Hold", "summary": "", "evidence": [], "source": "free_text"},
    }), encoding="utf-8")
    (runs / "cmp-c.json").write_text(json.dumps({
        "id": "cmp-c",
        "kind": "analysis",
        "ticker": "AAPL",
        "date": "2026-11-09",
    }), encoding="utf-8")
    same = compare.compare_pair("cmp-a", "cmp-a", tmp_path)
    assert same["same_run"] is True
    assert same["same_ticker"] is True
    assert same["left"]["id"] == same["right"]["id"] == "cmp-a"
    twin = compare.compare_pair("cmp-a", "cmp-b", tmp_path)
    assert twin["same_run"] is False
    assert twin["same_ticker"] is True
    other = compare.compare_pair("cmp-a", "cmp-c", tmp_path)
    assert other["same_run"] is False
    assert other["same_ticker"] is False
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    css = Path("webui/static/app.css").read_text(encoding="utf-8")
    assert "左右是同一次运行，不是两次不同结果。" in script
    assert "这是同一代码的两次运行。" in script
    assert 'id="compare-note"' in script
    assert "note.textContent = relation" in script
    assert "compare-card--solo" in script
    assert "compareLabel" in script
    assert "还没有可对比的运行。" in script
    assert "至少需要两次运行才能对比" not in script
    assert "runs.length >= 1" in script
    assert "collapseNarrowShell" in script
    assert 'narrowShell.addEventListener("change", collapseNarrowShell)' in script
    assert ".compare-card--solo" in css
    assert 'document.querySelector("#compare-grid").innerHTML = cards' in script
    assert "innerHTML = `${relation" not in script

def test_exchange_alias_is_the_same_code(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "cmp-ss.json").write_text(json.dumps({
        "id": "cmp-ss", "kind": "analysis", "ticker": "600519.SS", "date": "2026-11-10",
        "decision": {"rating": "Hold", "summary": "", "evidence": [], "source": "free_text"},
    }), encoding="utf-8")
    (runs / "cmp-bare.json").write_text(json.dumps({
        "id": "cmp-bare", "kind": "analysis", "ticker": "600519", "date": "2026-11-11",
        "decision": {"rating": "Buy", "summary": "加仓。", "evidence": ["ATR 4.2"], "source": "structured"},
    }), encoding="utf-8")
    pair = compare.compare_pair("cmp-bare", "cmp-ss", tmp_path)
    assert pair["same_run"] is False
    assert pair["same_ticker"] is True
    assert pair["left"]["ticker"] == "600519"
    assert pair["right"]["ticker"] == "600519.SS"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "这是同一代码的两次运行。" in script
    assert "watchShownPrice(oldValue)" in script
    assert "图上区间以实际K线为准" in script



def test_free_text_rating_is_not_called_a_structured_rating(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "free.json").write_text(json.dumps({
        "id": "free", "kind": "analysis", "ticker": "AAPL", "date": "2026-11-10",
        "decision": {"rating": "Hold", "summary": "看起来不错。", "evidence": [], "source": "free_text"},
    }), encoding="utf-8")
    (runs / "structured.json").write_text(json.dumps({
        "id": "structured", "kind": "analysis", "ticker": "600519", "date": "2026-11-11",
        "decision": {"rating": "Buy", "summary": "加仓。", "evidence": ["ATR 4.2"], "source": "structured"},
    }), encoding="utf-8")
    (runs / "blank.json").write_text(json.dumps({
        "id": "blank", "kind": "analysis", "ticker": "IBM", "date": "2026-11-09",
    }), encoding="utf-8")
    pair = compare.compare_pair("free", "structured", tmp_path)
    free = pair["left"]
    assert free["source"] == "missing"
    assert free["rating_label"] == "记录里有 Hold，但不把它当成结构化评级。"
    assert not free["rating_label"].startswith("评级")
    assert "看起来不错" not in free["rating_label"]
    structured = pair["right"]
    assert structured["rating_label"] == "评级 Buy"
    blank = compare.compare_pair("blank", "structured", tmp_path)["left"]
    assert blank["rating_label"] == "没有结构化评级。"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function compareRatingLine" in script
    assert "item.rating_label" in script
    assert "没有结构化要点" not in script


# --- V2-INPROGRESS-2 -----------------------------------------------------------------

def test_every_timeline_role_has_a_saved_prompt_version():
    import inspect

    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    from webui.progress import TEAMS

    versions = {item["id"]: item for item in compare.capture_prompt_versions()}
    labelled = {compare.PROMPT_LABELS[name][0] for name in versions}
    timeline = {agent for _team, _zh, agents in TEAMS for agent, _agent_zh, _analyst in agents}
    assert timeline <= labelled
    assert "You are a trading analyst reviewing your own past decision" in versions["memory_reflection"]["text"]
    assert "_build_system_message" in versions["sentiment_analyst"]["text"]
    # A run saved before every role was frozen still matches its manager hash.
    import hashlib
    old = hashlib.sha256(inspect.getsource(create_portfolio_manager).encode("utf-8")).hexdigest()
    assert versions["portfolio_manager"]["sha256"] == old


def _payload(show):
    from webui import service

    state = {"market_report": "行情正文", "final_trade_decision": "持有", "final_rating": "Hold"}
    notice = {"report": None, "saved_to": None, "html": None, "save_error": None, "saved": False, "save_lines": []}
    return service.analysis_result_payload(state, "Hold", notice, {"show": show}, Path("/tmp/missing.log"))


def test_new_runs_save_report_sections_without_show():
    hidden = _payload(False)
    assert "shown_report" not in hidden
    headings = [item["heading"] for item in hidden["report_sections"]["sections"]]
    assert headings == ["I. Analyst Team Reports", "V. Portfolio Manager Decision"]
    shown = _payload(True)
    assert shown["shown_report"] == shown["report_sections"]


def test_a_report_section_error_does_not_cost_the_result_without_show(monkeypatch):
    from webui import service

    def broken(_state):
        raise ValueError("bad section")

    monkeypatch.setattr(service, "shown_report", broken)
    payload = _payload(False)
    assert "report_sections" not in payload and payload["decision"] == "Hold"


def _seed(tmp_path, run_id, payload, lines=None):
    runs, logs = tmp_path / "runs", tmp_path / "logs"
    runs.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)
    result = logs / f"{run_id}.result.json"
    result.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    record = {"id": run_id, "kind": "analysis", "ticker": "NVDA", "date": "2026-11-12", "result": str(result)}
    if lines is not None:
        log = logs / f"{run_id}.log"
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        record["log"] = str(log)
    (runs / f"{run_id}.json").write_text(json.dumps(record), encoding="utf-8")


def test_compare_reads_new_report_sections_and_old_shown_report(tmp_path):
    _seed(tmp_path, "new-run", {"report_sections": {"sections": [
        {"heading": "I. Analyst Team Reports", "items": [{"agent": "Market Analyst", "text": "新运行段落"}]}]}})
    _seed(tmp_path, "old-run", {"shown_report": {"sections": [
        {"heading": "V. Portfolio Manager Decision", "items": [{"agent": "Portfolio Manager", "text": "旧运行段落"}]}]}})
    _seed(tmp_path, "older-run", {"decision": "Hold"})
    pair = compare.compare_pair("new-run", "old-run", tmp_path)
    assert pair["left"]["sections"] == [{"heading": "I. Analyst Team Reports", "text": "新运行段落"}]
    assert pair["right"]["sections"] == [{"heading": "V. Portfolio Manager Decision", "text": "旧运行段落"}]
    assert compare.compare_pair("older-run", "older-run", tmp_path)["left"]["sections"] == []


def test_replay_pages_a_long_run_without_dropping_steps(tmp_path):
    from tests.v2_compare_fixtures import long_log

    lines = long_log()
    _seed(tmp_path, "long-run", {}, lines)
    first = compare.replay_run("long-run", tmp_path, current=[])
    assert first["total_steps"] == len(lines) > compare.REPLAY_PAGE_SIZE
    assert first["pages"] == -(-len(lines) // compare.REPLAY_PAGE_SIZE)
    steps = []
    for page in range(1, first["pages"] + 1):
        found = compare.replay_run("long-run", tmp_path, current=[], page=str(page))
        assert found["first_step"] == (page - 1) * compare.REPLAY_PAGE_SIZE + 1
        assert len(found["steps"]) <= compare.REPLAY_PAGE_SIZE
        steps.extend(found["steps"])
    # Oldest first, every status line once: the first and last steps are both kept.
    assert [(step["agent"], step["time"]) for step in steps] == [
        (line.split("\t")[1], line.split("\t")[3]) for line in lines]
    for bad, status in (("0", 400), ("x", 400), (str(first["pages"] + 1), 404)):
        try:
            compare.replay_run("long-run", tmp_path, current=[], page=bad)
        except compare.CompareError as exc:
            assert exc.status == status
        else:
            raise AssertionError(bad)


def test_replay_of_a_run_without_steps_is_one_empty_page(tmp_path):
    _seed(tmp_path, "empty-run", {}, [])
    found = compare.replay_run("empty-run", tmp_path, current=[])
    assert (found["page"], found["pages"], found["total_steps"], found["first_step"], found["steps"]) == (1, 1, 0, 0, [])


def test_prompt_bodies_are_stored_once_and_the_record_keeps_hashes(tmp_path):
    versions = compare.capture_prompt_versions()
    first = compare.freeze_prompt_versions({}, versions, root=tmp_path)
    second = compare.freeze_prompt_versions({}, versions, root=tmp_path)
    assert first == second
    assert all(set(row) == {"id", "sha256"} for row in first["prompt_versions"])
    stored = sorted((tmp_path / compare.PROMPT_STORE).glob("*.txt"))
    assert len(stored) == len({item["sha256"] for item in versions})
    _seed(tmp_path, "hashed-run", {}, ["STATUS\tTrader\tcompleted\t10:00:00"])
    record_path = tmp_path / "runs" / "hashed-run.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["prompt_versions"] = first["prompt_versions"]
    record_path.write_text(json.dumps(record), encoding="utf-8")
    replay = compare.replay_run("hashed-run", tmp_path, current=versions)
    trader = next(row for row in replay["prompts"] if row["id"] == "trader")
    assert trader["saved"] is True and trader["changed"] is False
    assert trader["text"] == next(item["text"] for item in versions if item["id"] == "trader")
    # A stored body edited after the run no longer matches its hash: not a saved version.
    target = tmp_path / compare.PROMPT_STORE / f"{trader['sha256']}.txt"
    target.write_text("被改过的提示词", encoding="utf-8")
    again = compare.replay_run("hashed-run", tmp_path, current=versions)
    edited = next(row for row in again["prompts"] if row["id"] == "trader")
    assert edited["saved"] is False and edited["text"] == "" and edited["changed"] is None
