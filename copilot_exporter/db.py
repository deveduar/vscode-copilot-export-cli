"""Safe read-only access to VS Code ``state.vscdb`` databases."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Tuple

_CHAT_KEY_SQL = """
SELECT key, value
FROM itemTable
WHERE key IN (
    'memento/interactive-session',
    'memento/interactive-session-view-copilot',
    'interactive.sessions',
    'chat.ChatSessionStore.index',
    'chat.ChatSessionStore.sessions',
    'workbench.panel.chat.state'
)
OR key LIKE 'chat.%'
OR key LIKE 'chat/%'
OR key LIKE 'memento/interactive%'
OR key LIKE 'interactive.%'
OR key LIKE '%ChatSessionStore%'
OR key LIKE 'workbench.panel.chat%'
ORDER BY key
"""


class DatabaseError(Exception):
    """Raised when a database cannot be read."""


def _connect_readonly(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def _copy_with_sidecars(source: Path, destination: Path) -> None:
    shutil.copy2(source, destination)
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, destination.with_name(destination.name + suffix))


def _read_rows(connection: sqlite3.Connection) -> List[Tuple[str, str]]:
    cursor = connection.execute(_CHAT_KEY_SQL)
    rows: List[Tuple[str, str]] = []
    for key, value in cursor:
        if value is None:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        elif not isinstance(value, str):
            value = str(value)
        rows.append((str(key), value))
    return rows


@contextmanager
def _opened(path: Path) -> Iterator[sqlite3.Connection]:
    connection = _connect_readonly(path)
    try:
        yield connection
    finally:
        connection.close()


def read_chat_rows(path: Path) -> List[Tuple[str, str]]:
    """Read chat-related key/value rows using a hot-safe strategy.

    The live database is opened in read-only mode. If VS Code holds a lock that
    prevents even a read, a temporary copy (including WAL/SHM sidecars) is made
    and read instead.
    """

    try:
        with _opened(path) as connection:
            return _read_rows(connection)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as first_error:
        try:
            with tempfile.TemporaryDirectory(prefix="copilot-export-") as tmp:
                copy_path = Path(tmp) / "state.vscdb"
                _copy_with_sidecars(path, copy_path)
                with _opened(copy_path) as connection:
                    return _read_rows(connection)
        except (OSError, sqlite3.DatabaseError) as second_error:
            raise DatabaseError(
                f"cannot read database {path}: {first_error}; snapshot failed: {second_error}"
            ) from second_error


def _read_agent_session_rows(connection: sqlite3.Connection) -> dict:
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    rows = {}
    if "session_metadata" in tables:
        rows["session_metadata"] = list(
            connection.execute("SELECT key, value FROM session_metadata")
        )
    if "turns" in tables:
        rows["turns"] = list(connection.execute("SELECT id FROM turns ORDER BY rowid"))
    if "assistant_response_parts" in tables:
        rows["assistant_response_parts"] = list(
            connection.execute(
                "SELECT turn_id, parts FROM assistant_response_parts ORDER BY rowid"
            )
        )
    if "terminal_outputs" in tables:
        rows["terminal_outputs"] = list(
            connection.execute(
                "SELECT tool_call_id, turn_id, output FROM terminal_outputs ORDER BY rowid"
            )
        )
    if "tool_result_resources" in tables:
        rows["tool_result_resources"] = list(
            connection.execute(
                "SELECT tool_call_id, turn_id, kind, name, mime_type, content "
                "FROM tool_result_resources ORDER BY ordinal"
            )
        )
    return rows


def read_agent_session_rows(path: Path) -> dict:
    """Read Copilot agent-session tables without modifying VS Code storage."""

    try:
        with _opened(path) as connection:
            return _read_agent_session_rows(connection)
    except (sqlite3.OperationalError, sqlite3.DatabaseError) as first_error:
        try:
            with tempfile.TemporaryDirectory(prefix="copilot-export-") as tmp:
                copy_path = Path(tmp) / "session.db"
                _copy_with_sidecars(path, copy_path)
                with _opened(copy_path) as connection:
                    return _read_agent_session_rows(connection)
        except (OSError, sqlite3.DatabaseError) as second_error:
            raise DatabaseError(
                f"cannot read agent session database {path}: {first_error}; "
                f"snapshot failed: {second_error}"
            ) from second_error
