"""One-time cancellation of the approvals an earlier release left pending."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

from . import journal

if TYPE_CHECKING:
    from .backend import Transaction

# LEGACY_COMPAT: Approval continuations an earlier release left pending.
# Legacy format: approval_continuations without a span_id column, keeping their entity in an entity_name column and
# their pending sources in approval_continuation_sources; v2026.9.138 through v2026.10.215 also kept their reply's
# identity in response_attempts and response_attempt_sources.
# Last legacy release: v2026.10.215; replacement: the unreleased durable reply messages name the paused span in
# approval_continuations.span_id and read the reply's identity and held sources from the reply's records.
# Handling: an upgrade cannot guarantee that no approval is pending, so the schema upgrade cancels every such approval:
# it deletes the continuation with its calls and card records, and settles its pending sources unanswered. A click on
# one of its cards then does nothing, and its reply keeps what it showed. The upgrade then adds span_id and
# claim_span_id and drops approval_continuation_sources, the entity_name column, and the response attempt tables.
# Coverage: tests/test_legacy_continuation_identity.py.

logger = get_logger(__name__)

_PAGE_SIZE = 128


def upgrade_continuation_identity(
    transaction: Transaction,
    existing_tables: frozenset[str],
    continuation_columns: frozenset[str],
) -> None:
    """Cancel the approvals an earlier release left pending and name paused spans, inside the schema transaction."""
    # No columns: no approval_continuations table yet, or one a pre-Nio-1 upgrade just retired.
    if not continuation_columns or "span_id" in continuation_columns:
        return
    transaction.execute("ALTER TABLE approval_continuations ADD COLUMN span_id TEXT")
    transaction.execute("ALTER TABLE approval_continuations ADD COLUMN claim_span_id TEXT")
    # Releases before approval cards kept none to drop.
    cards = "approval_cards" in existing_tables
    # Each pass deletes the page it read, so the next one reads the rows after it.
    while rows := transaction.fetchall(
        "SELECT principal_id, approval_id FROM approval_continuations ORDER BY principal_id, approval_id LIMIT ?",
        (_PAGE_SIZE,),
    ):
        for row in rows:
            _cancel(transaction, str(row["principal_id"]), str(row["approval_id"]), cards=cards)
    transaction.execute("DROP TABLE IF EXISTS approval_continuation_sources")
    # The paused span's reply names the entity; the scan index on the copy goes with it.
    transaction.execute("DROP INDEX IF EXISTS approval_continuations_owner_scan")
    transaction.execute("ALTER TABLE approval_continuations DROP COLUMN entity_name")
    transaction.execute("DROP TABLE IF EXISTS response_attempt_sources")
    transaction.execute("DROP TABLE IF EXISTS response_attempts")


def _cancel(transaction: Transaction, principal_id: str, approval_id: str, *, cards: bool) -> None:
    """Drop one earlier-release approval with its cards; its calls cascade and its pending sources settle unanswered."""
    pending = tuple(
        str(source["event_id"])
        for source in transaction.fetchall(
            "SELECT event_id FROM approval_continuation_sources WHERE principal_id = ? AND approval_id = ?",
            (principal_id, approval_id),
        )
    )
    if cards:
        # The router's principal owns the cards, not the continuation's; the approval id names them alone.
        transaction.execute("DELETE FROM approval_cards WHERE continuation_id = ?", (approval_id,))
    transaction.execute(
        "DELETE FROM approval_continuations WHERE principal_id = ? AND approval_id = ?",
        (principal_id, approval_id),
    )
    journal.settle_many(transaction, principal_id, pending)
    logger.warning("legacy_approval_cancelled", principal_id=principal_id, approval_id=approval_id)
