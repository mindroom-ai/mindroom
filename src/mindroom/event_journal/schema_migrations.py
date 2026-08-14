"""Additive upgrades and compatibility boundaries for shipped journal schemas."""

from __future__ import annotations

from .schema import SQLITE_DIALECT, SchemaDialect

_APPROVAL_CARD_NATIVE_IDENTITY_COLUMNS = (
    ("continuation_id", "TEXT"),
    ("continuation_generation", "BIGINT"),
    ("tool_call_id", "TEXT"),
)


def validate_interactive_question_columns(existing_columns: frozenset[str]) -> None:
    """Refuse the old question-claim schema instead of guessing its ownership."""
    if "claimed_source_event_id" not in existing_columns:
        return
    msg = (
        "This event journal uses the incompatible pre-selection schema; "
        "recreate the event journal database before starting MindRoom"
    )
    raise RuntimeError(msg)


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
