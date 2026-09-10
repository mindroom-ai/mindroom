"""One-time retirement of application work admitted before Nio owned ingestion."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from .backend import Transaction

logger = get_logger(__name__)

# Recreate execution state instead of translating obsolete ownership or payloads.
# Children precede parents so this also works with foreign keys enabled.
_RETIRED_TABLES = (
    "interactive_selections",
    "interactive_questions",
    "interactive_questions_pre_selection",
    "approval_continuation_sources",
    "approval_continuation_calls",
    "approval_continuations",
    "approval_cards",
    "approval_cards_legacy_delivery",
    "background_approval_calls",
    "matrix_delivery_outbox",
    "response_outbox",
    "response_outbox_legacy_delivery",
    "reported_departures",
    "room_membership",
    "conversation_hydration",
    "room_history_recovery",
)


def upgrade_legacy_journal(transaction: Transaction, existing_tables: frozenset[str]) -> None:
    """Retire old work inside the transaction that installs the current schema.

    Keep event identities, projected history, completed-turn tracking, and the
    journal generation already bound to this installation. Creating the new
    consumer table in this same transaction makes the upgrade crash-atomic and
    prevents another open from retiring work admitted after the cutover.
    """
    if "journal_events" not in existing_tables or "matrix_sync_consumers" in existing_tables:
        return
    logger.warning(
        "event_journal_legacy_work_retiring",
        consequence="Unfinished pre-Nio-1 requests, deliveries, and approvals will not resume; history is preserved",
    )
    for table in _RETIRED_TABLES:
        if table in existing_tables:
            transaction.execute(f"DROP TABLE {table}")
    transaction.execute(
        "UPDATE journal_events SET state = 'settled', source_json = '', semantic_consumer = NULL "
        "WHERE state = 'pending'",
    )
    # Late keys may add historical context, but must never revive an old turn.
    transaction.execute("UPDATE journal_events SET kind = 'opaque_history' WHERE kind = 'decryption_failure'")
    # Nio begins its own membership epochs at zero. Old content remains history,
    # while empty membership/hydration tables require a fresh source baseline.
    transaction.execute("UPDATE journal_events SET membership_epoch = 0 WHERE membership_epoch != 0")
    transaction.execute("UPDATE visible_messages SET membership_epoch = 0 WHERE membership_epoch != 0")
