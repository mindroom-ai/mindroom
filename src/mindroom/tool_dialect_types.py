"""Types describing how one model family sees MindRoom's canonical tools on the provider wire."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.tool_access import ToolKey

type DialectName = Literal["mindroom", "claude", "codex"]

# Tool-call dict key recording the exact wire form of a translated call: dialect, toolkit, name, arguments, custom.
MINDROOM_WIRE_KEY = "mindroom_wire"


class DialectArgumentError(ValueError):
    """A wire tool call whose arguments cannot become a canonical call."""


@dataclass(frozen=True)
class WireFunction:
    """One canonical function as a model family's harness names and shapes it."""

    key: ToolKey
    wire_name: str
    description: str
    parameters: dict[str, Any]
    to_canonical: Callable[[dict[str, Any]], dict[str, Any]]
    """Translate wire arguments to canonical arguments; raises DialectArgumentError."""
    to_wire: Callable[[dict[str, Any]], dict[str, Any]]
    """Translate canonical arguments to wire arguments for history recorded in another dialect."""
    render_result: Callable[[str], str] | None = None
    """Rewrite fixed MindRoom result templates into the dialect's wording; everything else passes through."""
    custom_format: dict[str, Any] | None = None
    """Responses API freeform tool format; the call's raw text arrives as the canonical ``input`` argument."""


@dataclass(frozen=True)
class ToolDialect:
    """The wire presentation of canonical tools for one model family."""

    name: DialectName
    functions: tuple[WireFunction, ...] = ()
    hidden: frozenset[ToolKey] = frozenset()
