"""Shared fixtures. Every test runs against a throwaway copy of the demo data.

Nothing here reaches the network or needs a key: this project has no LLM in it,
so the whole suite is deterministic and offline.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from safe_db_mcp.engine import SafeDatabase
from safe_db_mcp.server import build_server

#: The tables the seed schema creates.
DEMO_TABLES = frozenset({"authors", "books", "members", "loans"})


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    """Return a path to a fresh, seeded database for one test."""
    return tmp_path / "library.db"


@pytest.fixture
def database(database_path: Path) -> SafeDatabase:
    """Return a :class:`SafeDatabase` over a fresh copy of the demo data."""
    return SafeDatabase(path=database_path)


@pytest.fixture
def short_ttl_database(database_path: Path) -> SafeDatabase:
    """Return a database whose proposals expire almost immediately."""
    return SafeDatabase(path=database_path, ttl_seconds=0.05)


@pytest.fixture
def mcp_server(database_path: Path):
    """Return an MCP server wired to a fresh database."""
    return build_server(database_path=database_path)


@pytest.fixture
def run_async():
    """Return a helper that runs a coroutine to completion.

    Used instead of pytest-asyncio's decorator so the async tests read the same
    way whether or not the plugin's default mode changes.
    """

    def runner(coroutine):
        return asyncio.run(coroutine)

    return runner
