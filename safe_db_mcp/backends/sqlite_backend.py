"""The SQLite backend: the zero-setup default.

No server, no credentials, no network. SQLite is an embedded library, so
"connecting" is opening a file, and the demo database seeds itself from
``schema.sql`` on first use. That is what keeps clone-and-run honest.

The read path opens the file with ``mode=ro`` in the URI, which SQLite itself
enforces, so a read connection cannot write even if a validation rule were
wrong. The write path opens a normal handle with ``isolation_level=None`` so the
propose and confirm steps can issue ``BEGIN``, ``ROLLBACK`` and ``COMMIT``
explicitly rather than relying on the driver's implicit transaction handling.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ..validation import ValidatedStatement
from .base import Backend, BackendError, PreviewChanged
from .diffing import Snapshot, build_diff, comparable

#: Environment variable that overrides where the demo database lives.
DATABASE_PATH_ENV = "SAFEDB_DATABASE_PATH"

#: Default location, relative to the working directory: created on first run.
DEFAULT_DATABASE_PATH = Path("data") / "library.db"

#: The seed script shipped inside the package.
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

#: Rows a single read may return before the result is truncated.
MAX_RESULT_ROWS = 200

#: Above this row count a table is too large to snapshot for an exact diff.
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
    connection = sqlite3.connect(target)
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
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
    """Open a read/write connection with manual transaction control."""
    connection = sqlite3.connect(Path(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


def _table_names(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_schema "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [row["name"] for row in rows]


def _require_table(connection: sqlite3.Connection, table: str) -> None:
    if table not in _table_names(connection):
        raise ValueError(f"Unknown table '{table}'.")


def _columns(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    _require_table(connection, table)
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


def _row_count(connection: sqlite3.Connection, table: str) -> int:
    _require_table(connection, table)
    return int(connection.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"])


def _snapshot(connection: sqlite3.Connection, table: str) -> Snapshot:
    """Return ``{rowid: row}`` for every row in ``table``.

    Matching on SQLite's own ``rowid`` is what makes an ``UPDATE`` show up as a
    changed row rather than as a delete plus an insert.
    """
    _require_table(connection, table)
    rows = connection.execute(f'SELECT rowid AS _rowid, * FROM "{table}"').fetchall()
    return {
        int(row["_rowid"]): {key: row[key] for key in row.keys() if key != "_rowid"} for row in rows
    }


class SqliteBackend(Backend):
    """A SQLite file, served through validated operations only."""

    name = "sqlite"

    def __init__(self, path: Path | str | None = None) -> None:
        """Open, and if necessary seed, the database file.

        Args:
            path: Location of the SQLite file. Defaults to
                ``SAFEDB_DATABASE_PATH`` or ``data/library.db``.
        """
        self.path = ensure_database(Path(path) if path is not None else None)

    @property
    def description(self) -> str:
        """The database file this backend is serving."""
        return str(self.path)

    def table_names(self) -> list[str]:
        with read_connection(self.path) as connection:
            return _table_names(connection)

    def list_tables(self) -> list[dict[str, Any]]:
        with read_connection(self.path) as connection:
            return [
                {
                    "name": name,
                    "rows": _row_count(connection, name),
                    "columns": [column["name"] for column in _columns(connection, name)],
                }
                for name in _table_names(connection)
            ]

    def describe_table(self, table: str) -> dict[str, Any]:
        with read_connection(self.path) as connection:
            known = _table_names(connection)
            if table not in known:
                raise ValueError(f"Unknown table '{table}'. Tables: {', '.join(known)}.")
            keys = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
            return {
                "table": table,
                "rows": _row_count(connection, table),
                "columns": _columns(connection, table),
                "foreign_keys": [
                    {"column": row["from"], "references": f"{row['table']}.{row['to']}"}
                    for row in keys
                ],
            }

    def run_query(self, statement: ValidatedStatement) -> dict[str, Any]:
        try:
            with read_connection(self.path) as connection:
                cursor = connection.execute(statement.sql)
                rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
                columns = [d[0] for d in cursor.description] if cursor.description else []
        except sqlite3.Error as error:
            raise BackendError(f"SQLite refused the query: {error}") from error

        truncated = len(rows) > MAX_RESULT_ROWS
        return {
            "sql": statement.sql,
            "columns": columns,
            "rows": [{key: row[key] for key in row.keys()} for row in rows[:MAX_RESULT_ROWS]],
            "row_count": min(len(rows), MAX_RESULT_ROWS),
            "truncated": truncated,
        }

    def _key_columns(self, connection: sqlite3.Connection, table: str) -> list[str]:
        return [c["name"] for c in _columns(connection, table) if c["primary_key"]]

    def _run_and_snapshot(
        self, connection: sqlite3.Connection, statement: ValidatedStatement
    ) -> dict[str, Any]:
        """Execute inside an open transaction and build the preview payload."""
        table = statement.table
        assert table is not None

        diffable = _row_count(connection, table) <= MAX_DIFF_TABLE_ROWS
        before = _snapshot(connection, table) if diffable else {}
        rows_affected = connection.execute(statement.sql).rowcount
        after = _snapshot(connection, table) if diffable else {}

        return {
            "table": table,
            "operation": statement.operation,
            "rows_affected": rows_affected,
            "diff": build_diff(before, after) if diffable else None,
            "diff_available": diffable,
            "key_columns": self._key_columns(connection, table) if diffable else [],
        }

    def preview_write(self, statement: ValidatedStatement) -> dict[str, Any]:
        try:
            with write_connection(self.path) as connection:
                connection.execute("BEGIN")
                try:
                    return self._run_and_snapshot(connection, statement)
                finally:
                    # Unconditional: a preview never persists, whether the
                    # statement succeeded, failed or raised.
                    connection.execute("ROLLBACK")
        except sqlite3.Error as error:
            raise BackendError(
                f"SQLite refused the change, nothing was written: {error}"
            ) from error

    def commit_write(self, statement: ValidatedStatement, approved: dict[str, Any]) -> int:
        try:
            with write_connection(self.path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    fresh = self._run_and_snapshot(connection, statement)
                    if comparable(fresh) != comparable(approved):
                        connection.execute("ROLLBACK")
                        raise PreviewChanged(
                            "The data changed since this change was proposed, so the preview "
                            "you approved is no longer what would happen. Nothing was written. "
                            "Propose the change again to see a current preview."
                        )
                    connection.execute("COMMIT")
                    return int(fresh["rows_affected"])
                except PreviewChanged:
                    raise
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        except sqlite3.Error as error:
            raise BackendError(
                f"SQLite refused the change, nothing was written: {error}"
            ) from error
