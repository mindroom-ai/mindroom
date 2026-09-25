"""Bounded local NDJSON protocol used by the native macOS host."""

# Protocol errors intentionally carry stable inline user-facing wire messages.
# ruff: noqa: EM101

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Mapping

NATIVE_PROTOCOL_VERSION = 1
MAX_NATIVE_INPUT_BYTES = 65_536
MAX_NATIVE_OUTPUT_BYTES = 262_144
NATIVE_ACTIONS = frozenset(
    {
        "status",
        "configure",
        "set_allowed_apps",
        "set_browser_config",
        "set_local_access",
        "finish_setup",
        "import_setup",
        "login",
        "pair",
        "start",
        "stop",
        "grant_control",
        "revoke_control",
        "reset_emergency_stop",
        "decide_shell",
        "grant_shell",
        "revoke_shell",
        "request_permission",
        "browser_connect",
        "browser_disconnect",
    },
)
_REQUEST_KEYS = frozenset({"v", "request_id", "action", "parameters"})


class NativeProtocolError(ValueError):
    """One local native-host message is invalid or cannot be handled."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recovery: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recovery = recovery
        self.retryable = retryable

    def to_payload(self) -> dict[str, object]:
        """Return the stable error object used on the wire."""
        return {
            "code": self.code,
            "message": str(self),
            "recovery": self.recovery,
            "retryable": self.retryable,
        }


@dataclass(frozen=True, slots=True)
class NativeRequest:
    """One validated native-host request."""

    request_id: str
    action: str
    parameters: dict[str, object]


def parse_native_request(line: bytes) -> NativeRequest:
    """Parse one bounded, strict request record without retaining input bytes."""
    if len(line) > MAX_NATIVE_INPUT_BYTES:
        raise NativeProtocolError(
            "request_too_large",
            "Native desktop requests must be at most 65,536 bytes.",
        )
    try:
        raw = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeProtocolError("invalid_json", "Native desktop request is not valid UTF-8 JSON.") from exc
    if not isinstance(raw, dict):
        raise NativeProtocolError("invalid_request", "Native desktop request must be a JSON object.")
    payload = cast("dict[str, object]", raw)
    unknown = set(payload) - _REQUEST_KEYS
    if unknown:
        raise NativeProtocolError("invalid_request", "Native desktop request has unsupported fields.")
    version = payload.get("v")
    if type(version) is not int or version != NATIVE_PROTOCOL_VERSION:
        raise NativeProtocolError("invalid_request", "Native desktop protocol version is unsupported.")
    request_id = payload.get("request_id")
    if not isinstance(request_id, str):
        raise NativeProtocolError("invalid_request", "Native desktop request_id must be a UUID.")
    try:
        UUID(request_id)
    except ValueError as exc:
        raise NativeProtocolError("invalid_request", "Native desktop request_id must be a UUID.") from exc
    action = payload.get("action")
    if not isinstance(action, str) or action not in NATIVE_ACTIONS:
        raise NativeProtocolError("invalid_request", "Native desktop action is unsupported.")
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict) or any(not isinstance(key, str) for key in parameters):
        raise NativeProtocolError("invalid_request", "Native desktop parameters must be a JSON object.")
    return NativeRequest(request_id=request_id, action=action, parameters=cast("dict[str, object]", parameters))


def encode_native_message(payload: Mapping[str, object]) -> bytes:
    """Encode one bounded compact NDJSON message."""
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise NativeProtocolError("internal_error", "Native desktop response could not be encoded.") from exc
    if len(encoded) > MAX_NATIVE_OUTPUT_BYTES:
        raise NativeProtocolError("internal_error", "Native desktop response exceeded its output limit.")
    return encoded


__all__ = [
    "MAX_NATIVE_INPUT_BYTES",
    "MAX_NATIVE_OUTPUT_BYTES",
    "NATIVE_ACTIONS",
    "NATIVE_PROTOCOL_VERSION",
    "NativeProtocolError",
    "NativeRequest",
    "encode_native_message",
    "parse_native_request",
]
