"""Additive upgrades and compatibility boundaries for shipped journal schemas."""

from __future__ import annotations

from .schema import SQLITE_DIALECT, SchemaDialect

_APPROVAL_CARD_NATIVE_IDENTITY_COLUMNS = (
    ("continuation_id", "TEXT"),
    ("continuation_generation", "BIGINT"),
    ("tool_call_id", "TEXT"),
)


def pre_schema_migration_statements(
    *,
    interactive_question_columns: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Archive the obsolete derived-prompt table before creating its replacement."""
    if "claimed_source_event_id" not in interactive_question_columns:
        return ()
    return (
        "CREATE TABLE interactive_questions_pre_selection AS SELECT * FROM interactive_questions",
        "DROP TABLE interactive_questions",
    )


def migration_statements(
    dialect: SchemaDialect,
    *,
    approval_card_columns: frozenset[str] = frozenset(),
    approval_continuation_call_columns: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Add nullable approval columns without assigning meaning to old rows."""
    if dialect == SQLITE_DIALECT:
        card_statements = tuple(
            f"ALTER TABLE approval_cards ADD COLUMN {name} {column_type}"
            for name, column_type in _APPROVAL_CARD_NATIVE_IDENTITY_COLUMNS
            if name not in approval_card_columns
        )
        call_statements = (
            ("ALTER TABLE approval_continuation_calls ADD COLUMN human_approval_required BOOLEAN",)
            if "human_approval_required" not in approval_continuation_call_columns
            else ()
        )
        return (*card_statements, *call_statements)
    return (
        *tuple(
            f"ALTER TABLE approval_cards ADD COLUMN IF NOT EXISTS {name} {column_type}"
            for name, column_type in _APPROVAL_CARD_NATIVE_IDENTITY_COLUMNS
        ),
        "ALTER TABLE approval_continuation_calls ADD COLUMN IF NOT EXISTS human_approval_required BOOLEAN",
    )
