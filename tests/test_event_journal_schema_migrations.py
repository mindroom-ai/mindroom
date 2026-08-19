"""Focused ownership tests for pre-schema event-journal upgrades."""

from mindroom.event_journal.schema_migrations import pre_schema_migration_statements


def test_pre_schema_migrations_add_missing_approval_provenance_column() -> None:
    """An existing legacy approval-call table receives the provenance column."""
    assert pre_schema_migration_statements(
        approval_continuation_call_columns=frozenset({"tool_call_id"}),
    ) == ("ALTER TABLE approval_continuation_calls ADD COLUMN human_approval_required BOOLEAN",)


def test_pre_schema_migrations_add_missing_delivery_result_column() -> None:
    """An existing outbox gains storage that is never part of its wire payload."""
    assert pre_schema_migration_statements(
        matrix_delivery_outbox_columns=frozenset({"payload_json"}),
    ) == ("ALTER TABLE matrix_delivery_outbox ADD COLUMN result_json TEXT",)


def test_pre_schema_migrations_skip_absent_or_current_approval_table() -> None:
    """The approval upgrade is a no-op without a legacy table needing it."""
    assert pre_schema_migration_statements() == ()
    assert (
        pre_schema_migration_statements(
            approval_continuation_call_columns=frozenset({"tool_call_id", "human_approval_required"}),
            matrix_delivery_outbox_columns=frozenset({"payload_json", "result_json"}),
        )
        == ()
    )


def test_pre_schema_migrations_compose_independent_upgrades() -> None:
    """Independent shipped-schema upgrades remain composable and ordered."""
    assert pre_schema_migration_statements(
        approval_continuation_call_columns=frozenset({"tool_call_id"}),
        interactive_question_columns=frozenset({"claimed_source_event_id"}),
        matrix_delivery_outbox_columns=frozenset({"payload_json"}),
    ) == (
        "ALTER TABLE approval_continuation_calls ADD COLUMN human_approval_required BOOLEAN",
        "ALTER TABLE matrix_delivery_outbox ADD COLUMN result_json TEXT",
        "CREATE TABLE interactive_questions_pre_selection AS SELECT * FROM interactive_questions",
        "DROP TABLE interactive_questions",
    )
