"""The reference client, run for real.

Everything else in the suite talks to the server in process. This module runs
``examples/reference_client.py`` as its own process, which spawns the server as
a second process and talks to it over stdio - the same path Claude Code uses.

It exists so the second client in the README is covered by CI rather than only
by a screenshot, and so a change that breaks the packaged entry point
(``python -m safe_db_mcp``) cannot pass unnoticed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "examples" / "reference_client.py"


@pytest.fixture(scope="module")
def demo_run(tmp_path_factory) -> subprocess.CompletedProcess[str]:
    """Run the reference client once against a scratch database."""
    database = tmp_path_factory.mktemp("reference") / "library.db"
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--database", str(database)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_the_reference_client_completes_successfully(demo_run) -> None:
    assert demo_run.returncode == 0, f"stdout:\n{demo_run.stdout}\nstderr:\n{demo_run.stderr}"


def test_it_discovers_the_five_operations(demo_run) -> None:
    for tool in ("list_tables", "describe_table", "run_query", "propose_change", "confirm_change"):
        assert tool in demo_run.stdout


def test_it_shows_a_read_answering_immediately(demo_run) -> None:
    assert "Read: run immediately" in demo_run.stdout
    assert "Priya Raman" in demo_run.stdout


def test_it_shows_the_write_previewed_before_it_is_committed(demo_run) -> None:
    assert "nothing committed" in demo_run.stdout
    assert "'status': 'suspended'" in demo_run.stdout
    assert "'status': 'active'" in demo_run.stdout


def test_it_shows_an_unsafe_statement_refused(demo_run) -> None:
    assert "DROP" in demo_run.stdout
    assert "refuses schema changes" in demo_run.stdout


def test_it_shows_a_spent_change_id_refused(demo_run) -> None:
    assert "already been confirmed" in demo_run.stdout
    assert "Unknown change_id" in demo_run.stdout


def test_it_reports_no_failures(demo_run) -> None:
    assert "FAILED:" not in demo_run.stdout
    assert "All four behaviours observed as expected" in demo_run.stdout
