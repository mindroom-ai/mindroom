"""Voice call (MatrixRTC / Element Call) configuration models."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from mindroom.config.voice import SpeechServiceConfig  # noqa: TC001 - Pydantic needs the runtime model
from mindroom.credentials import validate_service_name
from mindroom.model_defaults import OPENAI_REALTIME


class _CallPipelineConfig(BaseModel):
    """Concrete voice pipeline settings shared by defaults and resolved agents."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["realtime", "cascaded"] = Field(
        default="realtime",
        description="Voice backend: OpenAI realtime speech-to-speech or cascaded STT/agent/TTS",
    )
    model: str = Field(
        default=OPENAI_REALTIME,
        description="OpenAI realtime speech-to-speech model used during calls",
    )
    credentials_service: str = Field(
        default="openai",
        description="Credential service containing the API key for OpenAI realtime calls",
    )
    voice: str | None = Field(default=None, description="Realtime model voice preset")
    stt: SpeechServiceConfig | None = Field(default=None, description="Cascaded speech-to-text service")
    tts: SpeechServiceConfig | None = Field(default=None, description="Cascaded text-to-speech service")

    @field_validator("credentials_service")
    @classmethod
    def _validate_credentials_service(cls, value: str) -> str:
        """Normalize the strict credential service binding for realtime calls."""
        return validate_service_name(value)


class CallAgentConfig(BaseModel):
    """Optional per-agent overrides for the default voice pipeline."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["realtime", "cascaded"] | None = Field(
        default=None,
        description="Per-agent voice backend override",
    )
    model: str | None = Field(default=None, description="Per-agent realtime model override")
    credentials_service: str | None = Field(
        default=None,
        description="Per-agent realtime credential service override",
    )
    voice: str | None = Field(
        default=None,
        description="Per-agent realtime voice override (explicit null clears the default)",
    )
    stt: SpeechServiceConfig | None = Field(
        default=None,
        description="Per-agent speech-to-text override (explicit null clears the default)",
    )
    tts: SpeechServiceConfig | None = Field(
        default=None,
        description="Per-agent text-to-speech override (explicit null clears the default)",
    )

    @field_validator("credentials_service")
    @classmethod
    def _validate_credentials_service(cls, value: str | None) -> str | None:
        """Normalize an explicitly authored credential service override."""
        return None if value is None else validate_service_name(value)

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Preserve omitted fields while retaining explicitly authored nulls."""
        data = handler(self)
        return {field_name: value for field_name, value in data.items() if field_name in self.model_fields_set}


class ResolvedCallAgentConfig(_CallPipelineConfig):
    """Effective voice pipeline for one calls-enabled agent."""

    @model_validator(mode="after")
    def validate_cascaded_services(self) -> Self:
        """Require both independently configured speech legs in cascaded mode."""
        if self.backend != "cascaded":
            return self
        missing = [name for name, service in (("stt", self.stt), ("tts", self.tts)) if service is None]
        if missing:
            msg = "Cascaded calls require: " + ", ".join(missing)
            raise ValueError(msg)
        return self


class CallsConfig(_CallPipelineConfig):
    """Global MatrixRTC settings, pipeline defaults, and per-agent overrides."""

    enabled: bool = Field(default=False, description="Enable agents joining Element Call voice calls")
    agents: dict[str, CallAgentConfig] = Field(
        default_factory=dict,
        description="Call configuration by agent name (at most one agent per room)",
    )
    livekit_service_url: str | None = Field(
        default=None,
        description="Same-server MatrixRTC authorization service URL override (otherwise discovered from .well-known)",
    )

    def resolve_agent_config(self, agent_name: str) -> ResolvedCallAgentConfig:
        """Resolve and validate one agent's effective voice pipeline."""
        defaults = {field_name: getattr(self, field_name) for field_name in ResolvedCallAgentConfig.model_fields}
        overrides = self.agents[agent_name].model_dump(exclude_unset=True)
        return ResolvedCallAgentConfig.model_validate(defaults | overrides)

    @model_validator(mode="after")
    def validate_agent_pipelines(self) -> Self:
        """Validate cascaded requirements after applying each agent's overrides."""
        for agent_name in self.agents:
            try:
                self.resolve_agent_config(agent_name)
            except ValidationError as exc:
                msg = f"Invalid effective call configuration for agent {agent_name!r}: {exc}"
                raise ValueError(msg) from exc
        return self
