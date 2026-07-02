"""Transition tests: `ResolvedEntityView` fields match the Config accessors they replace."""
# ruff: noqa: D103

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig


def _representative_config() -> Config:
    return Config(
        agents={
            "overriding_agent": AgentConfig(
                display_name="Overriding Agent",
                num_history_runs=7,
                max_tool_calls_from_history=3,
                compaction=CompactionOverrideConfig(threshold_percent=0.6),
            ),
            "inheriting_agent": AgentConfig(display_name="Inheriting Agent"),
        },
        teams={
            "overriding_team": TeamConfig(
                display_name="Overriding Team",
                role="Team with authored overrides",
                agents=["overriding_agent"],
                num_history_messages=11,
                compaction=CompactionOverrideConfig(enabled=False),
            ),
            "inheriting_team": TeamConfig(
                display_name="Inheriting Team",
                role="Team without authored overrides",
                agents=["inheriting_agent"],
            ),
        },
        defaults=DefaultsConfig(
            tools=[],
            num_history_runs=4,
            max_tool_calls_from_history=9,
            compaction=CompactionConfig(
                enabled=False,
                threshold_tokens=12_000,
                reserve_tokens=2_048,
                model="summary-model",
            ),
        ),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=48_000),
            "summary-model": ModelConfig(provider="openai", id="summary-model-id", context_window=32_000),
        },
    )


@pytest.mark.parametrize(
    "entity_name",
    ["overriding_agent", "inheriting_agent", "overriding_team", "inheriting_team"],
)
def test_view_fields_match_entity_accessors(entity_name: str) -> None:
    config = _representative_config()
    view = config.resolve_entity(entity_name)

    assert view.name == entity_name
    assert view.history_settings == config.get_entity_history_settings(entity_name)
    assert view.compaction_config == config.get_entity_compaction_config(entity_name)
    assert view.has_authored_compaction_config == config.has_authored_entity_compaction_config(entity_name)


def test_defaults_scope_view_matches_default_accessors() -> None:
    config = _representative_config()
    view = config.resolve_entity(None)

    assert view.name is None
    assert view.history_settings == config.get_default_history_settings()
    assert view.compaction_config == config.get_default_compaction_config()
    assert view.has_authored_compaction_config == config.has_authored_default_compaction_config()


def test_unknown_entity_raises_on_field_access() -> None:
    view = _representative_config().resolve_entity("missing")

    with pytest.raises(ValueError, match="Unknown entity: missing"):
        _ = view.history_settings
    with pytest.raises(ValueError, match="Unknown entity: missing"):
        _ = view.compaction_config
    with pytest.raises(ValueError, match="Unknown entity: missing"):
        _ = view.has_authored_compaction_config
