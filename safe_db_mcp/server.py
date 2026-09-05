"""The MCP surface: five tools, and nothing else reachable.

This module is intentionally thin. Every rule lives in
:mod:`safe_db_mcp.engine`, :mod:`safe_db_mcp.validation` and
:mod:`safe_db_mcp.proposals`; the functions here only translate an MCP tool
call into a method call and an exception into a readable error string.

That thinness is the design. A client connected to this server can reach the
database in exactly five ways, and the shape of those five is fixed by the
protocol, not by an instruction the client is asked to follow.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .engine import ProposalError, SafeDatabase, SqlRejected

SERVER_NAME = "safe-database"

INSTRUCTIONS = """\
This server exposes one SQLite database through five validated tools.

Reads (list_tables, describe_table, run_query) run immediately on a read-only
connection. run_query accepts a single SELECT and nothing else.

Writes are two-step and cannot be done in one call. Send the INSERT, UPDATE or
DELETE to propose_change, which runs it in a transaction, rolls back, and
returns a preview with a single-use change_id. Show the preview to the human,
then call confirm_change with that id to commit. There is no tool that writes
without a change_id, so a write can never skip the preview.

DDL (DROP, ALTER, CREATE), PRAGMA, ATTACH, transaction control, SQL comments,
stacked statements and UPDATE/DELETE without a WHERE clause are all refused.
"""


def _json(payload: Any) -> str:
    """Render a tool result as indented JSON for the client to display."""
    return json.dumps(payload, indent=2, default=str)


def build_server(database_path: Path | str | None = None, ttl_seconds: float | None = None):
    """Construct the MCP server and register its five tools.

    Args:
        database_path: Location of the SQLite file. Defaults to
            ``SAFEDB_DATABASE_PATH`` or ``data/library.db``.
        ttl_seconds: Proposal time-to-live override, in seconds.

    Returns:
        A configured ``MCPServer`` ready for ``run()`` or for an in-process
        client to connect to.
    """
    database = SafeDatabase(path=database_path, ttl_seconds=ttl_seconds)
    mcp = MCPServer(
        name=SERVER_NAME,
        title="Safe Database",
        instructions=INSTRUCTIONS,
        version="1.0.0",
    )

    @mcp.tool()
    def list_tables() -> str:
        """List every table in the database with its row count and columns.

        Read-only. Runs immediately, no confirmation needed.
        """
        return _json(database.list_tables())

    @mcp.tool()
    def describe_table(table: str) -> str:
        """Show the columns, types, constraints and foreign keys of one table.

        Read-only. Runs immediately, no confirmation needed.

        Args:
            table: Name of the table to describe.
        """
        try:
            return _json(database.describe_table(table))
        except ValueError as error:
            raise ToolError(str(error)) from error

    @mcp.tool()
    def run_query(sql: str) -> str:
        """Run one read-only SELECT and return the rows.

        Only a single SELECT is accepted. Anything else - a write, DDL, PRAGMA,
        ATTACH, a stacked statement, a comment - is rejected before it reaches
        the database, and the connection used here is opened read-only anyway.

        Args:
            sql: A single SELECT statement.
        """
        try:
            return _json(database.run_query(sql))
        except SqlRejected as error:
            raise ToolError(f"Rejected: {error}") from error
        except sqlite3.Error as error:
            raise ToolError(f"SQLite refused the query: {error}") from error

    @mcp.tool()
    def propose_change(sql: str) -> str:
        """Preview a write without committing it, and get a change_id back.

        The statement is validated, executed inside a transaction to compute a
        real preview, then rolled back. Nothing is written. Show the returned
        preview to the human and call confirm_change with the change_id to
        commit it. The id is single use and expires.

        Accepts one INSERT, UPDATE or DELETE against an existing table.
        UPDATE and DELETE must have a WHERE clause.

        Args:
            sql: A single INSERT, UPDATE or DELETE statement.
        """
        try:
            return _json(database.propose_change(sql))
        except SqlRejected as error:
            raise ToolError(f"Rejected: {error}") from error
        except sqlite3.Error as error:
            raise ToolError(f"SQLite refused the change, nothing was written: {error}") from error

    @mcp.tool()
    def confirm_change(change_id: str) -> str:
        """Commit a change that propose_change previewed.

        The statement is re-validated and re-executed, then committed. The
        change_id must be one propose_change returned, still unused and not yet
        expired. There is no way to commit a write without one.

        Args:
            change_id: The id returned by propose_change.
        """
        try:
            return _json(database.confirm_change(change_id))
        except ProposalError as error:
            raise ToolError(f"Refused: {error}") from error
        except SqlRejected as error:
            raise ToolError(f"Rejected on re-validation: {error}") from error
        except sqlite3.Error as error:
            raise ToolError(f"SQLite refused the change, nothing was written: {error}") from error

    @mcp.tool()
    def list_pending_changes() -> str:
        """List the proposed changes that are still awaiting confirmation.

        Read-only. Shows each pending change_id, its SQL and how long it has
        left before it expires.
        """
        return _json(database.list_pending_changes())

    return mcp


def main() -> None:
    """Entry point: build the server and serve it over stdio."""
    build_server().run(transport="stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
