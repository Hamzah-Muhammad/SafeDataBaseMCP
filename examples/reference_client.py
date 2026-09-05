"""A minimal MCP client for the safe-database server, with no framework at all.

This script exists to prove one thing: the server is a standard MCP server, not
something shaped around Claude Code. It uses the official ``mcp`` Python SDK
client and the standard library. There is no agent framework, no LLM, no API
key and no network call - it spawns the server over stdio, calls tools, and
prints what comes back.

It walks the same four behaviours the README describes:

1. a read that runs immediately (``list_tables``, then ``run_query``);
2. a write proposed and previewed without being committed (``propose_change``);
3. that same write committed by id (``confirm_change``);
4. an unsafe statement refused at the tool layer (``DROP TABLE``), plus a
   replay of the spent ``change_id`` to show single use is enforced.

Run it from the repository root::

    python examples/reference_client.py

By default it works on a scratch database under the system temp directory, so
running it never touches ``data/library.db``. Pass ``--database <path>`` to
point it somewhere else.

The server writes its own log to stderr. That is captured here and printed in
one block at the end rather than interleaved, so the conversation reads in
order and the server's record of what it refused stays visible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The statement the demo proposes and then commits.
DEMO_WRITE = "UPDATE members SET status = 'active' WHERE id = 4"

#: The statement the demo expects the server to refuse.
DEMO_UNSAFE = "DROP TABLE loans"


def heading(text: str) -> None:
    """Print a section header."""
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def show(label: str, payload: Any) -> None:
    """Print a labelled block of JSON."""
    print(f"\n--- {label} ---")
    print(json.dumps(payload, indent=2, default=str))


def result_text(result: Any) -> str:
    """Return the text of a tool result, whether it succeeded or errored."""
    parts = [block.text for block in result.content if getattr(block, "text", None)]
    return "\n".join(parts)


def result_json(result: Any) -> dict[str, Any]:
    """Parse a successful tool result as JSON, failing loudly if it errored."""
    if result.is_error:
        raise RuntimeError(f"Tool call failed unexpectedly: {result_text(result)}")
    return json.loads(result_text(result))


async def run_demo(database_path: Path) -> int:
    """Connect over stdio and walk the read, write and refusal paths.

    Args:
        database_path: The SQLite file the server should use. It is created and
            seeded by the server on first connect if it does not exist.

    Returns:
        A process exit code: ``0`` if every step behaved as expected.
    """
    env = dict(os.environ)
    env["SAFEDB_DATABASE_PATH"] = str(database_path)

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "safe_db_mcp"],
        cwd=str(REPO_ROOT),
        env=env,
    )

    failures: list[str] = []
    # errlog needs a real file handle, so the server's stderr is captured to a
    # scratch file and replayed at the end rather than interleaved.
    log_path = database_path.with_suffix(".server.log")

    with log_path.open("w", encoding="utf-8") as server_log:
        async with Client(stdio_client(parameters, errlog=server_log)) as client:
            heading("0. What the server offers")
            listing = await client.list_tools()
            for tool in listing.tools:
                summary = (tool.description or "").strip().splitlines()[0]
                print(f"  {tool.name:22} {summary}")
            print(f"\n  {len(listing.tools)} tools. That is the entire surface.")

            # 1. A read runs immediately.
            heading("1. Read: run immediately, no confirmation")
            tables = result_json(await client.call_tool("list_tables", {}))
            print(
                "  tables: "
                + ", ".join(f"{t['name']} ({t['rows']} rows)" for t in tables["tables"])
            )

            query = (
                "SELECT m.full_name, m.status, COUNT(l.id) AS open_loans "
                "FROM members m LEFT JOIN loans l "
                "ON l.member_id = m.id AND l.returned_on IS NULL "
                "GROUP BY m.id ORDER BY open_loans DESC, m.full_name LIMIT 5"
            )
            rows = result_json(await client.call_tool("run_query", {"sql": query}))
            show("run_query", rows["rows"])

            # 2. A write is previewed, not committed.
            heading("2. Write, step one: propose (previewed, nothing committed)")
            proposal = result_json(await client.call_tool("propose_change", {"sql": DEMO_WRITE}))
            change_id = proposal["change_id"]
            print(f"  sql            {proposal['sql']}")
            print(f"  rows affected  {proposal['rows_affected']}")
            print(f"  status         {proposal['status']}")
            print(f"  change_id      {change_id} (expires in {proposal['expires_in_seconds']}s)")
            show("preview diff", proposal["diff"])

            pending = result_json(await client.call_tool("list_pending_changes", {}))
            print(f"\n  pending changes awaiting confirmation: {pending['pending_count']}")

            before = result_json(
                await client.call_tool(
                    "run_query", {"sql": "SELECT id, full_name, status FROM members WHERE id = 4"}
                )
            )
            print(f"  row on disk right now: {before['rows'][0]}")
            if before["rows"][0]["status"] == "active":
                failures.append("propose_change appears to have committed; it must not.")

            # 3. Confirming commits it, exactly once.
            heading("3. Write, step two: confirm (committed)")
            committed = result_json(
                await client.call_tool("confirm_change", {"change_id": change_id})
            )
            print(f"  status         {committed['status']}")
            print(f"  rows affected  {committed['rows_affected']}")

            after = result_json(
                await client.call_tool(
                    "run_query", {"sql": "SELECT id, full_name, status FROM members WHERE id = 4"}
                )
            )
            print(f"  row on disk now: {after['rows'][0]}")
            if after["rows"][0]["status"] != "active":
                failures.append("confirm_change did not commit the write.")

            # 4. Refusals.
            heading("4. Refusals, enforced at the tool layer")

            unsafe = await client.call_tool("run_query", {"sql": DEMO_UNSAFE})
            print(f"  run_query('{DEMO_UNSAFE}')")
            print(f"    is_error: {unsafe.is_error}\n    {result_text(unsafe).strip()}")
            if not unsafe.is_error:
                failures.append("DROP TABLE was not refused.")

            stacked = await client.call_tool(
                "run_query", {"sql": "SELECT 1; DELETE FROM loans WHERE id = 1"}
            )
            print("\n  run_query('SELECT 1; DELETE FROM loans WHERE id = 1')")
            print(f"    is_error: {stacked.is_error}\n    {result_text(stacked).strip()}")
            if not stacked.is_error:
                failures.append("A stacked statement was not refused.")

            replay = await client.call_tool("confirm_change", {"change_id": change_id})
            print(f"\n  confirm_change('{change_id}') again")
            print(f"    is_error: {replay.is_error}\n    {result_text(replay).strip()}")
            if not replay.is_error:
                failures.append("A spent change_id was accepted a second time.")

            invented = await client.call_tool("confirm_change", {"change_id": "0000000000000000"})
            print("\n  confirm_change('0000000000000000') without ever proposing")
            print(f"    is_error: {invented.is_error}\n    {result_text(invented).strip()}")
            if not invented.is_error:
                failures.append("An invented change_id was accepted.")

    heading("What the server logged to stderr")
    logged = log_path.read_text(encoding="utf-8").strip()
    print(logged if logged else "  (nothing)")
    log_path.unlink(missing_ok=True)

    heading("Result")
    if failures:
        for failure in failures:
            print(f"  FAILED: {failure}")
        return 1
    print("  All four behaviours observed as expected: reads ran, the write was")
    print("  previewed before it was committed, and every unsafe call was refused.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and run the demo."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="SQLite file to use. Defaults to a fresh scratch copy in the temp directory.",
    )
    args = parser.parse_args(argv)

    database_path = args.database
    if database_path is None:
        scratch = Path(tempfile.gettempdir()) / "safe_db_mcp_reference_client.db"
        scratch.unlink(missing_ok=True)
        database_path = scratch

    print(f"Database for this run: {database_path}")
    return asyncio.run(run_demo(database_path))


if __name__ == "__main__":
    raise SystemExit(main())
