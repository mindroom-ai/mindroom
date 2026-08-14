"""Additive upgrades and compatibility boundaries for shipped journal schemas."""

from __future__ import annotations


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
