"""Prompt-curation settings: defaults, per-agent inheritance, and the validation that runs at config load."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.config.prompt_curation import PromptCurationConfig


def _config(defaults: dict[str, object] | None = None, **agent_fields: object) -> Config:
    return Config(
        defaults={"prompt_curation": defaults} if defaults is not None else {},
        agents={"mind": AgentConfig(display_name="Mind", memory_backend="file", **agent_fields)},
        router=RouterConfig(model="default"),
    )


def test_defaults_curate_memory_md_conservatively() -> None:
    """Defaults turn curation on for MEMORY.md only, with the documented bounds."""
    settings = _config().resolve_entity("mind").prompt_curation

    assert settings.enabled is True
    assert settings.trigger_tokens == 50_000
    assert settings.trigger_context_fraction is None
    assert settings.target_ratio == 0.9
    assert (settings.min_reduction_per_pass, settings.max_reduction_per_pass) == (0.10, 0.15)
    assert settings.max_file_shrink == 0.25
    assert settings.max_content_loss == 0.05
    assert settings.cooldown_hours == 24
    assert settings.files == ["MEMORY.md"]
    assert settings.protected_files == ["SOUL.md", "IDENTITY.md", "AGENTS.md"]


def test_agent_override_inherits_omitted_fields() -> None:
    """Per-agent settings override only the fields they set."""
    config = _config(
        {"trigger_tokens": 40_000, "cooldown_hours": 12},
        prompt_curation={"files": ["MEMORY.md", "USER.md"], "trigger_tokens": 30_000},
    )

    settings = config.resolve_entity("mind").prompt_curation

    assert settings.files == ["MEMORY.md", "USER.md"]
    assert settings.trigger_tokens == 30_000
    assert settings.cooldown_hours == 12


def test_agent_can_turn_off_curation_that_defaults_enable() -> None:
    """Curation can be switched off globally or for one agent."""
    assert _config(prompt_curation={"enabled": False}).resolve_entity("mind").prompt_curation.enabled is False
    assert _config({"enabled": False}).resolve_entity("mind").prompt_curation.enabled is False


def test_merged_settings_must_not_curate_a_protected_file() -> None:
    """An agent cannot curate a file the merged settings protect; config load fails."""
    with pytest.raises(ValidationError, match=r"USER\.md"):
        _config({"protected_files": ["SOUL.md", "USER.md"]}, prompt_curation={"files": ["MEMORY.md", "USER.md"]})


def test_reduction_bounds_must_be_ordered() -> None:
    """The minimum cut per pass cannot exceed the maximum."""
    with pytest.raises(ValidationError, match="min_reduction_per_pass"):
        PromptCurationConfig(min_reduction_per_pass=0.2, max_reduction_per_pass=0.1)


@pytest.mark.parametrize("path", ["../MEMORY.md", "/abs/MEMORY.md", "memory/topic.md", "notes.txt", ".git/x.md", ""])
def test_curatable_paths_must_be_workspace_markdown_outside_memory(path: str) -> None:
    """Curatable paths are workspace-relative Markdown files outside memory/ and .git."""
    with pytest.raises(ValidationError):
        PromptCurationConfig(files=[path])


def test_curatable_paths_are_unique() -> None:
    """A curatable file is listed once."""
    with pytest.raises(ValidationError, match="Duplicate"):
        PromptCurationConfig(files=["MEMORY.md", "MEMORY.md"])
