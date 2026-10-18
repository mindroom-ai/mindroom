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


def test_every_entity_type_says_what_does_not_belong() -> None:
    """Without a generic type to fall back on, a type's description is what keeps paths, URLs, and handles out of it."""
    for name, model in ENTITY_TYPES.items():
        assert model.__doc__
        description = model.__doc__.lower()
        assert "never" in description or "not" in description, name


def test_graphiti_accepts_the_types_and_the_exclusion() -> None:
    """Graphiti's own checks in ``add_episode`` reject field names that clash with its nodes and unknown exclusions."""
    from graphiti_core.helpers import validate_excluded_entity_types  # noqa: PLC0415
    from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types  # noqa: PLC0415

    assert validate_entity_types(ENTITY_TYPES)
    assert validate_excluded_entity_types(EXCLUDED_ENTITY_TYPES, ENTITY_TYPES)


def test_instructions_keep_paths_and_urls_out_and_date_by_occurrence() -> None:
    """Paths and URLs belong inside facts, and a fact is dated by when it held, not when it was discussed."""
    text = extraction_instructions()
    for word in ("file paths", "URLs", "environment variables", "commit hashes", "branch names"):
        assert word in text
    assert "when it happened" in text
