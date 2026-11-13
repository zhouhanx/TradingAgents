"""V2-INFO-CRAWL console API: sources, jobs and items (session token on every call)."""

from __future__ import annotations

import contextlib
import threading
from pathlib import Path

from tradingagents.dataflows.info_crawl import EXISTING_SOURCE_NOTES, SOURCES, run_job, sources_for
from tradingagents.dataflows.info_crawl.base import NO_TIME_NOTE, iso_utc, utc_now
from tradingagents.dataflows.info_crawl.jobs import JobRequestError, new_job
from tradingagents.dataflows.info_crawl.registry import ASSET_TYPES
from tradingagents.dataflows.info_crawl.store import (
    JsonlItemStore,
    default_store,
    mark_active,
    mark_finished,
    valid_job_id,
)
from tradingagents.redaction import redact_secrets

ASSET_LABELS = {"stock": "股票", "crypto": "加密货币", "macro": "宏观"}


class InfoCrawlError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_STORE = None
_STORE_EXPLICIT = False
_STORE_LOCK = threading.Lock()


def _configured_default_store(candidate):
    """Return the crawl store for the cache directory the console currently uses."""
    try:
        from webui.service import effective_cache_dir

        root = Path(effective_cache_dir()).resolve() / "info-crawl"
        if isinstance(candidate, JsonlItemStore) and candidate.root.resolve() != root:
            return JsonlItemStore(root, days=candidate.days)
    except (OSError, ValueError):
        # Keep the process-wide/default store if an old or malformed config
        # cannot be resolved. The settings validator rejects such paths before
        # a run starts.
        pass
    return candidate


def store():
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            # The console may persist a cache directory independently of the
            # process-wide TradingAgents config. Crawl requests are served by
            # this process, while selected rows are consumed by a worker with
            # the run config; resolve the same effective directory here.
            _STORE = _configured_default_store(default_store())
            with contextlib.suppress(OSError):
                _STORE.purge()
        elif not _STORE_EXPLICIT and isinstance(_STORE, JsonlItemStore):
            # Saving a new cache directory while the console is open must move
            # subsequent crawl reads/writes too; do not pin the first directory
            # for the process lifetime.
            current = _configured_default_store(_STORE)
            if current is not _STORE:
                _STORE = current
                with contextlib.suppress(OSError):
                    _STORE.purge()
        return _STORE


def set_store(value) -> None:
    """Tests swap in a MemoryItemStore."""
    global _STORE, _STORE_EXPLICIT
    with _STORE_LOCK:
        _STORE = value
        _STORE_EXPLICIT = value is not None


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
    mark_active(job["id"])  # before the first save, so a poll never sees it interrupted
    try:
        target.save_job(job)
    except Exception as exc:  # noqa: BLE001 - never leave a job id marked active
        mark_finished(job["id"])
        job["status"] = "failed"
        job["reason"] = redact_secrets(f"抓取任务没有保存成功（{type(exc).__name__}），请重新抓取。")
        job["finished_at"] = iso_utc(utc_now())
        with contextlib.suppress(Exception):
            target.save_job(job)
        return {"job": job}
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
    target = store()
    job = target.load_job(job_id)
    if job is None:
        raise InfoCrawlError("没有这个抓取任务，可能已超过 30 天被清理。", 404)
    items = [item.to_dict() | {"source_name": SOURCES[item.source].name_zh if item.source in SOURCES else item.source}
             | ({"inject_note": NO_TIME_NOTE} if not item.published_at else {})
             for item in target.load_items(job_id, source)]
    return {"job": job, "items": items}
