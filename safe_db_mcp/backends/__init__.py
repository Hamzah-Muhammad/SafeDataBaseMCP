"""Backends, and the one place a backend is chosen.

``SAFEDB_BACKEND`` selects between them and defaults to ``sqlite``, so a fresh
clone keeps working with no database server, no credentials and no network.
"""

from __future__ import annotations

import os

from .base import Backend, BackendError, PreviewChanged

#: Environment variable selecting the backend.
BACKEND_ENV = "SAFEDB_BACKEND"

#: The backends this server can serve.
VALID_BACKENDS = ("sqlite", "postgres")


def build_backend(name: str | None = None, **kwargs) -> Backend:
    """Construct the configured backend.

    Args:
        name: ``sqlite`` or ``postgres``. Defaults to ``SAFEDB_BACKEND``, and to
            ``sqlite`` when that is unset.
        **kwargs: Passed through to the backend. ``path`` for SQLite,
            ``settings`` for Postgres.

    Raises:
        ValueError: If ``name`` is not a backend this server knows.
    """
    chosen = (name or os.environ.get(BACKEND_ENV) or "sqlite").strip().lower()

    if chosen == "sqlite":
        from .sqlite_backend import SqliteBackend

        return SqliteBackend(**kwargs)

    if chosen == "postgres":
        from .postgres_backend import PostgresBackend

        return PostgresBackend(**kwargs)

    raise ValueError(
        f"Unknown backend '{chosen}'. Set {BACKEND_ENV} to one of: {', '.join(VALID_BACKENDS)}."
    )


__all__ = [
    "BACKEND_ENV",
    "VALID_BACKENDS",
    "Backend",
    "BackendError",
    "PreviewChanged",
    "build_backend",
]
