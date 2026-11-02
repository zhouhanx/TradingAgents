"""Shared pieces of every information source: the item, the query, the fetch policy."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape

import requests

from tradingagents.redaction import redact_secrets

SUMMARY_LIMIT = 500
TITLE_LIMIT = 300
MAX_RETRIES = 2
RETRY_AFTER_CAP = 30.0
# TTLs (PM): announcements 15 min, news 10 min, SEC filings 6 h.
TTL_ANNOUNCEMENT = 15 * 60
TTL_NEWS = 10 * 60
TTL_FILING = 6 * 60 * 60


class SourceUnavailable(Exception):
    """The source could not answer now (network, 5xx, 429 after retries, bad body).

    The message is Chinese and shown as the source's 「不可用」 reason.
    """


@dataclass(frozen=True)
class InfoQuery:
    keyword: str = ""
    asset_type: str = "stock"  # stock | crypto | macro
    hours: int = 72

    def cache_key(self) -> str:
        return f"{self.asset_type}|{self.keyword.strip().lower()}|{self.hours}"


@dataclass
class InfoItem:
    """One normalised item. Times are UTC ISO-8601; no author or other personal fields."""

    source: str
    title: str
    url: str
    published_at: str
    summary: str = ""
    kind: str = "news"
    id: str = ""
    fetched_at: str = field(default_factory=lambda: utc_now().isoformat().replace("+00:00", "Z"))

    def __post_init__(self):
        self.title = clean_text(self.title, TITLE_LIMIT)
        self.summary = clean_text(self.summary, SUMMARY_LIMIT)
        self.url = redact_secrets(str(self.url or "").strip())
        self.published_at = iso_utc(self.published_at)
        if not self.id:
            self.id = fingerprint(self.url, self.title)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> InfoItem:
        allowed = {key: data.get(key) for key in ("source", "title", "url", "published_at", "summary", "kind", "id", "fetched_at") if data.get(key) is not None}
        return cls(**allowed)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value) -> str:
    """Any supported time (datetime, RFC 822, ISO, epoch seconds/ms, GDELT stamp) as UTC ISO."""
    dt = parse_time(value)
    return (dt or utc_now()).astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float) or (isinstance(value, str) and value.isdigit() and len(value) >= 10 and "T" not in value and len(value) != 14):
        number = float(value)
        if number > 1e12:
            number /= 1000.0
        return datetime.fromtimestamp(number, UTC)
    text = str(value).strip()
    match = re.fullmatch(r"(\d{8})T?(\d{6})Z?", text) or re.fullmatch(r"(\d{8})(\d{6})", text)
    if match:
        return datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def clean_text(value, limit: int) -> str:
    text = _SPACE.sub(" ", _TAG.sub(" ", unescape(str(value or "")))).strip()
    text = redact_secrets(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def fingerprint(url: str, title: str) -> str:
    base = (url or "").strip().lower().rstrip("/") or _SPACE.sub(" ", (title or "").lower()).strip()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]


class TTLCache:
    """In-process answer cache; the clock is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self._data: dict[str, tuple[float, list]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float):
        with self._lock:
            hit = self._data.get(key)
            if hit and self.clock() - hit[0] < ttl:
                return hit[1]
            return None

    def put(self, key: str, value: list) -> None:
        with self._lock:
            self._data[key] = (self.clock(), value)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class RateLimiter:
    """Serial requests per source with a minimum spacing between them."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.clock = clock
        self.sleep = sleep
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, source: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(source, threading.Lock())

    def wait(self, source: str, min_interval: float) -> None:
        last = self._last.get(source)
        if last is not None:
            gap = min_interval - (self.clock() - last)
            if gap > 0:
                self.sleep(gap)
        self._last[source] = self.clock()


CACHE = TTLCache()
LIMITER = RateLimiter()


def _retry_after(response) -> float | None:
    raw = (response.headers or {}).get("Retry-After") if response is not None else None
    if raw is None:
        return None
    try:
        return min(max(float(raw), 0.0), RETRY_AFTER_CAP)
    except ValueError:
        when = parse_time(raw)
        return None if when is None else min(max((when - utc_now()).total_seconds(), 0.0), RETRY_AFTER_CAP)


def http_get(source: str, url: str, *, min_interval: float, params: dict | None = None,
             headers: dict | None = None, timeout: float = 15.0,
             session=None, limiter: RateLimiter | None = None, retry_wait: float = 2.0):
    """GET with the shared policy: serial per source, spacing, retry network/5xx/429 up to 2 times.

    429 honours ``Retry-After``; other 4xx are not retried. Raises :class:`SourceUnavailable`.
    """
    limiter = limiter or LIMITER
    getter = session.get if session is not None else requests.get
    sent_headers = {"User-Agent": "TradingAgents-info-crawl/1.0 (personal research)", **(headers or {})}
    with limiter.lock_for(source):
        attempt = 0
        while True:
            limiter.wait(source, min_interval)
            try:
                response = getter(url, params=params, headers=sent_headers, timeout=timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt < MAX_RETRIES:
                    attempt += 1
                    limiter.sleep(min(2.0 * attempt, RETRY_AFTER_CAP))
                    continue
                raise SourceUnavailable(f"网络连接失败（{type(exc).__name__}）") from exc
            status = response.status_code
            if status == 429 or status >= 500:
                if attempt < MAX_RETRIES:
                    attempt += 1
                    wait = _retry_after(response) if status == 429 else None
                    limiter.sleep(wait if wait is not None else retry_wait * attempt)
                    continue
                raise SourceUnavailable("请求过于频繁，已被来源限流（HTTP 429）" if status == 429 else f"来源服务暂时不可用（HTTP {status}）")
            if status >= 400:
                raise SourceUnavailable(f"来源拒绝了请求（HTTP {status}）")
            return response


class SourceAdapter:
    """Base of every source. Subclasses set the metadata and implement :meth:`fetch`."""

    id: str = ""
    name_zh: str = ""
    kind: str = "news"            # announcement | news | filing | press
    assets: tuple[str, ...] = ()  # stock / crypto / macro
    terms_url: str = ""           # shown in the source detail only (PM ruling 3)
    description: str = ""
    min_interval: float = 1.0     # seconds between requests to this source
    ttl: float = TTL_NEWS
    optional: bool = False
    personal_use: bool = False   # terms limit use to personal research (「仅限个人使用」)
    min_window_hours: int = 0     # filings are sparse: SEC keeps a wider window
    retry_wait: float = 2.0       # back-off when a 429 carries no Retry-After
    requires_env: tuple[str, ...] = ()

    def availability(self) -> tuple[bool, str]:
        import os
        missing = [name for name in self.requires_env if not os.getenv(name, "").strip()]
        if missing:
            return False, f"未配置 {'、'.join(missing)}，该来源暂不可用"
        return True, ""

    def get(self, url: str, **kwargs):
        kwargs.setdefault("retry_wait", self.retry_wait)
        return http_get(self.id, url, min_interval=self.min_interval, **kwargs)

    def fetch(self, query: InfoQuery) -> list[InfoItem]:  # pragma: no cover - abstract
        raise NotImplementedError

    def collect(self, query: InfoQuery, cache: TTLCache | None = None) -> list[InfoItem]:
        cache = cache or CACHE
        key = f"{self.id}|{query.cache_key()}"
        hit = cache.get(key, self.ttl)
        if hit is not None:
            return list(hit)
        try:
            items = self.fetch(query)
        except SourceUnavailable:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise SourceUnavailable("来源返回的内容无法解析") from exc
        except requests.RequestException as exc:
            raise SourceUnavailable(f"网络请求失败（{type(exc).__name__}）") from exc
        cache.put(key, items)
        return list(items)

    def public(self) -> dict:
        ok, reason = self.availability()
        return {
            "id": self.id, "name": self.name_zh, "kind": self.kind, "assets": list(self.assets),
            "terms_url": self.terms_url, "description": self.description, "optional": self.optional, "personal_use": self.personal_use,
            "min_interval": self.min_interval, "ttl": int(self.ttl), "available": ok, "reason": reason,
        }


def within(item: InfoItem, hours: int, now: datetime | None = None) -> bool:
    when = parse_time(item.published_at)
    return when is None or ((now or utc_now()) - when).total_seconds() <= hours * 3600


def matches(text: str, keyword: str) -> bool:
    words = [w for w in re.split(r"[\s,，]+", keyword.lower()) if w]
    low = text.lower()
    return not words or any(w in low for w in words)
