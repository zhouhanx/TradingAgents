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
    assert {item["id"] for item in versions} == {"portfolio_manager", "research_manager"}
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
