"""What a storage backend has to provide, and nothing more.

The propose/confirm gate does not know what database it is gating. It knows how
to validate a statement, how to mint and spend a ``change_id``, and how to ask a
backend for four things: the schema, a read, a rolled-back preview, and a
commit. Everything dialect-specific lives behind this interface.

That split is the same argument the project makes about tool surfaces, applied
one layer down. The guarantee is "no write reaches disk without a preview the
caller had to hold". If that guarantee were implemented inside the SQLite code,
adding Postgres would mean re-earning it. Here it is implemented once, above
both backends, and a new backend cannot weaken it because a backend is never
asked to commit anything the engine did not first preview.
"""

from __future__ import annotations

import abc
from typing import Any

from ..validation import ValidatedStatement


class BackendError(RuntimeError):
    """Raised when the database refuses something the grammar allowed.

    Wraps the driver's own exception so callers do not have to catch both
    ``sqlite3.Error`` and ``psycopg.Error``. The original is kept as
    ``__cause__``, and the message is safe to show to whoever sent the SQL.
    """


class PreviewChanged(BackendError):
    """Raised when the data moved between propose and confirm.

    This is the concurrent-writer race, caught rather than tolerated. Confirming
    recomputes the preview inside the committing transaction and compares it to
    the one the caller approved. If they differ, nothing is committed and the
    caller is told to propose again against current data.
    """


class Backend(abc.ABC):
    """One database, reachable only through validated operations."""

    #: Human-readable name of the backend, used in payloads and errors.
    name: str = "backend"

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """A short string identifying what this backend is connected to.

        Never includes a password. For Postgres this is ``host:port/database``,
        for SQLite it is the file path.
        """

    @abc.abstractmethod
    def table_names(self) -> list[str]:
        """Return the user tables in the database, sorted."""

    @abc.abstractmethod
    def list_tables(self) -> list[dict[str, Any]]:
        """Return one entry per table with its row count and column names."""

    @abc.abstractmethod
    def describe_table(self, table: str) -> dict[str, Any]:
        """Return columns, constraints and foreign keys for one table.

        Raises:
            ValueError: If no such table exists.
        """

    @abc.abstractmethod
    def run_query(self, statement: ValidatedStatement) -> dict[str, Any]:
        """Execute a validated ``SELECT`` on a read-only connection.

        Implementations must make the read path incapable of writing at the
        database level, not merely unlikely to. Validation is the first line;
        this is the second.
        """

    @abc.abstractmethod
    def preview_write(self, statement: ValidatedStatement) -> dict[str, Any]:
        """Run a validated write in a transaction, snapshot it, and roll back.

        Returns:
            A preview: ``rows_affected``, a row-level ``diff`` where the table
            is small enough and identifiable enough to snapshot, and
            ``diff_available`` saying which of those happened.

        Nothing may reach disk. The rollback has to hold even if the statement
        raises.
        """

    @abc.abstractmethod
    def commit_write(self, statement: ValidatedStatement, approved: dict[str, Any]) -> int:
        """Re-run a validated write and commit it, if the preview still holds.

        Args:
            statement: The re-validated statement to execute.
            approved: The preview the caller approved, as returned by
                :meth:`preview_write`.

        Returns:
            The number of rows actually affected by the committed statement.

        Raises:
            PreviewChanged: If recomputing the preview inside the committing
                transaction no longer matches ``approved``. Nothing is
                committed in that case.
            BackendError: If the database refuses the statement.
        """
