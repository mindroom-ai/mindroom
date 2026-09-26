"""Typed wire protocol for the Matrix desktop worker."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Literal, cast

from mindroom.desktop.input import DESKTOP_SAFE_KEYS
from mindroom.matrix.encrypted_file import encrypted_file_content_from_values

DESKTOP_COMMAND_EVENT_TYPE = "io.mindroom.desktop.command.v2"
DESKTOP_RESPONSE_EVENT_TYPE = "io.mindroom.desktop.response.v2"
DESKTOP_PAIRING_CLAIM_EVENT_TYPE = "io.mindroom.desktop.pairing_claim.v1"
DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE = "io.mindroom.desktop.pairing_accepted.v1"
DESKTOP_PROTOCOL_VERSION = 2
MAX_COMMAND_TTL_MS = 120_000
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_SHELL_OUTPUT_BYTES = 10 * 1024 * 1024
SHELL_OUTPUT_MIME_TYPE = "text/plain"
# Measured as ASCII-escaped JSON, the form nio encrypts. Olm framing and base64 then add a third, and
# maximum-length Matrix IDs add about 1.5 KiB of envelope. The rest covers the bridge's metrics and the
# request_status receipt that wraps a stored response.
MAX_INLINE_RESPONSE_BYTES = 40_960
_MAX_COMMAND_PARAMETERS_BYTES = 16 * 1024
_PAIRING_VERIFICATION_HEX_CHARS = 16

type DesktopAction = Literal[
    "status",
    "request_status",
    "list_apps",
    "launch_app",
    "get_app_state",
    "screenshot",
    "click_element",
    "set_value",
    "scroll_element",
    "perform_action",
    "click",
    "double_click",
    "hover",
    "drag",
    "type_text",
    "scroll",
    "keypress",
    "browser_observe",
    "browser_control",
    "list_folders",
    "list_directory",
    "read_file",
    "run_shell",
    "check_shell",
    "kill_shell",
]

DESKTOP_CONTROL_ACTIONS = frozenset(
    {
        "launch_app",
        "click_element",
        "set_value",
        "scroll_element",
        "perform_action",
        "click",
        "double_click",
        "hover",
        "drag",
        "type_text",
        "scroll",
        "keypress",
        "browser_control",
    },
)
DESKTOP_BROWSER_ACTIONS = frozenset({"browser_observe", "browser_control"})
DESKTOP_FILE_ACTIONS = frozenset({"list_folders", "list_directory", "read_file"})
DESKTOP_SHELL_ACTIONS = frozenset({"run_shell", "check_shell", "kill_shell"})
DESKTOP_APP_ACTIONS = frozenset(
    {"get_app_state", "screenshot", *(DESKTOP_CONTROL_ACTIONS - DESKTOP_BROWSER_ACTIONS)},
)
_DESKTOP_ACTIONS = frozenset(
    {
        "status",
        "request_status",
        "list_apps",
        *DESKTOP_APP_ACTIONS,
        *DESKTOP_BROWSER_ACTIONS,
        *DESKTOP_FILE_ACTIONS,
        *DESKTOP_SHELL_ACTIONS,
    },
)


type DesktopObservationMode = Literal["tree", "screenshot", "both"]
type DesktopMediaKind = Literal["screenshot", "output_attachment"]

_MEDIA_MIME_TYPES: dict[DesktopMediaKind, frozenset[str]] = {
    "screenshot": frozenset({"image/jpeg", "image/png"}),
    "output_attachment": frozenset({SHELL_OUTPUT_MIME_TYPE}),
}
_MEDIA_MAX_BYTES: dict[DesktopMediaKind, int] = {
    "screenshot": MAX_SCREENSHOT_BYTES,
    "output_attachment": MAX_SHELL_OUTPUT_BYTES,
}


def desktop_observation_mode(action: str, value: object = "both") -> DesktopObservationMode:
    """Validate explicit native observation choices before any local operation."""
    if not isinstance(value, str) or value not in {"tree", "screenshot", "both"}:
        msg = "Desktop observation must be tree, screenshot, or both."
        raise DesktopProtocolError(msg)
    if value != "both" and action not in DESKTOP_APP_ACTIONS:
        msg = "Desktop observation selection requires an application action."
        raise DesktopProtocolError(msg)
    if action == "screenshot" and value == "tree":
        msg = "The screenshot action requires a screenshot observation."
        raise DesktopProtocolError(msg)
    return cast("DesktopObservationMode", value)


class DesktopProtocolError(ValueError):
    """One desktop wire payload is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class DesktopSetupDescriptor:
    """Copyable setup data requiring local identity confirmation before pairing."""

    homeserver: str
    user_id: str
    code: str
    controller_user_id: str
    controller_device_id: str
    controller_ed25519: str
    requester_id: str
    agent_name: str
    cloudflare_access: bool

    def to_content(self) -> dict[str, object]:
        """Serialize transient setup data, including the short-lived pairing code."""
        return {"v": 1, "kind": "mindroom_desktop_setup", **asdict(self)}

    @classmethod
    def from_content(cls, raw: object) -> DesktopSetupDescriptor:
        """Validate the exact descriptor shape before showing its identity locally."""
        content = _object_mapping(raw, "setup")
        fields = {
            "homeserver",
            "user_id",
            "code",
            "controller_user_id",
            "controller_device_id",
            "controller_ed25519",
            "requester_id",
            "agent_name",
            "cloudflare_access",
        }
        if (
            set(content) != fields | {"v", "kind"}
            or _required_int(content, "v", "setup") != 1
            or content.get("kind") != "mindroom_desktop_setup"
            or not isinstance(content["cloudflare_access"], bool)
        ):
            msg = "Desktop setup descriptor has unsupported fields, version, or type."
            raise DesktopProtocolError(msg)
        return cls(
            homeserver=_bounded_str(content, "homeserver", "setup", max_length=2048),
            user_id=_bounded_str(content, "user_id", "setup", max_length=512),
            code=_bounded_str(content, "code", "setup", max_length=256),
            controller_user_id=_bounded_str(content, "controller_user_id", "setup", max_length=512),
            controller_device_id=_bounded_str(content, "controller_device_id", "setup", max_length=256),
            controller_ed25519=_bounded_str(content, "controller_ed25519", "setup", max_length=256),
            requester_id=_bounded_str(content, "requester_id", "setup", max_length=512),
            agent_name=_bounded_str(content, "agent_name", "setup", max_length=256),
            cloudflare_access=content["cloudflare_access"],
        )


@dataclass(frozen=True, slots=True)
class DesktopPairingClaim:
    """One short-lived pairing token presented by an authenticated local device."""

    token: str

    def to_content(self) -> dict[str, object]:
        """Serialize one pairing claim without duplicating device identity fields."""
        return {"v": DESKTOP_PROTOCOL_VERSION, "token": self.token}

    @classmethod
    def from_content(cls, raw: object) -> DesktopPairingClaim:
        """Parse one strict pairing claim."""
        content = _object_mapping(raw, "pairing claim")
        _require_protocol_version(content)
        return cls(token=_bounded_str(content, "token", "pairing claim", max_length=256))


@dataclass(frozen=True, slots=True)
class DesktopPairingAccepted:
    """Authenticated controller acknowledgement for one claimed pairing token."""

    verification: str

    def to_content(self) -> dict[str, object]:
        """Serialize one acknowledgement without returning the bearer token."""
        return {"v": DESKTOP_PROTOCOL_VERSION, "verification": self.verification}

    @classmethod
    def from_content(cls, raw: object) -> DesktopPairingAccepted:
        """Parse one strict pairing acknowledgement."""
        content = _object_mapping(raw, "pairing acknowledgement")
        _require_protocol_version(content)
        return cls(
            verification=_bounded_str(
                content,
                "verification",
                "pairing acknowledgement",
                max_length=64,
            ),
        )


def desktop_pairing_verification(token: str, device_ed25519: str) -> str:
    """Derive a terminal-visible confirmation bound to one claimed device key."""
    digest = hashlib.sha256(f"{token}\0{device_ed25519}".encode()).hexdigest()
    return digest[:_PAIRING_VERIFICATION_HEX_CHARS].upper()


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
        return encrypted_file_content_from_values(
            url=self.url,
            key=self.key,
            iv=self.iv,
            sha256=self.sha256,
            mime_type=self.mime_type,
            size=self.size,
        )

    @classmethod
    def from_content(cls, raw: object, *, kind: DesktopMediaKind = "screenshot") -> EncryptedDesktopMedia:
        """Parse one strict encrypted-file payload of the media kind expected in its response field."""
        content = _object_mapping(raw, kind)
        key = _object_mapping(content.get("key"), f"{kind}.key")
        hashes = _object_mapping(content.get("hashes"), f"{kind}.hashes")
        if key.get("alg") != "A256CTR" or key.get("kty") != "oct" or key.get("ext") is not True:
            msg = f"{kind}.key must describe an extractable A256CTR octet key."
            raise DesktopProtocolError(msg)
        url = _required_str(content, "url", kind)
        if not url.startswith("mxc://"):
            msg = f"{kind}.url must be an mxc:// URI."
            raise DesktopProtocolError(msg)
        version = _required_str(content, "v", kind)
        if version != "v2":
            msg = f"{kind}.v must be v2."
            raise DesktopProtocolError(msg)
        size = _required_int(content, "size", kind)
        if size <= 0 or size > _MEDIA_MAX_BYTES[kind]:
            msg = f"{kind}.size must be between 1 and {_MEDIA_MAX_BYTES[kind]}."
            raise DesktopProtocolError(msg)
        mime_type = _required_str(content, "mimetype", kind)
        if mime_type not in _MEDIA_MIME_TYPES[kind]:
            msg = f"{kind}.mimetype must be {' or '.join(sorted(_MEDIA_MIME_TYPES[kind]))}."
            raise DesktopProtocolError(msg)
        return cls(
            url=url,
            key=_required_str(key, "k", f"{kind}.key"),
            iv=_required_str(content, "iv", kind),
            sha256=_required_str(hashes, "sha256", f"{kind}.hashes"),
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
        try:
            encoded_parameters = json.dumps(
                parameters,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        except (TypeError, ValueError) as exc:
            msg = "command.parameters must contain finite JSON values."
            raise DesktopProtocolError(msg) from exc
        if len(encoded_parameters) > _MAX_COMMAND_PARAMETERS_BYTES:
            msg = f"command.parameters must not exceed {_MAX_COMMAND_PARAMETERS_BYTES} encoded bytes."
            raise DesktopProtocolError(msg)
        return cls(
            request_id=_bounded_identifier(content, "request_id", "command"),
            session_id=_bounded_identifier(content, "session_id", "command"),
            sequence=sequence,
            issued_at_ms=issued_at_ms,
            expires_at_ms=expires_at_ms,
            action=cast("DesktopAction", action),
            requester_id=_bounded_str(content, "requester_id", "command", max_length=255),
            agent_name=_bounded_str(content, "agent_name", "command", max_length=128),
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

    def content_bytes(self) -> int:
        """Return this response's size inside the Olm plaintext, serialized as nio's ``Api.to_json`` does."""
        return len(json.dumps(self.to_content(), separators=(",", ":")).encode())

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
    return _bounded_str(content, key, label, max_length=128)


def _bounded_str(content: dict[str, object], key: str, label: str, *, max_length: int) -> str:
    value = _required_str(content, key, label)
    if len(value) > max_length:
        msg = f"{label}.{key} must not exceed {max_length} characters."
        raise DesktopProtocolError(msg)
    return value


def _require_protocol_version(content: dict[str, object]) -> None:
    version = _required_int(content, "v", "payload")
    if version != DESKTOP_PROTOCOL_VERSION:
        msg = f"Unsupported desktop protocol version: {version}."
        raise DesktopProtocolError(msg)


__all__ = [
    "DESKTOP_APP_ACTIONS",
    "DESKTOP_BROWSER_ACTIONS",
    "DESKTOP_COMMAND_EVENT_TYPE",
    "DESKTOP_CONTROL_ACTIONS",
    "DESKTOP_FILE_ACTIONS",
    "DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE",
    "DESKTOP_PAIRING_CLAIM_EVENT_TYPE",
    "DESKTOP_PROTOCOL_VERSION",
    "DESKTOP_RESPONSE_EVENT_TYPE",
    "DESKTOP_SAFE_KEYS",
    "DESKTOP_SHELL_ACTIONS",
    "MAX_COMMAND_TTL_MS",
    "MAX_INLINE_RESPONSE_BYTES",
    "MAX_SCREENSHOT_BYTES",
    "MAX_SHELL_OUTPUT_BYTES",
    "SHELL_OUTPUT_MIME_TYPE",
    "DesktopAction",
    "DesktopCommand",
    "DesktopMediaKind",
    "DesktopObservationMode",
    "DesktopPairingAccepted",
    "DesktopPairingClaim",
    "DesktopProtocolError",
    "DesktopResponse",
    "DesktopSetupDescriptor",
    "EncryptedDesktopMedia",
    "desktop_observation_mode",
    "desktop_pairing_verification",
    "event_content",
]
