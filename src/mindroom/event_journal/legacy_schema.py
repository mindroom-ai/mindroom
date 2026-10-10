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


_PRE_REPLY_OUTBOX_COLUMNS = (
    "principal_id, delivery_id, stage, event_type, room_id, membership_epoch, thread_id, transaction_id, "
    "payload_json, result_json, edits_event_id, edit_target_pending, attempted, retired, "
    "permanent_failure_reason, sending_device_id, acknowledged_event_id, created_at_ns"
)


# LEGACY_COMPAT: Outbox rows without reply identity and with only initial and final stages.
# Legacy format: matrix_delivery_outbox without reply_id, span_id, reply_sequence, and reply_row_json columns, whose
# stage CHECK constraint admits only 'initial' and 'final'.
# Last legacy release: v2026.10.227; replacement: the unreleased durable reply messages add the four nullable
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


_PRE_WAITING_REPLY_COLUMNS = (
    "principal_id, reply_id, entity_name, room_id, thread_id, membership_epoch, event_id, state, current_span_id, "
    "last_span_id, presentation_json, possibly_shown_json, confirmed_seq, revision, placeholder_only, "
    "stop_receipt_order, stop_applied_receipt_order, stop_button_event_id, redaction_pending_json, owed_write_json, "
    "reply_sequence, created_at_ns, updated_at_ns"
)
_PRE_WAKE_SPAN_COLUMNS = (
    "principal_id, span_id, reply_id, kind, delivery_id, approval_id, bot_generation, base_sequence, rollback_json, "
    "outcome, claimed_at_ns"
)


# LEGACY_COMPAT: Reply records without background-work waits.
# Legacy format: reply_messages without the hold_key column, whose state CHECK constraint does not admit 'waiting',
# and reply_spans whose kind CHECK constraint does not admit 'wake'.
# Last legacy release: v2026.10.231; replacement: the unreleased background tool jobs add the 'waiting' reply state,
# the 'wake' span kind, and the nullable hold_key column.
# Handling: SQLite rebuilds both tables under the new definitions and copies every row unchanged, before the
# schema's indexes are created; PostgreSQL adds the column and replaces both constraints. Existing replies keep a
# null hold key, which every reader treats as a reply that never waited.
# Coverage: tests/test_journal_upgrade_boundary.py::test_reply_upgrade_keeps_rows_and_admits_waiting_and_wake.
def upgrade_reply_waits(
    transaction: Transaction,
    reply_columns: frozenset[str],
    *,
    reply_messages_ddl: str,
    reply_spans_ddl: str,
    sqlite: bool,
) -> None:
    """Admit waiting replies and wake spans in reply tables created before them, inside the schema transaction."""
    if not reply_columns or "hold_key" in reply_columns:
        return
    if sqlite:
        for table, ddl, columns in (
            ("reply_messages", reply_messages_ddl, _PRE_WAITING_REPLY_COLUMNS),
            ("reply_spans", reply_spans_ddl, _PRE_WAKE_SPAN_COLUMNS),
        ):
            transaction.execute(f"ALTER TABLE {table} RENAME TO {table}_pre_wait")
            transaction.execute(ddl)
            transaction.execute(
                f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_pre_wait",  # noqa: S608 - fixed names
            )
            # Dropping the renamed table drops the indexes that followed it; the
            # schema statements that run next create them on the new table.
            transaction.execute(f"DROP TABLE {table}_pre_wait")
        return
    transaction.execute("ALTER TABLE reply_messages ADD COLUMN IF NOT EXISTS hold_key TEXT")
    for table, column, values in (
        ("reply_messages", "state", "'active', 'paused', 'waiting', 'completed', 'cancelled', 'failed', 'gone'"),
        ("reply_spans", "kind", "'turn', 'replay', 'approval_resume', 'regeneration', 'wake'"),
    ):
        # The released constraints are unnamed in their DDL; find them in the
        # catalog rather than trusting the names PostgreSQL generated.
        for row in transaction.fetchall(
            f"""
            SELECT conname FROM pg_constraint
            WHERE conrelid = '{table}'::regclass AND contype = 'c'
              AND strpos(pg_get_constraintdef(oid), '{column}') > 0
            """,  # noqa: S608 - fixed names
        ):
            transaction.execute(f'ALTER TABLE {table} DROP CONSTRAINT "{row["conname"]}"')
        transaction.execute(
            f"ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_check CHECK ({column} IN ({values}))",
        )
