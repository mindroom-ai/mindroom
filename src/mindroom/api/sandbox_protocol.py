"""Sandbox subprocess payload and response protocol helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

RESPONSE_MARKER = "__SANDBOX_RESPONSE__"


class SandboxSubprocessEnvelope(BaseModel):
    """Explicit parent-to-child payload for sandbox subprocess execution."""

    request: dict[str, Any] = Field(default_factory=dict)
    runtime_paths: dict[str, Any] = Field(default_factory=dict)


def serialize_subprocess_envelope(
    *,
    request: dict[str, Any],
    runtime_paths: dict[str, Any],
) -> str:
    """Serialize the explicit parent-to-child subprocess payload."""
    return SandboxSubprocessEnvelope(
        request=dict(request),
        runtime_paths=dict(runtime_paths),
    ).model_dump_json()


def parse_subprocess_envelope(payload: str) -> SandboxSubprocessEnvelope:
    """Parse the serialized subprocess payload from stdin."""
    return SandboxSubprocessEnvelope.model_validate_json(payload)


def response_marker_payload(response_json: str) -> str:
    """Prefix one response payload with the stderr marker."""
    return RESPONSE_MARKER + response_json


def extract_response_json(stderr: str) -> str | None:
    """Extract the trailing marked JSON response from subprocess stderr."""
    marker_pos = stderr.rfind(RESPONSE_MARKER)
    if marker_pos == -1:
        return None

    response_json = stderr[marker_pos + len(RESPONSE_MARKER) :].strip()
    return response_json or None
