"""Seed a Postgres database with the demo library schema.

Deliberately a separate command rather than something the server does on
connect. The whole argument of this project is that a server should not be able
to run DDL, so the server does not run DDL, not even to help you. Setting up a
database is an operator action, and it looks like one.

    python -m safe_db_mcp.seed_postgres --drop-existing

Connects as the writer using the same ``SAFEDB_PG_*`` and ``SAFEDB_CREDENTIALS``
configuration the server uses, so if this works the server will connect too.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .backends.postgres_backend import PostgresSettings, _psycopg

SCHEMA_PATH = Path(__file__).with_name("schema_postgres.sql")

TABLES = ("loans", "books", "members", "authors")


def seed(settings: PostgresSettings | None = None, drop_existing: bool = False) -> list[str]:
    """Apply the demo schema, and return the tables that now exist.

    Args:
        settings: Connection settings. Defaults to the environment.
        drop_existing: Drop the demo tables first. Off by default so this
            cannot quietly destroy data in a database you pointed it at.

    Raises:
        RuntimeError: If the demo tables already exist and ``drop_existing`` is
            not set.
    """
    psycopg, sql, _ = _psycopg()
    resolved = settings if settings is not None else PostgresSettings.from_env()
    login = resolved.login_for("writer")

    with psycopg.connect(resolved.conninfo(login), autocommit=False) as connection:
        existing = {
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            ).fetchall()
        }
        clashes = sorted(existing.intersection(TABLES))

        if clashes and not drop_existing:
            raise RuntimeError(
                f"These tables already exist: {', '.join(clashes)}. "
                "Re-run with --drop-existing if you really want to replace them."
            )

        if drop_existing:
            for table in TABLES:
                connection.execute(
                    sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(table))
                )

        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        connection.commit()

        return sorted(
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
            ).fetchall()
        )


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and seed the database."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop the demo tables before recreating them. Destructive, off by default.",
    )
    args = parser.parse_args(argv)

    settings = PostgresSettings.from_env()
    print(f"Seeding postgresql://{settings.host}:{settings.port}/{settings.database}")
    try:
        tables = seed(settings, drop_existing=args.drop_existing)
    except RuntimeError as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    print("Tables now present: " + ", ".join(tables))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
