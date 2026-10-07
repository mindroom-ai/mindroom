"""Built-in automations an operator enables per agent: a cron schedule, a check in code, and a visible prompt."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from mindroom.config.validation import duplicate_items

_CRON_FIELDS = 5
# No file may shrink by more than this in one pass, so no pass can be asked to cut more in total.
MAX_FILE_SHRINK = 0.25


class _ScheduledAutomation(BaseModel):
    """Fields every built-in shares: when it checks, where it posts, and which model answers."""

    model_config = ConfigDict(extra="forbid")

    cron: str
    room: str | None = Field(
        default=None,
        description="Room alias or ID for the prompt; defaults to the agent's first configured room",
    )
    model: str | None = Field(
        default=None,
        description="Model for the prompt's runs, a key of models; defaults to the agent's own model",
    )

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, value: str) -> str:
        """Reject expressions that are not five fields or can never fire, such as February 31."""
        # why-lazy: croniter stays out of the config import surface.
        from croniter import croniter  # noqa: PLC0415

        try:
            if len(value.split()) != _CRON_FIELDS:
                raise ValueError(value)  # noqa: TRY301 - one message for every invalid form
            croniter(value, datetime.now(UTC)).get_next(datetime)
        except ValueError as exc:
            msg = f"Automation cron must be a five-field expression that can fire: {value!r}"
            raise ValueError(msg) from exc
        return value


class PromptCurationAutomation(_ScheduledAutomation):
    """Daily check that asks the agent to condense its always-loaded prompt files once they grow too large."""

    name: Literal["prompt_curation"] = Field(default="prompt_curation", description="Built-in automation name")
    cron: str = Field(default="0 4 * * *", description="When to check, in the configured timezone")
    trigger_tokens: int = Field(
        default=50_000,
        ge=1,
        description="Post the prompt once MEMORY.md and the context files total more than this many estimated tokens",
    )
    min_reduction: float = Field(
        default=0.10,
        gt=0,
        le=MAX_FILE_SHRINK,
        description="Smallest fraction of the files' size each pass is asked to remove, at most the 0.25 per-file bound",
    )
    max_reduction: float = Field(
        default=0.15,
        gt=0,
        lt=1,
        description="Largest fraction of the files' size one pass may remove before verify asks for a re-check",
    )
    max_content_loss: float = Field(
        default=0.05,
        ge=0,
        lt=1,
        description=(
            "Largest net drop in total memory content (the files plus memory/**), as a fraction of the files' "
            "size, before verify asks for a re-check; detail should move to memory/ instead of being deleted"
        ),
    )

    @model_validator(mode="after")
    def validate_reductions(self) -> Self:
        """Reject a minimum cut larger than the maximum."""
        if self.min_reduction > self.max_reduction:
            msg = "min_reduction must not exceed max_reduction"
            raise ValueError(msg)
        return self


class MemoryConsolidationAutomation(_ScheduledAutomation):
    """Nightly reconciliation of memory/ with new conversations, daily notes, and the sources memory cites."""

    name: Literal["memory_consolidation"] = Field(
        default="memory_consolidation",
        description="Built-in automation name",
    )
    cron: str = Field(default="15 3 * * *", description="When to check, in the configured timezone")


Automation = Annotated[PromptCurationAutomation | MemoryConsolidationAutomation, Field(discriminator="name")]
# Every automation message carries its automation's name as its hook source, so the turns it starts can be recognized.
AUTOMATION_NAMES = frozenset(
    model.model_fields["name"].default for model in (PromptCurationAutomation, MemoryConsolidationAutomation)
)


def _normalize_automation_entries(values: object) -> object:
    """Accept a bare built-in name as shorthand for that built-in with its defaults."""
    if not isinstance(values, list):
        return values
    return [{"name": value} if isinstance(value, str) else value for value in values]


def _validate_unique_automations(values: list[Automation]) -> list[Automation]:
    """Allow each built-in at most once per agent."""
    if duplicates := duplicate_items([automation.name for automation in values]):
        msg = f"Duplicate automations are not allowed: {', '.join(duplicates)}"
        raise ValueError(msg)
    return values


AutomationList = Annotated[
    list[Automation],
    BeforeValidator(_normalize_automation_entries),
    AfterValidator(_validate_unique_automations),
]
