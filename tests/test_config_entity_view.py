"""Behavioral tests for `ResolvedEntityView`, the per-entity resolved config API."""
# ruff: noqa: D103

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, CultureConfig, TeamConfig
from mindroom.config.errors import ConfigRuntimeValidationError
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config, RuntimeConfig
from mindroom.config.memory import AgentMemorySearchConfig, MemoryConfig, MemorySearchConfig
from mindroom.config.models import (
    CompactionConfig,
    CompactionOverrideConfig,
    DefaultsConfig,
    ModelConfig,
    ToolConfigEntry,
)
from mindroom.config.runtime import expand_tool_names
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.history.types import HistoryPolicy
from mindroom.tool_system.catalog import ConfigField, ToolValidationInfo
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _representative_config(tmp_path: Path) -> RuntimeConfig:
    authored = Config(
        agents={
            "overriding_agent": AgentConfig(
                display_name="Overriding Agent",
                model="summary-model",
                num_history_runs=7,
                max_tool_calls_from_history=3,
                compaction=CompactionOverrideConfig(threshold_percent=0.6),
                memory_backend="file",
                memory_search=AgentMemorySearchConfig(mode="semantic"),
                tools=[
                    ToolConfigEntry(name="calculator"),
                    ToolConfigEntry(
                        name="shell",
                        defer=True,
                        initial=True,
                        overrides={"shell_path_prepend": ["/run/wrappers/bin"]},
                    ),
                ],
                worker_scope="user_agent",
                knowledge_bases=["engineering_docs"],
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
        knowledge_bases={"engineering_docs": KnowledgeBaseConfig(path="./knowledge_docs")},
    )
    return RuntimeConfig.from_authored(authored, test_runtime_paths(tmp_path))


def test_history_settings_resolution(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)

    assert config.resolve_entity("overriding_agent").history_settings.policy == HistoryPolicy(mode="runs", limit=7)
    assert config.resolve_entity("overriding_agent").history_settings.max_tool_calls_from_history == 3
    assert config.resolve_entity("overriding_team").history_settings.policy == HistoryPolicy(mode="messages", limit=11)
    assert config.resolve_entity("overriding_team").history_settings.max_tool_calls_from_history == 9
    for inheriting_scope in ("inheriting_agent", "inheriting_team", None):
        settings = config.resolve_entity(inheriting_scope).history_settings
        assert settings.policy == HistoryPolicy(mode="runs", limit=4)
        assert settings.max_tool_calls_from_history == 9
        assert settings.system_message_role == "system"


def test_compaction_resolution(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)

    merged = config.resolve_entity("overriding_agent").compaction_config
    assert merged.enabled is True
    assert merged.threshold_tokens is None
    assert merged.threshold_percent == 0.6
    assert merged.reserve_tokens == 2_048
    assert merged.model == "summary-model"

    disabled = config.resolve_entity("overriding_team").compaction_config
    assert disabled.enabled is False
    assert disabled.threshold_tokens == 12_000

    for inheriting_scope in ("inheriting_agent", "inheriting_team", None):
        inherited = config.resolve_entity(inheriting_scope).compaction_config
        assert inherited == CompactionConfig(
            enabled=False,
            threshold_tokens=12_000,
            reserve_tokens=2_048,
            model="summary-model",
        )
        assert config.resolve_entity(inheriting_scope).has_authored_compaction_config is True


def test_memory_resolution(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)

    assert config.resolve_entity("overriding_agent").memory_backend == "file"
    overridden_search = config.resolve_entity("overriding_agent").memory_search
    assert overridden_search.mode == "semantic"
    assert overridden_search.include == ["notes/**/*.md"]

    for inheriting_scope in ("inheriting_agent", "overriding_team", ROUTER_AGENT_NAME, None):
        assert config.resolve_entity(inheriting_scope).memory_backend == "none"
        assert config.resolve_entity(inheriting_scope).memory_search.mode == "keyword"
        assert config.resolve_entity(inheriting_scope).memory_search.include == ["notes/**/*.md"]


def test_model_name_resolution(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)

    assert config.resolve_entity("overriding_agent").model_name == "summary-model"
    assert config.resolve_entity("inheriting_agent").model_name == "default"
    assert config.resolve_entity("overriding_team").model_name == "default"
    assert config.resolve_entity(ROUTER_AGENT_NAME).model_name == "default"
    with pytest.raises(ValueError, match="defaults-only scope has no authored model"):
        _ = config.resolve_entity(None).model_name


def test_tool_resolution(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)
    view = config.resolve_entity("overriding_agent")

    assert view.available_tools == expand_tool_names(config, ["calculator", "shell"])
    assert [entry.name for entry in view.tool_configs] == (
        expand_tool_names(config, ["calculator"]) + expand_tool_names(config, ["shell"])
    )
    shell_entry = next(entry for entry in view.tool_configs if entry.name == "shell")
    assert shell_entry.defer is True
    assert shell_entry.initial is True
    assert shell_entry.tool_config_overrides == {"shell_path_prepend": ["/run/wrappers/bin"]}

    assert [entry.name for entry in view.authored_deferred_tool_configs] == ["shell"]
    assert view.authored_deferred_tool_config("shell") is not None
    assert view.authored_deferred_tool_config("calculator") is None
    assert view.tool_runtime_overrides("shell") == {"shell_path_prepend": "/run/wrappers/bin"}
    assert view.tool_runtime_overrides("calculator") is None
    assert view.deferred_tool_scope_incompatible_tools("shell") == []

    inheriting = config.resolve_entity("inheriting_agent")
    assert inheriting.available_tools == []
    assert inheriting.tool_configs == []
    assert inheriting.authored_deferred_tool_configs == []


def test_scope_resolution(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)

    assert config.resolve_entity("overriding_agent").execution_scope == "user_agent"
    assert config.resolve_entity("overriding_agent").scope_label == "worker_scope=user_agent"
    assert config.resolve_entity("inheriting_agent").execution_scope is None
    assert config.resolve_entity("inheriting_agent").scope_label == "unscoped"


def test_culture_and_knowledge_resolution(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)

    culture = config.resolve_entity("overriding_agent").culture
    assert culture is not None
    culture_name, culture_config = culture
    assert culture_name == "engineering"
    assert culture_config.description == "Write tests first"
    assert config.resolve_entity("inheriting_agent").culture is None
    # Culture assignment is a membership scan, so non-agent names resolve to None instead of raising.
    assert config.resolve_entity("overriding_team").culture is None
    with pytest.raises(ValueError, match="defaults-only scope has no per-agent config"):
        _ = config.resolve_entity(None).culture

    assert config.resolve_entity("overriding_agent").knowledge_base_ids == ["engineering_docs"]
    assert config.resolve_entity("inheriting_agent").knowledge_base_ids == []
    assert config.resolve_entity("overriding_agent").private_knowledge_base_id is None


def test_unauthored_compaction_reports_not_authored(tmp_path: Path) -> None:
    config = RuntimeConfig.from_authored(
        Config(
            agents={"plain_agent": AgentConfig(display_name="Plain Agent")},
            defaults=DefaultsConfig(tools=[], compaction=None),
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )

    assert config.resolve_entity("plain_agent").has_authored_compaction_config is False
    assert config.resolve_entity(None).has_authored_compaction_config is False


def test_resolve_entity_returns_a_fresh_value_per_call(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)

    first = config.resolve_entity("overriding_agent")
    second = config.resolve_entity("overriding_agent")

    assert first is not second
    assert first == second


def test_agent_only_fields_raise_for_defaults_scope(tmp_path: Path) -> None:
    view = _representative_config(tmp_path).resolve_entity(None)

    with pytest.raises(ValueError, match="defaults-only scope has no per-agent config"):
        _ = view.available_tools


def test_agent_only_fields_raise_for_team_names(tmp_path: Path) -> None:
    view = _representative_config(tmp_path).resolve_entity("overriding_team")

    with pytest.raises(ValueError, match="Unknown agent: overriding_team"):
        _ = view.available_tools


def test_unknown_entity_is_rejected_before_view_construction(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown entity: missing"):
        _representative_config(tmp_path).resolve_entity("missing")


def test_authored_config_parses_without_runtime_tool_catalog(tmp_path: Path) -> None:
    authored = Config(defaults=DefaultsConfig(tools=["not_registered_at_runtime"]))

    assert authored.defaults.tool_names == ["not_registered_at_runtime"]
    with pytest.raises(ConfigRuntimeValidationError, match="Unknown tool 'not_registered_at_runtime'"):
        RuntimeConfig.from_authored(authored, test_runtime_paths(tmp_path))


def test_resolved_entity_is_materialized_at_construction(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)
    view = config.resolve_entity("overriding_agent")

    config.agents["overriding_agent"].memory_backend = "none"

    assert view.memory_backend == "file"
    assert config.resolve_entity("overriding_agent").memory_backend == "none"


def test_resolved_entity_tool_overrides_are_deeply_isolated(tmp_path: Path) -> None:
    config = _representative_config(tmp_path)
    view = config.resolve_entity("overriding_agent")

    returned_entry = next(entry for entry in view.tool_configs if entry.name == "shell")
    returned_paths = returned_entry.tool_config_overrides["shell_path_prepend"]
    assert isinstance(returned_paths, list)
    returned_paths.append("/mutated/returned/value")

    authored_paths = config.agents["overriding_agent"].tools[1].overrides["shell_path_prepend"]
    assert isinstance(authored_paths, list)
    authored_paths.append("/mutated/runtime/config")

    fresh_entry = next(entry for entry in view.tool_configs if entry.name == "shell")
    assert fresh_entry.tool_config_overrides == {"shell_path_prepend": ["/run/wrappers/bin"]}


def test_runtime_tool_overrides_use_bound_validation_snapshot(tmp_path: Path) -> None:
    tool_name = "snapshot_only_review_tool_1467"
    paths_field = ConfigField(name="paths", label="Paths", type="string[]")
    authored = Config(
        agents={
            "test_agent": AgentConfig(
                display_name="Test Agent",
                tools=[ToolConfigEntry(name=tool_name, overrides={"paths": ["one", "two"]})],
            ),
        },
        defaults=DefaultsConfig(tools=[]),
    )
    snapshot = {
        tool_name: ToolValidationInfo(
            name=tool_name,
            config_fields=(paths_field,),
            agent_override_fields=(paths_field,),
        ),
    }

    config = RuntimeConfig.from_authored(
        authored,
        test_runtime_paths(tmp_path),
        tool_validation_snapshot=snapshot,
    )

    assert config.resolve_entity("test_agent").tool_runtime_overrides(tool_name) == {"paths": "one, two"}


def test_model_less_team_materializes_non_model_fields(tmp_path: Path) -> None:
    config = RuntimeConfig.from_authored(
        Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent")},
            teams={
                "test_team": TeamConfig(
                    display_name="Test Team",
                    role="Test team",
                    agents=["test_agent"],
                    model=None,
                    num_history_runs=2,
                ),
            },
            defaults=DefaultsConfig(tools=[]),
        ),
        test_runtime_paths(tmp_path),
    )

    view = config.resolve_entity("test_team")

    assert view.history_settings.policy == HistoryPolicy(mode="runs", limit=2)
    assert view.compaction_config == config.defaults.compaction
    with pytest.raises(ValueError, match="Team test_team has no model configured"):
        _ = view.model_name
