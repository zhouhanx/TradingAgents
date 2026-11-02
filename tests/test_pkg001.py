"""PKG-001: the WebUI ships in the wheel/sdist, the Node test files do not, and
tradingagents.sh runs the installed entry point.

The wheel and sdist are built from a copy of this checkout into a temporary
directory, installed into a throwaway venv, and the installed entry points are
run there with a temporary HOME. No model is called; the only server started is
the installed console on 127.0.0.1.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import pytest

from tests._model_server import allow_loopback_only  # imported before the network guard

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "webui" / "static"
NODE_FILES = ("webui/package.json", "webui/package-lock.json", "webui/node_modules", "webui/API-NOTES.md")
SCRIPTS = {"tradingagents": "cli.main:app", "tradingagents-launcher": "cli.launcher:main",
           "tradingagents-web": "webui.server:main"}

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the launcher and tradingagents.sh are Unix-only")


def _static_files():
    return sorted(p.relative_to(ROOT).as_posix() for p in STATIC.rglob("*") if p.is_file())


def _source_copy(dest: Path) -> Path:
    """The files a checkout would package: tracked plus untracked-but-not-ignored."""
    listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=ROOT,
                            capture_output=True, check=False)
    if listed.returncode == 0:
        names = [n for n in listed.stdout.decode().split("\0") if n]
    else:  # not a git checkout: everything but caches, build output and node_modules
        skip = {".git", "node_modules", "__pycache__", "build", "dist", ".awr", "results"}
        names = [p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*")
                 if p.is_file() and not skip & set(p.relative_to(ROOT).parts)]
    for name in names:
        source = ROOT / name
        if source.is_file() and "node_modules" not in Path(name).parts:
            (dest / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest / name)
    return dest


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools is not installed (pip install -e '.[dev]')")
    base = tmp_path_factory.mktemp("pkg001")
    source = _source_copy(base / "src")
    out = base / "dist"
    out.mkdir()
    for hook in ("build_sdist", "build_wheel"):
        code = f"import sys; from setuptools import build_meta as b; print(b.{hook}(sys.argv[1]))"
        built = subprocess.run([sys.executable, "-c", code, str(out)], cwd=source, capture_output=True,
                               text=True, timeout=600)
        assert built.returncode == 0, built.stderr[-2000:]
    wheel = next(out.glob("*.whl"))
    sdist = next(out.glob("*.tar.gz"))
    return wheel, sdist


@pytest.fixture(scope="module")
def venv(dist, tmp_path_factory):
    """A throwaway venv with only the built wheel installed (dependencies come from
    this interpreter's site-packages, listed after the venv's own)."""
    import site

    wheel, _ = dist
    root = tmp_path_factory.mktemp("venv") / "v"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True, timeout=120)
    python = root / "bin" / "python"
    purelib = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                             check=True, capture_output=True, text=True).stdout.strip()
    (Path(purelib) / "zz_pkg001_deps.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    if shutil.which("uv"):
        install = ["uv", "pip", "install", "--python", str(python)]
    elif importlib.util.find_spec("pip"):
        install = [sys.executable, "-m", "pip", "--python", str(python), "install"]
    else:
        pytest.skip("neither uv nor pip is available to install the wheel")
    subprocess.run([*install, "--no-deps", "--no-index", str(wheel)], check=True, capture_output=True,
                   text=True, timeout=300)
    return root


def _env(home, **extra):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "LANG": "C.UTF-8", "TERM": "dumb"}
    env.update(extra)
    return env


# ------------------------------------------------------------------ what is in the packages
def test_the_wheel_carries_the_webui_and_its_entry_points(dist):
    wheel, _ = dist
    names = set(zipfile.ZipFile(wheel).namelist())
    for name in ("webui/__init__.py", "webui/__main__.py", "webui/server.py", "webui/service.py",
                 "webui/worker.py", "cli/static/welcome.txt", *_static_files()):
        assert name in names, name
    for node in NODE_FILES:
        assert not any(n == node or n.startswith(node + "/") for n in names), node
    entry = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
    text = zipfile.ZipFile(wheel).read(entry).decode()
    for script, target in SCRIPTS.items():
        assert f"{script} = {target}" in text


def test_the_sdist_carries_the_webui_and_the_script_but_no_node_files(dist):
    _, sdist = dist
    with tarfile.open(sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    for name in ("webui/server.py", "tradingagents.sh", "MANIFEST.in", *_static_files()):
        assert name in names, name
    for node in NODE_FILES[:3]:
        assert not any(n == node or n.startswith(node + "/") for n in names), node
    assert not any("__pycache__" in n for n in names)


# ------------------------------------------------------------------ the installed package

def test_static_subdir_junk_is_not_packaged(tmp_path_factory):
    """W99: a planted static/node_modules/*.js must not enter the wheel or sdist."""
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools is not installed (pip install -e '.[dev]')")
    base = tmp_path_factory.mktemp("w99")
    source = _source_copy(base / "src")
    junk_dir = source / "webui" / "static" / "node_modules" / "evil"
    junk_dir.mkdir(parents=True)
    (junk_dir / "z.js").write_text("/* should not ship */\n")
    (source / "webui" / "static" / "sub" / "deep.js").parent.mkdir(parents=True, exist_ok=True)
    (source / "webui" / "static" / "sub" / "deep.js").write_text("/* nested js also out */\n")
    out = base / "dist"
    out.mkdir()
    for hook in ("build_sdist", "build_wheel"):
        code = f"import sys; from setuptools import build_meta as b; print(b.{hook}(sys.argv[1]))"
        built = subprocess.run([sys.executable, "-c", code, str(out)], cwd=source, capture_output=True,
                               text=True, timeout=600)
        assert built.returncode == 0, built.stderr[-2000:]
    wheel = next(out.glob("*.whl"))
    sdist = next(out.glob("*.tar.gz"))
    wnames = set(zipfile.ZipFile(wheel).namelist())
    assert not any("node_modules" in n for n in wnames), [n for n in wnames if "node_modules" in n]
    assert not any(n.endswith("static/sub/deep.js") or "/static/sub/" in n for n in wnames), wnames
    assert "webui/static/app.js" in wnames
    import tarfile
    with tarfile.open(sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    assert not any("node_modules" in n for n in names), [n for n in names if "node_modules" in n]
    assert "webui/static/sub/deep.js" not in names
    assert "webui/static/app.js" in names


def test_the_installed_package_is_what_runs(venv, tmp_path):
    code = "import cli, webui, tradingagents; print(cli.__file__); print(webui.__file__)"
    out = subprocess.run([str(venv / "bin" / "python"), "-c", code], cwd=tmp_path, env=_env(tmp_path),
                         capture_output=True, text=True, check=True, timeout=120).stdout.split()
    for path in out:
        assert Path(path).resolve().is_relative_to(venv.resolve()), path
    for script in SCRIPTS:
        assert os.access(venv / "bin" / script, os.X_OK), script


def test_the_installed_entry_points_answer_help(venv, tmp_path):
    launcher = subprocess.run([str(venv / "bin" / "tradingagents-launcher"), "--help"], cwd=tmp_path,
                              env=_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert launcher.returncode == 0
    assert launcher.stdout.startswith("usage: tradingagents-launcher ")
    assert "python -m cli.launcher" not in launcher.stdout.splitlines()[0]
    web = subprocess.run([str(venv / "bin" / "tradingagents-launcher"), "web", "--help"], cwd=tmp_path,
                         env=_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert web.returncode == 0
    assert web.stdout.startswith("usage: tradingagents-launcher web ")
    assert "--port" in web.stdout
    direct = subprocess.run([str(venv / "bin" / "tradingagents-web"), "--help"], cwd=tmp_path,
                            env=_env(tmp_path), capture_output=True, text=True, timeout=120)
    assert direct.returncode == 0
    assert direct.stdout.startswith("usage: tradingagents-web ")


def test_non_editable_analysis_uses_installed_entry_not_cwd_shadow(venv, tmp_path):
    """W103: wheel install must not run a poison ``cli/`` sitting in LAUNCHER_ROOT/cwd."""
    shadow = tmp_path / "project"
    (shadow / "cli").mkdir(parents=True)
    (shadow / "cli" / "__init__.py").write_text("")
    (shadow / "cli" / "main.py").write_text(
        "import sys\nprint('POISON', file=sys.stderr)\nraise SystemExit(99)\n"
    )
    marker = tmp_path / "analysis-cmd.txt"
    launcher_bin = str(venv / "bin" / "tradingagents-launcher")
    tradingagents_bin = str(venv / "bin" / "tradingagents")
    probe = (
        "import sys\n"
        f"sys.argv[0] = {launcher_bin!r}\n"
        "from cli.launcher import analysis_command, launcher_prog\n"
        "cmd = analysis_command(['--help'])\n"
        f"open({str(marker)!r}, 'w').write('\\n'.join(cmd))\n"
        "print(launcher_prog())\n"
        "print(cmd[0])\n"
    )
    # Probe from a clean cwd so the poison ``cli/`` does not shadow the import of
    # ``cli.launcher`` itself; the child command is what must ignore the shadow.
    out = subprocess.run([str(venv / "bin" / "python"), "-c", probe], cwd=str(tmp_path),
                         env=_env(tmp_path, TRADINGAGENTS_LAUNCHER_ROOT=str(shadow)),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[0] == "tradingagents-launcher"
    assert out.stdout.splitlines()[1] == tradingagents_bin
    cmd = marker.read_text().splitlines()
    assert cmd[0] == tradingagents_bin
    ran = subprocess.run(cmd, cwd=str(shadow), env=_env(tmp_path), capture_output=True,
                         text=True, timeout=120)
    assert ran.returncode == 0, ran.stderr
    assert "POISON" not in ran.stderr
    assert "Usage" in ran.stdout or "usage" in ran.stdout.lower() or "--ticker" in ran.stdout


def test_editable_analysis_still_resolves_via_sibling_to_project(venv, tmp_path_factory, tmp_path):
    """W103: editable install keeps sibling entry; edits in the project tree apply."""
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools is not installed")
    base = tmp_path_factory.mktemp("editable")
    source = _source_copy(base / "src")
    root = base / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root)], check=True, timeout=120)
    python = root / "bin" / "python"
    import site
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    (Path(purelib) / "zz_pkg001_deps.pth").write_text("\n".join(site.getsitepackages()) + "\n")
    if shutil.which("uv"):
        install = ["uv", "pip", "install", "--python", str(python)]
    elif importlib.util.find_spec("pip"):
        install = [sys.executable, "-m", "pip", "--python", str(python), "install"]
    else:
        pytest.skip("neither uv nor pip is available")
    subprocess.run([*install, "--no-deps", "-e", str(source)], check=True, capture_output=True,
                   text=True, timeout=300)
    stamp = source / "cli" / "_w103_editable_stamp.py"
    stamp.write_text("STAMP = 'editable-ok'\n")
    launcher_bin = str(root / "bin" / "tradingagents-launcher")
    tradingagents_bin = str(root / "bin" / "tradingagents")
    probe = (
        "import sys\n"
        f"sys.argv[0] = {launcher_bin!r}\n"
        "from cli.launcher import analysis_command\n"
        "print(analysis_command(['--help'])[0])\n"
        "import cli._w103_editable_stamp as s\n"
        "print(s.STAMP)\n"
    )
    out = subprocess.run([str(python), "-c", probe], cwd=tmp_path, env=_env(tmp_path),
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[0] == tradingagents_bin
    assert out.stdout.splitlines()[1] == "editable-ok"


def test_the_installed_console_serves_the_webui(venv, tmp_path, monkeypatch):
    allow_loopback_only(monkeypatch)
    process = subprocess.Popen([str(venv / "bin" / "tradingagents-web"), "--port", "0"], cwd=tmp_path,
                               env=_env(tmp_path, NO_PROXY="*"), stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    try:
        line = process.stdout.readline()
        port = re.search(r":(\d+)\s*$", line)
        assert port, line
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for path, needle in (("/", b'src="/app.js"'), ("/app.js", b"function"), ("/console.css", b"font-family"),
                             ("/icons/logo.svg", b"<svg")):
            with opener.open(f"http://127.0.0.1:{port.group(1)}{path}", timeout=10) as response:
                assert response.status == 200 and needle in response.read(), path
    finally:
        process.terminate()
        process.wait(timeout=15)


def test_tradingagents_sh_runs_the_installed_entry_point(venv, tmp_path):
    base = tmp_path / "conda"
    (base / "envs").mkdir(parents=True)
    (base / "envs" / "tradingagent").symlink_to(venv)
    fake = tmp_path / "conda-bin"
    fake.write_text(f"#!{sys.executable}\nprint({str(base)!r})\n")
    fake.chmod(0o700)
    env = _env(tmp_path, CONDA_EXE=str(fake), TRADINGAGENTS_RUNTIME_HOME=str(tmp_path / "rt"))
    web = subprocess.run(["bash", str(ROOT / "tradingagents.sh"), "web", "--help"], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=120)
    assert web.returncode == 0 and "--port" in web.stdout, web.stderr
    assert web.stdout.startswith("usage: tradingagents-launcher web "), web.stdout.splitlines()[:1]
    status = subprocess.run(["bash", str(ROOT / "tradingagents.sh"), "status"], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=120)
    assert "Traceback" not in status.stderr and status.stdout.strip(), status.stderr


# ------------------------------------------------------------------ offline: no external requests
URL = re.compile(r"""https?://[A-Za-z0-9.-]+|["'`(]//[A-Za-z0-9-]+\.[A-Za-z]""")


# V2-KLINE: the vendored chart library is checked on its own below (its license
# header names its upstream sites; it makes no request of its own).
@pytest.mark.parametrize("name", [n for n in _static_files()
                                  if n.endswith((".html", ".css", ".js")) and "/vendor/" not in n])
def test_static_files_make_no_external_requests(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert "@import" not in text
    assert "@font-face" not in text, "fonts are system stacks; a web font must be bundled, not fetched"
    for match in re.finditer(r"""url\(\s*['"]?([^'")]+)""", text):
        assert match.group(1).startswith(("/", "data:", "#")), match.group(0)
    for match in re.finditer(r"""(?:src|href)\s*=\s*["']([^"'$]+)""", text):
        assert match.group(1).startswith(("/", "#", "data:")), match.group(0)
    # The one allowed literal is the 服务地址 input's placeholder text.
    allowed = {"http://www.w3.org", "http://localhost"}
    found = [m.group(0) for m in URL.finditer(text) if m.group(0) not in allowed]
    assert found == [], found


def test_vendored_chart_library_names_only_its_license_and_home():
    text = (STATIC / "vendor" / "lightweight-charts.js").read_text(encoding="utf-8")
    assert sorted(set(URL.findall(text))) == ["http://www.w3.org", "https://www.apache.org", "https://www.tradingview.com"]
    assert "fetch(" not in text and "XMLHttpRequest" not in text


def test_font_stacks_end_in_a_generic_family():
    for name in ("webui/static/console.css", "webui/static/app.css"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for stack in re.findall(r"font-family:\s*([^;]+);", text):
            if stack.strip() == "inherit":
                continue
            assert stack.strip().split(",")[-1].strip() in {"sans-serif", "serif", "monospace", "system-ui"}, stack


def test_node_files_stay_out_of_the_package_config():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tool = config["tool"]["setuptools"]
    assert "webui*" in tool["packages"]["find"]["include"]
    assert {"package.json", "package-lock.json", "node_modules/*", "static/node_modules/*"} <= set(tool["exclude-package-data"]["webui"])
    # The static files are package data in their own right, not only via MANIFEST.in.
    assert {"static/*.html", "static/*.css", "static/*.js", "static/icons/*"} <= set(tool["package-data"]["webui"])
    assert json.dumps(config["project"]["scripts"], sort_keys=True) == json.dumps(SCRIPTS, sort_keys=True)
