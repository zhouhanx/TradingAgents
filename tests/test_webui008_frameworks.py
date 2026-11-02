"""WEBUI-008 backend: the analysis-framework registry, GET /api/frameworks, and the
``framework`` every run records (launch, queue, restart, old records).

启动分析 becomes a page of framework cards; a run says which framework it used so
查看状态 can label it from the backend. Old records without the field are
TradingAgents everywhere (PM).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_run004_concurrency import _kill, home, sleepers  # noqa: F401
from tests.test_webui_security import console  # noqa: F401
from webui import frameworks, service

pytestmark = pytest.mark.unit

REAL_START = service.start_validated_run
SEL = {"ticker": "NVDA", "analysis_date": "2026-10-30", "asset_type": "stock", "analysts": ["market"]}


@pytest.fixture
def demo_framework(monkeypatch):
    """A second registered framework, for tests only: adding one is one registry entry."""
    demo = frameworks.Framework(id="demo", name="Demo", description="测试用框架。", icon="/icons/logo.svg", tags=("股票",))
    registry = (*frameworks.FRAMEWORKS, demo)
    monkeypatch.setattr(frameworks, "FRAMEWORKS", registry)
    monkeypatch.setattr(frameworks, "_BY_ID", {item.id: item for item in registry})
    return demo


# ------------------------------------------------------------------ registry

def test_the_registry_lists_exactly_tradingagents():
    assert [item.id for item in frameworks.FRAMEWORKS] == ["tradingagents"]
    [row] = frameworks.public_frameworks()
    assert row == {
        "id": "tradingagents",
        "name": "TradingAgents",
        "description": "多智能体交易研究。分析师、研究员、交易员、风控和组合经理分工协作，给出评级和交易计划。",
        "icon": "/icons/logo.svg",
        "tags": ["股票", "加密货币", "4 步设置"],
    }
    assert frameworks.DEFAULT_FRAMEWORK == "tradingagents"
    assert Path("webui/static/icons/logo.svg").is_file()


@pytest.mark.parametrize("raw", [None, "", "   ", "tradingagents", "TradingAgents", " tradingagents "])
def test_no_framework_or_tradingagents_resolves_to_tradingagents(raw):
    assert frameworks.resolve_framework(raw) == "tradingagents"


@pytest.mark.parametrize("raw", ["autogen", "trading agents", "../tradingagents", 5, ["tradingagents"], {"id": "x"}, True])
def test_an_unknown_or_non_text_framework_is_refused(raw):
    with pytest.raises(frameworks.UnknownFramework) as refused:
        frameworks.resolve_framework(raw)
    assert "可用的分析框架：TradingAgents（tradingagents）" in str(refused.value)
    assert "没有启动任何分析" in str(refused.value)


def test_a_long_unknown_name_is_shortened_in_the_message():
    with pytest.raises(frameworks.UnknownFramework) as refused:
        frameworks.resolve_framework("x" * 500)
    assert "x" * 64 + "…" in str(refused.value) and "x" * 65 not in str(refused.value)


def test_a_new_registry_entry_is_all_it_takes(demo_framework):
    assert [row["id"] for row in frameworks.public_frameworks()] == ["tradingagents", "demo"]
    assert frameworks.resolve_framework("Demo") == "demo"
    assert frameworks.framework_fields({"framework": "demo"}) == {"framework": "demo", "framework_name": "Demo"}


def test_old_records_are_tradingagents():
    for record in (None, {}, {"framework": None}, {"framework": ""}, {"framework": 7}):
        assert frameworks.framework_fields(record) == {"framework": "tradingagents", "framework_name": "TradingAgents"}
    # a stored id that is no longer registered still shows (as its id), it is not relabelled
    assert frameworks.framework_fields({"framework": "gone"}) == {"framework": "gone", "framework_name": "未知工具（gone）"}  # W67


# ------------------------------------------------------------------ last_used (PM ruling)

def test_last_used_is_the_newest_start_counting_queued_failed_and_old_records(demo_framework):
    records = [
        {"id": "a", "framework": "tradingagents", "status": "finished", "started_at": "2026-10-30T09:00:00"},
        {"id": "b", "status": "failed", "started_at": "2026-10-31T08:00:00"},                      # old record
        {"id": "c", "framework": "tradingagents", "status": "queued", "started_at": None,
         "queued_at": "2026-10-31T13:52:00"},                                                       # queued
        {"id": "d", "framework": "demo", "status": "failed", "started_at": "2026-10-29T10:00:00"},
        {"id": "e", "kind": "backtest", "status": "running", "started_at": "2026-11-01T00:00:00"},  # 回测: not counted
        {"id": "f", "framework": "tradingagents", "status": "running"},                             # no time
    ]
    rows = {row["id"]: row for row in frameworks.public_frameworks(records)}
    assert rows["tradingagents"]["last_used"] == "2026-10-31T13:52:00"
    assert rows["demo"]["last_used"] == "2026-10-29T10:00:00"


def test_a_framework_without_runs_has_no_last_used(demo_framework):
    rows = {row["id"]: row for row in frameworks.public_frameworks([
        {"id": "a", "status": "failed", "started_at": "2026-10-31T08:00:00"}])}
    assert rows["tradingagents"]["last_used"] == "2026-10-31T08:00:00"
    assert "last_used" not in rows["demo"]
    assert "last_used" not in frameworks.public_frameworks([])[0]


# ------------------------------------------------------------------ GET /api/frameworks

def test_the_frameworks_endpoint_lists_the_registry(console):  # noqa: F811
    status, body, headers = console["send"]("GET", "/api/frameworks")
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    data = json.loads(body)
    assert data["default"] == "tradingagents"
    assert [row["id"] for row in data["frameworks"]] == ["tradingagents"]
    assert "last_used" not in data["frameworks"][0]


def test_the_frameworks_endpoint_reports_last_used_from_the_run_records(console):  # noqa: F811
    service.JOBS.write_run({"id": "old1", "status": "failed", "started_at": "2026-10-31T13:52:00"})
    service.JOBS.write_run({"id": "q1", "framework": "tradingagents", "status": "queued", "started_at": None,
                            "queued_at": "2026-10-31T14:05:00"})
    status, body, _ = console["send"]("GET", "/api/frameworks")
    assert status == 200
    assert json.loads(body)["frameworks"][0]["last_used"] == "2026-10-31T14:05:00"


def test_the_frameworks_endpoint_follows_the_api_security_rules(console):  # noqa: F811
    status, _, _ = console["send"]("GET", "/api/frameworks", token=False)
    assert status == 403
    status, _, _ = console["send"]("GET", "/api/frameworks", headers={"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    status, _, _ = console["send"]("POST", "/api/frameworks", {})
    assert status == 404  # read-only: there is no write route


# ------------------------------------------------------------------ launch

@pytest.fixture
def real_start(console, monkeypatch):  # noqa: F811
    monkeypatch.setattr(service, "start_validated_run", REAL_START)
    validated = []
    monkeypatch.setattr(service, "validate_analysis",
                        lambda body: validated.append(body) or {"selections": dict(SEL), "ignored": []})
    ran = []
    service.set_runner(lambda job: ran.append(job) or 0)
    yield console, ran, validated
    service.set_runner(None)


def test_a_launch_without_a_framework_records_tradingagents(real_start):
    app, ran, _ = real_start
    status, body, _ = app["send"]("POST", "/api/runs", {"background": False})
    assert status == 200
    record = json.loads(body)
    assert record["framework"] == "tradingagents"
    assert service.JOBS.read_run(record["id"])["framework"] == "tradingagents"
    assert ran[0]["framework"] == "tradingagents"


def test_a_launch_naming_a_registered_framework_records_it(real_start, demo_framework):
    app, ran, _ = real_start
    for name, expected in (("tradingagents", "tradingagents"), ("Demo", "demo")):
        status, body, _ = app["send"]("POST", "/api/runs", {"background": False, "framework": name})
        assert status == 200
        record = json.loads(body)
        assert record["framework"] == expected == service.JOBS.read_run(record["id"])["framework"]
    assert [job["framework"] for job in ran] == ["tradingagents", "demo"]


@pytest.mark.parametrize("value", ["autogen", 5, ["tradingagents"]])
def test_an_unknown_framework_is_a_400_and_nothing_starts(real_start, value):
    app, ran, validated = real_start
    status, body, _ = app["send"]("POST", "/api/runs", {"background": False, "framework": value})
    assert status == 400
    error = json.loads(body)["error"]
    assert "分析框架" in error and "没有启动任何分析" in error
    assert ran == [] and validated == [] and service.JOBS.list_runs() == []


def test_the_validate_route_names_the_framework_and_refuses_an_unknown_one(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_validate_analysis", lambda payload: calls.append(payload) or {"selections": {}})
    assert service.validate_analysis({})["framework"] == "tradingagents"
    with pytest.raises(service.ConsoleError) as refused:
        service.validate_analysis({"framework": "autogen"})
    assert refused.value.status == 400 and "没有这个分析框架：autogen" in str(refused.value)
    assert len(calls) == 1  # refused before the run is checked


def test_start_validated_run_refuses_an_unregistered_framework(home):  # noqa: F811
    service.set_runner(lambda job: 0)
    with pytest.raises(service.ConsoleError):
        service.start_validated_run(dict(SEL), background=False, framework="autogen")
    assert service.JOBS.list_runs() == []


# ------------------------------------------------------------------ list / detail

def test_list_and_detail_carry_the_framework_and_old_records_are_tradingagents(home, demo_framework):  # noqa: F811
    service.JOBS.write_run({"id": "old1", "status": "failed", "ticker": "OLD", "started_at": "2026-10-29T10:00:00"})
    service.JOBS.write_run({"id": "new1", "framework": "demo", "status": "failed", "ticker": "NEW",
                            "started_at": "2026-10-30T10:00:00"})
    data = service.api_runs()
    rows = {row["id"]: row for row in data["runs"]}
    assert (rows["old1"]["framework"], rows["old1"]["framework_name"]) == ("tradingagents", "TradingAgents")
    assert (rows["new1"]["framework"], rows["new1"]["framework_name"]) == ("demo", "Demo")
    assert data["run"]["id"] == "new1" and data["run"]["framework_name"] == "Demo"
    old = service.api_runs("old1")["run"]
    assert (old["framework"], old["framework_name"]) == ("tradingagents", "TradingAgents")
    # nothing rewrote the old file
    assert "framework" not in service.JOBS.read_run("old1")


# ------------------------------------------------------------------ restart & queue

def test_restart_keeps_the_framework(home, demo_framework, monkeypatch):  # noqa: F811
    seen = []
    service.set_runner(lambda job: seen.append(job["framework"]) or 0)
    monkeypatch.setattr(service, "remember_run", lambda selections: None)
    service.JOBS.write_run({"id": "r1", "framework": "demo", "status": "failed", "selections": dict(SEL),
                            "checkpoint": {"enabled": True, "saved": True, "scope": "r1"}})
    service.JOBS.write_run({"id": "r0", "status": "failed", "selections": dict(SEL)})  # old record
    first = service.restart_run(run_id="r1")
    second = service.restart_run(run_id="r0")
    assert service.JOBS.read_run(first["id"])["framework"] == "demo"
    assert service.JOBS.read_run(second["id"])["framework"] == "tradingagents"
    assert seen == ["demo", "tradingagents"]


def test_restart_of_a_run_whose_framework_is_gone_starts_nothing(home, monkeypatch):  # noqa: F811
    service.set_runner(lambda job: 0)
    service.JOBS.write_run({"id": "r1", "framework": "gone", "status": "failed", "selections": dict(SEL)})
    calls = []
    real_start = service.start_validated_run
    monkeypatch.setattr(service, "start_validated_run", lambda *a, **k: calls.append(k) or real_start(*a, **k))
    with pytest.raises(service.ConsoleError) as refused:
        service.restart_run(run_id="r1")
    assert refused.value.status == 400
    assert str(refused.value) == "原任务用的分析框架 gone 已不可用，没有重新启动。"
    assert calls == [], "restart refuses before it tries to start anything"
    assert [r["id"] for r in service.JOBS.list_runs()] == ["r1"]


def test_a_queued_run_keeps_its_framework_when_it_starts(sleepers, demo_framework, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TRADINGAGENTS_WEBUI_MAX_RUNS", "1")
    a = service.start_validated_run({**SEL, "ticker": "AAA"})
    b = service.start_validated_run({**SEL, "ticker": "BBB"}, framework="demo")
    assert a["status"] == "running" and b["status"] == "queued" and b["framework"] == "demo"
    job = json.loads(Path(b["result"]).with_name(f"{b['id']}.job.json").read_text(encoding="utf-8"))
    assert job["framework"] == "demo"
    _kill(a)
    started = service.dispatch_queue()
    assert [r["id"] for r in started] == [b["id"]]
    stored = service.JOBS.read_run(b["id"])
    assert stored["status"] == "running" and stored["framework"] == "demo"
    row = next(r for r in service.api_runs()["runs"] if r["id"] == b["id"])
    assert (row["framework"], row["framework_name"]) == ("demo", "Demo")
