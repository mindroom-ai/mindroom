"""Voice call (MatrixRTC / Element Call) configuration models."""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from mindroom.config.schema_hints import dashboard_hint
from mindroom.config.validation import CredentialServiceName  # noqa: TC001
from mindroom.config.voice import SpeechServiceConfig  # noqa: TC001 - Pydantic needs the runtime model

REALTIME_SPEAKING_RATE_BOUNDS: tuple[float, float] = (0.25, 1.5)
_CASCADED_SPEAKING_RATE_BOUNDS: tuple[float, float] = (0.25, 4.0)
_NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class _CallProfileBase(BaseModel):
    """Shared validation for voice-call profiles."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("speed", mode="before", check_fields=False)
    @classmethod
    def _validate_speed_type(cls, value: object) -> object:
        if isinstance(value, bool):
            msg = "Speaking speed must be a number, not a boolean"
            raise ValueError(msg)  # noqa: TRY004 - keep Pydantic validation errors structured
        return value

    @property
    def speaking_rate_bounds(self) -> tuple[float, float]:
        """Return the valid speaking rate range for this profile."""
        return REALTIME_SPEAKING_RATE_BOUNDS


class LiveCustomVoice(BaseModel):
    """Reference to an OpenAI prompt-to-voice custom voice."""

    model_config = ConfigDict(extra="forbid")

    id: _NonBlankStr = Field(description="OpenAI custom voice ID, such as voice_...")


class RealtimeCallProfile(_CallProfileBase):
    """One OpenAI realtime speech-to-speech call profile."""

    backend: Literal["realtime"] = Field(description="OpenAI realtime speech-to-speech session")
    model: str = Field(description="OpenAI realtime speech-to-speech model")
    credentials_service: CredentialServiceName = Field(
        description="Named credential service containing the realtime API key",
    )
    voice: str = Field(description="Realtime model voice preset")
    speed: float | None = Field(
        default=None,
        ge=REALTIME_SPEAKING_RATE_BOUNDS[0],
        le=REALTIME_SPEAKING_RATE_BOUNDS[1],
        allow_inf_nan=False,
        description="Initial speaking rate multiplier (0.25 to 1.5; default 1.0)",
    )


class LiveCallProfile(_CallProfileBase):
    """One OpenAI Live voice session delegating tasks to a normal agent."""

    backend: Literal["live"] = Field(description="OpenAI Live voice session that delegates tasks to the agent")
    model: str = Field(description="OpenAI Live speech-to-speech model")
    credentials_service: CredentialServiceName = Field(
        description="Named credential service containing the Live API key",
    )
    voice: str | LiveCustomVoice = Field(
        description="Live model voice preset name or custom voice reference ({id: 'voice_...'})",
    )
    speed: float | None = Field(
        default=None,
        ge=REALTIME_SPEAKING_RATE_BOUNDS[0],
        le=REALTIME_SPEAKING_RATE_BOUNDS[1],
        allow_inf_nan=False,
        description="Initial speaking rate multiplier (0.25 to 1.5; default 1.0)",
    )
    agent_model: str | None = Field(
        default=None,
        description="Configured LLM alias for delegated agent turns",
        json_schema_extra=dashboard_hint(reference="model"),
    )


class CascadedCallProfile(_CallProfileBase):
    """One STT, normal agent turn, and TTS call profile."""

    backend: Literal["cascaded"] = Field(description="Speech-to-text, a normal agent turn, then text-to-speech")
    model: str | None = Field(
        default=None,
        description="Configured LLM alias for cascaded agent turns",
        json_schema_extra=dashboard_hint(reference="model"),
    )
    stt: SpeechServiceConfig = Field(description="Speech-to-text service")
    tts: SpeechServiceConfig = Field(description="Text-to-speech service")
    speed: float | None = Field(
        default=None,
        ge=_CASCADED_SPEAKING_RATE_BOUNDS[0],
        le=_CASCADED_SPEAKING_RATE_BOUNDS[1],
        allow_inf_nan=False,
        description="Initial speaking rate multiplier (0.25 to 4.0, or 0.25 to 1.5 for gpt-realtime TTS; default 1.0)",
    )

    @property
    def speaking_rate_bounds(self) -> tuple[float, float]:
        """Return the valid speaking rate range for this profile's TTS model."""
        if self.tts.model.startswith("gpt-realtime"):
            return REALTIME_SPEAKING_RATE_BOUNDS
        return _CASCADED_SPEAKING_RATE_BOUNDS

    @model_validator(mode="after")
    def _validate_speed(self) -> Self:
        """Reject duplicate speed config and enforce gpt-realtime TTS bounds."""
        has_extra_speed = "speed" in self.tts.extra_kwargs
        if self.speed is not None and has_extra_speed:
            msg = "Configure speed on either the cascaded profile or tts.extra_kwargs, not both"
            raise ValueError(msg)
        if self.tts.model.startswith("gpt-realtime"):
            min_rate, max_rate = REALTIME_SPEAKING_RATE_BOUNDS
            if self.speed is not None and not (min_rate <= self.speed <= max_rate):
                msg = f"gpt-realtime TTS speed must be between {min_rate:g} and {max_rate:g}"
                raise ValueError(msg)
            if has_extra_speed:
                raw_speed = self.tts.extra_kwargs["speed"]
                if (
                    isinstance(raw_speed, bool)
                    or not isinstance(raw_speed, (int, float))
                    or not math.isfinite(raw_speed)
                    or not (min_rate <= raw_speed <= max_rate)
                ):
                    msg = (
                        f"gpt-realtime TTS extra_kwargs.speed must be a finite number "
                        f"between {min_rate:g} and {max_rate:g}"
                    )
                    raise ValueError(msg)
        return self


CallProfile = Annotated[RealtimeCallProfile | LiveCallProfile | CascadedCallProfile, Field(discriminator="backend")]


class CallTranscriptsConfig(BaseModel):
    """Transcripts of calls between people, with notes when the call ends."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Transcribe calls between people")
    notes_model: str | None = Field(
        default=None,
        description="Configured LLM alias that writes each call's notes (none writes no notes)",
        json_schema_extra=dashboard_hint(reference="model"),
    )
    languages: list[_NonBlankStr] = Field(
        default_factory=list,
        description="Language codes hinted to speech-to-text (the first is sent)",
    )


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
        description=(
            "Call profile name by agent name, or by personal_agents template key for every user's agent from that "
            "template; direct room calls auto-join only when one calls-enabled agent owns the room"
        ),
        json_schema_extra=dashboard_hint(key_reference="personal_room_agent"),
    )
    livekit_service_url: str | None = Field(
        default=None,
        description="Same-server MatrixRTC authorization service URL override (otherwise discovered from .well-known)",
    )
    transcripts: CallTranscriptsConfig = Field(
        default_factory=CallTranscriptsConfig,
        description="Transcripts of calls between people in rooms with a MindRoom account",
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
