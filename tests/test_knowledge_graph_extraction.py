"""What Graphiti is told when it extracts an episode: entity types and instructions."""

from __future__ import annotations

from mindroom.knowledge_graph.extraction import ENTITY_TYPES, EXCLUDED_ENTITY_TYPES, extraction_instructions


def test_every_entity_type_has_a_description_and_no_fields() -> None:
    """Graphiti reads a type's docstring as its description and rejects fields that clash with its own."""
    assert set(ENTITY_TYPES) == {
        "Person",
        "Agent",
        "Organization",
        "Location",
        "Event",
        "Project",
        "Product",
        "Document",
        "Account",
    }
    for model in ENTITY_TYPES.values():
        assert model.__doc__
        assert model.__doc__.strip()
        assert model.model_fields == {}


def test_generic_entity_type_is_excluded() -> None:
    """A node the model cannot place in one of the types falls back to Graphiti's generic type and is dropped."""
    assert EXCLUDED_ENTITY_TYPES == ["Entity"]


def test_instructions_keep_paths_and_urls_out_and_date_by_occurrence() -> None:
    """Paths and URLs belong inside facts, and a fact is dated by when it held, not when it was discussed."""
    text = extraction_instructions()
    for word in ("file paths", "URLs", "environment variables", "commit hashes", "branch names"):
        assert word in text
    assert "when it happened" in text
