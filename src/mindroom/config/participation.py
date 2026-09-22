"""Opt-in participation in conversations between multiple humans."""

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.judgment import JudgmentConfig


class RoomParticipationConfig(BaseModel):
    """Pause and judgment settings for existing agents in one room's threads."""

    model_config = ConfigDict(extra="forbid")

    debounce_seconds: float = Field(default=3.0, ge=0.0, le=30.0, allow_inf_nan=False)
    instructions: str = ""
    judgment: JudgmentConfig | None = None
