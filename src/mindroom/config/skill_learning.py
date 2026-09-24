"""Opt-in bounded background skill review settings."""

from pydantic import BaseModel, ConfigDict, Field


class SkillLearningConfig(BaseModel):
    """Review completed standalone agent turns for reusable Markdown procedures."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Review completed standalone turns for reusable workspace skills")
    model: str | None = Field(default=None, description="Reviewer model alias; defaults to the agent model")
    cooldown_seconds: int = Field(
        default=300,
        ge=0,
        le=86400,
        description="Seconds to coalesce completed turns before review",
    )
    max_input_chars: int = Field(
        default=24000,
        ge=1000,
        le=100000,
        description="Maximum combined trace and skill context characters per review",
    )
    max_output_chars: int = Field(
        default=12000,
        ge=500,
        le=32000,
        description="Maximum characters in a generated SKILL.md",
    )
    timeout_seconds: int = Field(default=60, ge=1, le=300, description="Maximum seconds per model review")
    max_attempts: int = Field(
        default=3,
        ge=1,
        le=5,
        description="Maximum review or publication attempts per queued generation",
    )
