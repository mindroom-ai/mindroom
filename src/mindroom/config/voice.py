"""Voice processing configuration models."""

from __future__ import annotations

from typing import Any, Literal, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.model_defaults import OPENAI_TRANSCRIPTION

_RESERVED_SPEECH_OPTION_NAMES = frozenset({"api_key", "base_url", "client", "model"})


def normalize_speech_base_url(host: str | None) -> str | None:
    """Normalize a speech service root to an OpenAI-compatible ``/v1`` URL."""
    normalized = host.strip().rstrip("/") if host else ""
    if not normalized:
        return None
    return normalized if normalized.endswith("/v1") else f"{normalized}/v1"


class SpeechServiceConfig(BaseModel):
    """Configuration for one OpenAI-compatible speech service."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai", "openai_compatible"] = Field(
        default="openai",
        description="Speech provider adapter (OpenAI or an OpenAI-compatible endpoint)",
    )
    model: str = Field(description="Provider speech model name")
    api_key: str | None = Field(default=None, description="Optional service-specific API key")
    host: str | None = Field(default=None, description="Optional service root or /v1 base URL")
    extra_kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="Provider-specific options passed to the speech adapter",
    )

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        """Require explicit HTTP endpoints and reject cloud-fallback-shaped blanks."""
        if value is None:
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if not normalized or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            msg = "Speech host must be a non-empty HTTP(S) URL"
            raise ValueError(msg)
        return normalized

    @field_validator("extra_kwargs")
    @classmethod
    def validate_extra_kwargs(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Keep common connection fields on their typed configuration surface."""
        reserved = sorted(set(value).intersection(_RESERVED_SPEECH_OPTION_NAMES))
        if reserved:
            msg = "Speech extra_kwargs must not redefine: " + ", ".join(reserved)
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_compatible_endpoint(self) -> Self:
        """OpenAI-compatible services need an explicit endpoint."""
        if self.provider == "openai_compatible" and self.host is None:
            msg = "OpenAI-compatible speech services require host"
            raise ValueError(msg)
        return self


class VoiceSTTConfig(SpeechServiceConfig):
    """Voice-message STT configuration with its historical model default."""

    model: str = Field(default=OPENAI_TRANSCRIPTION, description="STT model name")


class _VoiceLLMConfig(BaseModel):
    """Configuration for voice command intelligence."""

    model: str = Field(default="default", description="Model for command recognition")


class VoiceConfig(BaseModel):
    """Configuration for voice message handling."""

    enabled: bool = Field(default=False, description="Enable voice message processing")
    visible_router_echo: bool = Field(
        default=True,
        description="Post the normalized voice transcript or fallback as a visible router message",
    )
    stt: VoiceSTTConfig = Field(
        default_factory=VoiceSTTConfig,
        description="STT configuration",
    )
    intelligence: _VoiceLLMConfig = Field(
        default_factory=_VoiceLLMConfig,
        description="Command intelligence configuration",
    )
