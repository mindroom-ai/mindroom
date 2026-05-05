"""Sandbox subprocess payload and response protocol helpers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

_RESPONSE_MARKER = "__SANDBOX_RESPONSE__"


class SandboxSubprocessEnvelope(BaseModel):
    """Explicit parent-to-child payload for sandbox subprocess execution."""

    request: dict[str, Any] = Field(default_factory=dict)
    runtime_paths: dict[str, Any] = Field(default_factory=dict)
    committed_config: str = ""


def serialize_subprocess_envelope(
    *,
    request: dict[str, Any],
    runtime_paths: dict[str, Any],
    committed_config: str,
) -> str:
    """Serialize the explicit parent-to-child subprocess payload."""
    return SandboxSubprocessEnvelope(
        request=dict(request),
        runtime_paths=dict(runtime_paths),
        committed_config=committed_config,
    ).model_dump_json()


def parse_subprocess_envelope(payload: str) -> SandboxSubprocessEnvelope:
    """Parse the serialized subprocess payload from stdin."""
    return SandboxSubprocessEnvelope.model_validate_json(payload)


def response_marker_payload(response_json: str) -> str:
    """Prefix one response payload with the stderr marker."""
    return _RESPONSE_MARKER + response_json


def extract_response_json(stderr: str) -> str | None:
    """Extract the trailing marked JSON response from subprocess stderr."""
    marker_pos = stderr.rfind(_RESPONSE_MARKER)
    if marker_pos == -1:
        return None

    response_json = stderr[marker_pos + len(_RESPONSE_MARKER) :].strip()
    return response_json or None
