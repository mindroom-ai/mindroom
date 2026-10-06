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


# LEGACY_COMPAT: Approval calls without persisted argument digests.
# Legacy format: approval_continuation_calls rows written before per-call argument digests, which have no
# arguments_digest column or a NULL value there.
# Last legacy release: v2026.10.35; replacement: v2026.10.36 stores a SHA-256 digest of each paused call's
# canonical arguments.
# Handling: Add the nullable column and keep historical rows; an approved call without a digest never executes,
# because continuation refuses calls whose persisted arguments do not match their digest and fails normally.
# The exception is CLI recovery of a generated `agent` function, which runs the arguments saved in the journal's
# own CLI payload, where worker code cannot write, whether or not a digest was recorded.
# Coverage: tests/test_journal_upgrade_boundary.py::test_approval_argument_digest_upgrade_keeps_calls_unexecutable;
# tests/test_cli_approval_recovery.py::test_generated_cli_approval_rebuilds_and_authorizes_exact_function.
def upgrade_approval_argument_digests(transaction: Transaction, columns: frozenset[str]) -> None:
    """Add the argument digest column inside the schema transaction."""
    if "arguments_digest" not in columns:
        transaction.execute("ALTER TABLE approval_continuation_calls ADD COLUMN arguments_digest TEXT")


_PRE_REPLY_OUTBOX_COLUMNS = (
    "principal_id, delivery_id, stage, event_type, room_id, membership_epoch, thread_id, transaction_id, "
    "payload_json, result_json, edits_event_id, edit_target_pending, attempted, retired, "
    "permanent_failure_reason, sending_device_id, acknowledged_event_id, created_at_ns"
)


# LEGACY_COMPAT: Outbox rows without reply identity and with only initial and final stages.
# Legacy format: matrix_delivery_outbox without reply_id, span_id, reply_sequence, and reply_row_json columns, whose
# stage CHECK constraint admits only 'initial' and 'final'.
# Last legacy release: v2026.10.162; replacement: the unreleased durable reply messages add the four nullable
# columns and the 'edit' stage for non-terminal reply writes.
# Handling: SQLite rebuilds the table under the new definition and copies every row unchanged, before the schema's
# indexes are created; PostgreSQL adds the columns and replaces the stage constraint. Existing rows keep null reply
# identity, which every reader treats as a delivery that is not a reply row.
# Coverage: tests/test_journal_upgrade_boundary.py::test_outbox_upgrade_keeps_rows_and_admits_edit_stage.
def upgrade_outbox_reply_rows(
    transaction: Transaction,
    outbox_columns: frozenset[str],
    *,
    outbox_table_ddl: str,
    sqlite: bool,
) -> None:
    """Admit reply rows in an outbox created before them, inside the schema transaction."""
    if not outbox_columns or "reply_id" in outbox_columns:
        return
    if sqlite:
        transaction.execute("ALTER TABLE matrix_delivery_outbox RENAME TO matrix_delivery_outbox_pre_reply")
        transaction.execute(outbox_table_ddl)
        transaction.execute(
            f"INSERT INTO matrix_delivery_outbox ({_PRE_REPLY_OUTBOX_COLUMNS}) "  # noqa: S608 - fixed columns
            f"SELECT {_PRE_REPLY_OUTBOX_COLUMNS} FROM matrix_delivery_outbox_pre_reply",
        )
        # Dropping the renamed table drops the indexes that followed it; the
        # schema statements that run next create them on the new table.
        transaction.execute("DROP TABLE matrix_delivery_outbox_pre_reply")
        return
    transaction.execute("ALTER TABLE matrix_delivery_outbox ADD COLUMN IF NOT EXISTS reply_id TEXT")
    transaction.execute("ALTER TABLE matrix_delivery_outbox ADD COLUMN IF NOT EXISTS span_id TEXT")
    transaction.execute("ALTER TABLE matrix_delivery_outbox ADD COLUMN IF NOT EXISTS reply_sequence BIGINT")
    transaction.execute("ALTER TABLE matrix_delivery_outbox ADD COLUMN IF NOT EXISTS reply_row_json TEXT")
    # The released constraint is unnamed in its DDL; find it in the catalog
    # rather than trusting the name PostgreSQL generated for it.
    for row in transaction.fetchall(
        """
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'matrix_delivery_outbox'::regclass AND contype = 'c'
          AND strpos(pg_get_constraintdef(oid), 'stage') > 0
        """,
    ):
        transaction.execute(f'ALTER TABLE matrix_delivery_outbox DROP CONSTRAINT "{row["conname"]}"')
    transaction.execute(
        "ALTER TABLE matrix_delivery_outbox ADD CONSTRAINT matrix_delivery_outbox_stage_check "
        "CHECK (stage IN ('initial', 'final', 'edit'))",
    )
