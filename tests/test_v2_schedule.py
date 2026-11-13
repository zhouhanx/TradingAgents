"""V2-SCHEDULE: due triggers and reminder receipts do not start a model."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from webui import schedule

TZ = ZoneInfo("Asia/Shanghai")
DUE = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)


def _id(view):
    return view["schedules"][0]["id"]


def _created_before_due(root):
    """Schedules created now would not be due at the fixed DUE (a slot before
    creation is not due), so date them before it, as the newer tests do."""
    path = root / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["schedules"]:
        item["created_at"] = "2026-11-10T08:00:00+08:00"
    path.write_text(json.dumps(data), encoding="utf-8")


def test_create_pause_resume_and_remove(tmp_path):
    view = schedule.create_schedule("aapl", "9:30", tmp_path)
    assert view["schedules"][0]["symbol"] == "AAPL"
    assert view["schedules"][0]["at"] == "09:30"
    assert view["mode"] == "observe"
    assert view["schedules"][0]["status_label"] == "观察中"
    assert view["schedules"][0]["status_label"] != "启用"
    try:
        schedule.create_schedule("AAPL", "09:30", tmp_path)
    except schedule.ScheduleError as exc:
        assert "已有定时" in str(exc)
    else:
        raise AssertionError("duplicate should fail")
    paused = schedule.set_status(_id(view), "paused", tmp_path)
    assert paused["schedules"][0]["status"] == "paused"
    assert paused["schedules"][0]["status_label"] == "已暂停"
    resumed = schedule.set_status(_id(view), "active", tmp_path)
    assert resumed["schedules"][0]["status"] == "active"
    removed = schedule.set_status(_id(view), "removed", tmp_path)
    assert removed["schedules"] == []


def test_due_trigger_is_saved_once_and_observe_does_not_start(tmp_path):
    schedule.create_schedule("600519", "09:30", tmp_path)
    _created_before_due(tmp_path)
    calls = []

    def starter(trigger):
        calls.append(trigger["id"])
        return "run-should-not"

    first = schedule.fire_due(DUE, tmp_path, starter=starter)
    second = schedule.fire_due(DUE, tmp_path, starter=starter)
    assert calls == []
    assert len(first["triggers"]) == 1
    assert len(second["triggers"]) == 1
    assert first["checks"][-1]["result"] == "would_accept"
    assert first["checks"][-1]["started"] is False
    assert first["bindings"] == []


def test_paused_schedule_does_not_fire(tmp_path):
    view = schedule.create_schedule("NVDA", "09:30", tmp_path)
    schedule.set_status(_id(view), "paused", tmp_path)
    fired = schedule.fire_due(DUE, tmp_path)
    assert fired["triggers"] == []


def test_version_change_discards_and_rechecks_without_starting(tmp_path):
    schedule.create_schedule("XAUUSD", "09:30", tmp_path)
    _created_before_due(tmp_path)
    calls = []
    schedule.fire_due(DUE, tmp_path, version_during_check="sched-2", starter=lambda trigger: calls.append(trigger))
    view = schedule.list_schedules(tmp_path)
    assert calls == []
    assert view["checks"][-1]["result"] == "discarded"
    assert view["rechecks"][0]["rule_version"] == "sched-2"
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["rule_version"] = "sched-2"
    path.write_text(json.dumps(data), encoding="utf-8")
    again = schedule.recheck(tmp_path, starter=lambda trigger: calls.append(trigger))
    assert calls == []
    assert again["checks"][-1]["result"] == "would_accept"
    assert again["rechecks"] == []


def test_same_trigger_starts_once_only_when_a_starter_is_injected(tmp_path):
    schedule.create_schedule("BTC-USDT", "09:30", tmp_path)
    _created_before_due(tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "auto"
    path.write_text(json.dumps(data), encoding="utf-8")
    calls = []

    def starter(trigger):
        calls.append(trigger["id"])
        return "run-1"

    schedule.fire_due(DUE, tmp_path, starter=starter)
    trigger_id = schedule.list_schedules(tmp_path)["triggers"][0]["id"]
    again = schedule.check_trigger(trigger_id, tmp_path, starter=starter)
    assert calls == [trigger_id]
    assert again["duplicate"] is True
    assert again["run_id"] == "run-1"


def test_manual_run_is_allowed_in_observe_and_is_idempotent(tmp_path):
    view = schedule.create_schedule("600519", "09:30", tmp_path)
    calls = []

    def starter(trigger):
        calls.append(trigger["id"])
        return "run-m"

    now = datetime(2026, 11, 12, 10, 5, tzinfo=TZ)
    first = schedule.manual_run(_id(view), tmp_path, now=now, starter=starter)
    second = schedule.manual_run(_id(view), tmp_path, now=now, starter=starter)
    assert first["action"] == "wizard"
    assert first["started"] is True
    assert calls == [first["trigger_id"]]
    assert second["run_id"] == "run-m"
    assert second["duplicate"] is True


def test_reminder_is_saved_before_send_and_retries_without_dropping(tmp_path):
    now = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    seen = {}

    def sender(receipt):
        saved = json.loads((tmp_path / "schedule.json").read_text(encoding="utf-8"))
        row = next(item for item in saved["receipts"] if item["id"] == receipt["id"])
        seen["status_before_send"] = row["status"]
        raise RuntimeError("down")

    schedule.observe_events(
        [{"kind": "completion", "symbol": "600519", "run_id": "run-9", "rating": "Buy"}],
        tmp_path,
        now=now,
    )
    schedule.deliver(tmp_path, now=now, sender=sender)
    assert seen["status_before_send"] == "pending"
    failed = schedule.list_schedules(tmp_path)["reminders"][0]
    assert failed["status"] == "pending"
    assert failed["attempts"] == 1
    calls = []
    schedule.deliver(tmp_path, now=now, sender=lambda receipt: calls.append(receipt["id"]))
    assert calls == []
    retried = schedule.deliver(tmp_path, now=now + timedelta(seconds=5), sender=lambda receipt: calls.append("go"))
    assert calls == ["go"]
    assert retried["reminders"][0]["status"] == "sent"

    def fail(receipt):
        raise RuntimeError("still down")

    schedule.observe_events(
        [{"kind": "decision", "symbol": "600519", "old": "Hold", "new": "Buy"}],
        tmp_path,
        now=now,
    )
    for step in range(3):
        schedule.deliver(tmp_path, now=now + timedelta(seconds=400 * step), sender=fail)
    pending = next(item for item in schedule.list_schedules(tmp_path)["reminders"] if item["kind"] == "decision")
    assert pending["status"] == "pending"
    assert pending["system_problem"] is True
    schedule.drop_reminder(pending["id"], "用户关闭", tmp_path, now=now)
    again = schedule.enqueue_reminder("decision", pending["id"], "不应重新入队", tmp_path, now=now)
    assert again["queued"] is False
    assert again["status"] == "dropped"
    calls.clear()
    schedule.deliver(tmp_path, now=now + timedelta(hours=1), sender=lambda receipt: calls.append(receipt["id"]))
    assert pending["id"] not in calls


def test_blank_mode_is_not_called_observing(tmp_path):
    schedule.create_schedule("600519", "09:30", tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.list_schedules(tmp_path)
    row = view["schedules"][0]
    assert row["status_label"] == "状态未知"
    assert "观察" not in row["status_label"]
    assert view["mode"] != "observe"
    assert view["note"].startswith("当前规则状态未知，不把它写成观察模式")
    assert not view["note"].startswith("到点只记录触发")
    _backdate(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    fired = schedule.fire_due(DUE, tmp_path)
    assert fired["schedules"][0]["status_label"] == "状态未知"
    assert "不把它写成观察模式" in fired["checks"][-1]["reason"]
    assert not fired["checks"][-1]["reason"].startswith("观察模式")
    assert fired["checks"][-1]["started"] is False


def test_page_has_schedule_entry():
    html = Path("webui/static/index.html").read_text(encoding="utf-8")
    js = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'data-page="schedule"' in html
    assert "观察模式不会自动启动分析" in js
    label = js.split("function scheduleModeLabel", 1)[1].split("function schedulePageDescription", 1)[0]
    assert 'if (mode === "observe" || !mode) return "观察"' not in label
    assert 'if (!mode || !knownScheduleMode(mode)) return "状态未知"' in label
    assert 'if (mode === "observe") return "观察"' in label


def test_next_morning_records_yesterdays_slot_without_backfilling_every_day(tmp_path):
    schedule.create_schedule("600519", "09:30", tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schedules"][0]["created_at"] = "2026-11-10T08:00:00+08:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    morning = datetime(2026, 11, 12, 8, 0, tzinfo=TZ)
    view = schedule.fire_due(morning, tmp_path)
    assert [item["slot"] for item in view["triggers"]] == ["2026-11-11"]
    assert view["checks"][-1]["result"] == "would_accept"
    assert view["checks"][-1]["started"] is False
    later = schedule.fire_due(datetime(2026, 11, 12, 10, 0, tzinfo=TZ), tmp_path)
    assert [item["slot"] for item in later["triggers"]] == ["2026-11-11", "2026-11-12"]


def test_a_slot_before_creation_is_not_due_the_next_morning(tmp_path):
    schedule.create_schedule("AAPL", "09:30", tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schedules"][0]["created_at"] = "2026-11-11T10:00:00+08:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.fire_due(datetime(2026, 11, 12, 8, 0, tzinfo=TZ), tmp_path)
    assert view["triggers"] == []


def test_console_tick_records_a_due_slot_without_the_schedule_page(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    from webui import server

    before = tmp_path / "schedule.json"
    server.tick_open_schedules()
    assert not before.exists()
    schedule.create_schedule("600519", "09:30", tmp_path)
    data = json.loads(before.read_text(encoding="utf-8"))
    data["schedules"][0]["created_at"] = "2026-11-11T08:00:00+08:00"
    before.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(schedule, "_now_dt", lambda now=None: datetime(2026, 11, 11, 10, 0, tzinfo=TZ))
    server.tick_open_schedules()
    view = schedule.list_schedules(tmp_path)
    assert view["triggers"][0]["slot"] == "2026-11-11"
    assert view["checks"][-1]["started"] is False


def test_old_reports_are_not_reminders_until_a_new_one_appears(tmp_path, monkeypatch):
    schedule.create_schedule("600519", "09:30", tmp_path)
    reports = [{"ticker": "600519", "name": "600519_old", "rating": "Hold"}]

    def fake_reports(limit=40):
        return list(reports)

    monkeypatch.setattr("webui.service.list_reports", fake_reports)
    moment = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    first = schedule.tick(moment, tmp_path, observe_local=True)
    assert first["reminders"] == []
    reports.append({"ticker": "AAPL", "name": "AAPL_new", "rating": "Buy"})
    second = schedule.tick(moment + timedelta(minutes=1), tmp_path, observe_local=True)
    assert [item["body"] for item in second["reminders"]] == ["AAPL 的分析已完成，记录里有 Buy，但不把它当成结构化评级。"]
    third = schedule.tick(moment + timedelta(minutes=2), tmp_path, observe_local=True)
    assert [item["body"] for item in third["reminders"]] == ["AAPL 的分析已完成，记录里有 Buy，但不把它当成结构化评级。"]


def test_a_new_report_without_a_decision_is_not_called_a_rating(tmp_path, monkeypatch):
    schedule.create_schedule("AAPL", "09:30", tmp_path)
    reports = [{"ticker": "AAPL", "name": "AAPL_old", "rating": "Hold", "has_decision": False}]

    def fake_reports(limit=40):
        return list(reports)

    monkeypatch.setattr("webui.service.list_reports", fake_reports)
    moment = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    assert schedule.tick(moment, tmp_path, observe_local=True)["reminders"] == []
    reports.append({"ticker": "AAPL", "name": "AAPL_new", "rating": "Hold", "has_decision": False})
    second = schedule.tick(moment + timedelta(minutes=1), tmp_path, observe_local=True)
    assert [item["body"] for item in second["reminders"]] == [
        "AAPL 的分析已完成，报告头写着 Hold，没有组合经理结论。",
    ]
    assert "评级 Hold" not in second["reminders"][0]["body"]


def test_a_known_decision_reminder_still_names_the_rating(tmp_path, monkeypatch):
    schedule.create_schedule("AAPL", "09:30", tmp_path)
    reports = [{"ticker": "AAPL", "name": "AAPL_old", "rating": "Hold", "has_decision": True}]

    def fake_reports(limit=40):
        return list(reports)

    monkeypatch.setattr("webui.service.list_reports", fake_reports)
    moment = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    assert schedule.tick(moment, tmp_path, observe_local=True)["reminders"] == []
    reports.append({"ticker": "AAPL", "name": "AAPL_new", "rating": "Buy", "has_decision": True})
    second = schedule.tick(moment + timedelta(minutes=1), tmp_path, observe_local=True)
    assert [item["body"] for item in second["reminders"]] == ["AAPL 的分析已完成，评级 Buy。"]


def test_isolated_home_does_not_remind_about_reports_outside_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(home))
    outside = tmp_path / "other" / "600519_old" / "complete_report.md"
    reports = [{"ticker": "600519", "name": "600519_old", "rating": "Hold", "markdown": str(outside)}]

    def fake_reports(limit=40):
        return list(reports)

    monkeypatch.setattr("webui.service.list_reports", fake_reports)
    schedule.create_schedule("600519", "09:30", home)
    moment = datetime(2026, 11, 12, 8, 0, tzinfo=TZ)
    first = schedule.tick(moment, home, observe_local=True)
    assert first["reminders"] == []
    inside = home / "results" / "reports" / "AAPL_new" / "complete_report.md"
    reports.append({"ticker": "AAPL", "name": "AAPL_new", "rating": "Buy", "markdown": str(inside)})
    second = schedule.tick(moment + timedelta(minutes=1), home, observe_local=True)
    assert [item["body"] for item in second["reminders"]] == ["AAPL 的分析已完成，记录里有 Buy，但不把它当成结构化评级。"]


def test_decision_change_across_symbol_alias_reminds_once(tmp_path, monkeypatch):
    schedule.create_schedule("600519", "09:30", tmp_path)
    ratings = {"600519": "Hold"}
    monkeypatch.setattr("webui.watchlist.local_decisions", lambda: dict(ratings))
    monkeypatch.setattr("webui.service.list_reports", lambda limit=40: [])
    moment = datetime(2026, 11, 12, 8, 0, tzinfo=TZ)
    first = schedule.tick(moment, tmp_path, observe_local=True)
    assert [item for item in first["reminders"] if item.get("kind") == "decision"] == []
    ratings.clear()
    ratings["600519.SS"] = "Buy"
    second = schedule.tick(moment + timedelta(minutes=1), tmp_path, observe_local=True)
    bodies = [item["body"] for item in second["reminders"] if item.get("kind") == "decision"]
    assert bodies == ["600519.SS 的决定从 Hold 变为 Buy。"]
    third = schedule.tick(moment + timedelta(minutes=2), tmp_path, observe_local=True)
    assert [item["body"] for item in third["reminders"] if item.get("kind") == "decision"] == bodies


def test_the_same_decision_transition_can_remind_again_later(tmp_path):
    first = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    schedule.observe_events(
        [{"kind": "decision", "symbol": "600519", "old": "Hold", "new": "Buy"}],
        tmp_path,
        now=first,
    )
    schedule.observe_events(
        [{"kind": "decision", "symbol": "600519", "old": "Buy", "new": "Hold"}],
        tmp_path,
        now=first + timedelta(hours=1),
    )
    schedule.observe_events(
        [{"kind": "decision", "symbol": "600519.SS", "old": "Hold", "new": "Buy"}],
        tmp_path,
        now=first + timedelta(hours=2),
    )
    bodies = [item["body"] for item in schedule.list_schedules(tmp_path)["reminders"] if item.get("kind") == "decision"]
    assert bodies.count("600519.SS 的决定从 Hold 变为 Buy。") == 2
    again = schedule.observe_events(
        [{"kind": "decision", "symbol": "600519", "old": "Hold", "new": "Buy"}],
        tmp_path,
        now=first + timedelta(hours=2),
    )
    bodies = [item["body"] for item in again["reminders"] if item.get("kind") == "decision"]
    assert bodies.count("600519.SS 的决定从 Hold 变为 Buy。") == 2


def test_a_slot_during_pause_is_not_recorded_after_resume(tmp_path):
    view = schedule.create_schedule("600519", "09:30", tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schedules"][0]["created_at"] = "2026-11-10T08:00:00+08:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    schedule.set_status(_id(view), "paused", tmp_path, now=datetime(2026, 11, 11, 8, 0, tzinfo=TZ))
    schedule.set_status(_id(view), "active", tmp_path, now=datetime(2026, 11, 12, 10, 0, tzinfo=TZ))
    fired = schedule.fire_due(datetime(2026, 11, 12, 10, 30, tzinfo=TZ), tmp_path)
    assert fired["triggers"] == []
    later = schedule.fire_due(datetime(2026, 11, 13, 10, 0, tzinfo=TZ), tmp_path)
    assert [item["slot"] for item in later["triggers"]] == ["2026-11-13"]

def test_latest_due_is_kept_when_the_recent_window_moves_on(tmp_path):
    now = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    first = schedule.create_schedule("600519", "09:30", tmp_path)
    first_id = first["schedules"][0]["id"]
    for index in range(12):
        schedule.create_schedule(f"SYM{index:02d}", "09:30", tmp_path)
    _created_before_due(tmp_path)
    fired = schedule.fire_due(now, tmp_path)
    assert len(fired["triggers"]) == 10
    kept = next(item for item in fired["schedules"] if item["id"] == first_id)
    assert kept["latest_due"]["slot"] == "2026-11-12"
    assert kept["latest_check"]["result"] == "would_accept"
    assert kept["latest_due"]["id"] not in {item["id"] for item in fired["triggers"]}
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "item.latest_due" in script
    assert "已暂停。暂停期间的到点不会补记。" in script
    assert "schedule_id === item.id && trigger.kind" not in script

def test_latest_due_follows_the_newest_slot_not_append_order(tmp_path):
    now = datetime(2026, 11, 12, 10, 0, tzinfo=TZ)
    view = schedule.create_schedule("600519", "09:30", tmp_path)
    ident = view["schedules"][0]["id"]
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schedules"][0]["created_at"] = "2026-11-10T08:00:00+08:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    schedule.fire_due(now, tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    newer = next(item for item in data["triggers"] if item.get("schedule_id") == ident)
    older = dict(newer)
    older["id"] = newer["id"] + "-old"
    older["slot"] = "2026-11-11"
    data["triggers"].append(older)
    path.write_text(json.dumps(data), encoding="utf-8")
    listed = schedule.list_schedules(tmp_path)
    kept = next(item for item in listed["schedules"] if item["id"] == ident)
    assert kept["latest_due"]["slot"] == "2026-11-12"

def test_pending_reminder_stays_visible_outside_the_recent_window(tmp_path):
    schedule.create_schedule("600519", "09:30", tmp_path)
    for index in range(25):
        schedule.enqueue_reminder("completion", f"completion|run-{index}", f"提醒 {index}", tmp_path, symbol="600519")
    schedule.deliver(tmp_path, sender=lambda item: None)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["receipts"][0]["status"] = "pending"
    data["receipts"][0]["system_problem"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.list_schedules(tmp_path)
    pending = [item for item in view["reminders"] if item["status"] == "pending"]
    assert [item["body"] for item in pending] == ["提醒 0"]
    assert pending[0]["system_problem"] is True
    assert view["reminders"][0]["body"] == "提醒 0"
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert 'data-sched-symbol="${escapeHtml(item.symbol)}">去分析</button>' in script
    assert "立即分析" not in script
    assert "去分析只打开向导" in script

def test_dropped_reminder_stays_visible_outside_the_recent_window(tmp_path):
    schedule.create_schedule("600519", "09:30", tmp_path)
    for index in range(25):
        schedule.enqueue_reminder("completion", f"completion|drop-{index}", f"丢弃 {index}", tmp_path, symbol="600519")
    schedule.deliver(tmp_path, sender=lambda item: None)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["receipts"][0]["status"] = "dropped"
    data["receipts"][0]["drop_reason"] = "不再需要"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.list_schedules(tmp_path)
    dropped = [item for item in view["reminders"] if item["status"] == "dropped"]
    assert [item["body"] for item in dropped] == ["丢弃 0"]
    assert dropped[0]["status_label"] == "已丢弃"
    assert view["reminders"][0]["body"] == "丢弃 0"



def _backdate(tmp_path):
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data["schedules"]:
        item["created_at"] = "2026-11-01T00:00:00+08:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_observe_due_note_says_it_would_accept_and_not_start(tmp_path):
    schedule.create_schedule("600519", "09:30", tmp_path)
    _backdate(tmp_path)
    view = schedule.fire_due(DUE, tmp_path)
    note = view["schedules"][0]["due_note"]
    assert note == "11-12 09:30 到点会接受，未启动"
    assert "已启动" not in note


def test_admitted_check_without_a_run_is_not_called_started(tmp_path):
    schedule.create_schedule("BTC-USDT", "09:30", tmp_path)
    path = _backdate(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "auto"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.fire_due(DUE, tmp_path)
    note = view["schedules"][0]["due_note"]
    assert note == "11-12 09:30 已检查，未启动"
    assert view["schedules"][0]["latest_check"]["run_id"] is None
    assert "已启动" not in note


def test_start_flag_without_a_run_is_not_a_schedule_start(tmp_path):
    schedule.create_schedule("XAUUSD", "09:30", tmp_path)
    _backdate(tmp_path)
    schedule.fire_due(DUE, tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"][-1]["result"] = "admitted"
    data["checks"][-1]["started"] = True
    data["checks"][-1]["run_id"] = None
    path.write_text(json.dumps(data), encoding="utf-8")
    note = schedule.list_schedules(tmp_path)["schedules"][0]["due_note"]
    assert note == "11-12 09:30 已检查，未启动"
    assert not note.endswith("已启动")


def test_admitted_run_is_called_started(tmp_path):
    schedule.create_schedule("AAPL", "09:30", tmp_path)
    path = _backdate(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "auto"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.fire_due(DUE, tmp_path, starter=lambda trigger: "run-1")
    note = view["schedules"][0]["due_note"]
    assert note == "11-12 09:30 已启动"
    assert "未启动" not in note


def test_closed_rule_names_the_refusal(tmp_path):
    schedule.create_schedule("MSFT", "09:30", tmp_path)
    path = _backdate(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "off"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.fire_due(DUE, tmp_path)
    assert view["schedules"][0]["due_note"] == "11-12 09:30 规则已关闭，未启动"
    assert view["schedules"][0]["status_label"] == "规则已关闭"
    assert view["schedules"][0]["status_label"] != "启用"
    assert "观察模式" not in view["note"]


def test_discarded_check_is_not_called_started(tmp_path):
    schedule.create_schedule("NVDA", "09:30", tmp_path)
    _backdate(tmp_path)
    schedule.fire_due(DUE, tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"][-1]["result"] = "discarded"
    data["checks"][-1]["started"] = True
    data["checks"][-1]["run_id"] = "run-1"
    path.write_text(json.dumps(data), encoding="utf-8")
    note = schedule.list_schedules(tmp_path)["schedules"][0]["due_note"]
    assert note == "11-12 09:30 规则已变化，未按旧版本启动"
    assert "已启动" not in note


def test_paused_without_a_due_names_the_pause(tmp_path):
    view = schedule.create_schedule("IBM", "09:30", tmp_path)
    paused = schedule.set_status(view["schedules"][0]["id"], "paused", tmp_path)
    assert paused["schedules"][0]["due_note"] == "已暂停。暂停期间的到点不会补记。"
    assert paused["schedules"][0]["status_label"] == "已暂停"


def test_page_uses_the_schedule_due_note():
    script = Path("webui/static/app.js").read_text(encoding="utf-8")
    assert "function scheduleDueNote" in script
    assert "function schedulePageDescription" in script
    assert "function scheduleRuleText" in script
    assert "规则已关闭。到点不会启动分析。" in script
    assert "function reminderRetryText" in script
    assert "item.due_note" in script
    assert "已检查，未启动" in script
    assert "规则已关闭，到点不会启动。" in script
    assert 'item.status !== "pending"' in script
    assert 'item.system_problem ? " 发送失败，仍会重试。"' not in script
    assert ': `${when} 未启动`' not in script
    assert "检查结果未知，不把它写成未启动或已启动。" in script
    assert 'return `${when} 未启动`' not in script
    assert "function scheduleShownLabel" in script
    assert "定时状态未知，不把它写成观察中，也不写成可以暂停。" in script


def test_unknown_schedule_item_status_is_not_called_observing(tmp_path):
    schedule.create_schedule("600519", "09:30", tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schedules"][0]["status"] = "mystery"
    path.write_text(json.dumps(data), encoding="utf-8")
    row = schedule.list_schedules(tmp_path)["schedules"][0]
    assert row["status_label"] == "状态未知"
    assert "观察" not in row["status_label"]
    assert "启用" not in row["status_label"]
    assert row["due_note"] == "定时状态未知，不把它写成观察中，也不写成可以暂停。"
    assert "还没有到点触发" not in row["due_note"]
    assert "mystery" not in row["due_note"]


def test_unknown_check_result_is_not_called_not_started(tmp_path):
    schedule.create_schedule("600519", "09:30", tmp_path)
    _backdate(tmp_path)
    schedule.fire_due(DUE, tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checks"][-1]["result"] = "mystery"
    data["checks"][-1]["started"] = True
    data["checks"][-1]["run_id"] = "run-9"
    path.write_text(json.dumps(data), encoding="utf-8")
    note = schedule.list_schedules(tmp_path)["schedules"][0]["due_note"]
    assert note == "11-12 09:30 检查结果未知，不把它写成未启动或已启动。"
    assert "mystery" not in note
    assert not note.endswith("未启动")
    assert not note.endswith("已启动")


def test_closed_rule_is_not_labeled_enabled(tmp_path):
    schedule.create_schedule("MSFT", "09:30", tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "off"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.list_schedules(tmp_path)
    assert view["mode_label"] == "关闭"
    assert view["schedules"][0]["status_label"] == "规则已关闭"
    assert "启用" not in view["schedules"][0]["status_label"]
    assert "规则已关闭" in view["note"]


def test_auto_mode_is_not_called_enabled(tmp_path):
    schedule.create_schedule("AAPL", "09:30", tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "auto"
    path.write_text(json.dumps(data), encoding="utf-8")
    label = schedule.list_schedules(tmp_path)["schedules"][0]["status_label"]
    assert label == "自动"
    assert label != "启用"


def test_unknown_mode_is_not_called_enabled(tmp_path):
    schedule.create_schedule("IBM", "09:30", tmp_path)
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["mode"] = "later"
    path.write_text(json.dumps(data), encoding="utf-8")
    view = schedule.list_schedules(tmp_path)
    assert view["schedules"][0]["status_label"] == "状态未知"
    assert view["mode_label"] == "状态未知"
    assert view["mode_label"] != "later"
    assert "启用" not in view["schedules"][0]["status_label"]


def test_closed_recheck_is_a_refusal_not_observe(tmp_path):
    schedule.create_schedule("MSFT", "09:30", tmp_path)
    _created_before_due(tmp_path)
    calls = []
    schedule.fire_due(DUE, tmp_path, version_during_check="sched-2", starter=lambda trigger: calls.append(trigger))
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["rule_version"] = "sched-2"
    data["mode"] = "off"
    path.write_text(json.dumps(data), encoding="utf-8")
    again = schedule.recheck(tmp_path, starter=lambda trigger: calls.append(1))
    assert calls == []
    assert again["checks"][-1]["result"] == "refused"
    assert "观察模式" not in again["checks"][-1]["reason"]
    assert again["schedules"][0]["due_note"] == "11-12 09:30 规则已关闭，未启动"
    assert again["schedules"][0]["status_label"] == "规则已关闭"
    assert again["rechecks"] == []


def test_dropped_reminder_does_not_keep_a_retry_flag(tmp_path):
    schedule.enqueue_reminder("completion", "completion|drop-flag", "已丢弃的提醒", tmp_path, symbol="MSFT")
    path = tmp_path / "schedule.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["receipts"][0]["system_problem"] = True
    path.write_text(json.dumps(data), encoding="utf-8")
    pending = schedule.list_schedules(tmp_path)["reminders"][0]
    assert pending["status"] == "pending"
    assert pending["system_problem"] is True
    dropped = schedule.drop_reminder(pending["id"], "不再需要", tmp_path)["reminders"][0]
    assert dropped["status"] == "dropped"
    assert dropped["status_label"] == "已丢弃"
    assert dropped["system_problem"] is False
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["receipts"][0]["system_problem"] is False
    stored["receipts"][0]["status"] = "dropped"
    stored["receipts"][0]["system_problem"] = True
    path.write_text(json.dumps(stored), encoding="utf-8")
    stale = schedule.list_schedules(tmp_path)["reminders"][0]
    assert stale["status"] == "dropped"
    assert stale["system_problem"] is False
