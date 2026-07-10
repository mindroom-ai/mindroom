"""Voice call (MatrixRTC / Element Call) configuration models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mindroom.model_defaults import OPENAI_REALTIME


class CallsConfig(BaseModel):
    """Configuration for agents joining Matrix voice calls."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Enable agents joining Element Call voice calls")
    model: str = Field(
        default=OPENAI_REALTIME,
        description="OpenAI realtime speech-to-speech model used during calls",
    )
    voice: str | None = Field(default=None, description="Realtime model voice preset")
    agents: list[str] = Field(
        default_factory=list,
        description="Agents allowed to join calls in their rooms (at most one per room)",
    )
    livekit_service_url: str | None = Field(
        default=None,
        description="Same-server MatrixRTC authorization service URL override (otherwise discovered from .well-known)",
    )
