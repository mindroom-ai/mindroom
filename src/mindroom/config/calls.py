"""Voice call (MatrixRTC / Element Call) configuration models."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.voice import SpeechServiceConfig  # noqa: TC001 - Pydantic needs the runtime model
from mindroom.credentials import validate_service_name
from mindroom.model_defaults import OPENAI_REALTIME


class CallsConfig(BaseModel):
    """Configuration for agents joining Matrix voice calls."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable agents joining Element Call voice calls")
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
    agents: list[str] = Field(
        default_factory=list,
        description="Agents allowed to join calls in their rooms (at most one per room)",
    )
    livekit_service_url: str | None = Field(
        default=None,
        description="Same-server MatrixRTC authorization service URL override (otherwise discovered from .well-known)",
    )

    @field_validator("credentials_service")
    @classmethod
    def _validate_credentials_service(cls, value: str) -> str:
        """Normalize the strict credential service binding for realtime calls."""
        return validate_service_name(value)

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
