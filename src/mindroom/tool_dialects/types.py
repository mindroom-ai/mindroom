"""Types and argument helpers describing how one model family sees MindRoom's canonical tools on the wire."""

from __future__ import annotations

import json
import math
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from mindroom.tool_system.tool_access import ToolKey

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

type DialectName = Literal["mindroom", "claude", "codex"]

APPLY_PATCH = ToolKey("coding", "apply_patch")
FILE_EDITS = (ToolKey("coding", "edit_file"), ToolKey("coding", "write_file"))


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
    custom_format: dict[str, Any] | None = None
    """Responses API freeform tool format; the call's raw text arrives as the canonical ``input`` argument."""
    carried_notes: tuple[str, ...] = ()
    """MindRoom notes copied from the canonical description into the wire description when present there."""


@dataclass(frozen=True)
class ToolDialect:
    """The wire presentation of canonical tools for one model family."""

    name: DialectName
    functions: tuple[WireFunction, ...] = ()
    replaced: Mapping[ToolKey, tuple[ToolKey, ...]] = field(default_factory=dict)
    """Canonical functions the dialect hides when the request also has a function that replaces them."""


_KIND_NAMES: dict[type, str] = {str: "a string", bool: "a boolean", int: "an integer", float: "a number"}


def wire_argument(
    arguments: dict[str, Any],
    tool: str,
    name: str,
    *aliases: str,
    kind: type = str,
    required: bool = True,
) -> Any:  # noqa: ANN401
    """Return argument *name* (or the first present alias) of a wire call, checked against *kind*.

    ``float`` accepts integers too; booleans never count as numbers.
    """
    for key in (name, *aliases):
        value = arguments.get(key)
        if value is None:
            continue
        accepted = (int, float) if kind is float else kind
        if not isinstance(value, accepted) or (kind is not bool and isinstance(value, bool)):
            msg = f"{tool} {name} must be {_KIND_NAMES[kind]}"
            raise DialectArgumentError(msg)
        if kind is float and not math.isfinite(value):
            msg = f"{tool} {name} must be a finite number"
            raise DialectArgumentError(msg)
        return value
    if required:
        msg = f"{tool} requires {name}"
        raise DialectArgumentError(msg)
    return None


def milliseconds_to_seconds(milliseconds: float, tool: str, name: str) -> int:
    """Return a positive wire duration in milliseconds as whole canonical seconds, rounded up."""
    if milliseconds <= 0:
        msg = f"{tool} {name} must be a positive number of milliseconds"
        raise DialectArgumentError(msg)
    return math.ceil(milliseconds / 1000)


def shell_command_text(args: object) -> str:
    """Return canonical shell ``args`` as the command line that ran; recorded history may hold any JSON value there.

    Like the shell tool, a JSON argv string runs as argv and a one-item list runs as a command line.
    """
    if isinstance(args, str) and args.lstrip().startswith("["):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return args
    if isinstance(args, list):
        if len(args) == 1 and isinstance(args[0], str):
            return args[0]
        return " ".join(shlex.quote(str(arg)) for arg in args)
    return str(args or "")


def object_schema(properties: dict[str, dict[str, Any]], *required: str) -> dict[str, Any]:
    """Return a closed JSON object schema with *properties*, of which *required* are required."""
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}
