"""V2-REPORT-COMPARE: side-by-side decisions and replay of a saved prompt version.

Replay reads the prompt text stored on the run. It does not read the current
source. A free-text decision is not turned into evidence quotes.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

from webui.service import webui_home

MISSING_EVIDENCE = "没有结构化证据引用，不从正文猜测。"
NO_STRUCTURED_RATING = "没有结构化评级。"
SAVED_PROMPT = "回放使用这次运行保存的提示词版本，不用当前提示词。"
NO_SAVED_PROMPT = "这一步没有单独保存的提示词版本。"
NO_PROMPT_BODY = "这次运行没有保存提示词正文，不能回放提示词版本。"
# English agent names match the progress timeline. Unknown ids stay unlabeled.
PROMPT_LABELS = {
    "portfolio_manager": ("Portfolio Manager", "组合经理"),
    "research_manager": ("Research Manager", "研究经理"),
}


class CompareError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def capture_prompt_versions() -> list[dict]:
    """The prompt functions as they are now. A run stores this copy at start."""
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    from tradingagents.agents.managers.research_manager import create_research_manager

    rows = []
    for name, func in (
        ("portfolio_manager", create_portfolio_manager),
        ("research_manager", create_research_manager),
    ):
        text = inspect.getsource(func)
        rows.append({
            "id": name,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text": text,
        })
    return rows


def freeze_prompt_versions(record: dict, versions: list[dict] | None = None) -> dict:
    record["prompt_versions"] = versions if versions is not None else capture_prompt_versions()
    return record


def _runs(root: Path | None) -> list[dict]:
    if root is None:
        from webui import service

        return service.JOBS.list_runs()
    folder = Path(root) / "runs"
    records = []
    if folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                data.setdefault("id", path.stem)
                records.append(data)
    return records


def _under_logs(path: Path, root: Path | None) -> bool:
    try:
        resolved = path.resolve()
        allowed = ((Path(root) if root is not None else webui_home()) / "logs").resolve()
    except OSError:
        return False
    return resolved == allowed or allowed in resolved.parents


def _result(record: dict, root: Path | None = None) -> dict:
    raw = record.get("result")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    path = Path(raw)
    if not path.is_file() or not _under_logs(path, root):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}



def _rating_label(source: str, rating: str) -> str:
    """A stored free-text rating is not a structured decision."""
    if source == "structured":
        return f"评级 {rating or '—'}"
    if rating:
        return f"记录里有 {rating}，但不把它当成结构化评级。"
    return NO_STRUCTURED_RATING


def _decision(record: dict, root: Path | None = None) -> dict:
    payload = _result(record, root)
    raw = record.get("decision") if isinstance(record.get("decision"), dict) else payload.get("decision_record")
    if isinstance(raw, dict) and raw.get("source") == "structured" and isinstance(raw.get("evidence"), list):
        quotes = [str(item) for item in raw["evidence"] if str(item or "").strip()]
        rating = str(raw.get("rating") or "")
        return {
            "rating": rating,
            "rating_label": _rating_label("structured", rating),
            "summary": str(raw.get("summary") or ""),
            "evidence": quotes,
            "source": "structured",
            "note": "",
        }
    rating = ""
    if isinstance(raw, dict):
        rating = str(raw.get("rating") or "")
    if not rating:
        rating = str(payload.get("rating") or record.get("rating") or "")
    return {
        "rating": rating,
        "rating_label": _rating_label("missing", rating),
        "summary": "",
        "evidence": [],
        "source": "missing",
        "note": MISSING_EVIDENCE,
    }


def _sections(record: dict, root: Path | None = None) -> list[dict]:
    payload = _result(record, root)
    shown = payload.get("shown_report") if isinstance(payload.get("shown_report"), dict) else {}
    rows = []
    for section in shown.get("sections") or []:
        if not isinstance(section, dict):
            continue
        texts = []
        for item in section.get("items") or []:
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"])[:400])
        if texts:
            rows.append({"heading": str(section.get("heading") or ""), "text": "\n".join(texts)})
        if len(rows) == 4:
            break
    return rows



def _prompt_row(item: dict, live_hash: dict, *, include_text: bool) -> dict:
    """Public prompt row. Chinese label comes from the known id, never from the text."""
    name = item.get("id")
    agent, zh = PROMPT_LABELS.get(name, ("", ""))
    saved_hash = item.get("sha256") or ""
    has_text = bool(str(item.get("text") or "").strip())
    live = live_hash.get(name)
    changed = None if not saved_hash or not has_text or not live else live != saved_hash
    row = {
        "id": name,
        "agent": agent,
        "zh": zh,
        "sha256": saved_hash,
        "saved": bool(saved_hash and has_text),
        # None: missing body or missing current source, so do not claim a match.
        "changed": changed,
    }
    if include_text:
        row["text"] = item.get("text") or ""
    return row


def _view(record: dict, current: list[dict] | None = None, root: Path | None = None) -> dict:
    decision = _decision(record, root)
    saved = record.get("prompt_versions") if isinstance(record.get("prompt_versions"), list) else []
    current_by_id = {item["id"]: item.get("sha256") for item in (current or [])}
    prompts = []
    for item in saved:
        if not isinstance(item, dict):
            continue
        prompts.append(_prompt_row(item, current_by_id, include_text=False))
    return {
        "id": record.get("id"),
        "ticker": record.get("ticker") or "",
        "date": record.get("date") or "",
        "rating": decision["rating"],
        "rating_label": decision["rating_label"],
        "source": decision["source"],
        "summary": decision["summary"],
        "evidence": decision["evidence"],
        "evidence_note": decision["note"],
        "sections": _sections(record, root),
        "prompts": prompts,
    }


def list_compare(root: Path | None = None) -> dict:
    rows = []
    for record in _runs(root):
        if record.get("kind", "analysis") != "analysis":
            continue
        decision = _decision(record, root)
        rows.append({
            "id": record.get("id"),
            "ticker": record.get("ticker") or "",
            "date": record.get("date") or "",
            "rating": decision["rating"],
        })
    return {"runs": rows, "note": SAVED_PROMPT}



def _same_ticker(left: str, right: str) -> bool:
    """600519 and 600519.SS are the same code. Empty symbols are not."""
    from webui.watchlist import canonical_symbol

    first, second = canonical_symbol(left), canonical_symbol(right)
    return bool(first and first == second)


def compare_pair(left: str, right: str, root: Path | None = None) -> dict:
    records = {str(item.get("id")): item for item in _runs(root)}
    missing = [item for item in (left, right) if item not in records]
    if missing:
        raise CompareError("没有找到要对比的运行。", 404)
    current = capture_prompt_versions()
    first, second = _view(records[left], current, root), _view(records[right], current, root)
    return {
        "left": first,
        "right": second,
        "same_run": str(left) == str(right),
        "same_ticker": _same_ticker(first.get("ticker"), second.get("ticker")),
        "note": MISSING_EVIDENCE,
    }


def _log_lines(record: dict, root: Path | None) -> list[str]:
    seeded = record.get("timeline_lines")
    if isinstance(seeded, list):
        return [str(line) for line in seeded]
    path = Path(str(record.get("log") or ""))
    base = Path(root) if root is not None else webui_home()
    try:
        resolved = path.resolve()
        allowed = (base / "logs").resolve()
    except OSError:
        return []
    if allowed not in resolved.parents and resolved != allowed:
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def replay_run(run_id: str, root: Path | None = None, *, current: list[dict] | None = None) -> dict:
    records = {str(item.get("id")): item for item in _runs(root)}
    record = records.get(str(run_id or ""))
    if record is None:
        raise CompareError("没有找到要回放的运行。", 404)
    from webui.progress import progress_timeline

    selected = (record.get("selections") or {}).get("analysts") if isinstance(record.get("selections"), dict) else None
    steps = list(reversed(progress_timeline(_log_lines(record, root), selected, limit=40)))
    saved = [item for item in (record.get("prompt_versions") or []) if isinstance(item, dict)]
    live = current if current is not None else capture_prompt_versions()
    live_hash = {item["id"]: item.get("sha256") for item in live}
    prompts = [_prompt_row(item, live_hash, include_text=True) for item in saved]
    uses_saved = any(item.get("saved") for item in prompts)
    return {
        "id": record.get("id"),
        "ticker": record.get("ticker") or "",
        "steps": steps,
        "prompts": prompts,
        "uses_saved_prompts": uses_saved,
        "note": SAVED_PROMPT if uses_saved else NO_PROMPT_BODY,
    }
