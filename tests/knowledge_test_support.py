"""Shared fakes for knowledge-index tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


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


def chroma_get_result(
    *,
    ids: list[str],
    metadatas: list[dict[str, Any]],
    include: Sequence[str],
) -> dict[str, Any]:
    """Shape a Chroma ``get`` result, honoring ``include`` the way Chroma does.

    Chroma omits whatever was not requested: ``include=[]`` returns
    ``metadatas: None`` while still returning ids. That asymmetry is the whole
    reason an existence probe must read ids, so a fake that returned metadatas
    regardless would let a probe reading them pass in tests while reporting
    "no vectors" for every file in production.

    ``include`` is required rather than defaulted. Every production caller
    passes it explicitly, and guessing a default here would model a shape
    nothing exercises.
    """
    unsupported = set(include) - {"metadatas"}
    if unsupported:
        msg = f"unsupported include fields: {sorted(unsupported)}"
        raise AssertionError(msg)
    return {"ids": ids, "metadatas": metadatas if "metadatas" in include else None}
