"""The five operations, with no MCP in sight.

:class:`SafeDatabase` is the whole feature set of this project expressed as
plain Python. :mod:`safe_db_mcp.server` is a thin adapter that exposes these
methods as MCP tools and nothing more.

Keeping the two apart is deliberate. It means the propose/confirm gate and
every rejection rule can be tested directly, offline, without a transport in
the way, and it means the same guarantees would hold if a second transport were
added later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .database import (
    MAX_DIFF_TABLE_ROWS,
    MAX_RESULT_ROWS,
    describe_columns,
    ensure_database,
    foreign_keys,
    list_table_names,
    read_connection,
    rows_to_dicts,
    snapshot_table,
    table_row_count,
    write_connection,
)
from .proposals import Proposal, ProposalError, ProposalStore
from .validation import SqlRejected, validate_read, validate_write


class SafeDatabase:
    """A SQLite database reachable only through five validated operations."""

    def __init__(self, path: Path | str | None = None, ttl_seconds: float | None = None) -> None:
        """Open (and seed, if needed) the database this instance serves.

        Args:
            path: Location of the SQLite file. Defaults to the value of
                ``SAFEDB_DATABASE_PATH`` or ``data/library.db``.
            ttl_seconds: Proposal time-to-live override, in seconds.
        """
        self.path = ensure_database(Path(path) if path is not None else None)
        self.proposals = ProposalStore(ttl_seconds=ttl_seconds)

    # Reads

    def table_names(self) -> list[str]:
        """Return every user table in the database."""
        with read_connection(self.path) as connection:
            return list_table_names(connection)

    def list_tables(self) -> dict[str, Any]:
        """Return each table with its row count and column names.

        Returns:
            A dictionary with a ``database`` path and a ``tables`` list, one
            entry per table.
        """
        with read_connection(self.path) as connection:
            tables = []
            for name in list_table_names(connection):
                tables.append(
                    {
                        "name": name,
                        "rows": table_row_count(connection, name),
                        "columns": [c["name"] for c in describe_columns(connection, name)],
                    }
                )
        return {"database": str(self.path), "tables": tables}

    def describe_table(self, table: str) -> dict[str, Any]:
        """Return the column and foreign key definitions of one table.

        Args:
            table: The table to describe.

        Raises:
            ValueError: If no such table exists.
        """
        with read_connection(self.path) as connection:
            known = list_table_names(connection)
            if table not in known:
                raise ValueError(f"Unknown table '{table}'. Tables: {', '.join(known)}.")
            return {
                "table": table,
                "rows": table_row_count(connection, table),
                "columns": describe_columns(connection, table),
                "foreign_keys": foreign_keys(connection, table),
            }

    def run_query(self, sql: str) -> dict[str, Any]:
        """Validate ``sql`` as a single SELECT and run it read-only.

        Args:
            sql: The statement to run.

        Returns:
            The column names, the rows (capped at
            :data:`~safe_db_mcp.database.MAX_RESULT_ROWS`), and whether the
            result was truncated.

        Raises:
            SqlRejected: If the statement is outside the read grammar.
        """
        statement = validate_read(sql)
        with read_connection(self.path) as connection:
            cursor = connection.execute(statement.sql)
            rows = cursor.fetchmany(MAX_RESULT_ROWS + 1)
            columns = [d[0] for d in cursor.description] if cursor.description else []
        truncated = len(rows) > MAX_RESULT_ROWS
        return {
            "sql": statement.sql,
            "columns": columns,
            "rows": rows_to_dicts(list(rows[:MAX_RESULT_ROWS])),
            "row_count": min(len(rows), MAX_RESULT_ROWS),
            "truncated": truncated,
        }

    # The write gate

    def propose_change(self, sql: str) -> dict[str, Any]:
        """Validate a write, run it uncommitted, and return a preview plus id.

        The statement is executed inside a real transaction so the preview is
        computed by SQLite rather than guessed, and then rolled back. Nothing
        reaches disk. The returned ``change_id`` is the only way to commit it.

        Args:
            sql: A single ``INSERT``, ``UPDATE`` or ``DELETE``.

        Returns:
            The preview: rows affected, a row-level diff where the table is
            small enough to snapshot, the ``change_id`` and its expiry.

        Raises:
            SqlRejected: If the statement is outside the write grammar.
            sqlite3.Error: If the statement is valid SQL by our grammar but the
                database refuses it (a constraint violation, for instance). The
                transaction is rolled back before the error propagates.
        """
        statement = validate_write(sql, frozenset(self.table_names()))
        table = statement.table
        assert table is not None  # validate_write always resolves a table

        with write_connection(self.path) as connection:
            connection.execute("BEGIN")
            try:
                diffable = table_row_count(connection, table) <= MAX_DIFF_TABLE_ROWS
                before = snapshot_table(connection, table) if diffable else {}
                cursor = connection.execute(statement.sql)
                rows_affected = cursor.rowcount
                after = snapshot_table(connection, table) if diffable else {}
                diff = _diff_snapshots(before, after) if diffable else None
            finally:
                # The rollback is unconditional: a preview never persists,
                # whether the statement succeeded, failed or raised.
                connection.execute("ROLLBACK")

        preview: dict[str, Any] = {
            "table": table,
            "operation": statement.operation,
            "rows_affected": rows_affected,
            "diff": diff,
            "diff_available": diffable,
        }
        proposal = self.proposals.add(statement.sql, statement.operation, table, preview)
        return _proposal_payload(proposal, committed=False)

    def confirm_change(self, change_id: str) -> dict[str, Any]:
        """Commit a pending proposal, once.

        The statement is re-validated before it runs, so a proposal cannot
        outlive the rules that admitted it.

        Args:
            change_id: The id returned by :meth:`propose_change`.

        Returns:
            The committed result: rows affected and the proposal's preview.

        Raises:
            ProposalError: If the id is unknown, already used or expired.
            SqlRejected: If re-validation fails.
        """
        proposal = self.proposals.claim(change_id)
        try:
            statement = validate_write(proposal.sql, frozenset(self.table_names()))
            with write_connection(self.path) as connection:
                connection.execute("BEGIN")
                try:
                    cursor = connection.execute(statement.sql)
                    rows_affected = cursor.rowcount
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
        except Exception:
            # The proposal was claimed but nothing was committed, so hand it
            # back rather than burning it on a failure.
            self.proposals.release(change_id)
            raise

        payload = _proposal_payload(proposal, committed=True)
        payload["rows_affected"] = rows_affected
        return payload

    def list_pending_changes(self) -> dict[str, Any]:
        """Return every proposal that is still unused and unexpired."""
        self.proposals.purge_expired()
        pending = self.proposals.pending()
        return {
            "pending_count": len(pending),
            "ttl_seconds": self.proposals.ttl_seconds,
            "changes": [_proposal_payload(p, committed=False) for p in pending],
        }


def _proposal_payload(proposal: Proposal, *, committed: bool) -> dict[str, Any]:
    """Render a proposal as the dictionary the tools return."""
    payload: dict[str, Any] = {
        "change_id": proposal.change_id,
        "sql": proposal.sql,
        "operation": proposal.operation,
        "table": proposal.table,
        "committed": committed,
    }
    payload.update(proposal.preview)
    if committed:
        payload["status"] = "committed"
    else:
        payload["status"] = "pending"
        payload["expires_in_seconds"] = round(proposal.seconds_remaining(), 1)
    return payload


def _diff_snapshots(
    before: dict[int, dict[str, object]], after: dict[int, dict[str, object]]
) -> dict[str, list[dict[str, object]]]:
    """Compare two ``{rowid: row}`` snapshots into added/removed/updated lists.

    Rows are matched on SQLite's ``rowid``, so an ``UPDATE`` shows as a changed
    row with its before and after values rather than as a delete plus an insert.
    """
    added = [row for rowid, row in after.items() if rowid not in before]
    removed = [row for rowid, row in before.items() if rowid not in after]
    updated = []
    for rowid, old in before.items():
        new = after.get(rowid)
        if new is None or new == old:
            continue
        changed = {
            key: {"before": old[key], "after": new[key]} for key in new if new[key] != old[key]
        }
        updated.append({"row": new, "changed": changed})
    return {"added": added, "removed": removed, "updated": updated}


__all__ = ["ProposalError", "SafeDatabase", "SqlRejected"]
