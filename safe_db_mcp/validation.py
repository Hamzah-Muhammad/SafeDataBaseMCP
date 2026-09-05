"""SQL validation: the allowed grammar, and everything the server refuses.

This module is the whole security surface of SafeDataBaseMCP. It is pure and
synchronous on purpose: it takes a string, and either returns a description of
a statement the server is willing to run, or raises :class:`SqlRejected` with a
reason a human can read. No database handle is involved, so every rule in here
is unit-testable offline with no fixtures.

Two grammars are enforced, not one:

* the *read* grammar - exactly one ``SELECT`` (a leading ``WITH`` is allowed
  only when the statement still resolves to a ``SELECT``);
* the *write* grammar - exactly one ``INSERT``, ``UPDATE`` or ``DELETE``
  against a known table, and ``UPDATE``/``DELETE`` must carry a ``WHERE``.

Everything else is refused: DDL, ``PRAGMA``, ``ATTACH``, transaction control,
stacked statements, comments, and any reference to SQLite internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import sqlparse
from sqlparse import tokens as T
from sqlparse.sql import Function, Identifier, IdentifierList, Statement, Where

#: Hard ceiling on the size of a statement the server will even parse.
MAX_SQL_LENGTH = 4000

#: Statement types the read path accepts.
READ_TYPES = frozenset({"SELECT"})

#: Statement types the write path accepts.
WRITE_TYPES = frozenset({"INSERT", "UPDATE", "DELETE"})

#: Keywords that are refused anywhere in any statement, on either path. These
#: cover schema mutation, file/extension loading, transaction control and the
#: SQLite-specific escape hatches that turn a query engine into a shell.
FORBIDDEN_KEYWORDS = frozenset(
    {
        "ALTER",
        "ANALYZE",
        "ATTACH",
        "BEGIN",
        "COMMIT",
        "CREATE",
        "DETACH",
        "DROP",
        "GRANT",
        "INDEXED",
        "PRAGMA",
        "REINDEX",
        "RELEASE",
        "RENAME",
        "REPLACE",
        "REVOKE",
        "ROLLBACK",
        "SAVEPOINT",
        "TRIGGER",
        "TRUNCATE",
        "VACUUM",
    }
)

#: Function/identifier names refused anywhere. ``load_extension`` and the
#: fileio helpers are the usual routes from "can run SQL" to "can run code".
FORBIDDEN_NAMES = frozenset(
    {
        "load_extension",
        "readfile",
        "writefile",
        "edit",
        "fts3_tokenizer",
        "sqlite_dbpage",
        "sqlite_compileoption_used",
    }
)

#: Identifier prefixes that belong to SQLite itself and are never addressable.
RESERVED_TABLE_PREFIX = "sqlite_"

#: DML keywords that must not appear on the read path, even nested in a CTE.
DML_KEYWORDS = frozenset({"INSERT", "UPDATE", "DELETE", "UPSERT", "MERGE"})


class SqlRejected(ValueError):
    """Raised when a statement falls outside the allowed grammar.

    The message is written to be shown verbatim to whoever sent the SQL, so it
    always says which rule was broken rather than just "invalid".
    """


Intent = Literal["read", "write"]


@dataclass(frozen=True)
class ValidatedStatement:
    """A statement the server has agreed to run.

    Attributes:
        sql: The statement, stripped of surrounding whitespace and any trailing
            semicolon. This is the exact text that will be executed.
        operation: ``SELECT``, ``INSERT``, ``UPDATE`` or ``DELETE``.
        table: The table the statement targets. Always populated for writes;
            ``None`` for reads, which may touch several tables.
    """

    sql: str
    operation: str
    table: str | None


def _reject(reason: str) -> None:
    raise SqlRejected(reason)


def _check_shape(sql: str) -> Statement:
    """Parse ``sql`` and enforce the rules that apply to any statement.

    Returns the single parsed statement. Raises :class:`SqlRejected` if the
    input is empty, oversized, commented, stacked, or unparseable.
    """
    if not isinstance(sql, str):
        _reject("SQL must be a string.")

    stripped = sql.strip()
    if not stripped:
        _reject("Empty SQL. Send a single statement.")

    if len(stripped) > MAX_SQL_LENGTH:
        _reject(f"SQL is longer than the {MAX_SQL_LENGTH} character limit.")

    if "\x00" in stripped:
        _reject("SQL contains a null byte.")

    # Comments are refused outright. Nothing the allowed grammar can express
    # needs one, and they are the standard way to smuggle a second intent past
    # a naive check.
    if "--" in stripped or "/*" in stripped:
        _reject("SQL comments are not allowed. Send the statement without comments.")

    statements = [s for s in sqlparse.split(stripped) if s.strip()]
    if len(statements) > 1:
        _reject(
            f"Only one statement per call is allowed; {len(statements)} were sent. "
            "Statement stacking is refused."
        )
    if not statements:
        _reject("No SQL statement found.")

    parsed = [p for p in sqlparse.parse(statements[0]) if str(p).strip()]
    if len(parsed) != 1:
        _reject("Could not parse this as exactly one SQL statement.")

    return parsed[0]


def _check_forbidden(statement: Statement) -> None:
    """Refuse any statement containing a forbidden keyword, name or table."""
    for token in statement.flatten():
        value = token.value
        upper = value.upper()

        if token.ttype in T.Keyword.DDL:
            _reject(f"'{upper}' is a DDL statement. This server refuses schema changes.")

        if token.ttype in T.Keyword and upper in FORBIDDEN_KEYWORDS:
            _reject(f"'{upper}' is not allowed. This server refuses schema and session changes.")

        if token.ttype in T.Name:
            lowered = value.strip('"[]`').lower()
            if lowered in FORBIDDEN_NAMES:
                _reject(f"'{lowered}' is not callable through this server.")
            if lowered.startswith(RESERVED_TABLE_PREFIX):
                _reject(f"'{lowered}' is SQLite internal state and is not addressable.")
            # sqlparse groups some statements (PRAGMA above all) as a bare
            # identifier rather than a keyword, so re-check names against the
            # keyword denylist instead of trusting the token type.
            if upper in FORBIDDEN_KEYWORDS:
                _reject(
                    f"'{upper}' is not allowed. This server refuses schema and session changes."
                )


def _statement_type(statement: Statement) -> str:
    """Return the statement type, resolving a leading CTE to its inner verb."""
    kind = statement.get_type()
    if kind != "UNKNOWN":
        return kind

    # sqlparse reports UNKNOWN for `WITH ... SELECT`. Walk to the first
    # meaningful keyword after the CTE definition and use that instead.
    tokens = [t for t in statement.flatten() if not t.is_whitespace]
    if tokens and tokens[0].value.upper() == "WITH":
        for token in tokens:
            upper = token.value.upper()
            if upper in READ_TYPES or upper in WRITE_TYPES:
                return upper
    return "UNKNOWN"


def _target_table(statement: Statement, operation: str) -> str:
    """Extract the single table a write statement targets.

    Raises :class:`SqlRejected` if the target cannot be identified unambiguously
    or if the statement names more than one table.
    """
    anchors = {"INSERT": {"INTO"}, "UPDATE": {"UPDATE"}, "DELETE": {"FROM"}}[operation]

    tokens = [t for t in statement.tokens if not t.is_whitespace]
    for index, token in enumerate(tokens):
        if token.ttype is None or token.value.upper() not in anchors:
            continue

        for candidate in tokens[index + 1 :]:
            if isinstance(candidate, IdentifierList):
                _reject("A write may only target one table.")
            if isinstance(candidate, (Identifier, Function)):
                name = candidate.get_real_name()
                if name:
                    return name
                break
            if candidate.ttype in T.Name:
                return candidate.value.strip('"[]`')
            if candidate.ttype in T.Keyword:
                continue
            break

    _reject(f"Could not identify the table this {operation} targets.")
    raise AssertionError("unreachable")  # pragma: no cover


def _has_where(statement: Statement) -> bool:
    return any(isinstance(token, Where) for token in statement.tokens)


def validate_read(sql: str) -> ValidatedStatement:
    """Validate ``sql`` as a single read-only ``SELECT``.

    Args:
        sql: The candidate statement.

    Returns:
        The :class:`ValidatedStatement` the caller may execute.

    Raises:
        SqlRejected: If the statement is not a single, comment-free ``SELECT``
            that touches no forbidden keyword, function or internal table.
    """
    statement = _check_shape(sql)
    _check_forbidden(statement)

    kind = _statement_type(statement)
    if kind in WRITE_TYPES or kind in DML_KEYWORDS:
        article = "an" if kind in {"INSERT", "UPSERT", "UPDATE"} else "a"
        _reject(
            f"run_query is read-only and refused {article} {kind}. "
            "Route writes through propose_change and confirm_change."
        )
    if kind not in READ_TYPES:
        _reject(f"run_query accepts a single SELECT; got '{kind}'.")

    # A CTE body could still hide DML, so scan the flattened token stream
    # rather than trusting the statement type alone.
    for token in statement.flatten():
        if token.ttype in T.DML and token.value.upper() in DML_KEYWORDS:
            _reject(f"run_query is read-only and refused an embedded {token.value.upper()}.")

    text = str(statement).strip().rstrip(";").strip()
    return ValidatedStatement(sql=text, operation="SELECT", table=None)


def validate_write(sql: str, known_tables: frozenset[str] | set[str]) -> ValidatedStatement:
    """Validate ``sql`` as a single row-level write against a known table.

    Args:
        sql: The candidate statement.
        known_tables: Table names that currently exist in the database. The
            target is matched case-insensitively against this set, so a typo
            is refused before anything is executed.

    Returns:
        The :class:`ValidatedStatement` the caller may propose.

    Raises:
        SqlRejected: If the statement is not a single ``INSERT``/``UPDATE``/
            ``DELETE``, targets an unknown table, or is an ``UPDATE``/``DELETE``
            with no ``WHERE`` clause.
    """
    statement = _check_shape(sql)
    _check_forbidden(statement)

    kind = _statement_type(statement)
    if kind in READ_TYPES:
        _reject("propose_change is for writes. Use run_query for a SELECT.")
    if kind not in WRITE_TYPES:
        _reject(f"propose_change accepts a single INSERT, UPDATE or DELETE; got '{kind}'.")

    if kind in {"UPDATE", "DELETE"} and not _has_where(statement):
        article = "An" if kind == "UPDATE" else "A"
        _reject(
            f"{article} {kind} without a WHERE clause would touch every row and is refused. "
            "Add a WHERE clause."
        )

    table = _target_table(statement, kind)
    lookup = {name.lower(): name for name in known_tables}
    if table.lower() not in lookup:
        known = ", ".join(sorted(known_tables))
        _reject(f"Unknown table '{table}'. Tables in this database: {known}.")

    text = str(statement).strip().rstrip(";").strip()
    return ValidatedStatement(sql=text, operation=kind, table=lookup[table.lower()])
