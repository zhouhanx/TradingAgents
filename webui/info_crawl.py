"""V2-INFO-CRAWL console API: sources, jobs and items (session token on every call)."""

from __future__ import annotations

import contextlib
import threading

from tradingagents.dataflows.info_crawl import EXISTING_SOURCE_NOTES, SOURCES, run_job, sources_for
from tradingagents.dataflows.info_crawl.jobs import JobRequestError, new_job
from tradingagents.dataflows.info_crawl.registry import ASSET_TYPES
from tradingagents.dataflows.info_crawl.store import default_store, valid_job_id

ASSET_LABELS = {"stock": "股票", "crypto": "加密货币", "macro": "宏观"}


class InfoCrawlError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_STORE = None
_STORE_LOCK = threading.Lock()


def store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = default_store()
            with contextlib.suppress(OSError):
                _STORE.purge()
        return _STORE


def set_store(value) -> None:
    """Tests swap in a MemoryItemStore."""
    global _STORE
    with _STORE_LOCK:
        _STORE = value


def list_sources(asset: str | None = None) -> dict:
    asset = (asset or "").strip().lower() or None
    if asset and asset not in ASSET_TYPES:
        raise InfoCrawlError("资产类型只能是 股票、加密货币 或 宏观")
    return {
        "sources": [adapter.public() for adapter in sources_for(asset)],
        "assets": [{"id": key, "label": ASSET_LABELS[key]} for key in ASSET_TYPES],
        "existing": [dict(row) for row in EXISTING_SOURCE_NOTES],
        "inject_default": False,
    }


def start_job(payload, *, background: bool = True) -> dict:
    try:
        job = new_job(payload)
    except JobRequestError as exc:
        raise InfoCrawlError(str(exc)) from exc
    target = store()
    target.save_job(job)
    if background:
        threading.Thread(target=run_job, args=(job, target), daemon=True, name=f"info-crawl-{job['id']}").start()
        return {"job": job}
    return {"job": run_job(job, target)}


def get_job(job_id: str | None) -> dict:
    if not job_id:
        return {"jobs": store().list_jobs()}
    if not valid_job_id(job_id):
        raise InfoCrawlError("任务编号不合法")
    job = store().load_job(job_id)
    if job is None:
        raise InfoCrawlError("没有这个抓取任务，可能已超过 30 天被清理。", 404)
    return {"job": job}


def get_items(job_id: str | None, source: str | None = None) -> dict:
    if not valid_job_id(job_id):
        raise InfoCrawlError("请提供有效的抓取任务编号")
    if source and source not in SOURCES:
        raise InfoCrawlError("未知来源")
    job = store().load_job(job_id)
    if job is None:
        raise InfoCrawlError("没有这个抓取任务，可能已超过 30 天被清理。", 404)
    items = [item.to_dict() | {"source_name": SOURCES[item.source].name_zh if item.source in SOURCES else item.source}
             for item in store().load_items(job_id, source)]
    return {"job": job, "items": items}
