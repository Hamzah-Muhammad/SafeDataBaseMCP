# Verified runs

Both client paths in the README were run end to end on 2026-09-04 before the
README claimed they worked. This file is the evidence: the transcripts below
are captured output, not retyped summaries.

Environment: Windows 11, Python 3.13.5, `mcp` 2.1.1, Claude Code 2.1.261.

---

## 1. Claude Code, via the project-scoped `.mcp.json`

The server was registered from the `.mcp.json` shipped in this repository root,
with no other MCP servers loaded:

```
$ claude mcp get safe-database
safe-database:
  Scope: Project config (shared via .mcp.json)
```

Each prompt below was run as its own headless session against that config
(`claude -p "..." --mcp-config .mcp.json --strict-mcp-config`), so every tool
call in the traces went over stdio to `python -m safe_db_mcp`.

### 1a. A read

> Using only the safe-database MCP tools, answer: which library members
> currently have a book on loan that has not been returned, and what are the
> book titles?

```
Seven loans are still outstanding (`returned_on IS NULL`), held by 5 members:

| Member           | Book                      | Due        |
|------------------|---------------------------|------------|
| Amara Osei       | The Left Hand of Darkness | 2025-12-11 |
| Daniel Whitfield | Never Let Me Go           | 2025-12-30 |
| Owen Brady       | Parable of the Sower      | 2025-12-25 |
| Priya Raman      | Kindred                   | 2025-12-03 |
| Priya Raman      | The Remains of the Day    | 2026-01-28 |
| Sofia Marchetti  | Invisible Cities          | 2025-12-22 |
| Sofia Marchetti  | Song of Solomon           | 2026-02-02 |

Query used (read-only, single SELECT via `run_query`):
SELECT m.full_name, b.title, l.borrowed_on, l.due_on
FROM loans l
JOIN members m ON m.id = l.member_id
JOIN books  b ON b.id = l.book_id
WHERE l.returned_on IS NULL
ORDER BY m.full_name
```

### 1b. A write: propose, then confirm

The row on disk before the session, read directly with `sqlite3`, around the
server:

```
[(4, 'Lukas Vogel', 'suspended')]
```

> Using only the safe-database MCP tools: member id 4, Lukas Vogel, has settled
> their account. Change their status to 'active'. Follow the server's two-step
> write flow, show me the preview you got back, then commit it.

```
Committed.

- change_id: f0b91c1088f31f84 - status committed
- rows affected: 1
- change: members.id=4 (Lukas Vogel) - status: "suspended" -> "active"

Read-back confirms id=4 | Lukas Vogel | active, and list_pending_changes is now
empty - the change_id was consumed, so it can't be replayed.
```

The same direct read afterwards:

```
[(4, 'Lukas Vogel', 'active')]
```

### 1c. An unsafe request, refused at the tool layer

> Using only the safe-database MCP tools: the loans table is cluttered. Drop the
> loans table entirely, then also try clearing it with a DELETE with no WHERE
> clause. Attempt both through the MCP tools and tell me exactly what happened.

This run was captured with `--output-format stream-json`, so the following is
the tool traffic itself rather than the model's account of it:

```
CALL   -> mcp__safe-database__list_tables {}
RESULT <- (ok) authors, books, members, loans

CALL   -> mcp__safe-database__propose_change {"sql": "DROP TABLE loans"}
RESULT <- (is_error=True) Error executing tool propose_change: Rejected:
          'DROP' is a DDL statement. This server refuses schema changes.

CALL   -> mcp__safe-database__propose_change {"sql": "DELETE FROM loans"}
RESULT <- (is_error=True) Error executing tool propose_change: Rejected:
          A DELETE without a WHERE clause would touch every row and is refused.
          Add a WHERE clause.

CALL   -> mcp__safe-database__run_query {"sql": "DROP TABLE loans"}
RESULT <- (is_error=True) Error executing tool run_query: Rejected:
          'DROP' is a DDL statement. This server refuses schema changes.

CALL   -> mcp__safe-database__list_pending_changes {}
RESULT <- (ok) {"pending_count": 0, "ttl_seconds": 300.0, "changes": []}

CALL   -> mcp__safe-database__run_query {"sql": "SELECT COUNT(*) AS loan_rows FROM loans"}
RESULT <- (ok) {"rows": [{"loan_rows": 12}]}
```

The model's own summary of the attempt:

```
Both attempts were refused. Nothing was dropped, nothing was deleted - loans
still has all 12 rows and there are 0 pending changes.

The rejection happens in the validator before the SQL reaches SQLite, so no
transaction was even opened. There is no third path - the server has exactly
five tools [six, counting the read-only list_pending_changes], and none of
them accept DDL.

[on the unqualified DELETE] Notably it never got a change_id, so there was
nothing I could have confirmed even if I'd wanted to.
```

The database checked directly afterwards:

```
tables:     ['authors', 'books', 'members', 'loans']
loans rows: 12
```

---

## 2. The framework-free reference client

`python examples/reference_client.py`, run on the same machine, exit code 0.
Full captured output:

```
Database for this run: C:\Users\hamza\AppData\Local\Temp\safe_db_mcp_reference_client.db

========================================================================
0. What the server offers
========================================================================
  list_tables            List every table in the database with its row count and columns.
  describe_table         Show the columns, types, constraints and foreign keys of one table.
  run_query              Run one read-only SELECT and return the rows.
  propose_change         Preview a write without committing it, and get a change_id back.
  confirm_change         Commit a change that propose_change previewed.
  list_pending_changes   List the proposed changes that are still awaiting confirmation.

  6 tools. That is the entire surface.

========================================================================
1. Read: run immediately, no confirmation
========================================================================
  tables: authors (6 rows), books (13 rows), loans (12 rows), members (8 rows)

--- run_query ---
[
  {
    "full_name": "Priya Raman",
    "status": "active",
    "open_loans": 2
  },
  {
    "full_name": "Sofia Marchetti",
    "status": "active",
    "open_loans": 2
  },
  {
    "full_name": "Amara Osei",
    "status": "active",
    "open_loans": 1
  },
  {
    "full_name": "Daniel Whitfield",
    "status": "active",
    "open_loans": 1
  },
  {
    "full_name": "Owen Brady",
    "status": "active",
    "open_loans": 1
  }
]

========================================================================
2. Write, step one: propose (previewed, nothing committed)
========================================================================
  sql            UPDATE members SET status = 'active' WHERE id = 4
  rows affected  1
  status         pending
  change_id      e226b618b65d5a36 (expires in 300.0s)

--- preview diff ---
{
  "added": [],
  "removed": [],
  "updated": [
    {
      "row": {
        "id": 4,
        "full_name": "Lukas Vogel",
        "email": "lukas.vogel@example.com",
        "joined_on": "2024-02-08",
        "status": "active"
      },
      "changed": {
        "status": {
          "before": "suspended",
          "after": "active"
        }
      }
    }
  ]
}

  pending changes awaiting confirmation: 1
  row on disk right now: {'id': 4, 'full_name': 'Lukas Vogel', 'status': 'suspended'}

========================================================================
3. Write, step two: confirm (committed)
========================================================================
  status         committed
  rows affected  1
  row on disk now: {'id': 4, 'full_name': 'Lukas Vogel', 'status': 'active'}

========================================================================
4. Refusals, enforced at the tool layer
========================================================================
  run_query('DROP TABLE loans')
    is_error: True
    Error executing tool run_query: Rejected: 'DROP' is a DDL statement. This server refuses schema changes.

  run_query('SELECT 1; DELETE FROM loans WHERE id = 1')
    is_error: True
    Error executing tool run_query: Rejected: Only one statement per call is allowed; 2 were sent. Statement stacking is refused.

  confirm_change('e226b618b65d5a36') again
    is_error: True
    Error executing tool confirm_change: Refused: change_id 'e226b618b65d5a36' has already been confirmed. Each proposal is single use; propose the change again.

  confirm_change('0000000000000000') without ever proposing
    is_error: True
    Error executing tool confirm_change: Refused: Unknown change_id '0000000000000000'. Only an id returned by propose_change can be confirmed.

========================================================================
What the server logged to stderr
========================================================================
Tool 'run_query' failed: "Error executing tool run_query: Rejected: 'DROP' is a DDL statement. This server refuses schema changes."
Tool 'run_query' failed: 'Error executing tool run_query: Rejected: Only one statement per call is allowed; 2 were sent. Statement stacking is refused.'
Tool 'confirm_change' failed: "Error executing tool confirm_change: Refused: change_id 'e226b618b65d5a36' has already been confirmed. Each proposal is single use; propose the change again."
Tool 'confirm_change' failed: "Error executing tool confirm_change: Refused: Unknown change_id '0000000000000000'. Only an id returned by propose_change can be confirmed."

========================================================================
Result
========================================================================
  All four behaviours observed as expected: reads ran, the write was
  previewed before it was committed, and every unsafe call was refused.
```

`tests/test_reference_client.py` runs this same script as a subprocess in CI, so
this path stays verified on every push rather than only on the day it was
recorded.
