"""One-time conversion of the free-text failure reasons failing paused replies stored."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .backend import Transaction

# LEGACY_COMPAT: Free-text failure reasons of failing approval continuations.
# Legacy format: approval_continuations.failure_reason TEXT, the reason a failing continuation failed: the cancellation
# codes cancelled_by_user, sync_restart_cancelled, and interrupted, the fixed text a resume a shutdown cut short
# stored, failed for a reply rule's fence, or an error's own text.
# Last legacy release: v2026.10.633; replacement: the next release stores failure_kind (error, user_stop, restart,
# interrupted, or shutdown) and failure_detail, the text of an error.
# Handling: the schema upgrade adds failure_kind and failure_detail, converts each failing continuation's reason
# (cancelled_by_user to user_stop, sync_restart_cancelled to restart, interrupted to interrupted, the shutdown text to
# shutdown, any other text to error with the text as its detail), and drops failure_reason, so a failing continuation
# settles with the note and card reason its failure gave it before.
# Coverage: tests/test_legacy_approval_failures.py::test_each_stored_failure_reason_becomes_its_kind,
# tests/test_legacy_approval_failures.py::test_the_conversion_runs_once.

_KINDS = {
    "cancelled_by_user": "user_stop",
    "sync_restart_cancelled": "restart",
    "interrupted": "interrupted",
    "Tool approval continuation was interrupted before final delivery and denied safely.": "shutdown",
}


def upgrade_failure_kind(transaction: Transaction, continuation_columns: frozenset[str]) -> None:
    """Replace each continuation's stored failure reason with its kind and detail, inside the schema transaction."""
    if "failure_reason" not in continuation_columns:
        return
    transaction.execute("ALTER TABLE approval_continuations ADD COLUMN failure_kind TEXT")
    transaction.execute("ALTER TABLE approval_continuations ADD COLUMN failure_detail TEXT")
    for reason, kind in _KINDS.items():
        transaction.execute(
            "UPDATE approval_continuations SET failure_kind = ? WHERE state = 'failing' AND failure_reason = ?",
            (kind, reason),
        )
    transaction.execute(
        """
        UPDATE approval_continuations SET failure_kind = 'error', failure_detail = failure_reason
        WHERE state = 'failing' AND failure_kind IS NULL
        """,
    )
    transaction.execute("ALTER TABLE approval_continuations DROP COLUMN failure_reason")
