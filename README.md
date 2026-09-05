# SafeDataBaseMCP

[![CI](https://github.com/Hamzah-Muhammad/SafeDataBaseMCP/actions/workflows/ci.yml/badge.svg)](https://github.com/Hamzah-Muhammad/SafeDataBaseMCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg?logo=python&logoColor=white)](pyproject.toml)
[![MCP](https://img.shields.io/badge/MCP-stdio-6E56CF.svg)](https://modelcontextprotocol.io)
[![No LLM](https://img.shields.io/badge/LLM-none%20required-brightgreen.svg)](#no-api-key-anywhere)

**An MCP server that gives an agent a database, and takes away the ability to wreck it.**

Reads answer immediately. Writes cannot happen in one step: the agent sends an `INSERT`/`UPDATE`/`DELETE` to `propose_change`, the server runs it inside a transaction to compute a real preview, rolls it back, and hands back a single-use `change_id`. Nothing reaches disk until `confirm_change` is called with that id. There is no tool that writes without one.

```
list_tables      ---> answers immediately (read-only connection)
describe_table   ---> answers immediately
run_query        ---> answers immediately, single SELECT only

propose_change   ---> BEGIN -> execute -> snapshot diff -> ROLLBACK
                      returns: preview + single-use change_id, expires in 300s
                          |
                   a human reads the diff
                          |
confirm_change   ---> re-validate -> execute -> COMMIT    (once, then the id is dead)
```

Everything outside that grammar is refused before it reaches SQLite: `DROP`, `ALTER`, `CREATE`, `PRAGMA`, `ATTACH`, `VACUUM`, transaction control, stacked statements, SQL comments, `load_extension`, SQLite's internal tables, and `UPDATE`/`DELETE` with no `WHERE` clause.

---

## Why an MCP server at all, when the agent could just run SQL?

This is the actual point of the project, so it goes first.

A capable coding agent - Claude Code, for instance - already has a Bash tool. It can reach a SQLite file with three lines of Python and do anything it likes to it. Connecting it to this MCP server gives it **no new capability whatsoever**. A tool that adds nothing an agent could not already do would normally be pointless.

What it adds is a **constraint**, and that is the whole design:

| Without the server | With the server |
|---|---|
| The interface is "run arbitrary Python or shell against the database file". | The interface is six named tools: five operations, plus a read-only view of what is pending. |
| Every statement the agent can compose is reachable, including `DROP TABLE`. | The grammar is checked before execution; anything outside it is refused with a reason. |
| A destructive write is one tool call away, and looks like any other tool call. | A destructive write is impossible in one call. The tool that writes accepts only a `change_id`, and only `propose_change` can mint one. |
| "Show me a preview before you change anything" is an instruction in a prompt. | The preview is the only way to obtain the token that the write tool requires. |
| Reads and writes go through the same all-powerful channel. | Reads run on a connection opened `mode=ro`. SQLite itself refuses a write on that handle. |

The last two rows are the ones that matter.

**The gate is structural, not advisory.** Plenty of agent setups get this behaviour by asking for it: a system prompt that says "always show the user a diff before writing." That works until the model is having a bad day, until the context is long, until the instruction is three thousand tokens up the conversation. It is a convention, and conventions are followed statistically.

Here, `confirm_change` takes exactly one parameter and it is a `change_id`. That id is a random token minted by `propose_change` and held in a store that enforces single use and expiry. The model cannot invent one, cannot reuse a spent one, and cannot skip the preview step, because there is no argument it could pass that would let it. The preview is not a courtesy the agent extends to the user; it is a value the agent physically needs in order to proceed. Removing the preview step from the flow would require editing this repository, not writing a more persuasive prompt.

**Defence in depth, not one clever check.** Validation is a parser-based allowlist, not a regex denylist, and it is not the only thing standing between a bad statement and the data:

1. `sqlparse` classifies the statement; anything that is not a single `SELECT` (read path) or a single `INSERT`/`UPDATE`/`DELETE` against a known table (write path) is refused.
2. The flattened token stream is scanned for forbidden keywords, forbidden functions and SQLite's internal tables, so DML hidden in a CTE or a `UNION` is caught too.
3. Reads execute on a connection opened `mode=ro`, so even a validation bug could not produce a write on the read path.
4. `propose_change` computes its preview inside a transaction that is rolled back in a `finally` block, so a preview cannot persist even if the statement raises.
5. `confirm_change` re-validates the SQL before re-executing it, so a proposal cannot outlive the rules that admitted it.

Each of those is boring on its own. The point is that no single one of them is load bearing.

**What this is not.** This is not a claim that a sandboxed tool surface makes an agent safe in general. The agent in the room still has Bash. What it demonstrates is the design move: when you want a guarantee about how an agent touches something that matters, you get it by shrinking and shaping the interface, not by adding more instructions to a prompt. That move is the reason MCP is interesting, and it is what this repository is built to show.

---

## Two ways to connect

The server is a plain MCP server over stdio. It is not built around any one client, and this repository proves that by shipping two, both of which were run end to end before this README was written.

### 1. Claude Code (the primary demo)

A project-scoped [`.mcp.json`](.mcp.json) sits in the repository root, so cloning the repo and opening it in Claude Code is the entire setup:

```bash
git clone https://github.com/Hamzah-Muhammad/SafeDataBaseMCP.git
cd SafeDataBaseMCP
pip install -r requirements.txt
claude
```

Claude Code discovers `.mcp.json`, asks once whether to trust the project's MCP servers, and the tools appear as `mcp__safe-database__*`. The demo database seeds itself on first connect. Check it is connected with `/mcp`, or:

```bash
claude mcp get safe-database
```

If you would rather register it yourself instead of using the shipped file:

```bash
claude mcp add safe-database -- python -m safe_db_mcp
```

Then ask it things. A read:

> **Which library members currently have a book on loan that has not been returned?**

```
Seven loans are still outstanding, held by 5 members:

| Member           | Book                      | Due        |
|------------------|---------------------------|------------|
| Amara Osei       | The Left Hand of Darkness | 2025-12-11 |
| Daniel Whitfield | Never Let Me Go           | 2025-12-30 |
| Priya Raman      | Kindred                   | 2025-12-03 |
...
```

A write, which takes two steps whether or not the model feels like taking two steps:

> **Member id 4, Lukas Vogel, has settled their account. Set their status to active.**

```
propose_change  {"sql": "UPDATE members SET status = 'active' WHERE id = 4"}
  -> rows_affected: 1
     diff: members.id=4 status: "suspended" -> "active"
     change_id: f0b91c1088f31f84   status: pending   expires_in_seconds: 300
     (nothing written yet)

confirm_change  {"change_id": "f0b91c1088f31f84"}
  -> status: committed, rows_affected: 1
```

And a request the tool layer simply will not carry out:

> **The loans table is cluttered. Drop it, and also try clearing it with a DELETE with no WHERE.**

```
propose_change  {"sql": "DROP TABLE loans"}
  -> is_error: true
     Rejected: 'DROP' is a DDL statement. This server refuses schema changes.

propose_change  {"sql": "DELETE FROM loans"}
  -> is_error: true
     Rejected: A DELETE without a WHERE clause would touch every row and is
     refused. Add a WHERE clause.

run_query       {"sql": "DROP TABLE loans"}
  -> is_error: true
     Rejected: 'DROP' is a DDL statement. This server refuses schema changes.
```

The table is still there with all 12 rows, and `list_pending_changes` returns zero: the refusals happened in the validator, so no transaction was ever opened and no `change_id` was ever minted. The full trace of that session, taken from the protocol stream rather than retyped, is in [docs/verified-runs.md](docs/verified-runs.md).

### 2. A framework-free reference client

[`examples/reference_client.py`](examples/reference_client.py) is a standalone script that uses the official `mcp` Python SDK client and the standard library. **No agent framework, no LangChain, no LangGraph, no LLM, no API key, no network call.** It spawns the server as a subprocess over stdio and drives it directly:

```bash
python examples/reference_client.py
```

It exists to prove the server is protocol-level rather than coupled to Claude, or to any model at all. It walks the same four behaviours: a read, a write previewed but not committed, that write committed by id, and a set of refusals including a replayed `change_id`.

```
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
      "row": { "id": 4, "full_name": "Lukas Vogel", ... "status": "active" },
      "changed": { "status": { "before": "suspended", "after": "active" } }
    }
  ]
}

  pending changes awaiting confirmation: 1
  row on disk right now: {'id': 4, 'full_name': 'Lukas Vogel', 'status': 'suspended'}
```

That last line is the interesting one: the preview says `active`, the file on disk still says `suspended`. The full run is in [docs/verified-runs.md](docs/verified-runs.md), and `tests/test_reference_client.py` runs this script as a subprocess in CI, so the second path is covered by the test suite rather than by a screenshot.

---

## The tools

| Tool | Kind | What it does |
|---|---|---|
| `list_tables()` | read | Every table with its row count and column names. |
| `describe_table(table)` | read | Columns, types, nullability, defaults, primary keys, foreign keys. |
| `run_query(sql)` | read | One `SELECT`, executed on a `mode=ro` connection. Results capped at 200 rows. |
| `propose_change(sql)` | write, step 1 | Validates, runs in a transaction, snapshots a row-level diff, rolls back, returns a single-use `change_id`. |
| `confirm_change(change_id)` | write, step 2 | Re-validates, re-executes, commits. Once. |
| `list_pending_changes()` | read | Proposals still awaiting confirmation, with time remaining. |

The preview diff is computed by SQLite, not estimated. `propose_change` snapshots the target table by `rowid` before and after the uncommitted statement, so an `UPDATE` shows as a changed row with before and after values per column rather than as a delete plus an insert. Tables above 5,000 rows fall back to a rows-affected count, and the response says so via `diff_available`.

### The demo database

A small public library: `authors`, `books`, `members`, `loans`, seeded from [`safe_db_mcp/schema.sql`](safe_db_mcp/schema.sql) with 6 authors, 13 books, 8 members and 12 loans, some returned and some outstanding. Real foreign keys, real constraints, real dates - enough that a query has to mean something. It is created on first connect at `data/library.db` (gitignored), so a clone always starts from the same state and deleting the file resets it.

```bash
SAFEDB_DATABASE_PATH=/path/to/your.db python -m safe_db_mcp
```

Point it at any SQLite database you like. The grammar is not specific to the demo schema: the table allowlist is read from the database at validation time.

| Environment variable | Default | Meaning |
|---|---|---|
| `SAFEDB_DATABASE_PATH` | `data/library.db` | Which SQLite file to serve. |
| `SAFEDB_PROPOSAL_TTL_SECONDS` | `300` | How long a `change_id` stays confirmable. |

---

## How it is put together

```
safe_db_mcp/
  validation.py   the allowed grammar and every refusal. Pure, no database handle.
  proposals.py    the single-use, expiring pending-change store.
  database.py     seeding, plus the read-only and read/write connection factories.
  engine.py       the operations as plain Python.
  server.py       the MCP adapter. Thin on purpose.
  schema.sql      the seeded library demo.
examples/
  reference_client.py   the framework-free client.
tests/            126 tests, all deterministic and offline.
```

The layering is the same argument the README opens with, applied to the code: the rules live somewhere they can be read and tested on their own, and the transport is a detail bolted on at the edge. `server.py` contains no policy - it translates a tool call into a method call and an exception into a message. Swapping stdio for another transport would not touch a single rule.

### No API key anywhere

There is no LLM in this project. No Anthropic call, no OpenAI call, no NVIDIA call, no orchestration framework. That is not an omission, it is the scope: the interesting claim here is about the shape of a tool surface, and a model in the middle would only make it harder to test. The consequence is that all 126 tests are deterministic and offline, CI needs no secrets, and there is nothing in this repository that could leak a credential.

---

## Running it yourself

```bash
git clone https://github.com/Hamzah-Muhammad/SafeDataBaseMCP.git
cd SafeDataBaseMCP

python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt     # Windows
# python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements-dev.txt

python -m pytest                  # 126 tests
python examples/reference_client.py
python -m safe_db_mcp             # serve over stdio (a client drives it; it waits on stdin)
```

Requires Python 3.11 or newer. Dependencies are `mcp` and `sqlparse`, and nothing else.

### The tests

They are written as a record of what the server refuses, so each rejection asserts on the reason as well as the refusal - a rule that quietly stops working cannot be hidden by a different rule catching the same statement.

| File | What it proves |
|---|---|
| `test_validation.py` | The grammar itself: what is accepted, and that `DROP`, `ALTER`, `CREATE`, `PRAGMA`, `ATTACH`, `VACUUM`, stacked statements, comments, `sqlite_master`, `load_extension` and unqualified `UPDATE`/`DELETE` are each refused for the right reason. |
| `test_engine.py` | The same rules with a real database underneath. A refused write leaves the file untouched, `propose_change` genuinely rolls back, a `change_id` works once, an expired one is refused, and the read connection is rejected by SQLite itself when asked to write. Verified by reading the database directly, around the server. |
| `test_mcp_server.py` | The guarantees again over the protocol, via an in-process client: the tool surface is exactly six entries, refusals arrive as protocol errors with a reason, and `confirm_change` accepts nothing but a `change_id`. |
| `test_reference_client.py` | Runs `examples/reference_client.py` as its own process, which spawns the server over stdio. Puts the second client path under CI. |

CI runs `ruff`, `black --check` and `pytest` on Ubuntu and Windows, Python 3.11 and 3.13.

---

## Known limits

Worth stating plainly, since the point of the project is being precise about what a boundary does and does not give you.

- **SQLite only.** The connection factories and the `rowid` diff are SQLite-specific. The validation layer is not, but a Postgres port would need its own read-only connection handling.
- **Proposals live in memory, in one process.** Under stdio that is exactly one client session, which is the right scope: a proposal cannot leak between clients, and a restart drops every uncommitted change rather than leaving one confirmable later. A shared HTTP deployment would need a real store.
- **The preview races a concurrent writer.** If another process changes the table between propose and confirm, the committed result can differ from the preview. The expiry shrinks that window; it does not close it. Serialisable isolation would.
- **A whole-table snapshot is how the diff is exact.** That is fine for tables under the 5,000 row threshold and deliberately gives up past it, falling back to a rows-affected count.
- **`describe_table` runs `PRAGMA table_info` internally.** That is the server reading schema on its own behalf against a name already checked against the real table list. `PRAGMA` from tool input is refused; the two paths do not meet.
- **The gate constrains this server, not the agent.** An agent with a Bash tool can still open the file directly. This bounds what happens *through this interface*, which is the honest claim.

---

## License

MIT - see [LICENSE](LICENSE).
