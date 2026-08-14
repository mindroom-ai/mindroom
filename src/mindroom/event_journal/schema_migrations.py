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
) -> tuple[str, ...]:
    """Add nullable native-card identity columns without activating old rows."""
    if dialect == SQLITE_DIALECT:
        return tuple(
            f"ALTER TABLE approval_cards ADD COLUMN {name} {column_type}"
            for name, column_type in _APPROVAL_CARD_NATIVE_IDENTITY_COLUMNS
            if name not in approval_card_columns
        )
    return tuple(
        f"ALTER TABLE approval_cards ADD COLUMN IF NOT EXISTS {name} {column_type}"
        for name, column_type in _APPROVAL_CARD_NATIVE_IDENTITY_COLUMNS
    )
