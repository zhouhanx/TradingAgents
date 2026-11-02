"""Verify the public shell entry point without invoking Conda or a model."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "posix", reason="tradingagents.sh is a Unix entry point")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("args,forwarded", [
    ([], []),
    (["start", "--background", "--ticker", "NVDA"], ["start", "--background", "--ticker", "NVDA"]),
    (["restart", "--portfolio", "my book.json"], ["restart", "--portfolio", "my book.json"]),
    (["stop"], ["stop"]),
    (["status"], ["status"]),
    (["login", "grok"], ["login", "grok"]),
    (["logout", "chatgpt"], ["logout", "chatgpt"]),
    (["--ticker", "NVDA"], ["start", "--ticker", "NVDA"]),
    (["--help"], ["--help"]),
])
def test_script_selects_conda_and_preserves_arguments(tmp_path, args, forwarded):
    base = tmp_path / "conda"
    fake = tmp_path / "fake conda"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"expected = {str(base)!r}\n"
        "if sys.argv[1:3] != ['info', '--base']:\n"
        "    raise SystemExit('unexpected conda invocation: ' + ' '.join(sys.argv[1:]))\n"
        "print(expected)\n"
    )
    fake.chmod(0o700)
    env_python = base / "envs" / "tradingagent" / "bin" / "python"
    env_python.parent.mkdir(parents=True)
    env_python.write_text("#!/bin/sh\nexit 99\n")   # PKG-001: never run directly any more
    env_python.chmod(0o700)
    launcher = env_python.parent / "tradingagents-launcher"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'program': sys.argv[0], 'args': sys.argv[1:], 'cwd': os.getcwd(), "
        "'root': os.environ.get('TRADINGAGENTS_LAUNCHER_ROOT'), "
        "'proxy': os.environ['NO_PROXY'], 'lower_proxy': os.environ['no_proxy']}))\n"
    )
    launcher.chmod(0o700)
    env = {**os.environ, "CONDA_EXE": str(fake), "NO_PROXY": "existing.example", "no_proxy": "lower.example"}
    result = subprocess.run(
        ["bash", str(ROOT / "tradingagents.sh"), *args], cwd=tmp_path, env=env,
        text=True, capture_output=True, timeout=10, check=True,
    )
    actual = json.loads(result.stdout)
    assert Path(actual["program"]) == launcher        # the installed entry point (PKG-001)
    assert actual["args"] == forwarded
    assert Path(actual["cwd"]).resolve() == ROOT
    assert Path(actual["root"]).resolve() == ROOT     # runtime state stays keyed to this checkout
    assert actual["proxy"] == "existing.example,localhost,127.0.0.1,::1"
    assert actual["lower_proxy"] == "lower.example,localhost,127.0.0.1,::1"


def test_script_reports_missing_environment(tmp_path):
    fake = tmp_path / "conda"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"print({str(tmp_path / 'base')!r})\n"
    )
    fake.chmod(0o700)
    result = subprocess.run(
        ["bash", str(ROOT / "tradingagents.sh"), "status"],
        cwd=tmp_path,
        env={**os.environ, "CONDA_EXE": str(fake)},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 1
    assert "tradingagent" in result.stderr


def test_script_asks_for_the_install_when_the_entry_point_is_missing(tmp_path):
    base = tmp_path / "base"
    env_python = base / "envs" / "tradingagent" / "bin" / "python"
    env_python.parent.mkdir(parents=True)
    env_python.write_text("#!/bin/sh\necho ran-the-source-tree\n")
    env_python.chmod(0o700)
    fake = tmp_path / "conda"
    fake.write_text(f"#!{sys.executable}\nprint({str(base)!r})\n")
    fake.chmod(0o700)
    result = subprocess.run(
        ["bash", str(ROOT / "tradingagents.sh"), "status"], cwd=tmp_path,
        env={**os.environ, "CONDA_EXE": str(fake)}, text=True, capture_output=True, timeout=10, check=False,
    )
    assert result.returncode == 1
    assert "ran-the-source-tree" not in result.stdout
    assert "还没有安装 TradingAgents" in result.stderr and 'pip install -e ".[dev]"' in result.stderr
