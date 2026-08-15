"""Additive upgrades and compatibility boundaries for shipped journal schemas."""

from __future__ import annotations


def pre_schema_migration_statements(
    *,
    approval_continuation_call_columns: frozenset[str] = frozenset(),
    interactive_question_columns: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Return upgrades that must run before installing the current schema."""
    statements: list[str] = []
    if approval_continuation_call_columns and "human_approval_required" not in approval_continuation_call_columns:
        statements.append(
            "ALTER TABLE approval_continuation_calls ADD COLUMN human_approval_required BOOLEAN",
        )
    if "claimed_source_event_id" in interactive_question_columns:
        statements.extend(
            (
                "CREATE TABLE interactive_questions_pre_selection AS SELECT * FROM interactive_questions",
                "DROP TABLE interactive_questions",
            ),
        )
    return tuple(statements)
