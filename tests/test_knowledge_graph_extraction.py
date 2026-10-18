"""What Graphiti is told when it extracts an episode: entity types and instructions."""

from __future__ import annotations

import re

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
    """The extractor sees only a type's description, so it has to say what is not that type."""
    for name, model in ENTITY_TYPES.items():
        assert model.__doc__
        assert re.search(r"\b(never|not)\b", model.__doc__.lower()), name


def test_graphiti_prompt_lists_exactly_the_types_with_their_docstrings_and_no_entity() -> None:
    """With ``Entity`` excluded, the types in Graphiti's extraction prompt are ours, described by our docstrings."""
    from graphiti_core.utils.maintenance.node_operations import (  # noqa: PLC0415
        _build_entity_types_context,
        _filter_entity_types_context,
    )

    context = _filter_entity_types_context(_build_entity_types_context(ENTITY_TYPES), EXCLUDED_ENTITY_TYPES)

    assert [
        (entry["entity_type_id"], entry["entity_type_name"], entry["entity_type_description"]) for entry in context
    ] == [(type_id, name, model.__doc__) for type_id, (name, model) in enumerate(ENTITY_TYPES.items())]


def test_instructions_keep_paths_and_an_assistants_own_activity_out() -> None:
    """Paths and URLs belong inside facts, dated by when they held; an assistant's lookups stay out, its effects stay in."""
    text = extraction_instructions("")
    for phrase in ("file paths", "URLs", "environment variables", "commit hashes", "branch names", "when it happened"):
        assert phrase in text
    assert "`agent <name>`" in text
    for phrase in ("lookups", "access", "configuration", "counts", "listing returned", "changed things for others"):
        assert phrase in text


def test_configured_instructions_follow_the_built_in_ones() -> None:
    """An operator's instructions add to the built-in ones, and blank text adds nothing."""
    built_in = extraction_instructions("")
    assert extraction_instructions("  \n") == built_in
    assert extraction_instructions("  Focus on lab equipment.\n") == f"{built_in}\nFocus on lab equipment."
