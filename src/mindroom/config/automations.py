"""Built-in automations an operator enables per agent: a cron schedule, a check in code, and a visible prompt."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.validation import duplicate_items
from mindroom.tool_system.worker_routing import agent_workspace_relative_path

_CRON_FIELDS = 5


def _validate_cron(value: str) -> str:
    # why-lazy: croniter stays out of the config import surface.
    from croniter import croniter  # noqa: PLC0415

    if len(value.split()) != _CRON_FIELDS or not croniter.is_valid(value):
        msg = f"Automation cron must be a valid five-field expression: {value!r}"
        raise ValueError(msg)
    return value


class PromptCurationAutomation(BaseModel):
    """Daily check that asks the agent to condense its always-loaded prompt files once they grow too large."""

    model_config = ConfigDict(extra="forbid")

    name: Literal["prompt_curation"] = Field(default="prompt_curation", description="Built-in automation name")
    cron: str = Field(default="0 4 * * *", description="When to check, in the configured timezone")
    room: str | None = Field(
        default=None,
        description="Room alias or ID for the prompt; defaults to the agent's first configured room",
    )
    trigger_tokens: int = Field(
        default=50_000,
        ge=1,
        description="Post the prompt once MEMORY.md and the context files total more than this many estimated tokens",
    )
    min_reduction: float = Field(
        default=0.10,
        gt=0,
        lt=1,
        description="Fraction of the files' size each pass is asked to remove, unless less is needed",
    )
    max_reduction: float = Field(
        default=0.15,
        gt=0,
        lt=1,
        description="Largest fraction of the files' size one pass may remove",
    )
    max_file_shrink: float = Field(
        default=0.25,
        gt=0,
        le=1,
        description="Largest fraction any single file may shrink in one pass",
    )
    max_content_loss: float = Field(
        default=0.05,
        ge=0,
        lt=1,
        description=(
            "Largest net drop in total memory content (the files plus memory/**), as a fraction of the files' "
            "size; detail must move to memory/ instead of being deleted"
        ),
    )
    protected_files: list[str] = Field(
        default_factory=list,
        description="Workspace-relative files the pass must leave unchanged",
    )

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        """Reject cron expressions croniter cannot schedule."""
        return _validate_cron(value)

    @field_validator("protected_files")
    @classmethod
    def validate_protected_files(cls, values: list[str]) -> list[str]:
        """Normalize protected paths and reject paths outside the workspace."""
        return [agent_workspace_relative_path(value).as_posix() for value in values]

    @model_validator(mode="after")
    def validate_reductions(self) -> Self:
        """Reject a minimum cut larger than the maximum."""
        if self.min_reduction > self.max_reduction:
            msg = "min_reduction must not exceed max_reduction"
            raise ValueError(msg)
        return self


def normalize_automation_entries(values: object) -> object:
    """Accept a bare built-in name as shorthand for that built-in with its defaults."""
    if not isinstance(values, list):
        return values
    return [{"name": value} if isinstance(value, str) else value for value in values]


def validate_unique_automations(values: list[PromptCurationAutomation]) -> list[PromptCurationAutomation]:
    """Allow each built-in at most once per agent."""
    if duplicates := duplicate_items([automation.name for automation in values]):
        msg = f"Duplicate automations are not allowed: {', '.join(duplicates)}"
        raise ValueError(msg)
    return values
