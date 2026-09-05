"""The Postgres backend: the same guarantees, enforced by a real database.

Everything the SQLite backend achieves with a file flag, this achieves with
database machinery, which is stronger:

* reads connect as a **separate role** holding nothing but ``SELECT``. Not a
  connection flag this process sets on itself and could forget to set, but a
  grant the server enforces against a login that has no other privilege. The
  transaction is also opened ``READ ONLY``, so there are two independent
  reasons a write on the read path fails;
* writes run at **SERIALIZABLE** isolation, and the confirm step recomputes the
  preview inside the committing transaction and refuses if it moved. Together
  those close the concurrent-writer race the SQLite backend can only narrow;
* rows are matched on the declared **primary key** rather than on a rowid, so
  the diff means the same thing it does in SQLite. A table with no primary key
  reports ``diff_available: false`` rather than guessing.

Connection details come from ``SAFEDB_PG_*`` and the password comes from
:mod:`safe_db_mcp.aws.credentials`, which may hand back an RDS IAM token instead
of a password. Nothing in this module knows or cares which it got.

``psycopg`` is an optional dependency, imported lazily so the SQLite default
stays dependency-free.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from ..aws.credentials import AwsSettings, DatabaseLogin, resolve_from_env
from ..validation import ValidatedStatement
from .base import Backend, BackendError, PreviewChanged
from .diffing import Snapshot, build_diff, comparable

#: Rows a single read may return before the result is truncated.
MAX_RESULT_ROWS = 200

#: Above this row count a table is too large to snapshot for an exact diff.
MAX_DIFF_TABLE_ROWS = 5000


def _psycopg():
    """Import psycopg lazily, with a readable error if it is not installed."""
    try:
        import psycopg  # noqa: PLC0415 - deliberately lazy, it is optional
        from psycopg import sql  # noqa: PLC0415
        from psycopg.rows import dict_row  # noqa: PLC0415
    except ImportError as error:  # pragma: no cover - depends on the install
        raise BackendError(
            "The Postgres backend needs psycopg, which is an optional dependency. "
            "Install it with: pip install 'safe-db-mcp[postgres]'"
        ) from error
    return psycopg, sql, dict_row


@dataclass(frozen=True)
class PostgresSettings:
    """Where the database is and who connects to it.

    Two logins on purpose. ``reader_user`` should hold ``SELECT`` and nothing
    else; ``writer_user`` owns the tables. Running reads as the writer would
    work and would quietly throw away the strongest guarantee this backend has.
    """

    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "safedb"
    #: The schema the demo tables live in. Set on the connection via libpq
    #: ``options``, so every query resolves inside it without a round trip.
    schema: str = "public"
    reader_user: str = "safedb_reader"
    writer_user: str = "safedb_writer"
    #: ``require`` and stronger are what you want against RDS. Local sockets
    #: with no TLS need ``prefer`` or ``disable``.
    sslmode: str = "require"
    #: Path to a CA bundle. For RDS this is the AWS global bundle, which is what
    #: makes ``verify-full`` meaningful rather than decorative.
    sslrootcert: str | None = None
    connect_timeout: int = 10
    aws: AwsSettings = None  # type: ignore[assignment]

    @classmethod
    def from_env(cls) -> PostgresSettings:
        """Build settings from the ``SAFEDB_PG_*`` environment variables."""
        return cls(
            host=os.environ.get("SAFEDB_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("SAFEDB_PG_PORT", "5432")),
            database=os.environ.get("SAFEDB_PG_DATABASE", "safedb"),
            schema=os.environ.get("SAFEDB_PG_SCHEMA", "public"),
            reader_user=os.environ.get("SAFEDB_PG_READER_USER", "safedb_reader"),
            writer_user=os.environ.get("SAFEDB_PG_WRITER_USER", "safedb_writer"),
            sslmode=os.environ.get("SAFEDB_PG_SSLMODE", "require"),
            sslrootcert=os.environ.get("SAFEDB_PG_SSLROOTCERT") or None,
            connect_timeout=int(os.environ.get("SAFEDB_PG_CONNECT_TIMEOUT", "10")),
            aws=AwsSettings.from_env(),
        )

    @property
    def aws_settings(self) -> AwsSettings:
        """The AWS settings, defaulting to plain environment credentials."""
        return self.aws if self.aws is not None else AwsSettings()

    def login_for(self, role: str) -> DatabaseLogin:
        """Resolve the login for ``role``, which is ``reader`` or ``writer``.

        Routes through :mod:`safe_db_mcp.aws.credentials`, so the same call
        returns an environment password locally and a Secrets Manager lookup or
        an RDS IAM token in AWS, depending only on configuration.
        """
        from ..aws.credentials import (  # noqa: PLC0415 - avoids a cycle at import time
            resolve_from_secrets_manager,
            resolve_rds_iam_token,
        )

        username = self.reader_user if role == "reader" else self.writer_user
        source = self.aws_settings.source

        if source == "secretsmanager":
            return resolve_from_secrets_manager(self.aws_settings)
        if source == "rds-iam":
            return resolve_rds_iam_token(self.aws_settings, self.host, self.port, username)
        return resolve_from_env(username, f"SAFEDB_PG_{role.upper()}_PASSWORD")

    def conninfo(self, login: DatabaseLogin) -> str:
        """Build a libpq connection string. Never logged, it holds the secret."""
        parts = {
            "host": self.host,
            "port": str(self.port),
            "dbname": self.database,
            "user": login.username,
            "password": login.password,
            "sslmode": self.sslmode,
            "connect_timeout": str(self.connect_timeout),
            "application_name": "safe-db-mcp",
            "options": f"-csearch_path={self.schema}",
        }
        if self.sslrootcert:
            parts["sslrootcert"] = self.sslrootcert
        return " ".join(f"{key}='{value}'" for key, value in parts.items())


class PostgresBackend(Backend):
    """A Postgres database, served through validated operations only."""

    name = "postgres"

    def __init__(self, settings: PostgresSettings | None = None) -> None:
        """Configure the backend. Connections are opened per call, not held."""
        self.settings = settings if settings is not None else PostgresSettings.from_env()

    @property
    def description(self) -> str:
        """Host, port and database. Deliberately carries no credentials."""
        s = self.settings
        return f"postgresql://{s.host}:{s.port}/{s.database}"

    # Connections

    def _connect(self, role: str):
        """Open a connection for ``reader`` or ``writer``.

        The reader connection is marked ``read_only``, which makes every
        transaction on it ``READ ONLY`` at the server. That is belt and braces
        on top of the reader role's grants, not a replacement for them.
        """
        psycopg, _, dict_row = _psycopg()
        login = self.settings.login_for(role)
        try:
            connection = psycopg.connect(
                self.settings.conninfo(login), row_factory=dict_row, autocommit=False
            )
        except psycopg.Error as error:
            raise BackendError(f"Could not connect to Postgres: {error}") from error

        if role == "reader":
            connection.read_only = True
        else:
            connection.isolation_level = psycopg.IsolationLevel.SERIALIZABLE
        return connection

    # Schema reads

    def table_names(self) -> list[str]:
        with self._connect("reader") as connection:
            return self._table_names(connection)

    def _table_names(self, connection) -> list[str]:
        rows = connection.execute(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = current_schema() "
            "ORDER BY tablename"
        ).fetchall()
        return [row["tablename"] for row in rows]

    def _require_table(self, connection, table: str) -> None:
        if table not in self._table_names(connection):
            raise ValueError(f"Unknown table '{table}'.")

    def _columns(self, connection, table: str) -> list[dict[str, Any]]:
        """Return column definitions, read from ``pg_catalog``.

        Deliberately not ``information_schema``: those views hide rows the
        current role has no privilege on, so the reader role, which holds only
        SELECT, sees no constraints at all and every column comes back looking
        like it is not a primary key. ``pg_catalog`` answers the same for every
        role, which is what a schema description has to do.
        """
        self._require_table(connection, table)
        rows = connection.execute(
            """
            SELECT a.attname                                  AS column_name,
                   format_type(a.atttypid, a.atttypmod)       AS data_type,
                   a.attnotnull                               AS not_null,
                   pg_get_expr(d.adbin, d.adrelid)            AS column_default,
                   EXISTS (
                       SELECT 1 FROM pg_constraint pc
                       WHERE pc.conrelid = c.oid
                         AND pc.contype = 'p'
                         AND a.attnum = ANY (pc.conkey)
                   )                                          AS is_pk
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
            WHERE c.relname = %(table)s
              AND c.relkind = 'r'
              AND n.nspname = current_schema()
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            {"table": table},
        ).fetchall()
        return [
            {
                "name": row["column_name"],
                "type": row["data_type"],
                "not_null": bool(row["not_null"]),
                "default": row["column_default"],
                "primary_key": bool(row["is_pk"]),
            }
            for row in rows
        ]

    def _key_columns(self, connection, table: str) -> list[str]:
        return [c["name"] for c in self._columns(connection, table) if c["primary_key"]]

    def _row_count(self, connection, table: str) -> int:
        _, sql, _ = _psycopg()
        self._require_table(connection, table)
        statement = sql.SQL("SELECT COUNT(*) AS n FROM {}").format(sql.Identifier(table))
        return int(connection.execute(statement).fetchone()["n"])

    def _snapshot(self, connection, table: str, keys: list[str]) -> Snapshot:
        """Return ``{primary key tuple: row}`` for every row in ``table``.

        Postgres has no stable equivalent of SQLite's rowid: ``ctid`` moves
        whenever a row is updated, so it cannot match a row across a write. The
        declared primary key is the honest identity, and a table without one
        does not get a diff at all.
        """
        _, sql, _ = _psycopg()
        statement = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
        rows = connection.execute(statement).fetchall()
        return {tuple(row[key] for key in keys): dict(row) for row in rows}

    # The operations

    def list_tables(self) -> list[dict[str, Any]]:
        with self._connect("reader") as connection:
            return [
                {
                    "name": name,
                    "rows": self._row_count(connection, name),
                    "columns": [c["name"] for c in self._columns(connection, name)],
                }
                for name in self._table_names(connection)
            ]

    def describe_table(self, table: str) -> dict[str, Any]:
        with self._connect("reader") as connection:
            known = self._table_names(connection)
            if table not in known:
                raise ValueError(f"Unknown table '{table}'. Tables: {', '.join(known)}.")
            keys = connection.execute(
                """
                SELECT a.attname  AS column_name,
                       rc.relname AS references_table,
                       ra.attname AS references_column
                FROM pg_constraint con
                JOIN pg_class c  ON c.oid  = con.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_class rc ON rc.oid = con.confrelid
                JOIN LATERAL unnest(con.conkey, con.confkey) AS cols(local, remote) ON true
                JOIN pg_attribute a  ON a.attrelid  = con.conrelid  AND a.attnum  = cols.local
                JOIN pg_attribute ra ON ra.attrelid = con.confrelid AND ra.attnum = cols.remote
                WHERE con.contype = 'f'
                  AND c.relname = %(table)s
                  AND n.nspname = current_schema()
                ORDER BY a.attname
                """,
                {"table": table},
            ).fetchall()
            return {
                "table": table,
                "rows": self._row_count(connection, table),
                "columns": self._columns(connection, table),
                "foreign_keys": [
                    {
                        "column": row["column_name"],
                        "references": f"{row['references_table']}.{row['references_column']}",
                    }
                    for row in keys
                ],
            }

    def run_query(self, statement: ValidatedStatement) -> dict[str, Any]:
        psycopg, _, _ = _psycopg()
        try:
            with self._connect("reader") as connection:
                cursor = connection.execute(statement.sql)
                rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
                columns = [d.name for d in cursor.description] if cursor.description else []
        except psycopg.Error as error:
            raise BackendError(f"Postgres refused the query: {_clean(error)}") from error

        truncated = len(rows) > MAX_RESULT_ROWS
        return {
            "sql": statement.sql,
            "columns": columns,
            "rows": [dict(row) for row in rows[:MAX_RESULT_ROWS]],
            "row_count": min(len(rows), MAX_RESULT_ROWS),
            "truncated": truncated,
        }

    def _run_and_snapshot(self, connection, statement: ValidatedStatement) -> dict[str, Any]:
        """Execute inside an open transaction and build the preview payload."""
        table = statement.table
        assert table is not None

        keys = self._key_columns(connection, table)
        diffable = bool(keys) and self._row_count(connection, table) <= MAX_DIFF_TABLE_ROWS

        before = self._snapshot(connection, table, keys) if diffable else {}
        rows_affected = connection.execute(statement.sql).rowcount
        after = self._snapshot(connection, table, keys) if diffable else {}

        return {
            "table": table,
            "operation": statement.operation,
            "rows_affected": rows_affected,
            "diff": build_diff(before, after) if diffable else None,
            "diff_available": diffable,
            "key_columns": keys if diffable else [],
        }

    def preview_write(self, statement: ValidatedStatement) -> dict[str, Any]:
        psycopg, _, _ = _psycopg()
        try:
            with self._connect("writer") as connection:
                try:
                    return self._run_and_snapshot(connection, statement)
                finally:
                    # Unconditional. psycopg would also roll back on the way out
                    # of the context manager, but the guarantee is important
                    # enough to state in the code rather than inherit.
                    connection.rollback()
        except psycopg.Error as error:
            raise BackendError(
                f"Postgres refused the change, nothing was written: {_clean(error)}"
            ) from error

    def commit_write(self, statement: ValidatedStatement, approved: dict[str, Any]) -> int:
        psycopg, _, _ = _psycopg()
        try:
            with self._connect("writer") as connection:
                try:
                    fresh = self._run_and_snapshot(connection, statement)
                    if comparable(fresh) != comparable(approved):
                        connection.rollback()
                        raise PreviewChanged(
                            "The data changed since this change was proposed, so the preview "
                            "you approved is no longer what would happen. Nothing was written. "
                            "Propose the change again to see a current preview."
                        )
                    connection.commit()
                    return int(fresh["rows_affected"])
                except PreviewChanged:
                    raise
                except Exception:
                    connection.rollback()
                    raise
        except psycopg.errors.SerializationFailure as error:
            raise PreviewChanged(
                "Postgres could not serialise this change against a concurrent "
                "transaction, so nothing was written. Propose the change again."
            ) from error
        except psycopg.Error as error:
            raise BackendError(
                f"Postgres refused the change, nothing was written: {_clean(error)}"
            ) from error


def _clean(error: Exception) -> str:
    """Render a driver error as a single readable line."""
    return " ".join(str(error).split())
