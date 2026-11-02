"""Running a crawl job over several sources, and reading its items back for analysis."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

from tradingagents.dataflows.info_crawl.base import (
    InfoQuery,
    SourceUnavailable,
    iso_utc,
    utc_now,
    within,
)
from tradingagents.dataflows.info_crawl.registry import ASSET_TYPES, SOURCES
from tradingagents.dataflows.info_crawl.store import dedupe, valid_item_id, valid_job_id
from tradingagents.redaction import redact_secrets

ALLOWED_HOURS = (24, 72, 168)
PROMPT_ITEM_LIMIT = 30


class JobRequestError(ValueError):
    """A crawl request the API rejects; the message is Chinese."""


def new_job(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise JobRequestError("请求内容必须是 JSON 对象")
    keyword = str(payload.get("keyword") or "").strip()
    if not keyword or len(keyword) > 80:
        raise JobRequestError("请填写标的代码或关键词（不超过 80 个字符）")
    asset = str(payload.get("asset_type") or "stock").strip().lower()
    if asset not in ASSET_TYPES:
        raise JobRequestError("资产类型只能是 股票、加密货币 或 宏观")
    try:
        hours = int(payload.get("hours") or 72)
    except (TypeError, ValueError):
        hours = -1
    if hours not in ALLOWED_HOURS:
        raise JobRequestError("时间范围只能是 24 小时、3 天或 7 天")
    raw_sources = payload.get("sources")
    if raw_sources is None:
        wanted = [sid for sid, adapter in SOURCES.items() if asset in adapter.assets and not adapter.optional]
    elif isinstance(raw_sources, list) and all(isinstance(item, str) for item in raw_sources):
        wanted = list(dict.fromkeys(raw_sources))
    else:
        raise JobRequestError("来源列表格式不正确")
    unknown = [sid for sid in wanted if sid not in SOURCES]
    if unknown:
        raise JobRequestError(f"未知来源：{'、'.join(unknown)}")
    wanted = [sid for sid in wanted if asset in SOURCES[sid].assets]
    if not wanted:
        raise JobRequestError("请至少选择一个适用于该资产类型的来源")
    return {
        "id": f"ic-{utc_now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}",
        "keyword": keyword, "asset_type": asset, "hours": hours, "status": "running",
        "created_at": iso_utc(utc_now()), "finished_at": None,
        "sources": {sid: {"status": "pending", "count": 0, "reason": ""} for sid in wanted},
    }


def run_job(job: dict, store, *, sources=None, cache=None) -> dict:
    """Fetch every source of ``job``; one failure only marks that source 不可用."""
    registry = sources or SOURCES
    query = InfoQuery(keyword=job["keyword"], asset_type=job["asset_type"], hours=int(job["hours"]))
    store.save_job(job)
    collected = []

    def one(source_id):
        adapter = registry[source_id]
        ok, reason = adapter.availability()
        if not ok:
            return source_id, None, reason
        try:
            return source_id, adapter.collect(query, cache=cache), ""
        except SourceUnavailable as exc:
            return source_id, None, redact_secrets(str(exc))
        except Exception as exc:  # noqa: BLE001 - one source never breaks the job
            return source_id, None, redact_secrets(f"来源处理失败（{type(exc).__name__}）")

    with ThreadPoolExecutor(max_workers=max(1, min(6, len(job["sources"])))) as pool:
        for source_id, items, reason in pool.map(one, list(job["sources"])):
            if items is None:
                job["sources"][source_id] = {"status": "unavailable", "count": 0, "reason": reason or "来源暂不可用"}
                continue
            hours = max(query.hours, registry[source_id].min_window_hours)
            kept = [item for item in items if within(item, hours)]
            collected.extend(kept)
            job["sources"][source_id] = {"status": "ok", "count": len(kept), "reason": ""}
    items = sorted(dedupe(collected), key=lambda item: item.published_at, reverse=True)
    store.save_items(job["id"], items)
    states = [row["status"] for row in job["sources"].values()]
    job["status"] = "failed" if all(state == "unavailable" for state in states) else ("partial" if "unavailable" in states else "done")
    job["finished_at"] = iso_utc(utc_now())
    job["total"] = len(items)
    store.save_job(job)
    return job


def format_items_for_prompt(items, limit: int = PROMPT_ITEM_LIMIT) -> str:
    """Items as prompt lines, each with its ``[来源·日期·链接]`` citation."""
    lines = []
    for item in list(items)[:limit]:
        name = SOURCES[item.source].name_zh if item.source in SOURCES else item.source
        lines.append(f"- {item.title} [{name}·{item.published_at[:10]}·{item.url}]")
    return "\n".join(lines)


def get_info_items(job_id: str, item_ids=None, store=None) -> str:
    """Router method: only the rows the user ticked (``item_ids``) of ``job_id``.

    No ids means nothing is brought in: 「带入分析」 is opt-in per row (PM).
    """
    if not valid_job_id(job_id):
        return ""
    wanted = [item for item in (item_ids or []) if valid_item_id(item)]
    if not wanted:
        return ""
    if store is None:
        from tradingagents.dataflows.info_crawl.store import default_store
        store = default_store()
    chosen = set(wanted)
    return format_items_for_prompt([item for item in store.load_items(job_id) if item.id in chosen])
