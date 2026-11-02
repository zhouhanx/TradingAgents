"""python -m webui entry.

W109: Ctrl-C during the (large) import of the console ends with a short Chinese
line instead of a KeyboardInterrupt traceback. A Ctrl-C that lands inside the
interpreter before this file runs still shows Python's own message — that case
cannot be wrapped from here.
"""

from __future__ import annotations

import sys

try:
    from webui.server import main
except KeyboardInterrupt:
    print("已取消。", file=sys.stderr, flush=True)
    raise SystemExit(130) from None

try:
    raise SystemExit(main())
except KeyboardInterrupt:
    print("已取消。", file=sys.stderr, flush=True)
    raise SystemExit(130) from None
