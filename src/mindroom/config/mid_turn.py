"""Opt-in judgments for messages queued during an active response."""

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.judgment import JudgmentConfig
from mindroom.config.schema_hints import dashboard_hint


class MidTurnConfig(BaseModel):
    """Choose whether an agent's active turn may finish before handling queued messages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    judgment: JudgmentConfig = Field(description="LLM model alias or TypeSafe backend that makes the decision")
    instructions: str = Field(
        default="",
        description="Extra guidance for the finish-or-wrap-up decision",
        json_schema_extra=dashboard_hint(multiline=True),
    )
    defer_reaction: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"\S",
        description="Reaction such as 👀 added when a queued message can wait; the message remains queued",
    )
