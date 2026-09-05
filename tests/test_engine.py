"""The operations themselves, and the gate between propose and confirm.

Where :mod:`tests.test_validation` proves a statement is refused, these tests
prove the database on disk is unchanged when it is - and that the propose step
really does roll back rather than merely promising to.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from safe_db_mcp.database import read_connection
from safe_db_mcp.engine import SafeDatabase
from safe_db_mcp.proposals import ProposalError
from safe_db_mcp.validation import SqlRejected


def member_status(path: Path, member_id: int) -> str | None:
    """Read one member's status straight from the file, bypassing the server."""
    with read_connection(path) as connection:
        row = connection.execute("SELECT status FROM members WHERE id = ?", (member_id,)).fetchone()
    return None if row is None else row["status"]


def row_count(path: Path, table: str) -> int:
    """Count rows in a table straight from the file, bypassing the server."""
    with read_connection(path) as connection:
        return int(connection.execute(f'SELECT COUNT(*) AS n FROM "{table}"').fetchone()["n"])


class TestSeeding:
    """The demo database builds itself from a clean clone."""

    def test_database_is_created_and_seeded(self, database_path: Path) -> None:
        assert not database_path.exists()
        database = SafeDatabase(path=database_path)
        assert database_path.exists()
        assert set(database.table_names()) == {"authors", "books", "members", "loans"}

    def test_existing_database_is_not_reseeded(self, database: SafeDatabase) -> None:
        proposal = database.propose_change("DELETE FROM loans WHERE id = 1")
        database.confirm_change(proposal["change_id"])
        before = row_count(database.path, "loans")

        reopened = SafeDatabase(path=database.path)
        assert row_count(reopened.path, "loans") == before


class TestReads:
    """Reads run immediately and return real data."""

    def test_list_tables_reports_row_counts(self, database: SafeDatabase) -> None:
        listing = database.list_tables()
        by_name = {t["name"]: t for t in listing["tables"]}
        assert by_name["authors"]["rows"] == 6
        assert by_name["books"]["rows"] == 13
        assert "title" in by_name["books"]["columns"]

    def test_describe_table_reports_columns_and_foreign_keys(self, database: SafeDatabase) -> None:
        described = database.describe_table("loans")
        columns = {c["name"]: c for c in described["columns"]}
        assert columns["id"]["primary_key"] is True
        assert columns["book_id"]["not_null"] is True
        references = {fk["references"] for fk in described["foreign_keys"]}
        assert references == {"books.id", "members.id"}

    def test_describe_table_rejects_an_unknown_table(self, database: SafeDatabase) -> None:
        with pytest.raises(ValueError, match="Unknown table 'ledger'"):
            database.describe_table("ledger")

    def test_run_query_returns_rows(self, database: SafeDatabase) -> None:
        result = database.run_query("SELECT title FROM books WHERE published_year < 1970")
        titles = {row["title"] for row in result["rows"]}
        assert "Things Fall Apart" in titles
        assert result["truncated"] is False

    def test_run_query_refuses_a_write_and_changes_nothing(self, database: SafeDatabase) -> None:
        before = member_status(database.path, 4)
        with pytest.raises(SqlRejected):
            database.run_query("UPDATE members SET status = 'active' WHERE id = 4")
        assert member_status(database.path, 4) == before

    def test_the_read_connection_is_genuinely_read_only(self, database: SafeDatabase) -> None:
        # Not a validation test: this proves SQLite itself refuses a write on
        # the handle run_query uses, so the read path is safe by construction.
        with read_connection(database.path) as connection:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("UPDATE members SET status = 'x' WHERE id = 1")


class TestProposeDoesNotCommit:
    """propose_change previews. It never writes."""

    def test_update_is_previewed_but_not_written(self, database: SafeDatabase) -> None:
        assert member_status(database.path, 4) == "suspended"
        proposal = database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")

        assert proposal["status"] == "pending"
        assert proposal["committed"] is False
        assert proposal["rows_affected"] == 1
        assert member_status(database.path, 4) == "suspended"

    def test_the_preview_diff_shows_before_and_after(self, database: SafeDatabase) -> None:
        proposal = database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")
        updated = proposal["diff"]["updated"]
        assert len(updated) == 1
        assert updated[0]["changed"]["status"] == {"before": "suspended", "after": "active"}
        assert proposal["diff"]["added"] == []
        assert proposal["diff"]["removed"] == []

    def test_insert_preview_shows_the_new_row_only(self, database: SafeDatabase) -> None:
        before = row_count(database.path, "authors")
        proposal = database.propose_change(
            "INSERT INTO authors (name, birth_year, country) VALUES ('N K Jemisin', 1972, 'US')"
        )
        assert proposal["diff"]["added"][0]["name"] == "N K Jemisin"
        assert proposal["diff"]["removed"] == []
        assert row_count(database.path, "authors") == before

    def test_delete_preview_shows_the_row_that_would_go(self, database: SafeDatabase) -> None:
        before = row_count(database.path, "loans")
        proposal = database.propose_change("DELETE FROM loans WHERE id = 12")
        assert [row["id"] for row in proposal["diff"]["removed"]] == [12]
        assert row_count(database.path, "loans") == before

    def test_a_constraint_violation_rolls_back_and_raises(self, database: SafeDatabase) -> None:
        before = row_count(database.path, "members")
        with pytest.raises(sqlite3.IntegrityError):
            database.propose_change(
                "INSERT INTO members (full_name, email, joined_on) "
                "VALUES ('Copy', 'amara.osei@example.com', '2026-01-01')"
            )
        assert row_count(database.path, "members") == before
        assert database.list_pending_changes()["pending_count"] == 0


class TestConfirmCommits:
    """confirm_change is the only path to disk, and it runs once."""

    def test_confirm_writes_the_change(self, database: SafeDatabase) -> None:
        proposal = database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")
        committed = database.confirm_change(proposal["change_id"])

        assert committed["status"] == "committed"
        assert committed["committed"] is True
        assert committed["rows_affected"] == 1
        assert member_status(database.path, 4) == "active"

    def test_a_change_id_is_single_use(self, database: SafeDatabase) -> None:
        proposal = database.propose_change("DELETE FROM loans WHERE id = 12")
        database.confirm_change(proposal["change_id"])
        after_first = row_count(database.path, "loans")

        with pytest.raises(ProposalError, match="already been confirmed"):
            database.confirm_change(proposal["change_id"])
        assert row_count(database.path, "loans") == after_first

    def test_an_unknown_change_id_is_refused(self, database: SafeDatabase) -> None:
        with pytest.raises(ProposalError, match="Unknown change_id"):
            database.confirm_change("0000000000000000")

    def test_confirm_without_ever_proposing_is_refused(self, database: SafeDatabase) -> None:
        assert database.list_pending_changes()["pending_count"] == 0
        with pytest.raises(ProposalError, match="Unknown change_id"):
            database.confirm_change("abcdef0123456789")

    @pytest.mark.parametrize("change_id", ["", "   ", None])
    def test_a_missing_change_id_is_refused(
        self, database: SafeDatabase, change_id: object
    ) -> None:
        with pytest.raises(ProposalError, match="A change_id is required"):
            database.confirm_change(change_id)  # type: ignore[arg-type]

    def test_an_expired_change_id_is_refused(self, short_ttl_database: SafeDatabase) -> None:
        database = short_ttl_database
        proposal = database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")
        time.sleep(0.1)

        with pytest.raises(ProposalError, match="expired"):
            database.confirm_change(proposal["change_id"])
        assert member_status(database.path, 4) == "suspended"

    def test_a_second_proposal_of_the_same_write_gets_a_new_id(
        self, database: SafeDatabase
    ) -> None:
        first = database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")
        second = database.propose_change("UPDATE members SET status = 'active' WHERE id = 4")
        assert first["change_id"] != second["change_id"]


class TestPendingChanges:
    """The pending list reflects what is actually confirmable."""

    def test_pending_lists_unconfirmed_proposals(self, database: SafeDatabase) -> None:
        first = database.propose_change("DELETE FROM loans WHERE id = 12")
        database.propose_change("DELETE FROM loans WHERE id = 11")

        pending = database.list_pending_changes()
        assert pending["pending_count"] == 2
        assert first["change_id"] in {c["change_id"] for c in pending["changes"]}

    def test_confirmed_proposals_leave_the_pending_list(self, database: SafeDatabase) -> None:
        proposal = database.propose_change("DELETE FROM loans WHERE id = 12")
        database.confirm_change(proposal["change_id"])
        assert database.list_pending_changes()["pending_count"] == 0

    def test_expired_proposals_leave_the_pending_list(
        self, short_ttl_database: SafeDatabase
    ) -> None:
        short_ttl_database.propose_change("DELETE FROM loans WHERE id = 12")
        time.sleep(0.1)
        assert short_ttl_database.list_pending_changes()["pending_count"] == 0


class TestRejectionsLeaveNoTrace:
    """A refused statement creates no proposal and touches no data."""

    @pytest.mark.parametrize(
        "sql",
        [
            "DROP TABLE members",
            "ALTER TABLE members ADD COLUMN secret TEXT",
            "PRAGMA writable_schema = 1",
            "ATTACH DATABASE 'other.db' AS other",
            "DELETE FROM loans WHERE id = 1; DROP TABLE loans",
            "DELETE FROM members",
            "UPDATE members SET status = 'active'",
            "INSERT INTO ledger (amount) VALUES (1)",
            "UPDATE sqlite_sequence SET seq = 0 WHERE name = 'books'",
        ],
    )
    def test_unsafe_writes_are_refused_and_nothing_is_staged(
        self, database: SafeDatabase, sql: str
    ) -> None:
        tables_before = database.table_names()
        members_before = row_count(database.path, "members")

        with pytest.raises(SqlRejected):
            database.propose_change(sql)

        assert database.table_names() == tables_before
        assert row_count(database.path, "members") == members_before
        assert database.list_pending_changes()["pending_count"] == 0
