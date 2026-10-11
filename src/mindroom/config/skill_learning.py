"""Opt-in background skill review settings."""

from pydantic import BaseModel, ConfigDict, Field


class SkillLearningConfig(BaseModel):
    """Periodically review standalone agent conversations and maintain learned workspace skills."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Review conversations and maintain learned workspace skills")
    model: str | None = Field(
        default=None,
        description="Review model alias; defaults to the model of the reviewed response",
    )
    review_interval: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Model replies, counting each tool-calling step, between reviews of one conversation",
    )
    timeout_seconds: int = Field(default=120, ge=10, le=900, description="Maximum seconds per review")
    notify: bool = Field(default=True, description="Post a notice in the conversation when a review changes skills")
    archive_after_days: int = Field(
        default=30,
        ge=0,
        le=3650,
        description=(
            "Archive learned skills with no use, creation, or skill_manage edit for this many days; "
            "0 keeps them indefinitely"
        ),
    )
