"""Shared helpers for MindRoom's native toolkits."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from mindroom.tool_system.tool_function import ToolFunction

# Sentinel distinguishing an omitted constructor toggle from an explicit ``None`` (which stays fail-closed).
UNSET: Any = object()


def resolve_bool_toggle(value: bool | None, *, default: bool) -> bool:
    """Return ``default`` when omitted, ``False`` on explicit ``None``, otherwise ``bool(value)``."""
    if value is UNSET:
        return default
    return bool(value)


def narrow_action_enum(function: ToolFunction, allowed_actions: Sequence[str]) -> ToolFunction:
    """Restrict a tool function's ``action`` JSON schema enum to ``allowed_actions``."""
    properties = function.parameters.get("properties")
    if not isinstance(properties, dict) or "action" not in properties:
        return function
    updated_properties = dict(properties)
    updated_properties["action"] = {**updated_properties["action"], "enum": list(allowed_actions)}
    return replace(function, parameters={**function.parameters, "properties": updated_properties})


def format_tabular_rows(
    columns: Sequence[str],
    rows: Sequence[Sequence[object]],
    *,
    empty_message: str | None = None,
) -> str:
    """Format query columns and rows as CSV-like text."""
    if not rows and empty_message is not None:
        return empty_message
    header = ",".join(columns)
    data = "\n".join(",".join(str(cell) for cell in row) for row in rows)
    return f"{header}\n{data}"


def quote_sql_identifier(identifier: str) -> str:
    """Quote a SQL identifier or dot-separated qualified identifier safely."""
    stripped = identifier.strip()
    if not stripped or "\x00" in stripped:
        msg = f"Invalid SQL identifier: {identifier!r}"
        raise ValueError(msg)
    parts = stripped.split(".")
    if any(not part for part in parts):
        msg = f"Invalid SQL identifier: {identifier!r}"
        raise ValueError(msg)
    return ".".join('"' + part.replace('"', '""') + '"' for part in parts)


def escape_sql_literal(value: str) -> str:
    """Escape a string for single-quoted SQL literals while rejecting NUL bytes."""
    if "\x00" in value:
        msg = "SQL string value must not contain NUL bytes"
        raise ValueError(msg)
    return value.replace("'", "''")


def first_sql_statement(query: str) -> str:
    """Strip backticks and keep only the first semicolon-delimited SQL statement."""
    return query.replace("`", "").split(";")[0].strip()


def require_non_empty(value: str | None, field_name: str) -> str:
    """Return a non-blank string or raise ``ValueError`` naming ``field_name``."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{field_name} is required"
        raise ValueError(msg)
    return value.strip()


def require_enabled_action(action: str, enabled: Iterable[str]) -> str | None:
    """Return an error string if ``action`` is disabled, otherwise ``None``."""
    allowed = tuple(enabled)
    if action not in allowed:
        return f"Action {action!r} is disabled. Enabled actions: {', '.join(allowed)}"
    return None
