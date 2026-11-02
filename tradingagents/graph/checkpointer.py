"""LangGraph checkpoint support for resumable analysis runs.

Per-ticker SQLite databases so concurrent tickers don't contend.

A run can also pass a ``scope`` (the console uses its run id): its database
then lives at ``checkpoints/runs/<scope>/<TICKER>.db``, so two runs of the
same ticker at the same time never share a thread (RUN-004). Without a scope
the path is the original ``checkpoints/<TICKER>.db``, which the CLI keeps using.

The databases hold analysis state and, when a node fails, LangGraph's
``__error__`` write. That error text is redacted before it is stored, and the
directory and files are private to the user (0700 / 0600) (RUN-002 W18).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from tradingagents.dataflows.symbols import safe_ticker_component
from tradingagents.redaction import redact_text

ERROR_CHANNEL = "__error__"
DIR_MODE = 0o700
FILE_MODE = 0o600
# A stored error keeps at most this much text; it is cut before it is redacted (W28).
ERROR_LIMIT = 4000


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, DIR_MODE)
    return path


def _private_files(db: Path) -> None:
    """Make the database and its SQLite side files (-wal, -shm, -journal) 0600."""
    for path in (db, *db.parent.glob(f"{db.name}-*")):
        with contextlib.suppress(OSError):
            os.chmod(path, FILE_MODE)


def _create_private(db: Path) -> None:
    """Create ``db`` empty with 0600 so SQLite's side files inherit that mode."""
    if db.exists():
        return
    with contextlib.suppress(FileExistsError):
        os.close(os.open(db, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE))


def redact_error(value: Any) -> str:
    """The text a failed node's ``__error__`` write keeps: redacted, never raw."""
    text = repr(value) if isinstance(value, BaseException) else str(value)
    return redact_text(text, ERROR_LIMIT)


class RedactingSqliteSaver(SqliteSaver):
    """A SqliteSaver whose ``__error__`` writes are redacted before they are stored."""

    def put_writes(
        self,
        config,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        cleaned = [
            (channel, redact_error(value) if channel == ERROR_CHANNEL else value)
            for channel, value in writes
        ]
        super().put_writes(config, cleaned, task_id, task_path)

    def scrub_stored_errors(self) -> None:
        """Redact ``__error__`` rows written before this saver existed."""
        with self.cursor() as cur:
            rows = cur.execute(
                "SELECT rowid, type, value FROM writes WHERE channel = ?", (ERROR_CHANNEL,)
            ).fetchall()
            for rowid, kind, value in rows:
                try:
                    text = redact_error(self.serde.loads_typed((kind, value)))
                    new_kind, new_value = self.serde.dumps_typed(text)
                except Exception:
                    cur.execute("DELETE FROM writes WHERE rowid = ?", (rowid,))
                    continue
                if (new_kind, new_value) != (kind, value):
                    cur.execute(
                        "UPDATE writes SET type = ?, value = ? WHERE rowid = ?",
                        (new_kind, new_value, rowid),
                    )


RUNS_DIR = "runs"
_SCOPE_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def safe_scope(scope: str) -> str:
    """A run scope is one plain path component: letters, digits, ``_`` and ``-``."""
    text = str(scope or "")
    if not text or len(text) > 64 or not set(text) <= _SCOPE_OK:
        raise ValueError(f"invalid checkpoint scope: {scope!r}")
    return text


def scope_dir(data_dir: str | Path, scope: str) -> Path:
    """The directory holding one run's checkpoint databases (not created)."""
    return Path(data_dir) / "checkpoints" / RUNS_DIR / safe_scope(scope)


def _db_path(data_dir: str | Path, ticker: str, scope: str | None = None) -> Path:
    """Return the SQLite checkpoint DB path for a ticker (and run scope, when given)."""
    # Reject ticker values that would escape the checkpoints directory.
    safe = safe_ticker_component(ticker).upper()
    p = _private_dir(Path(data_dir) / "checkpoints")
    if scope:
        p = _private_dir(_private_dir(p / RUNS_DIR) / safe_scope(scope))
    return p / f"{safe}.db"


def db_file(data_dir: str | Path, ticker: str, scope: str | None = None) -> Path:
    """Where the checkpoint database for ``ticker`` (and ``scope``) is, without creating it."""
    safe = safe_ticker_component(ticker).upper()
    base = Path(data_dir) / "checkpoints"
    return (scope_dir(data_dir, scope) if scope else base) / f"{safe}.db"


def copy_checkpoint_db(source: Path, data_dir: str | Path, ticker: str, scope: str) -> bool:
    """Seed run ``scope``'s database from ``source`` with SQLite's backup API.

    The source is only read, so a legacy per-ticker database or another run's
    database is never changed by the run that resumes from it. Returns False
    when there is nothing to copy or the run already has its own database.
    """
    source = Path(source)
    if not source.is_file():
        return False
    target = _db_path(data_dir, ticker, scope)
    if target.exists() and target.stat().st_size:
        return False
    _create_private(target)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
        _private_files(target)
    return True


def thread_id(ticker: str, date: str, signature: str = "") -> str:
    """Deterministic thread ID for a ticker+date pair.

    ``signature`` folds in graph-shape-affecting run choices so a resume under a
    different graph can't reuse this checkpoint (#1089); omitting it keeps the
    legacy ID.
    """
    base = f"{ticker.upper()}:{date}"
    if signature:
        base = f"{base}:{signature}"
    return hashlib.sha256(base.encode()).hexdigest()[:16]


@contextmanager
def get_checkpointer(data_dir: str | Path, ticker: str, scope: str | None = None) -> Generator[SqliteSaver, None, None]:
    """Context manager yielding a redacting SqliteSaver backed by a private per-ticker DB."""
    db = _db_path(data_dir, ticker, scope)
    _create_private(db)
    conn = sqlite3.connect(str(db), check_same_thread=False)
    try:
        saver = RedactingSqliteSaver(conn)
        saver.setup()
        saver.scrub_stored_errors()
        _private_files(db)
        yield saver
    finally:
        conn.close()
        _private_files(db)


def checkpoint_step(data_dir: str | Path, ticker: str, date: str, signature: str = "",
                    scope: str | None = None) -> int | None:
    """Return the step number of the latest checkpoint, or None if none exists."""
    db = _db_path(data_dir, ticker, scope)
    if not db.exists():
        return None
    tid = thread_id(ticker, date, signature)
    with get_checkpointer(data_dir, ticker, scope) as saver:
        config = {"configurable": {"thread_id": tid}}
        cp = saver.get_tuple(config)
        if cp is None:
            return None
        return cp.metadata.get("step")


def clear_all_checkpoints(data_dir: str | Path) -> int:
    """Remove all checkpoint databases. Returns the number of databases deleted.

    SQLite keeps committed state in ``-wal`` and ``-shm`` files beside the
    database, so deleting only the ``.db`` leaves a cleared checkpoint with data
    still on disk.
    """
    cp_dir = Path(data_dir) / "checkpoints"
    if not cp_dir.exists():
        return 0
    dbs = list(cp_dir.glob("*.db"))
    for db in dbs:
        for path in (db, *cp_dir.glob(f"{db.name}-*")):
            path.unlink(missing_ok=True)
    return len(dbs)


def clear_run_checkpoints(data_dir: str | Path, keep=frozenset()) -> int:
    """Remove per-run checkpoint directories except the scopes in ``keep``.

    Returns the number of databases deleted. ``clear_all_checkpoints`` only ever
    touches the per-ticker databases, so a live run's own database is safe from it.
    """
    root = Path(data_dir) / "checkpoints" / RUNS_DIR
    if not root.is_dir():
        return 0
    count = 0
    for folder in root.iterdir():
        if not folder.is_dir() or folder.is_symlink() or folder.name in keep:
            continue
        for path in folder.iterdir():
            if path.name.endswith(".db"):
                count += 1
            path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            folder.rmdir()
    return count


def clear_checkpoint(data_dir: str | Path, ticker: str, date: str, signature: str = "",
                     scope: str | None = None) -> None:
    """Remove checkpoint for a specific ticker+date by deleting the thread's rows."""
    db = _db_path(data_dir, ticker, scope)
    if not db.exists():
        return
    tid = thread_id(ticker, date, signature)
    conn = sqlite3.connect(str(db))
    try:
        for table in ("writes", "checkpoints"):
            conn.execute(f"DELETE FROM {table} WHERE thread_id = ?", (tid,))
        conn.commit()
    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()
