"""Voice processing configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class VoiceSTTConfig(BaseModel):
    """Configuration for voice speech-to-text."""

    provider: str = Field(default="openai", description="STT provider (openai or compatible)")
    model: str = Field(default="whisper-1", description="STT model name")
    api_key: str | None = Field(default=None, description="API key for STT service")
    host: str | None = Field(default=None, description="Host URL for self-hosted STT")


class VoiceLLMConfig(BaseModel):
    """Configuration for voice command intelligence."""

    model: str = Field(default="default", description="Model for command recognition")


class VoiceConfig(BaseModel):
    """Configuration for voice message handling."""

    enabled: bool = Field(default=False, description="Enable voice message processing")
    stt: VoiceSTTConfig = Field(default_factory=VoiceSTTConfig, description="STT configuration")
    intelligence: VoiceLLMConfig = Field(
        default_factory=VoiceLLMConfig,
        description="Command intelligence configuration",
    )
