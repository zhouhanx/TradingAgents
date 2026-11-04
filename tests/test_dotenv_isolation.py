"""AUDIT-FIX-02: no test may reach the real project or home .env."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import cli.prompts as prompts
import webui.service as service
from tests.conftest import _PROJECT_ROOT, _real_dotenv_paths


def _fake_prompt(value):
    return type("P", (), {"ask": staticmethod(lambda: value)})()


@pytest.mark.parametrize("nested", [False, True])
def test_key_prompt_writes_only_in_cwd_even_below_a_parent_env(monkeypatch, tmp_path, nested):
    # Parent .env stands in for the real project one; tmp_path inside it is the
    # layout that leaked the fake key before (TMPDIR under the project).
    parent_env = tmp_path / ".env"
    parent_env.write_text("DEEPSEEK_API_KEY=\n")
    work = tmp_path / "a" / "b" if nested else tmp_path / "w"
    work.mkdir(parents=True)
    monkeypatch.chdir(work)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with patch.object(prompts.questionary, "password", return_value=_fake_prompt("sk-iso")):
        prompts.ensure_api_key("deepseek")
    assert parent_env.read_text() == "DEEPSEEK_API_KEY=\n"
    assert "sk-iso" in (work / ".env").read_text()


@pytest.mark.parametrize("module", [prompts, service])
def test_find_dotenv_never_walks_up(monkeypatch, tmp_path, module):
    (tmp_path / ".env").write_text("X=1\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert module.find_dotenv(usecwd=True) == ""


@pytest.mark.parametrize("module", [prompts, service])
def test_set_key_refuses_the_real_project_env(module):
    target = _PROJECT_ROOT / ".env"
    before = target.read_bytes() if target.exists() else None
    with pytest.raises(AssertionError, match="real config"):
        module.set_key(str(target), "AUDIT_GUARD_PROBE", "x")
    assert (target.read_bytes() if target.exists() else None) == before


def test_real_paths_cover_project_and_home():
    names = {p.name for p in _real_dotenv_paths()}
    assert names == {".env", ".env.enterprise"}
    assert (_PROJECT_ROOT / ".env").resolve() in _real_dotenv_paths()
