"""Opt-in judgments for messages queued during an active response."""

from pydantic import BaseModel, ConfigDict

from mindroom.config.judgment import JudgmentConfig


class RoomMidTurnConfig(BaseModel):
    """Choose whether an active turn may finish before handling queued messages."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    judgment: JudgmentConfig
    instructions: str = ""
