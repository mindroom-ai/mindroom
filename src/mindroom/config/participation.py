"""Opt-in participation in conversations between multiple humans."""

from pydantic import BaseModel, ConfigDict, Field


class RoomParticipationConfig(BaseModel):
    """Designated individual agent and pause for one room's adaptive turns."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    debounce_seconds: float = Field(default=3.0, ge=0.0, le=30.0, allow_inf_nan=False)
    instructions: str = ""
