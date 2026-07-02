"""Transition tests: `ResolvedEntityView` fields match the Config accessors they replace."""
# ruff: noqa: D103

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig, CultureConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.memory import AgentMemorySearchConfig, MemoryConfig, MemorySearchConfig
from mindroom.config.models import CompactionConfig, CompactionOverrideConfig, DefaultsConfig, ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME


def _representative_config() -> Config:
    return Config(
        agents={
            "overriding_agent": AgentConfig(
                display_name="Overriding Agent",
                model="summary-model",
                num_history_runs=7,
                max_tool_calls_from_history=3,
                compaction=CompactionOverrideConfig(threshold_percent=0.6),
                memory_backend="file",
                memory_search=AgentMemorySearchConfig(mode="semantic"),
                tools=["calculator", {"name": "shell", "defer": True, "initial": True}],  # type: ignore[list-item]
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
        # Non-default global memory settings so inheritance assertions are non-degenerate.
        memory=MemoryConfig(backend="none", search=MemorySearchConfig(include=["notes/**/*.md"])),
        cultures={
            "engineering": CultureConfig(
                description="Write tests first",
                agents=["overriding_agent"],
            ),
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
    assert view.memory_backend == config.get_agent_memory_backend(entity_name)
    assert view.memory_search == config.get_agent_memory_search(entity_name)
    assert view.model_name == config.get_entity_model_name(entity_name)


@pytest.mark.parametrize("agent_name", ["overriding_agent", "inheriting_agent"])
def test_view_agent_fields_match_agent_accessors(agent_name: str) -> None:
    config = _representative_config()
    view = config.resolve_entity(agent_name)

    assert view.available_tools == config.get_agent_available_tools(agent_name)
    assert view.tool_configs == config.get_agent_tool_configs(agent_name)
    assert view.authored_deferred_tool_configs == config.get_agent_authored_deferred_tool_configs(agent_name)
    assert view.authored_deferred_tool_config("shell") == config.get_agent_authored_deferred_tool_config(
        agent_name,
        "shell",
    )
    assert view.tool_runtime_overrides("shell") == config.get_agent_tool_runtime_overrides(agent_name, "shell")
    assert view.deferred_tool_scope_incompatible_tools("shell") == config.get_deferred_tool_scope_incompatible_tools(
        agent_name,
        "shell",
    )
    assert view.execution_scope == config.get_agent_execution_scope(agent_name)
    assert view.scope_label == config.get_agent_scope_label(agent_name)
    assert view.culture == config.get_agent_culture(agent_name)
    assert view.knowledge_base_ids == config.get_agent_knowledge_base_ids(agent_name)


def test_agent_only_fields_raise_for_defaults_scope() -> None:
    view = _representative_config().resolve_entity(None)

    with pytest.raises(ValueError, match="defaults-only scope has no per-agent config"):
        _ = view.available_tools


def test_agent_only_fields_raise_for_team_names() -> None:
    view = _representative_config().resolve_entity("overriding_team")

    with pytest.raises(ValueError, match="Unknown agent: overriding_team"):
        _ = view.available_tools


def test_defaults_scope_view_matches_default_accessors() -> None:
    config = _representative_config()
    view = config.resolve_entity(None)

    assert view.name is None
    assert view.history_settings == config.get_default_history_settings()
    assert view.compaction_config == config.get_default_compaction_config()
    assert view.has_authored_compaction_config == config.has_authored_default_compaction_config()
    assert view.memory_backend == config.memory.backend
    assert view.memory_search == config.memory.search
    with pytest.raises(ValueError, match="defaults-only scope has no authored model"):
        _ = view.model_name


def test_router_view_resolves_router_model() -> None:
    config = _representative_config()
    view = config.resolve_entity(ROUTER_AGENT_NAME)

    assert view.model_name == config.get_entity_model_name(ROUTER_AGENT_NAME)
    assert view.memory_backend == config.get_agent_memory_backend(ROUTER_AGENT_NAME)


def test_unauthored_compaction_reports_not_authored() -> None:
    config = Config(
        agents={"plain_agent": AgentConfig(display_name="Plain Agent")},
        defaults=DefaultsConfig(tools=[], compaction=None),
        models={"default": ModelConfig(provider="openai", id="test-model")},
    )

    assert config.resolve_entity("plain_agent").has_authored_compaction_config is False
    assert config.resolve_entity(None).has_authored_compaction_config is False


def test_resolve_entity_returns_a_fresh_view_per_call() -> None:
    config = _representative_config()

    assert config.resolve_entity("overriding_agent") is not config.resolve_entity("overriding_agent")


def test_unknown_entity_raises_on_field_access() -> None:
    view = _representative_config().resolve_entity("missing")

    with pytest.raises(ValueError, match="Unknown entity: missing"):
        _ = view.history_settings
    with pytest.raises(ValueError, match="Unknown entity: missing"):
        _ = view.compaction_config
    with pytest.raises(ValueError, match="Unknown entity: missing"):
        _ = view.has_authored_compaction_config
