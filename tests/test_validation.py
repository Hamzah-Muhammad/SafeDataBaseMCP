"""The allowed grammar, and the things outside it.

These tests are the record of what this server refuses. Each rejection case
asserts on the reason as well as the refusal, so a rule that stops working
cannot be masked by a different rule happening to catch the same statement.
"""

from __future__ import annotations

import pytest

from safe_db_mcp.validation import (
    MAX_SQL_LENGTH,
    SqlRejected,
    validate_read,
    validate_write,
)

from .conftest import DEMO_TABLES

ACCEPTED_READS = [
    "SELECT * FROM books",
    "select title, published_year from books where published_year > 1970",
    "SELECT b.title, a.name FROM books b JOIN authors a ON a.id = b.author_id",
    "SELECT COUNT(*) FROM loans WHERE returned_on IS NULL",
    "SELECT * FROM books ORDER BY title LIMIT 5",
    "SELECT * FROM books;",
    "WITH open_loans AS (SELECT * FROM loans WHERE returned_on IS NULL) "
    "SELECT COUNT(*) FROM open_loans",
    "SELECT status, COUNT(*) FROM members GROUP BY status HAVING COUNT(*) > 1",
]

ACCEPTED_WRITES = [
    (
        "INSERT INTO members (full_name, email, joined_on) VALUES ('A B', 'a@b.co', '2026-01-01')",
        "INSERT",
        "members",
    ),
    ("INSERT INTO authors VALUES (99, 'New Author', 1980, 'Canada')", "INSERT", "authors"),
    ("UPDATE members SET status = 'active' WHERE id = 4", "UPDATE", "members"),
    ("update books set shelf = 'SF-A-99' where id = 1", "UPDATE", "books"),
    ("DELETE FROM loans WHERE id = 12", "DELETE", "loans"),
    ("DELETE FROM loans WHERE returned_on IS NOT NULL AND id > 100", "DELETE", "loans"),
]


class TestAcceptedGrammar:
    """The statements the server is supposed to run."""

    @pytest.mark.parametrize("sql", ACCEPTED_READS)
    def test_read_grammar_accepts_selects(self, sql: str) -> None:
        statement = validate_read(sql)
        assert statement.operation == "SELECT"
        assert not statement.sql.endswith(";")

    @pytest.mark.parametrize("sql,operation,table", ACCEPTED_WRITES)
    def test_write_grammar_accepts_row_level_dml(
        self, sql: str, operation: str, table: str
    ) -> None:
        statement = validate_write(sql, DEMO_TABLES)
        assert statement.operation == operation
        assert statement.table == table

    def test_table_name_is_matched_case_insensitively(self) -> None:
        statement = validate_write("DELETE FROM LOANS WHERE id = 1", DEMO_TABLES)
        assert statement.table == "loans"


class TestSchemaAndSessionRejections:
    """DDL, PRAGMA, ATTACH and transaction control are refused on both paths."""

    @pytest.mark.parametrize(
        "sql,fragment",
        [
            ("DROP TABLE books", "DDL"),
            ("DROP INDEX idx_books_author", "DDL"),
            ("ALTER TABLE books RENAME TO b2", "DDL"),
            ("CREATE TABLE evil (id INTEGER)", "DDL"),
            ("CREATE TRIGGER t AFTER INSERT ON books BEGIN SELECT 1; END", "DDL"),
            ("TRUNCATE TABLE loans", "DDL"),
            ("VACUUM", "not allowed"),
            ("REINDEX books", "not allowed"),
            ("ATTACH DATABASE 'other.db' AS other", "not allowed"),
            ("DETACH DATABASE other", "not allowed"),
            ("PRAGMA table_info(books)", "not allowed"),
            ("PRAGMA writable_schema = 1", "not allowed"),
        ],
    )
    def test_read_path_refuses(self, sql: str, fragment: str) -> None:
        with pytest.raises(SqlRejected, match=fragment):
            validate_read(sql)

    @pytest.mark.parametrize(
        "sql,fragment",
        [
            ("DROP TABLE books", "DDL"),
            ("ALTER TABLE books ADD COLUMN x TEXT", "DDL"),
            ("CREATE TABLE evil (id INTEGER)", "DDL"),
            ("PRAGMA foreign_keys = OFF", "not allowed"),
            ("ATTACH DATABASE 'other.db' AS other", "not allowed"),
            ("VACUUM", "not allowed"),
        ],
    )
    def test_write_path_refuses(self, sql: str, fragment: str) -> None:
        with pytest.raises(SqlRejected, match=fragment):
            validate_write(sql, DEMO_TABLES)


class TestStackingAndComments:
    """A call carries exactly one statement, and no comments."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM books; DROP TABLE books",
            "SELECT 1; SELECT 2",
            "SELECT * FROM books;DELETE FROM loans WHERE id = 1",
        ],
    )
    def test_stacked_reads_are_refused(self, sql: str) -> None:
        with pytest.raises(SqlRejected, match="Statement stacking is refused"):
            validate_read(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "UPDATE members SET status='x' WHERE id=1; DROP TABLE members",
            "DELETE FROM loans WHERE id=1; DELETE FROM loans WHERE id=2",
        ],
    )
    def test_stacked_writes_are_refused(self, sql: str) -> None:
        with pytest.raises(SqlRejected, match="Statement stacking is refused"):
            validate_write(sql, DEMO_TABLES)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM books -- and then some",
            "SELECT * /* hidden */ FROM books",
            "SELECT 1 --",
        ],
    )
    def test_comments_are_refused(self, sql: str) -> None:
        with pytest.raises(SqlRejected, match="comments are not allowed"):
            validate_read(sql)


class TestReadPathIsReadOnly:
    """run_query refuses every write, however it is dressed up."""

    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO members (full_name) VALUES ('x')",
            "UPDATE members SET status = 'active' WHERE id = 1",
            "DELETE FROM loans WHERE id = 1",
        ],
    )
    def test_dml_is_refused(self, sql: str) -> None:
        with pytest.raises(SqlRejected, match="read-only"):
            validate_read(sql)

    def test_dml_hidden_in_a_cte_is_refused(self) -> None:
        sql = "WITH x AS (DELETE FROM loans WHERE id = 1 RETURNING *) SELECT * FROM x"
        with pytest.raises(SqlRejected, match="read-only|DELETE"):
            validate_read(sql)


class TestSqliteInternals:
    """SQLite's own tables and escape-hatch functions are unreachable."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM sqlite_master",
            "SELECT * FROM sqlite_schema",
            "SELECT name FROM books UNION SELECT name FROM sqlite_master",
        ],
    )
    def test_internal_tables_are_refused(self, sql: str) -> None:
        with pytest.raises(SqlRejected, match="SQLite internal state"):
            validate_read(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT load_extension('evil.so')",
            "SELECT readfile('/etc/passwd')",
            "SELECT writefile('out.txt', 'data')",
        ],
    )
    def test_escape_hatch_functions_are_refused(self, sql: str) -> None:
        with pytest.raises(SqlRejected, match="not callable through this server"):
            validate_read(sql)

    def test_writing_to_an_internal_table_is_refused(self) -> None:
        with pytest.raises(SqlRejected, match="SQLite internal state"):
            validate_write("UPDATE sqlite_sequence SET seq = 0 WHERE name = 'books'", DEMO_TABLES)


class TestWriteSpecificRules:
    """Rules that only apply once a statement is on the write path."""

    @pytest.mark.parametrize(
        "sql",
        ["DELETE FROM loans", "UPDATE members SET status = 'active'", "delete from books"],
    )
    def test_unqualified_writes_are_refused(self, sql: str) -> None:
        with pytest.raises(SqlRejected, match="without a WHERE clause"):
            validate_write(sql, DEMO_TABLES)

    def test_unknown_table_is_refused_and_lists_the_real_ones(self) -> None:
        with pytest.raises(SqlRejected, match="Unknown table 'ledger'") as caught:
            validate_write("INSERT INTO ledger (a) VALUES (1)", DEMO_TABLES)
        assert "members" in str(caught.value)

    def test_a_select_sent_to_the_write_path_is_refused(self) -> None:
        with pytest.raises(SqlRejected, match="Use run_query"):
            validate_write("SELECT * FROM books", DEMO_TABLES)


class TestMalformedInput:
    """Shape checks that run before anything else."""

    @pytest.mark.parametrize("sql", ["", "   ", "\n\t "])
    def test_empty_sql_is_refused(self, sql: str) -> None:
        with pytest.raises(SqlRejected, match="Empty SQL"):
            validate_read(sql)

    def test_non_string_sql_is_refused(self) -> None:
        with pytest.raises(SqlRejected, match="must be a string"):
            validate_read(42)  # type: ignore[arg-type]

    def test_oversized_sql_is_refused(self) -> None:
        sql = "SELECT * FROM books WHERE title IN (" + ",".join(["'x'"] * 2000) + ")"
        assert len(sql) > MAX_SQL_LENGTH
        with pytest.raises(SqlRejected, match="character limit"):
            validate_read(sql)

    def test_null_byte_is_refused(self) -> None:
        with pytest.raises(SqlRejected, match="null byte"):
            validate_read("SELECT * FROM books\x00")
