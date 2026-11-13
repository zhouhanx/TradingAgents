"""V2-WATCHLIST: price and decision changes are triggers, not automatic runs."""

import json
import time
from pathlib import Path

from webui import watchlist

_CLOCK = [0.0]


def _quote(price):
    """A fetched quote newer than any rule made so far in the test (R2: only a fresh quote fires).

    Taken from the clock at call time: a full run imports this module minutes before it runs.
    """
    _CLOCK[0] = max(_CLOCK[0] + 1, time.time() + 60)
    return {"price": price, "fetched_at": _CLOCK[0]}


def _rule_hit(root, symbol):
    """R4: a price trigger now needs a rule. 高于 11, then 10 -> 12 fires it once."""
    watchlist.change_rule({"symbol": symbol, "op": "add", "kind": "above", "value": 11}, root)
    watchlist.refresh({symbol: _quote(10)}, root)
    return watchlist.refresh({symbol: _quote(12)}, root)


def test_add_remove_and_duplicate(tmp_path):
    view = watchlist.add_symbol("aapl", tmp_path)
    assert view["items"][0]["symbol"] == "AAPL"
    assert view["items"][0]["asset_label"] == "股票"
    assert view["mode"] == "observe"
    assert view["mode_label"] == "观察"
    try:
        watchlist.add_symbol("AAPL", tmp_path)
    except watchlist.WatchlistError as exc:
        assert "已在自选" in str(exc)
    else:
        raise AssertionError("duplicate should fail")
    emptied = watchlist.remove_symbol("AAPL", tmp_path)
    assert emptied["items"] == []


def test_price_change_alone_is_not_a_trigger_and_a_rule_hit_is_one(tmp_path):
    # R4: before V2-WATCHLIST rules every price change was a trigger; now only a rule hit is.
    watchlist.add_symbol("600519", tmp_path)
    assert watchlist.refresh({"600519": 10}, tmp_path)["triggers"] == []
    assert watchlist.refresh({"600519": 12}, tmp_path)["triggers"] == []
    second = _rule_hit(tmp_path, "600519")
    assert len(second["triggers"]) == 1
    assert second["triggers"][0]["kind"] == "rule"
    assert second["triggers"][0]["started"] is False
    assert second["triggers"][0]["run_id"] is None
    again = watchlist.refresh({"600519": _quote(12.0)}, tmp_path)
    assert len(again["triggers"]) == 1
    watchlist.refresh({"600519": _quote(12)}, tmp_path)
    assert len(watchlist.list_watch(tmp_path)["triggers"]) == 1


def test_decision_change_is_one_trigger_and_matches_canonical_symbol(tmp_path):
    watchlist.add_symbol("BTC-USDT", tmp_path)
    watchlist.add_symbol("XAUUSD", tmp_path)
    first = watchlist.refresh({}, tmp_path, {"BTC-USD": "Hold", "GC=F": "Underweight"})
    assert first["triggers"] == []
    assert first["items"][0]["asset_label"] == "加密货币"
    assert first["items"][0]["asset_type"] == "crypto"
    assert first["items"][1]["asset_label"] == "贵金属/商品期货"
    assert first["items"][1]["asset_type"] == "commodity"
    second = watchlist.refresh({}, tmp_path, {"BTCUSD": "Sell", "XAUUSD": "Sell"})
    kinds = {(item["symbol"], item["kind"]) for item in second["triggers"]}
    assert kinds == {("BTC-USDT", "decision"), ("XAUUSD", "decision")}
    assert all(item["started"] is False and item["run_id"] is None for item in second["triggers"])
    third = watchlist.refresh({}, tmp_path, {"BTC-USDT": "Sell"})
    assert len(third["triggers"]) == 2


def test_kline_open_does_not_consume_trigger(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    _rule_hit(tmp_path, "NVDA")
    opened = watchlist.open_kline("NVDA", tmp_path)
    assert opened == {"action": "kline", "symbol": "NVDA", "consumed_trigger": False}
    assert len(watchlist.list_watch(tmp_path)["triggers"]) == 1


def test_explicit_analyze_records_one_trigger_per_rule(tmp_path):
    watchlist.add_symbol("BTC-USDT", tmp_path)
    first = watchlist.explicit_analyze("BTC-USDT", tmp_path)
    second = watchlist.explicit_analyze("BTC-USDT", tmp_path)
    assert first["action"] == "wizard" and first["started"] is False
    assert first["trigger_id"] == second["trigger_id"]
    assert len(watchlist.list_watch(tmp_path)["triggers"]) == 1


def test_observe_mode_refuses_start_and_auto_admits_once_without_a_run(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    changed = _rule_hit(tmp_path, "600519")
    trigger_id = changed["triggers"][0]["id"]
    refused = watchlist.admit("600519", trigger_id, tmp_path)
    assert refused["allowed"] is False
    assert "观察模式" in refused["reason"]
    assert refused["started"] is False

    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "auto"
    path.write_text(json.dumps(data), encoding="utf-8")
    first = watchlist.admit("600519", trigger_id, tmp_path)
    second = watchlist.admit("600519", trigger_id, tmp_path)
    assert first["allowed"] is True
    assert first["run_id"] is None
    assert first["started"] is False
    assert second["duplicate"] is True
    assert len(json.loads(path.read_text(encoding="utf-8"))["admissions"]) == 1

    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["last_price"] = 10  # back under the 高于 11 rule
    data["admissions"] = []
    path.write_text(json.dumps(data), encoding="utf-8")
    stale = watchlist.admit("600519", trigger_id, tmp_path)
    assert stale["allowed"] is False
    assert "条件已经变化" in stale["reason"]


def test_page_has_watchlist_entry():
    html = Path("webui/static/index.html").read_text(encoding="utf-8")
    js = Path("webui/static/app.js").read_text(encoding="utf-8")
    css = Path("webui/static/app.css").read_text(encoding="utf-8")
    assert 'data-page="watchlist"' in html
    assert "观察模式不会自动启动分析" in js
    label = js.split("function watchModeLabel", 1)[1].split("function watchPageDescription", 1)[0]
    assert 'if (!mode || mode === "observe") return "观察"' not in label
    assert 'if (!mode || !knownWatchMode(mode)) return "状态未知"' in label
    assert 'if (mode === "observe") return "观察"' in label
    assert "function watchPageDescription" in js
    assert "function watchRuleText" in js
    assert "已停用" in js
    assert 'item.enabled === false' in js
    assert "watch-form" in js
    assert "watch-actions" in css
    assert "close > 0" in js


def test_isolated_home_does_not_import_other_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setattr("webui.service.list_reports", lambda limit=80: [])
    monkeypatch.setattr(
        "webui.service.list_memory",
        lambda limit=80: {"path": "/Users/zhouhan/.tradingagents/memory/trading_memory.md",
                          "entries": [{"ticker": "BTC-USD", "rating": "Underweight"}]},
    )
    assert watchlist.local_decisions() == {}
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_HOME", raising=False)
    assert watchlist.local_decisions()["BTC-USD"] == "Underweight"


def test_a_header_rating_without_a_portfolio_decision_is_not_a_local_decision(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_HOME", raising=False)
    monkeypatch.setattr("webui.service.list_reports", lambda limit=80: [
        {"ticker": "AAPL", "rating": "Hold", "date": "2026-11-11", "modified": 10, "has_decision": False},
        {"ticker": "NVDA", "rating": "Buy", "date": "2026-11-11", "modified": 11, "has_decision": True},
    ])
    monkeypatch.setattr("webui.service.list_memory", lambda limit=80: {"path": "", "entries": []})
    assert watchlist.local_decisions() == {"NVDA": "Buy"}


def test_newer_memory_date_beats_an_older_report(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_HOME", raising=False)
    monkeypatch.setattr("webui.service.list_reports", lambda limit=80: [
        {"ticker": "600519", "rating": "Hold", "date": "2026-11-01", "modified": 1_000_000_000},
    ])
    monkeypatch.setattr("webui.service.list_memory", lambda limit=80: {
        "path": "",
        "entries": [{"ticker": "600519", "rating": "Buy", "date": "2026-11-11"}],
    })
    assert watchlist.local_decisions()["600519.SS"] == "Buy"


def test_newer_report_beats_an_older_memory_date(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_HOME", raising=False)
    monkeypatch.setattr("webui.service.list_reports", lambda limit=80: [
        {"ticker": "600519", "rating": "Buy", "date": "2026-11-11", "modified": 1_000},
    ])
    monkeypatch.setattr("webui.service.list_memory", lambda limit=80: {
        "path": "",
        "entries": [{"ticker": "600519", "rating": "Hold", "date": "2026-11-01"}],
    })
    assert watchlist.local_decisions()["600519.SS"] == "Buy"


def test_refresh_records_a_newer_local_decision(tmp_path, monkeypatch):
    watchlist.add_symbol("600519", tmp_path)
    watchlist.refresh({}, tmp_path, {"600519": "Hold"})
    monkeypatch.setattr(watchlist, "local_decisions", lambda: {"600519": "Buy"})
    view = watchlist.refresh({}, tmp_path, observe_local=True)
    changed = [item for item in view["triggers"] if item["kind"] == "decision"]
    assert changed[-1]["old_value"] == "Hold"
    assert changed[-1]["new_value"] == "Buy"
    assert changed[-1]["started"] is False


def test_isolated_home_ignores_reports_stored_outside_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "other" / "600519_old" / "complete_report.md"
    inside = home / "results" / "reports" / "AAPL_new" / "complete_report.md"
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(home))
    monkeypatch.setattr("webui.service.list_reports", lambda limit=80: [
        {"ticker": "600519", "rating": "Sell", "date": "2026-11-11", "modified": 10, "markdown": str(outside)},
        {"ticker": "AAPL", "rating": "Buy", "date": "2026-11-11", "modified": 11, "markdown": str(inside)},
    ])
    monkeypatch.setattr("webui.service.list_memory", lambda limit=80: {"path": str(home / "memory.md"), "entries": []})
    assert watchlist.local_decisions() == {"AAPL": "Buy"}


def test_exchange_alias_uses_the_newer_rating(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_WEBUI_HOME", raising=False)
    monkeypatch.setattr("webui.service.list_reports", lambda limit=80: [
        {"ticker": "600519", "rating": "Hold", "date": "2026-11-01", "modified": 1_000},
    ])
    monkeypatch.setattr("webui.service.list_memory", lambda limit=80: {
        "path": "",
        "entries": [{"ticker": "600519.SS", "rating": "Buy", "date": "2026-11-11"}],
    })
    assert watchlist.local_decisions() == {"600519.SS": "Buy"}


def test_refresh_matches_an_exchange_alias(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    watchlist.refresh({}, tmp_path, {"600519": "Hold"})
    view = watchlist.refresh({}, tmp_path, {"600519.SS": "Buy"})
    assert view["items"][0]["last_decision"] == "Buy"
    changed = [item for item in view["triggers"] if item["kind"] == "decision"]
    assert changed[-1]["old_value"] == "Hold"
    assert changed[-1]["new_value"] == "Buy"


def test_invalid_quote_does_not_replace_a_price_or_create_a_trigger(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    watchlist.refresh({"600519": 10}, tmp_path)
    for bad in (0, -1, float("nan"), float("inf"), "无"):
        view = watchlist.refresh({"600519": bad}, tmp_path)
        assert view["items"][0]["last_price"] == 10
        assert view["triggers"] == []

def test_stored_non_positive_price_is_not_published(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["last_price"] = 0
    path.write_text(json.dumps(data), encoding="utf-8")
    view = watchlist.list_watch(tmp_path)
    assert view["items"][0]["last_price"] is None
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function watchShownPrice" in script
    assert "n <= 0" in script



def test_unchecked_trigger_is_not_called_started(tmp_path):
    watchlist.add_symbol("NVDA", tmp_path)
    changed = _rule_hit(tmp_path, "NVDA")
    row = changed["triggers"][0]
    assert row["status_label"] == "未检查"
    assert row["checked"] is False
    assert row["started"] is False
    assert "已启动" not in row["status_label"]


def test_observe_check_is_visible_and_does_not_block_a_later_admission(tmp_path):
    watchlist.add_symbol("AAPL", tmp_path)
    changed = _rule_hit(tmp_path, "AAPL")
    ident = changed["triggers"][0]["id"]
    refused = watchlist.admit("AAPL", ident, tmp_path)
    assert refused["allowed"] is False
    assert refused["started"] is False
    path = tmp_path / "watchlist.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["admissions"] == []
    assert stored["checks"][-1]["result"] == "refused"
    assert stored["checks"][-1]["started"] is False
    row = watchlist.list_watch(tmp_path)["triggers"][0]
    assert row["status_label"] == "已检查，未启动"
    assert row["started"] is False
    stored["mode"] = "auto"
    path.write_text(json.dumps(stored), encoding="utf-8")
    admitted = watchlist.admit("AAPL", ident, tmp_path)
    assert admitted["allowed"] is True
    assert admitted["run_id"] is None
    assert admitted["started"] is False
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored["admissions"]) == 1
    again = watchlist.list_watch(tmp_path)["triggers"][0]
    assert again["status_label"] == "已检查，未启动"
    assert again["started"] is False


def test_start_flag_without_a_run_is_not_published_as_started(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    _rule_hit(tmp_path, "600519")
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["triggers"][0]["started"] = True
    data["triggers"][0]["run_id"] = "run-1"
    path.write_text(json.dumps(data), encoding="utf-8")
    row = watchlist.list_watch(tmp_path)["triggers"][0]
    assert row["status_label"] == "未检查"
    assert row["started"] is False
    assert row["checked"] is False
    assert row["run_id"] is None

    data = json.loads(path.read_text(encoding="utf-8"))
    data["admissions"] = [{
        "trigger_id": data["triggers"][0]["id"],
        "rule_version": "watch-1",
        "admitted_at": "2026-11-11T00:00:00+00:00",
        "run_id": None,
        "started": True,
        "note": "flag without a run",
    }]
    path.write_text(json.dumps(data), encoding="utf-8")
    row = watchlist.list_watch(tmp_path)["triggers"][0]
    assert row["status_label"] == "已检查，未启动"
    assert row["started"] is False
    assert row["run_id"] is None

    data = json.loads(path.read_text(encoding="utf-8"))
    data["admissions"][0]["run_id"] = "run-1"
    data["admissions"][0]["started"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    row = watchlist.list_watch(tmp_path)["triggers"][0]
    assert row["status_label"] == "已启动"
    assert row["started"] is True
    assert row["run_id"] == "run-1"


def test_older_checked_trigger_is_not_hidden_by_newer_rows(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    changed = _rule_hit(tmp_path, "600519")
    ident = changed["triggers"][0]["id"]
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"] = [{
        "trigger_id": ident,
        "result": "refused",
        "reason": "当前是观察模式，不会启动分析。",
        "checked_at": "2026-11-11T00:00:00+00:00",
        "started": False,
        "run_id": None,
    }]
    for index in range(21):  # R6: the recent window is twenty rows
        data["triggers"].append({
            "id": f"extra-{index}",
            "symbol": "600519",
            "kind": "price",
            "rule_version": "watch-1",
            "old_value": index,
            "new_value": index + 1,
            "created_at": f"2026-11-11T00:{index:02d}:00+00:00",
            "started": True,
            "run_id": f"run-{index}",
        })
    path.write_text(json.dumps(data), encoding="utf-8")
    view = watchlist.list_watch(tmp_path)
    visible_ids = [item["id"] for item in view["visible_triggers"]]
    visible = {item["id"]: item for item in view["visible_triggers"]}
    assert ident in visible
    assert visible[ident]["status_label"] == "已检查，未启动"
    assert visible[ident]["started"] is False
    assert "extra-0" not in visible
    assert visible_ids[-1] == ident
    assert visible_ids[0] == "extra-20"
    assert all(item["status_label"] == "未检查" for item in visible.values() if item["id"] != ident)
    assert all(item["started"] is False for item in view["triggers"])


def test_page_badge_follows_the_check_not_the_start_flag():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    css = Path("webui/static/app.css").read_text(encoding="utf-8")
    assert "function watchTriggerStatus" in script
    assert "function watchVisibleTriggers" in script
    assert "visible_triggers" in script
    assert 'item.started ? "已启动"' not in script
    assert "已检查，未启动" in script
    assert "未检查" in script
    assert "watch-triggers .status-badge" in css


def test_watch_trigger_without_status_is_not_called_unchecked():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    body = script.split("function watchTriggerStatus(", 1)[1].split("function watchTriggerStatusMissing", 1)[0]
    assert "触发状态未知，不把它写成未检查。" in script
    assert 'if (item && item.checked === false) return "未检查";' in body
    assert body.rstrip().endswith('return "状态未知";\n}')


def test_explicit_click_opens_the_wizard_and_is_not_unchecked(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    first = watchlist.explicit_analyze("600519", tmp_path)
    second = watchlist.explicit_analyze("600519", tmp_path)
    assert first["started"] is False
    assert second["trigger_id"] == first["trigger_id"]
    row = watchlist.list_watch(tmp_path)["triggers"][0]
    assert row["status_label"] == "只打开向导，未启动"
    assert row["started"] is False
    assert row["run_id"] is None
    assert row["checked"] is True
    assert row["status_label"] != "未检查"
    assert "已启动" not in row["status_label"]
    stored = json.loads((tmp_path / "watchlist.json").read_text(encoding="utf-8"))
    assert stored["admissions"] == []
    assert [item["result"] for item in stored["checks"]] == ["wizard"]


def test_explicit_trigger_is_not_called_started(tmp_path):
    watchlist.add_symbol("AAPL", tmp_path)
    watchlist.explicit_analyze("AAPL", tmp_path)
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["triggers"][0]["started"] = True
    data["triggers"][0]["run_id"] = "run-1"
    data["admissions"] = [{
        "trigger_id": data["triggers"][0]["id"],
        "run_id": "run-1",
        "started": True,
    }]
    path.write_text(json.dumps(data), encoding="utf-8")
    row = watchlist.list_watch(tmp_path)["triggers"][0]
    assert row["status_label"] == "只打开向导，未启动"
    assert row["started"] is False
    assert row["run_id"] is None


def test_explicit_click_stays_visible_outside_the_recent_window(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    clicked = watchlist.explicit_analyze("600519", tmp_path)
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for index in range(21):  # R6: the recent window is twenty rows
        data["triggers"].append({
            "id": f"extra-{index}",
            "symbol": "600519",
            "kind": "price",
            "rule_version": "watch-1",
            "old_value": index,
            "new_value": index + 1,
            "created_at": f"2026-11-11T00:{index:02d}:00+00:00",
            "started": True,
            "run_id": f"run-{index}",
        })
    path.write_text(json.dumps(data), encoding="utf-8")
    visible = {item["id"]: item for item in watchlist.list_watch(tmp_path)["visible_triggers"]}
    assert clicked["trigger_id"] in visible
    assert visible[clicked["trigger_id"]]["status_label"] == "只打开向导，未启动"
    assert "extra-0" not in visible


def test_page_labels_an_explicit_click_as_the_wizard():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "只打开向导，未启动" in script
    assert 'item.kind === "explicit"' in script
    assert "<h3>触发</h3>" in script
    assert "<h3>最近触发</h3>" not in script
    assert "sticky.concat(recent).reverse()" in script


def test_disabled_item_is_labeled_stopped_and_not_enabled(tmp_path):
    watchlist.add_symbol("IBM", tmp_path)
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["enabled"] = False
    data["items"][0]["last_price"] = 12
    path.write_text(json.dumps(data), encoding="utf-8")
    item = watchlist.list_watch(tmp_path)["items"][0]
    assert item["enabled"] is False
    assert item["enabled_label"] == "已停用"
    assert item["enabled_label"] != "启用"
    assert item["last_price"] == 12


def test_non_observe_mode_is_not_called_observe(tmp_path):
    watchlist.add_symbol("MSFT", tmp_path)
    changed = _rule_hit(tmp_path, "MSFT")
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "off"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = watchlist.list_watch(tmp_path)
    assert view["mode_label"] == "状态未知"
    assert "当前是观察模式" not in view["note"]
    assert "状态未知" in view["note"]
    assert "不把它写成观察模式" in view["note"]
    assert "不会自动启动" in view["note"]
    refused = watchlist.admit("MSFT", changed["triggers"][0]["id"], tmp_path)
    assert refused["allowed"] is False
    assert "观察模式" not in refused["reason"]
    assert "不会启动" in refused["reason"]


def test_blank_watch_mode_is_not_called_observe(tmp_path):
    watchlist.add_symbol("600519", tmp_path)
    changed = _rule_hit(tmp_path, "600519")
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    view = watchlist.list_watch(tmp_path)
    assert view["mode"] != "observe"
    assert view["mode_label"] == "状态未知"
    assert view["note"].startswith("当前规则状态未知，不把它写成观察模式")
    assert not view["note"].startswith("价格或决定变化只记录触发。观察模式")
    refused = watchlist.admit("600519", changed["triggers"][0]["id"], tmp_path)
    assert refused["allowed"] is False
    assert refused["started"] is False
    assert refused["reason"].startswith("当前规则状态未知，不把它写成观察模式")
    assert not refused["reason"].startswith("当前是观察模式")
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.pop("mode", None)
    path.write_text(json.dumps(stored), encoding="utf-8")
    missing = watchlist.list_watch(tmp_path)
    assert missing["mode"] != "observe"
    assert missing["mode_label"] == "状态未知"
    assert missing["note"].startswith("当前规则状态未知，不把它写成观察模式")


def test_auto_mode_note_does_not_claim_observe(tmp_path):
    watchlist.add_symbol("AAPL", tmp_path)
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "auto"
    path.write_text(json.dumps(data), encoding="utf-8")
    note = watchlist.list_watch(tmp_path)["note"]
    assert "自动模式" in note
    assert "观察模式" not in note
    assert "本页也不调用模型" in note
