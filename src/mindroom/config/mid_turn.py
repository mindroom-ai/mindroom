"""Opt-in judgments for messages queued during an active response."""

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.judgment import JudgmentConfig


class MidTurnConfig(BaseModel):
    """Choose whether an agent's active turn may finish before handling queued messages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    judgment: JudgmentConfig
    instructions: str = ""
    defer_reaction: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"\S")
