"""Database access: seeding, read-only connections, and the write path.

The two connection factories here are the structural half of the safety story.
:func:`read_connection` opens the SQLite file with ``mode=ro`` in the URI, so a
statement that somehow got past :mod:`safe_db_mcp.validation` still could not
write - SQLite itself refuses. :func:`write_connection` opens a normal
read/write handle and is only ever reached from the propose/confirm path.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

#: Environment variable that overrides where the demo database lives.
DATABASE_PATH_ENV = "SAFEDB_DATABASE_PATH"

#: Default location, relative to the repository root: created on first run.
DEFAULT_DATABASE_PATH = Path("data") / "library.db"

#: The seed script shipped inside the package.
SCHEMA_PATH = Path(__file__).with_name("schema.sql")

#: Rows a single read may return before the result is truncated.
MAX_RESULT_ROWS = 200

#: Above this row count a table is too large to snapshot for an exact diff, and
#: the preview falls back to reporting rows affected only.
MAX_DIFF_TABLE_ROWS = 5000


def resolve_database_path() -> Path:
    """Return the database path, honouring ``SAFEDB_DATABASE_PATH``."""
    override = os.environ.get(DATABASE_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return DEFAULT_DATABASE_PATH


def ensure_database(path: Path | None = None) -> Path:
    """Create and seed the demo database if it is not there yet.

    Args:
        path: Where the database should live. Defaults to
            :func:`resolve_database_path`.

    Returns:
        The path to a database that exists and contains the seeded schema.
    """
    target = Path(path) if path is not None else resolve_database_path()
    if target.exists():
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    script = SCHEMA_PATH.read_text(encoding="utf-8")
    connection = sqlite3.connect(target)
    try:
        connection.executescript(script)
        connection.commit()
    finally:
        connection.close()
    return target


@contextmanager
def read_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a genuinely read-only connection to ``path``.

    The ``mode=ro`` URI flag is enforced by SQLite, not by this package, which
    is the point: the read path cannot write even if a validation rule were
    wrong.
    """
    uri = f"file:{Path(path).resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def write_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a read/write connection with manual transaction control.

    ``isolation_level=None`` turns off the sqlite3 module's implicit
    transaction handling so the propose and confirm paths can open, roll back
    and commit transactions explicitly and visibly.
    """
    connection = sqlite3.connect(Path(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


def list_table_names(connection: sqlite3.Connection) -> list[str]:
    """Return the user tables in the database, sorted, excluding SQLite's own."""
    rows = connection.execute(
        "SELECT name FROM sqlite_schema "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def table_row_count(connection: sqlite3.Connection, table: str) -> int:
    """Return the number of rows in ``table``.

    The table name is interpolated because SQLite cannot parameterise an
    identifier. It is safe here only because every caller passes a name that
    came out of :func:`list_table_names`, never out of user input.
    """
    if table not in list_table_names(connection):
        raise ValueError(f"Unknown table '{table}'.")
    return int(connection.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"])


def snapshot_table(connection: sqlite3.Connection, table: str) -> dict[int, dict[str, object]]:
    """Return ``{rowid: row}`` for every row in ``table``.

    Used on both sides of an uncommitted write so the preview can report which
    rows were added, removed or changed rather than only how many.
    """
    if table not in list_table_names(connection):
        raise ValueError(f"Unknown table '{table}'.")
    rows = connection.execute(f'SELECT rowid AS _rowid, * FROM "{table}"').fetchall()
    snapshot: dict[int, dict[str, object]] = {}
    for row in rows:
        record = {key: row[key] for key in row.keys() if key != "_rowid"}
        snapshot[int(row["_rowid"])] = record
    return snapshot


def describe_columns(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    """Return the column definitions of ``table``.

    ``PRAGMA table_info`` is a read the server performs on its own behalf. It
    is never reachable from tool input: :mod:`safe_db_mcp.validation` refuses
    ``PRAGMA`` outright, and ``table`` here is checked against the real table
    list first.
    """
    if table not in list_table_names(connection):
        raise ValueError(f"Unknown table '{table}'.")
    rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [
        {
            "name": row["name"],
            "type": row["type"] or "ANY",
            "not_null": bool(row["notnull"]),
            "default": row["dflt_value"],
            "primary_key": bool(row["pk"]),
        }
        for row in rows
    ]


def foreign_keys(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    """Return the foreign keys declared on ``table``."""
    if table not in list_table_names(connection):
        raise ValueError(f"Unknown table '{table}'.")
    rows = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
    return [{"column": row["from"], "references": f"{row['table']}.{row['to']}"} for row in rows]


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, object]]:
    """Convert sqlite3 rows into plain JSON-serialisable dictionaries."""
    return [{key: row[key] for key in row.keys()} for row in rows]
