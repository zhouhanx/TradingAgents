"""Analysis frameworks the console can start (WEBUI-008).

启动分析 lists these as cards; a run records which one it used. Adding a
framework is one more :data:`FRAMEWORKS` entry: the API, the run records and the
status page read everything from here. Only frameworks that really run are listed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

DEFAULT_FRAMEWORK = "tradingagents"


@dataclass(frozen=True)
class Framework:
    id: str
    name: str
    description: str
    icon: str
    tags: tuple[str, ...] = ()
    # "module:function" the worker calls with the job dict (W66). Not public.
    runner: str = "webui.service:default_runner"


FRAMEWORKS: tuple[Framework, ...] = (
    Framework(
        id="tradingagents",
        name="TradingAgents",
        description="多智能体交易研究。分析师、研究员、交易员、风控和组合经理分工协作，给出评级和交易计划。",
        icon="/icons/logo.svg",
        tags=("股票", "加密货币", "4 步设置"),
    ),
)

_BY_ID = {item.id: item for item in FRAMEWORKS}

# V2-INFO-CRAWL: cards on 启动分析 that open a tool page (#start/<id>) rather than
# a run wizard. They are not frameworks: nothing launches through /api/runs.
TOOLS: tuple[Framework, ...] = (
    Framework(
        id="info-crawl",
        name="信息抓取",
        description="从官方公开来源（SEC 公告、交易所公告、央行与财政部新闻稿、GDELT 新闻索引）汇总资讯，可勾选「带入分析」。",
        icon="/icons/logo.svg",
        tags=("公告", "新闻", "默认不带入分析"),
        runner="",
    ),
)


def public_tools() -> list[dict]:
    rows = []
    for item in TOOLS:
        row = {**asdict(item), "tags": list(item.tags), "tool": True}
        row.pop("runner", None)
        rows.append(row)
    return rows


class UnknownFramework(ValueError):
    """A launch named a framework that is not registered."""


def public_frameworks(records: list[dict] | None = None) -> list[dict]:
    """GET /api/frameworks rows, in registry order.

    ``last_used`` (PM ruling) is the start time of the framework's most recent run
    among ``records`` (analyses; 回测 runs are not counted), queued and failed runs included; a run from before WEBUI-008
    counts as TradingAgents. A framework without runs has no ``last_used`` key.
    """
    latest: dict[str, str] = {}
    for record in records or []:
        if not isinstance(record, dict) or record.get("kind") == "backtest":
            continue  # 回测 has its own page; the card counts analysis runs
        stamp = record.get("started_at") or record.get("queued_at")
        if not isinstance(stamp, str) or not stamp:
            continue
        key = framework_of(record)
        if stamp > latest.get(key, ""):
            latest[key] = stamp
    rows = []
    for item in FRAMEWORKS:
        row = {**asdict(item), "tags": list(item.tags)}
        row.pop("runner", None)  # internal: how the worker starts it
        if item.id in latest:
            row["last_used"] = latest[item.id]
        rows.append(row)
    return rows


def available_sentence() -> str:
    return "、".join(f"{item.name}（{item.id}）" for item in FRAMEWORKS)


def resolve_framework(raw) -> str:
    """The registered framework id a launch asked for; no value means the default.

    W68 (PM ruling, accepted as is): the launch API ``framework`` field is
    matched after stripping surrounding whitespace and lowercasing, so
    ``"TradingAgents"`` or ``" tradingagents "`` start TradingAgents; a missing,
    null, empty or blank value means the default ``tradingagents``. Anything
    else unknown or not text is the Chinese 400 and nothing starts. The worker
    is strict (:func:`job_framework`): job files always hold the resolved id.
    See webui/API-NOTES.md.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return DEFAULT_FRAMEWORK
    if not isinstance(raw, str):
        raise UnknownFramework(f"分析框架必须是文字 id。可用的分析框架：{available_sentence()}。没有启动任何分析。")
    key = raw.strip().lower()
    if key not in _BY_ID:
        shown = raw.strip()
        shown = shown if len(shown) <= 64 else shown[:64] + "…"
        raise UnknownFramework(f"没有这个分析框架：{shown}。可用的分析框架：{available_sentence()}。没有启动任何分析。")
    return key


def framework_of(record: dict | None) -> str:
    """The framework a stored run used; a run from before WEBUI-008 has none and is TradingAgents."""
    value = (record or {}).get("framework")
    return value if isinstance(value, str) and value else DEFAULT_FRAMEWORK


def framework_name(framework_id: str) -> str:
    item = _BY_ID.get(framework_id)
    if item:
        return item.name
    # W67: a run from a framework that is no longer registered.
    shown = str(framework_id)
    shown = shown if len(shown) <= 64 else shown[:64] + "…"
    return f"未知工具（{shown}）"


def framework_fields(record: dict | None) -> dict:
    """``framework`` and ``framework_name`` for an API answer about one run."""
    key = framework_of(record)
    return {"framework": key, "framework_name": framework_name(key)}


def job_framework(job: dict | None) -> str:
    """The framework a job file names, for the worker (W66).

    A job from before WEBUI-008 has no ``framework`` (or null) and is TradingAgents.
    Anything else must be an exactly registered id (the console only ever writes
    resolved ids); otherwise :class:`UnknownFramework` and nothing is started.
    """
    raw = (job or {}).get("framework")
    if raw is None:
        return DEFAULT_FRAMEWORK
    if isinstance(raw, str) and raw in _BY_ID:
        return raw
    shown = raw if isinstance(raw, str) else type(raw).__name__
    shown = shown if len(shown) <= 64 else shown[:64] + "…"
    raise UnknownFramework(
        f"任务文件里的分析框架 {shown} 没有注册，没有启动分析。可用的分析框架：{available_sentence()}。")


def runner_for(framework_id: str):
    """The callable that runs one job of ``framework_id`` (W66)."""
    from importlib import import_module

    item = _BY_ID.get(framework_id)
    if item is None:
        raise UnknownFramework(f"没有这个分析框架：{framework_id}。没有启动分析。")
    module, _, name = item.runner.partition(":")
    return getattr(import_module(module), name)
