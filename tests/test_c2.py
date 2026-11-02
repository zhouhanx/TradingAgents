"""C2 (v2 warnings): W99, W100, W105, W106, W110, W111.

Packaging and host-normalization checks; no real model calls, no .env, no auth.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from webui import server

pytestmark = pytest.mark.skipif(os.name != "posix", reason="tradingagents.sh and packaging checks are Unix-only")
ROOT = Path(__file__).resolve().parents[1]


def _env(home: Path, **extra):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LANG": "C.UTF-8", "TERM": "dumb"}
    env.update({k: str(v) for k, v in extra.items()})
    return env


# ------------------------------------------------------------------ W105 / W106
@pytest.mark.parametrize(
    "given, bound, printed",
    [
        ("[[::1]]", "::1", "[::1]"),
        ("[[[::1]]]", "::1", "[::1]"),
        ("[[ ::1 ]]", "::1", "[::1]"),
        ("１２７.０.０.１", "127.0.0.1", "127.0.0.1"),
        ("１２７.０００.０００.１１", "127.000.000.11", "127.000.000.11"),
        ("０.０.０.０", "0.0.0.0", "0.0.0.0"),
        ("［１２７.０.０.１］", "127.0.0.1", "127.0.0.1"),
    ],
)
def test_normalize_bind_host_strips_brackets_and_nfkc(given, bound, printed):
    assert server.normalize_bind_host(given) == bound
    assert server.url_host(bound) == printed


def test_double_bracket_ipv6_prints_a_usable_url(monkeypatch, tmp_path):
    """W105: [[::1]] starts and the banner uses http://[::1]:port."""
    import socket

    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        s.bind(("::1", 0))
        s.close()
    except OSError:
        pytest.skip("IPv6 loopback unavailable")

    monkeypatch.setenv("TRADINGAGENTS_WEBUI_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    # Avoid holding the real console lock across tests if any linger.
    from webui import service
    monkeypatch.setattr(service, "acquire_console_lock", lambda: None)
    monkeypatch.setattr(service, "release_console_lock", lambda: None)
    monkeypatch.setattr(server, "start_run_dispatcher", lambda: None)

    host = server.normalize_bind_host("[[::1]]")
    assert host == "::1"
    httpd = server.serve(host, 0)
    try:
        port = httpd.server_address[1]
        banner = f"控制台已启动：http://{server.url_host(host)}:{port}"
        assert banner == f"控制台已启动：http://[::1]:{port}"
        assert "[[" not in banner
    finally:
        httpd.server_close()


def test_fullwidth_loopback_is_loopback_after_nfkc():
    """W106: full-width 127.0.0.1 is ASCII loopback after normalize."""
    host = server.normalize_bind_host("１２７.０.０.１")
    assert host == "127.0.0.1"
    assert server.is_loopback_host(host)
    ok, msg = server.lan_bind_check(host, allow_lan=False)
    assert ok and msg == ""


def test_fullwidth_lan_host_with_allow_lan_prints_ascii():
    host = server.normalize_bind_host("１９２.１６８.１.１０")
    assert host == "192.168.1.10"
    assert server.url_host(host) == "192.168.1.10"
    ok, msg = server.lan_bind_check(host, allow_lan=True)
    assert ok and "192.168.1.10" in msg and "１９２" not in msg


# ------------------------------------------------------------------ W100
def test_tradingagents_sh_refuses_a_directory_launcher(tmp_path):
    base = tmp_path / "conda"
    bindir = base / "envs" / "tradingagent" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").write_text("#!/bin/sh\n")
    (bindir / "python").chmod(0o700)
    # Directory named like the entry point: -x is true, -f is false (W100).
    (bindir / "tradingagents-launcher").mkdir()
    fake = tmp_path / "conda-bin"
    fake.write_text(f"#!{sys.executable}\nprint({str(base)!r})\n")
    fake.chmod(0o700)
    result = subprocess.run(
        ["bash", str(ROOT / "tradingagents.sh"), "status"],
        cwd=tmp_path,
        env={**os.environ, "CONDA_EXE": str(fake), "HOME": str(tmp_path)},
        text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 1
    assert "Is a directory" not in result.stderr
    assert "还没有安装 TradingAgents" in result.stderr
    assert 'pip install -e ".[dev]"' in result.stderr


# ------------------------------------------------------------------ W110
def test_readme_install_uses_dev_extra():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert 'pip install -e ".[dev]"' in text
    # bare install line for the primary howto must not remain
    assert "\npip install .\n" not in text


# ------------------------------------------------------------------ W99 helpers (reuse pkg001 build when available)
def test_manifest_in_has_no_recursive_static_js_glob():
    text = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "recursive-include webui/static *.js" not in text
    assert "prune webui/static/node_modules" in text
    assert "include webui/static/*.js" in text or "webui/static/*.js" in text
