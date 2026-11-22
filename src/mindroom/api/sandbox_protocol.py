"""Sandbox subprocess payload and response protocol helpers."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

_RESPONSE_MARKER = "__SANDBOX_RESPONSE__"
_TIMING_MARKER = "__SANDBOX_TIMING__"
# A child forked from a preloading template exits with this instead of running a request the preload does not suit.
PRELOAD_UNSUITED_RETURNCODE = 75


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


def response_marker_payload(response_json: str, *, timing: dict[str, object] | None = None) -> str:
    """Prefix one response payload with the stderr marker, after a line of the child's stage timing when given."""
    timing_line = "" if timing is None else f"{_TIMING_MARKER}{json.dumps(timing)}\n"
    return f"{timing_line}{_RESPONSE_MARKER}{response_json}"


def extract_timing(stderr: str) -> dict[str, object] | None:
    """Extract the child's stage timing from the line before the marked response, or None without a whole one."""
    before_response = stderr[: max(stderr.rfind(_RESPONSE_MARKER), 0)]
    _, found, raw_timing = before_response.rpartition(_TIMING_MARKER)
    try:
        timing = json.loads(raw_timing) if found else None
    except ValueError:
        return None
    return timing if isinstance(timing, dict) else None


def extract_response_json(stderr: str) -> str | None:
    """Extract the trailing marked JSON response from subprocess stderr."""
    marker_pos = stderr.rfind(_RESPONSE_MARKER)
    if marker_pos == -1:
        return None

    response_json = stderr[marker_pos + len(_RESPONSE_MARKER) :].strip()
    return response_json or None
