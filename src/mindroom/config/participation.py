"""Opt-in participation in conversations between multiple humans."""

from pydantic import BaseModel, ConfigDict, Field


class TypeSafeParticipationConfig(BaseModel):
    """Explicit permission to send bounded conversation text to TypeSafe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    threshold: float = Field(default=0.8, ge=0.0, le=1.0, allow_inf_nan=False)
    timeout_seconds: float = Field(default=1.5, gt=0.0, le=30.0, allow_inf_nan=False)


class RoomParticipationConfig(BaseModel):
    """Designated individual agent and pause for one room's adaptive turns."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    debounce_seconds: float = Field(default=3.0, ge=0.0, le=30.0, allow_inf_nan=False)
    instructions: str = ""
    typesafe: TypeSafeParticipationConfig | None = None
