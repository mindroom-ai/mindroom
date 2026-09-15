"""Migrations for legacy journal layouts and approval records."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from .backend import Transaction

logger = get_logger(__name__)

# LEGACY_COMPAT: Application-owned journal ingestion before durable Matrix consumers.
# Legacy format: Application-owned journal ingestion without matrix_sync_consumers.
# Last legacy release: v2026.9.28; replacement: v2026.9.29 transferred ingestion ownership.
# Handling: Retire unfinished transport work while preserving history, terminal turns, and journal generation.
# Coverage: tests/test_journal_upgrade_boundary.py::test_released_journal_upgrades_and_preserves_new_work.

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


# LEGACY_COMPAT: Approval calls without persisted toolkit origins.
# Legacy format: Approval calls written before per-call toolkit origin persistence.
# Last legacy release: v2026.9.139; replacement: v2026.9.140 added per-call toolkit_name storage.
# Handling: Fence unresumable current generations for normal failure recovery, including already-upgraded rows.
# Preserve historical calls, existing failures, and recoverable FINAL delivery debt.
# Coverage: tests/test_journal_upgrade_boundary.py::test_approval_toolkit_upgrade_fences_unresumable_calls,
# tests/test_journal_upgrade_boundary.py::test_approval_toolkit_upgrade_preserves_compatible_work,
# tests/test_journal_upgrade_boundary.py::test_approval_toolkit_upgrade_preserves_frozen_final.
def upgrade_approval_toolkit_origins(transaction: Transaction, columns: frozenset[str]) -> None:
    """Add historical origins and fence unresumable work in the schema transaction."""
    if "toolkit_name" not in columns:
        transaction.execute("ALTER TABLE approval_continuation_calls ADD COLUMN toolkit_name TEXT")
    transaction.execute(
        """
        UPDATE approval_continuations
        SET state = 'failing', failure_reason = COALESCE(failure_reason, ?)
        WHERE state IN ('waiting', 'ready', 'claimed')
          AND EXISTS (
            SELECT 1 FROM approval_continuation_calls AS calls
            WHERE calls.principal_id = approval_continuations.principal_id
              AND calls.approval_id = approval_continuations.approval_id
              AND calls.generation = approval_continuations.generation
              AND calls.toolkit_name IS NULL
              AND (calls.decision IS NULL OR calls.decision = 'approved')
          )
          AND NOT EXISTS (
            SELECT 1 FROM matrix_delivery_outbox AS final
            JOIN approval_continuation_sources AS source
              ON source.principal_id = final.principal_id
             AND source.event_id = final.delivery_id
            WHERE source.principal_id = approval_continuations.principal_id
              AND source.approval_id = approval_continuations.approval_id
              AND source.source_ordinal = 0
              AND final.stage = 'final'
              AND final.permanent_failure_reason IS NULL
          )
        """,
        (
            "This approval is from an older version of MindRoom and can no longer be used. "
            "Please check what already completed, then send a new request for anything unfinished.",
        ),
    )
