#!/usr/bin/env bash
# Run from any directory without manually activating Conda.
# Usage: ./tradingagents.sh <start|stop|restart|status|logs|login|logout|auth-status|cli|web> [args]
set -euo pipefail

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

find_conda() {
    if [[ -n "${CONDA_EXE:-}" && -x "$CONDA_EXE" ]]; then
        printf '%s\n' "$CONDA_EXE"
        return
    fi
    if command -v conda >/dev/null 2>&1; then
        command -v conda
        return
    fi
    local candidate
    for candidate in \
        "$HOME/dev_env/conda/anaconda3/condabin/conda" \
        "$HOME/miniconda3/bin/conda" \
        "$HOME/anaconda3/bin/conda" \
        "$HOME/miniforge3/bin/conda" \
        /opt/miniconda3/bin/conda \
        /opt/anaconda3/bin/conda; do
        if [[ -x "$candidate" ]]; then
            printf '%s\n' "$candidate"
            return
        fi
    done
    return 1
}

if ! CONDA_BIN="$(find_conda)"; then
    printf '%s\n' '未找到 Conda。请安装 Conda，或将 CONDA_EXE 设置为 conda 可执行文件的完整路径。' >&2
    exit 1
fi

if ! CONDA_BASE="$("$CONDA_BIN" info --base)"; then
    printf '%s\n' '无法读取 Conda 安装目录。' >&2
    exit 1
fi
ENV_PYTHON="${CONDA_BASE}/envs/tradingagent/bin/python"
if [[ ! -x "$ENV_PYTHON" ]]; then
    printf '%s\n' '未找到 tradingagent 环境。请先在项目根目录执行 conda env create -f environment.yml。' >&2
    exit 1
fi

# PKG-001 / W102: run the entry point installed into the tradingagent environment
# (pip install -e ".[dev]" or pip install .), not the source tree on sys.path.
LAUNCHER="${CONDA_BASE}/envs/tradingagent/bin/tradingagents-launcher"
# W100: a directory also passes ``-x``; require a regular executable file.
if [[ ! -f "$LAUNCHER" || ! -x "$LAUNCHER" ]]; then
    printf '%s\n' "tradingagent 环境里还没有安装 TradingAgents。请在项目根目录执行：\"$ENV_PYTHON\" -m pip install -e \".[dev]\"" >&2
    exit 1
fi
# Keep the runtime state (status/stop/logs) keyed to this project directory, as
# before, wherever the installed package lives.
export TRADINGAGENTS_LAUNCHER_ROOT="$PROJECT_DIR"

# Keep OAuth loopback callbacks local while retaining existing proxy exclusions.
export NO_PROXY="${NO_PROXY:+${NO_PROXY},}localhost,127.0.0.1,::1"
export no_proxy="${no_proxy:+${no_proxy},}localhost,127.0.0.1,::1"

# Preserve the earlier direct-option form, e.g. ./tradingagents.sh --ticker NVDA ... .
case "${1:-}" in
    --help|-h) ;;
    -*) set -- start "$@" ;;
esac

# Exec the installed entry point directly so prompts, streaming output and the
# launcher's own exit codes are preserved. conda run would wrap a normal
# "not running" status as a Conda failure.
exec "$LAUNCHER" "$@"
