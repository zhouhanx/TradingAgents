"""python -m cli entry.

W109: Ctrl-C during import of the CLI ends with a short Chinese line. ``python -m
cli.main`` still imports cli.main as __main__ and may show a traceback if the
interrupt lands in that module's top-level imports; prefer ``python -m cli`` or
the installed ``tradingagents`` entry point.
"""

from __future__ import annotations

import sys

try:
    from cli.main import app
except KeyboardInterrupt:
    print("已取消。", file=sys.stderr, flush=True)
    raise SystemExit(130) from None

try:
    app(prog_name="tradingagents")
except KeyboardInterrupt:
    print("已取消。", file=sys.stderr, flush=True)
    raise SystemExit(130) from None
