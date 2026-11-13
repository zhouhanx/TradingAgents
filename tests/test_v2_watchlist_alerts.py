"""V2-WATCHLIST W3: rules fire once, re-arm, and reach the 定时 reminder list (R2, R4-R6, R8).

No model call and no network. Quotes carry the server's fetch time; only a quote fetched
after a rule was armed may fire it.
"""

import json
import time

import pytest

from webui import schedule, watchlist


class Clock:
    """Quote fetch times start two minutes after the test begins (not after import: a full
    run imports this module minutes before the test runs), so they are newer than its rules."""

    def __init__(self):
        self.now = time.time() + 120

    def quote(self, price, *, prev=None, day="2026-11-13", step=60):
        self.now += step
        return {"price": price, "prev_close": prev, "trading_day": day, "fetched_at": self.now, "delayed": True}


@pytest.fixture
def clock():
    return Clock()


def _add(root, kind, value, symbol="NVDA"):
    view = watchlist.change_rule({"symbol": symbol, "op": "add", "kind": kind, "value": value}, root)
    return next(i for i in view["items"] if i["symbol"] == symbol)["rules"][-1]["id"]


def _rule(view, ident, symbol="NVDA"):
    return next(r for r in next(i for i in view["items"] if i["symbol"] == symbol)["rules"] if r["id"] == ident)


def _rule_triggers(view):
    return [row for row in view["triggers"] if row["kind"] == "rule"]


@pytest.mark.unit
def test_above_fires_once_and_rearms_after_crossing_back(tmp_path, clock):
    watchlist.add_symbol("NVDA", tmp_path)
    ident = _add(tmp_path, "above", 190)
    view = watchlist.refresh({"NVDA": clock.quote(188.42)}, tmp_path)
    assert _rule_triggers(view) == [] and view["fired"] == []
    view = watchlist.refresh({"NVDA": clock.quote(190.62)}, tmp_path)
    rows = _rule_triggers(view)
    assert len(rows) == 1 and rows[0]["id"] == f"rule|NVDA|{ident}#1"
    assert rows[0]["text"] == "高于 190.00 · 现价 190.62" and rows[0]["started"] is False
    assert view["fired"] == [{"id": rows[0]["id"], "symbol": "NVDA", "label": "高于 190.00", "text": "NVDA 高于 190.00"}]
    state = _rule(view, ident)
    assert state["state"] == "hit" and state["state_label"].endswith("价格回到 190.00 以下后重新生效。")
    assert state["state_label"].startswith("已触发（北京 ")
    for price in (191, 195, 190):  # still above: no repeat
        view = watchlist.refresh({"NVDA": clock.quote(price)}, tmp_path)
    assert len(_rule_triggers(view)) == 1
    view = watchlist.refresh({"NVDA": clock.quote(189)}, tmp_path)  # crossed back: armed again
    assert _rule(view, ident)["state"] == "armed" and len(_rule_triggers(view)) == 1
    view = watchlist.refresh({"NVDA": clock.quote(190.5)}, tmp_path)
    assert [row["id"] for row in _rule_triggers(view)][-1] == f"rule|NVDA|{ident}#2"
    assert len(_rule_triggers(view)) == 2 and view["mode"] == "observe"


@pytest.mark.unit
def test_below_and_the_old_price_never_fire(tmp_path, clock):
    watchlist.add_symbol("BTC-USDT", tmp_path)
    watchlist.refresh({"BTC-USDT": clock.quote(89000)}, tmp_path)  # fetched before the rule exists
    _add(tmp_path, "below", 90000, symbol="BTC-USDT")
    clock.now -= 600  # a cached answer fetched before the rule: not fresh
    view = watchlist.refresh({"BTC-USDT": clock.quote(89500, step=1)}, tmp_path)
    assert _rule_triggers(view) == []
    view = watchlist.refresh({"BTC-USDT": None}, tmp_path)  # no price this time: nothing judged
    assert _rule_triggers(view) == []
    clock.now = time.time() + 3600
    view = watchlist.refresh({"BTC-USDT": clock.quote(89870.2)}, tmp_path)
    assert [row["text"] for row in _rule_triggers(view)] == ["低于 90000.0 · 现价 89870.2"]


@pytest.mark.unit
def test_a_rule_made_while_its_condition_holds_fires_on_the_next_fresh_quote_only(tmp_path, clock):
    watchlist.add_symbol("NVDA", tmp_path)
    first = clock.quote(200)
    watchlist.refresh({"NVDA": first}, tmp_path)
    _add(tmp_path, "above", 190)
    view = watchlist.refresh({"NVDA": dict(first)}, tmp_path)  # the same cached answer again
    assert _rule_triggers(view) == []
    clock.now = time.time() + 7200
    view = watchlist.refresh({"NVDA": clock.quote(200)}, tmp_path)
    assert len(_rule_triggers(view)) == 1


@pytest.mark.unit
def test_change_rules_fire_once_per_trading_day_and_direction(tmp_path, clock):
    watchlist.add_symbol("XAUUSD", tmp_path)
    ident = _add(tmp_path, "pct", 1, symbol="XAUUSD")
    view = watchlist.refresh({"XAUUSD": clock.quote(4065, prev=4040)}, tmp_path)  # +0.62%
    assert _rule_triggers(view) == []
    view = watchlist.refresh({"XAUUSD": clock.quote(4085.25, prev=4040)}, tmp_path)  # +1.12%
    rows = _rule_triggers(view)
    assert [row["text"] for row in rows] == ["涨跌 ≥ 1% · 较昨收 +1.12%"]
    state = _rule(view, ident, "XAUUSD")
    assert state["state"] == "today" and "同一方向每个交易日只提醒一次" in state["state_label"]
    for price in (4090, 4041, 4100):
        view = watchlist.refresh({"XAUUSD": clock.quote(price, prev=4040)}, tmp_path)
    assert len(_rule_triggers(view)) == 1
    view = watchlist.refresh({"XAUUSD": clock.quote(3990, prev=4040)}, tmp_path)  # -1.24%: other direction
    assert len(_rule_triggers(view)) == 2 and _rule_triggers(view)[-1]["side"] == "down"
    view = watchlist.refresh({"XAUUSD": clock.quote(4100, prev=4040, day="2026-11-16")}, tmp_path)
    assert len(_rule_triggers(view)) == 3
    assert len({row["id"] for row in _rule_triggers(view)}) == 3
    view = watchlist.refresh({"XAUUSD": clock.quote(4100, prev=None, day="2026-11-17")}, tmp_path)
    assert len(_rule_triggers(view)) == 3  # no previous close: not judged


@pytest.mark.unit
def test_crypto_change_text_says_beijing_eight(tmp_path, clock):
    watchlist.add_symbol("BTC-USDT", tmp_path)
    _add(tmp_path, "pct", 3, symbol="BTC-USDT")
    view = watchlist.refresh({"BTC-USDT": clock.quote(89870.2, prev=93430)}, tmp_path)
    assert _rule_triggers(view)[0]["text"] == "涨跌 ≥ 3% · 较昨收（北京 08:00） -3.81%"
    assert "UTC" not in json.dumps(view, ensure_ascii=False)


@pytest.mark.unit
def test_window_rule_compares_with_the_last_fetched_price(tmp_path, clock):
    watchlist.add_symbol("NVDA", tmp_path)
    _add(tmp_path, "win", 1.5)
    watchlist.refresh({"NVDA": clock.quote(100)}, tmp_path)
    view = watchlist.refresh({"NVDA": clock.quote(101)}, tmp_path)
    assert _rule_triggers(view) == []
    view = watchlist.refresh({"NVDA": clock.quote(103)}, tmp_path)  # +1.98% since 101
    assert [row["text"] for row in _rule_triggers(view)] == ["间隔内涨跌 ≥ 1.5% · +1.98%（上次 101.00）"]
    view = watchlist.refresh({"NVDA": clock.quote(110, step=3600)}, tmp_path)  # an hour later is not one interval
    assert len(_rule_triggers(view)) == 1
    view = watchlist.refresh({"NVDA": clock.quote(100)}, tmp_path)  # -9.09% in one interval
    assert len(_rule_triggers(view)) == 2


@pytest.mark.unit
def test_disabled_rules_and_items_do_not_fire(tmp_path, clock):
    watchlist.add_symbol("NVDA", tmp_path)
    ident = _add(tmp_path, "above", 1)
    watchlist.change_rule({"symbol": "NVDA", "op": "toggle", "rule_id": ident, "enabled": False}, tmp_path)
    assert _rule_triggers(watchlist.refresh({"NVDA": clock.quote(5)}, tmp_path)) == []


@pytest.mark.unit
def test_a_rule_hit_is_one_price_reminder_marked_sent(tmp_path, clock):
    watchlist.add_symbol("NVDA", tmp_path)
    _add(tmp_path, "above", 190)
    watchlist.refresh({"NVDA": clock.quote(190.62)}, tmp_path)
    watchlist.refresh({"NVDA": clock.quote(191)}, tmp_path)
    reminders = [row for row in schedule.list_schedules(tmp_path)["reminders"] if row.get("kind") == "price"]
    assert len(reminders) == 1
    assert reminders[0]["status"] == "sent" and reminders[0]["symbol"] == "NVDA"
    assert reminders[0]["body"].startswith("NVDA 高于 190.00，现价 190.62（北京 ")


@pytest.mark.unit
def test_a_price_reminder_does_not_turn_on_decision_and_completion_reminders(tmp_path, clock, monkeypatch):
    """R5: the file a price reminder creates keeps the other two reminders off."""
    watchlist.add_symbol("NVDA", tmp_path)
    _add(tmp_path, "above", 190)
    watchlist.refresh({"NVDA": clock.quote(191)}, tmp_path)
    stored = json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))
    assert stored["local_events"] is False
    calls = []
    monkeypatch.setattr(schedule, "_local_events", lambda data: calls.append(1) or [
        {"kind": "decision", "symbol": "NVDA", "old": "Hold", "new": "Buy"}])
    schedule.tick(root=tmp_path, observe_local=True)  # the server's background tick
    assert calls == []
    assert [row["kind"] for row in schedule.list_schedules(tmp_path)["reminders"]] == ["price"]
    schedule.tick(root=tmp_path, observe_local=True, enable_local=True)  # the 定时 page's own visit
    assert calls == [1]
    assert json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))["local_events"] is True


@pytest.mark.unit
def test_an_existing_schedule_file_keeps_its_reminders_on(tmp_path, clock):
    schedule.create_schedule("600519", "09:30", tmp_path)
    watchlist.add_symbol("NVDA", tmp_path)
    _add(tmp_path, "above", 190)
    watchlist.refresh({"NVDA": clock.quote(191)}, tmp_path)
    assert json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))["local_events"] is True
    old = tmp_path / "old"
    old.mkdir()
    (old / "schedule.json").write_text(json.dumps({"schedules": [], "receipts": []}), encoding="utf-8")
    assert schedule._load(old)["local_events"] is True  # a file from before the flag


@pytest.mark.unit
def test_unseen_count_is_stored_and_cleared_by_seen(tmp_path, clock):
    watchlist.add_symbol("NVDA", tmp_path)
    watchlist.add_symbol("BTC-USDT", tmp_path)
    _add(tmp_path, "above", 190)
    _add(tmp_path, "below", 90000, symbol="BTC-USDT")
    view = watchlist.refresh({"NVDA": clock.quote(190.62), "BTC-USDT": clock.quote(89870.2)}, tmp_path)
    assert view["unseen_count"] == 2 and len(view["fired"]) == 2
    assert watchlist.list_watch(tmp_path)["unseen_count"] == 2  # another tab reads the same number
    assert all(row["unseen"] for row in _rule_triggers(view))
    view = watchlist.mark_seen(tmp_path)
    assert view["unseen_count"] == 0 and not any(row["unseen"] for row in view["triggers"])
    assert _rule(view, view["items"][0]["rules"][0]["id"])["state"] == "hit"  # the highlight stays


@pytest.mark.unit
def test_store_keeps_500_triggers_and_checked_ones(tmp_path, clock):
    watchlist.add_symbol("NVDA", tmp_path)
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["triggers"] = [{"id": f"old-{i}", "symbol": "NVDA", "kind": "price", "rule_version": "watch-1",
                         "created_at": "2026-11-01T00:00:00+00:00"} for i in range(505)]
    data["checks"] = [{"trigger_id": "old-0", "result": "refused", "reason": "x", "checked_at": "2026-11-01T00:00:00+00:00"}]
    path.write_text(json.dumps(data), encoding="utf-8")
    _add(tmp_path, "above", 1)
    view = watchlist.refresh({"NVDA": clock.quote(5)}, tmp_path)
    ids = [row["id"] for row in view["triggers"]]
    assert len(ids) == 500 and view["trigger_total"] == 500
    assert "old-0" in ids and "old-1" not in ids and ids[-1].startswith("rule|NVDA|")
    visible = view["visible_triggers"]
    assert len(visible) == 21 and visible[-1]["id"] == "old-0"  # 20 newest plus the checked one


@pytest.mark.unit
def test_a_rule_trigger_admission_rechecks_the_rule(tmp_path, clock):
    watchlist.add_symbol("NVDA", tmp_path)
    _add(tmp_path, "above", 190)
    view = watchlist.refresh({"NVDA": clock.quote(191)}, tmp_path)
    ident = _rule_triggers(view)[0]["id"]
    refused = watchlist.admit("NVDA", ident, tmp_path)
    assert refused["allowed"] is False and "观察模式" in refused["reason"]
    path = tmp_path / "watchlist.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "auto"
    data["items"][0]["last_price"] = 180
    path.write_text(json.dumps(data), encoding="utf-8")
    assert "条件已经变化" in watchlist.admit("NVDA", ident, tmp_path)["reason"]
