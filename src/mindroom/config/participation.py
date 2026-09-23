"""Opt-in participation in conversations between multiple humans."""

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.judgment import JudgmentConfig
from mindroom.config.schema_hints import dashboard_hint


class ParticipationConfig(BaseModel):
    """Pause and judgment settings for an agent's existing threads in any room."""

    model_config = ConfigDict(extra="forbid")

    debounce_seconds: float = Field(
        default=3.0,
        ge=0.0,
        le=30.0,
        allow_inf_nan=False,
        description="Quiet window in seconds for eligible text before deciding whether to participate",
    )
    instructions: str = Field(
        default="",
        description="Additional guidance for deciding whether the agent should participate",
        json_schema_extra=dashboard_hint(multiline=True),
    )
    decline_reaction: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"\S",
        description="Reaction such as 👍 added to a deliberately declined message; unset keeps declines invisible",
    )
    judgment: JudgmentConfig | None = Field(
        default=None,
        description="Separate judgment backend for the decision; unset uses the agent's reply model",
    )
