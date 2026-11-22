"""Sandbox subprocess payload and response protocol helpers."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

from pydantic import BaseModel, Field

_RESPONSE_MARKER = "__SANDBOX_RESPONSE__"
_TIMING_MARKER = "__SANDBOX_TIMING__"


class SandboxSubprocessEnvelope(BaseModel):
    """Explicit parent-to-child payload for sandbox subprocess execution."""

    request: dict[str, Any] = Field(default_factory=dict)
    runtime_paths: dict[str, Any] = Field(default_factory=dict)
    config_yaml: str


def serialize_subprocess_envelope(
    *,
    request: dict[str, Any],
    runtime_paths: dict[str, Any],
    config_yaml: str,
) -> str:
    """Serialize the explicit parent-to-child subprocess payload."""
    return SandboxSubprocessEnvelope(
        request=dict(request),
        runtime_paths=dict(runtime_paths),
        config_yaml=config_yaml,
    ).model_dump_json()


def parse_subprocess_envelope(payload: str) -> SandboxSubprocessEnvelope:
    """Parse the serialized subprocess payload from stdin."""
    return SandboxSubprocessEnvelope.model_validate_json(payload)


def response_marker_payload(response_json: str, *, timing: Mapping[str, object] | None = None) -> str:
    """Prefix one response payload with optional timing and the stderr marker."""
    if timing is None:
        return _RESPONSE_MARKER + response_json
    return f"{_TIMING_MARKER}{json.dumps(dict(timing), sort_keys=True)}\n{_RESPONSE_MARKER}{response_json}"


def extract_timing(stderr: str) -> dict[str, object] | None:
    """Extract the trailing marked child-timing JSON object from subprocess stderr."""
    response_pos = stderr.rfind(_RESPONSE_MARKER)
    search_end = len(stderr) if response_pos == -1 else response_pos
    marker_pos = stderr.rfind(_TIMING_MARKER, 0, search_end)
    if marker_pos == -1:
        return None
    line_start = marker_pos + len(_TIMING_MARKER)
    line_end = stderr.find("\n", line_start, search_end)
    raw = stderr[line_start : search_end if line_end == -1 else line_end].strip()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_response_json(stderr: str) -> str | None:
    """Extract the trailing marked JSON response from subprocess stderr."""
    marker_pos = stderr.rfind(_RESPONSE_MARKER)
    if marker_pos == -1:
        return None

    response_json = stderr[marker_pos + len(_RESPONSE_MARKER) :].strip()
    return response_json or None
