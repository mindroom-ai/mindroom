"""Tool-owned approval bypass predicates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

_ToolApprovalBypassPredicate = Callable[[Callable[..., Any], Mapping[str, object]], bool]

_BYPASS_PREDICATES: dict[str, _ToolApprovalBypassPredicate] = {}


def register_tool_approval_bypass(function_name: str, predicate: _ToolApprovalBypassPredicate) -> None:
    """Register a tool-owned predicate that can skip Matrix approval for one call."""
    if not function_name:
        msg = "function_name must not be empty"
        raise ValueError(msg)
    _BYPASS_PREDICATES[function_name] = predicate


def should_bypass_tool_approval(
    function_name: str,
    entrypoint: Callable[..., Any],
    arguments: Mapping[str, object],
) -> bool:
    """Return whether a registered tool wants this call to skip Matrix approval."""
    predicate = _BYPASS_PREDICATES.get(function_name)
    if predicate is None:
        return False
    return predicate(entrypoint, arguments)
