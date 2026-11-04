"""Where crawl jobs and their items live: jsonl files (atomic writes) or memory (tests)."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from tradingagents.dataflows.files import replace_file
from tradingagents.dataflows.info_crawl.base import InfoItem, iso_utc, utc_now
from tradingagents.redaction import redact_secrets

RETENTION_DAYS = 30
_JOB_ID = re.compile(r"^[a-z0-9-]{6,64}$")


_ITEM_ID = re.compile(r"^[0-9a-f]{16}$")


def valid_item_id(item_id) -> bool:
    return isinstance(item_id, str) and bool(_ITEM_ID.match(item_id))


def valid_job_id(job_id) -> bool:
    return isinstance(job_id, str) and bool(_JOB_ID.match(job_id))


INTERRUPTED_REASON = "抓取没有完成（服务重启或写盘失败），请重新抓取。"

# Jobs whose crawl thread runs in this process (AUDIT-CHECK-03 / W159). A job
# saved as "running" that is not here was cut off by a restart or a failed write.
_ACTIVE: set[str] = set()
_ACTIVE_LOCK = threading.Lock()


def mark_active(job_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.add(job_id)


def mark_finished(job_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(job_id)


def is_active(job_id: str) -> bool:
    with _ACTIVE_LOCK:
        return job_id in _ACTIVE


def settle_job(job: dict | None) -> dict | None:
    """A loaded job as the API may show it: keyword redacted, and a "running" job
    with no live crawl thread reported as interrupted, never running forever."""
    if not job:
        return job
    job = dict(job)
    if "keyword" in job:
        job["keyword"] = redact_secrets(str(job.get("keyword") or ""))
    if job.get("status") == "running" and not is_active(str(job.get("id"))):
        job["status"] = "interrupted"
        job["reason"] = INTERRUPTED_REASON
        job["finished_at"] = job.get("finished_at") or iso_utc(utc_now())
    return job


def dedupe(items: list[InfoItem]) -> list[InfoItem]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        out.append(item)
    return out


class MemoryItemStore:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.items: dict[str, list[InfoItem]] = {}
        self._lock = threading.Lock()

    def save_job(self, job: dict) -> None:
        with self._lock:
            self.jobs[job["id"]] = json.loads(json.dumps(job))

    def load_job(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        return settle_job(json.loads(json.dumps(job))) if job else None

    def list_jobs(self, limit: int = 20) -> list[dict]:
        rows = sorted(self.jobs.values(), key=lambda job: job.get("created_at", ""), reverse=True)
        return [settle_job(json.loads(json.dumps(job))) for job in rows[:limit]]

    def save_items(self, job_id: str, items: list[InfoItem]) -> None:
        with self._lock:
            self.items[job_id] = dedupe(list(items))

    def load_items(self, job_id: str, source: str | None = None) -> list[InfoItem]:
        rows = list(self.items.get(job_id, []))
        return [item for item in rows if not source or item.source == source]

    def purge(self, days: int = RETENTION_DAYS, now: float | None = None) -> int:
        return 0


class JsonlItemStore:
    """``<root>/<job>.jsonl`` holds items, ``<root>/<job>.job.json`` the job. Files older than ``days`` are purged."""

    def __init__(self, root, days: int = RETENTION_DAYS):
        self.root = Path(root)
        self.days = days
        self._lock = threading.Lock()

    def _path(self, job_id: str, suffix: str) -> Path:
        if not valid_job_id(job_id):
            raise ValueError("任务编号不合法")
        return self.root / f"{job_id}{suffix}"

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_file(path, lambda temp: Path(temp).write_text(text, encoding="utf-8"))

    def save_job(self, job: dict) -> None:
        with self._lock:
            self._write(self._path(job["id"], ".job.json"), json.dumps(job, ensure_ascii=False))

    def load_job(self, job_id: str) -> dict | None:
        try:
            return settle_job(json.loads(self._path(job_id, ".job.json").read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def list_jobs(self, limit: int = 20) -> list[dict]:
        if not self.root.is_dir():
            return []
        files = sorted(self.root.glob("*.job.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        jobs = []
        for path in files:
            try:
                jobs.append(settle_job(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
        return jobs

    def save_items(self, job_id: str, items: list[InfoItem]) -> None:
        text = "".join(json.dumps(item.to_dict(), ensure_ascii=False) + "\n" for item in dedupe(list(items)))
        with self._lock:
            self._write(self._path(job_id, ".jsonl"), text)

    def load_items(self, job_id: str, source: str | None = None) -> list[InfoItem]:
        try:
            lines = self._path(job_id, ".jsonl").read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                item = InfoItem.from_dict(json.loads(line))
            except (ValueError, TypeError):
                continue  # a damaged line is skipped, not fatal
            if not source or item.source == source:
                out.append(item)
        return out

    def purge(self, days: int | None = None, now: float | None = None) -> int:
        if not self.root.is_dir():
            return 0
        cutoff = (now or time.time()) - (self.days if days is None else days) * 86400
        removed = 0
        for path in list(self.root.glob("*.jsonl")) + list(self.root.glob("*.job.json")):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed


def default_store() -> JsonlItemStore:
    from tradingagents.dataflows.config import get_config
    return JsonlItemStore(Path(get_config()["data_cache_dir"]) / "info-crawl")
