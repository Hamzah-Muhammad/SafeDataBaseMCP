"""The pending-change store: what makes propose/confirm a gate, not a habit.

A proposal is a validated write that has been executed inside a transaction and
rolled back, together with the preview that execution produced. It is held here
until it is confirmed, expires, or the server exits.

Three properties are enforced in code, so no prompt or convention is load
bearing:

* **single use** - confirming a proposal marks it used; a second confirm of the
  same id is refused;
* **expiry** - a proposal that has sat unconfirmed past its time-to-live is
  refused, so a stale preview can never be committed against changed data;
* **no ad-hoc ids** - a ``change_id`` only exists because ``propose_change``
  minted it, so ``confirm_change`` cannot be reached without a preview first.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: Environment variable overriding how long a proposal stays confirmable.
TTL_ENV = "SAFEDB_PROPOSAL_TTL_SECONDS"

#: Default time-to-live for a proposal, in seconds.
DEFAULT_TTL_SECONDS = 300.0


class ProposalError(RuntimeError):
    """Raised when a ``change_id`` cannot be confirmed, with the reason why."""


def default_ttl_seconds() -> float:
    """Return the configured proposal time-to-live in seconds."""
    raw = os.environ.get(TTL_ENV)
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return value if value > 0 else DEFAULT_TTL_SECONDS


@dataclass
class Proposal:
    """One pending write, and the preview computed for it.

    Attributes:
        change_id: The single-use token ``confirm_change`` expects.
        sql: The exact statement that will be re-validated and executed.
        operation: ``INSERT``, ``UPDATE`` or ``DELETE``.
        table: The table the statement targets.
        preview: The rolled-back result of running it: rows affected and diff.
        created_at: Monotonic timestamp when the proposal was minted.
        expires_at: Monotonic timestamp after which it is refused.
        used: Set once the proposal has been confirmed. Never unset.
    """

    change_id: str
    sql: str
    operation: str
    table: str
    preview: dict[str, Any]
    created_at: float
    expires_at: float
    used: bool = False
    used_at: float | None = field(default=None)

    def seconds_remaining(self, now: float | None = None) -> float:
        """Return how long this proposal stays confirmable, floored at zero."""
        current = time.monotonic() if now is None else now
        return max(0.0, self.expires_at - current)

    def is_expired(self, now: float | None = None) -> bool:
        """Return whether the proposal's time-to-live has run out."""
        current = time.monotonic() if now is None else now
        return current >= self.expires_at


class ProposalStore:
    """An in-process registry of pending changes.

    The store deliberately lives in memory for the lifetime of one server
    process. Under stdio transport that is exactly one client's session, so a
    proposal cannot leak across clients, and a restart drops every uncommitted
    change rather than leaving one confirmable later.
    """

    def __init__(self, ttl_seconds: float | None = None) -> None:
        """Create an empty store.

        Args:
            ttl_seconds: Override the proposal time-to-live. Defaults to
                :func:`default_ttl_seconds`.
        """
        self._ttl = default_ttl_seconds() if ttl_seconds is None else float(ttl_seconds)
        self._proposals: dict[str, Proposal] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> float:
        """The time-to-live applied to newly created proposals."""
        return self._ttl

    def add(self, sql: str, operation: str, table: str, preview: dict[str, Any]) -> Proposal:
        """Mint a new proposal and return it.

        Args:
            sql: The validated statement.
            operation: ``INSERT``, ``UPDATE`` or ``DELETE``.
            table: The target table.
            preview: The rolled-back preview to show the caller.

        Returns:
            The stored :class:`Proposal`, including its fresh ``change_id``.
        """
        now = time.monotonic()
        proposal = Proposal(
            change_id=secrets.token_hex(8),
            sql=sql,
            operation=operation,
            table=table,
            preview=preview,
            created_at=now,
            expires_at=now + self._ttl,
        )
        with self._lock:
            self._proposals[proposal.change_id] = proposal
        return proposal

    def get(self, change_id: str) -> Proposal | None:
        """Return the proposal with this id, used or expired ones included."""
        with self._lock:
            return self._proposals.get(change_id)

    def claim(self, change_id: str) -> Proposal:
        """Take exclusive ownership of a proposal so it can be committed.

        Marking the proposal used happens here, under the lock and before any
        SQL runs, so two concurrent confirms of the same id cannot both proceed.

        Args:
            change_id: The token returned by ``propose_change``.

        Returns:
            The claimed :class:`Proposal`.

        Raises:
            ProposalError: If the id is unknown, already used, or expired.
        """
        if not isinstance(change_id, str) or not change_id.strip():
            raise ProposalError("A change_id is required. Call propose_change first.")

        with self._lock:
            proposal = self._proposals.get(change_id.strip())
            if proposal is None:
                raise ProposalError(
                    f"Unknown change_id '{change_id}'. "
                    "Only an id returned by propose_change can be confirmed."
                )
            if proposal.used:
                raise ProposalError(
                    f"change_id '{change_id}' has already been confirmed. "
                    "Each proposal is single use; propose the change again."
                )
            if proposal.is_expired():
                raise ProposalError(
                    f"change_id '{change_id}' expired after {self._ttl:.0f}s. "
                    "Propose the change again to get a fresh preview."
                )
            proposal.used = True
            proposal.used_at = time.monotonic()
            return proposal

    def release(self, change_id: str) -> None:
        """Undo a :meth:`claim` when the commit did not happen.

        Called only when re-validation or execution fails after claiming, so a
        proposal is not burned by a failure that changed nothing.
        """
        with self._lock:
            proposal = self._proposals.get(change_id)
            if proposal is not None:
                proposal.used = False
                proposal.used_at = None

    def pending(self) -> list[Proposal]:
        """Return the proposals that are still unused and unexpired."""
        now = time.monotonic()
        with self._lock:
            return [p for p in self._proposals.values() if not p.used and not p.is_expired(now)]

    def purge_expired(self) -> int:
        """Drop expired, unused proposals and return how many were removed."""
        now = time.monotonic()
        with self._lock:
            stale = [
                key
                for key, proposal in self._proposals.items()
                if not proposal.used and proposal.is_expired(now)
            ]
            for key in stale:
                del self._proposals[key]
        return len(stale)
