"""Shared helpers for native MindRoom toolkits."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from mindroom.tool_system.tool_function import ToolFunction


class UnsetType(Enum):
    """Sentinel distinguishing omitted constructor arguments from explicit ``None``."""

    UNSET = "UNSET"


# Typed as ``Any`` so both ``bool | None = UNSET`` and ``bool | None | UnsetType = UNSET`` typecheck cleanly.
UNSET: Any = UnsetType.UNSET
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
_PYTHON_LITERAL_FIXES = (
    (re.compile(r"\btrue\b"), "True"),
    (re.compile(r"\bfalse\b"), "False"),
    (re.compile(r"\bnone\b"), "None"),
)


def resolve_bool_toggle(value: bool | None | UnsetType, *, default: bool) -> bool:
    """Return ``default`` when omitted, ``False`` on explicit ``None``, otherwise ``bool(value)``."""
    if value is UNSET:
        return default
    return bool(value)


def resolve_toggle(
    value: bool | None | UnsetType,
    *,
    default: bool,
    enable_all: bool = False,
) -> bool:
    """Resolve a boolean toggle without letting explicit ``None`` silently enable a tool."""
    if value is None:
        return False
    if enable_all:
        return True
    if value is UNSET:
        return default
    return bool(value)


def restrict_actions(
    function: ToolFunction,
    allowed_actions: Sequence[str],
    *,
    action_fields: Mapping[str, str] | None = None,
    common_fields: Sequence[str] = (),
) -> ToolFunction:
    """Narrow an action-dispatched tool function's schema and docstring bullets to ``allowed_actions``.

    Preserves the function's declared ``action`` enum order and returns ``function`` unchanged when all
    declared actions remain allowed. When ``action_fields`` is ``None``, only the ``action`` enum and
    ``- `<action>`: ...`` docstring bullets are narrowed; when provided, ``properties`` and ``required``
    are also pruned to ``action``, ``common_fields``, and the fields declared for allowed actions.
    """
    properties = function.parameters.get("properties")
    if not isinstance(properties, dict) or "action" not in properties:
        return function

    current_enum = properties["action"].get("enum")
    if isinstance(current_enum, list):
        allowed_set = set(allowed_actions)
        ordered_allowed = [action for action in current_enum if action in allowed_set]
        if len(ordered_allowed) < len(allowed_set):
            current_set = set(current_enum)
            ordered_allowed.extend(dict.fromkeys(action for action in allowed_actions if action not in current_set))
        if ordered_allowed == current_enum:
            return function
    else:
        ordered_allowed = list(allowed_actions)

    updated_properties = dict(properties)
    updated_properties["action"] = {**updated_properties["action"], "enum": ordered_allowed}
    if action_fields is not None:
        keep = {
            "action",
            *common_fields,
            *(field for action in ordered_allowed for field in action_fields.get(action, "").split()),
        }
        updated_properties = {name: value for name, value in updated_properties.items() if name in keep}

    description = function.description
    if description is not None:
        description = "\n".join(
            line
            for line in description.splitlines()
            if not line.startswith("- `") or any(f"`{action}`" in line.partition(":")[0] for action in ordered_allowed)
        )

    parameters = {**function.parameters, "properties": updated_properties}
    if action_fields is not None and "required" in parameters:
        parameters["required"] = [name for name in parameters["required"] if name in updated_properties]
    return replace(function, description=description, parameters=parameters)


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
    """Return stripped ``value`` or raise ``ValueError`` naming ``field_name``."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{field_name} is required."
        raise ValueError(msg)
    return value.strip()


def require_non_blank_text(value: str | None, label: str) -> str:
    """Return ``value`` verbatim after verifying it is a non-blank string."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{label} is required."
        raise ValueError(msg)
    return value


def require_enabled_action(action: str, enabled: Iterable[str]) -> str | None:
    """Return an error string if ``action`` is disabled, otherwise ``None``."""
    allowed = tuple(enabled)
    if action not in allowed:
        return f"Action {action!r} is disabled. Enabled actions: {', '.join(allowed)}"
    return None


def safe_path_segment(value: object, field_name: str) -> str:
    """Validate and URL-encode a single REST path segment, rejecting traversal and delimiters."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or value in {".", ".."}
        or len(value) > 1024
        or any(ch in "/\\?#%" or ord(ch) < 32 or ord(ch) == 127 for ch in value)
    ):
        msg = f"Invalid {field_name}: must be a single path segment without '/', '\\\\', '?', '#', '%', or '..'."
        raise ValueError(msg)
    return quote(value, safe="")


def safe_host_label(value: object, field_name: str) -> str:
    """Validate a single DNS subdomain label (e.g. shop or company subdomain)."""
    if not isinstance(value, str) or not _HOST_LABEL_RE.fullmatch(value.strip()):
        msg = f"Invalid {field_name}: must be a single DNS label of letters, digits, and hyphens."
        raise ValueError(msg)
    return value.strip().lower()


def json_dumps(payload: Any) -> str:  # noqa: ANN401
    """Serialize ``payload`` to compact JSON, stringifying non-JSON values safely."""
    return json.dumps(payload, default=str)


def error_json(message: str, **extra: Any) -> str:  # noqa: ANN401
    """Return a JSON error payload."""
    return json_dumps({"error": message, **extra})


def status_error_json(message: str) -> str:
    """Return a JSON status/message error payload."""
    return json.dumps({"status": "error", "message": message})


def format_http_error(
    service: str,
    exc: Exception,
    *,
    secrets: Iterable[str | None] = (),
    detail_path: tuple[str, ...] = ("message",),
) -> str:
    """Format an HTTP/JSON error with optional nested detail extraction and secret redaction."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        detail = ""
        try:
            body: Any = exc.response.json()
        except ValueError:
            body = None
        for key in detail_path:
            if not isinstance(body, dict):
                body = None
                break
            body = body.get(key)
        if isinstance(body, str):
            detail = body.strip()
            for secret in secrets:
                if secret:
                    detail = detail.replace(secret, "[REDACTED]")
        return f"{service} API error ({status}): {detail}" if detail else f"{service} API error ({status})"
    return f"{service} request failed: {type(exc).__name__}"


def prepare_python_code(code: str) -> str:
    """Fix lowercase JSON-style boolean/null literals common in model-generated Python."""
    for pattern, replacement in _PYTHON_LITERAL_FIXES:
        code = pattern.sub(replacement, code)
    return code
