"""Voice call (MatrixRTC / Element Call) configuration models."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.voice import SpeechServiceConfig  # noqa: TC001 - Pydantic needs the runtime model
from mindroom.credentials import validate_service_name


class RealtimeCallProfile(BaseModel):
    """One OpenAI realtime speech-to-speech call profile."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["realtime"]
    model: str = Field(description="OpenAI realtime speech-to-speech model")
    credentials_service: str = Field(description="Named credential service containing the realtime API key")
    voice: str = Field(description="Realtime model voice preset")

    @field_validator("credentials_service")
    @classmethod
    def _validate_credentials_service(cls, value: str) -> str:
        """Normalize the strict realtime credential binding."""
        return validate_service_name(value)


class CascadedCallProfile(BaseModel):
    """One STT, normal agent turn, and TTS call profile."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["cascaded"]
    model: str | None = Field(default=None, description="Configured LLM alias for cascaded agent turns")
    stt: SpeechServiceConfig = Field(description="Speech-to-text service")
    tts: SpeechServiceConfig = Field(description="Text-to-speech service")


CallProfile = Annotated[RealtimeCallProfile | CascadedCallProfile, Field(discriminator="backend")]


class CallsConfig(BaseModel):
    """MatrixRTC settings, named call profiles, and agent assignments."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable agents joining Element Call voice calls")
    profiles: dict[str, CallProfile] = Field(
        default_factory=dict,
        description="Reusable backend-specific call profiles",
    )
    agents: dict[str, str] = Field(
        default_factory=dict,
        description="Call profile name by agent name (at most one agent per room)",
    )
    livekit_service_url: str | None = Field(
        default=None,
        description="Same-server MatrixRTC authorization service URL override (otherwise discovered from .well-known)",
    )

    def resolve_agent_config(self, agent_name: str) -> CallProfile:
        """Return the explicitly assigned call profile for one agent."""
        return self.profiles[self.agents[agent_name]]

    @model_validator(mode="after")
    def _validate_agent_profiles(self) -> Self:
        """Require every calls-enabled agent to select a defined profile."""
        missing_profiles = sorted({profile for profile in self.agents.values() if profile not in self.profiles})
        if missing_profiles:
            msg = "calls.agents references unknown profile(s): " + ", ".join(missing_profiles)
            raise ValueError(msg)
        return self
