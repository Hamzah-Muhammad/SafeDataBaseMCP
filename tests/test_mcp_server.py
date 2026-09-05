"""The same guarantees, exercised through the MCP protocol.

:mod:`tests.test_engine` proves the rules hold in plain Python. These tests
prove nothing is lost in the adapter: the tool surface is exactly six tools,
errors come back as protocol errors rather than as silent successes, and a
client that only speaks MCP still cannot commit a write without a preview.

An in-process client is used here rather than a subprocess, so these stay fast;
:mod:`tests.test_reference_client` covers the real stdio path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.client import Client

EXPECTED_TOOLS = {
    "list_tables",
    "describe_table",
    "run_query",
    "propose_change",
    "confirm_change",
    "list_pending_changes",
}


def text_of(result: Any) -> str:
    """Return the concatenated text content of a tool result."""
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


def json_of(result: Any) -> dict[str, Any]:
    """Parse a successful tool result as JSON."""
    assert not result.is_error, text_of(result)
    return json.loads(text_of(result))


class TestToolSurface:
    """What a connected client can actually see."""

    def test_exactly_the_expected_tools_are_exposed(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                return await client.list_tools()

        listing = run_async(check())
        assert {tool.name for tool in listing.tools} == EXPECTED_TOOLS

    def test_every_tool_documents_itself(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                return await client.list_tools()

        for tool in run_async(check()).tools:
            assert tool.description and tool.description.strip()

    def test_the_server_ships_instructions_describing_the_gate(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                return client.instructions

        instructions = run_async(check()) or ""
        assert "propose_change" in instructions
        assert "confirm_change" in instructions


class TestReadsOverMcp:
    """Reads answer immediately."""

    def test_list_tables(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                return json_of(await client.call_tool("list_tables", {}))

        payload = run_async(check())
        assert {t["name"] for t in payload["tables"]} == {
            "authors",
            "books",
            "members",
            "loans",
        }
        assert payload["tables"][0]["rows"] > 0

    def test_describe_table(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                return json_of(await client.call_tool("describe_table", {"table": "books"}))

        payload = run_async(check())
        assert {c["name"] for c in payload["columns"]} >= {"title", "isbn", "author_id"}

    def test_describe_table_errors_on_an_unknown_table(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                return await client.call_tool("describe_table", {"table": "ledger"})

        result = run_async(check())
        assert result.is_error
        assert "Unknown table" in text_of(result)

    def test_run_query_returns_rows(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                return json_of(
                    await client.call_tool(
                        "run_query", {"sql": "SELECT name FROM authors ORDER BY name"}
                    )
                )

        payload = run_async(check())
        assert payload["row_count"] == 6
        assert payload["rows"][0]["name"] == "Chinua Achebe"


class TestWriteGateOverMcp:
    """The propose/confirm gate holds over the protocol."""

    def test_propose_then_confirm_commits_once(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                proposal = json_of(
                    await client.call_tool(
                        "propose_change",
                        {"sql": "UPDATE members SET status = 'active' WHERE id = 4"},
                    )
                )
                mid = json_of(
                    await client.call_tool(
                        "run_query", {"sql": "SELECT status FROM members WHERE id = 4"}
                    )
                )
                committed = json_of(
                    await client.call_tool("confirm_change", {"change_id": proposal["change_id"]})
                )
                after = json_of(
                    await client.call_tool(
                        "run_query", {"sql": "SELECT status FROM members WHERE id = 4"}
                    )
                )
                replay = await client.call_tool(
                    "confirm_change", {"change_id": proposal["change_id"]}
                )
                return proposal, mid, committed, after, replay

        proposal, mid, committed, after, replay = run_async(check())

        assert proposal["status"] == "pending"
        assert mid["rows"][0]["status"] == "suspended", "propose_change must not commit"
        assert committed["status"] == "committed"
        assert after["rows"][0]["status"] == "active"
        assert replay.is_error
        assert "already been confirmed" in text_of(replay)

    def test_pending_changes_are_visible_between_the_two_steps(self, mcp_server, run_async) -> None:
        async def check():
            async with Client(mcp_server) as client:
                await client.call_tool("propose_change", {"sql": "DELETE FROM loans WHERE id = 12"})
                return json_of(await client.call_tool("list_pending_changes", {}))

        pending = run_async(check())
        assert pending["pending_count"] == 1
        assert pending["changes"][0]["sql"].startswith("DELETE FROM loans")


class TestRejectionsOverMcp:
    """Every refusal reaches the client as an error, with a reason."""

    @pytest.mark.parametrize(
        "tool,arguments,fragment",
        [
            ("run_query", {"sql": "DROP TABLE books"}, "DDL"),
            ("run_query", {"sql": "SELECT 1; DROP TABLE books"}, "stacking"),
            ("run_query", {"sql": "PRAGMA table_info(books)"}, "not allowed"),
            ("run_query", {"sql": "ATTACH DATABASE 'x.db' AS x"}, "not allowed"),
            ("run_query", {"sql": "SELECT * FROM sqlite_master"}, "internal state"),
            (
                "run_query",
                {"sql": "DELETE FROM loans WHERE id = 1"},
                "read-only",
            ),
            ("propose_change", {"sql": "DROP TABLE books"}, "DDL"),
            ("propose_change", {"sql": "DELETE FROM loans"}, "WHERE clause"),
            ("propose_change", {"sql": "INSERT INTO ledger (a) VALUES (1)"}, "Unknown table"),
            (
                "propose_change",
                {"sql": "UPDATE members SET status='x' WHERE id=1; DROP TABLE members"},
                "stacking",
            ),
            ("confirm_change", {"change_id": "0000000000000000"}, "Unknown change_id"),
            ("confirm_change", {"change_id": ""}, "change_id is required"),
        ],
    )
    def test_unsafe_calls_are_refused(
        self, mcp_server, run_async, tool: str, arguments: dict, fragment: str
    ) -> None:
        async def check():
            async with Client(mcp_server) as client:
                result = await client.call_tool(tool, arguments)
                unchanged = json_of(await client.call_tool("list_tables", {}))
                pending = json_of(await client.call_tool("list_pending_changes", {}))
                return result, unchanged, pending

        result, unchanged, pending = run_async(check())

        assert result.is_error, f"{tool}({arguments}) should have been refused"
        assert fragment in text_of(result)
        assert {t["name"] for t in unchanged["tables"]} == {
            "authors",
            "books",
            "members",
            "loans",
        }
        assert pending["pending_count"] == 0

    def test_there_is_no_tool_that_writes_in_one_step(self, mcp_server, run_async) -> None:
        # The structural claim the project rests on: no tool other than
        # confirm_change writes, and confirm_change needs an id only
        # propose_change can mint.
        async def check():
            async with Client(mcp_server) as client:
                listing = await client.list_tools()
                names = {tool.name for tool in listing.tools}
                schema = next(t.input_schema for t in listing.tools if t.name == "confirm_change")
                return names, schema

        names, schema = run_async(check())
        assert names == EXPECTED_TOOLS
        assert list(schema["properties"]) == ["change_id"]
