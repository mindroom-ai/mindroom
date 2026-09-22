"""Opt-in participation in conversations between multiple humans."""

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.judgment import JudgmentConfig


class ParticipationConfig(BaseModel):
    """Pause and judgment settings for an agent's existing threads in any room."""

    model_config = ConfigDict(extra="forbid")

    debounce_seconds: float = Field(default=3.0, ge=0.0, le=30.0, allow_inf_nan=False)
    instructions: str = ""
    decline_reaction: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"\S")
    judgment: JudgmentConfig | None = None
