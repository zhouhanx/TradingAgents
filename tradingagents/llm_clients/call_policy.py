"""Bounded model calls: one retry layer, one wall-clock budget, typed failures.

A model call that hangs or flaps must neither crash a run on the first blip nor
stack retries until it takes an hour. When a chat client carries a
:class:`CallPolicy`, this module is the only place that retries:

* the SDK's own retries are switched off (``max_retries=0`` on the client), so
  attempts do not multiply across layers;
* only transient failures are retried: timeouts, connection errors, HTTP 5xx and
  429 (honouring ``Retry-After``). A 400, 401, 403, 404 or an expired sign-in is
  raised at once;
* every call has a hard wall-clock budget. No attempt starts and no back-off
  sleep runs past it, and every socket operation of an attempt (connect, TLS,
  each read and write) gets at most the time left, so neither a silent server,
  a stream of keep-alive comments nor a body that trickles in a byte at a time
  can outlast it. A watchdog shuts the attempt's sockets down shortly after the
  deadline as a backstop (see :func:`install_deadline_transport`).

A streamed attempt is collected whole before its chunks are handed on, so a
stream that breaks halfway is retried from scratch instead of leaving half an
answer in the result.

The raised exception keeps its original type (callers may catch it) and carries
``tradingagents_call``: the failure kind, attempts made, the timeout and the
model, for the console's failure summary.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime
import email.utils
import logging
import math
import random
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

try:  # the OpenAI SDK ships on httpx2; langchain and older SDKs use httpx
    import httpx2 as _http
except ImportError:  # pragma: no cover - depends on the installed SDK
    _http = httpx

_HTTP_MODULES = tuple({id(module): module for module in (_http, httpx)}.values())
_TIMEOUT_TYPES = tuple(module.Timeout for module in _HTTP_MODULES)
_TIMEOUT_ERRORS = tuple(module.TimeoutException for module in _HTTP_MODULES)
_TRANSPORT_ERRORS = tuple(module.TransportError for module in _HTTP_MODULES)

logger = logging.getLogger(__name__)

DEFAULT_RETRIES = 3
DEFAULT_BUDGET = 900.0          # seconds for one model call, retries and waits included
CONNECT_TIMEOUT = 15.0
WRITE_TIMEOUT = 60.0
POOL_TIMEOUT = 15.0
READ_TIMEOUT = 300.0            # the read bound when the timeout field is empty
# The watchdog shuts an attempt's sockets down this long after its deadline. The
# socket-level clamp normally ends the attempt at the deadline; this is the
# backstop for anything that blocks outside it.
CUTOFF_GRACE = 0.25
# Worst-case slack on top of the budget: the watchdog's grace plus closing the
# connection and unwinding the call (see ``worst_case_seconds``).
CUTOFF_SLACK = 1.0

KIND_TIMEOUT = "timeout"
KIND_CONNECTION = "connection"
KIND_RATE_LIMIT = "rate_limit"
KIND_SERVER = "server"
KIND_AUTH = "auth"
KIND_OTHER = "other"

ATTRIBUTE = "tradingagents_call"


class CallBudgetExceeded(TimeoutError):
    """The call's wall-clock budget ran out. Never retried."""


class IncompleteResponse(ConnectionError):
    """A stream closed cleanly without finishing the answer. Retried."""


class TruncatedResponse(RuntimeError):
    """The model ended the answer early (finish_reason length, an incomplete
    status). Not a clean answer, and not retried: it would stop again (W26)."""


@dataclass(frozen=True)
class CallPolicy:
    """How long one model call may take and how often it is retried."""

    retries: int = DEFAULT_RETRIES       # extra attempts after the first
    budget: float = DEFAULT_BUDGET       # seconds, whole call
    backoff_base: float = 1.0
    backoff_cap: float = 30.0
    # Wall clock for one attempt; None lets an attempt use what is left of the
    # budget. An attempt cut by this cap (not the budget) is retried.
    attempt_cap: float | None = None

    def __post_init__(self):
        if isinstance(self.retries, bool) or int(self.retries) != self.retries or self.retries < 0:
            raise ValueError(f"retries must be a non-negative integer, got {self.retries!r}")
        if not (self.budget > 0) or not math.isfinite(self.budget):
            raise ValueError(f"budget must be a finite number > 0, got {self.budget!r}")
        if self.attempt_cap is not None and (not (self.attempt_cap > 0) or not math.isfinite(self.attempt_cap)):
            raise ValueError(f"attempt_cap must be a finite number > 0, got {self.attempt_cap!r}")

    @property
    def attempts(self) -> int:
        return int(self.retries) + 1

    def backoff(self, attempt: int) -> float:
        """Exponential back-off after the given (1-based) failed attempt, with jitter."""
        base = min(self.backoff_cap, self.backoff_base * (2 ** (attempt - 1)))
        return base * (0.5 + random.random() / 2)


def split_timeout(read: float) -> Any:
    """Separate connect/write/pool bounds from the read bound.

    For a streamed response httpx's read timeout is the longest silence between
    two pieces of data, not the whole answer.
    """
    read = float(read)
    return _http.Timeout(connect=min(CONNECT_TIMEOUT, read), read=read,
                         write=min(WRITE_TIMEOUT, read), pool=min(POOL_TIMEOUT, read))


def timeout_parts(timeout: Any) -> dict[str, float | None]:
    """connect/read/write/pool seconds of a float, tuple or ``httpx.Timeout``."""
    if timeout is None:
        return {"connect": None, "read": None, "write": None, "pool": None}
    if isinstance(timeout, _TIMEOUT_TYPES):
        return {"connect": timeout.connect, "read": timeout.read,
                "write": timeout.write, "pool": timeout.pool}
    if isinstance(timeout, tuple):
        connect, read = (float(timeout[0]), float(timeout[1])) if len(timeout) == 2 else (None, None)
        return {"connect": connect, "read": read, "write": read, "pool": read}
    value = float(timeout)
    return {"connect": value, "read": value, "write": value, "pool": value}


def read_timeout(timeout: Any) -> float | None:
    return timeout_parts(timeout)["read"]


def worst_case_seconds(policy: CallPolicy, timeout: Any = None) -> float:
    """Upper bound on how long one model call can take under ``policy``.

    No attempt starts and no back-off runs after the budget, and every socket
    operation of an attempt is given at most the time left, so the call ends at
    the budget; ``CUTOFF_SLACK`` covers the watchdog's grace and unwinding.
    ``timeout`` no longer matters (kept for callers); a hostname lookup is the
    one step no socket timeout reaches.
    """
    return policy.budget + CUTOFF_SLACK


def _capped(timeout: Any, remaining: float) -> Any:
    parts = timeout_parts(timeout)
    if all(value is None for value in parts.values()):
        # An empty timeout field still gets the split bounds: a connect must
        # not be allowed to wait the whole budget.
        parts = timeout_parts(split_timeout(READ_TIMEOUT))
    remaining = max(remaining, 0.001)
    return _http.Timeout(**{key: remaining if value is None else min(value, remaining)
                            for key, value in parts.items()})


def default_timeout(timeout: Any) -> Any:
    """The split bounds a policy-managed client uses: an empty field becomes
    connect 15 / read 300 / write 60 / pool 15, a number is split."""
    if timeout is None or timeout == "":
        return split_timeout(READ_TIMEOUT)
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
        return split_timeout(timeout)
    return timeout


# ---------------------------------------------------------------- classification

def _auth_types() -> tuple[tuple[type, ...], tuple[type, ...]]:
    """(unreachable, auth) error types of the sign-in providers, lazily imported."""
    unreachable: list[type] = []
    auth: list[type] = []
    try:
        from .grok_auth import GrokAuthError, GrokUnreachable
        unreachable.append(GrokUnreachable)
        auth.append(GrokAuthError)
    except Exception:  # pragma: no cover - optional module
        pass
    try:
        from .chatgpt_auth import ChatGPTAuthError, ChatGPTUnreachable
        unreachable.append(ChatGPTUnreachable)
        auth.append(ChatGPTAuthError)
    except Exception:  # pragma: no cover - optional module
        pass
    return tuple(unreachable), tuple(auth)


def _status(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def http_status(exc: BaseException) -> int | None:
    """The HTTP status of a model-call failure anywhere on the chain."""
    for item in _chain(exc):
        status = _status(item)
        if status is not None:
            return status
    return None


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def classify(exc: BaseException) -> tuple[str, bool]:
    """(kind, retryable) for a model-call failure."""
    import openai

    unreachable, auth = _auth_types()
    for item in _chain(exc):
        if isinstance(item, CallBudgetExceeded):
            return KIND_TIMEOUT, False
        if isinstance(item, IncompleteResponse):
            return KIND_CONNECTION, True
        if isinstance(item, TruncatedResponse):
            return KIND_OTHER, False
        if isinstance(item, (openai.APITimeoutError, *_TIMEOUT_ERRORS)):
            return KIND_TIMEOUT, True
        if unreachable and isinstance(item, unreachable):
            return KIND_CONNECTION, True
        if auth and isinstance(item, auth):
            return KIND_AUTH, False
        if isinstance(item, openai.APIStatusError):
            status = _status(item)
            if status == 429:
                return KIND_RATE_LIMIT, True
            if status in (401, 403):
                return KIND_AUTH, False
            if status == 408:
                return KIND_TIMEOUT, True
            if status is not None and 500 <= status <= 599:
                return KIND_SERVER, True
            return KIND_OTHER, False
        if isinstance(item, (openai.APIConnectionError, *_TRANSPORT_ERRORS)):
            return KIND_CONNECTION, True
        if isinstance(item, TimeoutError):
            return KIND_TIMEOUT, True
    return KIND_OTHER, False


def _seconds_or_none(value: float) -> float | None:
    """A wait the server asked for: nan is ignored, a negative one (or -inf)
    means \"now\", +inf stays +inf (longer than any budget: give up)."""
    if math.isnan(value):
        return None
    return max(value, 0.0)


def _parse_retry_after(raw: Any, *, millis: bool) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        value = None
    if value is not None:
        return _seconds_or_none(value / 1000.0 if millis else value)
    if millis:
        return None
    try:  # an HTTP-date
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    try:
        return _seconds_or_none(parsed.timestamp() - time.time())
    except (OverflowError, OSError, ValueError):
        return None


def retry_after(exc: BaseException) -> float | None:
    """Seconds the server asked us to wait (``retry-after-ms`` / ``Retry-After``).

    Never raises: a value that is not a number or an HTTP-date, or is nan, is
    ignored (``None``: the normal back-off applies); a negative one, or a date
    in the past, means no wait; +inf is longer than any budget, so the caller
    gives up at once with the original error and never sleeps on it.
    """
    for item in _chain(exc):
        response = getattr(item, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            continue
        try:
            raw_ms = headers.get("retry-after-ms")
            raw = headers.get("retry-after")
        except Exception:  # noqa: BLE001 - odd header containers
            continue
        for value, millis in ((raw_ms, True), (raw, False)):
            seconds = _parse_retry_after(value, millis=millis)
            if seconds is not None:
                return seconds
    return None


# ---------------------------------------------------------------- the attempt loop

class AttemptTimedOut(TimeoutError):
    """One attempt ran past its own wall-clock cap (``attempt_cap``). Retried."""


@dataclass
class _Attempt:
    deadline: float                  # this attempt's wall clock: min(cap, call deadline)
    timeout: Any
    clock: Callable[[], float]
    call_deadline: float = math.inf
    sockets: list = field(default_factory=list)
    cut: bool = False
    released: bool = False
    stream_done: bool = False        # the attempt's stream sent data: [DONE]
    lock: Any = field(default_factory=threading.Lock)

    def expired_error(self) -> TimeoutError:
        if self.deadline < self.call_deadline:
            return AttemptTimedOut("the model call attempt ran past its time cap")
        return CallBudgetExceeded("the model call ran past its time budget")

    def track(self, sock: Any) -> None:
        """Remember a socket this attempt uses, so the watchdog can cut it.

        The attempt becomes the socket's owner: a pooled keep-alive connection
        is claimed by whichever call used it last (W27).
        """
        if sock is None:
            return
        with self.lock:
            if self.released:
                return
            _claim(sock, self)
            if not any(item is sock for item in self.sockets):
                self.sockets.append(sock)
            cut = self.cut
        if cut:
            _shutdown(sock)

    def cut_off(self) -> None:
        """Watchdog: shut down the sockets this attempt still owns (wakes a blocked read).

        A socket that went back to the pool and was picked up by another call
        belongs to that call now and is left alone (W27).
        """
        with self.lock:
            if self.released:
                return
            self.cut = True
            sockets = list(self.sockets)
        for sock in sockets:
            if _owner(sock) is self:
                _shutdown(sock)

    def release(self) -> None:
        """The attempt is over: its watchdog may no longer touch any socket."""
        with self.lock:
            self.released = True
            sockets, self.sockets = self.sockets, []
        with _OWNERS_LOCK:
            for sock in sockets:
                if _OWNERS.get(id(sock)) is self:
                    del _OWNERS[id(sock)]


# Which attempt used a socket last. A socket of a keep-alive connection is
# owned by the call using it now, never by an earlier call it served (W27).
_OWNERS: dict[int, _Attempt] = {}
_OWNERS_LOCK = threading.Lock()


def _claim(sock: Any, attempt: _Attempt | None) -> None:
    with _OWNERS_LOCK:
        if attempt is None:
            _OWNERS.pop(id(sock), None)
        else:
            _OWNERS[id(sock)] = attempt


def _owner(sock: Any) -> _Attempt | None:
    with _OWNERS_LOCK:
        return _OWNERS.get(id(sock))


def _shutdown(sock: Any) -> None:
    with contextlib.suppress(Exception):
        sock.shutdown(socket.SHUT_RDWR)


_CURRENT: contextvars.ContextVar[_Attempt | None] = contextvars.ContextVar("tradingagents_attempt", default=None)


def attempt_timeout() -> Any:
    """The capped timeout of the attempt in flight, for the request payload."""
    current = _CURRENT.get()
    return None if current is None else current.timeout


def check_deadline() -> None:
    """Raise when the attempt in flight is past its wall clock.

    :class:`CallBudgetExceeded` (not retried) at the call's budget,
    :class:`AttemptTimedOut` (retried) at an earlier per-attempt cap.
    """
    current = _CURRENT.get()
    if current is not None and current.clock() >= current.deadline:
        raise current.expired_error()


def io_timeout(timeout: float | None, expired: Callable[[str], BaseException]) -> float | None:
    """The timeout for one socket operation of the attempt in flight.

    At most the time left before the attempt's deadline; past it, ``expired``
    (the transport's own timeout exception) is raised, so every read, write,
    connect and TLS handshake ends at the deadline whatever the server sends.
    Outside a policy-managed call, ``timeout`` unchanged.
    """
    current = _CURRENT.get()
    if current is None:
        return timeout
    left = current.deadline - current.clock()
    if left <= 0 or current.cut:
        raise expired("the model call reached its time limit")
    return left if timeout is None else min(timeout, left)


def note_stream_done() -> None:
    """The attempt in flight read a ``data: [DONE]`` event."""
    current = _CURRENT.get()
    if current is not None:
        current.stream_done = True


def stream_done() -> bool:
    current = _CURRENT.get()
    return current is not None and current.stream_done


def track_socket(sock: Any) -> None:
    """Note that the call in flight (or no managed call) is using ``sock`` now."""
    if sock is None:
        return
    current = _CURRENT.get()
    if current is None:
        _claim(sock, None)
    else:
        current.track(sock)


# ---------------------------------------------------------------- the deadline transport

class _DeadlineStream:
    """A network stream whose every operation ends at the attempt's deadline."""

    def __init__(self, inner: Any, core: Any):
        self._inner = inner
        self._core = core
        track_socket(self._socket())

    def _socket(self) -> Any:
        with contextlib.suppress(Exception):
            return self._inner.get_extra_info("socket")
        return None

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        track_socket(self._socket())
        return self._inner.read(max_bytes, io_timeout(timeout, self._core.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        track_socket(self._socket())
        return self._inner.write(buffer, io_timeout(timeout, self._core.WriteTimeout))

    def close(self) -> None:
        return self._inner.close()

    def start_tls(self, ssl_context: Any, server_hostname: str | None = None,
                  timeout: float | None = None) -> Any:
        inner = self._inner.start_tls(ssl_context, server_hostname,
                                      io_timeout(timeout, self._core.ConnectTimeout))
        return _DeadlineStream(inner, self._core)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _DeadlineBackend:
    """Wraps an httpcore network backend; a no-op outside a policy-managed call."""

    tradingagents_deadline = True

    def __init__(self, inner: Any, core: Any):
        self._inner = inner
        self._core = core

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        timeout = io_timeout(timeout, self._core.ConnectTimeout)
        return _DeadlineStream(self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options),
            self._core)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        timeout = io_timeout(timeout, self._core.ConnectTimeout)
        return _DeadlineStream(self._inner.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options), self._core)

    def sleep(self, seconds: float) -> None:
        return self._inner.sleep(seconds)


def _core_module(pool: Any) -> Any:
    name = type(pool).__module__.split(".")[0]
    import importlib
    return importlib.import_module(name)


def install_deadline_transport(http_client: Any) -> bool:
    """Make the sync HTTP client's sockets honour the attempt deadline.

    Wraps the network backend of every connection pool of ``http_client``
    (the default transport and any proxy mounts). Outside a policy-managed
    call the wrapper passes timeouts through unchanged, so a client shared
    with other models is unaffected. Returns whether anything was wrapped.
    """
    transports = [getattr(http_client, "_transport", None)]
    transports += list((getattr(http_client, "_mounts", None) or {}).values())
    wrapped = False
    for transport in transports:
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            continue
        if getattr(backend, "tradingagents_deadline", False):
            wrapped = True
            continue
        try:
            pool._network_backend = _DeadlineBackend(backend, _core_module(pool))
        except Exception:  # noqa: BLE001 - an unknown transport keeps its sockets
            logger.debug("could not install the deadline transport on %r", transport, exc_info=True)
            continue
        wrapped = True
    return wrapped


def _annotate(exc: BaseException, info: dict) -> None:
    with contextlib.suppress(Exception):  # exotic exception types
        setattr(exc, ATTRIBUTE, info)


def call_info(exc: BaseException) -> dict | None:
    """The ``tradingagents_call`` annotation anywhere on the exception chain."""
    for item in _chain(exc):
        info = getattr(item, ATTRIBUTE, None)
        if isinstance(info, dict):
            return info
    return None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _clock() -> float:
    return time.monotonic()


def run_with_policy(call: Callable[[], Any], *, policy: CallPolicy, timeout: Any,
                    model: str | None = None, provider: str | None = None,
                    sleep: Callable[[float], None] | None = None,
                    clock: Callable[[], float] | None = None) -> Any:
    """Run ``call`` under ``policy``: bounded attempts, budget and typed failure."""
    real_clock = clock is None
    sleep = sleep or _sleep
    clock = clock or _clock
    deadline = clock() + policy.budget
    attempt = 0
    waited = 0.0
    while True:
        attempt += 1
        now = clock()
        remaining = deadline - now
        if remaining <= 0:
            exc = CallBudgetExceeded("the model call ran past its time budget")
            info = _info(KIND_TIMEOUT, attempt - 1, policy, timeout, model, provider, waited)
            info["budget_exhausted"] = True
            _annotate(exc, info)
            raise exc
        attempt_deadline = deadline if policy.attempt_cap is None else min(deadline, now + policy.attempt_cap)
        current = _Attempt(attempt_deadline, _capped(timeout, attempt_deadline - now), clock,
                           call_deadline=deadline)
        token = _CURRENT.set(current)
        watchdog = _start_watchdog(current, attempt_deadline - now) if real_clock else None
        try:
            return call()
        except Exception as exc:
            if watchdog is not None:
                watchdog.cancel()
            kind, retryable = _safe_classify(exc)
            if current.cut or clock() >= attempt_deadline:
                kind = KIND_TIMEOUT          # cut at the deadline, whatever the socket said
                retryable = attempt_deadline < deadline
            info = _info(kind, attempt, policy, timeout, model, provider, waited)
            if not retryable or attempt >= policy.attempts:
                if clock() >= deadline:
                    info["budget_exhausted"] = True
                _annotate(exc, info)
                raise
            wait = _wait_before_retry(exc, policy, attempt)
            if clock() + wait >= deadline:
                info["budget_exhausted"] = True
                _annotate(exc, info)
                raise
            logger.warning("Model call failed (%s, attempt %d of %d); retrying in %.1fs",
                           kind, attempt, policy.attempts, wait)
            sleep(wait)
            waited += wait
        finally:
            if watchdog is not None:
                watchdog.cancel()
            current.release()
            _CURRENT.reset(token)


def _start_watchdog(current: _Attempt, seconds: float) -> threading.Timer:
    timer = threading.Timer(max(seconds, 0.0) + CUTOFF_GRACE, current.cut_off)
    timer.daemon = True
    timer.start()
    return timer


def _safe_classify(exc: BaseException) -> tuple[str, bool]:
    try:
        return classify(exc)
    except Exception:  # noqa: BLE001 - never let the policy hide the real error
        logger.debug("could not classify %r", exc, exc_info=True)
        return KIND_OTHER, False


def _wait_before_retry(exc: BaseException, policy: CallPolicy, attempt: int) -> float:
    """Retry-After when the server sent a usable one, else the back-off.

    Always >= 0 and never nan; +inf only for a server asking for longer than
    any budget, which the loop turns into giving up (it is never slept on).
    """
    try:
        asked = retry_after(exc)
    except Exception:  # noqa: BLE001 - a malformed header must not replace the 429
        asked = None
    wait = asked if asked is not None else policy.backoff(attempt)
    if math.isnan(wait) or wait < 0:
        wait = policy.backoff(attempt)
    return wait


def _info(kind, attempts, policy, timeout, model, provider, waited) -> dict:
    return {
        "kind": kind,
        "attempts": attempts,
        "max_attempts": policy.attempts,
        "timeout": read_timeout(timeout),
        "budget": policy.budget,
        "model": model,
        "provider": provider,
        "waited": round(waited, 3),
    }


def policy_for(retries: Any = None, budget: Any = None, attempt_cap: Any = None) -> CallPolicy:
    """A policy from config values. ``None`` keeps each default."""
    return CallPolicy(
        retries=DEFAULT_RETRIES if retries in (None, "") else int(retries),
        budget=DEFAULT_BUDGET if budget in (None, "") else float(budget),
        attempt_cap=None if attempt_cap in (None, "") else float(attempt_cap),
    )
