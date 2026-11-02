"""Local console server. Binds to loopback unless told otherwise."""

from __future__ import annotations

import contextlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
import unicodedata
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from webui import service
from webui.catalog import PROVIDER_ICONS, provider_rows, public_catalog
from webui.frameworks import DEFAULT_FRAMEWORK, public_frameworks

STATIC = Path(__file__).resolve().parent / "static"

# SEC-001: every state-changing request must carry this header with the per-process
# token the console page received. A cross-origin page cannot read the page (same-origin
# policy) and cannot add a custom header to a no-cors request, so it cannot forge one.
TOKEN_HEADER = "X-TradingAgents-Token"
TOKEN_META = "tradingagents-token"
# W1 (SEC-002): report links carry this per-process key as ``k=``. It is a second
# random value, not the API token: it only opens report files, and it never travels
# in a header the page could leak. Links stop working when the console restarts.
LINK_KEY_META = "tradingagents-link-key"
LINK_KEY_PARAM = "k"
_LOOPBACK_NAMES = ("127.0.0.1", "localhost", "[::1]")
_WILDCARD_BINDS = {"", "0.0.0.0", "::", "[::]"}
# GET endpoints the browser may open by navigation (link, download, new tab). They cannot
# send the token header, so they need the link key (``k=``) instead, plus the Host and
# Sec-Fetch-Site checks. A request without Sec-Fetch headers (old browser, curl) still
# needs the key (W1).
_NAVIGABLE_API = {"/api/reports/file"}
REPORT_CSP = (
    "sandbox allow-popups allow-popups-to-escape-sandbox; default-src 'none'; "
    "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'none'"
)


def allowed_hosts(bind_host: str, port: int) -> frozenset[str]:
    """Host header values this console answers. Anything else is DNS rebinding or a typo."""
    names = set(_LOOPBACK_NAMES)
    host = (bind_host or "").strip().lower()
    if host not in _WILDCARD_BINDS:
        names.add(f"[{host}]" if ":" in host and not host.startswith("[") else host)
    return frozenset(f"{name}:{port}" for name in names)


_REPORT_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".htm": "text/html; charset=utf-8",
    ".json": "application/json",
    ".log": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
}


def report_content_type(path: Path) -> str:
    """Content-Type by extension; unknown kinds are opaque bytes, never sniffed as HTML."""
    return _REPORT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def report_download_name(path: Path) -> str:
    """reports/<run>/complete_report.md -> <run>_complete_report.md, so downloads do not collide."""
    parent = path.parent.name
    return f"{parent}_{path.name}" if parent and parent != "reports" else path.name


def content_disposition(name: str) -> str:
    """``attachment`` with an ASCII fallback and an RFC 5987 ``filename*`` for the real name."""
    from urllib.parse import quote

    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\;' else "_" for ch in name)
    stem, dot, suffix = fallback.rpartition(".")
    if not fallback.strip("_. ") or (dot and not stem.strip("_")):
        fallback = f"report.{suffix}" if dot and suffix.isascii() and suffix.strip("_") else "report"
    header = f'attachment; filename="{fallback}"'
    if fallback != name:
        header += f"; filename*=UTF-8''{quote(name, safe='')}"
    return header


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


class Handler(BaseHTTPRequestHandler):
    server_version = "TradingAgentsConsole/0.1"

    # --- SEC-001 request guards -----------------------------------------------------

    def _session_token(self) -> str:
        return getattr(self.server, "session_token", "") or ""

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        allowed = getattr(self.server, "allowed_hosts", None)
        if allowed is None:
            port = self.server.server_address[1]
            allowed = allowed_hosts(str(self.server.server_address[0]), port)
        return host in allowed

    def _same_origin(self) -> bool:
        """Origin (or Referer when Origin is absent) must be this console, if sent."""
        expected = f"http://{(self.headers.get('Host') or '').strip().lower()}"
        origin = self.headers.get("Origin")
        if origin is not None:
            return _origin_of(origin.strip()) == expected
        referer = self.headers.get("Referer")
        if referer is not None:
            return _origin_of(referer.strip()) == expected
        return True

    def _token_ok(self) -> bool:
        expected = self._session_token()
        supplied = self.headers.get(TOKEN_HEADER) or ""
        if not expected:
            return False
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _link_key_ok(self) -> bool:
        expected = getattr(self.server, "link_key", "") or ""
        query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
        supplied = (query.get(LINK_KEY_PARAM) or [""])[0]
        if not expected:
            return False
        return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _cross_site_fetch(self) -> bool:
        return (self.headers.get("Sec-Fetch-Site") or "").strip().lower() in {"cross-site", "same-site"}

    def _refuse(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _guard(self, path: str, *, state_changing: bool) -> bool:
        """Return True when the request may proceed; otherwise the refusal is already sent."""
        if not self._host_ok():
            self._refuse(421, "Host 不是本机控制台地址，请求已拒绝。")
            return False
        if state_changing:
            if not self._same_origin() or self._cross_site_fetch():
                self._refuse(403, "跨站请求已拒绝。")
                return False
            if not self._token_ok():
                self._refuse(403, "缺少或错误的控制台会话令牌。请刷新控制台页面后重试。")
                return False
            return True
        if path.startswith("/api/"):
            if self._cross_site_fetch():
                self._refuse(403, "跨站请求已拒绝。")
                return False
            if path in _NAVIGABLE_API:
                if not (self._link_key_ok() or self._token_ok()):
                    self._refuse(403, LINK_KEY_REFUSED)
                    return False
            elif not self._token_ok():
                self._refuse(403, "缺少或错误的控制台会话令牌。请刷新控制台页面后重试。")
                return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._guard(parsed.path, state_changing=False):
            return
        if parsed.path == "/api/catalog":
            self._json(self._catalog())
            return
        if parsed.path == "/api/frameworks":
            # WEBUI-008: the 启动分析 cards. Read-only; same session-token rule as every API read.
            self._json({"frameworks": public_frameworks(service.JOBS.list_runs()), "default": DEFAULT_FRAMEWORK})
            return
        if parsed.path == "/api/session":
            # W6: the page probes this to learn early that its session token went stale.
            self._json({"ok": True})
            return
        if parsed.path == "/api/auth/status":
            query = parse_qs(parsed.query, keep_blank_values=True)
            if "plan" in query:
                raw = query["plan"][0]
                self._call(lambda: service.auth_status(raw))
            else:
                self._json(service.auth_status())
            return
        if parsed.path == "/api/auth/login":
            self._json(service.JOBS.login_state())
            return
        if parsed.path == "/api/ollama":
            query = parse_qs(parsed.query)
            self._json(service.ollama_hint((query.get("url") or [None])[0]))
            return
        if parsed.path == "/api/asset":
            query = parse_qs(parsed.query)
            self._call(lambda: service.asset_options((query.get("ticker") or [""])[0]))
            return
        if parsed.path == "/api/kline/latest":
            # V2-KLINE-RT: the newest bar for the page's visible-only polling; same token rule.
            query = parse_qs(parsed.query)
            self._kline_latest((query.get("symbol") or [""])[0])
            return
        if parsed.path == "/api/kline":
            # V2-KLINE: K 线行情 bars; read-only, same session-token rule as every API read.
            query = parse_qs(parsed.query)
            self._kline((query.get("symbol") or [""])[0], (query.get("tf") or [""])[0])
            return
        if parsed.path == "/api/models":
            query = parse_qs(parsed.query)
            provider = (query.get("provider") or [""])[0]
            mode = (query.get("mode") or ["quick"])[0]
            self._call(lambda: service.list_models(provider, mode))
            return
        if parsed.path == "/api/config":
            self._json({
                "saved": service.public_saved_config(),
                "keys": service.key_status(),
                "data_env": service.data_env_status(),
                "cloud_env": service.cloud_env_status(),
                "prefs": service.remembered_form(),
                "depth_overrides": service.depth_lines_for(),
                "environment_lines": service.environment_confirmation_lines(),
                "env_output_language": service.env_output_language(),
                "env_skipped": service.env_skipped_values(),
                "recent": service.recent_tickers(),
            })
            return
        if parsed.path == "/api/runs":
            query = parse_qs(parsed.query)
            run_id = (query.get("id") or [None])[0]
            self._json(service.api_runs(run_id))
            return
        if parsed.path == "/api/logs":
            query = parse_qs(parsed.query)
            raw = (query.get("lines") or [None])[0]
            run_id = (query.get("id") or [None])[0]
            self._call(lambda: _read_logs(raw, run_id))
            return
        if parsed.path == "/api/message-log":
            query = parse_qs(parsed.query)
            self._call(lambda: service.read_message_tool_log((query.get("path") or [""])[0]))
            return
        if parsed.path == "/api/memory":
            self._json(service.list_memory())
            return
        if parsed.path == "/api/backtests":
            self._call(service.list_backtest_sweeps)     # W13: a clear error, not a dropped connection
            return
        if parsed.path == "/api/reports":
            self._call(lambda: {
                "reports": service.list_reports(),
                "live": service.list_live_sections(),
                "directory": service.effective_results_dir(),
            })
            return
        if parsed.path == "/api/live-sections":
            query = parse_qs(parsed.query)
            self._call(lambda: service.read_live_section((query.get("path") or [""])[0]))
            return
        if parsed.path == "/api/help":
            self._json(service.cli_help())
            return
        if parsed.path == "/api/checkpoints":
            self._json(service.list_checkpoints())
            return
        if parsed.path == "/api/updates":
            self._json(service.cached_updates())
            return
        if parsed.path == "/api/reports/file":
            query = parse_qs(parsed.query)
            self._report_file((query.get("path") or [""])[0], download=(query.get("download") or [""])[0] == "1")
            return
        if parsed.path == "/api/reports/outline":
            query = parse_qs(parsed.query)
            self._call(lambda: service.report_outline((query.get("path") or [""])[0]))
            return
        if parsed.path in {"/vendor/lightweight-charts.js", "/vendor/lightweight-charts.LICENSE.txt"}:
            self._static(STATIC / "vendor" / Path(parsed.path).name)
            return
        if parsed.path.startswith("/icons/"):
            self._static(STATIC / "icons" / Path(parsed.path).name)
            return
        if parsed.path in {"/", "/index.html"}:
            self._index()
            return
        relative = parsed.path.lstrip("/")
        if relative in {"app.js", "app.css", "console.css"}:
            self._static(STATIC / relative)
            return
        self._json({"error": "没有这个页面。"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._guard(parsed.path, state_changing=True):
            return
        try:
            body = self._body()
        except service.ConsoleError as exc:
            self._json({"error": str(exc)}, exc.status)
            return
        routes = {
            "/api/auth/login": lambda: service.start_login(body.get("provider") if "provider" in body else None),
            "/api/auth/login/paste": lambda: service.JOBS.paste_login(str(body.get("url") or "")),
            "/api/auth/logout": lambda: service.logout(body.get("provider") if "provider" in body else None),
            "/api/config": lambda: service.save_config(body),
            "/api/keys": lambda: service.save_api_key(str(body.get("provider") or ""), str(body.get("key") or "")),
            "/api/data-env": lambda: service.save_data_env(str(body.get("env") or ""), str(body.get("value") or "")),
            "/api/runs": lambda: self._start(body),
            "/api/runs/validate": lambda: service.validate_analysis(body),
            "/api/runs/stop": lambda: service.stop_run(_run_id(body)),
            "/api/runs/restart": lambda: service.restart_run(resume=body.get("resume") is True, run_id=_run_id(body)),
            "/api/runs/cancel": lambda: service.cancel_run(_run_id(body)),
            "/api/backtests/validate": lambda: service.validate_backtest(body),
            "/api/backtests": lambda: service.start_backtest(body),
            "/api/checkpoints/clear": lambda: service.clear_checkpoints(),
            "/api/updates/refresh": lambda: service.refresh_updates(),
        }
        handler = routes.get(parsed.path)
        if handler is None:
            self._json({"error": "没有这个操作。"}, 404)
            return
        self._call(handler)

    def _catalog(self) -> dict:
        catalog = public_catalog()
        catalog["providers"] = [
            {
                **item,
                "icon": f"/icons/{PROVIDER_ICONS[item['key']]}" if item["key"] in PROVIDER_ICONS else None,
                "subscription": item["key"] in {"chatgpt", "grok"},
            }
            for item in provider_rows()
        ]
        catalog["welcome"] = service.welcome_panel()
        catalog["model_prompts"] = service.model_prompt_map()
        return catalog

    def _start(self, body: dict) -> dict:
        framework = service.launch_framework(body)  # unknown framework: 400 before anything is checked or started
        checked = service.validate_analysis(body)
        record = service.start_validated_run(checked["selections"], background=bool(body.get("background", True)),
                                             framework=checked.get("framework", framework))
        record["ignored"] = checked["ignored"]
        return record

    def _call(self, func) -> None:
        try:
            self._json(func())
        except service.ConsoleError as exc:
            self._json({"error": str(exc)}, exc.status)
        except Exception as exc:  # surface the failure; do not pretend success
            # The exception text can carry OS errors and server paths: it goes to the
            # server's own stderr only, the client gets a generic sentence.
            _log_internal_error(self.command, urlparse(self.path).path, exc)
            self._json({"error": INTERNAL_ERROR}, 500)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            raise service.ConsoleError("请求不是 JSON。") from exc
        if not isinstance(data, dict):
            raise service.ConsoleError("请求体必须是对象。")
        return data

    def _json(self, payload, status: int = 200) -> None:
        # "replace": a lone surrogate read back from a file never breaks the reply (R2-MF1)
        body = json.dumps(service.redact(payload), ensure_ascii=False).encode("utf-8", "replace")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _index(self) -> None:
        """The console page, carrying this process's session token for the page's own JS."""
        markup = (STATIC / "index.html").read_text(encoding="utf-8")
        meta = (f'<meta name="{TOKEN_META}" content="{self._session_token()}">\n  '
                f'<meta name="{LINK_KEY_META}" content="{getattr(self.server, "link_key", "") or ""}">')
        markup = markup.replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n  {meta}', 1)
        data = markup.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        self.wfile.write(data)

    def _kline(self, symbol: str, tf: str) -> None:
        from webui import kline as kline_data

        try:
            self._json(kline_data.kline(symbol, tf or None))
        except kline_data.KlineError as exc:
            self._json({"error": str(exc)}, 400)

    def _kline_latest(self, symbol: str) -> None:
        from webui import kline as kline_data

        try:
            self._json(kline_data.latest(symbol))
        except kline_data.KlineError as exc:
            self._json({"error": str(exc)}, 400)

    def _static(self, path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_file() or STATIC.resolve() not in resolved.parents:
            self._json({"error": "找不到静态文件。"}, 404)
            return
        path = resolved
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if path.suffix in {".js", ".css", ".html", ".svg"}:
            mime = {
                ".js": "text/javascript",
                ".css": "text/css",
                ".html": "text/html",
                ".svg": "image/svg+xml",
            }[path.suffix]
        text = mime.startswith("text/") or mime == "image/svg+xml"
        self.send_response(200)
        self.send_header("Content-Type", f"{mime}; charset=utf-8" if text else mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if path.suffix == ".svg":
            # WEBUI-007 item 2: the logo/favicon SVG is an image, never a document that runs script.
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; sandbox")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: Path, *, download: bool = False) -> None:
        data = path.read_bytes()
        content_type = report_content_type(path)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        # Only HTML is shown inline (打开 HTML); everything else, and any explicit
        # download, is an attachment with its own file name.
        if download or not content_type.startswith("text/html"):
            self.send_header("Content-Disposition", content_disposition(report_download_name(path)))
        # Report HTML is model-written text. Sandbox it into an opaque origin with no
        # scripts so it can never read the console token or call the API as the page.
        self.send_header("Content-Security-Policy", REPORT_CSP)
        # W1/W34: a link inside a report never carries this URL (and its k=) away.
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _report_file(self, raw_path: str, *, download: bool = False) -> None:
        try:
            self._file(service.resolve_report(raw_path), download=download)
        except service.ConsoleError as exc:
            self._json({"error": str(exc)}, exc.status)
        except OSError:  # W16: exists but cannot be read -> the same 404, no errno or path
            self._json({"error": REPORT_UNREADABLE}, 404)
        except Exception as exc:  # a response, not a dead request; no internal error text
            _log_internal_error(self.command, "/api/reports/file", exc)
            self._json({"error": "读取报告失败。"}, 500)

    def log_message(self, _fmt: str, *args) -> None:
        if os.environ.get("TRADINGAGENTS_WEBUI_REQUEST_LOG") != "1":
            return
        status = args[1] if len(args) > 1 else "-"
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"{timestamp} {self.command} {_log_path(self.path)} {status}", flush=True)


LINK_KEY_REFUSED = "报告链接已失效或不完整（控制台重启后旧链接不能再用）。请回到控制台的报告页重新打开。"
REPORT_UNREADABLE = "找不到这份报告，或这份报告无法读取。"
INTERNAL_ERROR = "服务器内部错误，没有完成这次操作。详情已写入运行 WebUI 的终端。"


# W90/W117: the link key and any secret-named parameter (apikey, api-key, password,
# secret, refresh_token, ...; the name also when URL-encoded, %6B=) never reach the
# request log.
_LOG_PARAM = re.compile(r"([?&;])([^=&;#\s]*)=([^&;#\s]*)")
# B1: auth / sig / signature and the AWS / Google signed-URL parameters too.
_LOG_SHORT_NAMES = frozenset({"k", "key", "auth", "sig", "signature", "x-amz-signature", "x-amz-credential",
                              "x-amz-security-token", "x-goog-signature", "x-goog-credential"})


def _log_secret_name(name: str) -> bool:
    from urllib.parse import unquote_plus

    from webui.redaction import _secret_key_name

    plain = name
    for _ in range(3):                      # %6B, %256B
        plain = unquote_plus(plain)
    return plain.strip().casefold() in _LOG_SHORT_NAMES or _secret_key_name(plain)


def _log_path(path: str) -> str:
    def one(match: re.Match) -> str:
        sep, name, _value = match.groups()
        if _log_secret_name(name):
            return f"{sep}{name}=[redacted]"
        from urllib.parse import unquote_plus

        from webui.redaction import scan_value

        plain = unquote_plus(_value)                # B1: a key under any name (AIza..., sk-...)
        return f"{sep}{name}=[redacted]" if scan_value(plain) != plain else match.group(0)

    return _LOG_PARAM.sub(one, str(path))


def _log_internal_error(method: str, path: str, exc: BaseException) -> None:
    """Server-side detail for a 500. The query string (which can hold user paths) is
    left out; the traceback stays in this process's stderr."""
    print(f"WebUI internal error on {method} {path}: {type(exc).__name__}", file=sys.stderr, flush=True)
    traceback.print_exception(exc, file=sys.stderr)


RUN_ID_REQUIRED = "请求里的任务 id 为空或不是文字，没有对任何任务执行操作。"


def _run_id(body: dict) -> str:
    """The run a stop / restart / cancel names.

    PM ruling (W36): a missing ``id`` counts as empty. A request with no ``id``,
    or an ``id`` that is empty, blank or not a string, is refused with 400
    before anything happens; there is no "newest run" fallback.
    """
    value = body.get("id")
    if not isinstance(value, str) or not value.strip():
        raise service.ConsoleError(RUN_ID_REQUIRED, 400)
    return value


def _read_logs(raw: str | None, run_id: str | None = None) -> dict:
    found = service.read_log(service.parse_log_line_count(raw), run_id)
    if found.get("error"):
        raise service.ConsoleError(found["error"])
    return found


class ConsoleHTTPServer(ThreadingHTTPServer):
    """W53/W61: the stdlib listen backlog of 5 resets connections in a burst of 40+."""

    request_queue_size = 128
    daemon_threads = True


class ConsoleHTTPServerV6(ConsoleHTTPServer):
    """W89: an IPv6 literal (--host ::1) needs an AF_INET6 socket."""

    address_family = socket.AF_INET6


def _visible_host(text: str) -> str:
    """W112: NFKC, then no format characters (ZWSP, BOM, bidi marks) anywhere,
    then no surrounding whitespace. What is classified is exactly what is bound."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf").strip()


def normalize_bind_host(host: str) -> str:
    """Normalize a ``--host`` value for bind and the printed URL.

    W89: strip surrounding whitespace; ``[::1]`` → ``::1``.
    W105: peel every matching bracket layer (``[[::1]]`` → ``::1``).
    W106: NFKC-fold full-width digits/dots so ``１２７.０.０.１`` binds and prints
    as ASCII ``127.0.0.1``. Raises ValueError for an empty host.
    """
    name = _visible_host(host or "")
    while name.startswith("[") and name.endswith("]"):
        name = _visible_host(name[1:-1])
    if not name:
        raise ValueError("empty host")
    return name


def url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def serve(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    host = normalize_bind_host(host)
    server_class = ConsoleHTTPServerV6 if ":" in host else ConsoleHTTPServer
    httpd = server_class((host, port), Handler)
    httpd.session_token = secrets.token_urlsafe(32)
    httpd.link_key = secrets.token_urlsafe(24)
    httpd.allowed_hosts = allowed_hosts(host, httpd.server_address[1])
    return httpd


def start_run_dispatcher(interval: float = 2.0) -> threading.Thread:
    """Recover runs left by an earlier console, then keep starting queued runs (RUN-004).

    See ``service.recover_runs_on_start`` for what happens to running and queued
    runs across a console restart.
    """
    import contextlib
    with contextlib.suppress(Exception):
        service.recover_runs_on_start()

    def loop() -> None:
        while True:
            time.sleep(interval)
            with contextlib.suppress(Exception):
                service.dispatch_queue()

    thread = threading.Thread(target=loop, name="webui-run-dispatcher", daemon=True)
    thread.start()
    return thread


def console_prog() -> str:
    """Prog name for ``web --help`` / argparse errors (W103).

    ``tradingagents-web`` → itself; ``tradingagents-launcher web`` / ``python -m
    cli.launcher web`` → ``<launcher> web``; catalog/pytest → the canonical
    ``python -m cli.launcher web`` so Proto rows stay aligned with ``-m`` runs.
    """
    if not sys.argv:
        return "python -m cli.launcher web"
    base = Path(sys.argv[0]).name
    if base == "tradingagents-web":
        return "tradingagents-web"
    if base == "tradingagents-launcher":
        return "tradingagents-launcher web"
    if base == "launcher.py":
        return "python -m cli.launcher web"
    raw = Path(sys.argv[0])
    if base in {"__main__.py", "server.py"} and "webui" in raw.parts:
        return "python -m webui" if base == "__main__.py" else "python -m webui.server"
    return "python -m cli.launcher web"


def build_console_parser(prog: str | None = None):
    """The argparse parser used by ``web``. Default prog follows ``console_prog()``."""
    import argparse
    parser = argparse.ArgumentParser(
        prog=console_prog() if prog is None else prog,
        description="打开 TradingAgents 控制台",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--allow-lan", action="store_true",
                        help="允许 --host 绑定到非本机地址（局域网里任何能打开该地址的人都会拿到控制台令牌）")
    return parser


def is_loopback_host(host: str) -> bool:
    """127.0.0.0/8, ::1 and localhost. A wildcard (0.0.0.0, ::) or any other address is not."""
    import ipaddress

    name = (host or "").strip().lower().strip("[]")
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


LAN_REFUSED = (
    "拒绝启动：--host {host} 不是本机回环地址。控制台页面会把会话令牌交给任何能打开这个地址的人"
    "（局域网里的其他电脑也可以），拿到令牌就能用你的订阅启动分析、读报告。"
    "只在本机使用请去掉 --host（默认 127.0.0.1）；确实要让局域网访问，请加 --allow-lan。"
)
LAN_WARNING = (
    "警告：控制台绑定在 {host}，不是本机回环地址。能打开这个地址的人都会拿到会话令牌，"
    "可以用你的订阅启动分析、读报告。只在可信网络里这样用，用完请关掉控制台。"
)


def lan_bind_check(host: str, allow_lan: bool) -> tuple[bool, str]:
    """W2: (may start, message for stderr). Loopback: (True, "")."""
    if is_loopback_host(host):
        return True, ""
    if not allow_lan:
        return False, LAN_REFUSED.format(host=host)
    return True, LAN_WARNING.format(host=host)


def console_help_text() -> str:
    """Stdout of ``python -m cli.launcher web --help`` (canonical catalog form)."""
    return build_console_parser().format_help()


def console_arg_error(argv: list[str]) -> str:
    """Stderr argparse writes for these exact ``web`` arguments. The values stay in the sentence."""
    import contextlib
    from io import StringIO

    parser = build_console_parser()
    err = StringIO()
    old = sys.stderr
    sys.stderr = err
    try:
        with contextlib.suppress(SystemExit):
            parser.parse_args(argv)
    finally:
        sys.stderr = old
    return err.getvalue()


BAD_HOST = "无法启动控制台：--host 不能为空。只在本机使用请去掉 --host（默认 127.0.0.1）。"
BIND_FAILED = ("无法启动控制台：不能在 {host} 端口 {port} 上监听（{reason}）。"
               "请检查 --host 是否是本机地址（例如 127.0.0.1 或 ::1），以及端口是否已被占用。")


def main(argv: list[str] | None = None) -> int:
    parser = build_console_parser()
    args = parser.parse_args(argv)
    try:
        host = normalize_bind_host(args.host)        # W89: ' ::1 ', '[::1]'
    except ValueError:
        print(BAD_HOST, file=sys.stderr, flush=True)
        return 2
    may_start, lan_message = lan_bind_check(host, args.allow_lan)
    if lan_message:
        print(lan_message, file=sys.stderr, flush=True)
    if not may_start:
        return 2
    # W89: stop signals and the lock release cover the whole start-up too, so a
    # failed bind or a SIGTERM/SIGHUP while recovering never leaves console.lock.
    previous = _stop_on_signals()
    httpd = None
    try:
        try:
            service.acquire_console_lock()  # W40: one console per home, before recovery touches the queue
        except service.ConsoleError as exc:
            print(str(exc), file=sys.stderr, flush=True)
            return 1
        try:
            httpd = serve(host, args.port)
        except (OSError, OverflowError, ValueError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
            print(BIND_FAILED.format(host=host, port=args.port, reason=reason), file=sys.stderr, flush=True)
            return 1
        start_run_dispatcher()
        print(f"控制台已启动：http://{url_host(host)}:{httpd.server_address[1]}", flush=True)
        httpd.serve_forever()
    except (KeyboardInterrupt, ConsoleStopped):
        print("控制台已停止。")
    finally:
        _restore_signals(previous)
        if httpd is not None:
            httpd.server_close()
        service.release_console_lock()  # W71/W89: on every path
    return 0


class ConsoleStopped(BaseException):
    """SIGTERM or SIGHUP (``kill``, a closed terminal): stop like Ctrl-C does.

    W104: inherits BaseException so ``contextlib.suppress(Exception)`` around
    startup recovery cannot swallow it — the console stops on the first signal.
    """


# W74: SIGTERM/SIGHUP end the console the same way as Ctrl-C, so the PID in
# console.lock is cleared too (the flock itself was always released by the OS).
_STOP_SIGNALS = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name))


def _raise_stopped(signum, frame):
    raise ConsoleStopped(signum)


def _stop_on_signals() -> dict:
    previous = {}
    for sig in _STOP_SIGNALS:
        with contextlib.suppress(ValueError, OSError):   # not the main thread: leave as is
            previous[sig] = signal.signal(sig, _raise_stopped)
    return previous


def _restore_signals(previous: dict) -> None:
    for sig, handler in previous.items():
        with contextlib.suppress(ValueError, OSError, TypeError):
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
