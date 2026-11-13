"""Operations the console performs without invoking the CLI.

Secrets stay in the same local files the CLI already uses. Responses never
include tokens, API keys, or .env contents.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, set_key

from cli.prompts import (
    _OPENROUTER_MAINSTREAM,
    ANALYST_CHOICES,
    _fetch_openrouter_models_or_raise,
    detect_asset_type,
    filter_analysts_for_asset_type,
    parse_analysis_date,
    parse_analysts,
    parse_ticker,
)
from cli.run import _build_run_config
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.model_catalog import get_model_options
from tradingagents.llm_clients.openai_client import SIGNED_IN_PROVIDERS, check_endpoint
from tradingagents.llm_clients.subscriptions import SUBSCRIPTIONS, get_subscription
from tradingagents.portfolio import CostedPortfolioContext, PortfolioContext
from webui.catalog import reasoning_family
from webui.frameworks import (
    DEFAULT_FRAMEWORK,
    UnknownFramework,
    framework_fields,
    framework_of,
    resolve_framework,
)
from webui.redaction import redact, scrub_config  # noqa: E402,F401 - redact is re-exported


class ConsoleError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def webui_home() -> Path:
    override = os.environ.get("TRADINGAGENTS_WEBUI_HOME")
    root = Path(override) if override else Path.home() / ".tradingagents" / "webui"
    root.mkdir(parents=True, exist_ok=True)
    return root


CONSOLE_LOCK_NAME = "console.lock"
_CONSOLE_LOCK = None  # kept open for the console's lifetime
_CONSOLE_LOCK_PATH = None  # path opened with O_NOFOLLOW (fdopen.name is the fd)


def acquire_console_lock():
    """Only one console per TRADINGAGENTS_WEBUI_HOME (W40).

    A second console would re-run recovery and re-launch the first one's queued
    runs. The lock is an flock on ``<home>/console.lock``: the OS drops it when
    the holding process exits, so a crashed console never leaves a stale lock.
    Raises :class:`ConsoleError` (409) when another console holds it.

    W107/W108: the path is opened with ``O_NOFOLLOW`` so a symlink is never
    written through, and a directory / permission problem becomes a clear Chinese
    error (exit 1 from ``web``) instead of a traceback.
    """
    global _CONSOLE_LOCK, _CONSOLE_LOCK_PATH
    import errno
    import fcntl
    import stat as stat_mod

    if _CONSOLE_LOCK is not None:
        return _CONSOLE_LOCK
    path = webui_home() / CONSOLE_LOCK_NAME

    def _unusable(exc: OSError) -> ConsoleError:
        if path.is_symlink() or getattr(exc, "errno", None) in (errno.ELOOP, getattr(errno, "EFTYPE", -1)):
            return ConsoleError(
                "无法启动控制台：console.lock 是一个符号链接，没有写入链接目标，也没有启动。"
                "请删掉这个符号链接后再打开控制台。", 1)
        if path.is_dir() or getattr(exc, "errno", None) == errno.EISDIR:
            return ConsoleError(
                "无法启动控制台：console.lock 是一个目录，无法加锁。"
                "请删掉这个目录后再打开控制台。", 1)
        if getattr(exc, "errno", None) in (errno.EACCES, errno.EPERM, errno.EROFS):
            return ConsoleError(
                "无法启动控制台：没有权限写入 console.lock。"
                "请检查控制台目录的权限后再打开。", 1)
        reason = exc.strerror or type(exc).__name__
        return ConsoleError(f"无法启动控制台：无法使用 console.lock（{reason}）。", 1)

    for _attempt in range(10):
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise _unusable(exc) from exc
        handle = os.fdopen(fd, "r+", encoding="utf-8")  # noqa: SIM115 - held open for the console lifetime
        try:
            info = os.fstat(handle.fileno())
            if not stat_mod.S_ISREG(info.st_mode):
                handle.close()
                raise _unusable(OSError(errno.EINVAL, "is not a regular file"))
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ConsoleError:
            raise
        except OSError as exc:
            handle.close()
            # Another live console holds the flock — not an "unusable path" case.
            if getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise ConsoleError(
                    "同一个控制台目录已有另一个控制台在运行，没有启动第二个，也没有动排队中的任务。"
                    "请先停掉那个控制台，或用 TRADINGAGENTS_WEBUI_HOME 指定另一个目录。", 409) from exc
            raise _unusable(exc) from exc
        # W89: a console that exits removes console.lock. If it did so between our
        # open and our flock, we hold a lock on a file nobody else can see: retry.
        # lstat: if the path is now a symlink, do not treat it as our file (W107).
        try:
            same = (
                not path.is_symlink()
                and os.path.samestat(os.fstat(handle.fileno()), os.lstat(path))
            )
        except OSError:
            same = False
        if same:
            break
        handle.close()
    else:
        raise ConsoleError("无法取得控制台锁 console.lock，请重试。", 1)
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    global _CONSOLE_LOCK_PATH
    _CONSOLE_LOCK = handle
    _CONSOLE_LOCK_PATH = path
    return handle


def release_console_lock() -> None:
    global _CONSOLE_LOCK, _CONSOLE_LOCK_PATH
    if _CONSOLE_LOCK is not None:
        with contextlib.suppress(OSError):
            # W71/W89: any exit (clean, failed start, SIGTERM/SIGHUP) removes
            # console.lock while still holding it, so no dead PID is left behind.
            os.unlink(_CONSOLE_LOCK_PATH or _CONSOLE_LOCK.name)
        with contextlib.suppress(OSError):
            _CONSOLE_LOCK.seek(0)
            _CONSOLE_LOCK.truncate()
            _CONSOLE_LOCK.flush()
        with contextlib.suppress(OSError):
            _CONSOLE_LOCK.close()
        _CONSOLE_LOCK = None
        _CONSOLE_LOCK_PATH = None


def _config_path() -> Path:
    return webui_home() / "config.json"


def load_saved_config() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def public_saved_config() -> dict:
    """W84: what GET /api/config shows; a config.json written before W84 may still
    hold a nested credential, which is redacted here, not echoed."""
    return scrub_config(load_saved_config())


def save_config(payload: dict) -> dict:
    # W60/W84: no credential in plain text at any depth (holdings notes, portfolio
    # fields, nested dicts and lists), and none kept from an older config.json.
    clean = scrub_config(payload)
    clean = constrain_runtime(clean)
    check_portfolio(clean, "设置没有保存。")          # W79: no bad holdings in config.json
    merged = scrub_config({**load_saved_config(), **clean})
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
    return merged


def save_config_result(payload: dict) -> dict:
    """POST /api/config. The file stays the config; saved is only the response flag."""
    return {"saved": True, "config": save_config(payload)}


_TIER_KEYS = (
    "quick_think_provider", "deep_think_provider",
    "openai_reasoning_effort", "google_thinking_level", "anthropic_effort",
)


def remembered_form() -> dict:
    """The same stable answers the CLI prefills. Ticker and date stay out."""
    from cli.prefs import load_last_run, sanitize

    prefs = sanitize(load_last_run(), "stock")
    saved = load_saved_config()
    merged = dict(prefs)
    for key in (*_TIER_KEYS, "quick_think_llm", "deep_think_llm"):
        if saved.get(key) not in (None, ""):
            merged[key] = saved[key]
    if prefs.get("llm_provider") and not saved.get("quick_think_provider"):
        merged.setdefault("quick_think_provider", prefs["llm_provider"])
        merged.setdefault("deep_think_provider", prefs["llm_provider"])
    return {
        "values": merged,
        "source": "cli.prefs.load_last_run",
        "note": "这些是上次选择的预填，不会跳过步骤。标的和日期不记住，和命令行一样。",
    }


def remember_run(selections: dict) -> None:
    """Write the CLI's remembered answers, plus this console's two model tiers."""
    from cli.prefs import save_last_run

    save_last_run(selections)
    extra = {key: selections.get(key) for key in _TIER_KEYS if selections.get(key) not in (None, "")}
    for key in ("quick_think_llm", "deep_think_llm", "llm_provider"):
        if selections.get(key) not in (None, ""):
            extra[key] = selections[key]
    if extra:
        save_config(extra)


def _subscription_label(provider: str) -> str:
    from cli.main import _subscription_label as cli_label
    return cli_label(provider)


_LAUNCHER_STATUS_PHASES = {"starting", "running", "stopping", "completed"}


RESULT_WRITTEN_SENTENCE = "分析进程已退出，结果文件已写出。这只说明进程结束，不表示分析完成或评级已确认。"
ANALYSIS_EXIT_WITHOUT_RESULT = "分析进程已退出，但没有写出结果。进程退出不等于分析完成。"
BACKTEST_EXIT_WITHOUT_RESULT = "回测进程已退出，但没有写出结果。进程退出不等于回测完成。"
UNKNOWN_EXIT_WITHOUT_RESULT = "进程已退出，但没有写出结果。不把它写成分析完成。"


def exit_without_result_sentence(record: dict | None) -> str:
    """A dead process with no result file. A backtest is not an analysis."""
    kind = (record or {}).get("kind")
    if kind == "backtest":
        return BACKTEST_EXIT_WITHOUT_RESULT
    if kind == "analysis":
        return ANALYSIS_EXIT_WITHOUT_RESULT
    return UNKNOWN_EXIT_WITHOUT_RESULT


def result_written_sentence(record: dict | None) -> str:
    """A dead process that already wrote its result file. A backtest is not an analysis."""
    kind = (record or {}).get("kind")
    if kind == "backtest":
        return "回测进程已退出，结果文件已写出。这只说明进程结束，不表示回测完成。"
    if kind == "analysis":
        return RESULT_WRITTEN_SENTENCE
    return "进程已退出，结果文件已写出。不把它写成分析完成。"


def _result_file(record: dict) -> Path | None:
    """The run's result file when the worker has already written it."""
    raw = record.get("result")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    try:
        if path.is_file():
            return path
    except OSError:
        return None
    return None


def _mark_result_finished(record: dict) -> None:
    """The worker finished. Never signal the recorded pid: it may already belong to someone else."""
    record["status"] = "finished"
    record["detail"] = result_written_sentence(record)


def refresh_run_process(record: dict | None) -> dict | None:
    """Notice when this console's analysis process has exited.

    A dead process is not an analysis completion. The launcher sentence
    ``TradingAgents: not running`` is not used here; that sentence belongs to
    the launcher supervisor, which this record is not.

    A written result wins over a reused pid. macOS hands the exited worker's
    pid to another program before the next poll; calling that a pid-reuse
    failure leaves the run running forever from the queue's point of view.
    The current pid is never signalled on this path.
    """
    if not record or record.get("status") != "running":
        return record
    pid = record.get("pid")
    if not isinstance(pid, int):
        return record
    state = _worker_state(record)
    if state in ("ours", "unknown"):  # W47: unverifiable is not proof it exited; keep the slot
        return record
    raw_result = record.get("result")
    result = Path(raw_result) if isinstance(raw_result, str) and raw_result else None
    if result is not None and result.is_file():
        _mark_result_finished(record)
        generic = record["detail"]
    elif state == "foreign":
        _mark_pid_reused(record)
    else:
        record["status"] = "failed"
        record["detail"] = exit_without_result_sentence(record)
        generic = record["detail"]
        try:
            failure = _read_run_failure(result)
            if failure is not None:
                attach_failure(record, failure)
        except Exception:  # a damaged failure file never breaks /api/runs (MF3)
            record.pop("failure", None)
            record["detail"] = generic
        _note_saved_checkpoint(record)
    record["finished_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        JOBS.write_run(record)
    except Exception:  # noqa: BLE001 - never lose the run record (R2-MF1)
        if "failure" not in record:
            raise
        record.pop("failure", None)
        record["detail"] = generic
        with contextlib.suppress(Exception):  # the old file is still whole; answer from memory
            JOBS.write_run(record)
    return record


def _note_saved_checkpoint(record: dict) -> None:
    """A run that died without a failure file may still have saved steps (RUN-004).

    The worker writes ``checkpoint.saved`` only when it gets to report a failure.
    A process killed outright (or lost across a console restart) never does, so
    the run's own checkpoint database decides whether 从断点重新启动 is offered.
    """
    checkpoint = record.get("checkpoint")
    if not isinstance(checkpoint, dict) or not checkpoint.get("enabled") or checkpoint.get("saved") is not None:
        return
    scope = checkpoint.get("scope")
    ticker = (record.get("selections") or {}).get("ticker")
    if not scope or not ticker:
        return
    try:
        from tradingagents.graph.checkpointer import db_file
        db = db_file(_selections_cache_dir(record.get("selections") or {}), ticker, scope)
        checkpoint["saved"] = db.is_file() and db.stat().st_size > 0
    except Exception:  # noqa: BLE001 - a status read never fails on this
        checkpoint["saved"] = False


def _selections_cache_dir(selections: dict) -> str:
    try:
        return build_run_config(selections)["data_cache_dir"]
    except Exception:  # noqa: BLE001
        return effective_cache_dir()


def _read_run_failure(result: Path | None) -> dict | None:
    if result is None:
        return None
    from webui.run_failure import failure_path, read_failure
    return read_failure(failure_path(result))


def attach_failure(record: dict, failure: dict) -> dict:
    """Put the worker's failure summary on the run record (already redacted)."""
    from webui.run_failure import checkpoint_sentence, clean_failure, summary

    failure = clean_failure(failure)
    if failure is None:
        return record
    text = failure.get("summary") or summary(failure)
    failure["summary"] = text
    record["failure"] = failure
    record["detail"] = text
    if failure.get("checkpoint") is not None:
        record["checkpoint"] = failure["checkpoint"]
    hint = checkpoint_sentence(failure.get("checkpoint"))
    if hint:
        record["detail"] = f"{text}。{hint}"
    return record


def console_status_sentence(run: dict | None) -> dict:
    """Map a console job to a launcher status sentence only when the words match.

    The launcher prints ``TradingAgents: <phase>`` for starting, running,
    stopping and completed. Console words such as finished, failed, stopped
    and idle are not those phases, so they must not be wrapped in that sentence.
    """
    from cli.launcher import NOT_RUNNING, status_sentence

    if not run:
        return {
            "zh": "没有控制台任务。",
            "cli": NOT_RUNNING,
            "note": "这里不代为查询启动脚本的进程。未运行不是环境错误。",
        }
    status = str(run.get("status") or "")
    pid = run.get("pid")
    if status in _LAUNCHER_STATUS_PHASES:
        cli = status_sentence(status, pid if isinstance(pid, int) else None)
        suffix = cli.removeprefix(f"TradingAgents: {status}")
        return {
            "zh": f"控制台任务 {status}{suffix}。",
            "cli": cli,
            "note": "这是控制台进程，不是启动脚本的 supervisor。句式与 status 相同，不表示已经查询过启动脚本。",
        }
    return {
        "zh": f"控制台任务状态是 {status}。启动脚本不会打印这个词。",
        "cli": None,
        "note": f"启动脚本在 supervisor 已退出时说 {NOT_RUNNING}。这里不把 finished、failed、stopped 或 idle 写成那句。",
    }


def status_argv_line_for(run: dict | None) -> str | None:
    """The launcher argv line, only for a stored console process command.

    ``status`` prints it when the supervisor response includes argv. A missing
    list is not filled in from the ticker or selections.
    """
    if not run or str(run.get("status") or "") not in _LAUNCHER_STATUS_PHASES:
        return None
    argv = run.get("argv")
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return None
    from cli.launcher import status_argv_line
    return status_argv_line(argv)


def auth_status_sentence(provider: str, account: str | None, error: str | None = None) -> str:
    """The same local-status line the CLI prints. It does not validate the sign-in."""
    from cli.main import (
        auth_status_absent_sentence,
        auth_status_error_sentence,
        auth_status_signed_in_sentence,
    )

    label = _subscription_label(provider)
    if error:
        return auth_status_error_sentence(label, error)
    if account:
        return auth_status_signed_in_sentence(label, account)
    return auth_status_absent_sentence(label)


def logout_sentence(provider: str, outcome: str | None) -> tuple[str, str]:
    """Chinese label and the CLI sentence for a sign-out outcome."""
    from cli.main import (
        CHATGPT_SIGNOUT_UNCONFIRMED,
        local_signout_unconfirmed,
        not_signed_in_sentence,
        signed_out_sentence,
    )

    label = _subscription_label(provider)
    if outcome == "revoked":
        return f"已退出 {label}，并完成服务端撤销。", signed_out_sentence(label)
    if outcome == "forgotten":
        if provider == "chatgpt":
            return (
                "已在本机退出，但没能联系 OpenAI 撤销授权。授权在过期前仍然有效。",
                CHATGPT_SIGNOUT_UNCONFIRMED,
            )
        return (
            f"已在本机退出 {label}。服务端撤销没有确认。",
            local_signout_unconfirmed(label),
        )
    return f"本来就没有登录 {label}。", not_signed_in_sentence(label)


def _auth_file_problem(spec) -> str | None:
    """The auth module's local read-failure reason, if it can tell one."""
    probe = getattr(spec.auth, "auth_file_problem", None)
    if probe is None:
        return None
    try:
        return probe()
    except Exception as exc:  # a status probe must not break the page
        return type(exc).__name__


def auth_status(plan=None) -> dict:
    """Local sign-in names only. Does not refresh tokens or prove quota.

    ``None`` matches ``auth-status`` with the plan omitted and reports every
    subscription. A present value is case-sensitive and must be one the CLI
    accepts. An illegal value raises that command's stderr for the exact text.
    """
    from webui.catalog import plan_cli_error

    if plan is not None and plan not in SIGNED_IN_PROVIDERS:
        raise ConsoleError(plan_cli_error("auth-status", plan))
    selected = (plan,) if plan is not None else tuple(SUBSCRIPTIONS)
    accounts = []
    for key in selected:
        spec = SUBSCRIPTIONS[key]
        # The file's shape is checked first, so a malformed file is never shown as
        # signed in (or signed out) whatever signed_in_as would make of it.
        # CLI-001: the same reader auth-status uses, so the cli line always matches.
        from cli.main import local_sign_in

        email, problem = local_sign_in(spec)
        if problem:
            # The file exists but is corrupt/unreadable: that is not "signed out".
            accounts.append({
                "provider": key,
                "zh": spec.display_name,
                "state": "read_error",
                "signed_in": False,
                "read_error": True,
                "account": None,
                "detail": f"本机登录文件读取失败：{problem}。只读取了本机文件，没有联网校验。",
                "cli": auth_status_sentence(key, None, problem),
            })
            continue
        accounts.append({
            "provider": key,
            "zh": spec.display_name,
            "state": "signed_in" if email else "signed_out",
            "signed_in": bool(email),
            "account": email or None,
            "detail": "只读取本机登录材料，未做在线校验。",
            "cli": auth_status_sentence(key, email or None),
            "plan_use": plan_use_sentence(key, email) if email else None,
        })
    return {
        "accounts": accounts,
        "live_validation": False,
        "plan": plan,
        "cli": "auth-status" if plan is None else f"auth-status {plan}",
    }


def asset_options(ticker: str) -> dict:
    """The asset type and analysts the CLI would offer for this ticker."""
    symbol = parse_ticker(ticker)
    asset = detect_asset_type(symbol)
    analysts = filter_analysts_for_asset_type([item for _, item in ANALYST_CHOICES], asset)
    from tradingagents.memory.settlement import resolve_benchmark

    config = dict(DEFAULT_CONFIG)
    explicit = load_saved_config().get("benchmark_ticker")
    if explicit:
        config["benchmark_ticker"] = explicit
    return {
        "ticker": symbol,
        "asset_type": asset.value,
        "analysts": [item.value for item in analysts],
        "fundamentals_hidden": asset.value in ("crypto", "commodity"),
        "benchmark": resolve_benchmark(symbol, config),
        "benchmark_source": "TRADINGAGENTS_BENCHMARK_TICKER" if explicit else "benchmark_map",
    }


def model_prompt_title(provider: str, mode: str) -> str:
    """The questionary title the CLI uses for this provider and tier."""
    from cli.prompts import model_picker_title

    return model_picker_title(provider, mode)


def model_prompt_map() -> dict[str, dict[str, str]]:
    """CLI model-picker titles for every provider, without listing models."""
    from cli.prompts import _llm_provider_table

    return {
        key: {
            "quick": model_prompt_title(key, "quick"),
            "deep": model_prompt_title(key, "deep"),
        }
        for _display, key, _url in _llm_provider_table()
    }


def subscription_model_list_sentence(provider: str, exc: BaseException) -> str:
    """The yellow line the CLI prints when a plan cannot list models.

    The CLI then still offers Custom model ID. This is the visible text, not
    the Rich-escaped form.
    """
    from cli.prompts import _subscription_label, subscription_models_error
    return subscription_models_error(_subscription_label(provider), exc)


def list_models(provider: str, mode: str = "quick") -> dict:
    from cli.prompts import (
        AZURE_DEPLOYMENT_REQUIRED,
        CUSTOM_MODEL_CHOICE,
        MODEL_ID_REQUIRED,
        OPENROUTER_ID_PROMPT,
    )

    key = provider.lower()
    selected_mode = mode if mode in {"quick", "deep"} else "quick"
    title = model_prompt_title(key, selected_mode)
    if key in SIGNED_IN_PROVIDERS:
        spec = get_subscription(key)
        try:
            models = [{"zh": name, "id": model_id} for name, model_id in spec.auth.list_models()]
        except Exception as exc:
            return {
                "provider": key,
                "source": "subscription",
                "mode": selected_mode,
                "models": [],
                "custom_allowed": True,
                "title": title,
                "cli": subscription_model_list_sentence(key, exc),
                "detail": f"模型列表没有读到。命令行会继续提供 {CUSTOM_MODEL_CHOICE}，这里也可以手填。",
            }
        return {
            "provider": key,
            "source": "subscription",
            "mode": selected_mode,
            "models": models,
            "custom_allowed": True,
            "title": title,
        }
    if key == "azure":
        return {
            "provider": key,
            "source": "azure",
            "mode": selected_mode,
            "models": [],
            "custom_allowed": True,
            "title": title,
            "hint": AZURE_DEPLOYMENT_REQUIRED,
            "detail": "Azure 不提供模型列表。请填写部署名。",
        }
    if key == "openrouter":
        warning = None
        try:
            fetched = _fetch_openrouter_models_or_raise()
        except Exception as exc:
            fetched = []
            from cli.prompts import openrouter_fetch_error
            warning = openrouter_fetch_error(exc)
        mainstream = [
            (name, model_id) for name, model_id in fetched
            if not model_id.startswith("~") and model_id.split("/", 1)[0] in _OPENROUTER_MAINSTREAM
        ]
        top = (mainstream or fetched)[:5]
        payload = {
            "provider": key,
            "source": "openrouter",
            "mode": selected_mode,
            "models": [{"zh": name, "id": model_id} for name, model_id in top],
            "custom_allowed": True,
            "title": title,
            "detail": None if top else "没有拉到 OpenRouter 模型，可手填模型 ID。",
        }
        if warning:
            payload["cli"] = warning
        payload["custom_prompt"] = OPENROUTER_ID_PROMPT
        payload["hint"] = MODEL_ID_REQUIRED
        return payload
    try:
        options = get_model_options(key, selected_mode)
    except KeyError:
        # Azure and any provider without a curated list still accept a model or
        # deployment ID, the same as the CLI custom-ID prompt.
        return {
            "provider": key,
            "source": "custom",
            "mode": selected_mode,
            "models": [],
            "custom_allowed": True,
            "title": title,
            "detail": f"这个供应商没有内置模型列表。请手填模型或部署 ID，对应 CLI 的 {CUSTOM_MODEL_CHOICE}。",
        }
    return {
        "provider": key,
        "source": "catalog",
        "mode": selected_mode,
        "title": title,
        "models": [{"zh": name, "id": model_id} for name, model_id in options if model_id != "custom"],
        "custom_allowed": True,
    }


def ollama_hint(url: str | None = None) -> dict:
    """The same endpoint lines the CLI prints after Ollama is selected."""
    from cli.prompts import ollama_endpoint_lines

    resolved = (url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1").strip()
    from_env = bool(os.environ.get("OLLAMA_BASE_URL") and os.environ.get("OLLAMA_BASE_URL") == resolved)
    lines = ollama_endpoint_lines(resolved, os.environ.get("OLLAMA_BASE_URL") if from_env else None)
    return {
        "url": resolved,
        "from_env": from_env,
        "cli": lines,
        "notes": lines[1:],
    }


def key_status() -> list[dict]:
    rows = []
    seen = set()
    for provider, env_var in _known_key_env().items():
        if env_var in seen:
            continue
        seen.add(env_var)
        rows.append({
            "provider": provider,
            "env_var": env_var,
            "configured": bool(os.environ.get(env_var)),
        })
    return rows


def _known_key_env() -> dict[str, str]:
    from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
    return {
        provider: env_var
        for provider, env_var in PROVIDER_API_KEY_ENV.items()
        if env_var
    }


def _env_rows(items) -> list[dict]:
    return [
        {
            "env": item["env"],
            "zh": item["zh"],
            "masked": item["masked"],
            "configured": bool(os.environ.get(item["env"])),
            "note": item["note"],
        }
        for item in items
    ]


def data_env_status() -> list[dict]:
    """Whether the data-source variables the CLI reads are set. Values stay out."""
    from webui.catalog import DATA_ENV

    return _env_rows(DATA_ENV)


def cloud_env_status() -> list[dict]:
    """Whether Azure and Bedrock variables the CLI reads are set. Values stay out."""
    from webui.catalog import CLOUD_ENV

    return _env_rows(CLOUD_ENV)


def save_data_env(name: str, value: str) -> dict:
    """Write one data or cloud variable the CLI already reads. Do not echo it."""
    from webui.catalog import CLOUD_ENV, DATA_ENV

    allowed = {item["env"]: item for item in (*DATA_ENV, *CLOUD_ENV)}
    env_var = str(name or "").strip()
    if env_var not in allowed:
        raise ConsoleError("只能保存账户页列出的数据或云端环境变量。")
    text = (value or "").strip()
    if not text:
        raise ConsoleError(f"{env_var} 不能为空。留空不会清除已有值。")
    if env_var == "AZURE_OPENAI_ENDPOINT" and not text.startswith(("http://", "https://")):
        raise ConsoleError("AZURE_OPENAI_ENDPOINT 必须以 http:// 或 https:// 开头。")
    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")
    path = Path(env_path)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)
    set_key(str(path), env_var, text)
    os.environ[env_var] = text
    return {"saved": True, "env_var": env_var, "configured": True}


def saved_env_sentence(env_var: str, env_path: str) -> str:
    """The line ensure_api_key prints after writing a key. The value stays out."""
    from cli.prompts import saved_key_sentence
    return saved_key_sentence(env_var, env_path)


def skipped_key_sentence(env_var: str) -> str:
    from cli.prompts import skipped_key_sentence as cli_sentence
    return cli_sentence(env_var)


def missing_key_sentence(env_var: str) -> str:
    from cli.prompts import missing_key_sentence as cli_sentence
    return cli_sentence(env_var)


def unset_key_sentence(env_var: str) -> str:
    from cli.prompts import unset_key_sentence as cli_sentence
    return cli_sentence(env_var)


def paste_key_prompt(env_var: str) -> str:
    from cli.prompts import paste_key_prompt as cli_prompt
    return cli_prompt(env_var)


def plan_use_sentence(provider: str, account: str) -> str:
    from cli.prompts import _subscription_auth_label, plan_use_sentence as cli_sentence
    return cli_sentence(_subscription_auth_label(provider), account)


def save_api_key(provider: str, key: str) -> dict:
    env_var = get_api_key_env(provider)
    if not env_var:
        raise ConsoleError("这个供应商不使用 API Key。订阅请用登录，不要把令牌填到这里。")
    value = (key or "").strip()
    if not value:
        return {
            "saved": False,
            "provider": provider.lower(),
            "env_var": env_var,
            "cli": skipped_key_sentence(env_var),
            "zh": "已跳过。没有写入密钥。",
        }
    env_path = find_dotenv(usecwd=True) or str(Path.cwd() / ".env")
    path = Path(env_path)
    if not path.exists():
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    os.chmod(path, 0o600)
    set_key(str(path), env_var, value)
    os.environ[env_var] = value
    return {
        "saved": True,
        "provider": provider.lower(),
        "env_var": env_var,
        "cli": saved_env_sentence(env_var, env_path),
        "prompt": paste_key_prompt(env_var),
    }


def logout(provider) -> dict:
    from webui.catalog import plan_cli_error

    if provider is None:
        raise ConsoleError(plan_cli_error("logout", None))
    if provider not in SIGNED_IN_PROVIDERS:
        raise ConsoleError(plan_cli_error("logout", provider))
    key = provider
    spec = get_subscription(key)
    outcome = spec.auth.sign_out()
    zh, cli = logout_sentence(key, outcome)
    return {"provider": key, "outcome": outcome, "zh": zh, "cli": cli}



_HOLDING_FIELD_ZH = {
    "ticker": "代码（ticker）需要是不为空的文字",
    "quantity": "数量（quantity）需要是不为 0 的数字（负数表示做空）",
    "average_price": "成本价（average_price）需要是大于 0 的数字",
}


def _portfolio_problem_zh(error: dict) -> str:
    loc = tuple(error.get("loc") or ())
    missing = error.get("type") == "missing"
    if not loc:
        return "持仓需要是一个 JSON 对象（可含 cash、currency 和 positions）"
    if loc[0] == "cash":
        return "现金（cash）需要是不小于 0 的数字"
    if loc[0] == "currency":
        return "币种（currency）需要是文字"
    if loc[0] != "positions":
        return f"无法识别的字段：{str(loc[0])[:40]}"
    if len(loc) == 1:
        return "持仓列表（positions）需要是数组"
    where = f"第 {int(loc[1]) + 1} 个持仓" if isinstance(loc[1], int) else "持仓"
    if len(loc) == 2:
        if error.get("type") in ("model_type", "dict_type"):
            return f"{where}需要是一个 JSON 对象"
        return f"{where}需要填写数量或成本价"
    field = str(loc[2])
    if missing:
        return f"{where}缺少{'成本价（average_price）' if field == 'average_price' else _HOLDING_FIELD_ZH.get(field, field)[:2]}"
    return f"{where}的{_HOLDING_FIELD_ZH.get(field, f'字段 {field[:40]} 不正确')}"


def portfolio_unusable(exc: Exception, tail: str = "没有启动任何分析。") -> str:
    """W49: what is wrong with the holdings, in Chinese, without pydantic's own text."""
    from pydantic import ValidationError

    problems = []
    if isinstance(exc, ValidationError):
        for error in exc.errors():
            line = _portfolio_problem_zh(error)
            if line not in problems:
                problems.append(line)
    if not problems:
        problems = ["格式不正确，请对照示例检查"]
    shown = "；".join(problems[:5]) + ("；……" if len(problems) > 5 else "")
    return f"持仓内容不可用：{shown}。{tail}"


def check_portfolio(payload: dict, tail: str = "没有启动任何分析。") -> None:
    """The holdings rules every endpoint shares: analysis, backtest estimate and
    start (W78), and the saved drawer settings (W79). A 400 in Chinese.

    W50: the WebUI says where the holdings came from. The step-1 form needs the
    cost (as the CLI does); an imported holdings JSON, or a request that does not
    say (clients older than WEBUI-006 only had the JSON import), is read as
    before, so a legacy position without average_price still runs.
    """
    portfolio = payload.get("portfolio") if isinstance(payload, dict) else None
    if not portfolio:
        return
    model = CostedPortfolioContext if payload.get("portfolio_source") == "form" else PortfolioContext
    try:
        model.model_validate(portfolio)
    except Exception as exc:
        raise ConsoleError(portfolio_unusable(exc, tail)) from exc


def validate_analysis(payload: dict) -> dict:
    framework = launch_framework(payload)
    try:
        checked = _validate_analysis(payload)
    except ValueError as exc:
        message = str(exc)
        # The interactive date prompt retries with this prefix. The flag parser
        # text is still inside the message.
        from cli.prompts import date_retry_error
        if message.startswith("not a date:") or message.endswith("is in the future"):
            message = date_retry_error(message)
        raise ConsoleError(message) from exc
    checked["framework"] = framework
    return checked


def launch_framework(payload: dict) -> str:
    """The registered framework a launch asked for (WEBUI-008); unknown is a 400, nothing starts."""
    try:
        return resolve_framework(payload.get("framework") if isinstance(payload, dict) else None)
    except UnknownFramework as exc:
        raise ConsoleError(str(exc)) from exc


def _validate_analysis(payload: dict) -> dict:
    """Check a run the same way the CLI parsers do. Does not call a model."""
    from cli.prompts import interactive_ticker_or_default
    from cli.selections import analysis_date_question_default

    raw_ticker = str(payload.get("ticker") or "")
    ticker_from_default = not raw_ticker.strip()
    ticker = interactive_ticker_or_default(raw_ticker) if ticker_from_default else parse_ticker(raw_ticker)
    raw_date = str(payload.get("date") or "")
    date_from_default = not raw_date.strip()
    date = analysis_date_question_default() if date_from_default else parse_analysis_date(raw_date)
    asset = detect_asset_type(ticker)
    if "analysts" not in payload or payload.get("analysts") is None:
        raw_analysts = ["market"]
    else:
        raw_analysts = payload.get("analysts")
        if isinstance(raw_analysts, str):
            raw_analysts = [part.strip() for part in raw_analysts.split(",") if part.strip()]
        elif isinstance(raw_analysts, list):
            raw_analysts = [str(part).strip() for part in raw_analysts if str(part).strip()]
        else:
            raise ConsoleError("分析师必须按名称选择。")
    if not raw_analysts:
        from cli.prompts import ANALYST_REQUIRED
        raise ConsoleError("请至少选择一名分析师。对应命令行：" + ANALYST_REQUIRED)
    analysts = parse_analysts(",".join(raw_analysts), asset)
    analysts = filter_analysts_for_asset_type(analysts, asset)
    if not analysts:
        raise ConsoleError("当前资产类型下没有可用分析师。")
    depth = int(payload.get("research_depth") or 1)
    from cli.prompts import DEPTH_OPTIONS
    allowed = {value for _, value in DEPTH_OPTIONS}
    if depth not in allowed:
        values = [str(value) for _, value in DEPTH_OPTIONS]
        names = [display.split(" - ", 1)[0] for display, _ in DEPTH_OPTIONS]
        value_text = "、".join(values[:-1]) + " 或 " + values[-1]
        raise ConsoleError(f"研究深度只能是 {value_text}，对应 CLI 的 {' / '.join(names)}。")
    provider = str(payload.get("llm_provider") or "").lower()
    if not provider:
        raise ConsoleError("请选择模型供应商。订阅请选 chatgpt 或 grok，不要选 openai。")
    quick = str(payload.get("quick_think_llm") or "").strip()
    deep = str(payload.get("deep_think_llm") or "").strip()
    quick_provider = str(payload.get("quick_think_provider") or provider).lower()
    deep_provider = str(payload.get("deep_think_provider") or provider).lower()
    if not quick or not deep:
        from cli.prompts import AZURE_DEPLOYMENT_REQUIRED, MODEL_ID_REQUIRED
        from cli.selections import tier_model_sentence
        pieces = []
        for tier, tier_key, missing in (
            ("quick", quick_provider, not quick),
            ("deep", deep_provider, not deep),
        ):
            if not missing:
                continue
            if tier_key != provider:
                sentence = tier_model_sentence(
                    tier, tier_key, f"TRADINGAGENTS_{tier.upper()}_THINK_LLM",
                )
            elif tier_key == "azure":
                sentence = AZURE_DEPLOYMENT_REQUIRED
            else:
                sentence = MODEL_ID_REQUIRED
            if sentence not in pieces:
                pieces.append(sentence)
        extra = f" 对应命令行：{' '.join(pieces)}" if pieces else ""
        raise ConsoleError("快模型和深模型都必须填写。" + extra)
    if provider == "openai_compatible":
        url = str(payload.get("backend_url") or "").strip()
        if not url:
            from cli.prompts import NO_ENDPOINT_EXIT
            raise ConsoleError(
                "OpenAI-compatible 必须填写推理地址。对应命令行：" + NO_ENDPOINT_EXIT
            )
        if not url.startswith(("http://", "https://")):
            from cli.prompts import URL_SCHEME_REQUIRED
            raise ConsoleError(
                "OpenAI-compatible 地址必须以 http:// 或 https:// 开头。"
                f"对应命令行：{URL_SCHEME_REQUIRED}"
            )
    language = str(payload.get("output_language") or "Chinese").strip() or "Chinese"
    if os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE"):
        # The CLI skips the language question when this variable is set.
        language = str(DEFAULT_CONFIG.get("output_language") or language)
    if language.lower() == "custom":
        from cli.prompts import CUSTOM_LANGUAGE_INVALID
        raise ConsoleError("自定义语言还没有名称。对应命令行：" + CUSTOM_LANGUAGE_INVALID)
    portfolio = payload.get("portfolio")
    check_portfolio(payload)
    plan_lines = []
    seen_plans = set()
    for tier, tier_provider in (("quick", quick_provider), ("deep", deep_provider)):
        url = payload.get(f"{tier}_think_backend_url") or (payload.get("backend_url") if tier_provider == provider else None)
        try:
            check_endpoint(tier_provider, url)
        except ValueError as exc:
            raise ConsoleError(str(exc)) from exc
        plan_line = _require_credential(tier_provider)
        if plan_line and tier_provider not in seen_plans:
            seen_plans.add(tier_provider)
            plan_lines.append(plan_line)
    selections = {
        "ticker": ticker,
        "analysis_date": date,
        "ticker_from_default": ticker_from_default,
        "date_from_default": date_from_default,
        "asset_type": asset.value,
        "analysts": [item.value for item in analysts],
        "research_depth": depth,
        "max_risk_discuss_rounds": _optional_rounds(payload.get("max_risk_discuss_rounds")),
        "llm_provider": provider,
        "quick_think_provider": quick_provider,
        "deep_think_provider": deep_provider,
        "quick_think_llm": quick,
        "deep_think_llm": deep,
        "backend_url": payload.get("backend_url") or None,
        "output_language": language,
        "quick_think_backend_url": payload.get("quick_think_backend_url") or None,
        "deep_think_backend_url": payload.get("deep_think_backend_url") or None,
        "openai_reasoning_effort": _effort_for("openai", payload, quick_provider, deep_provider),
        "google_thinking_level": _effort_for("google", payload, quick_provider, deep_provider),
        "anthropic_effort": _effort_for("anthropic", payload, quick_provider, deep_provider),
        "save": bool(payload.get("save", True)),
        "html": bool(payload.get("html", True)),
        "show": bool(payload.get("show", False)),
        "checkpoint": payload.get("checkpoint"),
        "clear_checkpoints": bool(payload.get("clear_checkpoints")),
        "portfolio": portfolio,
        "temperature": payload.get("temperature") or None,
        "max_tokens": payload.get("max_tokens") or None,
        "llm_max_retries": payload.get("llm_max_retries") or None,
        "max_tool_rounds": payload.get("max_tool_rounds") or None,
        "benchmark_ticker": payload.get("benchmark_ticker") or None,
        "data_vendors": normalize_data_vendors(payload),
        "tool_vendors": normalize_tool_vendors(payload),
        "results_dir": resolve_results_dir(str(payload["results_dir"])) if payload.get("results_dir") else None,
        "news_article_limit": optional_limit(payload.get("news_article_limit"), limit_labels()["news_article_limit"]),
        "global_news_article_limit": optional_limit(payload.get("global_news_article_limit"), limit_labels()["global_news_article_limit"]),
        "global_news_lookback_days": optional_limit(payload.get("global_news_lookback_days"), limit_labels()["global_news_lookback_days"]),
        "holding_period_days": optional_limit(payload.get("holding_period_days"), limit_labels()["holding_period_days"]),
        "memory_log_max_entries": optional_limit(payload.get("memory_log_max_entries"), limit_labels()["memory_log_max_entries"]),
        "global_news_queries": normalize_news_queries(payload),
        "timeout": optional_timeout(payload.get("timeout")),
        "llm_call_budget": optional_call_budget(payload.get("llm_call_budget")),
        "save_path": resolve_save_path(payload.get("save_path"), ticker, resolve_results_dir(str(payload["results_dir"])) if payload.get("results_dir") else None),
        "memory_log_path": resolve_memory_path(str(payload["memory_log_path"])) if payload.get("memory_log_path") else None,
        "data_cache_dir": resolve_cache_dir(str(payload["data_cache_dir"])) if payload.get("data_cache_dir") else None,
        # V2-INFO-CRAWL (PM): items join the analysis only when 「带入分析」 is ticked.
        "info_crawl": _info_crawl_pick(payload),
        "local_import": _local_import_from_payload(payload),
    }
    apply_env_skips(selections)
    _validate_info_crawl_attachment(selections)
    _require_local_import_news(selections)
    provider = selections["llm_provider"]
    quick_provider = selections.get("quick_think_provider") or provider
    deep_provider = selections.get("deep_think_provider") or provider
    for tier_provider in dict.fromkeys((quick_provider or provider, deep_provider or provider)):
        plan_line = _require_credential(tier_provider)
        if plan_line and tier_provider not in seen_plans:
            seen_plans.add(tier_provider)
            plan_lines.append(plan_line)
    ignored = []
    used = called_providers(provider, quick_provider, deep_provider)
    if payload.get("openai_reasoning_effort") and not any(reasoning_family(item) == "openai" for item in used):
        ignored.append("当前供应商没有 OpenAI 思考强度。Grok 不能单独设置这一项。")
    ignored.extend(chatgpt_plan_refusals(selections))
    from tradingagents import __version__
    from tradingagents.reporting import analysts_line, model_line, vendors_line
    settings = {**build_run_config(selections), "version": __version__, "analysts": selections.get("analysts")}
    header_lines = [
        f"- Analysis date: {selections['analysis_date']}",
        f"- {model_line(settings)}",
        f"- {analysts_line(settings)}",
    ]
    vendors = vendors_line(settings)
    if vendors:
        header_lines.append(f"- {vendors}")
    return {
        "ok": True,
        "selections": selections,
        "ignored": ignored,
        "notices": [*_info_crawl_notices(selections), *_local_import_notices(selections)],
        "model_header": header_lines[1],
        "header_lines": header_lines,
        "confirmations": [
            *flag_confirmations(selections), *plan_lines, *depth_lines_for(selections),
            *matching_environment_lines(selections), *_info_crawl_notices(selections),
            *_local_import_notices(selections),
        ],
    }







def called_providers(main: str | None, quick: str | None, deep: str | None) -> list[str]:
    """Providers the graph will call. A tier falls back to the main provider only when unset."""
    used = []
    for item in (quick or main, deep or main):
        key = str(item or "").lower()
        if key and key not in used:
            used.append(key)
    return used


def chatgpt_plan_refusals(selections: dict) -> list[str]:
    """The warning ChatGPTPlanChatOpenAI logs when sampling settings are dropped.

    Grok and API-key providers are not covered by that warning. The values stay
    in the run config; the ChatGPT client drops them and does not send them.
    """
    providers = called_providers(
        selections.get("llm_provider"),
        selections.get("quick_think_provider"),
        selections.get("deep_think_provider"),
    )
    if "chatgpt" not in providers:
        return []
    lines = []
    for name in ("temperature", "max_tokens"):
        if selections.get(name) not in (None, ""):
            from tradingagents.llm_clients.openai_client import chatgpt_plan_refusal
            lines.append(chatgpt_plan_refusal(name))
    return lines


def env_skipped_values() -> dict:
    """Values the CLI takes from the environment because it skips that question."""
    from cli.prompts import resolve_backend_url
    from cli.selections import depth_from_env

    skipped = {}
    if os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE"):
        skipped["output_language"] = DEFAULT_CONFIG.get("output_language")
    if depth_from_env():
        skipped["research_depth"] = DEFAULT_CONFIG.get("max_debate_rounds")
    if os.environ.get("TRADINGAGENTS_MAX_RISK_ROUNDS"):
        skipped["max_risk_discuss_rounds"] = DEFAULT_CONFIG.get("max_risk_discuss_rounds")
    provider_from_env = bool(os.environ.get("TRADINGAGENTS_LLM_PROVIDER"))
    if provider_from_env:
        provider = str(DEFAULT_CONFIG.get("llm_provider") or "").lower()
        skipped["llm_provider"] = provider
        skipped["backend_url"] = resolve_backend_url(provider, env_url=DEFAULT_CONFIG.get("backend_url"))
        for key in ("openai_reasoning_effort", "google_thinking_level", "anthropic_effort"):
            skipped[key] = DEFAULT_CONFIG.get(key)
    elif os.environ.get("TRADINGAGENTS_LLM_BACKEND_URL"):
        skipped["backend_url"] = DEFAULT_CONFIG.get("backend_url")
    for env_var, key in (
        ("TRADINGAGENTS_QUICK_THINK_BACKEND_URL", "quick_think_backend_url"),
        ("TRADINGAGENTS_DEEP_THINK_BACKEND_URL", "deep_think_backend_url"),
    ):
        if os.environ.get(env_var):
            skipped[key] = DEFAULT_CONFIG.get(key)
    if os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM") or os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM"):
        skipped["quick_think_llm"] = DEFAULT_CONFIG.get("quick_think_llm")
        skipped["deep_think_llm"] = DEFAULT_CONFIG.get("deep_think_llm")
    if not provider_from_env:
        for env_var, key in (
            ("TRADINGAGENTS_OPENAI_REASONING_EFFORT", "openai_reasoning_effort"),
            ("TRADINGAGENTS_GOOGLE_THINKING_LEVEL", "google_thinking_level"),
            ("TRADINGAGENTS_ANTHROPIC_EFFORT", "anthropic_effort"),
        ):
            if os.environ.get(env_var):
                skipped[key] = DEFAULT_CONFIG.get(key)
    for env_var, key in (
        ("TRADINGAGENTS_QUICK_THINK_PROVIDER", "quick_think_provider"),
        ("TRADINGAGENTS_DEEP_THINK_PROVIDER", "deep_think_provider"),
        ("TRADINGAGENTS_TEMPERATURE", "temperature"),
        ("TRADINGAGENTS_MAX_TOKENS", "max_tokens"),
        ("TRADINGAGENTS_LLM_MAX_RETRIES", "llm_max_retries"),
        ("TRADINGAGENTS_LLM_CALL_BUDGET", "llm_call_budget"),
        ("TRADINGAGENTS_MAX_TOOL_ROUNDS", "max_tool_rounds"),
        ("TRADINGAGENTS_BENCHMARK_TICKER", "benchmark_ticker"),
        ("TRADINGAGENTS_RESULTS_DIR", "results_dir"),
        ("TRADINGAGENTS_CACHE_DIR", "data_cache_dir"),
        ("TRADINGAGENTS_MEMORY_LOG_PATH", "memory_log_path"),
    ):
        if os.environ.get(env_var):
            skipped[key] = DEFAULT_CONFIG.get(key)
    return skipped


def apply_env_skips(selections: dict) -> dict:
    """Replace page values the CLI would not have asked for."""
    selections.update(env_skipped_values())
    return selections


def env_output_language() -> str | None:
    """The language the CLI would use when TRADINGAGENTS_OUTPUT_LANGUAGE is set."""
    if not os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE"):
        return None
    value = DEFAULT_CONFIG.get("output_language")
    return str(value) if value else None


def environment_confirmation_lines(config: dict | None = None) -> list[str]:
    """Visible lines the CLI prints when an environment variable skips a prompt.

    The values are the env-applied config, not a page override.
    """
    source = config if config is not None else DEFAULT_CONFIG
    lines = []
    if os.environ.get("TRADINGAGENTS_OUTPUT_LANGUAGE"):
        from cli.selections import output_language_env_sentence
        lines.append(output_language_env_sentence(source.get("output_language")))
    if os.environ.get("TRADINGAGENTS_LLM_PROVIDER"):
        provider = str(source.get("llm_provider") or "").lower()
        from cli.selections import backend_url_sentence, llm_provider_env_sentence
        lines.append(llm_provider_env_sentence(provider))
        from cli.prompts import resolve_backend_url
        url = resolve_backend_url(provider, env_url=source.get("backend_url"))
        lines.append(backend_url_sentence(url))
    if os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM") or os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM"):
        from cli.selections import thinking_agents_env_sentence
        lines.append(thinking_agents_env_sentence(source.get("quick_think_llm"), source.get("deep_think_llm")))
    from cli.selections import (
        ANTHROPIC_EFFORT_ENV_LABEL,
        GEMINI_THINKING_ENV_LABEL,
        OPENAI_REASONING_ENV_LABEL,
        thinking_env_sentence,
    )
    for env_var, key, label in (
        ("TRADINGAGENTS_GOOGLE_THINKING_LEVEL", "google_thinking_level", GEMINI_THINKING_ENV_LABEL),
        ("TRADINGAGENTS_OPENAI_REASONING_EFFORT", "openai_reasoning_effort", OPENAI_REASONING_ENV_LABEL),
        ("TRADINGAGENTS_ANTHROPIC_EFFORT", "anthropic_effort", ANTHROPIC_EFFORT_ENV_LABEL),
    ):
        if os.environ.get(env_var):
            lines.append(thinking_env_sentence(label, source.get(key)))
    return lines


def matching_environment_lines(selections: dict, config: dict | None = None) -> list[str]:
    """Keep an environment sentence only when this submission uses that value."""
    from cli.selections import (
        ANTHROPIC_EFFORT_ENV_LABEL,
        GEMINI_THINKING_ENV_LABEL,
        OPENAI_REASONING_ENV_LABEL,
    )

    source = config if config is not None else DEFAULT_CONFIG
    kept = []
    for line in environment_confirmation_lines(source):
        if line.startswith("✓ Output language from environment:"):
            if selections.get("output_language") == source.get("output_language"):
                kept.append(line)
        elif line.startswith("✓ LLM provider from environment:"):
            if str(selections.get("llm_provider") or "").lower() == str(source.get("llm_provider") or "").lower():
                kept.append(line)
        elif line.startswith("✓ Backend URL:"):
            submitted = selections.get("backend_url")
            from cli.prompts import resolve_backend_url
            resolved = resolve_backend_url(
                str(source.get("llm_provider") or ""), env_url=source.get("backend_url"),
            )
            if submitted in (None, "", resolved):
                kept.append(line)
        elif line.startswith("✓ Thinking agents from environment:"):
            if (selections.get("quick_think_llm") == source.get("quick_think_llm")
                    and selections.get("deep_think_llm") == source.get("deep_think_llm")):
                kept.append(line)
        elif line.startswith(f"✓ {GEMINI_THINKING_ENV_LABEL} from environment:"):
            if selections.get("google_thinking_level") == source.get("google_thinking_level"):
                kept.append(line)
        elif line.startswith(f"✓ {OPENAI_REASONING_ENV_LABEL} from environment:"):
            if selections.get("openai_reasoning_effort") == source.get("openai_reasoning_effort"):
                kept.append(line)
        elif (line.startswith(f"✓ {ANTHROPIC_EFFORT_ENV_LABEL} from environment:")
              and selections.get("anthropic_effort") == source.get("anthropic_effort")):
            kept.append(line)
    return kept


def depth_lines_for(selections: dict | None = None) -> list[str]:
    """CLI depth sentences. A page risk count does not replace the environment."""
    from cli.run import depth_environment_lines

    return depth_environment_lines(DEFAULT_CONFIG)


def run_preamble_lines(selections: dict) -> list[str]:
    """CLI sentences printed before the live view, and omitted from it.

    A supplied ticker or date is a flag. A blank field uses the interactive
    default and must not claim the flag sentence.
    """
    lines = []
    if not selections.get("ticker_from_default"):
        from cli.selections import ticker_flag_sentence
        lines.append(ticker_flag_sentence(selections["ticker"]))
    if not selections.get("date_from_default"):
        from cli.selections import analysis_date_flag_sentence
        lines.append(analysis_date_flag_sentence(selections["analysis_date"]))
    lines.extend(matching_environment_lines(selections))
    lines.extend(depth_lines_for(selections))
    return lines


def flag_confirmations(selections: dict) -> list[str]:
    """The CLI sentences printed when ticker, date and analysts arrive as flags.

    A blank ticker or date uses the interactive default and does not claim the
    flag sentence. The page does not claim the other answers came from the environment.
    """
    lines = []
    if not selections.get("ticker_from_default"):
        from cli.selections import ticker_flag_sentence
        lines.append(ticker_flag_sentence(selections["ticker"]))
    if selections.get("asset_type") not in (None, "", "stock"):
        from cli.selections import detected_asset_sentence
        lines.append(detected_asset_sentence(selections["asset_type"]))
    if not selections.get("date_from_default"):
        from cli.selections import analysis_date_flag_sentence
        lines.append(analysis_date_flag_sentence(selections["analysis_date"]))
    from cli.selections import selected_analysts_sentence
    lines.append(selected_analysts_sentence(selections.get("analysts") or []))
    return lines


def report_saved_lines(saved_to: str, report_name: str, html_name: str | None = None) -> list[str]:
    """The visible save lines from cli/run.py, without Rich markup."""
    from cli.run import COMPLETE_REPORT_PREFIX, HTML_REPORT_PREFIX, REPORT_SAVED_PREFIX

    lines = [
        f"{REPORT_SAVED_PREFIX} {saved_to}",
        f"{COMPLETE_REPORT_PREFIX} {report_name}",
    ]
    if html_name:
        lines.append(f"{HTML_REPORT_PREFIX} {html_name}")
    return lines


def resolve_save_path(requested, ticker: str, results_dir: str | None = None) -> str | None:
    """Keep a custom save folder under results/reports.

    The CLI prompt can choose any folder. The console refuses paths outside
    its results directory so a page cannot write elsewhere.
    """
    text = str(requested or "").strip()
    if not text:
        return None
    from tradingagents.dataflows.symbols import safe_ticker_component

    root = (Path(results_dir or effective_results_dir()) / "reports").resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / safe_ticker_component(ticker) / candidate
    resolved = candidate.resolve()
    if root not in resolved.parents and resolved != root:
        raise ConsoleError("保存路径必须在结果目录的 reports 下。命令行可以选其他文件夹，控制台不写出这个目录。")
    return str(resolved)


def normalize_data_vendors(payload: dict) -> dict[str, str] | None:
    """Keep only vendor chains the router can serve. Empty means leave defaults."""
    raw = payload.get("data_vendors")
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise ConsoleError("数据供应商必须按类别填写。")
    from tradingagents.dataflows.router import TOOLS_CATEGORIES, VENDOR_METHODS

    cleaned: dict[str, str] = {}
    for category, info in TOOLS_CATEGORIES.items():
        if category not in raw or raw[category] in (None, ""):
            continue
        allowed: list[str] = []
        for method in info["tools"]:
            for name in VENDOR_METHODS.get(method, {}):
                if name not in allowed:
                    allowed.append(name)
        chain = [part.strip() for part in str(raw[category]).split(",") if part.strip()]
        if not chain or chain == ["default"]:
            cleaned[category] = "default"
            continue
        unknown = [part for part in chain if part not in allowed]
        if unknown:
            raise ConsoleError(
                f"{category} 不支持 {', '.join(unknown)}。可选：{', '.join(allowed)}。"
            )
        cleaned[category] = ",".join(chain)
    return cleaned or None


# Read timeout (seconds) when the page leaves 模型调用超时 empty. Calls stream,
# so this is the longest silence between chunks; connect/write/pool get their
# own shorter bounds (RUN-002, was a flat 120 seconds).
CONSOLE_MODEL_TIMEOUT = 300
# Wall-clock budget for one model call, retries and waits included, when the
# page leaves 单次调用总时限 empty.
CONSOLE_CALL_BUDGET = 900


def limit_labels() -> dict[str, str]:
    """Integer config-key labels. The names come from the catalog, not a second list."""
    from webui.catalog import config_key_rows

    return {
        row["field"]: f"{row['zh']} {row['field']}"
        for row in config_key_rows()
        if row.get("limit")
    }


def optional_limit(value, label: str) -> int | None:
    """A positive integer from the page. Empty keeps the CLI default."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ConsoleError(f"{label}必须是整数。")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ConsoleError(f"{label}必须是整数。") from exc
    if number < 1:
        raise ConsoleError(f"{label}至少为 1。")
    return number


def normalize_tool_vendors(payload: dict) -> dict[str, str] | None:
    """Tool overrides the router prefers over category chains. Empty means none."""
    raw = payload.get("tool_vendors")
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise ConsoleError("工具级供应商必须按工具名填写。")
    from tradingagents.dataflows.router import VENDOR_METHODS

    cleaned: dict[str, str] = {}
    for method, value in raw.items():
        if method not in VENDOR_METHODS:
            raise ConsoleError(f"没有这个数据工具：{method}。")
        if value in (None, ""):
            continue
        allowed = list(VENDOR_METHODS[method])
        chain = [part.strip() for part in str(value).split(",") if part.strip()]
        if not chain or chain == ["default"]:
            continue
        unknown = [part for part in chain if part not in allowed]
        if unknown:
            raise ConsoleError(
                f"{method} 不支持 {', '.join(unknown)}。可选：{', '.join(allowed)}。"
            )
        cleaned[method] = ",".join(chain)
    return cleaned or None


def optional_timeout(value) -> float | None:
    """A positive model-call timeout. Empty keeps the console's CONSOLE_MODEL_TIMEOUT bound."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ConsoleError("模型调用超时必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConsoleError("模型调用超时必须是数字。") from exc
    if number <= 0:
        raise ConsoleError(
            f"模型调用超时必须大于 0。命令行不设这一项时使用供应商默认；控制台留空则使用 {CONSOLE_MODEL_TIMEOUT} 秒。"
        )
    return number


def optional_call_budget(value) -> float | None:
    """A positive per-call budget in seconds. Empty keeps CONSOLE_CALL_BUDGET."""
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ConsoleError("单次调用总时限必须是数字。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConsoleError("单次调用总时限必须是数字。") from exc
    if number <= 0:
        raise ConsoleError(f"单次调用总时限必须大于 0。留空则使用 {CONSOLE_CALL_BUDGET} 秒。")
    return number


def apply_console_call_bounds(config: dict) -> dict:
    """The console's model-call bounds where the page left them empty.

    A streamed read timeout of CONSOLE_MODEL_TIMEOUT seconds (split into
    connect/read/write/pool by the client) and one retry layer bounded by
    CONSOLE_CALL_BUDGET seconds per call. Retries default to 3 (llm_max_retries).
    """
    if not config.get("timeout"):
        config["timeout"] = CONSOLE_MODEL_TIMEOUT
    if not config.get("llm_call_budget"):
        config["llm_call_budget"] = CONSOLE_CALL_BUDGET
    return config


def console_checkpoint_enabled(selections: dict) -> bool:
    """Console analyses checkpoint unless the page or TRADINGAGENTS_CHECKPOINT_ENABLED says no."""
    chosen = selections.get("checkpoint")
    if chosen is not None:
        return bool(chosen)
    if os.environ.get("TRADINGAGENTS_CHECKPOINT_ENABLED"):
        return bool(DEFAULT_CONFIG.get("checkpoint_enabled"))
    return True


def normalize_news_queries(payload: dict) -> list[str] | None:
    """Macro search lines. Empty keeps the built-in queries."""
    if "global_news_queries" not in payload:
        return None
    raw = payload.get("global_news_queries")
    if raw in (None, "", []):
        return None
    if isinstance(raw, str):
        parts = raw.splitlines()
    elif isinstance(raw, list):
        parts = raw
    else:
        raise ConsoleError("宏观新闻查询必须按行填写。")
    queries = [str(item).strip() for item in parts if str(item).strip()]
    return queries or None


def constrain_runtime(payload: dict) -> dict:
    """Reject console paths and vendor names before they are stored."""
    clean = dict(payload)
    if "data_cache_dir" in clean:
        clean["data_cache_dir"] = resolve_cache_dir(str(clean["data_cache_dir"])) if str(clean.get("data_cache_dir") or "").strip() else None
    if "results_dir" in clean:
        clean["results_dir"] = resolve_results_dir(str(clean["results_dir"])) if str(clean.get("results_dir") or "").strip() else None
    if "memory_log_path" in clean:
        clean["memory_log_path"] = resolve_memory_path(str(clean["memory_log_path"])) if str(clean.get("memory_log_path") or "").strip() else None
    if "data_vendors" in clean:
        clean["data_vendors"] = normalize_data_vendors(clean)
    if "tool_vendors" in clean:
        clean["tool_vendors"] = normalize_tool_vendors(clean)
    for key, label in limit_labels().items():
        if key in clean:
            clean[key] = optional_limit(clean.get(key), label)
    if "global_news_queries" in clean:
        clean["global_news_queries"] = normalize_news_queries(clean)
    if "timeout" in clean:
        clean["timeout"] = optional_timeout(clean.get("timeout"))
    if "llm_call_budget" in clean:
        clean["llm_call_budget"] = optional_call_budget(clean.get("llm_call_budget"))
    if "max_risk_discuss_rounds" in clean:
        clean["max_risk_discuss_rounds"] = _optional_rounds(clean.get("max_risk_discuss_rounds"))
    return clean


def _optional_rounds(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        rounds = int(value)
    except (TypeError, ValueError) as exc:
        raise ConsoleError("风险辩论轮数必须是整数，对应 TRADINGAGENTS_MAX_RISK_ROUNDS。") from exc
    if rounds < 1:
        raise ConsoleError("风险辩论轮数至少为 1。")
    return rounds


def _effort_for(family: str, payload: dict, *providers: str) -> str | None:
    field = {
        "openai": "openai_reasoning_effort",
        "google": "google_thinking_level",
        "anthropic": "anthropic_effort",
    }[family]
    if not any(reasoning_family(item) == family for item in providers if item):
        return None
    value = payload.get(field)
    return str(value) if value else None


def _require_credential(provider: str) -> str | None:
    if provider in SIGNED_IN_PROVIDERS:
        spec = get_subscription(provider)
        unreadable = ConsoleError(f"{spec.display_name} 的本机登录文件读取失败。请在账户页重新登录。")
        if _auth_file_problem(spec):
            raise unreadable
        try:
            account = spec.auth.signed_in_as()
        except Exception:  # same validator path as the status page; never leak internals
            raise unreadable from None
        if not account:
            raise ConsoleError(f"尚未登录 {spec.display_name}。请先在账户页登录。")
        return plan_use_sentence(provider, account)
    env_var = get_api_key_env(provider)
    if env_var and not os.environ.get(env_var):
        raise ConsoleError(
            f"供应商 {provider} 需要 {env_var}。请在账户页保存，不要把订阅令牌填到这里。"
            f" 对应命令行：{missing_key_sentence(env_var)}"
        )


def _memory_root() -> Path:
    return Path.home() / ".tradingagents"


def resolve_memory_path(requested: str) -> str:
    """Keep a console-chosen memory log under ~/.tradingagents.

    The CLI environment variable may point elsewhere. The page must not.
    """
    text = str(requested or "").strip()
    if not text:
        raise ConsoleError("记忆日志路径不能为空。")
    root = _memory_root().resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / "memory" / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ConsoleError("记忆日志必须在 ~/.tradingagents 下。命令行环境变量可以指向别处，控制台不写到外面。")
    return str(resolved)


def resolve_cache_dir(requested: str) -> str:
    """Keep a console-chosen cache directory under ~/.tradingagents."""
    text = str(requested or "").strip()
    if not text:
        raise ConsoleError("缓存目录不能为空。")
    root = _memory_root().resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ConsoleError("缓存目录必须在 ~/.tradingagents 下。命令行 TRADINGAGENTS_CACHE_DIR 可以指向别处，控制台不写到外面。")
    return str(resolved)


def resolve_results_dir(requested: str) -> str:
    """Keep a console-chosen results directory under ~/.tradingagents."""
    text = str(requested or "").strip()
    if not text:
        raise ConsoleError("结果目录不能为空。")
    root = _memory_root().resolve()
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, ValueError):
        raise ConsoleError(RESULTS_DIR_INVALID) from None
    _check_path_text(str(resolved), RESULTS_DIR_INVALID)    # W13/W15/W16: too long -> 400 here
    if resolved != root and root not in resolved.parents:
        raise ConsoleError("结果目录必须在 ~/.tradingagents 下。命令行 TRADINGAGENTS_RESULTS_DIR 可以指向别处，控制台不写到外面。")
    return str(resolved)


def console_default_results_dir() -> str:
    """Where results go when the page names no results directory (W24).

    A console started with its own TRADINGAGENTS_WEBUI_HOME (a second install, a
    test home) keeps its results under ``<home>/results`` instead of writing into
    the shared ~/.tradingagents/logs. TRADINGAGENTS_RESULTS_DIR still wins, as in
    the CLI; without either variable nothing changes.
    """
    if os.environ.get("TRADINGAGENTS_WEBUI_HOME") and not os.environ.get("TRADINGAGENTS_RESULTS_DIR"):
        return str(webui_home() / "results")
    return DEFAULT_CONFIG["results_dir"]


def effective_results_dir() -> str:
    saved = load_saved_config().get("results_dir")
    if not saved:
        return console_default_results_dir()
    try:
        return resolve_results_dir(str(saved))
    except ConsoleError:
        return console_default_results_dir()


def effective_cache_dir() -> str:
    # The CLI treats an explicit cache environment variable as authoritative;
    # the crawler must make the same choice rather than letting an older saved
    # WebUI setting split its store from the worker.
    if os.environ.get("TRADINGAGENTS_CACHE_DIR"):
        return DEFAULT_CONFIG["data_cache_dir"]
    saved = load_saved_config().get("data_cache_dir")
    if not saved:
        return DEFAULT_CONFIG["data_cache_dir"]
    try:
        return resolve_cache_dir(str(saved))
    except ConsoleError:
        return DEFAULT_CONFIG["data_cache_dir"]



def _stored_or_env_path(selections: dict, skipped: dict, key: str, resolver) -> str | None:
    """Keep a stored path. The current environment fills only an omitted field.

    A new launch copies the environment into selections before this runs, so
    that copy still follows the environment. A restart's stored path is
    different from the current environment and wins. Environment paths may sit
    outside the console root; only a console-chosen path is resolved there.
    """
    explicit = _explicit_selection(selections, key)
    env_value = skipped.get(key) if key in skipped else None
    if explicit is not None and (env_value is None or str(explicit) != str(env_value)):
        return resolver(str(explicit))
    if env_value is not None:
        return str(env_value)
    if explicit is not None:
        return resolver(str(explicit))
    return None


def _explicit_selection(selections: dict, key: str):
    """A stored or caller-supplied value. None and "" mean the field was omitted."""
    value = selections.get(key)
    return None if value in (None, "") else value


def build_run_config(selections: dict) -> dict:
    """Build the worker config.

    A new launch copies the environment into selections first
    (``apply_env_skips``). A restart must not do that again: an explicit
    selection wins over the current environment, and the environment fills
    only fields the selection omitted.
    """
    config = _build_run_config(selections, selections.get("checkpoint"))
    skipped = env_skipped_values()
    for key in ("temperature", "max_tokens", "llm_max_retries", "max_tool_rounds", "benchmark_ticker"):
        explicit = _explicit_selection(selections, key)
        if explicit is not None:
            value = explicit
            if key == "temperature":
                value = float(value)
            elif key != "benchmark_ticker":
                value = int(value)
            config[key] = value
        elif key in skipped:
            config[key] = skipped[key]
    for tier in ("quick", "deep"):
        provider = selections.get(f"{tier}_think_provider")
        if provider:
            config[f"{tier}_think_provider"] = provider
        url = selections.get(f"{tier}_think_backend_url")
        if url:
            config[f"{tier}_think_backend_url"] = url
    vendors = selections.get("data_vendors")
    if vendors:
        config["data_vendors"] = {**(config.get("data_vendors") or {}), **vendors}
    config["memory_log_path"] = _stored_or_env_path(
        selections, skipped, "memory_log_path", resolve_memory_path)
    if selections.get("max_risk_discuss_rounds"):
        config["max_risk_discuss_rounds"] = int(selections["max_risk_discuss_rounds"])
    cache_dir = _stored_or_env_path(selections, skipped, "data_cache_dir", resolve_cache_dir)
    if cache_dir is not None:
        config["data_cache_dir"] = cache_dir
    else:
        # The WebUI settings drawer persists this value separately from the
        # process-wide DEFAULT_CONFIG. Keep an analysis launched by a caller
        # that omitted the field on the same cache tree as the information
        # crawler (and as the form defaults).
        config["data_cache_dir"] = effective_cache_dir()
    results_dir = _stored_or_env_path(selections, skipped, "results_dir", resolve_results_dir)
    config["results_dir"] = results_dir if results_dir is not None else console_default_results_dir()
    if selections.get("tool_vendors"):
        config["tool_vendors"] = {**(config.get("tool_vendors") or {}), **selections["tool_vendors"]}
    for key in limit_labels():
        if selections.get(key) not in (None, ""):
            config[key] = int(selections[key])
    if selections.get("global_news_queries"):
        config["global_news_queries"] = list(selections["global_news_queries"])
    pick = selections.get("info_crawl")
    if (_info_crawl_has_consumer(selections)
            and isinstance(pick, dict) and pick.get("job") and pick.get("items")):
        config["info_crawl_job"] = pick["job"]
        config["info_crawl_items"] = list(pick["items"])
    local_import = selections.get("local_import")
    if isinstance(local_import, list) and local_import:
        config["local_import_items"] = list(local_import)
    if selections.get("timeout") not in (None, ""):
        config["timeout"] = float(selections["timeout"])
    if selections.get("llm_call_budget") not in (None, ""):
        config["llm_call_budget"] = float(selections["llm_call_budget"])
    return config



LOCAL_IMPORT_NO_NEWS = "未选新闻分析师，本地导入的资料不会被使用。请勾选新闻分析师，或清除导入。"


def validate_local_import(payload: dict) -> dict:
    """Check a CSV/JSON the browser already read. No network fetch."""
    if not isinstance(payload, dict):
        raise ConsoleError("请上传 CSV 或 JSON 文件。")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ConsoleError("文件是空的。")
    from tradingagents.dataflows.local_import import LocalImportError, parse_local_import
    try:
        items = parse_local_import(text, str(payload.get("filename") or ""))
    except LocalImportError as exc:
        raise ConsoleError(str(exc)) from None
    return {
        "ok": True,
        "count": len(items),
        "filename": items[0].get("filename") or "",
        "items": items,
        "preview": [{"title": item["title"], "published_at": item.get("published_at") or "未注明日期"} for item in items[:5]],
    }


def _local_import_from_payload(payload) -> list | None:
    if not isinstance(payload, dict) or not payload.get("local_import"):
        return None
    raw = payload.get("local_import")
    from tradingagents.dataflows.local_import import LocalImportError, parse_local_import
    try:
        if isinstance(raw, str):
            return parse_local_import(raw, str(payload.get("local_import_name") or ""))
        if isinstance(raw, list):
            import json
            return parse_local_import(json.dumps(raw, ensure_ascii=False), "import.json")
    except LocalImportError as exc:
        raise ConsoleError(str(exc)) from None
    raise ConsoleError("本地导入的内容格式不正确。")


def _local_import_notices(selections: dict | None) -> list[str]:
    items = selections.get("local_import") if isinstance(selections, dict) else None
    if items and not _info_crawl_has_consumer(selections):
        return [LOCAL_IMPORT_NO_NEWS]
    return []


def _require_local_import_news(selections: dict) -> None:
    if selections.get("local_import") and not _info_crawl_has_consumer(selections):
        raise ConsoleError(LOCAL_IMPORT_NO_NEWS)

def _info_crawl_pick(payload) -> dict | None:
    """The 信息抓取 rows to bring into the run: ``{"job", "items"}``, or ``None``.

    ``info_inject`` is the list of item ids the user ticked row by row (default
    none, PM). An empty or missing list brings nothing in.
    """
    if not isinstance(payload, dict):
        return None
    raw = payload.get("info_inject")
    if raw in (None, [], False):
        return None
    from tradingagents.dataflows.info_crawl.store import valid_item_id, valid_job_id
    if not isinstance(raw, list) or not all(valid_item_id(item) for item in raw) or len(raw) > 100:
        raise ConsoleError("带入分析的条目编号不合法")
    job = payload.get("info_crawl_job")
    if not valid_job_id(job):
        raise ConsoleError("带入分析的信息抓取任务编号不合法")
    return {"job": job, "items": list(dict.fromkeys(raw))}


INFO_CRAWL_ATTACHMENT_GONE = (
    "带入分析的资讯附件已失效：当前缓存目录中找不到所选抓取任务或条目。"
    "可能是切换了缓存目录、服务重启后任务中断，或资讯已超过 30 天被清理。"
    "请返回「信息抓取」重新抓取并勾选。"
)
INFO_CRAWL_NO_NEWS_ANALYST = "未选新闻分析师，附件不会被使用。"


def info_attach_run_line(config: dict) -> str | None:
    """AUDIT-FIX-04: tell the run log how many attached rows the worker really found.

    ``build_run_config`` only carries ``info_crawl_job``/``info_crawl_items`` when the
    news analyst (their only consumer) is selected, so this line is about rows that
    will be used. The rows are read from the run's own cache tree
    (``<data_cache_dir>/info-crawl``), the same directory the console crawls into.
    """
    job, wanted = config.get("info_crawl_job"), list(dict.fromkeys(config.get("info_crawl_items") or []))
    if not job or not wanted:
        return None
    from tradingagents.dataflows.info_crawl.store import JsonlItemStore, default_store

    root = config.get("info_crawl_dir")
    if not root and config.get("data_cache_dir"):
        root = Path(config["data_cache_dir"]) / "info-crawl"
    try:
        target = JsonlItemStore(Path(root)) if root else default_store()
        found = len({item.id for item in target.load_items(job)} & set(wanted))
    except Exception:  # noqa: BLE001 - the notice must never stop a run
        found = 0
    if found == len(wanted):
        return f"已带入 {found} 条信息抓取资讯（由新闻分析师使用）。"
    return (f"注意：带入的 {len(wanted)} 条资讯中只找到 {found} 条（其余可能已过期清理，或数据缓存目录已更换），"
            "本次分析只使用找到的条目。")


def _effective_analysis_analysts(selections: dict | None) -> set[str]:
    """Return the analyst keys after any environment overlay has won."""
    selections = selections if isinstance(selections, dict) else {}
    # ``apply_env_skips`` normally writes this into selections before this
    # helper is reached.  Reading the overlay as well keeps direct worker/API
    # callers on the same rule and lets environment-backed test/config layers
    # override a stale page payload.
    raw = selections.get("analysts")
    if raw is None:
        # A direct caller may omit the already-applied list. Normal WebUI
        # validation writes the environment result into selections first.
        raw = env_skipped_values().get("analysts")
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple, set)):
        return set()
    return {
        str(getattr(item, "value", item)).strip().lower()
        for item in raw
        if str(getattr(item, "value", item)).strip()
    }


def _info_crawl_has_consumer(selections: dict | None) -> bool:
    """Only the news analyst consumes selected information-crawl rows."""
    return "news" in _effective_analysis_analysts(selections)


def _info_crawl_notices(selections: dict | None) -> list[str]:
    pick = selections.get("info_crawl") if isinstance(selections, dict) else None
    if isinstance(pick, dict) and pick.get("job") and pick.get("items") and not _info_crawl_has_consumer(selections):
        return [INFO_CRAWL_NO_NEWS_ANALYST]
    return []


def _validate_info_crawl_attachment(selections: dict, config: dict | None = None) -> None:
    """Refuse a run that would silently lose its selected crawl rows.

    The browser carries only job/item ids.  Those ids are useful only in the
    ``info-crawl`` directory belonging to this run's effective cache config;
    checking that directory before launch makes a cache-dir switch, restart,
    or retention purge actionable instead of producing a report without the
    user's requested evidence.
    """
    pick = selections.get("info_crawl") if isinstance(selections, dict) else None
    if not isinstance(pick, dict) or not pick.get("job") or not pick.get("items"):
        return
    if not _info_crawl_has_consumer(selections):
        return
    config = config or build_run_config(selections)
    try:
        from tradingagents.dataflows.info_crawl.store import JsonlItemStore

        target = JsonlItemStore(Path(config["data_cache_dir"]) / "info-crawl")
        # Retention is a property of the attachment, not only of the first
        # API read after a restart. Purge here so a worker cannot accept a
        # selected row merely because its directory was never opened by the
        # console's normal store initializer.
        target.purge()
        job = target.load_job(pick["job"])
        if job is None:
            raise ConsoleError(INFO_CRAWL_ATTACHMENT_GONE, 409)
        if job.get("status") not in {"done", "partial"}:
            raise ConsoleError(INFO_CRAWL_ATTACHMENT_GONE, 409)
        available = {item.id for item in target.load_items(pick["job"])}
        if not set(pick["items"]).issubset(available):
            raise ConsoleError(INFO_CRAWL_ATTACHMENT_GONE, 409)
    except ConsoleError:
        raise
    except (OSError, TypeError, ValueError, KeyError):
        raise ConsoleError(INFO_CRAWL_ATTACHMENT_GONE, 409) from None


RUNS_DIRNAME = "runs"
LATEST_POINTER = ".latest"
RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
LIVE_STATUSES = ("starting", "running", "stopping")
DEFAULT_MAX_RUNNING = 2
DEFAULT_MAX_QUEUED = 5


def _limit(env: str, key: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(env)
    if raw in (None, ""):
        raw = load_saved_config().get(key)
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def run_limits() -> dict:
    """How many analyses run at once and how many wait (RUN-004).

    ``TRADINGAGENTS_WEBUI_MAX_RUNS`` / ``TRADINGAGENTS_WEBUI_MAX_QUEUE`` win over
    the console config keys ``max_concurrent_runs`` / ``max_queued_runs``.
    Defaults 2 and 5; clamped to 1..8 running and 0..20 queued.
    """
    return {
        "max_running": _limit("TRADINGAGENTS_WEBUI_MAX_RUNS", "max_concurrent_runs", DEFAULT_MAX_RUNNING, 1, 8),
        "max_queued": _limit("TRADINGAGENTS_WEBUI_MAX_QUEUE", "max_queued_runs", DEFAULT_MAX_QUEUED, 0, 20),
    }


def _stamp(record: dict, *keys: str) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def order_runs(records: list[dict]) -> list[dict]:
    """Running first (oldest start first), then queued in arrival order, then the rest newest first."""
    live = sorted((r for r in records if r.get("status") in LIVE_STATUSES),
                  key=lambda r: (_stamp(r, "started_at"), int(r.get("start_seq") or 0), str(r.get("id"))))
    queued = sorted((r for r in records if r.get("status") == "queued"),
                    key=lambda r: (int(r.get("queue_seq") or 0), str(r.get("id"))))
    rest = sorted((r for r in records if r.get("status") not in (*LIVE_STATUSES, "queued")),
                  key=lambda r: (_stamp(r, "started_at", "queued_at", "finished_at"), str(r.get("id"))),
                  reverse=True)
    return live + queued + rest


def _migrated_backup_path(legacy: Path) -> Path:
    """Where a migrated current-run.json is kept: a new timestamped name every time,
    so a second migration never overwrites an earlier backup (W38)."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = legacy.with_name(f"current-run.json.migrated-{stamp}")
    n = 1
    while candidate.exists():
        n += 1
        candidate = legacy.with_name(f"current-run.json.migrated-{stamp}-{n}")
    return candidate


class JobStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._login: dict | None = None
        self._paste: queue.Queue[str] = queue.Queue()

    def path(self, name: str) -> Path:
        return webui_home() / name

    # RUN-004: one record per run under runs/<id>.json instead of a single
    # current-run.json. A current-run.json left by an older console is moved
    # into runs/ the first time the list is read (and kept as .migrated-<timestamp>).

    def runs_dir(self) -> Path:
        return self.path(RUNS_DIRNAME)

    def run_path(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
            raise ConsoleError("没有这个任务。", 404)
        return self.runs_dir() / f"{run_id}.json"

    def _migrate_legacy(self) -> None:
        legacy = self.path("current-run.json")
        if not legacy.is_file():
            return
        try:
            record = json.loads(legacy.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return
        if not isinstance(record, dict):
            return
        if not isinstance(record.get("id"), str) or not RUN_ID_PATTERN.fullmatch(record["id"]):
            record["id"] = "legacy-" + datetime.fromtimestamp(legacy.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
        record.setdefault("legacy", True)
        target = self.run_path(record["id"])
        if not target.exists():
            self.write_run(record)
        with contextlib.suppress(OSError):
            os.replace(legacy, _migrated_backup_path(legacy))

    def read_run(self, run_id: str) -> dict | None:
        path = self.run_path(run_id)
        if not path.is_file():
            self._migrate_legacy()
            if not path.is_file():
                return None
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def list_runs(self) -> list[dict]:
        """Every stored run, running first, then the queue in order, then the rest newest first."""
        self._migrate_legacy()
        folder = self.runs_dir()
        records = []
        if folder.is_dir():
            for path in folder.glob("*.json"):
                try:
                    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
                except (OSError, ValueError):
                    continue
                if isinstance(data, dict):
                    data.setdefault("id", path.stem)
                    records.append(data)
        return order_runs(records)

    def current_run(self) -> dict | None:
        """The newest run: what a request without a run id is about."""
        self._migrate_legacy()
        with contextlib.suppress(OSError, ConsoleError):
            latest = (self.runs_dir() / LATEST_POINTER).read_text(encoding="utf-8").strip()
            record = self.read_run(latest)
            if record is not None:
                return record
        runs = self.list_runs()
        return runs[0] if runs else None

    def write_run(self, record: dict) -> None:
        """Replace runs/<id>.json in one step (R2-MF1).

        The record is encoded first and written to a temporary file that
        replaces the old one only once it is complete on disk, so a record
        that cannot be encoded (or a crash mid-write) leaves the previous
        file whole instead of an empty one.
        """
        path = self.run_path(str(record.get("id") or ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(redact(record), ensure_ascii=False, indent=2).encode("utf-8")
        if not path.exists():
            # The newest run is what a request without a run id means (the old
            # single current-run.json was always the newest run).
            with contextlib.suppress(OSError):
                (path.parent / LATEST_POINTER).write_text(str(record["id"]), encoding="utf-8")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(temporary, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def begin_login(self, provider: str) -> dict:
        with self._lock:
            if self._login and self._login.get("status") == "waiting":
                raise ConsoleError("已有一个登录正在等待浏览器确认。", 409)
            self._paste = queue.Queue()
            self._login = {
                "provider": provider,
                "status": "waiting",
                "messages": [],
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
            return dict(self._login)

    def wait_paste(self, timeout: float = 600) -> str:
        try:
            return self._paste.get(timeout=timeout)
        except queue.Empty as exc:
            raise ConsoleError("等待粘贴登录地址超时。") from exc

    def paste_login(self, line: str) -> dict:
        text = (line or "").strip()
        if not text:
            from tradingagents.llm_clients.chatgpt_auth import CHATGPT_PASTE_PROMPT
            raise ConsoleError(
                "请粘贴浏览器最终打开的地址。对应命令行："
                + CHATGPT_PASTE_PROMPT
            )
        with self._lock:
            if not self._login or self._login.get("status") != "waiting":
                raise ConsoleError("当前没有等待粘贴的登录。")
            if self._login.get("provider") != "chatgpt":
                raise ConsoleError("Grok 登录不读取粘贴地址。命令行不会打印 ChatGPT 的粘贴句。")
        self._paste.put(text)
        return {"accepted": True}

    def login_state(self) -> dict:
        with self._lock:
            state = dict(self._login or {"status": "idle", "messages": []})
        if state.get("status") == "signed_in" and state.get("provider") and state.get("account"):
            label = _subscription_label(state["provider"])
            from cli.main import signed_in_sentence
            state["cli"] = signed_in_sentence(label, state["account"])
            state["zh"] = f"已登录 {label}：{state['account']}。"
        return state

    def append_login(self, line: str) -> None:
        with self._lock:
            if self._login is None:
                return
            self._login["messages"].append(line)
            if line.startswith("http://") or line.startswith("https://"):
                self._login["url"] = line.strip()
            if "code:" in line.lower() or "enter code" in line.lower():
                self._login["hint"] = line

    def finish_login(self, *, account: str | None = None, error: str | None = None) -> None:
        with self._lock:
            if self._login is None:
                return
            self._login["status"] = "failed" if error else "signed_in"
            self._login["account"] = account
            self._login["error"] = error


JOBS = JobStore()


def start_login(provider) -> dict:
    from webui.catalog import plan_cli_error

    if provider is None:
        raise ConsoleError(plan_cli_error("login", None))
    if provider not in SIGNED_IN_PROVIDERS:
        raise ConsoleError(plan_cli_error("login", provider))
    key = provider
    spec = get_subscription(key)
    state = JOBS.begin_login(key)

    def _say(line: str) -> None:
        JOBS.append_login(str(line))

    def _read_paste() -> str:
        try:
            return JOBS.wait_paste()
        except ConsoleError as exc:
            JOBS.finish_login(error=str(exc))
            raise EOFError from None

    def _run() -> None:
        try:
            account = spec.auth.sign_in(say=_say, read_line=_read_paste)
            JOBS.finish_login(account=str(account))
        except Exception as exc:
            JOBS.finish_login(error=str(exc) or type(exc).__name__)

    threading.Thread(target=_run, name=f"webui-login-{key}", daemon=True).start()
    return state


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]


def _reap(pid: int) -> bool:
    """Collect our own exited child. A pid that is not our child (a worker started by
    an earlier console, RUN-004) is left to :func:`_alive`'s signal probe."""
    try:
        child, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return child == pid


def _is_zombie(pid: int) -> bool:
    """Whether an already-exited, unreaped worker is only a zombie (macOS/Linux)."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return raw[raw.rindex(")") + 2:].split()[0] == "Z"
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, env={**os.environ, "LC_ALL": "C"})
        return out.returncode == 0 and out.stdout.strip().startswith("Z")
    return False


def _alive(pid: int) -> bool:
    if _reap(pid):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return not _is_zombie(pid)


def _process_start(pid: int) -> str | None:
    """When process ``pid`` started, as an opaque stamp, or None when it is gone (W39).

    Linux reads the start time in clock ticks from /proc; elsewhere (macOS) ``ps``
    gives it to the second. A pid reused by another program gets a different stamp.
    """
    with contextlib.suppress(OSError, ValueError, IndexError):
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        return "proc:" + raw[raw.rindex(")") + 2:].split()[19]
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, env={**os.environ, "LC_ALL": "C"})
        stamp = out.stdout.strip()
        if out.returncode == 0 and stamp:
            return "ps:" + stamp
    return None


def _process_cmdline(pid: int) -> str:
    with contextlib.suppress(OSError):
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        out = subprocess.run(["ps", "-ww", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout
    return ""


def _worker_state(record: dict) -> str:
    """Is the run's recorded pid still this run's worker? "ours", "gone", "foreign" or "unknown" (W39, W47).

    A live pid alone proves nothing after a console restart: the worker may have
    exited and the pid been given to an unrelated program. The pid must match AND
    the process start time recorded at spawn must match (records written before
    the start time was kept fall back to the run's job file on the command line).
    A definite mismatch is "foreign": it is never signalled and never holds a slot.
    A live pid whose start time cannot be read at all (/proc and ps both fail) is
    "unknown" (W47): the run keeps its slot and stays running, but it is never
    signalled either, since it cannot be proved to be ours.
    """
    pid = record.get("pid")
    if not isinstance(pid, int) or not _alive(pid):
        return "gone"
    recorded = record.get("proc_start")
    if recorded:
        current = _process_start(pid)
        if current is None:
            return "unknown"
        if current != recorded:
            return "foreign"
        if current.startswith("ps:"):
            # AUDIT-FIX-01: ps gives the start time to the second only, so another
            # program started in the same second has the same stamp. Its command
            # line must match too; unreadable means unproven, never signalled.
            if _process_argv(pid) is None and not _process_cmdline(pid).strip():
                return "unknown"
            return "ours" if _argv_matches(record, pid) else "foreign"
        return "ours"
    return "ours" if _argv_matches(record, pid) else "foreign"


def _process_argv(pid: int) -> list[str] | None:
    """The process's argv as exact tokens (/proc), or None when it cannot be read that way."""
    with contextlib.suppress(OSError):
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        return [part.decode("utf-8", "surrogateescape") for part in raw.split(b"\0") if part]
    return None


def _expected_job_path(record: dict) -> str | None:
    argv = record.get("argv")
    if isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv):
        return argv[-1]
    run_id = record.get("id")
    result = record.get("result")
    if isinstance(run_id, str) and RUN_ID_PATTERN.fullmatch(run_id) and isinstance(result, str) and result:
        return str(Path(result).with_name(f"{run_id}.job.json"))
    return None


def _argv_matches(record: dict, pid: int) -> bool:
    """Without a start time, the worker is ours only if its command line is exactly ours (W48, W57).

    A run-id substring is not enough (another run's id, a log path or an editor
    command can contain it). The recorded ``argv`` must equal the process argv
    token by token; a record without ``argv`` needs ``-m webui.worker <its job file>``
    as the final exact tokens. ``ps`` output cannot be split safely, so it must
    equal the recorded argv joined by spaces.
    """
    argv = record.get("argv")
    recorded = list(argv) if isinstance(argv, list) and all(isinstance(a, str) for a in argv) and argv else None
    actual = _process_argv(pid)
    if actual is not None:
        if recorded is not None:
            return actual == recorded
        job_path = _expected_job_path(record)
        # W69: exactly <python> -m webui.worker <job>, nothing before or after.
        return (bool(job_path) and len(actual) == 4 and _is_python(actual[0])
                and actual[1:] == ["-m", "webui.worker", job_path])
    line = _process_cmdline(pid).strip()
    if not line:
        return False
    if recorded is not None:
        return line == " ".join(recorded)
    job_path = _expected_job_path(record)
    suffix = " -m webui.worker " + job_path if job_path else None
    if not suffix or not line.endswith(suffix):
        return False
    program = line[: -len(suffix)]
    return bool(program) and " " not in program and _is_python(program)   # W69: one leading token, a Python


def _is_python(program: str) -> bool:
    """A Python interpreter path (python, python3, python3.13, sys.executable)."""
    return program == sys.executable or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(program).name) is not None


def _is_our_worker(record: dict) -> bool:
    return _worker_state(record) == "ours"


PID_REUSED_SENTENCE = ("这个任务记录的进程号现在属于另一个程序（启动时间对不上），"
                       "原来的分析进程已经不在，没有写出结果。控制台没有向那个程序发送任何信号。")


def pid_reused_sentence(record: dict | None) -> str:
    """The recorded pid belongs to another program. A backtest is not an analysis."""
    kind = (record or {}).get("kind")
    if kind == "analysis":
        return PID_REUSED_SENTENCE
    if kind == "backtest":
        return ("进程号现在属于另一个程序，启动时间对不上。"
                "原来的回测进程已经不在，没有写出结果。没有向那个程序发送信号。")
    return ("进程号现在属于另一个程序，启动时间对不上。"
            "原来的进程已经不在，没有写出结果。不把它写成分析进程。没有向那个程序发送信号。")


PROCESS_UNVERIFIED_SENTENCE = ("读不到这个任务进程的启动时间，无法确认它还是本任务的分析进程。"
                               "为安全起见没有发送任何信号；任务仍算运行中，继续占用名额。"
                               # W58: say what to do next instead of only refusing, on its own line.
                               "\n下一步：稍后点「刷新」再试；若确认该进程已不在或可以结束，"
                               "请在终端结束任务详情里显示的进程号，再刷新本页；"
                               "也可以等它自行结束，名额会自动释放。")


def process_unverified_sentence(record: dict | None) -> str:
    """Cannot prove the pid is ours, so no signal. A backtest is not an analysis."""
    kind = (record or {}).get("kind")
    if kind == "analysis":
        return PROCESS_UNVERIFIED_SENTENCE
    if kind == "backtest":
        return "读不到启动时间，无法确认这是回测进程。没有发送任何信号，任务仍算运行中。\n下一步：稍后点「刷新」再试。"
    return "读不到启动时间，无法确认这是本任务的进程。不把它写成分析进程。没有发送任何信号，任务仍算运行中。\n下一步：稍后点「刷新」再试。"


def _mark_pid_reused(record: dict) -> None:
    record["status"] = "failed"
    record["interrupted"] = True
    record["pid_reused"] = True
    record["detail"] = record["zh"] = pid_reused_sentence(record)
    _note_saved_checkpoint(record)


def _signal_owned_group(pid: int, sig: int) -> bool:
    """Signal only a process that is its own group leader, matching the CLI launcher."""
    try:
        pgid = os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    if pgid != pid:
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def backtest_busy_sentence(limits: dict, running: int, queued: int, *, starting: int = 0, stopping: int = 0) -> str:
    """Why a backtest did not start (W44): it needs a free run slot and an empty queue."""
    waiting = f"，另有 {queued} 个在排队" if queued else ""
    if starting or stopping:
        bits = []
        if running:
            bits.append(f"正在运行 {running} 个")
        if starting:
            bits.append(f"启动中 {starting} 个")
        if stopping:
            bits.append(f"停止中 {stopping} 个")
        detail = "，".join(bits)
    else:
        detail = f"正在运行 {running} 个"
    return (f"运行名额已满或有任务在排队（{detail}，上限 {limits['max_running']} 个{waiting}），"
            f"回测没有启动。回测不排队：等有任务结束、队列清空后再启动。")


def _record(run_id: str | None = None) -> dict | None:
    """The run ``run_id`` names, or the first run of the list when no id is given."""
    if isinstance(run_id, dict):  # internal callers pass the record they already hold
        return run_id
    if run_id in (None, ""):
        return JOBS.current_run()
    record = JOBS.read_run(str(run_id))
    if record is None:
        raise ConsoleError("没有这个任务。", 404)
    return record



def stopped_sentence(record: dict) -> str:
    """What a finished stop says. A backtest is not an analysis."""
    kind = record.get("kind")
    if kind == "backtest":
        return "回测已停止，不是正常完成。不把它写成分析。"
    if kind == "analysis":
        return "任务已停止。不是正常完成。"
    return "任务已停止，不是正常完成。不把它写成分析。"


def stop_run(run_id: str | None = None) -> dict:
    record = _record(run_id)
    if record and record.get("status") == "queued":
        return cancel_run(record["id"])
    try:
        return _stop_record(record)
    finally:
        dispatch_queue()


def _stop_record(record: dict | None) -> dict:
    from cli.launcher import STILL_STARTING, STOP_NOT_RUNNING, STOP_TIMEOUT_SENTENCE, STOPPING

    if not record:
        return {
            "status": "idle",
            "detail": "没有由控制台启动的任务。",
            "zh": "没有由控制台启动的任务。不把它写成分析。",
            "cli": STOP_NOT_RUNNING,
            "note": "这句对应启动脚本在没有自己的进程时的说法。这里没有查询那个进程。",
        }
    pid = record.get("pid")
    if record.get("status") == "running" and not isinstance(pid, int):
        return {
            "status": "starting",
            "detail": "控制台任务还在启动。",
            "zh": "控制台任务还在启动。请稍后再停止。",
            "cli": STILL_STARTING,
            "note": "这句对应启动脚本在子进程还没就绪时的说法。这里没有查询那个进程。",
        }
    state = _worker_state(record) if isinstance(pid, int) else "gone"
    if state == "foreign" and _result_file(record) is not None:
        # The worker already wrote its result; the live pid is a later reuse. Do not signal it.
        _mark_result_finished(record)
        record["finished_at"] = datetime.now().isoformat(timespec="seconds")
        JOBS.write_run(record)
        return record
    if state == "foreign":
        # The pid now belongs to another program: never signal it (W39).
        _mark_pid_reused(record)
        record["finished_at"] = datetime.now().isoformat(timespec="seconds")
        JOBS.write_run(record)
        return record
    if state == "unknown":
        # W47: cannot prove the process is ours, so no signal; the run keeps running.
        raise ConsoleError(process_unverified_sentence(record), 409)
    if not isinstance(pid, int) or not _alive(pid):
        record["status"] = "idle" if not pid else record.get("status", "finished")
        record["detail"] = "没有仍在运行的控制台进程。"
        record["zh"] = "没有仍在运行的控制台进程。"
        record["cli"] = STOP_NOT_RUNNING
        record["note"] = "这句对应启动脚本在没有自己的进程时的说法。这里没有查询那个进程。"
        JOBS.write_run(record)
        return record
    signaled = _signal_owned_group(pid, signal.SIGTERM)
    if not signaled:
        raise ConsoleError("这个进程不是控制台启动的进程组，已拒绝发送停止信号。", 409)
    record["cli"] = STOPPING
    deadline = time.time() + 15
    while time.time() < deadline and _alive(pid):
        time.sleep(0.05)
    if _is_our_worker(record):
        record["cli"] = STOP_TIMEOUT_SENTENCE
        record["zh"] = "15 秒内没有停止。命令行会停在这里；控制台接着发送了 SIGKILL。"
        _signal_owned_group(pid, signal.SIGKILL)
    else:
        record["zh"] = stopped_sentence(record)
    record["status"] = "stopped"
    record["detail"] = record["zh"]
    record["finished_at"] = datetime.now().isoformat(timespec="seconds")
    JOBS.write_run(record)
    return record


def cancel_run(run_id: str) -> dict:
    """Take a queued run off the queue. Nothing was started, so nothing else changes.

    The run must be named: an empty or missing id is refused and changes nothing
    (W36), it never means "the newest run" here.
    """
    if not isinstance(run_id, str) or not run_id.strip():
        raise ConsoleError("取消排队需要指定是哪个任务（id），没有取消任何任务。", 400)
    with RUN_LOCK:
        record = _record(run_id)
        if not record or record.get("status") != "queued":
            raise ConsoleError("只有排队中的任务可以取消排队。", 409)
        record["status"] = "cancelled"
        record["detail"] = record["zh"] = "已取消排队。这个任务没有启动，没有调用模型，也没有写断点。"
        record["finished_at"] = datetime.now().isoformat(timespec="seconds")
        JOBS.write_run(record)
        return redact(record)


def restart_run(resume: bool = False, run_id: str | None = None) -> dict:
    """Start an analysis again with the same selections.

    ``resume`` is the status page's 从断点重新启动: it needs a checkpoint the failed
    run saved, and it turns checkpoints on (and never clears them) for the new run so
    the completed roles are not called again. The new run reads a copy of that
    run's own checkpoint (a run from before RUN-004 has none: its per-ticker
    database is copied instead), so the old run's database is never written to.
    """
    record = _record(run_id)
    kind = (record or {}).get("kind")
    if kind == "backtest":
        raise ConsoleError("这是回测，不能按分析重新启动。没有启动。", 409)
    if kind not in (None, "", "analysis"):
        raise ConsoleError("任务类型未知，不把它写成分析重新启动。没有启动。", 409)
    if not record or not record.get("selections"):
        raise ConsoleError("没有可重新启动的分析参数。")
    selections, notices = restart_selections(record["selections"])
    try:
        framework = resolve_framework(framework_of(record))
    except UnknownFramework as exc:
        raise ConsoleError(f"原任务用的分析框架 {framework_of(record)} 已不可用，没有重新启动。") from exc
    resume_from = None
    if resume:
        checkpoint = record.get("checkpoint") or {}
        if not checkpoint.get("saved"):
            raise ConsoleError("本次未保存断点，只能重新启动。", 409)
        selections = {**selections, "checkpoint": True, "clear_checkpoints": False}
        resume_from = {"run": record.get("id"), "scope": checkpoint.get("scope")}
    if isinstance(record.get("pid"), int) and _worker_state(record) in ("ours", "unknown"):
        _stop_record(record)  # an unverifiable process is refused there (W47): no second run
    if not notices:
        return start_validated_run(selections, background=True, resume_from=resume_from, framework=framework)
    answer = start_validated_run(selections, background=True, resume_from=resume_from, notices=notices,
                                 framework=framework)
    answer["zh"] = " ".join([*notices, str(answer.get("zh") or "")]).strip()
    return answer


# W54: run parameters a restart turns back into numbers. A record written before
# W54 has max_tokens as "[redacted]" (its key contains "token"); that, or any other
# value that is not a number, falls back to the default instead of ending the new
# run at int("[redacted]") before any model is called.
_RESTART_NUMBERS = {"max_tokens": int, "temperature": float, "llm_max_retries": int, "max_tool_rounds": int}


def restart_number_notice(key: str, value: Any) -> str:
    if value == "[redacted]":
        return f"原任务的 {key} 已被脱敏，已改用默认值。"
    return f"原任务的 {key} 不是数字，已改用默认值。"


def restart_selections(selections: dict) -> tuple[dict, list[str]]:
    """The stored selections, with unusable numeric parameters reset to the default."""
    clean = dict(selections)
    notices = []
    for key, kind in _RESTART_NUMBERS.items():
        value = clean.get(key)
        if value in (None, ""):
            continue
        try:
            if isinstance(value, bool):
                raise ValueError(key)
            number = kind(value)
            if isinstance(number, float) and not math.isfinite(number):
                raise ValueError(key)
        except (TypeError, ValueError):
            clean[key] = None
            notices.append(restart_number_notice(key, value))
    return clean, notices


def validate_backtest(payload: dict) -> dict:
    try:
        return _validate_backtest(payload)
    except ValueError as exc:
        raise ConsoleError(str(exc)) from exc


def _validate_backtest(payload: dict) -> dict:
    from cli.main import NO_TICKER_SENTENCE
    from cli.models import AssetType
    from tradingagents.backtest import iter_grid
    from webui.catalog import backtest_missing_errors

    missing = backtest_missing_errors()
    if "tickers" not in payload or payload.get("tickers") is None:
        raise ConsoleError(missing["tickers"])
    raw_tickers = payload.get("tickers")
    if not isinstance(raw_tickers, str):
        raw_tickers = str(raw_tickers)
    if payload.get("start") in (None, ""):
        raise ConsoleError(missing["start"])
    if payload.get("end") in (None, ""):
        raise ConsoleError(missing["end"])
    tickers = [part.strip() for part in raw_tickers.split(",") if part.strip()]
    names = [parse_ticker(item) for item in tickers]
    if not names:
        raise ConsoleError(NO_TICKER_SENTENCE)
    check_portfolio(payload, "没有启动回测。")       # W78: the estimate checks the holdings too
    raw_every = 7 if payload.get("every") in (None, "") else payload.get("every")
    try:
        every = int(raw_every)
    except (TypeError, ValueError):
        from cli.main import every_type_error
        raise ConsoleError(every_type_error(raw_every)) from None
    dates = iter_grid(str(payload.get("start") or ""), str(payload.get("end") or ""), every)
    asset = AssetType(str(payload.get("asset_type") or "stock").strip().lower())
    from cli.models import AnalystType

    raw = payload.get("analysts")
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    elif isinstance(raw, list):
        raw = [str(part).strip() for part in raw if str(part).strip()]
    # An empty --analysts is falsy on the CLI and runs every allowed analyst.
    omitted = not raw
    if omitted:
        parsed = filter_analysts_for_asset_type(list(AnalystType), asset)
    else:
        parsed = parse_analysts(",".join(raw), asset)
    return {
        "tickers": names,
        "dates": dates,
        "asset_type": asset.value,
        "analysts": [item.value for item in parsed],
        "analysts_omitted": omitted,
        "cells": len(names) * len(dates),
        "started": False,
    }


def list_backtest_sweeps(limit: int = 20) -> dict:
    """Existing sweep directories the CLI would resume with --run-id. Does not call a model."""
    try:
        return _list_backtest_sweeps(limit)
    except OSError:      # W13: e.g. Errno 36 from a results_dir saved before the check
        raise ConsoleError(RESULTS_DIR_UNREADABLE, 409) from None


def _list_backtest_sweeps(limit: int) -> dict:
    root = Path(effective_results_dir()) / "backtest"
    if not root.is_dir():
        return {
            "sweeps": [],
            "note": "还没有回测目录。命令行的 --run-id 对应 results_dir/backtest 下的目录。",
        }
    from cli.main import continue_sweep_sentence

    rows = []
    for path in root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        log = path / "trading_memory.md"
        row = {
            "id": path.name,
            "has_log": log.is_file(),
            "summary": None,
            "error": None,
            "continue_sweep": continue_sweep_sentence(path.name),
        }
        if log.is_file():
            try:
                from tradingagents.backtest import summarize
                row["summary"] = summarize(log).render()
            except Exception as exc:
                row["error"] = type(exc).__name__
        rows.append(row)
    rows.sort(key=lambda item: item["id"], reverse=True)
    return {
        "sweeps": rows[:limit],
        "directory": str(root),
        "note": "这些编号对应命令行 --run-id。点一行只填入编号，并显示该编号的续跑原句和已有摘要，不会启动模型。",
    }


def live_section_files() -> dict[str, str]:
    """Filenames the CLI writes, titled by the Current Report panel headings."""
    from cli.display import LIVE_REPORT_SECTION_TITLES

    return {f"{key}.md": title for key, title in LIVE_REPORT_SECTION_TITLES.items()}


LIVE_SECTION_FILES = live_section_files()


def list_live_sections(limit: int = 40) -> list[dict]:
    """CLI live section files under results_dir/<ticker>/<date>/reports."""
    try:
        return _list_live_sections(limit)
    except OSError:      # W13
        raise ConsoleError(RESULTS_DIR_UNREADABLE, 409) from None


def _list_live_sections(limit: int) -> list[dict]:
    root = Path(effective_results_dir()).resolve()
    if not root.is_dir():
        return []
    _probe_readable(root)            # W91
    found = []
    for path in root.glob("*/*/reports/*.md"):
        resolved = path.resolve()
        if resolved.name not in LIVE_SECTION_FILES or resolved.parent.name != "reports":
            continue
        if root not in resolved.parents:
            continue
        found.append({
            "ticker": resolved.parents[2].name,
            "date": resolved.parents[1].name,
            "file": resolved.name,
            "title": LIVE_SECTION_FILES[resolved.name],
            "path": str(resolved),
            "modified": resolved.stat().st_mtime,
        })
    found.sort(key=lambda item: item["modified"], reverse=True)
    return found[:limit]


def read_live_section(path: str) -> dict:
    """Read one CLI live section file, and nothing outside that tree."""
    candidate = _requested_path(path, "找不到这份分段文件。")
    root = Path(effective_results_dir()).resolve()
    if candidate.name not in LIVE_SECTION_FILES or candidate.parent.name != "reports":
        raise ConsoleError("这不是命令行会写的分段文件。", 404)
    if root not in candidate.parents or not _is_file(candidate):
        raise ConsoleError("找不到这份分段文件。", 404)
    return {
        "path": str(candidate),
        "file": candidate.name,
        "title": LIVE_SECTION_FILES[candidate.name],
        "text": candidate.read_text(encoding="utf-8", errors="replace"),
    }


def _header_models(text: str) -> dict:
    """``deep``/``quick`` model names from the report's ``- TradingAgents <ver>: ...`` line.

    reporting.model_line writes ``prov, deep A, quick B`` or ``deep prov A, quick prov B``;
    the model is the last word of each clause. A ``?`` (unknown) is left out.
    """
    found = {}
    for clause in text.split(":", 1)[-1].split(","):
        words = clause.split()
        if len(words) >= 2 and words[0] in ("deep", "quick") and words[-1] != "?":
            found[words[0]] = words[-1]
    return found


def _report_meta(markdown: Path) -> dict:
    """Ticker, analysis date, rating, generated time, models and debate rounds from the
    report header only (the lines before the first ``## `` section)."""
    meta: dict = {"ticker": None, "date": None, "rating": None, "generated": None,
                  "deep_model": None, "quick_model": None, "debate_rounds": None}
    try:
        with markdown.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index > 40 or line.startswith("## "):
                    break
                line = line.strip()
                if line.startswith("# ") and ":" in line:
                    meta["ticker"] = line.split(":", 1)[1].strip() or None
                for key, label in (("date", "- Analysis date:"), ("rating", "- Rating:"), ("generated", "- Generated:")):
                    if line.startswith(label):
                        meta[key] = line.split(":", 1)[1].strip() or None
                if line.startswith("- TradingAgents ") and ":" in line:
                    models = _header_models(line)
                    meta["deep_model"], meta["quick_model"] = models.get("deep"), models.get("quick")
                if line.startswith("- Analysts:"):
                    rounds = re.search(r"research debate rounds (\d+)", line)
                    meta["debate_rounds"] = int(rounds.group(1)) if rounds else None
    except OSError:
        pass
    return meta


_DECISION_READ_LIMIT = 16384


def _decision_paragraph(text: str) -> str:
    """A portfolio-manager paragraph, using the report page's 20-character threshold.

    A header rating is not this paragraph. Headings and a short label such as
    ``持有`` do not count.
    """
    for block in re.split(r"\n\s*\n", text or ""):
        cleaned = re.sub(r"^#+\s.*$", "", block, flags=re.MULTILINE)
        cleaned = re.sub(r"[*_`>#|]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) > 20:
            return cleaned
    return ""


def _portfolio_decision_text(report_dir: Path, base: Path) -> str:
    """Decision body that stays inside the results tree. Outside links are empty."""
    try:
        file = (report_dir / "5_portfolio" / "decision.md").resolve()
    except (OSError, RuntimeError):
        return ""
    if base not in file.parents or not file.is_file():
        return ""
    try:
        with file.open(encoding="utf-8", errors="replace") as handle:
            return handle.read(_DECISION_READ_LIMIT)
    except OSError:
        return ""


def list_reports(limit: int = 40) -> list[dict]:
    try:
        return _list_reports(limit)
    except OSError:      # W13
        raise ConsoleError(RESULTS_DIR_UNREADABLE, 409) from None


def _probe_readable(directory: Path) -> None:
    """Raise OSError when ``directory`` exists but cannot be listed (W91)."""
    with os.scandir(directory) as entries:
        next(entries, None)


def _list_reports(limit: int) -> list[dict]:
    root = Path(effective_results_dir()) / "reports"
    if not root.exists():
        return []
    _probe_readable(root)            # W91: glob() hides an unreadable directory
    base = root.resolve()
    found = []
    for page in root.glob("*/complete_report.html"):
        # QA W8: a run directory that is a symlink out of the results directory is not
        # a report here (its files would 404 through resolve_report anyway).
        try:
            real = page.resolve()
        except (OSError, RuntimeError):
            continue
        if base not in real.parents or not real.is_file():
            continue
        markdown = page.with_name("complete_report.md")
        has_markdown = markdown.exists() and base in markdown.resolve().parents
        meta = _report_meta(markdown) if has_markdown else {}
        name = page.parent.name
        ticker, _, stamp = name.rpartition("_")
        found.append({
            "name": name,
            "html": str(page),
            "markdown": str(markdown) if has_markdown else None,
            "modified": page.stat().st_mtime,
            "ticker": meta.get("ticker") or (ticker.rsplit("_", 1)[0] if ticker else name),
            "date": meta.get("date"),
            "rating": meta.get("rating"),
            "generated": meta.get("generated"),
            "deep_model": meta.get("deep_model"),
            "quick_model": meta.get("quick_model"),
            "debate_rounds": meta.get("debate_rounds"),
            "has_decision": bool(_decision_paragraph(_portfolio_decision_text(real.parent, base))),
        })
    found.sort(key=lambda item: item["modified"], reverse=True)
    return found[:limit]


def recent_tickers(limit: int = 6) -> list[dict]:
    """Tickers of the latest console analyses (newest first, each once) for the wizard chips.

    Read from the console's own job files; saved report names fill in when there are
    fewer jobs. Only the ticker and its asset type are returned.
    """
    seen: dict[str, dict] = {}
    logs = webui_home() / "logs"
    jobs = []
    if logs.is_dir():
        for path in logs.glob("*.job.json"):
            try:
                jobs.append((path.stat().st_mtime, path))
            except OSError:
                continue
    for _mtime, path in sorted(jobs, reverse=True):
        if len(seen) >= limit:
            break
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(job, dict) or job.get("kind", "analysis") != "analysis":
            continue
        selections = job.get("selections") if isinstance(job.get("selections"), dict) else {}
        ticker = str(selections.get("ticker") or "").strip().upper()
        if ticker and ticker not in seen:
            asset = selections.get("asset_type")
            seen[ticker] = {"ticker": ticker, "asset_type": asset if asset in ("stock", "crypto", "commodity") else None}
    if len(seen) < limit:
        for row in list_reports():
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker and ticker not in seen:
                seen[ticker] = {"ticker": ticker, "asset_type": None}
            if len(seen) >= limit:
                break
    return list(seen.values())[:limit]


def report_outline(path: str) -> dict:
    """The same section files ``--show`` displays, read from a saved report tree."""
    from tradingagents.reporting import _SECTIONS
    from webui.progress import TEAMS

    html = resolve_report(path)
    root = html.parent
    agent_zh = {
        agent: zh
        for _team, _team_zh, agents in TEAMS
        for agent, zh, _analyst in agents
    }
    from webui.catalog import report_sections
    section_zh = {item["cli"]: item["zh"] for item in report_sections()}
    sections = []
    for heading, folder, agents in _SECTIONS:
        items = []
        for agent, filename, _keys in agents:
            file = (root / folder / filename).resolve()
            if root not in file.parents or not file.is_file():
                continue
            items.append({
                "agent": agent,
                "zh": agent_zh.get(agent, agent),
                "file": filename,
                "text": file.read_text(encoding="utf-8", errors="replace"),
            })
        if items:
            sections.append({
                "heading": heading,
                "zh": section_zh.get(heading, heading),
                "folder": folder,
                "items": items,
            })
    rating = None
    header = []
    markdown = root / "complete_report.md"
    if markdown.is_file():
        for line in markdown.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("## "):
                break
            if line.startswith("# "):
                header.append(line[2:].strip())
                continue
            if not line.startswith("- "):
                continue
            header.append(line[2:].strip())
            if line.startswith("- Rating:"):
                rating = line.split(":", 1)[1].strip() or None
    from cli.display import COMPLETE_REPORT_RULE
    return {
        "name": root.name,
        "rating": rating,
        "header": header,
        "sections": sections,
        "shown": bool(sections),
        "rule": COMPLETE_REPORT_RULE,
    }


# Longer than any path or name the OS will look up (PATH_MAX, NAME_MAX on Linux and
# macOS): refused before touching the filesystem, never an Errno 36 echoed back.
MAX_REQUEST_PATH_BYTES = 4096
MAX_REQUEST_SEGMENT_BYTES = 255


RESULTS_DIR_INVALID = "结果目录无效：路径太长、某一级目录名超过 255 字节或含有空字符。请换一个结果目录。"
RESULTS_DIR_UNREADABLE = "结果目录无法读取（路径太长或无效）。请在设置里换一个结果目录。"


def _check_path_text(path, message: str = "路径无效。") -> None:
    """400 for a NUL byte, a path over 4096 bytes or a segment over 255 bytes (W15)."""
    if not isinstance(path, str) or "\x00" in path:
        raise ConsoleError(message, 400)
    encoded = path.encode("utf-8", "surrogatepass")
    if len(encoded) > MAX_REQUEST_PATH_BYTES or any(
            len(segment) > MAX_REQUEST_SEGMENT_BYTES for segment in encoded.split(b"/")):
        raise ConsoleError(message, 400)


def _requested_path(path: str, missing: str) -> Path:
    """A client-supplied path, resolved. A NUL byte or a path/segment longer than the
    OS allows is a bad request (400); any path the OS refuses to look up is the
    endpoint's 404. Never a 500 that echoes the OS error text or a server path."""
    _check_path_text(path)
    try:
        return Path(path).resolve()
    except (OSError, ValueError):
        raise ConsoleError(missing, 404) from None


def _is_file(candidate: Path) -> bool:
    """``is_file`` for a client-supplied path: an OS refusal (too long, loop, ...) is
    simply "not a file", so the caller answers its own 404."""
    try:
        return candidate.is_file()
    except (OSError, ValueError):
        return False


def resolve_report(path: str) -> Path:
    candidate = _requested_path(path, "找不到这份报告。")
    root = (Path(effective_results_dir()) / "reports").resolve()
    if root not in candidate.parents and candidate != root:
        raise ConsoleError("报告路径不在结果目录内。", 404)
    if not _is_file(candidate):
        raise ConsoleError("找不到这份报告。", 404)
    return candidate


Runner = Callable[[dict], int]
_runner: Runner | None = None
_spawn_argv: Callable[[dict], list[str]] | None = None


def set_runner(runner: Runner | None) -> None:
    global _runner
    _runner = runner


def set_spawn_argv(factory: Callable[[dict], list[str]] | None) -> None:
    global _spawn_argv
    _spawn_argv = factory


def seed_run_checkpoint(data_dir: str, job: dict) -> bool:
    """Give a run its starting checkpoint before it opens its own database (RUN-004).

    * 从断点重新启动 copies the failed run's database (``resume_from.scope``), or the
      per-ticker database when that run is from before RUN-004 (no scope).
    * A fresh run with checkpoints on copies a per-ticker database left by an older
      console or the CLI, so such a checkpoint still resumes as before; 启动前清除断点
      has already removed those.

    Sources are only read. Returns True when the per-ticker database was the source.
    """
    from tradingagents.graph.checkpointer import copy_checkpoint_db, db_file

    selections = job["selections"]
    ticker = selections["ticker"]
    source_scope = (job.get("resume_from") or {}).get("scope")
    if source_scope:
        copy_checkpoint_db(db_file(data_dir, ticker, source_scope), data_dir, ticker, job["checkpoint_scope"])
        return False
    return copy_checkpoint_db(db_file(data_dir, ticker), data_dir, ticker, job["checkpoint_scope"])


def default_runner(job: dict) -> int:
    from tradingagents.graph.checkpointer import clear_all_checkpoints
    from tradingagents.graph.trading_graph import TradingAgentsGraph
    from tradingagents.portfolio import PortfolioContext

    selections = job["selections"]
    config = build_run_config(selections)
    apply_console_call_bounds(config)
    config["checkpoint_enabled"] = console_checkpoint_enabled(selections)
    # Re-check in the worker: the user can leave the page open while the
    # retention job purges rows or while the cache directory is changed. A
    # missing attachment must fail visibly, never become an analysis with less
    # evidence than the user selected.
    _validate_info_crawl_attachment(selections, config)
    if selections.get("clear_checkpoints"):
        cleared = clear_all_checkpoints(config["data_cache_dir"])
        print(checkpoint_clear_sentence(cleared), flush=True)
    seeded_from_ticker_db = False
    if config["checkpoint_enabled"] and job.get("checkpoint_scope"):
        config["checkpoint_run_scope"] = job["checkpoint_scope"]
        seeded_from_ticker_db = seed_run_checkpoint(config["data_cache_dir"], job)
    portfolio = None
    if selections.get("portfolio"):
        portfolio = PortfolioContext.model_validate(selections["portfolio"])
    from cli.stats_handler import StatsCallbackHandler

    handler = StatsCallbackHandler()
    graph = TradingAgentsGraph(selections["analysts"], config=config, debug=False, callbacks=[handler])
    state, decision = _stream_like_cli(graph, selections, portfolio, handler)
    if seeded_from_ticker_db:
        # The run finished from a copy of the per-ticker checkpoint; drop that thread
        # too so a later run starts fresh, as it did before RUN-004.
        from tradingagents.graph.checkpointer import clear_checkpoint
        with contextlib.suppress(Exception):
            clear_checkpoint(config["data_cache_dir"], selections["ticker"], selections["analysis_date"],
                             graph._run_signature(selections.get("asset_type") or "stock", portfolio))
    notice = save_report_notice(graph, state, selections)
    result_path = Path(job["result_path"])
    from cli.run import MESSAGE_TOOL_LOG_NAME, _run_directory
    tool_log = _run_directory(config, selections["ticker"], selections["analysis_date"]) / MESSAGE_TOOL_LOG_NAME
    payload = analysis_result_payload(state, decision, notice, selections, tool_log)
    shown = payload.get("shown_report")
    if shown:
        print(shown["rule"], flush=True)
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def shown_report(final_state: dict) -> dict:
    """The sections ``display_complete_report`` prints for ``--show``.

    Empty sections stay out. The rule title is the CLI rule, not a new heading.
    """
    from cli.display import COMPLETE_REPORT_RULE
    from tradingagents.reporting import _report_parts

    sections = []
    for heading, _folder, items in _report_parts(final_state or {}):
        sections.append({
            "heading": heading,
            "items": [{"agent": agent, "text": text} for agent, _filename, text in items],
        })
    return {"rule": COMPLETE_REPORT_RULE, "sections": sections}


def analysis_result_payload(state, decision, notice: dict, selections: dict, tool_log: Path) -> dict:
    """The finished-run file. ``--show`` keeps the displayed sections even if nothing was saved."""
    payload = {
        "decision": decision,
        "report": notice["report"],
        "rating": state.get("final_rating") if isinstance(state, dict) else None,
        "decision_record": state.get("decision_record") if isinstance(state, dict) and isinstance(state.get("decision_record"), dict) else None,
        "saved_to": notice["saved_to"],
        "html": notice["html"],
        "save_error": notice["save_error"],
        "saved": notice["saved"],
        "save_lines": notice.get("save_lines") or [],
        "message_tool_log": str(tool_log) if tool_log.is_file() else None,
    }
    if selections.get("show") and isinstance(state, dict):
        payload["shown_report"] = shown_report(state)
    return payload


def save_report_notice(graph, state, selections: dict) -> dict:
    """Print the same save sentences as the CLI, without failing the analysis."""
    if not selections.get("save", True):
        return {"report": None, "saved_to": None, "html": None, "save_error": None, "saved": False}
    try:
        report_file = Path(graph.save_reports(
            state, selections["ticker"], selections.get("save_path"), html=selections.get("html", True),
        ))
    except Exception as exc:
        from cli.run import SAVE_ERROR_PREFIX
        line = f"{SAVE_ERROR_PREFIX} {exc}"
        print(line, flush=True)
        return {
            "report": None, "saved_to": None, "html": None, "save_error": str(exc),
            "saved": False, "save_lines": [line],
        }
    saved_to = str(report_file.parent.resolve())
    html = None
    html_name = None
    if selections.get("html", True):
        page = report_file.with_suffix(".html")
        if page.is_file():
            html = str(page)
            html_name = page.name
    lines = report_saved_lines(saved_to, report_file.name, html_name)
    for line in lines:
        print(line, flush=True)
    return {
        "report": str(report_file), "saved_to": saved_to, "html": html,
        "save_error": None, "saved": True, "save_lines": lines,
    }




def message_line(msg_type: str, content: str, timestamp: str | None = None) -> str:
    """A CLI message-panel row: type, 200-character content, and HH:MM:SS."""
    shown = " ".join(str(content).split())
    if len(shown) > 200:
        shown = shown[:197] + "..."
    stamp = timestamp or datetime.now().strftime("%H:%M:%S")
    return f"MESSAGE\t{msg_type}\t{shown}\t{stamp}"


def message_tool_message_line(timestamp: str, message_type: str, content: str) -> str:
    """One CLI message_tool.log row. Newlines become spaces; content is not truncated."""
    from cli.run import message_tool_message_line as format_line

    return format_line(timestamp, message_type, content)


def message_tool_call_line(timestamp: str, tool_name: str, args) -> str:
    """The CLI tool row: ``HH:MM:SS [Tool Call] name(k=v, ...)``."""
    from cli.run import message_tool_call_line as format_line

    return format_line(timestamp, tool_name, args)


def prepare_message_tool_log(config: dict, ticker: str, trade_date: str):
    """Create the same sidecar the CLI touches before streaming. None if the run has no results dir."""
    from cli.run import MESSAGE_TOOL_LOG_NAME, _run_directory

    if not config or not config.get("results_dir"):
        return None
    path = _run_directory(config, ticker, trade_date) / MESSAGE_TOOL_LOG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return path


def append_message_tool_log(path, line: str) -> None:
    if path is None:
        return
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(line)


def write_live_report_sections(report_dir, buffer, seen: dict[str, str]) -> None:
    """Write the same per-section files the CLI saves while a run is streaming."""
    if report_dir is None:
        return
    for name, content in buffer.report_sections.items():
        if not content or seen.get(name) == content:
            continue
        text_value = "\n".join(str(item) for item in content) if isinstance(content, list) else str(content)
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{name}.md").write_text(text_value, encoding="utf-8")
        seen[name] = content


def read_message_tool_log(path: str, limit: int = 80) -> dict:
    """Read a CLI message_tool.log that stays under the results directory."""
    candidate = _requested_path(path, "日志路径不在结果目录内。")
    root = Path(effective_results_dir()).resolve()
    from cli.run import MESSAGE_TOOL_LOG_NAME
    if candidate.name != MESSAGE_TOOL_LOG_NAME or root not in candidate.parents:
        raise ConsoleError("日志路径不在结果目录内。", 404)
    if not _is_file(candidate):
        raise ConsoleError(f"还没有 {MESSAGE_TOOL_LOG_NAME}。", 404)
    lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    return {"path": str(candidate), "lines": lines[-limit:], "cli": MESSAGE_TOOL_LOG_NAME}


def _emit_messages(buffer, messages, log_path=None) -> None:
    from cli.display import format_tool_args, record_stream_messages

    before = len(buffer.tool_calls)
    added = record_stream_messages(buffer, messages)
    new_messages = list(buffer.messages)[-len(added):] if added else []
    for timestamp, msg_type, content in new_messages:
        print(message_line(msg_type, content, timestamp), flush=True)
        append_message_tool_log(log_path, message_tool_message_line(timestamp, msg_type, content))
    for timestamp, name, args in list(buffer.tool_calls)[before:]:
        if name:
            print(f"TOOL\t{name}\t{format_tool_args(args)}\t{timestamp}", flush=True)
            append_message_tool_log(log_path, message_tool_call_line(timestamp, name, args))


def _emit_tools(messages) -> None:
    from cli.display import format_tool_args

    for message in messages or []:
        calls = getattr(message, "tool_calls", None) or []
        for tool_call in calls:
            if isinstance(tool_call, dict):
                name, args = tool_call.get("name"), tool_call.get("args")
            else:
                name, args = getattr(tool_call, "name", None), getattr(tool_call, "args", None)
            if name:
                print(f"TOOL\t{name}\t{format_tool_args(args)}", flush=True)



def _emit_report(buffer, seen: dict[str, str]) -> None:
    """Print the CLI Current Report body when it changes."""
    from webui.progress import format_report_line

    report = buffer.current_report
    if not report or seen.get("__report") == report:
        return
    print(format_report_line(report), flush=True)
    seen["__report"] = report


def _emit_status(buffer, seen: dict[str, str]) -> None:
    for agent, status in buffer.agent_status.items():
        if seen.get(agent) == status:
            continue
        # The clock stamp feeds the status page timeline and role times (WEBUI-005).
        print(f"STATUS\t{agent}\t{status}\t{datetime.now().strftime('%H:%M:%S')}", flush=True)
        seen[agent] = status


_TASK_NOTE = re.compile(r"During task with name '([^']+)'")


def failed_role(exc: BaseException, agent_status: dict[str, str]) -> str | None:
    """The progress role a stream failure belongs to.

    LangGraph notes the node that raised; node names are the CLI agent names
    for every model-calling role. Otherwise the role still in progress.
    """
    item, seen = exc, set()
    while item is not None and id(item) not in seen:
        seen.add(id(item))
        for note in getattr(item, "__notes__", None) or ():
            match = _TASK_NOTE.search(str(note))
            if match and match.group(1) in agent_status:
                return match.group(1)
        item = item.__cause__ or item.__context__
    running = [agent for agent, status in agent_status.items() if status == "in_progress"]
    return running[0] if running else None


def _mark_failure(exc: BaseException, role: str | None, checkpoint: dict) -> None:
    from webui.run_failure import CHECKPOINT_ATTRIBUTE, ROLE_ATTRIBUTE

    with contextlib.suppress(Exception):
        if role is not None:
            setattr(exc, ROLE_ATTRIBUTE, role)
        setattr(exc, CHECKPOINT_ATTRIBUTE, checkpoint)


def _checkpoint_saved(graph, selections: dict, portfolio) -> bool:
    """Whether a resumable step exists for this run's signature."""
    try:
        from tradingagents.graph.checkpointer import checkpoint_step

        return checkpoint_step(
            graph.config["data_cache_dir"], selections["ticker"], selections["analysis_date"],
            graph._run_signature(selections.get("asset_type") or "stock", portfolio),
            graph.config.get("checkpoint_run_scope") or None,
        ) is not None
    except Exception:  # noqa: BLE001 - a failure summary must not raise
        return False


def _stream_like_cli(graph, selections: dict, portfolio, stats_handler=None):
    """Stream one run and print the same agent statuses the CLI live view tracks."""
    import time

    from cli.display import (
        ANALYST_ORDER,
        AnalystWallTimeTracker,
        MessageBuffer,
        apply_stream_chunk,
        format_footer_stats,
    )
    from tradingagents.agents.rating import run_rating
    from tradingagents.graph.analyst_execution import build_analyst_execution_plan

    selected = [key for key in ANALYST_ORDER if key in set(selections["analysts"])]
    tracker = AnalystWallTimeTracker(build_analyst_execution_plan(selected))
    buffer = MessageBuffer()
    buffer.init_for_analysis(selections["analysts"])
    seen: dict[str, str] = {}
    started = time.time()
    log_path = prepare_message_tool_log(
        getattr(graph, "config", None), selections["ticker"], selections["analysis_date"],
    )
    section_dir = None if log_path is None else log_path.parent / "reports"
    section_seen: dict[str, str] = {}

    def say_system(content: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        print(message_line("System", content, stamp), flush=True)
        append_message_tool_log(log_path, message_tool_message_line(stamp, "System", content))

    for spec in tracker.plan.specs:
        tracker.mark_started(spec.key)
    from cli.run import (
        analysis_date_message,
        checkpoint_fresh_message,
        checkpoint_resume_message,
        completed_analysis_message,
        selected_ticker_message,
    )
    from cli.selections import detected_asset_sentence, selected_analysts_sentence
    for line in run_preamble_lines(selections):
        print(line, flush=True)
    say_system(selected_ticker_message(selections["ticker"]))
    if selections.get("asset_type") not in (None, "", "stock"):
        say_system(detected_asset_sentence(selections["asset_type"]))
    say_system(analysis_date_message(selections["analysis_date"]))
    say_system(selected_analysts_sentence(selections["analysts"]))
    attach_line = info_attach_run_line(getattr(graph, "config", None) or {})
    if attach_line:
        say_system(attach_line)
    _emit_status(buffer, seen)
    init_state = graph.create_run_state(
        selections["ticker"], selections["analysis_date"], selections["asset_type"], portfolio,
    )
    args = graph.propagator.get_graph_args(callbacks=[stats_handler] if stats_handler else None)
    thread_id = graph.begin_checkpoint(
        selections["ticker"], selections["analysis_date"], selections["asset_type"], portfolio,
    )
    if thread_id is not None:
        args.setdefault("config", {}).setdefault("configurable", {})["thread_id"] = thread_id
        announcement = (
            checkpoint_resume_message(selections["ticker"], selections["analysis_date"])
            if getattr(graph, "_resuming", False)
            else checkpoint_fresh_message(selections["ticker"], selections["analysis_date"])
        )
        say_system(announcement)
    final_state: dict = {}
    try:
        for messages, chunk in graph.stream_run(graph.checkpoint_input(init_state), **args):
            _emit_messages(buffer, messages, log_path)
            if not chunk:
                continue
            apply_stream_chunk(buffer, chunk, tracker)
            write_live_report_sections(section_dir, buffer, section_seen)
            final_state.update(chunk)
            _emit_status(buffer, seen)
            _emit_report(buffer, seen)
        graph.record_decision(selections["ticker"], selections["analysis_date"], final_state)
        graph.clear_checkpoint_on_success(
            selections["ticker"], selections["analysis_date"], selections["asset_type"], portfolio,
        )
    except Exception as exc:
        # Name the role that was running and whether a checkpoint survives, so
        # the worker's failure file and the status page can say so (RUN-002).
        role = failed_role(exc, buffer.agent_status)
        if role is not None:
            buffer.update_agent_status(role, "error")
            _emit_status(buffer, seen)
        _mark_failure(exc, role, {
            "enabled": thread_id is not None,
            "saved": thread_id is not None and _checkpoint_saved(graph, selections, portfolio),
        })
        raise
    finally:
        graph.end_checkpoint()
    for agent in buffer.agent_status:
        buffer.update_agent_status(agent, "completed")
    _emit_status(buffer, seen)
    say_system(completed_analysis_message(selections["analysis_date"]))
    summary = tracker.format_summary()
    say_system(summary)
    # The CLI overwrites streamed debate text with the raw final section before
    # the footer is drawn. Empty values are left untouched, matching its writer.
    for section in list(buffer.report_sections):
        if section in final_state:
            buffer.update_report_section(section, final_state[section])
    write_live_report_sections(section_dir, buffer, section_seen)
    _emit_report(buffer, seen)
    _emit_status(buffer, seen)
    print("STATS\t" + format_footer_stats(buffer, stats_handler, started), flush=True)
    print(summary, flush=True)
    rating = run_rating(final_state)
    from webui.progress import review_notice
    notice = review_notice(rating)
    if notice:
        print("REVIEW\t" + notice, flush=True)
    from webui.progress import COMPLETION_SENTENCE
    print(COMPLETION_SENTENCE, flush=True)
    return final_state, rating


def _job_paths(run_id: str) -> tuple[Path, Path, Path]:
    log_path = webui_home() / "logs" / f"{run_id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    return log_path, webui_home() / "logs" / f"{run_id}.json", webui_home() / "logs" / f"{run_id}.job.json"


def _record_for(run_id: str, selections: dict, log_path: Path, result_path: Path, *, kind: str,
                framework: str = DEFAULT_FRAMEWORK) -> dict:
    return {
        "id": run_id,
        "kind": kind,
        "framework": framework,
        "status": "running",
        "ticker": selections.get("ticker"),
        "date": selections.get("analysis_date"),
        "selections": selections,
        "log": str(log_path),
        "result": str(result_path),
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "pid": None,
        "returncode": None,
    }


def could_not_start_console_sentence(record: dict | None, exc: BaseException) -> str:
    """The console could not spawn. The CLI sentence stays; the kind must not be guessed."""
    from cli.launcher import could_not_start_sentence
    kind = (record or {}).get("kind")
    if kind == "backtest":
        head = "控制台没能启动回测。"
    elif kind == "analysis":
        head = "控制台没能启动分析。"
    else:
        head = "控制台没能启动这个任务。不把它写成分析。"
    return head + "对应命令行：" + could_not_start_sentence(exc)


def background_start_sentence(log_path: Path | str) -> str:
    """The launcher sentence after a background start succeeds."""
    from cli.launcher import background_started_sentence
    return background_started_sentence(log_path)


def background_failed_zh(record: dict | None) -> str:
    """The worker exited before it was running. A backtest is not an analysis."""
    kind = (record or {}).get("kind")
    if kind == "backtest":
        return "后台启动失败。进程启动不等于回测完成。"
    if kind == "analysis":
        return "后台启动失败。进程启动不等于分析完成。"
    return "后台启动失败。进程启动不等于完成。不把它写成分析完成。"


def background_started_zh(record: dict | None) -> str:
    """A spawned process is running. That is not completion, and a backtest is not an analysis."""
    kind = (record or {}).get("kind")
    if kind == "backtest":
        return "已在后台启动。进程启动不等于回测完成。"
    if kind == "analysis":
        return "已在后台启动。进程启动不等于分析完成。"
    return "已在后台启动。进程启动不等于完成。不把它写成分析完成。"


def _mark_background_start(record: dict, log_path: Path) -> dict:
    record["log"] = str(log_path)
    record["cli"] = background_start_sentence(log_path)
    base = background_started_zh(record)
    notices = _info_crawl_notices(record.get("selections") or {})
    record["zh"] = " ".join([*notices, base]) if notices else base
    return record


def _spawn(job: dict, record: dict, log_path: Path) -> dict:
    argv = _spawn_argv(job) if _spawn_argv else [sys.executable, "-m", "webui.worker", job["job_path"]]
    record["argv"] = list(argv)
    log = log_path.open("a", encoding="utf-8")
    try:
        try:
            process = subprocess.Popen(
                argv,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            record["status"] = "failed"
            record["error"] = str(exc)
            JOBS.write_run(record)
            raise ConsoleError(could_not_start_console_sentence(record, exc)) from exc
    finally:
        log.close()
    record["pid"] = process.pid
    record["proc_start"] = _process_start(process.pid)
    time.sleep(0.05)
    if process.poll() is not None:
        record["status"] = "failed"
        record["returncode"] = process.returncode
        from cli.launcher import background_failed_sentence
        record["cli"] = background_failed_sentence(log_path)
        record["zh"] = background_failed_zh(record)
        JOBS.write_run(record)
        raise ConsoleError(
            "后台启动失败。对应命令行：" + background_failed_sentence(log_path)
        )
    record["status"] = "running"
    _mark_background_start(record, log_path)
    JOBS.write_run(record)
    return redact(record)


RUN_LOCK = threading.RLock()


def _status_counts(records: list[dict]) -> dict[str, int]:
    counts = {"running": 0, "starting": 0, "stopping": 0}
    for record in records:
        status = record.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _occupied_phrase(running: int, starting: int = 0, stopping: int = 0) -> str:
    """Say which live tasks occupy slots. Stopping is not running."""
    parts = []
    if running:
        parts.append(f"{running} 个任务运行中")
    if starting:
        parts.append(f"{starting} 个启动中")
    if stopping:
        parts.append(f"{stopping} 个停止中")
    if not parts:
        parts.append(f"{running} 个任务运行中")
    return "，".join(parts)


def queue_full_sentence(limits: dict, running: int | None = None, queued: int | None = None, *,
                        starting: int = 0, stopping: int = 0) -> str:
    running = limits["max_running"] if running is None else running
    queued = limits["max_queued"] if queued is None else queued
    occupied = _occupied_phrase(running, starting, stopping)
    return (f"队列已满：{occupied}，{queued} 个在排队"
            f"（最多同时运行 {limits['max_running']} 个，最多排队 {limits['max_queued']} 个）。"
            "等排队的任务开始后再启动，也可以在查看状态里取消一个排队的任务。")


def _live_runs(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("status") in LIVE_STATUSES]


def _slots_used(records: list[dict], limits: dict) -> int:
    """Every live run takes one slot; a backtest no longer takes all of them (W44)."""
    return len(_live_runs(records))


def _refresh_all() -> list[dict]:
    records = JOBS.list_runs()
    for record in records:
        if record.get("status") in LIVE_STATUSES:
            with contextlib.suppress(Exception):
                refresh_run_process(record)
    return order_runs(records)


def start_validated_run(selections: dict, *, background: bool = True, resume_from: dict | None = None,
                        notices: list[str] | None = None, framework: str = DEFAULT_FRAMEWORK) -> dict:
    """Start an analysis now, or queue it when every run slot is busy (RUN-004).

    At most ``max_running`` analyses run at once; later ones wait as 排队中 and
    start in arrival order when a run ends. When ``max_queued`` already wait,
    the start is refused with :func:`queue_full_sentence`.
    """
    # Callers normally pass the already validated selections. Do not reapply
    # the full environment overlay here: restart/direct callers may carry an
    # explicit, validated cache/results path that must remain part of the run.
    selections = dict(selections)
    run_notices = list(notices or [])
    for notice in _info_crawl_notices(selections):
        if notice not in run_notices:
            run_notices.append(notice)
    with RUN_LOCK:
        limits = run_limits()
        dispatch_queue()  # a slot that just freed goes to the queue first, never to this start
        records = _refresh_all()
        queued = [r for r in records if r.get("status") == "queued"]
        busy = _slots_used(records, limits) >= limits["max_running"] or bool(queued)
        if busy and len(queued) >= limits["max_queued"]:
            live = _status_counts(records)
            raise ConsoleError(queue_full_sentence(
                limits, live["running"], len(queued), starting=live["starting"], stopping=live["stopping"]
            ), 409)
        if _resume_source_missing(selections, resume_from):
            raise ConsoleError(RESUME_SOURCE_GONE, 409)
        _validate_info_crawl_attachment(selections)
        run_id = new_run_id()
        log_path, result_path, job_path = _job_paths(run_id)
        framework = launch_framework({"framework": framework})  # a caller cannot start an unregistered framework
        record = _record_for(run_id, selections, log_path, result_path, kind="analysis", framework=framework)
        from webui.compare import freeze_prompt_versions
        from webui.cost import freeze_role_models
        freeze_role_models(record, selections)
        freeze_prompt_versions(record)
        record["checkpoint"] = {"enabled": console_checkpoint_enabled(selections), "saved": None, "scope": run_id}
        if run_notices:
            record["notices"] = list(run_notices)
            info_notices = _info_crawl_notices(selections)
            if info_notices:
                record["detail"] = " ".join(info_notices)
        job = {
            "id": run_id,
            "kind": "analysis",
            "framework": framework,
            "selections": selections,
            "result_path": str(result_path),
            "log": str(log_path),
            "job_path": str(job_path),
            "checkpoint_scope": run_id,
        }
        if resume_from:
            job["resume_from"] = {"run": resume_from.get("run"), "scope": resume_from.get("scope")}
            record["resumed_from"] = resume_from.get("run")
        job_path.write_text(json.dumps(redact(job), ensure_ascii=False), encoding="utf-8")
        remember_run(selections)
        if busy:
            record["status"] = "queued"
            record["started_at"] = None
            record["queued_at"] = datetime.now().isoformat(timespec="seconds")
            record["queue_seq"] = time.time_ns()
            record["queue_ahead"] = len(queued)
            queue_sentence = f"已加入队列，前面还有 {len(queued)} 个。有任务结束后会自动开始。"
            info_notices = _info_crawl_notices(selections)
            record["detail"] = record["zh"] = " ".join([*info_notices, queue_sentence]) if info_notices else queue_sentence
            JOBS.write_run(record)
            answer = redact(record)
            answer["limits"] = limits
            return answer
        record["start_seq"] = time.time_ns()
        JOBS.write_run(record)
        return _launch(job, record, log_path, background=background)


RESUME_SOURCE_GONE = ("要续跑的断点已经被清除（「立即清除断点」或「启动前清除断点」），这个任务没有启动，"
                      "以免悄悄从头开始。需要的话请用「重新启动」从头运行。")


def _resume_source_missing(selections: dict, resume_from: dict | None) -> bool:
    """A 从断点重新启动 run whose source checkpoint is gone must not start from scratch (W37)."""
    if not resume_from:
        return False
    from tradingagents.graph.checkpointer import db_file
    try:
        source = db_file(_selections_cache_dir(selections), selections["ticker"], resume_from.get("scope"))
        return not (source.is_file() and source.stat().st_size > 0)
    except Exception:  # noqa: BLE001 - an unreadable source is a missing one
        return True


def dispatch_queue() -> list[dict]:
    """Start queued runs, oldest first, while run slots are free. Returns the runs started."""
    started = []
    with RUN_LOCK:
        limits = run_limits()
        records = _refresh_all()
        for record in [r for r in records if r.get("status") == "queued"]:
            if _slots_used(records, limits) >= limits["max_running"]:
                break
            job_path = Path(record.get("result") or "").with_name(f"{record['id']}.job.json")
            try:
                job = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record["status"] = "failed"
                record["detail"] = record["zh"] = "排队的任务参数文件不见了，没有启动。"
                record["finished_at"] = datetime.now().isoformat(timespec="seconds")
                JOBS.write_run(record)
                continue
            if _resume_source_missing(job.get("selections") or record.get("selections") or {}, job.get("resume_from")):
                record["status"] = "failed"
                record["resume_source_gone"] = True
                record["detail"] = record["zh"] = RESUME_SOURCE_GONE
                record["finished_at"] = datetime.now().isoformat(timespec="seconds")
                JOBS.write_run(record)
                continue
            record["status"] = "running"
            record["started_at"] = datetime.now().isoformat(timespec="seconds")
            record["start_seq"] = time.time_ns()
            for key in ("queue_ahead", "detail", "zh"):
                record.pop(key, None)
            JOBS.write_run(record)
            records = order_runs([r for r in records if r.get("id") != record["id"]] + [record])
            with contextlib.suppress(ConsoleError):
                started.append(_launch(job, record, Path(record["log"]), background=True))
        maybe_prune_run_checkpoints()
    return started


def recover_runs_on_start() -> dict:
    """What a console restart does with the runs it finds (RUN-004).

    * A running run whose process is still alive keeps running (the worker is in
      its own session and outlives the console) and keeps its slot.
    * A running run whose process is gone becomes 失败 with the reason, and offers
      从断点重新启动 when its own checkpoint database has saved steps.
    * A run that was still starting (no process yet) becomes 失败: it never started.
    * Queued runs stay queued in their original order; :func:`dispatch_queue`
      starts them as slots free up.
    """
    interrupted = []
    with RUN_LOCK:
        for record in JOBS.list_runs():
            if record.get("status") not in LIVE_STATUSES:
                continue
            pid = record.get("pid")
            state = _worker_state(record) if isinstance(pid, int) else "gone"
            if state in ("ours", "unknown"):  # W47: keeps running and keeps its slot
                continue
            raw_result = record.get("result")
            result = Path(raw_result) if isinstance(raw_result, str) and raw_result else None
            if result is not None and result.is_file():
                # Completed before the pid was reused or the process disappeared. Not an interruption.
                _mark_result_finished(record)
                record["finished_at"] = datetime.now().isoformat(timespec="seconds")
                JOBS.write_run(record)
                continue
            if state == "foreign":
                _mark_pid_reused(record)
                record["finished_at"] = datetime.now().isoformat(timespec="seconds")
                JOBS.write_run(record)
                interrupted.append(record["id"])
                continue
            record["status"] = "failed"
            record["interrupted"] = True
            record["detail"] = record["zh"] = (
                "控制台重启时这个任务的进程已经不在，没有写出结果。" if isinstance(pid, int)
                else "控制台重启时这个任务还没启动完成，没有运行。")
            failure = None
            with contextlib.suppress(Exception):
                failure = _read_run_failure(result)
            if failure is not None:
                with contextlib.suppress(Exception):
                    attach_failure(record, failure)
            if isinstance(pid, int):
                _note_saved_checkpoint(record)
            record["finished_at"] = datetime.now().isoformat(timespec="seconds")
            JOBS.write_run(record)
            interrupted.append(record["id"])
    started = dispatch_queue()
    with contextlib.suppress(Exception):   # housekeeping never blocks a console start
        prune_run_checkpoints()
    return {"interrupted": interrupted, "started": [r.get("id") for r in started]}


# W45: per-run checkpoint databases (checkpoints/runs/<scope>/) are cleaned up
# automatically by this rule, for the runs this console home has records of:
#   * a run that finished (完成) no longer needs its checkpoint: removed;
#   * a failed / stopped / cancelled / interrupted run keeps it for
#     CHECKPOINT_KEEP_DAYS after it ended, so 从断点重新启动 still works; then removed;
#   * a running or queued run's own scope, and the scope a queued or running
#     resume copies from, are never removed;
#   * a scope shared by several records (one run_id reused) is kept while ANY of
#     them still needs it (MF-1);
#   * a scope no record of this home names is left alone: the cache directory can
#     be shared with another console home or the CLI. 「立即清除断点」 removes those.
# A run's scopes are its id and the checkpoint_scope in its job file (a named
# backtest uses backtest-<name>). The rule runs when the console starts and then
# at most every CHECKPOINT_PRUNE_EVERY seconds from the queue dispatcher.
CHECKPOINT_KEEP_DAYS = 7
CHECKPOINT_PRUNE_EVERY = 600.0
_last_checkpoint_prune = 0.0


def _ended_at(record: dict) -> float | None:
    try:
        return datetime.fromisoformat(str(record.get("finished_at"))).timestamp()
    except (TypeError, ValueError):
        return None


def _record_job(record: dict) -> dict:
    job = _read_record_job(record)
    return job if isinstance(job, dict) else {}


def _read_record_job(record: dict) -> dict | None:
    """The record's job file: {} when there is none, None when it exists but cannot be read (W80)."""
    try:
        job_path = Path(str(record["result"])).with_name(f"{record.get('id')}.job.json")
    except (KeyError, TypeError):
        return {}
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return job if isinstance(job, dict) else None


def _unreadable_run_ids() -> set[str]:
    """Run files list_runs skips because they do not parse (W80): their ids."""
    folder = JOBS.runs_dir()
    found = set()
    if folder.is_dir():
        for path in folder.glob("*.json"):
            if path.name.endswith(".job.json"):      # job files: _read_record_job judges those
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, ValueError):
                found.add(path.stem)
                continue
            if not isinstance(data, dict):
                found.add(path.stem)
    return found


def _record_scopes(record: dict, job: dict) -> set[str]:
    scopes = {str(record.get("id"))}
    if job.get("checkpoint_scope"):
        scopes.add(str(job["checkpoint_scope"]))
    return scopes


def prune_run_checkpoints(now: float | None = None) -> list[str]:
    """Apply the W45 rule above. Returns the scope directories removed."""
    import shutil

    from tradingagents.graph.checkpointer import scope_dir

    global _last_checkpoint_prune
    now = time.time() if now is None else now
    _last_checkpoint_prune = now
    keep_for = CHECKPOINT_KEEP_DAYS * 86400
    records = JOBS.list_runs()
    raw_jobs = {str(r.get("id")): _read_record_job(r) for r in records}
    jobs = {key: job if isinstance(job, dict) else {} for key, job in raw_jobs.items()}
    # W80: a record or job file that cannot be read may name a scope another record
    # shares (backtest-<name>). Keep its own id, and while any such file exists keep
    # every shared job scope too: a lost checkpoint costs paid calls, disk does not.
    unreadable = _unreadable_run_ids()
    protected: set[str] = set(unreadable)
    if unreadable or any(job is None for job in raw_jobs.values()):
        for job in jobs.values():
            if job.get("checkpoint_scope"):
                protected.add(str(job["checkpoint_scope"]))
    for record in records:
        if record.get("status") in (*LIVE_STATUSES, "queued"):
            job = jobs[str(record.get("id"))]
            protected |= _record_scopes(record, job)
            source = (job.get("resume_from") or {}).get("scope")
            if source:
                protected.add(str(source))
        elif record.get("status") != "finished":
            # MF-1: a scope shared by several records (same run_id) stays while ANY
            # of them is still inside its retention window.
            ended = _ended_at(record)
            if ended is None or now - ended <= keep_for:
                protected |= _record_scopes(record, jobs[str(record.get("id"))])
    removed = []
    for record in records:
        status = record.get("status")
        if status in (*LIVE_STATUSES, "queued"):
            continue
        # An unfinished record still inside its window protected its scopes above.
        selections = record.get("selections") if isinstance(record.get("selections"), dict) else {}
        root = _selections_cache_dir(selections) if selections else effective_cache_dir()
        for scope in sorted(_record_scopes(record, jobs[str(record.get("id"))]) - protected):
            try:
                directory = scope_dir(root, scope)
            except ValueError:
                continue
            if directory.is_dir() and not directory.is_symlink():
                shutil.rmtree(directory, ignore_errors=True)
                if not directory.exists():
                    removed.append(str(directory))
    return removed


def maybe_prune_run_checkpoints(now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    if now - _last_checkpoint_prune < CHECKPOINT_PRUNE_EVERY:
        return []
    with contextlib.suppress(Exception):
        return prune_run_checkpoints(now)
    return []


_SUMMARY_CACHE: dict[str, tuple] = {}


def _progress_counts(record: dict) -> dict:
    """done/total roles and the team working now, for one row of the run list."""
    from webui.progress import progress_rows
    selected = (record.get("selections") or {}).get("analysts")
    status = record.get("status")
    if status == "queued":
        return {"done": 0, "total": len(progress_rows([], selected)), "team_zh": "等待开始", "blank": False}
    log = Path(record.get("log") or "")
    try:
        stat = log.stat()
        key = (stat.st_mtime_ns, stat.st_size, status)
    except OSError:
        key = (None, None, status)
    cached = _SUMMARY_CACHE.get(str(record.get("id")))
    if cached and cached[0] == key:
        return cached[1]
    lines = (read_log(5000, record).get("lines") or []) if key[0] is not None else []
    rows = progress_rows(lines, selected)
    failure = record.get("failure") if isinstance(record.get("failure"), dict) else {}
    failed_role = failure.get("role") if status == "failed" else None
    # Same count as the detail view, where the failed role shows as 错误 (run_progress).
    done = sum(1 for row in rows if row["status"] == "completed" and row["agent"] != failed_role)
    working = next((row for row in rows if row["status"] == "in_progress"), None)
    if status == "failed":
        failed_row = next((row for row in rows if row["agent"] == failure.get("role")), None)
        team = (failed_row or working or {}).get("team_zh") or "没有定位到团队"
    elif status == "finished":
        team = "进程已结束"
    elif status in ("stopped", "cancelled"):
        team = "已停止" if status == "stopped" else "已取消"
    elif status == "idle" or status not in {"starting", "running", "stopping", "completed"}:
        team = "不写成准备中"
    else:
        team = (working or {}).get("team_zh") or ("准备中" if done == 0 else next(
            (row["team_zh"] for row in rows if row["status"] == "pending"), "收尾中"))
    ended = status in {"finished", "failed", "stopped", "cancelled", "completed"}
    recorded = any(row.get("status") not in (None, "", "pending") for row in rows)
    known = {"starting", "running", "stopping", "completed", "finished", "failed", "stopped", "queued", "cancelled", "idle"}
    blank = (status == "idle" or status not in known) and not recorded
    answer = {"done": done, "total": len(rows), "team_zh": team, "unrecorded": ended and not recorded, "blank": blank}
    _SUMMARY_CACHE[str(record.get("id"))] = (key, answer)
    return answer


def run_summary(record: dict, position: int | None = None) -> dict:
    """One row of the status page's run list."""
    summary = {key: record.get(key) for key in (
        "id", "kind", "status", "ticker", "date", "started_at", "queued_at", "finished_at", "interrupted")}
    summary["position"] = position
    summary.update(framework_fields(record))
    with contextlib.suppress(Exception):
        summary.update(_progress_counts(record))
    summary.setdefault("done", 0)
    summary.setdefault("total", 0)
    summary.setdefault("team_zh", "")
    return summary


def api_runs(run_id: str | None = None) -> dict:
    """GET /api/runs: every run (running first) plus the selected run's detail."""
    with contextlib.suppress(Exception):
        dispatch_queue()
    records = JOBS.list_runs()
    for record in records:
        refresh_run_process(record)
    records = order_runs(records)
    selected = None
    asked = run_id not in (None, "")
    if asked and RUN_ID_PATTERN.fullmatch(str(run_id)):
        selected = next((r for r in records if r.get("id") == run_id), None) or JOBS.read_run(run_id)
    if asked and selected is None:
        # W65: an unknown ?id= is a clear not-found, never the newest run's detail.
        return _runs_not_found(records)
    if selected is None and records:
        selected = records[0]
    positions, summaries, counts, listing = _run_listing(records)
    sel_id = selected.get("id") if selected else None
    if selected is not None and sel_id in positions:
        selected = {**selected, "position": positions[sel_id]}
    if selected is not None:
        selected = {**selected, **framework_fields(selected)}
    return {
        "runs": summaries,
        "counts": counts,
        "listing": listing,
        "limits": run_limits(),
        "selected": sel_id,
        "run": selected,
        "progress": run_progress(selected) if selected else run_progress(),
        "result": read_result(selected) if selected else read_result(),
        "status_sentence": console_status_sentence(selected),
        "argv_line": status_argv_line_for(selected),
    }


RECENT_RUN_LIMIT = 30


def _run_listing(records: list[dict]):
    """W43: counts and queue positions over every run; the list shows the newest
    RECENT_RUN_LIMIT (running and queued first), and ``listing`` says how many
    were left out so the page never presents a truncated list as complete."""
    positions = {r["id"]: i + 1 for i, r in enumerate(r for r in records if r.get("status") == "queued")}
    every = [run_summary(r, positions.get(r.get("id"))) for r in records]
    counts = {
        "all": len(every),
        "running": sum(1 for r in every if r["status"] == "running"),
        "starting": sum(1 for r in every if r["status"] == "starting"),
        "stopping": sum(1 for r in every if r["status"] == "stopping"),
        "queued": sum(1 for r in every if r["status"] == "queued"),
        "failed": sum(1 for r in every if r["status"] == "failed"),
        "finished": sum(1 for r in every if r["status"] == "finished"),
    }
    listing = {"shown": min(len(every), RECENT_RUN_LIMIT), "total": len(every),
               "limit": RECENT_RUN_LIMIT, "truncated": len(every) > RECENT_RUN_LIMIT}
    return positions, every[:RECENT_RUN_LIMIT], counts, listing
RUN_NOT_FOUND_SENTENCE = "没有这个任务。"


def _runs_not_found(records: list[dict]) -> dict:
    """/api/runs for an id that names no run (W65): the list, no run detail."""
    positions, summaries, counts, listing = _run_listing(records)
    return {
        "runs": summaries,
        "counts": counts,
        "listing": listing,
        "limits": run_limits(),
        "selected": None,
        "run": None,
        "not_found": True,
        "error": RUN_NOT_FOUND_SENTENCE,
        "progress": None,
        "result": {"present": False, "note": RUN_NOT_FOUND_SENTENCE},
        "status_sentence": console_status_sentence(None),
        "argv_line": None,
    }


def _launch(job: dict, record: dict, log_path: Path, *, background: bool) -> dict:
    if _runner is None:
        return _spawn(job, record, log_path)
    runner = _runner
    run_id = record["id"]

    def _execute() -> None:
        try:
            code = runner(job)
            record["status"] = "finished" if code == 0 else "failed"
            record["returncode"] = code
        except Exception as exc:
            from webui.run_failure import ERROR_LIMIT, build_failure, redact_text, safe_text
            record["status"] = "failed"
            record["error"] = redact_text(safe_text(str(exc)), ERROR_LIMIT)
            attach_failure(record, build_failure(exc, job))
            log_path.write_text(record["error"], encoding="utf-8")
        finally:
            record["finished_at"] = datetime.now().isoformat(timespec="seconds")
            JOBS.write_run(record)
        dispatch_queue()

    if background:
        threading.Thread(target=_execute, name=f"webui-{run_id}", daemon=True).start()
        _mark_background_start(record, log_path)
    else:
        _execute()
    return redact(record)



def backtest_probe_date(dates: list[str]) -> str:
    """A date validate_analysis can parse. An empty grid has no cell date."""
    if dates:
        return dates[-1]
    from tradingagents.dataflows.date_window import get_current_date
    return get_current_date()


def backtest_span(dates: list[str], payload: dict) -> str:
    """The record label for a grid. Empty dates keep the requested bounds."""
    if dates:
        return f"{dates[0]}..{dates[-1]}"
    return f"{payload.get('start') or ''}..{payload.get('end') or ''}"


def start_backtest(payload: dict) -> dict:
    """Start a backtest in one free run slot (W44).

    A backtest takes one slot like an analysis, so analyses keep running beside
    it. It does not queue (the page has no 排队中 state for it yet): when every
    slot is busy, or analyses already wait, it is refused with
    :func:`backtest_busy_sentence` and nothing is written. The check and the
    start happen under RUN_LOCK, so a slot cannot be taken in between.
    """
    grid = validate_backtest(payload)
    with RUN_LOCK:
        limits = run_limits()
        dispatch_queue()  # a slot that just freed goes to the queue first
        records = _refresh_all()
        queued = [r for r in records if r.get("status") == "queued"]
        if _slots_used(records, limits) >= limits["max_running"] or queued:
            live = _status_counts(records)
            raise ConsoleError(backtest_busy_sentence(
                limits, live["running"], len(queued), starting=live["starting"], stopping=live["stopping"]
            ), 409)
        return _start_backtest_now(payload, grid)


def backtest_checkpoint_scope(console_id: str, backtest_run_id: str | None) -> str:
    """The checkpoint scope of a backtest.

    A named backtest run (``run_id``) resumes by being started again with the same
    name, so its half-finished cell resumes from ``backtest-<name>``. An unnamed
    one gets a new name every time and uses the console run id.
    """
    if backtest_run_id:
        from tradingagents.graph.checkpointer import safe_scope
        try:
            return safe_scope(f"backtest-{backtest_run_id}")
        except ValueError:   # e.g. a dot in the name: no shared scope, this run's own
            return console_id
    return console_id


def _start_backtest_now(payload: dict, grid: dict) -> dict:
    merged = {
        **payload,
        "ticker": grid["tickers"][0],
        "date": backtest_probe_date(grid["dates"]),
        "analysts": grid["analysts"],
    }
    checked = validate_analysis(merged)
    run_id = new_run_id()
    log_path, result_path, job_path = _job_paths(run_id)
    selections = checked["selections"]
    record = _record_for(run_id, selections, log_path, result_path, kind="backtest")
    record["ticker"] = ",".join(grid["tickers"])
    record["date"] = backtest_span(grid["dates"], payload)
    record["start_seq"] = time.time_ns()
    job = {
        "id": run_id,
        "kind": "backtest",
        "selections": selections,
        "tickers": grid["tickers"],
        "dates": grid["dates"],
        "asset_type": grid["asset_type"],
        "analysts": grid["analysts"],
        "run_id": payload.get("run_id"),
        "result_path": str(result_path),
        "log": str(log_path),
        "job_path": str(job_path),
        "checkpoint_scope": backtest_checkpoint_scope(run_id, payload.get("run_id")),
    }
    job_path.write_text(json.dumps(redact(job), ensure_ascii=False), encoding="utf-8")
    JOBS.write_run(record)
    if _runner is not None:
        code = _runner(job)
        record["status"] = "finished" if code == 0 else "failed"
        record["returncode"] = code
        JOBS.write_run(record)
        return redact(record)
    return _spawn(job, record, log_path)


def run_backtest_job(job: dict) -> int:
    from tradingagents.backtest import run_backtest
    from tradingagents.portfolio import PortfolioContext

    selections = job["selections"]
    config = build_run_config(selections)
    # The same bounds and checkpoints as a console analysis: a backtest is many
    # analyses, and one hung call must not hold its slot for hours.
    apply_console_call_bounds(config)
    config["checkpoint_enabled"] = console_checkpoint_enabled(selections)
    if config["checkpoint_enabled"] and job.get("checkpoint_scope"):
        config["checkpoint_run_scope"] = job["checkpoint_scope"]
    portfolio = PortfolioContext.model_validate(selections["portfolio"]) if selections.get("portfolio") else None

    def show_progress(done, total, ticker, date):
        from cli.main import backtest_progress_line
        print(backtest_progress_line(done, total, ticker, date), flush=True)

    result = run_backtest(
        job["tickers"],
        job["dates"],
        config,
        asset_type=job["asset_type"],
        portfolio=portfolio,
        selected_analysts=job["analysts"],
        run_id=job.get("run_id"),
        progress=show_progress,
    )
    from cli.main import backtest_completion_text

    text = backtest_completion_text(result)
    print(text, end="", flush=True)
    Path(job["result_path"]).write_text(text, encoding="utf-8")
    return 0


def list_memory(limit: int = 20) -> dict:
    """Read the memory log later runs consult. Does not call a model."""
    from tradingagents.memory.log import (
        DECISION_MARKER,
        PENDING_STATUS,
        REFLECTION_MARKER,
        TradingMemoryLog,
    )
    from tradingagents.memory.reflection import ALPHA_VS_LABEL

    config = dict(DEFAULT_CONFIG)
    saved = load_saved_config().get("memory_log_path")
    if saved:
        config["memory_log_path"] = resolve_memory_path(str(saved))
    log = TradingMemoryLog(config)
    entries = log.load_entries()
    rows = []
    for entry in entries[-limit:]:
        decision = entry.get("decision") or ""
        reflection = entry.get("reflection") or ""
        rows.append({
            "tag": entry.get("tag"),
            "date": entry.get("date"),
            "ticker": entry.get("ticker"),
            "rating": entry.get("rating"),
            "pending": bool(entry.get("pending")),
            "raw": entry.get("raw"),
            "alpha": entry.get("alpha"),
            "holding": entry.get("holding"),
            "resolved": entry.get("resolved"),
            "decision": decision[:400],
            "truncated": len(decision) > 400,
            "reflection": reflection[:400],
            "reflection_truncated": len(reflection) > 400,
        })
    rows.reverse()
    return {
        "path": str(log._log_path) if log._log_path else None,
        "count": len(entries),
        "shown": len(rows),
        "entries": rows,
        "note": (
            "命令行没有单独的查看命令。这是运行写入、下次分析读取的记忆日志。"
            f"首行标签按文件原样显示，例如 [date | ticker | rating | {PENDING_STATUS}]。"
            "第四段未结算时是 pending，结算后是原始收益率，接着是 alpha 和 holding。"
            "页面不把第四段叫成状态或收益列。"
            f"字段对应日志里的 {DECISION_MARKER}、{REFLECTION_MARKER}、alpha 和 holding。"
            f"复盘提示包含 {ALPHA_VS_LABEL}，写进日志的是模型原文，不保证包含那句。"
            "空列表表示还没有记录。"
        ),
    }


def read_result(run_id: str | None = None) -> dict:
    """The file a finished run writes. Missing means the analysis is not complete."""
    record = _record(run_id)
    if not record or not record.get("result"):
        return {"present": False, "note": "没有结果文件。进程结束并写出结果后才会出现。启动不等于完成。"}
    path = Path(record["result"])
    if not path.is_file():
        return {"present": False, "note": "结果文件还没有写出。启动不等于完成。"}
    text = path.read_text(encoding="utf-8", errors="replace")
    if record.get("kind") == "backtest":
        return {"present": True, "kind": "backtest", "text": text}
    try:
        data = json.loads(text)
    except ValueError:
        return {"present": True, "kind": "analysis", "text": text}
    if not isinstance(data, dict):
        return {"present": True, "kind": "analysis", "text": text}
    from webui.progress import review_notice

    rating = data.get("rating")
    report = data.get("report")
    html = data.get("html")
    if not isinstance(html, str) and isinstance(report, str) and report.endswith(".md"):
        page = Path(report).with_suffix(".html")
        if page.is_file():
            html = str(page)
    return {
        "present": True,
        "kind": "analysis",
        "decision": data.get("decision"),
        "rating": rating,
        "report": report,
        "html": html,
        "saved_to": data.get("saved_to"),
        "save_error": data.get("save_error"),
        "saved": data.get("saved"),
        "save_lines": data.get("save_lines") if isinstance(data.get("save_lines"), list) else [],
        "message_tool_log": data.get("message_tool_log") if isinstance(data.get("message_tool_log"), str) else None,
        "review": review_notice(rating),
        "shown_report": data.get("shown_report") if isinstance(data.get("shown_report"), dict) else None,
    }


def run_progress(run_id: str | None = None) -> dict:
    from webui.progress import (
        message_panel_rows,
        parse_backtest_lines,
        parse_completion,
        parse_message_lines,
        parse_report_line,
        parse_review_line,
        parse_stats_line,
        parse_tool_lines,
        parse_wall_time,
        progress_rows,
        progress_timeline,
    )

    record = _record(run_id)
    selected = None
    lines: list[str] = []
    if record:
        selected = (record.get("selections") or {}).get("analysts")
        lines = read_log(5000, record).get("lines") or []
    agents = progress_rows(lines, selected) if record else []
    failure = (record or {}).get("failure")
    failed = failure.get("role") if isinstance(failure, dict) else None
    if failed and record.get("status") == "failed":
        from webui.progress import STATUS_ZH
        for row in agents:
            if row["agent"] == failed:
                row["status"], row["status_zh"] = "error", STATUS_ZH["error"]
    timeline = progress_timeline(lines, selected) if record else []
    if failed and record.get("status") == "failed" and failed in {row["agent"] for row in agents}:
        from webui.progress import AGENT_ZH, STATUS_ZH, TEAMS
        if not timeline or timeline[0]["agent"] != failed or timeline[0]["status"] != "error":
            team_zh = next((zh for _team, zh, members in TEAMS for agent, _z, _a in members if agent == failed), "")
            stamp = str(record.get("finished_at") or "")[11:19]
            timeline.insert(0, {"agent": failed, "status": "error", "time": stamp, "zh": AGENT_ZH.get(failed, failed),
                                "team_zh": team_zh, "status_zh": STATUS_ZH["error"]})
    return {
        "agents": agents,
        "timeline": timeline,
        "tools": parse_tool_lines(lines),
        "backtest": parse_backtest_lines(lines),
        "stats": parse_stats_line(lines),
        "wall_time": parse_wall_time(lines),
        "messages": parse_message_lines(lines),
        "panel": message_panel_rows(lines),
        "report": parse_report_line(lines),
        "review": parse_review_line(lines),
        "complete": parse_completion(lines),
    }


def parse_log_line_count(raw: str | None) -> int:
    """The CLI logs --lines check. Omitted means 40, the launcher default."""
    if raw in (None, ""):
        return 40
    try:
        count = int(str(raw))
    except (TypeError, ValueError) as exc:
        from cli.launcher import LOGS_LINES_INTEGER
        raise ConsoleError(LOGS_LINES_INTEGER) from exc
    if count < 0:
        from cli.launcher import LOGS_USAGE
        raise ConsoleError(LOGS_USAGE)
    return count


def read_log(limit: int = 40, run_id: str | None = None) -> dict:
    """Tail the console job log. The path line is the CLI logs sentence when a path exists."""
    from cli.launcher import log_path_sentence

    record = _record(run_id)
    if not record or not record.get("log"):
        return {"lines": [], "path": None, "cli": None}
    path = Path(record["log"])
    sentence = log_path_sentence(path)
    if not path.is_file():
        return {"lines": [], "path": str(path), "cli": sentence}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        from cli.launcher import could_not_read_log_sentence
        return {
            "path": str(path),
            "lines": [],
            "cli": sentence,
            "error": could_not_read_log_sentence(exc),
        }
    return {"path": str(path), "lines": lines[-limit:] if limit else [], "cli": sentence}


def checkpoint_clear_sentence(count: int) -> str:
    """The same sentence the CLI prints for --clear-checkpoints."""
    from cli.main import checkpoint_cleared_sentence
    return checkpoint_cleared_sentence(count)


def clear_checkpoints() -> dict:
    """Delete the per-ticker databases and every per-run database except running ones."""
    from tradingagents.graph.checkpointer import clear_all_checkpoints, clear_run_checkpoints
    keep = {r["id"] for r in JOBS.list_runs() if r.get("status") in LIVE_STATUSES}
    count = clear_all_checkpoints(effective_cache_dir()) + clear_run_checkpoints(effective_cache_dir(), keep)
    return {
        "cleared": count,
        "cli": checkpoint_clear_sentence(count),
        "zh": f"已清除 {count} 个断点。这是删除结果，不是恢复，也没有启动模型。",
    }


def list_checkpoints() -> dict:
    """Checkpoint databases --clear-checkpoints would delete. Does not claim a resume."""
    root = Path(effective_cache_dir()) / "checkpoints"
    files = []
    if root.is_dir():
        for db in sorted(root.glob("*.db")):
            files.append({"ticker": db.stem, "bytes": db.stat().st_size})
        for db in sorted((root / "runs").glob("*/*.db")):
            files.append({"ticker": db.stem, "run_id": db.parent.name, "bytes": db.stat().st_size})
    return {
        "directory": str(root),
        "files": files,
        "note": "这些文件对应 --clear-checkpoints 会删除的断点库。是否续跑要等启动时用和 CLI 相同的签名判断，页面不提前声称已恢复。",
    }


def checkpoint_announcement(graph, selections: dict, portfolio) -> str | None:
    """The same sentence the CLI prints when checkpoint resume is on.

    Returns None when checkpointing is off, because the CLI does not print a
    resume line in that case.
    """
    if not graph.config.get("checkpoint_enabled"):
        return None
    from tradingagents.graph.checkpointer import checkpoint_step

    ticker = selections["ticker"]
    date = selections["analysis_date"]
    step = checkpoint_step(
        graph.config["data_cache_dir"],
        ticker,
        date,
        graph._run_signature(selections.get("asset_type") or "stock", portfolio),
        graph.config.get("checkpoint_run_scope") or None,
    )
    if step is None:
        from cli.run import checkpoint_fresh_message
        return checkpoint_fresh_message(ticker, date)
    from cli.run import checkpoint_resume_message
    return checkpoint_resume_message(ticker, date)



def welcome_panel() -> dict:
    """The CLI welcome box text, read from the same ASCII file the CLI opens."""
    art_path = Path(__file__).resolve().parents[1] / "cli" / "static" / "welcome.txt"
    art = art_path.read_text(encoding="utf-8") if art_path.is_file() else ""
    from cli.display import WELCOME_TITLE
    from cli.selections import (
        WELCOME_BODY,
        WELCOME_CREDIT,
        WELCOME_CREDIT_URL,
        WELCOME_SUBTITLE,
        WORKFLOW_LABEL,
        WORKFLOW_STEPS,
    )
    return {
        "title": WELCOME_TITLE,
        "subtitle": WELCOME_SUBTITLE,
        "body": WELCOME_BODY,
        "workflow_label": WORKFLOW_LABEL,
        "workflow": WORKFLOW_STEPS,
        "credit": WELCOME_CREDIT,
        "credit_url": WELCOME_CREDIT_URL,
        "ascii": art,
    }


def headless_setup_text(flags: dict | None = None) -> str:
    """The CLI sentence printed when a run has no terminal and questions remain.

    The console is the terminal, so a page submission does not raise this.
    The help page shows it so the wording stays visible.
    """
    from cli.selections import unattended_gaps

    gaps = unattended_gaps(flags or {})
    if not gaps:
        return ""
    from cli.run import HEADLESS_SETUP_PREFIX
    return HEADLESS_SETUP_PREFIX + "\n" + "\n".join(f"  {gap}" for gap in gaps)


def cli_help() -> dict:
    """Launcher and Typer help text, paired with the Chinese command map."""
    from typer.testing import CliRunner

    from cli.launcher import HELP
    from cli.main import app as cli_app
    from webui.catalog import public_catalog

    runner = CliRunner()
    # ``python -m cli.main`` is what the launcher execs. The Typer app name
    # TradingAgents is not the usage line those commands print.
    prog = "python -m cli.main"
    root = runner.invoke(cli_app, ["--help"], prog_name=prog)
    if root.exit_code != 0:
        raise ConsoleError(root.stdout or root.stderr or "无法读取 CLI 帮助。")
    commands = {}
    for name in ("backtest", "login", "logout", "auth-status"):
        result = runner.invoke(cli_app, [name, "--help"], prog_name=prog)
        if result.exit_code != 0:
            raise ConsoleError(result.stdout or result.stderr or f"无法读取 {name} 帮助。")
        commands[name] = result.stdout
    catalog = public_catalog()
    return {
        "launcher": HELP,
        "cli": root.stdout,
        "commands": commands,
        "chinese": {
            "commands": catalog["commands"],
            "analyze_flags": catalog["analyze_flags"],
            "backtest_flags": catalog["backtest_flags"],
            "analyze_doc": catalog["analyze_doc"],
            "backtest_doc": catalog["backtest_doc"],
            "steps": catalog["steps"],
        },
        "headless": headless_setup_text(),
        "note": "中文对照在前。英文是 CLI 自己的 --help，没有改写。",
    }


def cached_updates() -> dict:
    """What's-new lines from the same local cache the CLI reads. No network call."""
    from cli.updates import messages, read_cache, upgrade_command
    from tradingagents import __version__

    return {
        "lines": messages(read_cache(), running=__version__, upgrade=upgrade_command),
        "source": "cli.updates.read_cache",
        "live_check": False,
        "note": "页面打开时只读本机缓存。点检查更新才会询问，对应 CLI 的 What's new。",
    }


def refresh_updates() -> dict:
    """Run the CLI update check when the user asks. The server is not a terminal, so this does not use enabled()."""
    import os

    from cli import updates

    if os.environ.get("CI") or os.environ.get(updates.OPT_OUT):
        payload = cached_updates()
        payload["ran_check"] = False
        payload["reason"] = "更新检查已关闭。对应 TRADINGAGENTS_NO_UPDATE_CHECK 或 CI。"
        return payload
    before = updates.read_cache().get("checked_at")
    updates.refresh()
    payload = cached_updates()
    payload["ran_check"] = True
    payload["checked_at_changed"] = updates.read_cache().get("checked_at") != before
    payload["note"] = "已调用与命令行相同的 refresh。它失败时不抛错，并保留原缓存。checked_at 变化不等于网络成功。"
    return payload
