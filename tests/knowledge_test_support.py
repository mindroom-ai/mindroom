"""Shared fakes for knowledge-index tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence


def validate_where_operands(where: dict[str, Any] | None) -> None:
    """Reject the ``where`` shapes ChromaDB refuses before it reaches the store.

    Chroma validates an empty ``$in`` operand and raises, so a fake that only
    filtered records would answer "nothing matched" for a query production
    cannot issue at all -- and would answer it most convincingly on an empty
    collection, where no record is ever tested. Checking the operand rather
    than the records is what makes that case visible.
    """
    if not where:
        return
    for condition in where.values():
        if isinstance(condition, dict) and "$in" in condition and not condition["$in"]:
            msg = "empty $in operand: ChromaDB rejects this before querying the store"
            raise AssertionError(msg)


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
    documents: list[str] | None = None,
    embeddings: list[list[float]] | None = None,
) -> dict[str, Any]:
    """Shape a Chroma ``get`` result, honoring ``include`` the way Chroma does.

    Chroma omits whatever was not requested: ``include=[]`` returns
    ``metadatas: None`` while still returning ids. That asymmetry is the whole
    reason an existence probe must read ids, so a fake that returned metadatas
    regardless would let a probe reading them pass in tests while reporting
    "no vectors" for every file in production.

    ``include`` is required rather than defaulted. Every production caller
    passes it explicitly, and guessing a default here would model a shape
    nothing exercises. Asking for a field the caller did not supply raises
    rather than returning ``None``, so a fake that cannot serve a query shape
    says so instead of looking like an empty store.
    """
    available: dict[str, list[Any] | None] = {
        "metadatas": metadatas,
        "documents": documents,
        "embeddings": embeddings,
    }
    unsupported = set(include) - set(available)
    if unsupported:
        msg = f"unsupported include fields: {sorted(unsupported)}"
        raise AssertionError(msg)
    absent = [name for name in include if available[name] is None]
    if absent:
        msg = f"include asked for fields this fake was not given: {sorted(absent)}"
        raise AssertionError(msg)
    return {"ids": ids, **{name: (values if name in include else None) for name, values in available.items()}}
