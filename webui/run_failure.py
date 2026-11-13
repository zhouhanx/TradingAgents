"""What failed in a console run, written by the worker and read by the status page.

The worker process catches the exception that ended a run and writes
``<run>.failure.json`` next to the result file: which role failed, what kind of
failure it was, the provider and model, the timeout and attempts. The console
reads it back to say e.g. 「多头研究员 · Grok 订阅 grok-4.7 调用超时（300 秒，
已重试 3 次）」 instead of only "the process exited without a result".

Everything that leaves this module is redacted: tokens, keys, e-mail
addresses and absolute paths never reach the failure file, the run state or
the log (RUN-002).
"""

from __future__ import annotations

import json
import math
import os
import traceback
from pathlib import Path

from tradingagents.redaction import redact_text

__all__ = ["redact_text"]

FAILURE_SUFFIX = ".failure.json"
ROLE_ATTRIBUTE = "tradingagents_role"
CHECKPOINT_ATTRIBUTE = "tradingagents_checkpoint"
GENERIC_ROLE_ZH = "分析流程"

KIND_ZH = {
    "timeout": "调用超时",
    "connection": "连接失败",
    "rate_limit": "被限流",
    "server": "服务端错误",
    "auth": "登录失效或无权限",
    "other": "调用失败",
    # W70: the job named a framework this install cannot run; no model was called.
    "framework": "分析框架不可用（未调用模型）",
}

_MESSAGE_LIMIT = 600
# An in-process runner's error text on the run record.
ERROR_LIMIT = 2000
# The log keeps the end of a traceback, redacted.
TRACEBACK_LIMIT = 20_000
# A failure file the worker writes is a few KB; anything far bigger is not
# one, and is not read into memory (W28).
MAX_FAILURE_BYTES = 256 * 1024


def failure_path(result_path) -> Path:
    path = Path(result_path)
    return path.with_name(path.stem + FAILURE_SUFFIX)


def _role(exc: BaseException) -> str | None:
    seen = set()
    item = exc
    while item is not None and id(item) not in seen:
        seen.add(id(item))
        role = getattr(item, ROLE_ATTRIBUTE, None)
        if role:
            return role
        item = item.__cause__ or item.__context__
    return None


def provider_label(provider: str | None) -> str | None:
    if not provider:
        return None
    try:
        from tradingagents.llm_clients.subscriptions import SUBSCRIPTIONS
    except ImportError:  # pragma: no cover
        SUBSCRIPTIONS = {}
    if provider in SUBSCRIPTIONS:
        from cli.main import _subscription_label
        return f"{_subscription_label(provider)} 订阅"
    return f"{provider} API"


def build_failure(exc: BaseException, job: dict | None = None) -> dict:
    """The failure record for ``exc``: every string in it is redacted."""
    from tradingagents.llm_clients.call_policy import call_info, classify
    from webui.frameworks import UnknownFramework
    from webui.progress import AGENT_ZH

    info = call_info(exc) or {}
    kind = "framework" if _in_chain(exc, UnknownFramework) else (info.get("kind") or classify(exc)[0])
    role = _role(exc)
    attempts = info.get("attempts")
    message = redact_text(safe_text(f"{type(exc).__name__}: {exc}"), _MESSAGE_LIMIT)
    failure = {
        "role": role,
        "role_zh": AGENT_ZH.get(role, GENERIC_ROLE_ZH) if role else GENERIC_ROLE_ZH,
        "kind": kind,
        "kind_zh": KIND_ZH.get(kind, KIND_ZH["other"]),
        "error_type": type(exc).__name__,
        "message": message,
        "provider": info.get("provider"),
        "provider_label": provider_label(info.get("provider")),
        "model": info.get("model"),
        "timeout": info.get("timeout"),
        "budget": info.get("budget"),
        "attempts": attempts,
        "max_attempts": info.get("max_attempts"),
        "retries": max(attempts - 1, 0) if isinstance(attempts, int) else None,
        "budget_exhausted": bool(info.get("budget_exhausted")),
        "checkpoint": getattr(exc, CHECKPOINT_ATTRIBUTE, None) or _checkpoint_from_chain(exc),
        "run_id": (job or {}).get("id"),
    }
    failure["summary"] = summary(failure)
    try:
        from webui.cost import classify_failure

        classified = classify_failure(exc)
        # unknown means no structured field. Leave it off so a timeout or other
        # call-policy kind is not replaced by a weaker "unknown" label.
        if classified.get("kind") != "unknown":
            health = provider_health(str(classified.get("kind") or ""), bool(classified.get("budget")))
            if health is not None:
                failure["provider_health"] = health
    except Exception:
        pass
    return failure


def _in_chain(exc: BaseException, kind: type) -> bool:
    item, seen = exc, set()
    while item is not None and id(item) not in seen:
        seen.add(id(item))
        if isinstance(item, kind):
            return True
        item = item.__cause__ or item.__context__
    return False


def _checkpoint_from_chain(exc: BaseException):
    item, seen = exc, set()
    while item is not None and id(item) not in seen:
        seen.add(id(item))
        found = getattr(item, CHECKPOINT_ATTRIBUTE, None)
        if found is not None:
            return found
        item = item.__cause__ or item.__context__
    return None


def _seconds(value) -> str | None:
    number = _number(value)
    return None if number is None else f"{number:g}"


def summary(failure: dict) -> str:
    """「多头研究员 · Grok 订阅 grok-4.7 调用超时（300 秒，已重试 3 次）」

    Only fields of the right type are used; anything else falls back to the
    generic words, so a damaged failure file can never break the status page.
    """
    failure = clean_failure(failure) or {}
    who = " ".join(part for part in (failure.get("provider_label"), failure.get("model")) if part)
    role = failure.get("role_zh") or GENERIC_ROLE_ZH
    head = f"{role} · {who} " if who else f"{role} · "
    details = []
    timeout = _seconds(failure.get("timeout"))
    if failure.get("kind") == "timeout" and timeout is not None:
        details.append(f"{timeout} 秒")
    retries = failure.get("retries")
    if retries is not None:
        details.append(f"已重试 {retries} 次" if retries else "未重试")
    budget = _seconds(failure.get("budget"))
    if failure.get("budget_exhausted") and budget is not None:
        details.append(f"已到单次调用总时限 {budget} 秒")
    text = head + (failure.get("kind_zh") or KIND_ZH["other"])
    if details:
        text += "（" + "，".join(details) + "）"
    if not failure.get("provider") and failure.get("message"):
        text += "：" + failure["message"][:200]
    return text


def checkpoint_sentence(checkpoint) -> str | None:
    checkpoint = _checkpoint(checkpoint)
    if not checkpoint or not checkpoint.get("enabled"):
        return None
    if checkpoint.get("saved"):
        return "已保存断点，重新启动会从失败的角色继续，已完成的角色不再调用模型。"
    return "断点续跑已开启，但失败前还没有保存任何步骤，重新启动会从头开始。"


# The failure file's schema. ``clean_failure`` keeps a field only when it has
# the right type, so {"checkpoint": "yes"} or {"timeout": "abc"} just drop that
# field (MF3).
_TEXT_FIELDS = {
    "role": 80, "role_zh": 80, "kind": 40, "kind_zh": 40, "error_type": 120,
    "message": _MESSAGE_LIMIT, "provider": 80, "provider_label": 120, "model": 160,
    "summary": 1000, "run_id": 120,
}
_SECONDS_FIELDS = ("timeout", "budget")
_COUNT_FIELDS = ("attempts", "max_attempts", "retries")


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _count(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
        return None
    return value


def _checkpoint(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    clean = {}
    for key in ("enabled", "saved"):
        item = value.get(key)
        clean[key] = item if isinstance(item, bool) else None
    if clean["enabled"] is None:
        return None
    return clean


def safe_text(value: str) -> str:
    """``value`` as text that encodes to UTF-8: a lone surrogate (valid JSON,
    ``"\\udfff"``) becomes ``?`` instead of breaking every later write (R2-MF1).

    W31 (documented choice): only text that cannot be encoded is replaced. A
    legal supplementary-plane character such as U+10FFFF (JSON ``"\\udbff\\udfff"``)
    is valid UTF-8, so it is kept as-is; it never breaks /api/runs or a run record."""
    return value.encode("utf-8", "replace").decode("utf-8")


_HEALTH_KINDS = frozenset({"rate_limit", "budget_exceeded", "call_timeout", "unknown", "auth", "not_budget"})


def provider_health(kind: str, budget: bool) -> dict | None:
    """Only the structured classification. A mismatched budget flag is dropped."""
    if kind not in _HEALTH_KINDS:
        return None
    flagged = bool(budget)
    if flagged != (kind == "budget_exceeded"):
        return None
    return {"kind": kind, "budget": flagged}


def clean_failure(data) -> dict | None:
    """``data`` reduced to the failure schema, every text field redacted.

    Never raises: a field of the wrong type is dropped (and later shown with
    the generic words); anything that is not a dict gives ``None``. Text is
    cut to its field's length before it is redacted, and made UTF-8 safe.
    """
    if not isinstance(data, dict):
        return None
    clean: dict = {}
    for key, limit in _TEXT_FIELDS.items():
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            clean[key] = redact_text(safe_text(value), limit)
    if clean.get("kind") in KIND_ZH:
        clean["kind_zh"] = KIND_ZH[clean["kind"]]
    else:
        clean.pop("kind", None)
        clean.pop("kind_zh", None)
    for key in _SECONDS_FIELDS:
        number = _number(data.get(key))
        if number is not None:
            clean[key] = number
    for key in _COUNT_FIELDS:
        count = _count(data.get(key))
        if count is not None:
            clean[key] = count
    clean["budget_exhausted"] = data.get("budget_exhausted") is True
    health = provider_health(
        str((data.get("provider_health") or {}).get("kind") or "") if isinstance(data.get("provider_health"), dict) else "",
        bool(isinstance(data.get("provider_health"), dict) and data["provider_health"].get("budget") is True),
    )
    if health is not None:
        clean["provider_health"] = health
    checkpoint = _checkpoint(data.get("checkpoint"))
    if checkpoint is not None:
        clean["checkpoint"] = checkpoint
    return clean


def write_failure(path, failure: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def read_failure(path) -> dict | None:
    target = Path(path)
    try:
        with target.open("rb") as handle:
            raw = handle.read(MAX_FAILURE_BYTES + 1)
        if len(raw) > MAX_FAILURE_BYTES:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    return clean_failure(data)


def redacted_traceback(exc: BaseException) -> str:
    return redact_text(safe_text("".join(traceback.format_exception(exc))), TRACEBACK_LIMIT, tail=True)
