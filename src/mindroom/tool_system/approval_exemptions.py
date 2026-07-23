"""Tool-owned approval exemptions.

Built-in tools can register a predicate that marks specific calls as not
needing Matrix approval because the call cannot perform the protected action.
Exemptions are keyed by function name, matching the granularity of the
approval rules they refine.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_EXEMPTION_PREDICATES: dict[str, Callable[[Mapping[str, object]], bool]] = {}


def register_tool_approval_exemption(
    function_name: str,
    is_exempt: Callable[[Mapping[str, object]], bool],
) -> None:
    """Register a tool-owned predicate that marks calls whose approval would be redundant.

    Predicates must fail closed: return True only when the call provably cannot
    perform the action the approval rule protects.
    """
    _EXEMPTION_PREDICATES[function_name] = is_exempt


def tool_call_is_approval_exempt(function_name: str, arguments: Mapping[str, object]) -> bool:
    """Return whether the owning tool marked this call as not needing approval."""
    is_exempt = _EXEMPTION_PREDICATES.get(function_name)
    return is_exempt is not None and is_exempt(arguments)
