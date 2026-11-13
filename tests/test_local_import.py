"""V2-LOCAL-DATA-IMPORT: local CSV/JSON evidence, no network fetch."""

import ast
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from tradingagents.agents.analysts import news_analyst
from tradingagents.dataflows.local_import import (
    LocalImportError,
    format_local_import_lines,
    local_import_report_section,
    parse_local_import,
)
from webui.service import ConsoleError, _require_local_import_news, validate_local_import

ROOT = Path(__file__).resolve().parents[1]


def test_json_and_csv_parse_without_network():
    source = (ROOT / "tradingagents/dataflows/local_import.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = [node.names[0].name for node in tree.body if isinstance(node, ast.Import)]
    imported += [alias.name for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names]
    assert "requests" not in source
    assert "urllib" not in source
    rows = parse_local_import(
        '[{"title": "茅台新闻", "summary": "出厂价上调", "date": "2026-11-01", "url": "https://example.test/a"}]',
        "notes.json",
    )
    assert rows[0]["published_at"] == "2026-11-01"
    assert rows[0]["source"] == "本地导入"
    csv_rows = parse_local_import("标题,摘要,日期\n本地纪要,只在文件里,2026/10/02\n", "book.csv")
    assert csv_rows[0]["title"] == "本地纪要"
    assert csv_rows[0]["published_at"] == "2026-10-02"
    cited = format_local_import_lines(rows)
    assert cited.startswith("- 茅台新闻 [本地导入·2026-11-01·茅台新闻]")
    assert "https://" not in cited


def test_bad_files_are_chinese_and_limited():
    with pytest.raises(LocalImportError, match="空的"):
        parse_local_import("  ", "a.json")
    with pytest.raises(LocalImportError, match="512KB"):
        parse_local_import("x" * (512 * 1024 + 1), "a.csv")
    with pytest.raises(LocalImportError, match="只支持 CSV 或 JSON"):
        parse_local_import("hello", "notes.txt")
    with pytest.raises(LocalImportError, match="没有可导入"):
        parse_local_import('[{"id": 1}]', "a.json")
    too_many = "[" + ",".join(f'{{"title": "n{i}"}}' for i in range(51)) + "]"
    with pytest.raises(LocalImportError, match="50"):
        parse_local_import(too_many, "a.json")


def test_validate_endpoint_returns_preview_and_rejects_garbage():
    result = validate_local_import({"text": '{"items": [{"content": "只有正文"}]}', "filename": "a.json"})
    assert result["count"] == 1
    assert result["preview"][0]["title"]
    with pytest.raises(ConsoleError, match="JSON 无法解析"):
        validate_local_import({"text": "{", "filename": "bad.json"})


def test_news_report_cites_local_import(monkeypatch):
    item = {"title": "本地纪要", "summary": "文件内容", "published_at": "2026-10-02", "source": "本地导入"}
    monkeypatch.setattr(
        "tradingagents.dataflows.config.get_config",
        lambda: {"local_import_items": [item]},
    )
    monkeypatch.setattr(news_analyst, "take_turn", lambda *args, **kwargs: (AIMessage(content="正文"), "正文"))
    monkeypatch.setattr(news_analyst, "get_instrument_context_from_state", lambda state: "")
    monkeypatch.setattr(news_analyst, "get_language_instruction", lambda: "")
    node = news_analyst.create_news_analyst(object())
    out = node({"trade_date": "2026-11-01", "company_of_interest": "NVDA", "asset_type": "stock", "messages": []})
    assert "正文" in out["news_report"]
    assert "[本地导入·2026-10-02·本地纪要]" in out["news_report"]
    assert "不是联网抓取" in out["news_report"]
    section = local_import_report_section([item])
    assert section in out["news_report"]


def test_import_without_news_analyst_is_refused():
    with pytest.raises(ConsoleError, match="未选新闻分析师"):
        _require_local_import_news({"local_import": [{"title": "x"}], "analysts": ["market"]})
    _require_local_import_news({"local_import": [{"title": "x"}], "analysts": ["news"]})
