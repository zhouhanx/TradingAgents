"""V2-COST-STATS: frozen role models, provider token totals, separate subscription cost.

Prices come only from this console's cost-prices.json. A missing price stays
unknown. Provider budget is only the structured value budget_exceeded. HTTP 429,
RATE_LIMIT, and this process's wall-clock CallBudgetExceeded are not that.

Usage: the worker writes ``<run>.usage.json`` next to the run's result file
(``usage_path``), finished or failed. Each call row is the provider's own
``usage_metadata`` for that call, attributed to the graph role that made it.
There is no provider total yet (``reported``), so nothing is called reconciled.

Prices: ``<TRADINGAGENTS_WEBUI_HOME>/cost-prices.json`` (default
``~/.tradingagents/webui/cost-prices.json``)::

    {"models": {"<model id as the run saved it>": {
        "input_per_million": "0.50", "output_per_million": "1.50", "currency": "USD"}}}

(Example numbers only, not any provider's price.)

Prices are per million tokens, written as strings or numbers. ``currency``
defaults to USD. Other top-level keys are ignored. A model that is not listed
shows 价格未知. ``webui/cost-prices.example.json`` is an empty template.
A price that is negative, NaN, or not finite (``1e999`` overflows to inf) is
unknown for that model only (W234). A file that is not UTF-8, not JSON (the
``Infinity`` / ``NaN`` literals included), or not ``{"models": {...}}`` is
unreadable: no prices at all, ``prices_unreadable`` true, never an error (W233).
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from webui.service import webui_home

TOLERANCE_TOKENS = 0
TOLERANCE_COST = Decimal("0.000001")
NOT_BUDGET_CODES = frozenset({"AUTH", "RATE_LIMIT", "CONTEXT", "TOKEN_LIMIT"})
BUDGET_VALUE = "budget_exceeded"
NOT_BUDGET_STATUS = frozenset({401, 403, 429})
SUBSCRIPTION_PROVIDERS = frozenset({"chatgpt", "grok"})
PRICES_NAME = "cost-prices.json"
USAGE_SUFFIX = ".usage.json"
USAGE_SCHEMA = "cost-usage-1"
MAX_USAGE_BYTES = 4 * 1024 * 1024

ANALYST_NODES = {
    "market": "Market Analyst",
    "social": "Sentiment Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
QUICK_ROLES = (
    "Market Analyst",
    "Sentiment Analyst",
    "News Analyst",
    "Fundamentals Analyst",
    "Bull Researcher",
    "Bear Researcher",
    "Trader",
    "Aggressive Analyst",
    "Neutral Analyst",
    "Conservative Analyst",
    "Memory Log",
)
DEEP_ROLES = ("Research Manager", "Portfolio Manager")
ROLE_ZH = {
    "Market Analyst": "市场分析师",
    "Sentiment Analyst": "情绪分析师",
    "News Analyst": "新闻分析师",
    "Fundamentals Analyst": "基本面分析师",
    "Bull Researcher": "多头研究员",
    "Bear Researcher": "空头研究员",
    "Research Manager": "研究经理",
    "Trader": "交易员",
    "Aggressive Analyst": "激进分析师",
    "Neutral Analyst": "中立分析师",
    "Conservative Analyst": "保守分析师",
    "Portfolio Manager": "组合经理",
    "Memory Log": "记忆日志",
}


class CostError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _text(value) -> str:
    return str(value or "").strip()


def _provider(selections: dict, tier: str) -> str:
    main = _text(selections.get("llm_provider")).lower()
    chosen = _text(selections.get(f"{tier}_think_provider")).lower()
    return chosen or main


def _model(selections: dict, tier: str) -> str:
    return _text(selections.get(f"{tier}_think_llm"))


def _effort(provider: str, selections: dict) -> tuple[str, str]:
    if provider in {"openai", "chatgpt"}:
        return "openai_reasoning_effort", _text(selections.get("openai_reasoning_effort"))
    if provider == "google":
        return "google_thinking_level", _text(selections.get("google_thinking_level"))
    if provider == "anthropic":
        return "anthropic_effort", _text(selections.get("anthropic_effort"))
    return "", ""


def _billing(provider: str) -> str:
    return "subscription" if provider in SUBSCRIPTION_PROVIDERS else "api"


def _billing_kind(value) -> str:
    """Missing billing is not an API charge."""
    if value == "subscription":
        return "subscription"
    if value == "api":
        return "api"
    return "unknown"


def effort_label(provider: str, effort: str, field: str = "") -> str:
    """Do not call a missing supplier setting 'unset'.

    Grok and other providers have no separate effort field. Only OpenAI,
    Google and Anthropic can be 强度未设置.
    """
    from webui.catalog import reasoning_family

    text = _text(effort)
    if text:
        return text
    if field or reasoning_family(provider):
        return "强度未设置"
    return "无独立强度"


def role_snapshot(selections: dict) -> dict:
    """Freeze the models this run will use. Does not read or write global defaults."""
    selected = selections.get("analysts") or []
    names = [ANALYST_NODES[item] for item in selected if item in ANALYST_NODES]
    names.extend(role for role in QUICK_ROLES if role not in ANALYST_NODES.values())
    roles = []
    for name in (*names, *DEEP_ROLES):
        tier = "deep" if name in DEEP_ROLES else "quick"
        provider = _provider(selections, tier)
        field, effort = _effort(provider, selections)
        roles.append({
            "role": name,
            "zh": ROLE_ZH.get(name, name),
            "tier": tier,
            "provider": provider,
            "model": _model(selections, tier),
            "effort_field": field,
            "effort": effort,
            "billing": _billing(provider),
        })
    return {"source": "run_start", "roles": roles}


def freeze_role_models(record: dict, selections: dict) -> dict:
    """Write the snapshot onto this run record only."""
    record["role_models"] = role_snapshot(selections)
    return record


def _json_dict(value) -> dict | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _chain(exc: BaseException):
    seen = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _structured_maps(item: BaseException) -> list[dict]:
    found = []
    for raw in (getattr(item, "body", None), getattr(item, "details", None)):
        parsed = _json_dict(raw)
        if parsed is None:
            continue
        found.append(parsed)
        nested = parsed.get("error")
        nested_parsed = _json_dict(nested)
        if nested_parsed is not None:
            found.append(nested_parsed)
    return found


def _string_field(item: BaseException, maps: list[dict], name: str) -> str:
    direct = getattr(item, name, None)
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for mapping in maps:
        value = mapping.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _status(item: BaseException, maps: list[dict]) -> int | None:
    status = getattr(item, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(item, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    code = getattr(item, "code", None)
    if isinstance(code, int):
        return code
    for mapping in maps:
        for key in ("status", "code"):
            value = mapping.get(key)
            if isinstance(value, int):
                return value
    return None


def _fields(item: BaseException) -> dict:
    maps = _structured_maps(item)
    return {
        "status": _status(item, maps),
        "type": _string_field(item, maps, "type"),
        "code": _string_field(item, maps, "code"),
    }


def classify_failure(exc: BaseException) -> dict:
    """Classify a provider failure from structured fields only.

    A sentence such as "Budget has been exceeded" is not a structured field.
    This process's wall-clock CallBudgetExceeded is not a provider budget.
    """
    from tradingagents.llm_clients.call_policy import CallBudgetExceeded

    if isinstance(exc, CallBudgetExceeded):
        return {"kind": "call_timeout", "budget": False, "status": None, "type": "", "code": "",
                "note": "这是本机单次调用的时限，不是供应商预算。"}
    seen = [_fields(item) for item in _chain(exc)]
    seen = [item for item in seen if item["status"] is not None or item["type"] or item["code"]]
    if not seen:
        return {"kind": "unknown", "budget": False, "status": None, "type": "", "code": "",
                "note": "没有结构化字段，记为未知。"}
    status = next((item["status"] for item in seen if item["status"] is not None), None)
    types = {item["type"] for item in seen if item["type"]}
    codes = {item["code"] for item in seen if item["code"]}
    if status in NOT_BUDGET_STATUS or codes & NOT_BUDGET_CODES:
        kind = "rate_limit" if status == 429 or "RATE_LIMIT" in codes else "not_budget"
        if status in {401, 403} or "AUTH" in codes:
            kind = "auth"
        return {"kind": kind, "budget": False, "status": status, "type": next(iter(types), ""),
                "code": next(iter(codes), ""), "note": "429、认证和限流不是预算超限。"}
    if BUDGET_VALUE in types or BUDGET_VALUE in codes:
        return {"kind": "budget_exceeded", "budget": True, "status": status,
                "type": BUDGET_VALUE if BUDGET_VALUE in types else "",
                "code": BUDGET_VALUE if BUDGET_VALUE in codes else "",
                "note": "结构化字段为 budget_exceeded。"}
    return {"kind": "unknown", "budget": False, "status": status, "type": next(iter(types), ""),
            "code": next(iter(codes), ""), "note": "没有 budget_exceeded 字段，记为未知。"}


def _count(value) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def reconcile_usage(calls: list[dict], reported: dict | None = None) -> dict:
    """Sum provider usage. The stated tolerance is zero tokens."""
    rows = []
    total_in = 0
    total_out = 0
    for call in calls:
        incoming = _count(call.get("input_tokens"))
        outgoing = _count(call.get("output_tokens"))
        if incoming is None or outgoing is None:
            raise CostError("供应商用量必须是整数。")
        total_in += incoming
        total_out += outgoing
        rows.append({
            "role": _text(call.get("role")),
            "input_tokens": incoming,
            "output_tokens": outgoing,
        })
    if not isinstance(reported, dict) or not reported:
        return {
            "calls": rows,
            "input_tokens": total_in,
            "output_tokens": total_out,
            "tolerance_count": TOLERANCE_TOKENS,
            "matches_provider": False,
            "provider_total": False,
        }
    reported_in = _count(reported.get("input_tokens"))
    reported_out = _count(reported.get("output_tokens"))
    if reported_in is None or reported_out is None:
        raise CostError("供应商合计必须是整数。")
    mismatch = abs(reported_in - total_in) > TOLERANCE_TOKENS or abs(reported_out - total_out) > TOLERANCE_TOKENS
    return {
        "calls": rows,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "tolerance_count": TOLERANCE_TOKENS,
        "matches_provider": not mismatch,
        "provider_total": True,
    }


def _prices_path(root: Path | None) -> Path:
    base = Path(root) if root is not None else webui_home()
    return base / PRICES_NAME


def usage_path(result_path) -> Path:
    """``<run>.usage.json`` beside the run's result file (like ``<run>.failure.json``)."""
    path = Path(result_path)
    return path.with_name(path.stem + USAGE_SUFFIX)


def usage_record(stats: dict) -> dict:
    """What a run keeps for cost: each provider-reported call, plus the calls with none.

    No ``reported`` total: the provider's own total is checked later (REGRESSION-01).
    """
    calls = [dict(row) for row in stats.get("calls") or [] if isinstance(row, dict)]
    return {
        "schema": USAGE_SCHEMA,
        "source": "provider usage_metadata per call",
        "calls": calls,
        "llm_calls": stats.get("llm_calls"),
        "usage_reported": stats.get("usage_reported"),
        "usage_unknown": stats.get("usage_unknown"),
        "input_tokens": stats.get("tokens_in"),
        "output_tokens": stats.get("tokens_out"),
        "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def write_usage(result_path, stats: dict) -> Path:
    target = usage_path(result_path)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(usage_record(stats), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


def _usage_for(record: dict, root: Path | None) -> dict:
    """The run's usage: on the record (older tests and imports), else its usage file under logs."""
    if isinstance(record.get("usage"), dict):
        return record["usage"]
    result = record.get("result")
    if not isinstance(result, str) or not result.strip():
        return {}
    path = usage_path(result)
    try:
        resolved = path.resolve()
        base = ((Path(root) if root is not None else webui_home()) / "logs").resolve()
    except OSError:
        return {}
    if base not in resolved.parents:
        return {}
    try:
        with resolved.open("rb") as handle:
            raw = handle.read(MAX_USAGE_BYTES + 1)
    except OSError:
        return {}
    if len(raw) > MAX_USAGE_BYTES:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


MAX_PRICES_BYTES = 1024 * 1024


def _reject_constant(name: str):
    """Infinity / NaN are not JSON: a file using them is unreadable, not priced (W233)."""
    raise ValueError(f"not JSON: {name}")


def read_prices(root: Path | None = None) -> tuple[dict, bool]:
    """``(models, unreadable)``. A missing file is not an error; a broken one is (W233).

    Not UTF-8, not JSON (including Infinity / NaN literals), too large, or not
    ``{"models": {...}}`` -> no prices at all and ``unreadable`` True. Never raises.
    """
    path = _prices_path(root)
    try:
        if not path.is_file():
            return {}, False
        with path.open("rb") as handle:
            raw = handle.read(MAX_PRICES_BYTES + 1)
        if len(raw) > MAX_PRICES_BYTES:
            return {}, True
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return {}, True
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        return {}, True
    return models, False


def load_prices(root: Path | None = None) -> dict:
    return read_prices(root)[0]


def _unit_price(value) -> Decimal | None:
    """One per-million price: a finite number >= 0, else unknown (W234). 1e999 overflows to inf."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        amount = Decimal(str(value).strip())
        as_float = float(amount)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    if not amount.is_finite() or not math.isfinite(as_float) or amount < 0:
        return None
    return amount


def _price(model: str, prices: dict) -> dict | None:
    row = prices.get(model) if isinstance(prices, dict) else None
    if not isinstance(row, dict):
        return None
    incoming = _unit_price(row.get("input_per_million"))
    outgoing = _unit_price(row.get("output_per_million"))
    if incoming is None or outgoing is None:
        return None
    return {
        "input_per_million": incoming,
        "output_per_million": outgoing,
        "currency": _text(row.get("currency")) or "USD",
    }


def _money(tokens_in: int, tokens_out: int, price: dict) -> Decimal:
    million = Decimal(1_000_000)
    amount = (Decimal(tokens_in) * price["input_per_million"] + Decimal(tokens_out) * price["output_per_million"]) / million
    return amount.quantize(TOLERANCE_COST)


def _money_text(amount: Decimal, currency: str) -> str:
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"{text} {currency}"


def _role_usage(usage: dict) -> dict[str, dict]:
    found = {}
    for call in usage.get("calls") or []:
        role = _text(call.get("role")) or "未归集"
        bucket = found.setdefault(role, {"input_tokens": 0, "output_tokens": 0})
        bucket["input_tokens"] += call["input_tokens"]
        bucket["output_tokens"] += call["output_tokens"]
    return found


def _view_run(record: dict, prices: dict, root: Path | None = None) -> dict:
    snapshot = record.get("role_models")
    if not isinstance(snapshot, dict):
        selections = record.get("selections") if isinstance(record.get("selections"), dict) else {}
        snapshot = role_snapshot(selections)
        snapshot["source"] = "saved_selections"
    usage = _usage_for(record, root)
    calls = usage.get("calls") if isinstance(usage.get("calls"), list) else []
    reconciled = reconcile_usage(calls, usage.get("reported")) if calls else None
    by_role = _role_usage(reconciled) if reconciled and reconciled["matches_provider"] else {}
    api_amount = Decimal(0)
    api_currency = ""
    api_known = True
    subscription_tokens = False
    rows = []
    for role in snapshot.get("roles") or []:
        used = by_role.get(role["role"])
        incoming = used["input_tokens"] if used else None
        outgoing = used["output_tokens"] if used else None
        cost_text = "未调用"
        usage_label = ""
        kind = _billing_kind(role.get("billing"))
        if used and kind == "subscription":
            subscription_tokens = True
            cost_text = "订阅，不按次计费"
        elif used and kind == "unknown":
            cost_text = "计费方式未知"
            usage_label = "计费方式未知"
        elif used:
            price = _price(role.get("model") or "", prices)
            amount = None
            if price is not None:
                try:
                    amount = _money(incoming, outgoing, price)
                except (InvalidOperation, ArithmeticError):
                    amount = None  # too large to state to the cent: unknown, not a 500
            if amount is None:
                api_known = False
                cost_text = "价格未知"
            else:
                api_amount += amount
                api_currency = price["currency"]
                cost_text = _money_text(amount, price["currency"])
        if not used:
            matched = bool(reconciled and reconciled.get("matches_provider"))
            usage_label = "用量未归集" if calls and not matched else "未调用"
            if cost_text == "未调用":
                cost_text = usage_label
        rows.append({
            "role": role.get("role"),
            "zh": role.get("zh"),
            "tier": role.get("tier"),
            "provider": role.get("provider"),
            "model": role.get("model"),
            "effort_field": role.get("effort_field"),
            "effort": role.get("effort"),
            "effort_label": effort_label(
                role.get("provider") or "", role.get("effort") or "", role.get("effort_field") or ""
            ),
            "billing": role.get("billing"),
            "prompt_count": incoming,
            "completion_count": outgoing,
            "cost_text": cost_text,
            "usage_label": usage_label,
        })
    unassigned = False
    if not reconciled:
        match_text = "没有供应商用量，不估算。"
    elif not reconciled.get("provider_total"):
        match_text = "没有供应商合计，不把调用合计当成已核对。"
        api_known = False
    elif reconciled["matches_provider"]:
        names = {str(role.get("role") or "") for role in snapshot.get("roles") or []}
        unassigned = any(call.get("role") not in names for call in reconciled.get("calls") or [])
        if unassigned:
            match_text = "有用量没有归到这次运行的角色，不把部分合计当成已核对。"
        else:
            match_text = f"与供应商计数一致，容差 {TOLERANCE_TOKENS}。"
    else:
        match_text = "与供应商计数不一致，不把合计当成已核对。"
        api_known = False
    billing = {str(role.get("role") or ""): role.get("billing") for role in snapshot.get("roles") or []}
    def _tokens(call: dict) -> bool:
        return bool(call.get("input_tokens") or call.get("output_tokens"))

    known_roles = set(billing)

    def _call_kind(call: dict) -> str:
        role = str(call.get("role") or "")
        if role not in known_roles:
            return "unassigned"
        return _billing_kind(billing.get(role))

    api_calls_present = any(
        isinstance(call, dict) and _call_kind(call) == "api" and _tokens(call)
        for call in calls
    )
    unknown_calls = any(
        isinstance(call, dict) and _call_kind(call) == "unknown" and _tokens(call)
        for call in calls
    )
    if unknown_calls:
        api_text = "计费方式未知，不把它当成 API 用量。"
    elif not api_calls_present:
        api_text = "没有 API 用量"
    elif not reconciled or not reconciled.get("provider_total"):
        api_text = "不显示 API 合计：没有供应商合计。"
    elif not reconciled.get("matches_provider"):
        api_text = "不显示 API 合计：与供应商计数不一致。"
    elif unassigned:
        api_text = "不显示 API 合计：用量未全部归集。"
    elif not api_known:
        api_text = "价格未知"
    else:
        api_text = _money_text(api_amount, api_currency or "USD")
    show_role_money = bool(
        reconciled and reconciled.get("provider_total") and reconciled.get("matches_provider")
        and not unassigned and api_known
    )
    if not show_role_money:
        for row in rows:
            text = str(row.get("cost_text") or "")
            if any(ch.isdigit() for ch in text):
                row["cost_text"] = "不计入合计"
    return {
        "id": record.get("id"),
        "ticker": record.get("ticker") or "",
        "date": record.get("date") or "",
        "status": record.get("status") or "",
        "snapshot_source": snapshot.get("source") or "",
        "roles": rows,
        "prompt_count": reconciled["input_tokens"] if reconciled else None,
        "completion_count": reconciled["output_tokens"] if reconciled else None,
        "matches_provider": bool(reconciled and reconciled["matches_provider"]),
        "match_text": match_text,
        "api_text": api_text,
        "subscription_text": "订阅，不按次计费" if subscription_tokens else "",
        "health_text": health_sentence(_failure_for(record, root)),
        "tolerance_cost": str(TOLERANCE_COST),
        "usage_unknown": _count(usage.get("usage_unknown")) or 0,
    }


HEALTH_SENTENCE = {
    "rate_limit": "被限流，不是预算超限。",
    "budget_exceeded": "结构化字段为预算超限。",
    "call_timeout": "本机调用时限，不是供应商预算。",
    "unknown": "没有结构化字段，记为未知。",
    "auth": "登录失效或无权限，不是预算超限。",
    "not_budget": "不是预算超限。",
}
LEGACY_NOT_BUDGET = "没有结构化预算字段，不记为预算超限。"


def health_sentence(failure: dict | None) -> str:
    """Chinese line for one run. Free text never becomes a budget overrun."""
    if not isinstance(failure, dict):
        return ""
    health = failure.get("provider_health")
    if isinstance(health, dict):
        kind = str(health.get("kind") or "")
        budget = health.get("budget") is True
        if kind == "budget_exceeded" and budget:
            return HEALTH_SENTENCE["budget_exceeded"]
        if kind in HEALTH_SENTENCE and not budget:
            return HEALTH_SENTENCE[kind]
        return HEALTH_SENTENCE["unknown"]
    if failure.get("budget_exhausted") is True:
        return HEALTH_SENTENCE["call_timeout"]
    kind = failure.get("kind")
    if kind == "rate_limit":
        return HEALTH_SENTENCE["rate_limit"]
    if kind in {"timeout", "connection", "server", "auth", "other", "framework"}:
        return LEGACY_NOT_BUDGET
    return ""


def _failure_for(record: dict, root: Path | None) -> dict | None:
    failure = record.get("failure")
    if isinstance(failure, dict):
        return failure
    result = record.get("result")
    if not isinstance(result, str) or not result.strip():
        return None
    from webui.run_failure import failure_path, read_failure

    path = failure_path(result)
    try:
        resolved = path.resolve()
        base = ((Path(root) if root is not None else webui_home()) / "logs").resolve()
    except OSError:
        return None
    if resolved != base and base not in resolved.parents:
        return None
    return read_failure(resolved)


def list_cost(root: Path | None = None) -> dict:
    prices, prices_unreadable = read_prices(root)
    records = []
    if root is None:
        from webui import service

        records = service.JOBS.list_runs()
    else:
        folder = Path(root) / "runs"
        if folder.is_dir():
            for path in folder.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(data, dict):
                    data.setdefault("id", path.stem)
                    records.append(data)
    runs = []
    for record in records:
        if record.get("kind", "analysis") != "analysis":
            continue
        try:
            runs.append(_view_run(record, prices, root))
        except CostError as exc:
            runs.append({
                "id": record.get("id"),
                "ticker": record.get("ticker") or "",
                "date": record.get("date") or "",
                "status": record.get("status") or "",
                "roles": [],
                "api_text": "费用无法核对。",
                "subscription_text": "",
                "match_text": str(exc),
                "matches_provider": False,
                "health_text": health_sentence(_failure_for(record, root)),
            })
    return {
        "runs": runs,
        "prices_path": str(_prices_path(root)),
        "prices_models": sum(1 for model in prices if _price(model, prices) is not None),
        "prices_unreadable": prices_unreadable,
        "note": "单价只来自本机 cost-prices.json，不是供应商账单。订阅和 API 分开显示。没有单价就写价格未知。",
        "rule": "429 记为限流。只有 type 或 code 为 budget_exceeded 才记为预算超限。没有这些字段记未知。本机调用时限不是供应商预算。",
        "tolerance_count": TOLERANCE_TOKENS,
        "tolerance_cost": str(TOLERANCE_COST),
    }
