"""Shared helpers for merging Matrix and persisted metadata payloads."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast


def deep_merge_metadata(
    base: dict[str, Any] | None,
    extra: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a recursive metadata merge without mutating either input."""
    if base is None:
        return deepcopy(extra) if extra is not None else None
    if extra is None:
        return deepcopy(base)
    merged = deepcopy(base)
    for key, value in extra.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = (
                deep_merge_metadata(
                    cast("dict[str, Any]", existing),
                    cast("dict[str, Any]", value),
                )
                or {}
            )
        else:
            merged[key] = deepcopy(value)
    return merged
