"""External trigger policy configuration."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ENV_VAR_NAME_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_GITHUB_REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_EVENT_SOURCE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _strip_non_empty(value: str, *, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        msg = f"{field_name} must be a non-empty string"
        raise ValueError(msg)
    return stripped


def _validate_non_empty_list_items(values: list[str], *, field_name: str) -> list[str]:
    return [_strip_non_empty(item, field_name=field_name) for item in values]


class GitHubEventSourceConfig(BaseModel):
    """Shared GitHub webhook event source configuration."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["github"] = "github"
    secret_env: str = Field(description="Environment variable name holding the GitHub webhook secret")
    allowed_repos: list[str] = Field(description="Allowlisted owner/repo repositories for this source")
    allowed_events: list[str] = Field(
        default_factory=lambda: [
            "pull_request",
            "pull_request_review",
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
        stripped = _strip_non_empty(value, field_name="secret_env")
        if _ENV_VAR_NAME_PATTERN.fullmatch(stripped) is None:
            msg = "secret_env must match ^[A-Z_][A-Z0-9_]*$"
            raise ValueError(msg)
        return stripped

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
        """Reject empty subscriber user IDs."""
        return _validate_non_empty_list_items(values, field_name="subscribers entry")


class Ed25519EventSourceConfig(BaseModel):
    """Shared Ed25519-signed external event source configuration."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["ed25519"] = "ed25519"
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
        """Require a non-empty public key."""
        return _strip_non_empty(value, field_name="public_key")

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
        """Reject empty subscriber user IDs."""
        return _validate_non_empty_list_items(values, field_name="subscribers entry")


ExternalEventSourceConfig = Annotated[
    GitHubEventSourceConfig | Ed25519EventSourceConfig,
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
