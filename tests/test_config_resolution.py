"""Behavioral tests for the resolved per-entity config records: `resolve_scope`, `resolve_entity`, `resolve_agent`."""
# ruff: noqa: D103

from __future__ import annotations

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.memory import AgentMemorySearchConfig, MemoryConfig, MemorySearchConfig
from mindroom.config.models import (
    CompactionConfig,
    CompactionOverrideConfig,
    DefaultsConfig,
    ModelConfig,
    ToolConfigEntry,
)
from mindroom.config.resolution import ResolvedAgent, ResolvedEntity
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.history.types import HistoryPolicy
from mindroom.tool_system.agent_tools import resolve_agent_tools


def _representative_config() -> Config:
    return Config(
        agents={
            "overriding_agent": AgentConfig(
                display_name="Overriding Agent",
                model="summary-model",
                num_history_runs=7,
                max_tool_calls_from_history=3,
                compaction=CompactionOverrideConfig(
                    threshold_percent=0.6,
                    timeout_seconds=75.0,
                ),
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
                replay_window_tokens=24_000,
                reserve_tokens=2_048,
                model="summary-model",
                timeout_seconds=420.0,
            ),
        ),
        models={
            "default": ModelConfig(provider="openai", id="test-model", context_window=48_000),
            "summary-model": ModelConfig(provider="openai", id="summary-model-id", context_window=32_000),
        },
        # Non-default global memory settings so inheritance assertions are non-degenerate.
        memory=MemoryConfig(backend="none", search=MemorySearchConfig(include=["notes/**/*.md"])),
        knowledge_bases={"engineering_docs": KnowledgeBaseConfig(path="./knowledge_docs")},
    )


def test_history_settings_resolution() -> None:
    config = _representative_config()

    assert config.resolve_entity("overriding_agent").history_settings.policy == HistoryPolicy(mode="runs", limit=7)
    assert config.resolve_entity("overriding_agent").history_settings.max_tool_calls_from_history == 3
    assert config.resolve_entity("overriding_team").history_settings.policy == HistoryPolicy(mode="messages", limit=11)
    assert config.resolve_entity("overriding_team").history_settings.max_tool_calls_from_history == 9
    for inheriting_scope in ("inheriting_agent", "inheriting_team", ROUTER_AGENT_NAME, None):
        settings = config.resolve_scope(inheriting_scope).history_settings
        assert settings.policy == HistoryPolicy(mode="runs", limit=4)
        assert settings.max_tool_calls_from_history == 9


def test_compaction_resolution() -> None:
    config = _representative_config()

    merged = config.resolve_entity("overriding_agent").history_settings.compaction_config
    assert merged.enabled is True
    assert merged.threshold_tokens is None
    assert merged.threshold_percent == 0.6
    assert merged.replay_window_tokens == 24_000
    assert merged.reserve_tokens == 2_048
    assert merged.model == "summary-model"
    assert merged.timeout_seconds == 75.0

    disabled = config.resolve_entity("overriding_team").history_settings.compaction_config
    assert disabled.enabled is False
    assert disabled.threshold_tokens == 12_000
    assert disabled.timeout_seconds == 420.0

    for inheriting_scope in ("inheriting_agent", "inheriting_team", None):
        inherited = config.resolve_scope(inheriting_scope).history_settings.compaction_config
        assert inherited == CompactionConfig(
            enabled=False,
            threshold_tokens=12_000,
            replay_window_tokens=24_000,
            reserve_tokens=2_048,
            model="summary-model",
            timeout_seconds=420.0,
        )
    for scope in ("overriding_agent", "overriding_team", "inheriting_agent", "inheriting_team", None):
        assert config.resolve_scope(scope).history_settings.has_authored_compaction_config is True


def test_memory_resolution() -> None:
    config = _representative_config()

    assert config.resolve_entity("overriding_agent").memory_backend == "file"
    overridden_search = config.resolve_entity("overriding_agent").memory_search
    assert overridden_search.mode == "semantic"
    assert overridden_search.include == ["notes/**/*.md"]

    for inheriting_scope in ("inheriting_agent", "overriding_team", ROUTER_AGENT_NAME, None):
        assert config.resolve_scope(inheriting_scope).memory_backend == "none"
        assert config.resolve_scope(inheriting_scope).memory_search.mode == "keyword"
        assert config.resolve_scope(inheriting_scope).memory_search.include == ["notes/**/*.md"]


def test_model_name_resolution() -> None:
    config = _representative_config()

    assert config.resolve_entity("overriding_agent").model_name == "summary-model"
    assert config.resolve_entity("inheriting_agent").model_name == "default"
    assert config.resolve_entity("overriding_team").model_name == "default"
    assert config.resolve_entity(ROUTER_AGENT_NAME).model_name == "default"
    # The defaults scope of an ad hoc team has no authored model.
    assert not isinstance(config.resolve_scope(None), ResolvedEntity)


def test_tool_resolution() -> None:
    config = _representative_config()
    tools = config.resolve_agent("overriding_agent").tools

    assert tools.tool_names == tuple(Config.expand_tool_names(["calculator", "shell"]))
    assert [entry.name for entry in tools.authored] == ["calculator", "shell"]
    shell_entry = next(entry for entry in tools.authored if entry.name == "shell")
    assert shell_entry.defer is True
    assert shell_entry.initial is True
    assert shell_entry.tool_config_overrides == {"shell_path_prepend": ["/run/wrappers/bin"]}
    assert [entry.name for entry in tools.authored if entry.defer] == ["shell"]
    assert "shell" not in tools.scope_incompatible

    # The agent's toolkits receive its authored agent-only list joined into the tool's string setting.
    candidates = {entry.name: entry for entry in resolve_agent_tools(config, "overriding_agent").candidates()}
    assert candidates["shell"].tool_config_overrides == {"shell_path_prepend": "/run/wrappers/bin"}
    assert candidates["calculator"].tool_config_overrides == {}

    inheriting = config.resolve_agent("inheriting_agent").tools
    assert inheriting.tool_names == ()
    assert inheriting.authored == ()


def test_scope_resolution() -> None:
    config = _representative_config()

    assert config.resolve_agent("overriding_agent").policy.effective_execution_scope == "user_agent"
    assert config.resolve_agent("overriding_agent").policy.scope_label == "worker_scope=user_agent"
    assert config.resolve_agent("inheriting_agent").policy.effective_execution_scope is None
    assert config.resolve_agent("inheriting_agent").policy.scope_label == "unscoped"


def test_knowledge_resolution() -> None:
    config = _representative_config()

    assert config.resolve_agent("overriding_agent").knowledge_base_ids == ("engineering_docs",)
    assert config.resolve_agent("inheriting_agent").knowledge_base_ids == ()
    assert config.resolve_agent("overriding_agent").policy.private_knowledge_base_id is None


def test_unauthored_compaction_reports_not_authored() -> None:
    config = Config(
        agents={
            "plain_agent": AgentConfig(display_name="Plain Agent"),
            "compacting_agent": AgentConfig(
                display_name="Compacting Agent",
                compaction=CompactionOverrideConfig(enabled=True),
            ),
        },
        defaults=DefaultsConfig(tools=[], compaction=None),
        models={"default": ModelConfig(provider="openai", id="test-model")},
    )

    assert [
        config.resolve_scope(scope).history_settings.has_authored_compaction_config
        for scope in ("plain_agent", "compacting_agent", None)
    ] == [False, True, False]


def test_each_resolution_returns_a_fresh_record() -> None:
    config = _representative_config()

    assert config.resolve_agent("overriding_agent") is not config.resolve_agent("overriding_agent")


def test_the_defaults_scope_has_no_agent_record() -> None:
    assert not isinstance(_representative_config().resolve_scope(None), ResolvedAgent)


def test_a_team_has_no_agent_record() -> None:
    config = _representative_config()

    assert not isinstance(config.resolve_entity("overriding_team"), ResolvedAgent)
    with pytest.raises(ValueError, match="Unknown agent: overriding_team"):
        config.resolve_agent("overriding_team")


def test_unknown_entity_raises() -> None:
    """A name that is no agent, configured team, or the router raises instead of resolving to the defaults."""
    config = _representative_config()

    with pytest.raises(ValueError, match="Unknown entity: missing"):
        config.resolve_scope("missing")
    with pytest.raises(ValueError, match="Unknown entity: missing"):
        config.resolve_entity("missing")


def test_max_tool_calls_per_turn_resolution() -> None:
    config = Config(
        agents={
            "capped_agent": AgentConfig(display_name="Capped Agent", max_tool_calls_per_turn=40),
            "inheriting_agent": AgentConfig(display_name="Inheriting Agent"),
        },
        teams={
            "capped_team": TeamConfig(
                display_name="Capped Team",
                role="Team with a tool budget",
                agents=["capped_agent"],
                max_tool_calls_per_turn=12,
            ),
            "inheriting_team": TeamConfig(
                display_name="Inheriting Team",
                role="Team without a tool budget",
                agents=["inheriting_agent"],
            ),
        },
        defaults=DefaultsConfig(tools=[], max_tool_calls_per_turn=90),
        models={"default": ModelConfig(provider="openai", id="test-model")},
    )

    assert config.resolve_entity("capped_agent").max_tool_calls_per_turn == 40
    assert config.resolve_entity("capped_team").max_tool_calls_per_turn == 12
    for inheriting_scope in ("inheriting_agent", "inheriting_team", ROUTER_AGENT_NAME, None):
        assert config.resolve_scope(inheriting_scope).max_tool_calls_per_turn == 90


def test_max_tool_calls_per_turn_defaults_and_validation() -> None:
    assert DefaultsConfig(tools=[]).max_tool_calls_per_turn == 1000
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        DefaultsConfig(tools=[], max_tool_calls_per_turn=0)
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        AgentConfig(display_name="Broken", max_tool_calls_per_turn=0)


def test_file_access_defaults_to_workspace_and_agent_overrides_it() -> None:
    config = Config.model_validate(
        {
            "agents": {
                "plain": {"display_name": "Plain"},
                "admin": {"display_name": "Admin", "file_access": "unrestricted"},
            },
            "teams": {"crew": {"display_name": "Crew", "role": "r", "agents": ["plain"], "mode": "coordinate"}},
        },
    )
    assert config.resolve_scope(None).file_access == "workspace"
    assert config.resolve_entity("plain").file_access == "workspace"
    assert config.resolve_entity("admin").file_access == "unrestricted"
    assert config.resolve_entity("crew").file_access == "workspace"


def test_file_access_global_default_is_inherited() -> None:
    config = Config.model_validate(
        {
            "defaults": {"file_access": "unrestricted"},
            "agents": {
                "plain": {"display_name": "Plain"},
                "boxed": {"display_name": "Boxed", "file_access": "workspace"},
            },
        },
    )
    assert config.resolve_entity("plain").file_access == "unrestricted"
    assert config.resolve_entity("boxed").file_access == "workspace"


def test_file_access_rejects_unknown_values() -> None:
    with pytest.raises(ValueError, match="file_access"):
        Config.model_validate({"defaults": {"file_access": "readonly"}})
