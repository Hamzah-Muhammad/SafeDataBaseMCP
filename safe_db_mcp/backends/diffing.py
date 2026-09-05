"""Row-level diffing, and deciding whether an approved preview still holds.

Both backends snapshot the target table on either side of an uncommitted write
and compare by identity, so an ``UPDATE`` reads as a changed row with before and
after values per column rather than as a delete plus an insert.

:func:`comparable` is the other half. It reduces a preview to just the parts
that must not move between propose and confirm, so ``commit_write`` can refuse
if the world changed underneath an approved preview. It deliberately ignores
identity columns on inserted rows, because sequence allocation is not
transactional in Postgres: a rolled-back preview burns a sequence value, so the
committed row legitimately gets a different id than the preview showed. Treating
that as a conflict would make every insert fail.
"""

from __future__ import annotations

import json
from typing import Any

#: A snapshot maps an opaque identity (rowid, or a primary key tuple) to a row.
Snapshot = dict[Any, dict[str, Any]]


def build_diff(before: Snapshot, after: Snapshot) -> dict[str, list[dict[str, Any]]]:
    """Compare two snapshots into added, removed and updated rows.

    Args:
        before: Rows keyed by identity, before the statement ran.
        after: The same, after it ran but before the rollback.

    Returns:
        ``{"added": [...], "removed": [...], "updated": [...]}`` where each
        updated entry carries the new row plus a per-column before/after map.
    """
    added = [row for key, row in after.items() if key not in before]
    removed = [row for key, row in before.items() if key not in after]
    updated = []
    for key, old in before.items():
        new = after.get(key)
        if new is None or new == old:
            continue
        changed = {
            column: {"before": old[column], "after": new[column]}
            for column in new
            if new[column] != old.get(column)
        }
        updated.append({"row": new, "changed": changed})
    return {"added": added, "removed": removed, "updated": updated}


def _stable(value: Any) -> str:
    """Render a value as a stable string so rows can be sorted and compared."""
    return json.dumps(value, sort_keys=True, default=str)


def comparable(preview: dict[str, Any]) -> str:
    """Reduce a preview to the parts that must not change before it is committed.

    Two previews compare equal when committing the second would have the same
    effect the caller approved in the first.

    What is compared: the row count, which rows would be removed, which rows
    would be updated and how, and the non-identity values of any rows that would
    be added.

    What is ignored: identity columns on added rows, for the sequence reason
    described in this module's docstring, and ordering, since neither backend
    guarantees a snapshot order.

    Args:
        preview: A preview as returned by ``Backend.preview_write``.

    Returns:
        A canonical string. Compare two of these for equality; do not parse one.
    """
    diff = preview.get("diff")
    if not preview.get("diff_available") or diff is None:
        # Without a diff there is nothing to compare but the row count. That is
        # a weaker check, and `diff_available: false` in the payload says so.
        return _stable({"rows_affected": preview.get("rows_affected")})

    keys = set(preview.get("key_columns") or ())

    added = sorted(
        _stable({column: value for column, value in row.items() if column not in keys})
        for row in diff.get("added", [])
    )
    removed = sorted(_stable(row) for row in diff.get("removed", []))
    updated = sorted(_stable(entry) for entry in diff.get("updated", []))

    return _stable(
        {
            "rows_affected": preview.get("rows_affected"),
            "added": added,
            "removed": removed,
            "updated": updated,
        }
    )
