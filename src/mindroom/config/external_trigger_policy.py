"""External trigger policy configuration."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_GITHUB_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_EVENT_SOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HTTP_HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_SERVER_DNS_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9-]{1,63}$")
_SERVER_IPV6_LITERAL_PATTERN = re.compile(r"^[0-9A-Fa-f:.]{2,45}$")
_ED25519_PUBLIC_KEY_BYTES = 32


def _strip_non_empty(value: str, *, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        msg = f"{field_name} must be a non-empty string"
        raise ValueError(msg)
    return stripped


def _validate_non_empty_list_items(values: list[str], *, field_name: str) -> list[str]:
    return [_strip_non_empty(item, field_name=field_name) for item in values]


def _validate_secret_env_name(value: str) -> str:
    stripped = _strip_non_empty(value, field_name="secret_env")
    if _ENV_VAR_NAME_PATTERN.fullmatch(stripped) is None:
        msg = "secret_env must match ^[A-Z_][A-Z0-9_]*$"
        raise ValueError(msg)
    return stripped


def _validate_header_name(value: str, *, field_name: str) -> str:
    stripped = _strip_non_empty(value, field_name=field_name)
    if _HTTP_HEADER_NAME_PATTERN.fullmatch(stripped) is None:
        msg = f"{field_name} must be a valid ASCII HTTP header name without whitespace or colons"
        raise ValueError(msg)
    return stripped


def _is_valid_port(port: str) -> bool:
    return 1 <= len(port) <= 5 and port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535


def _is_valid_unbracketed_server_host(host: str) -> bool:
    if not host or len(host) > 255:
        return False
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        return all(_SERVER_DNS_LABEL_PATTERN.fullmatch(label) is not None for label in host.split("."))
    return True


def _is_valid_bracketed_ipv6_server_name(server_name: str) -> bool:
    closing_bracket_index = server_name.find("]")
    if closing_bracket_index == -1:
        return False
    ipv6_literal = server_name[1:closing_bracket_index]
    remainder = server_name[closing_bracket_index + 1 :]
    if remainder and (not remainder.startswith(":") or not _is_valid_port(remainder[1:])):
        return False
    if _SERVER_IPV6_LITERAL_PATTERN.fullmatch(ipv6_literal) is None:
        return False
    try:
        ipaddress.IPv6Address(ipv6_literal)
    except ValueError:
        return False
    return True


def _is_valid_matrix_server_name(server_name: str) -> bool:
    if not server_name:
        return False
    if server_name.startswith("["):
        return _is_valid_bracketed_ipv6_server_name(server_name)
    if ":" in server_name:
        host, port = server_name.rsplit(":", 1)
        return _is_valid_unbracketed_server_host(host) and _is_valid_port(port)
    return _is_valid_unbracketed_server_host(server_name)


def _is_valid_matrix_user_id(user_id: str) -> bool:
    if not user_id.startswith("@") or ":" not in user_id:
        return False
    localpart, server_name = user_id[1:].split(":", 1)
    if not localpart or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in localpart):
        return False
    return _is_valid_matrix_server_name(server_name)


def _validate_matrix_user_ids(values: list[str], *, field_name: str) -> list[str]:
    normalized: list[str] = []
    for raw in values:
        user_id = _strip_non_empty(raw, field_name=field_name)
        if not _is_valid_matrix_user_id(user_id):
            msg = f"Invalid Matrix user ID in {field_name}: {raw!r}"
            raise ValueError(msg)
        normalized.append(user_id)
    return normalized


def _decode_strict_base64(value: str) -> bytes | None:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _raw_ed25519_public_key_bytes(key: object) -> bytes | None:
    # Deferred: cryptography's SSH serialization loads bcrypt, which slim config entry points must not load.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: PLC0415
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: PLC0415

    if not isinstance(key, Ed25519PublicKey):
        return None
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _public_key_bytes_from_base64(value: str) -> bytes | None:
    decoded = _decode_strict_base64(value)
    return decoded if decoded is not None and len(decoded) == _ED25519_PUBLIC_KEY_BYTES else None


def _public_key_bytes_from_openssh(value: str) -> bytes | None:
    parts = value.split()
    if len(parts) >= 2 and parts[0] == "ssh-ed25519":
        encoded_key = value.encode("utf-8")
    elif len(parts) == 1 and _decode_strict_base64(value) is not None:
        encoded_key = f"ssh-ed25519 {value}".encode()
    else:
        return None
    # Deferred: cryptography's SSH serialization loads bcrypt, which slim config entry points must not load.
    from cryptography.exceptions import UnsupportedAlgorithm  # noqa: PLC0415
    from cryptography.hazmat.primitives.serialization import load_ssh_public_key  # noqa: PLC0415

    try:
        key = load_ssh_public_key(encoded_key)
    except (ValueError, UnsupportedAlgorithm):
        return None
    return _raw_ed25519_public_key_bytes(key)


def _public_key_bytes_from_pem(value: str) -> bytes | None:
    # Deferred: keep cryptography out of top-level config imports for slim entry points.
    from cryptography.exceptions import UnsupportedAlgorithm  # noqa: PLC0415
    from cryptography.hazmat.primitives.serialization import load_pem_public_key  # noqa: PLC0415

    try:
        key = load_pem_public_key(value.encode("utf-8"))
    except (ValueError, UnsupportedAlgorithm):
        return None
    return _raw_ed25519_public_key_bytes(key)


def normalize_ed25519_public_key(value: str) -> tuple[str, bytes]:
    """Normalize raw base64, OpenSSH, or PEM Ed25519 public keys to (raw_base64, raw_32_bytes)."""
    normalized_input = _strip_non_empty(value, field_name="public_key")
    public_key_bytes = (
        _public_key_bytes_from_pem(normalized_input)
        or _public_key_bytes_from_openssh(normalized_input)
        or _public_key_bytes_from_base64(normalized_input)
    )
    if public_key_bytes is None:
        msg = (
            "public_key must be an Ed25519 public key as raw base64 (32 key bytes), "
            "OpenSSH single-line format ('ssh-ed25519 <base64> [comment]'), or PEM SubjectPublicKeyInfo"
        )
        raise ValueError(msg)
    return base64.b64encode(public_key_bytes).decode("ascii"), public_key_bytes


class GitHubEventSourceConfig(BaseModel):
    """Shared GitHub webhook event source configuration."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["github"] = Field(default="github", description="Event source type for GitHub webhooks")
    secret_env: str = Field(description="Environment variable name holding the GitHub webhook secret")
    allowed_repos: list[str] = Field(description="Allowlisted owner/repo repositories for this source")
    allowed_events: list[str] = Field(
        default_factory=lambda: [
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "issue_comment",
            "check_run",
            "check_suite",
            "release",
            "issues",
            "push",
        ],
        description="Allowlisted GitHub X-GitHub-Event names",
    )
    subscribers: list[str] = Field(
        default_factory=list,
        description="Matrix user IDs allowed to subscribe reactive triggers to this source",
    )
    max_body_bytes: int = Field(
        default=262144,
        ge=1024,
        le=1048576,
        description="Maximum accepted webhook body size in bytes",
    )

    @field_validator("secret_env")
    @classmethod
    def validate_secret_env(cls, value: str) -> str:
        """Require a valid uppercase environment variable name, never a secret value."""
        return _validate_secret_env_name(value)

    @field_validator("allowed_repos")
    @classmethod
    def validate_allowed_repos(cls, values: list[str]) -> list[str]:
        """Require a non-empty allowlist of well-formed owner/repo entries."""
        if not values:
            msg = "allowed_repos must not be empty"
            raise ValueError(msg)
        normalized: list[str] = []
        for raw in values:
            repo = _strip_non_empty(raw, field_name="allowed_repos entry")
            if _GITHUB_REPO_PATTERN.fullmatch(repo) is None:
                msg = f"Invalid GitHub repository specification: {raw!r}"
                raise ValueError(msg)
            owner, name = repo.split("/", 1)
            if owner in {".", ".."} or name in {".", ".."}:
                msg = f"Invalid GitHub repository specification: {raw!r}"
                raise ValueError(msg)
            normalized.append(repo)
        return normalized

    @field_validator("allowed_events")
    @classmethod
    def validate_allowed_events(cls, values: list[str]) -> list[str]:
        """Reject empty GitHub event names."""
        return _validate_non_empty_list_items(values, field_name="allowed_events entry")

    @field_validator("subscribers")
    @classmethod
    def validate_subscribers(cls, values: list[str]) -> list[str]:
        """Require valid Matrix user IDs for subscribers."""
        return _validate_matrix_user_ids(values, field_name="subscribers entry")


class Ed25519EventSourceConfig(BaseModel):
    """Shared Ed25519-signed external event source configuration."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["ed25519"] = Field(default="ed25519", description="Event source type for Ed25519-signed events")
    public_key: str = Field(description="Ed25519 public key used to verify signed events")
    key_id: str = Field(
        default="default",
        min_length=1,
        max_length=128,
        description="Expected signing key identifier",
    )
    allowed_kinds: list[str] = Field(
        default_factory=list,
        description="Optional allowlist of accepted event kinds",
    )
    subscribers: list[str] = Field(
        default_factory=list,
        description="Matrix user IDs allowed to subscribe reactive triggers to this source",
    )
    replay_window_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Maximum accepted signature age in seconds",
    )
    max_body_bytes: int = Field(
        default=65536,
        ge=1024,
        le=262144,
        description="Maximum accepted request body size in bytes",
    )

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        """Validate and normalize an Ed25519 public key (raw base64, OpenSSH, or PEM) to raw base64."""
        return normalize_ed25519_public_key(value)[0]

    @field_validator("key_id")
    @classmethod
    def validate_key_id(cls, value: str) -> str:
        """Require a non-empty stripped key_id."""
        return _strip_non_empty(value, field_name="key_id")

    @field_validator("allowed_kinds")
    @classmethod
    def validate_allowed_kinds(cls, values: list[str]) -> list[str]:
        """Reject empty allowed_kinds entries."""
        return _validate_non_empty_list_items(values, field_name="allowed_kinds entry")

    @field_validator("subscribers")
    @classmethod
    def validate_subscribers(cls, values: list[str]) -> list[str]:
        """Require valid Matrix user IDs for subscribers."""
        return _validate_matrix_user_ids(values, field_name="subscribers entry")


class HmacEventSourceConfig(BaseModel):
    """Generic HMAC-SHA256 webhook event source configuration."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["hmac"] = Field(default="hmac", description="Event source type for generic HMAC-SHA256 webhooks")
    secret_env: str = Field(description="Environment variable name holding the HMAC webhook secret")
    signature_header: str = Field(
        default="X-Webhook-Signature-256",
        description="HTTP header name carrying the HMAC-SHA256 signature",
    )
    signature_prefix: str = Field(
        default="sha256=",
        description="Optional prefix before the hex digest; empty string means bare hex",
    )
    kind_header: str | None = Field(
        default=None,
        description="Optional HTTP header name carrying the event kind",
    )
    kind_field: str = Field(
        default="kind",
        description="JSON field or dot-path carrying the event kind when kind_header is unset",
    )
    event_id_header: str | None = Field(
        default=None,
        description="Optional HTTP header name carrying a deduplication event ID",
    )
    allowed_kinds: list[str] = Field(
        default_factory=list,
        description="Optional allowlist of accepted event kinds",
    )
    subscribers: list[str] = Field(
        default_factory=list,
        description="Matrix user IDs allowed to subscribe reactive triggers to this source",
    )
    max_body_bytes: int = Field(
        default=262144,
        ge=1024,
        le=1048576,
        description="Maximum accepted webhook body size in bytes",
    )

    @field_validator("secret_env")
    @classmethod
    def validate_secret_env(cls, value: str) -> str:
        """Require a valid uppercase environment variable name, never a secret value."""
        return _validate_secret_env_name(value)

    @field_validator("signature_header")
    @classmethod
    def validate_signature_header(cls, value: str) -> str:
        """Require a valid ASCII HTTP header name."""
        return _validate_header_name(value, field_name="signature_header")

    @field_validator("kind_header")
    @classmethod
    def validate_kind_header(cls, value: str | None) -> str | None:
        """Require a valid ASCII HTTP header name when specified."""
        return None if value is None else _validate_header_name(value, field_name="kind_header")

    @field_validator("event_id_header")
    @classmethod
    def validate_event_id_header(cls, value: str | None) -> str | None:
        """Require a valid ASCII HTTP header name when specified."""
        return None if value is None else _validate_header_name(value, field_name="event_id_header")

    @field_validator("kind_field")
    @classmethod
    def validate_kind_field(cls, value: str) -> str:
        """Require a non-empty JSON field name or dot-path with non-empty segments."""
        stripped = _strip_non_empty(value, field_name="kind_field")
        if any(not segment.strip() for segment in stripped.split(".")):
            msg = "kind_field must not contain empty path segments"
            raise ValueError(msg)
        return stripped

    @field_validator("allowed_kinds")
    @classmethod
    def validate_allowed_kinds(cls, values: list[str]) -> list[str]:
        """Reject empty allowed_kinds entries."""
        return _validate_non_empty_list_items(values, field_name="allowed_kinds entry")

    @field_validator("subscribers")
    @classmethod
    def validate_subscribers(cls, values: list[str]) -> list[str]:
        """Require valid Matrix user IDs for subscribers."""
        return _validate_matrix_user_ids(values, field_name="subscribers entry")


ExternalEventSourceConfig = Annotated[
    GitHubEventSourceConfig | Ed25519EventSourceConfig | HmacEventSourceConfig,
    Field(discriminator="kind"),
]


class ExternalTriggerPolicyConfig(BaseModel):
    """Global policy for tool-managed external trigger records."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Whether the external trigger API is enabled")
    default_replay_window_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Default accepted signature age for newly created signed triggers",
    )
    max_replay_window_seconds: int = Field(
        default=3600,
        ge=30,
        le=3600,
        description="Maximum accepted signature age any signed trigger may request",
    )
    default_max_body_bytes: int = Field(
        default=65536,
        ge=1024,
        le=262144,
        description="Default request body size limit for newly created triggers",
    )
    max_body_bytes: int = Field(
        default=262144,
        ge=1024,
        le=262144,
        description="Maximum request body size any trigger may request",
    )
    max_triggers_per_owner: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="Maximum trigger records one owner may create",
    )
    admin_users: list[str] = Field(
        default_factory=list,
        description="Matrix users allowed to manage external triggers for other owners",
    )
    reactive_enabled: bool = Field(default=True, description="Whether reactive triggers are enabled")
    max_reactive_triggers_per_owner: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Maximum reactive trigger records one owner may create",
    )
    max_fires_per_hour: int = Field(
        default=60,
        ge=1,
        le=600,
        description="Maximum allowed fires per hour cap for reactive triggers",
    )
    event_sources: dict[str, ExternalEventSourceConfig] = Field(
        default_factory=dict,
        description="Named shared external event sources keyed by source_id",
    )
    github_pr_polling_enabled: bool = Field(default=True, description="Enable polling for github_pr watches")
    min_github_pr_poll_interval_seconds: int = Field(default=60, ge=30, le=3600, description="Min PR poll interval (s)")
    max_github_pr_watches_per_owner: int = Field(default=20, ge=1, le=200, description="Max github_pr watches/owner")

    @field_validator("event_sources")
    @classmethod
    def validate_event_source_keys(
        cls,
        values: dict[str, ExternalEventSourceConfig],
    ) -> dict[str, ExternalEventSourceConfig]:
        """Require route-safe ASCII source_id keys matching ^[A-Za-z0-9_-]{1,64}$."""
        for key in values:
            if _EVENT_SOURCE_KEY_PATTERN.fullmatch(key) is None:
                msg = f"Invalid event_sources key {key!r}: must match ^[A-Za-z0-9_-]{{1,64}}$"
                raise ValueError(msg)
        return values

    @model_validator(mode="after")
    def validate_defaults_fit_caps(self) -> ExternalTriggerPolicyConfig:
        """Ensure defaults never exceed policy caps."""
        if self.default_replay_window_seconds > self.max_replay_window_seconds:
            msg = "default_replay_window_seconds must not exceed max_replay_window_seconds"
            raise ValueError(msg)
        if self.default_max_body_bytes > self.max_body_bytes:
            msg = "default_max_body_bytes must not exceed max_body_bytes"
            raise ValueError(msg)
        return self
