"""Shared fakes for knowledge-index tests."""

from __future__ import annotations

from typing import Any


def metadata_matches(metadata: dict[str, Any], key: str, condition: object) -> bool:
    """Mirror the subset of ChromaDB ``where`` matching the indexer relies on.

    Only the operators MindRoom actually issues are supported; anything else
    raises so a new query shape cannot silently pass against the fake.
    """
    if isinstance(condition, dict):
        if "$in" in condition:
            return metadata.get(key) in condition["$in"]
        if "$eq" in condition:
            return metadata.get(key) == condition["$eq"]
        msg = f"unsupported where condition: {condition!r}"
        raise AssertionError(msg)
    return metadata.get(key) == condition
