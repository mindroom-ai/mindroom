"""Opt-in listening: an agent hears chosen rooms and acts silently after a quiet period."""

from pydantic import BaseModel, ConfigDict, Field

from mindroom.config.schema_hints import dashboard_hint


class ListenConfig(BaseModel):
    """Rooms an agent listens in, when it runs a silent turn over what it heard, and what that turn may do."""

    model_config = ConfigDict(extra="forbid")

    rooms: list[str] = Field(
        min_length=1,
        description="Room keys, Matrix room IDs, or aliases the agent listens in",
        json_schema_extra=dashboard_hint(reference="room"),
    )
    quiet_seconds: int = Field(
        default=60,
        ge=10,
        description="Seconds a room must stay quiet before the agent runs a silent turn over what it heard",
    )
    max_messages: int = Field(
        default=200,
        ge=1,
        le=500,
        description="Most messages one silent turn reads; the rest wait for the next turn",
    )
    max_runs_per_hour: int = Field(
        default=6,
        ge=1,
        le=60,
        description="Most silent turns per hour; further due runs are skipped and what was heard is kept",
    )
    tools: list[str] = Field(
        default_factory=lambda: ["memory", "react"],
        min_length=1,
        description=(
            "Tools a silent turn may use, as toolkit names or toolkit.function, each one of the agent's own tools; "
            "no other tool is available"
        ),
        json_schema_extra=dashboard_hint(reference="tool"),
    )
    model: str | None = Field(
        default=None,
        description="Model alias under models for silent turns; unset uses the room's model, then the agent's model",
        json_schema_extra=dashboard_hint(reference="model"),
    )
