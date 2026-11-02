"""TradingAgents command line.

CLI-001 / C1: a terminal size of 0 (COLUMNS=0 / LINES=0, as some shells export)
means "not set", not "zero characters wide": rich and typer would otherwise print
every line as empty. It is dropped here, before any console is created, so
auth-status, --help and every console.print fall back to the real terminal size
(or 80x25 when piped). Invalid, negative, signed-empty, or absurdly large values
are treated the same way (W95 / W97).
"""

from __future__ import annotations

import os

# Rich builds a " " * width segment; a width past a C index overflows (W95).
_MAX_TERMINAL_SIZE = 4096


def _drop_unusable_terminal_size(environ=os.environ) -> None:
    for name in ("COLUMNS", "LINES"):
        value = environ.get(name)
        if value is None:
            continue
        try:
            # W97: '+40' and ' 40 ' both normalize to 40; rich only accepts plain digits.
            size = int(value.strip())
        except (TypeError, ValueError):
            environ.pop(name, None)
            continue
        if size <= 0 or size > _MAX_TERMINAL_SIZE:
            environ.pop(name, None)
        else:
            environ[name] = str(size)


_drop_unusable_terminal_size()
