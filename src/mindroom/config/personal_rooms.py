"""Opt-in personal agent room configuration."""

from string import Formatter

from pydantic import BaseModel, ConfigDict, Field, field_validator


class PersonalRoomsConfig(BaseModel):
    """One private room per eligible human, managed by a selected agent."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(description="Agent that owns and answers in personal rooms")
    onboarding_rooms: list[str] = Field(
        min_length=1,
        description="Configured rooms whose human joins trigger onboarding",
    )
    commands: list[str] = Field(
        default_factory=list,
        description="Exact self-onboarding commands accepted in onboarding rooms",
    )
    alias_prefix: str = Field(default="personal", pattern=r"^[a-z0-9_-]{1,40}$")
    name: str = Field(default="Personal room for {user}", max_length=1000)
    topic: str = Field(default="Private conversation with {agent}.", max_length=1000)
    welcome: str = Field(default="Welcome {user}! This is your personal room with {agent}.", max_length=10000)
    confirmation: str = Field(
        default="",
        max_length=10000,
        description="Optional once-only notice in the onboarding room",
    )
    welcome_dispatch: bool = Field(
        default=False,
        description="Send the welcome through trusted hook dispatch after the human joins",
    )
    backfill: bool = Field(default=False, description="On startup, reconcile existing eligible onboarding-room members")
    requester_admin: bool = Field(default=False, description="Grant the human Matrix room admin capability")
    avatar: str | None = Field(default=None, description="Optional room avatar file, relative to the configuration")
    avatar_from_requester: bool = Field(
        default=False,
        description="Copy the requester's profile avatar if the room is empty",
    )

    @field_validator("name", "topic", "welcome", "confirmation")
    @classmethod
    def validate_template(cls, value: str) -> str:
        """Permit only simple user, room, and agent placeholders."""
        for _, name, spec, conversion in Formatter().parse(value):
            if name is not None and (name not in {"user", "room", "agent"} or spec or conversion):
                msg = "Personal-room templates accept only {user}, {room}, and {agent}"
                raise ValueError(msg)
        return value

    @field_validator("commands")
    @classmethod
    def validate_commands(cls, value: list[str]) -> list[str]:
        """Avoid matching ordinary conversation or command arguments."""
        if any(
            not command.startswith("!") or len(command) < 2 or any(c.isspace() for c in command) for command in value
        ):
            msg = "Personal-room commands must start with ! and contain no whitespace"
            raise ValueError(msg)
        return list(dict.fromkeys(value))
