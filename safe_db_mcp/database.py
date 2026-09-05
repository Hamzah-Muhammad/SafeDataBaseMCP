"""Backwards-compatible re-exports.

The SQLite specifics moved to :mod:`safe_db_mcp.backends.sqlite_backend` when
the Postgres backend arrived. This module stays so existing imports keep
working, and so the connection helpers have one obvious name.
"""

from .backends.sqlite_backend import (
    DATABASE_PATH_ENV,
    DEFAULT_DATABASE_PATH,
    MAX_DIFF_TABLE_ROWS,
    MAX_RESULT_ROWS,
    SCHEMA_PATH,
    ensure_database,
    read_connection,
    resolve_database_path,
    write_connection,
)

__all__ = [
    "DATABASE_PATH_ENV",
    "DEFAULT_DATABASE_PATH",
    "MAX_DIFF_TABLE_ROWS",
    "MAX_RESULT_ROWS",
    "SCHEMA_PATH",
    "ensure_database",
    "read_connection",
    "resolve_database_path",
    "write_connection",
]
