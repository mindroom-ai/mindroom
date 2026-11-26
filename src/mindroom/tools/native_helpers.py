"""Shared helpers for native MindRoom toolkits."""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any
from urllib.parse import quote


class UnsetType(Enum):
    """Sentinel distinguishing omitted constructor arguments from explicit ``None``."""

    UNSET = "UNSET"


UNSET = UnsetType.UNSET
_HOST_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def resolve_toggle(
    value: bool | None | UnsetType,
    *,
    default: bool,
    enable_all: bool = False,
) -> bool:
    """Resolve a boolean toggle without letting explicit ``None`` silently enable a tool."""
    if value is UNSET:
        return True if enable_all else default
    if value is None:
        return False
    return bool(value)


def require_non_empty(value: str | None, label: str) -> str:
    """Return stripped ``value`` or raise ``ValueError`` when blank or missing."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{label} is required."
        raise ValueError(msg)
    return value.strip()


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
