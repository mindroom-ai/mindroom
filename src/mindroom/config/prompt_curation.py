"""Settings for background curation of the prompt files an agent loads on every turn."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.validation import duplicate_items

# File memory's searchable topic directory; curation moves detail there, so it is never curated itself.
_MEMORY_DIR = "memory"
_DEFAULT_FILES = ("MEMORY.md",)
_DEFAULT_PROTECTED_FILES = ("SOUL.md", "IDENTITY.md", "AGENTS.md")


def _workspace_markdown_path(value: str) -> str:
    """Return one workspace-relative Markdown path that curation may name, or raise."""
    path = PurePosixPath(value.strip())
    parts = path.parts
    if not parts or path.is_absolute() or ".." in parts:
        msg = f"Prompt curation paths must be relative paths inside the agent workspace: {value!r}"
        raise ValueError(msg)
    if path.suffix != ".md":
        msg = f"Prompt curation paths must be Markdown files: {value!r}"
        raise ValueError(msg)
    if parts[0] == _MEMORY_DIR or any(part.casefold() == ".git" for part in parts):
        msg = f"Prompt curation paths must not be under memory/ or .git: {value!r}"
        raise ValueError(msg)
    return path.as_posix()


def _workspace_markdown_paths(values: list[str], *, field_name: str) -> list[str]:
    normalized = [_workspace_markdown_path(value) for value in values]
    if duplicates := duplicate_items(normalized):
        msg = f"Duplicate {field_name} entries are not allowed: {', '.join(duplicates)}"
        raise ValueError(msg)
    return normalized


class PromptCurationConfig(BaseModel):
    """Condense oversized always-loaded prompt files gradually in a background run of the agent."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Condense curatable files once they exceed the trigger")
    trigger_tokens: int = Field(
        default=50_000,
        ge=1,
        description="Start curating once the curatable files total more than this many estimated tokens",
    )
    trigger_context_fraction: float | None = Field(
        default=None,
        gt=0,
        lt=1,
        description=(
            "Optional lower trigger as a fraction of the agent model's context_window, for small-window models"
        ),
    )
    target_ratio: float = Field(
        default=0.9,
        gt=0,
        lt=1,
        description="Keep curating on later passes until the files are under this fraction of the trigger",
    )
    min_reduction_per_pass: float = Field(
        default=0.10,
        gt=0,
        lt=1,
        description="Fraction of the curatable files' size each pass is asked to remove, unless less is needed",
    )
    max_reduction_per_pass: float = Field(
        default=0.15,
        gt=0,
        lt=1,
        description="Largest fraction of the curatable files' size one pass may remove",
    )
    max_file_shrink: float = Field(
        default=0.25,
        gt=0,
        le=1,
        description="Largest fraction any single curatable file may shrink in one pass",
    )
    max_content_loss: float = Field(
        default=0.05,
        ge=0,
        lt=1,
        description=(
            "Largest net drop in total memory content (curatable files plus memory/**), as a fraction of the "
            "curatable files' size before the pass; detail must move to memory/ instead of being deleted"
        ),
    )
    cooldown_hours: float = Field(
        default=24,
        gt=0,
        description="Minimum hours between passes for one agent workspace; doubles after each failed pass, up to 8x",
    )
    timeout_seconds: int = Field(default=600, ge=1, le=3600, description="Maximum seconds per pass")
    files: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_FILES),
        min_length=1,
        description="Workspace-relative prompt files curation may condense",
    )
    protected_files: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_PROTECTED_FILES),
        description="Workspace-relative files curation never changes and that files may not name",
    )

    @field_validator("files")
    @classmethod
    def validate_files(cls, values: list[str]) -> list[str]:
        """Normalize curatable paths and reject paths outside the workspace or under memory/."""
        return _workspace_markdown_paths(values, field_name="prompt_curation.files")

    @field_validator("protected_files")
    @classmethod
    def validate_protected_files(cls, values: list[str]) -> list[str]:
        """Normalize protected paths with the same rules as curatable ones."""
        return _workspace_markdown_paths(values, field_name="prompt_curation.protected_files")

    @model_validator(mode="after")
    def validate_consistency(self) -> Self:
        """Reject reversed reduction bounds and curatable files that are also protected."""
        if self.min_reduction_per_pass > self.max_reduction_per_pass:
            msg = "prompt_curation.min_reduction_per_pass must not exceed max_reduction_per_pass"
            raise ValueError(msg)
        if protected := sorted(set(self.files) & set(self.protected_files)):
            msg = f"prompt_curation.files names protected files: {', '.join(protected)}"
            raise ValueError(msg)
        return self


class AgentPromptCurationConfig(BaseModel):
    """Per-agent prompt-curation overrides; omitted fields inherit defaults.prompt_curation."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(default=None, description="Per-agent prompt curation switch")
    trigger_tokens: int | None = Field(default=None, ge=1, description="Per-agent trigger in estimated tokens")
    trigger_context_fraction: float | None = Field(
        default=None,
        gt=0,
        lt=1,
        description="Per-agent trigger as a fraction of the model's context_window",
    )
    target_ratio: float | None = Field(default=None, gt=0, lt=1, description="Per-agent hysteresis target")
    min_reduction_per_pass: float | None = Field(default=None, gt=0, lt=1, description="Per-agent minimum cut")
    max_reduction_per_pass: float | None = Field(default=None, gt=0, lt=1, description="Per-agent maximum cut")
    max_file_shrink: float | None = Field(default=None, gt=0, le=1, description="Per-agent per-file shrink cap")
    max_content_loss: float | None = Field(default=None, ge=0, lt=1, description="Per-agent net content-loss cap")
    cooldown_hours: float | None = Field(default=None, gt=0, description="Per-agent hours between passes")
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600, description="Per-agent pass timeout")
    files: list[str] | None = Field(default=None, min_length=1, description="Per-agent curatable files")
    protected_files: list[str] | None = Field(default=None, description="Per-agent protected files")

    @field_validator("files")
    @classmethod
    def validate_files(cls, values: list[str] | None) -> list[str] | None:
        """Normalize per-agent curatable paths."""
        return None if values is None else _workspace_markdown_paths(values, field_name="prompt_curation.files")

    @field_validator("protected_files")
    @classmethod
    def validate_protected_files(cls, values: list[str] | None) -> list[str] | None:
        """Normalize per-agent protected paths."""
        if values is None:
            return None
        return _workspace_markdown_paths(values, field_name="prompt_curation.protected_files")


def merge_prompt_curation(
    defaults: PromptCurationConfig,
    override: AgentPromptCurationConfig | None,
) -> PromptCurationConfig:
    """Return the validated settings one agent uses: its authored overrides on top of the defaults."""
    if override is None:
        return defaults
    return PromptCurationConfig.model_validate({**defaults.model_dump(), **override.model_dump(exclude_none=True)})
