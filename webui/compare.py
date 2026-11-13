"""V2-REPORT-COMPARE: side-by-side decisions and replay of a saved prompt version.

Replay reads the prompt text stored on the run. It does not read the current
source. A free-text decision is not turned into evidence quotes.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import secrets
from pathlib import Path

from webui.service import webui_home

MISSING_EVIDENCE = "没有结构化证据引用，不从正文猜测。"
NO_STRUCTURED_RATING = "没有结构化评级。"
SAVED_PROMPT = "回放使用这次运行保存的提示词版本，不用当前提示词。"
NO_SAVED_PROMPT = "这一步没有单独保存的提示词版本。"
NO_PROMPT_BODY = "这次运行没有保存提示词正文，不能回放提示词版本。"
# English agent names match the progress timeline. Unknown ids stay unlabeled.
PROMPT_LABELS = {
    "market_analyst": ("Market Analyst", "市场分析师"),
    "sentiment_analyst": ("Sentiment Analyst", "情绪分析师"),
    "news_analyst": ("News Analyst", "新闻分析师"),
    "fundamentals_analyst": ("Fundamentals Analyst", "基本面分析师"),
    "bull_researcher": ("Bull Researcher", "多头研究员"),
    "bear_researcher": ("Bear Researcher", "空头研究员"),
    "research_manager": ("Research Manager", "研究经理"),
    "trader": ("Trader", "交易员"),
    "aggressive_analyst": ("Aggressive Analyst", "激进分析师"),
    "neutral_analyst": ("Neutral Analyst", "中性分析师"),
    "conservative_analyst": ("Conservative Analyst", "保守分析师"),
    "portfolio_manager": ("Portfolio Manager", "组合经理"),
    "memory_reflection": ("Memory Log", "记忆日志"),
}
# Replay steps per page. A longer run pages instead of losing its early steps.
REPLAY_PAGE_SIZE = 40


class CompareError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _prompt_sources() -> tuple[tuple[str, tuple], ...]:
    """Every role's prompt function, in pipeline order.

    A role whose prompt text is built by a helper outside its factory lists that
    helper too. Portfolio and research manager keep one function each, so a run
    saved before every role was frozen still matches its old hash.
    """
    from tradingagents.agents.analysts import sentiment_analyst
    from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
    from tradingagents.agents.analysts.market_analyst import create_market_analyst
    from tradingagents.agents.analysts.news_analyst import create_news_analyst
    from tradingagents.agents.analysts.turn import take_turn
    from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
    from tradingagents.agents.managers.research_manager import create_research_manager
    from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
    from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
    from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
    from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
    from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
    from tradingagents.agents.trader.trader import create_trader
    from tradingagents.memory.reflection import Reflector

    return (
        ("market_analyst", (create_market_analyst, take_turn)),
        ("sentiment_analyst", (sentiment_analyst.create_sentiment_analyst,
                               sentiment_analyst._build_system_message, take_turn)),
        ("news_analyst", (create_news_analyst, take_turn)),
        ("fundamentals_analyst", (create_fundamentals_analyst, take_turn)),
        ("bull_researcher", (create_bull_researcher,)),
        ("bear_researcher", (create_bear_researcher,)),
        ("research_manager", (create_research_manager,)),
        ("trader", (create_trader,)),
        ("aggressive_analyst", (create_aggressive_debator,)),
        ("neutral_analyst", (create_neutral_debator,)),
        ("conservative_analyst", (create_conservative_debator,)),
        ("portfolio_manager", (create_portfolio_manager,)),
        ("memory_reflection", (Reflector._system_prompt,)),
    )


def capture_prompt_versions() -> list[dict]:
    """Every role's prompt functions as they are now. A run stores this copy at start."""
    rows = []
    for name, funcs in _prompt_sources():
        text = "\n\n".join(inspect.getsource(func) for func in funcs)
        rows.append({
            "id": name,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "text": text,
        })
    return rows


PROMPT_STORE = "prompt-versions"


def _prompt_store(root: Path | None) -> Path:
    return (Path(root) if root is not None else webui_home()) / PROMPT_STORE


def _store_prompt_text(text: str, digest: str, root: Path | None) -> bool:
    """Keep one copy of each prompt body by its hash. False when it cannot be written."""
    folder = _prompt_store(root)
    target = folder / f"{digest}.txt"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            return True
        temporary = folder / f".{digest}.{secrets.token_hex(4)}.tmp"
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(target)
    except OSError:
        return False
    return True


def _saved_text(item: dict, root: Path | None) -> str:
    """The saved prompt body: inline on older runs, else the stored copy whose hash matches."""
    inline = item.get("text")
    if isinstance(inline, str) and inline.strip():
        return inline
    digest = str(item.get("sha256") or "")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        return ""
    try:
        text = (_prompt_store(root) / f"{digest}.txt").read_text(encoding="utf-8")
    except OSError:
        return ""
    # A stored file that no longer matches its name is not the saved version.
    return text if hashlib.sha256(text.encode("utf-8")).hexdigest() == digest else ""


def freeze_prompt_versions(record: dict, versions: list[dict] | None = None,
                           root: Path | None = None) -> dict:
    """Save every role's prompt version on the run.

    The run record keeps the id and hash only; each body is stored once under
    <home>/prompt-versions/<sha256>.txt, so thirteen prompts do not grow every
    run record. A body that cannot be stored stays inline on the record.
    """
    rows = []
    for item in versions if versions is not None else capture_prompt_versions():
        text = str(item.get("text") or "")
        digest = str(item.get("sha256") or "")
        if text and digest and _store_prompt_text(text, digest, root):
            rows.append({"id": item.get("id"), "sha256": digest})
        else:
            rows.append(dict(item))
    record["prompt_versions"] = rows
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
    # New runs always save report_sections; a run saved before that only has
    # shown_report, and only when --show was ticked.
    shown = payload.get("report_sections")
    if not isinstance(shown, dict):
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



def _prompt_row(item: dict, live_hash: dict, *, include_text: bool, root: Path | None = None) -> dict:
    """Public prompt row. Chinese label comes from the known id, never from the text."""
    name = item.get("id")
    agent, zh = PROMPT_LABELS.get(name, ("", ""))
    saved_hash = item.get("sha256") or ""
    text = _saved_text(item, root)
    has_text = bool(text.strip())
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
        row["text"] = text
    return row


def _view(record: dict, current: list[dict] | None = None, root: Path | None = None) -> dict:
    decision = _decision(record, root)
    saved = record.get("prompt_versions") if isinstance(record.get("prompt_versions"), list) else []
    current_by_id = {item["id"]: item.get("sha256") for item in (current or [])}
    prompts = []
    for item in saved:
        if not isinstance(item, dict):
            continue
        prompts.append(_prompt_row(item, current_by_id, include_text=False, root=root))
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


def _page_number(raw) -> int:
    try:
        number = int(str(raw if raw is not None else 1).strip())
    except ValueError as exc:
        raise CompareError("回放页码必须是正整数。") from exc
    if number < 1:
        raise CompareError("回放页码必须是正整数。")
    return number


def replay_run(run_id: str, root: Path | None = None, *, current: list[dict] | None = None,
               page=1) -> dict:
    """One page of a run's steps, oldest first. A long run pages; nothing is dropped."""
    records = {str(item.get("id")): item for item in _runs(root)}
    record = records.get(str(run_id or ""))
    if record is None:
        raise CompareError("没有找到要回放的运行。", 404)
    number = _page_number(page)
    from webui.progress import progress_timeline

    selected = (record.get("selections") or {}).get("analysts") if isinstance(record.get("selections"), dict) else None
    lines = _log_lines(record, root)
    every = list(reversed(progress_timeline(lines, selected, limit=len(lines) + 1)))
    total = len(every)
    pages = max(1, -(-total // REPLAY_PAGE_SIZE))
    if number > pages:
        raise CompareError("回放没有这一页。", 404)
    first = (number - 1) * REPLAY_PAGE_SIZE
    steps = every[first:first + REPLAY_PAGE_SIZE]
    saved = [item for item in (record.get("prompt_versions") or []) if isinstance(item, dict)]
    live = current if current is not None else capture_prompt_versions()
    live_hash = {item["id"]: item.get("sha256") for item in live}
    prompts = [_prompt_row(item, live_hash, include_text=True, root=root) for item in saved]
    uses_saved = any(item.get("saved") for item in prompts)
    return {
        "id": record.get("id"),
        "ticker": record.get("ticker") or "",
        "steps": steps,
        "page": number,
        "pages": pages,
        "page_size": REPLAY_PAGE_SIZE,
        "total_steps": total,
        "first_step": first + 1 if steps else 0,
        "prompts": prompts,
        "uses_saved_prompts": uses_saved,
        "note": SAVED_PROMPT if uses_saved else NO_PROMPT_BODY,
    }
