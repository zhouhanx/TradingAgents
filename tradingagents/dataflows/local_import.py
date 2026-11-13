"""Import a user-exported CSV or JSON file as local analysis evidence.

This path never fetches a network. It only reads text the caller already has.
"""

from __future__ import annotations

import csv
import json
import re
from io import StringIO

from tradingagents.redaction import redact_secrets

MAX_BYTES = 512 * 1024
MAX_ROWS = 50
FIELD_LIMIT = 500
CITATION = "本地导入"

_TEXT_KEYS = ("title", "summary", "content", "text", "标题", "摘要", "正文", "内容")
_DATE_KEYS = ("published_at", "date", "published", "日期", "时间")
_URL_KEYS = ("url", "link", "链接")
_DATE = re.compile(r"^(\d{4})[-/](\d{2})[-/](\d{2})")
_BOM = "\ufeff"


class LocalImportError(ValueError):
    """A file the user can fix. The message is Chinese and safe to show."""


def parse_local_import(raw: str, filename: str = "") -> list[dict]:
    """Return citation-ready rows, or raise ``LocalImportError``."""
    if not isinstance(raw, str) or not raw.strip():
        raise LocalImportError("文件是空的。")
    if raw.startswith(_BOM):
        raw = raw[1:]
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise LocalImportError("文件超过 512KB，请拆小后再导入。")
    name = _safe_name(filename)
    kind = _kind(name, raw)
    rows = _parse_json(raw) if kind == "json" else _parse_csv(raw)
    items = [_row(row, name) for row in rows]
    items = [item for item in items if item]
    if not items:
        raise LocalImportError("没有可导入的资料行。请至少包含标题、摘要或正文。")
    if len(items) > MAX_ROWS:
        raise LocalImportError(f"一次最多导入 {MAX_ROWS} 条，请拆分文件。")
    return items


def format_local_import_lines(items) -> str:
    """Prompt lines. Each citation is ``[本地导入·日期·标题]`` and is not a URL fetch."""
    lines = []
    for item in list(items or [])[:MAX_ROWS]:
        if not isinstance(item, dict):
            continue
        title = _quote(item.get("title") or item.get("summary") or "")
        if not title:
            continue
        day = item.get("published_at") or "未注明日期"
        lines.append(f"- {title} [{CITATION}·{day}·{title}]")
    return "\n".join(lines)


def local_import_report_section(items) -> str:
    """Markdown appended to the news report so the citation does not depend on the model."""
    lines = format_local_import_lines(items)
    if not lines:
        return ""
    return (
        "## 本地导入\n\n"
        "以下内容来自用户导入的本地文件，不是联网抓取。引用形式为 [本地导入·日期·标题]。\n\n"
        f"{lines}"
    )


def _kind(filename: str, raw: str) -> str:
    lower = filename.lower()
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".csv"):
        return "csv"
    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return "json"
    if "," in raw.splitlines()[0] or "\t" in raw.splitlines()[0]:
        return "csv"
    raise LocalImportError("只支持 CSV 或 JSON。")


def _parse_json(raw: str) -> list:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LocalImportError("JSON 无法解析，请检查括号和引号。") from exc
    if isinstance(data, dict):
        data = data.get("items", data.get("data"))
    if not isinstance(data, list):
        raise LocalImportError("JSON 需要是对象数组，或包含 items 数组的对象。")
    return data


def _parse_csv(raw: str) -> list[dict]:
    try:
        sample = raw[:4096]
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(StringIO(raw), dialect=dialect)
    if not reader.fieldnames:
        raise LocalImportError("CSV 需要表头，并至少有一列标题、摘要或正文。")
    fields = {_norm(name) for name in reader.fieldnames if name}
    if not fields & set(_TEXT_KEYS):
        raise LocalImportError("CSV 需要表头，并至少有一列标题、摘要或正文。")
    return list(reader)


def _row(row, filename: str) -> dict | None:
    if isinstance(row, str):
        row = {"content": row}
    if not isinstance(row, dict):
        return None
    mapped = {_norm(key): value for key, value in row.items() if key is not None}
    title = _field(mapped, ("title", "标题"))
    summary = _field(mapped, ("summary", "摘要", "content", "text", "正文", "内容"))
    if not title and not summary:
        return None
    if not title:
        title = summary[:80]
    published = _date(_field(mapped, _DATE_KEYS))
    url = _field(mapped, _URL_KEYS)
    return {
        "title": title,
        "summary": summary or title,
        "published_at": published,
        "source": CITATION,
        "url": url,
        "filename": filename,
    }


def _field(mapped: dict, keys) -> str:
    for key in keys:
        value = mapped.get(_norm(key))
        if value is None:
            continue
        text = _limit(value)
        if text:
            return text
    return ""


def _limit(value) -> str:
    text = " ".join(str(value or "").split())
    text = redact_secrets(text)
    for marker in ("<<<", ">>>"):
        text = text.replace(marker, " ")
    text = text.replace("[", "(").replace("]", ")").strip()
    if len(text) <= FIELD_LIMIT:
        return text
    return text[: FIELD_LIMIT - 1].rstrip() + "…"


def _date(value: str) -> str:
    match = _DATE.match(value or "")
    if not match:
        return ""
    return "-".join(match.groups())


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _quote(value: str) -> str:
    return _limit(value)


def _safe_name(filename: str) -> str:
    name = str(filename or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")
    return name[:80]
