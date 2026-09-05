"""The operations themselves, with no MCP and no SQL dialect in sight.

:class:`SafeDatabase` owns the part that must be true regardless of what is
underneath: validate, mint a proposal, and never commit anything that was not
previewed first. It reaches the actual database only through
:class:`~safe_db_mcp.backends.base.Backend`, so SQLite and Postgres get the same
gate rather than each implementing their own version of it.

:mod:`safe_db_mcp.server` is a thin adapter that exposes these methods as MCP
tools. Keeping all three apart means the propose/confirm gate and every
rejection rule can be tested directly, offline, with no transport and no
database server in the way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .backends import Backend, BackendError, PreviewChanged, build_backend
from .proposals import Proposal, ProposalError, ProposalStore
from .validation import SqlRejected, validate_read, validate_write


class SafeDatabase:
    """A database reachable only through validated operations."""

    def __init__(
        self,
        path: Path | str | None = None,
        ttl_seconds: float | None = None,
        backend: Backend | None = None,
    ) -> None:
        """Wire up a backend and an empty proposal store.

        Args:
            path: SQLite file location, for the default backend. Ignored when
                ``backend`` is given or when ``SAFEDB_BACKEND=postgres``.
            ttl_seconds: Proposal time-to-live override, in seconds.
            backend: An already-constructed backend. Mostly for tests, which
                use it to point at a throwaway database.
        """
        if backend is not None:
            self.backend = backend
        elif path is not None:
            self.backend = build_backend("sqlite", path=path)
        else:
            self.backend = build_backend()
        self.proposals = ProposalStore(ttl_seconds=ttl_seconds)

    @property
    def path(self) -> Any:
        """The SQLite file this database is served from, if it is one.

        Only meaningful for the SQLite backend. Kept because the file path is
        the natural handle for tests that read the database directly, around
        the server, to prove a write did or did not land.
        """
        return getattr(self.backend, "path", None)

    # Reads

    def table_names(self) -> list[str]:
        """Return every user table in the database."""
        return self.backend.table_names()

    def list_tables(self) -> dict[str, Any]:
        """Return each table with its row count and column names."""
        return {
            "backend": self.backend.name,
            "database": self.backend.description,
            "tables": self.backend.list_tables(),
        }

    def describe_table(self, table: str) -> dict[str, Any]:
        """Return the column and foreign key definitions of one table.

        Raises:
            ValueError: If no such table exists.
        """
        return self.backend.describe_table(table)

    def run_query(self, sql: str) -> dict[str, Any]:
        """Validate ``sql`` as a single SELECT and run it read-only.

        Raises:
            SqlRejected: If the statement is outside the read grammar.
            BackendError: If the database refuses the query.
        """
        return self.backend.run_query(validate_read(sql))

    # The write gate

    def propose_change(self, sql: str) -> dict[str, Any]:
        """Validate a write, run it uncommitted, and return a preview plus id.

        The statement is executed inside a real transaction so the preview is
        computed by the database rather than guessed, then rolled back. Nothing
        reaches disk. The returned ``change_id`` is the only way to commit it.

        Args:
            sql: A single ``INSERT``, ``UPDATE`` or ``DELETE``.

        Returns:
            The preview, the ``change_id`` and its expiry.

        Raises:
            SqlRejected: If the statement is outside the write grammar.
            BackendError: If the database refuses it. Nothing is written.
        """
        statement = validate_write(sql, frozenset(self.table_names()))
        preview = self.backend.preview_write(statement)
        assert statement.table is not None  # validate_write always resolves one
        proposal = self.proposals.add(statement.sql, statement.operation, statement.table, preview)
        return _proposal_payload(proposal, committed=False)

    def confirm_change(self, change_id: str) -> dict[str, Any]:
        """Commit a pending proposal, once.

        Two checks stand between a ``change_id`` and a commit. The statement is
        re-validated, so a proposal cannot outlive the rules that admitted it.
        Then the backend recomputes the preview inside the committing
        transaction and refuses if it no longer matches the one that was
        approved, so a proposal cannot outlive the data it was previewed
        against either.

        Args:
            change_id: The id returned by :meth:`propose_change`.

        Returns:
            The committed result: rows affected and the approved preview.

        Raises:
            ProposalError: If the id is unknown, already used or expired, or if
                the data moved since the preview was taken.
            SqlRejected: If re-validation fails.
            BackendError: If the database refuses the statement.
        """
        proposal = self.proposals.claim(change_id)
        try:
            statement = validate_write(proposal.sql, frozenset(self.table_names()))
            rows_affected = self.backend.commit_write(statement, proposal.preview)
        except PreviewChanged as error:
            # Nothing was committed, so hand the proposal back rather than
            # burning it, then report it as a proposal problem: the caller's
            # next move is to propose again, not to retry this id.
            self.proposals.release(change_id)
            raise ProposalError(str(error)) from error
        except Exception:
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


__all__ = ["BackendError", "PreviewChanged", "ProposalError", "SafeDatabase", "SqlRejected"]
