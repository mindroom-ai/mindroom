"""Typed wire protocol for the Matrix desktop worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, cast

DESKTOP_COMMAND_EVENT_TYPE = "io.mindroom.desktop.command.v1"
DESKTOP_RESPONSE_EVENT_TYPE = "io.mindroom.desktop.response.v1"
DESKTOP_PROTOCOL_VERSION = 1
MAX_COMMAND_TTL_MS = 120_000
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024

type DesktopAction = Literal["status", "screenshot", "click", "type_text", "scroll", "keypress"]

_DESKTOP_ACTIONS = frozenset({"status", "screenshot", "click", "type_text", "scroll", "keypress"})


class DesktopProtocolError(ValueError):
    """One desktop wire payload is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class EncryptedDesktopMedia:
    """One encrypted Matrix media object carried inside an Olm response."""

    url: str
    key: str
    iv: str
    sha256: str
    mime_type: str
    size: int

    def to_content(self) -> dict[str, object]:
        """Serialize using the Matrix encrypted-file shape."""
        return {
            "url": self.url,
            "key": {
                "alg": "A256CTR",
                "ext": True,
                "k": self.key,
                "key_ops": ["encrypt", "decrypt"],
                "kty": "oct",
            },
            "iv": self.iv,
            "hashes": {"sha256": self.sha256},
            "v": "v2",
            "mimetype": self.mime_type,
            "size": self.size,
        }

    @classmethod
    def from_content(cls, raw: object) -> EncryptedDesktopMedia:
        """Parse one strict encrypted-file payload."""
        content = _object_mapping(raw, "screenshot")
        key = _object_mapping(content.get("key"), "screenshot.key")
        hashes = _object_mapping(content.get("hashes"), "screenshot.hashes")
        if key.get("alg") != "A256CTR" or key.get("kty") != "oct" or key.get("ext") is not True:
            msg = "screenshot.key must describe an extractable A256CTR octet key."
            raise DesktopProtocolError(msg)
        url = _required_str(content, "url", "screenshot")
        if not url.startswith("mxc://"):
            msg = "screenshot.url must be an mxc:// URI."
            raise DesktopProtocolError(msg)
        version = _required_str(content, "v", "screenshot")
        if version != "v2":
            msg = "screenshot.v must be v2."
            raise DesktopProtocolError(msg)
        size = _required_int(content, "size", "screenshot")
        if size <= 0 or size > MAX_SCREENSHOT_BYTES:
            msg = f"screenshot.size must be between 1 and {MAX_SCREENSHOT_BYTES}."
            raise DesktopProtocolError(msg)
        mime_type = _required_str(content, "mimetype", "screenshot")
        if mime_type not in {"image/jpeg", "image/png"}:
            msg = "screenshot.mimetype must be image/jpeg or image/png."
            raise DesktopProtocolError(msg)
        return cls(
            url=url,
            key=_required_str(key, "k", "screenshot.key"),
            iv=_required_str(content, "iv", "screenshot"),
            sha256=_required_str(hashes, "sha256", "screenshot.hashes"),
            mime_type=mime_type,
            size=size,
        )


@dataclass(frozen=True, slots=True)
class DesktopCommand:
    """One short-lived desktop action request."""

    request_id: str
    session_id: str
    sequence: int
    issued_at_ms: int
    expires_at_ms: int
    action: DesktopAction
    requester_id: str
    agent_name: str
    parameters: dict[str, object] = field(default_factory=dict)

    def to_content(self) -> dict[str, object]:
        """Serialize this command for Olm delivery."""
        return {
            "v": DESKTOP_PROTOCOL_VERSION,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "sequence": self.sequence,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
            "action": self.action,
            "requester_id": self.requester_id,
            "agent_name": self.agent_name,
            "parameters": dict(self.parameters),
        }

    @classmethod
    def from_content(cls, raw: object) -> DesktopCommand:
        """Parse one strict command payload."""
        content = _object_mapping(raw, "command")
        _require_protocol_version(content)
        action = _required_str(content, "action", "command")
        if action not in _DESKTOP_ACTIONS:
            msg = f"Unsupported desktop action: {action}."
            raise DesktopProtocolError(msg)
        issued_at_ms = _required_int(content, "issued_at_ms", "command")
        expires_at_ms = _required_int(content, "expires_at_ms", "command")
        sequence = _required_int(content, "sequence", "command")
        if sequence < 0:
            msg = "command.sequence must be non-negative."
            raise DesktopProtocolError(msg)
        if expires_at_ms <= issued_at_ms or expires_at_ms - issued_at_ms > MAX_COMMAND_TTL_MS:
            msg = f"Desktop command TTL must be between 1 and {MAX_COMMAND_TTL_MS} milliseconds."
            raise DesktopProtocolError(msg)
        parameters = _object_mapping(content.get("parameters", {}), "command.parameters")
        return cls(
            request_id=_bounded_identifier(content, "request_id", "command"),
            session_id=_bounded_identifier(content, "session_id", "command"),
            sequence=sequence,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            action=cast("DesktopAction", action),
            requester_id=_required_str(content, "requester_id", "command"),
            agent_name=_required_str(content, "agent_name", "command"),
            parameters=parameters,
        )


@dataclass(frozen=True, slots=True)
class DesktopResponse:
    """One correlated desktop command result."""

    request_id: str
    session_id: str
    ok: bool
    result: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    screenshot: EncryptedDesktopMedia | None = None

    def to_content(self) -> dict[str, object]:
        """Serialize this response for Olm delivery."""
        content: dict[str, object] = {
            "v": DESKTOP_PROTOCOL_VERSION,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "ok": self.ok,
            "result": dict(self.result),
        }
        if self.error is not None:
            content["error"] = self.error
        if self.screenshot is not None:
            content["screenshot"] = self.screenshot.to_content()
        return content

    @classmethod
    def from_content(cls, raw: object) -> DesktopResponse:
        """Parse one strict response payload."""
        content = _object_mapping(raw, "response")
        _require_protocol_version(content)
        ok = content.get("ok")
        if not isinstance(ok, bool):
            msg = "response.ok must be a boolean."
            raise DesktopProtocolError(msg)
        error = content.get("error")
        if error is not None and (not isinstance(error, str) or not error.strip()):
            msg = "response.error must be a non-empty string when present."
            raise DesktopProtocolError(msg)
        screenshot_raw = content.get("screenshot")
        if ok and error is not None:
            msg = "Successful desktop responses must not include an error."
            raise DesktopProtocolError(msg)
        if not ok and error is None:
            msg = "Failed desktop responses must include an error."
            raise DesktopProtocolError(msg)
        if not ok and screenshot_raw is not None:
            msg = "Failed desktop responses must not include a screenshot."
            raise DesktopProtocolError(msg)
        return cls(
            request_id=_bounded_identifier(content, "request_id", "response"),
            session_id=_bounded_identifier(content, "session_id", "response"),
            ok=ok,
            result=_object_mapping(content.get("result", {}), "response.result"),
            error=error,
            screenshot=(EncryptedDesktopMedia.from_content(screenshot_raw) if screenshot_raw is not None else None),
        )


def event_content(source: object) -> dict[str, object]:
    """Extract the custom-event content mapping from a decrypted nio source."""
    event = _object_mapping(source, "event")
    return _object_mapping(event.get("content"), "event.content")


def _object_mapping(raw: object, label: str) -> dict[str, object]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        msg = f"{label} must be an object with string keys."
        raise DesktopProtocolError(msg)
    return cast("dict[str, object]", raw).copy()


def _required_str(content: dict[str, object], key: str, label: str) -> str:
    value = content.get(key)
    if not isinstance(value, str) or not value.strip():
        msg = f"{label}.{key} must be a non-empty string."
        raise DesktopProtocolError(msg)
    return value


def _required_int(content: dict[str, object], key: str, label: str) -> int:
    value = content.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        msg = f"{label}.{key} must be an integer."
        raise DesktopProtocolError(msg)
    return value


def _bounded_identifier(content: dict[str, object], key: str, label: str) -> str:
    value = _required_str(content, key, label)
    if len(value) > 128:
        msg = f"{label}.{key} must not exceed 128 characters."
        raise DesktopProtocolError(msg)
    return value


def _require_protocol_version(content: dict[str, object]) -> None:
    version = _required_int(content, "v", "payload")
    if version != DESKTOP_PROTOCOL_VERSION:
        msg = f"Unsupported desktop protocol version: {version}."
        raise DesktopProtocolError(msg)


__all__ = [
    "DESKTOP_COMMAND_EVENT_TYPE",
    "DESKTOP_PROTOCOL_VERSION",
    "DESKTOP_RESPONSE_EVENT_TYPE",
    "MAX_COMMAND_TTL_MS",
    "MAX_SCREENSHOT_BYTES",
    "DesktopAction",
    "DesktopCommand",
    "DesktopProtocolError",
    "DesktopResponse",
    "EncryptedDesktopMedia",
    "event_content",
]
