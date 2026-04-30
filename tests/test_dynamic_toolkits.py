"""Tests for dynamic toolkit loading."""

from __future__ import annotations

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.agent._tools import determine_tools_for_model
from agno.models.message import Message
from agno.models.openai import OpenAIChat
from agno.run.agent import RunOutput
from agno.run.base import RunContext, RunStatus
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.ai import ai_response
from mindroom.api.openai_compat import _build_team, _derive_session_id
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.custom_tools.dynamic_tools import DynamicToolsToolkit
from mindroom.knowledge import KnowledgeResolution
from mindroom.memory import MemoryPromptParts
from mindroom.teams import materialize_exact_team_members
from mindroom.thread_utils import create_session_id
from mindroom.tool_system import dynamic_toolkits as dynamic_toolkits_module
from mindroom.tool_system.bootstrap import ensure_tool_registry_loaded
from mindroom.tool_system.catalog import ToolConfigOverrideError
from mindroom.tool_system.dynamic_toolkits import (
    DynamicToolkitConflictError,
    get_loaded_toolkits_for_session,
    merge_runtime_tool_configs,
    save_loaded_toolkits_for_session,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Return explicit runtime paths for one isolated dynamic-toolkit test."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def _base_config_data() -> dict[str, object]:
    """Return a minimal authored config payload for dynamic-toolkit tests."""
    return {
        "agents": {
            "code": {
                "display_name": "Code",
                "role": "Write code",
            },
        },
        "models": {
            "default": {
                "provider": "openai",
                "id": "gpt-4o-mini",
            },
        },
    }


@pytest.fixture(autouse=True)
def _clear_loaded_toolkits_state() -> Generator[None, None, None]:
    dynamic_toolkits_module._loaded_toolkits.clear()
    yield
    dynamic_toolkits_module._loaded_toolkits.clear()


def _validated_config(tmp_path: Path, raw: dict[str, object]) -> Config:
    """Validate one raw config payload against an isolated runtime."""
    return Config.validate_with_runtime(raw, _runtime_paths(tmp_path))


def _tool_payload(result: str) -> dict[str, object]:
    """Parse one JSON tool result payload."""
    return json.loads(result)


def _mock_openai_request(headers: dict[str, str] | None = None) -> MagicMock:
    """Create a minimal Request-like object for session-id derivation tests."""
    request = MagicMock()
    request.headers = {key.lower(): value for key, value in (headers or {}).items()}
    return request


def _tool_schema_count(agent: object, *, session_id: str) -> int:
    """Count the exported tool schemas for one fully built Agno agent."""
    run_output = RunOutput(
        run_id=f"run-{session_id}",
        agent_id="code",
        agent_name="Code",
        session_id=session_id,
        input="hello",
        content="ok",
    )
    run_context = RunContext(run_id=f"run-{session_id}", session_id=session_id)
    session = AgentSession(session_id=session_id, agent_id="code", created_at=1, updated_at=1)
    processed_tools = agent.get_tools(run_output, run_context, session)
    tools_for_model = determine_tools_for_model(
        agent=agent,
        model=agent.model,
        processed_tools=processed_tools,
        run_response=run_output,
        run_context=run_context,
        session=session,
    )
    return len(tools_for_model or [])


def test_config_accepts_dynamic_toolkit_agent_references(tmp_path: Path) -> None:
    """Agents may reference configured toolkits through allowed and initial lists."""
    raw = _base_config_data()
    raw["toolkits"] = {
        "development": {
            "description": "Coding tools",
            "tools": ["shell", "file"],
        },
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["agents"]["code"]["initial_toolkits"] = ["development"]

    config = Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert list(config.toolkits) == ["development"]
    assert config.agents["code"].allowed_toolkits == ["development"]
    assert config.agents["code"].initial_toolkits == ["development"]


def test_config_rejects_unknown_allowed_toolkit_reference(tmp_path: Path) -> None:
    """Unknown toolkit references should fail with an explicit config path."""
    raw = _base_config_data()
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]

    with pytest.raises(ValueError, match=r"Unknown toolkit 'development'") as exc_info:
        Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert "agents.code.allowed_toolkits[0]" in str(exc_info.value)


def test_config_rejects_unknown_initial_toolkit_reference(tmp_path: Path) -> None:
    """Unknown initial toolkits should fail with an explicit config path."""
    raw = _base_config_data()
    raw["toolkits"] = {"development": {"tools": ["shell"]}}
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["agents"]["code"]["initial_toolkits"] = ["research"]

    with pytest.raises(ValueError, match=r"Unknown toolkit 'research'") as exc_info:
        Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert "agents.code.initial_toolkits[0]" in str(exc_info.value)


def test_config_rejects_initial_toolkits_outside_allowed_toolkits(tmp_path: Path) -> None:
    """initial_toolkits must stay within allowed_toolkits."""
    raw = _base_config_data()
    raw["toolkits"] = {
        "development": {"tools": ["shell"]},
        "research": {"tools": ["duckduckgo"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["agents"]["code"]["initial_toolkits"] = ["research"]

    with pytest.raises(ValueError, match="initial_toolkits must be a subset of allowed_toolkits") as exc_info:
        Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert "agents.code.initial_toolkits" in str(exc_info.value)


def test_config_rejects_duplicate_tool_entries_inside_one_toolkit(tmp_path: Path) -> None:
    """Toolkit definitions should reject duplicate authored tool entries."""
    raw = _base_config_data()
    raw["toolkits"] = {"development": {"tools": ["shell", "shell"]}}

    with pytest.raises(ValueError, match="Duplicate toolkit tools are not allowed: shell"):
        Config.validate_with_runtime(raw, _runtime_paths(tmp_path))


@pytest.mark.parametrize("reserved_tool", ["delegate", "dynamic_tools", "self_config"])
def test_config_rejects_reserved_control_plane_tools_in_toolkits(
    tmp_path: Path,
    reserved_tool: str,
) -> None:
    """Toolkits may not include control-plane tools that are injected specially."""
    raw = _base_config_data()
    raw["toolkits"] = {"development": {"tools": [reserved_tool]}}

    with pytest.raises(ValueError, match=rf"reserved control-plane tool '{reserved_tool}'") as exc_info:
        Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert f"toolkits.development.tools[0].{reserved_tool}" in str(exc_info.value)


def test_config_rejects_toolkit_tools_that_are_not_runtime_loadable(tmp_path: Path) -> None:
    """Dynamic toolkits should only allow tools that resolve through the normal registry."""
    raw = _base_config_data()
    raw["toolkits"] = {"development": {"tools": ["memory"]}}

    with pytest.raises(ValueError, match=r"'memory' is not supported") as exc_info:
        Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert "toolkits.development.tools[0].memory" in str(exc_info.value)


def test_config_rejects_scope_incompatible_dynamic_toolkits_for_isolating_scope(tmp_path: Path) -> None:
    """Agents with isolating worker scopes must not allow shared-only dynamic toolkits."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {"mail": {"tools": ["homeassistant"]}}
    raw["agents"]["code"]["worker_scope"] = "user"
    raw["agents"]["code"]["allowed_toolkits"] = ["mail"]

    with pytest.raises(ValueError, match=r"code -> toolkit 'mail' -> homeassistant \(worker_scope=user\)"):
        Config.validate_with_runtime(raw, _runtime_paths(tmp_path))


def test_get_toolkit_tool_configs_expands_implied_tools(tmp_path: Path) -> None:
    """Toolkit resolution should preserve the existing implied-tool expansion rules."""
    raw = _base_config_data()
    raw["toolkits"] = {
        "messaging": {
            "tools": ["matrix_message"],
        },
    }

    config = Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert [entry.name for entry in config.get_toolkit_tool_configs("messaging")] == [
        "matrix_message",
        "attachments",
        "matrix_room",
    ]


def test_get_toolkit_tool_configs_expands_openclaw_compat_bundle(tmp_path: Path) -> None:
    """Dynamic toolkits should accept the registered OpenClaw compat bundle entry."""
    raw = _base_config_data()
    raw["toolkits"] = {
        "compat": {
            "tools": ["openclaw_compat"],
        },
    }

    config = Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert [entry.name for entry in config.get_toolkit_tool_configs("compat")] == [
        "openclaw_compat",
        "shell",
        "coding",
        "duckduckgo",
        "website",
        "browser",
        "scheduler",
        "subagents",
        "matrix_message",
        "attachments",
        "matrix_room",
    ]


def test_config_round_trips_toolkits_in_authored_dump(tmp_path: Path) -> None:
    """Authored serialization should preserve toolkit definitions."""
    raw = _base_config_data()
    raw["toolkits"] = {
        "development": {
            "description": "Coding tools",
            "tools": [{"shell": {"shell_path_prepend": "/tmp/bin"}}, "file"],  # noqa: S108
        },
    }

    config = Config.validate_with_runtime(deepcopy(raw), _runtime_paths(tmp_path))

    assert config.authored_model_dump()["toolkits"] == raw["toolkits"]


def test_config_accepts_and_round_trips_mcp_servers(tmp_path: Path) -> None:
    """The config schema should preserve authored MCP server definitions."""
    raw = _base_config_data()
    raw["mcp_servers"] = {
        "filesystem": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
        },
    }

    config = Config.validate_with_runtime(deepcopy(raw), _runtime_paths(tmp_path))

    assert config.mcp_servers["filesystem"].transport == "stdio"
    assert config.mcp_servers["filesystem"].command == "npx"
    assert config.mcp_servers["filesystem"].args == ["-y", "@modelcontextprotocol/server-filesystem", "."]
    assert config.authored_model_dump()["mcp_servers"] == raw["mcp_servers"]


def test_config_rejects_invalid_mcp_server_name(tmp_path: Path) -> None:
    """MCP server names should follow the same identifier rules as agents and teams."""
    raw = _base_config_data()
    raw["mcp_servers"] = {
        "filesystem-server": {
            "transport": "stdio",
            "command": "npx",
        },
    }

    with pytest.raises(
        ValueError,
        match=r"Agent, team, and MCP server names must be alphanumeric/underscore only",
    ) as exc_info:
        Config.validate_with_runtime(raw, _runtime_paths(tmp_path))

    assert "filesystem-server" in str(exc_info.value)


def test_dynamic_toolkit_override_normalization_uses_mcp_specific_validation(tmp_path: Path) -> None:
    """Dynamic toolkit merging should reject invalid MCP override payloads consistently."""
    raw = _base_config_data()
    raw["mcp_servers"] = {
        "demo": {
            "transport": "stdio",
            "command": "python",
            "args": ["-c", "print(0)"],
        },
    }
    raw["agents"]["code"]["tools"] = ["mcp_demo"]
    config = _validated_config(tmp_path, raw)
    ensure_tool_registry_loaded(_runtime_paths(tmp_path), config)

    with pytest.raises(ToolConfigOverrideError, match="include_tools and exclude_tools overlap"):
        dynamic_toolkits_module._normalize_effective_tool_config_overrides(
            "mcp_demo",
            {
                "include_tools": ["echo"],
                "exclude_tools": ["echo"],
            },
        )


def test_dynamic_toolkit_session_initializes_from_initial_toolkits(tmp_path: Path) -> None:
    """First session access should persist the configured initial toolkit set."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "development": {"tools": ["shell"]},
        "research": {"tools": ["duckduckgo"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["research", "development"]
    raw["agents"]["code"]["initial_toolkits"] = ["development"]
    config = _validated_config(tmp_path, raw)
    loaded = get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-1")

    assert loaded == ["development"]
    assert dynamic_toolkits_module._loaded_toolkits["session-1"] == ["development"]


def test_dynamic_toolkit_session_isolation_is_per_session_id(tmp_path: Path) -> None:
    """Different session IDs should not share loaded toolkit state."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "development": {"tools": ["shell"]},
        "research": {"tools": ["duckduckgo"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["development", "research"]
    config = _validated_config(tmp_path, raw)

    save_loaded_toolkits_for_session(
        session_id="session-a",
        loaded_toolkits=["research"],
    )
    save_loaded_toolkits_for_session(
        session_id="session-b",
        loaded_toolkits=["development"],
    )

    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-a") == [
        "research",
    ]
    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-b") == [
        "development",
    ]


def test_dynamic_toolkit_room_level_matrix_messages_share_room_scoped_state(tmp_path: Path) -> None:
    """Room-level Matrix turns should share toolkit state even when event ids differ."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "research": {"tools": ["duckduckgo"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    config = _validated_config(tmp_path, raw)
    first_session_id = create_session_id("!room:example.org", "$event-a:example.org")
    second_session_id = create_session_id("!room:example.org", "$event-b:example.org")

    save_loaded_toolkits_for_session(
        session_id=first_session_id,
        loaded_toolkits=["research"],
    )

    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id=second_session_id) == [
        "research",
    ]
    assert dynamic_toolkits_module._loaded_toolkits["!room:example.org"] == ["research"]


def test_dynamic_toolkit_session_reorders_loaded_toolkits_to_allowed_order(tmp_path: Path) -> None:
    """Persisted toolkit names should be canonicalized to allowed_toolkits order."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "development": {"tools": ["shell"]},
        "research": {"tools": ["duckduckgo"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["development", "research"]
    config = _validated_config(tmp_path, raw)
    save_loaded_toolkits_for_session(
        session_id="session-1",
        loaded_toolkits=["research", "development"],
    )

    loaded = get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-1")

    assert loaded == ["development", "research"]
    assert dynamic_toolkits_module._loaded_toolkits["session-1"] == ["development", "research"]


def test_dynamic_toolkit_session_drops_stale_toolkit_refs(tmp_path: Path) -> None:
    """Removed or disallowed toolkit names should be scrubbed from in-memory state."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "development": {"tools": ["shell"]},
        "research": {"tools": ["duckduckgo"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["development", "research"]
    config = _validated_config(tmp_path, raw)
    save_loaded_toolkits_for_session(
        session_id="session-1",
        loaded_toolkits=["stale", "research"],
    )

    loaded = get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-1")

    assert loaded == ["research"]
    assert dynamic_toolkits_module._loaded_toolkits["session-1"] == ["research"]


def test_dynamic_toolkit_merge_deduplicates_static_and_dynamic_tools(tmp_path: Path) -> None:
    """A dynamically loaded tool should not duplicate an identical static definition."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["tools"] = ["shell"]
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["agents"]["code"]["initial_toolkits"] = ["development"]
    raw["toolkits"] = {
        "development": {
            "tools": ["shell", "file"],
        },
    }
    config = _validated_config(tmp_path, raw)

    merged_tool_configs = merge_runtime_tool_configs(
        agent_name="code",
        config=config,
        loaded_toolkits=["development"],
        enable_dynamic_tools_manager=False,
    )

    assert [entry.name for entry in merged_tool_configs] == [
        "shell",
        "file",
    ]


def test_dynamic_toolkit_merge_rejects_conflicting_overrides(tmp_path: Path) -> None:
    """Loading a toolkit should fail when it redefines an active tool incompatibly."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["tools"] = [{"shell": {"shell_path_prepend": "/static/bin"}}]
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["agents"]["code"]["initial_toolkits"] = ["development"]
    raw["toolkits"] = {
        "development": {
            "tools": [{"shell": {"shell_path_prepend": "/dynamic/bin"}}],
        },
    }
    config = _validated_config(tmp_path, raw)

    with pytest.raises(DynamicToolkitConflictError, match="conflicts on tool 'shell'") as exc_info:
        merge_runtime_tool_configs(
            agent_name="code",
            config=config,
            loaded_toolkits=["development"],
            enable_dynamic_tools_manager=False,
        )

    assert exc_info.value.toolkit_name == "development"
    assert exc_info.value.tool_name == "shell"


def test_dynamic_toolkit_merge_accepts_equivalent_static_and_dynamic_overrides(tmp_path: Path) -> None:
    """Equivalent authored override forms should not conflict across static and dynamic sources."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["tools"] = [{"shell": {"shell_path_prepend": ["/a", "/b"]}}]
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["agents"]["code"]["initial_toolkits"] = ["development"]
    raw["toolkits"] = {
        "development": {
            "tools": [{"shell": {"shell_path_prepend": "/a, /b"}}],
        },
    }
    config = _validated_config(tmp_path, raw)

    merged_tool_configs = merge_runtime_tool_configs(
        agent_name="code",
        config=config,
        loaded_toolkits=["development"],
        enable_dynamic_tools_manager=False,
    )

    assert [entry.name for entry in merged_tool_configs] == ["shell"]
    assert merged_tool_configs[0].tool_config_overrides == {"shell_path_prepend": "/a, /b"}


def test_dynamic_toolkit_merge_accepts_equivalent_dynamic_overrides(tmp_path: Path) -> None:
    """Equivalent authored override forms should not conflict across dynamic toolkits."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["allowed_toolkits"] = ["first", "second"]
    raw["agents"]["code"]["initial_toolkits"] = ["first", "second"]
    raw["toolkits"] = {
        "first": {
            "tools": [{"shell": {"shell_path_prepend": ["/a", "/b"]}}],
        },
        "second": {
            "tools": [{"shell": {"shell_path_prepend": "/a, /b"}}],
        },
    }
    config = _validated_config(tmp_path, raw)

    merged_tool_configs = merge_runtime_tool_configs(
        agent_name="code",
        config=config,
        loaded_toolkits=["first", "second"],
        enable_dynamic_tools_manager=False,
    )

    assert [entry.name for entry in merged_tool_configs] == ["shell"]
    assert merged_tool_configs[0].tool_config_overrides == {"shell_path_prepend": "/a, /b"}


def test_team_members_with_same_session_id_share_dynamic_toolkit_state(tmp_path: Path) -> None:
    """In-memory toolkit state is shared by session_id across agent rebuilds."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {"research": {"tools": ["sleep"]}}
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    raw["agents"]["worker"] = {
        "display_name": "Worker",
        "role": "Help with work",
        "allowed_toolkits": ["research"],
    }
    config = _validated_config(tmp_path, raw)

    save_loaded_toolkits_for_session(
        session_id="team-session",
        loaded_toolkits=["research"],
    )

    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="team-session") == ["research"]
    assert get_loaded_toolkits_for_session(agent_name="worker", config=config, session_id="team-session") == [
        "research",
    ]


def test_dynamic_tools_manager_lists_allowed_toolkits_with_loaded_and_sticky_state(tmp_path: Path) -> None:
    """list_toolkits should expose the allowed catalog and current session state."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "development": {"description": "Code tools", "tools": ["sleep"]},
        "research": {"description": "Search tools", "tools": ["duckduckgo"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["development", "research"]
    raw["agents"]["code"]["initial_toolkits"] = ["development"]
    config = _validated_config(tmp_path, raw)
    manager = DynamicToolsToolkit(
        agent_name="code",
        config=config,
        session_id="session-1",
    )

    payload = _tool_payload(manager.list_toolkits())

    assert payload["status"] == "ok"
    assert payload["loaded_toolkits"] == ["development"]
    assert payload["toolkits"] == [
        {
            "description": "Code tools",
            "loaded": True,
            "name": "development",
            "sticky": True,
            "tool_names": ["sleep"],
        },
        {
            "description": "Search tools",
            "loaded": False,
            "name": "research",
            "sticky": False,
            "tool_names": ["duckduckgo"],
        },
    ]


def test_dynamic_tools_manager_load_and_unload_cycle_returns_structured_statuses(tmp_path: Path) -> None:
    """load_tools and unload_tools should persist changes with explicit outcome categories."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "research": {"tools": ["duckduckgo"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    config = _validated_config(tmp_path, raw)
    manager = DynamicToolsToolkit(
        agent_name="code",
        config=config,
        session_id="session-1",
    )

    loaded_payload = _tool_payload(manager.load_tools("research"))
    already_loaded_payload = _tool_payload(manager.load_tools("research"))
    unloaded_payload = _tool_payload(manager.unload_tools("research"))
    not_loaded_payload = _tool_payload(manager.unload_tools("research"))

    assert loaded_payload["status"] == "loaded"
    assert loaded_payload["loaded_toolkits"] == ["research"]
    assert loaded_payload["takes_effect"] == "next_request"
    assert already_loaded_payload["status"] == "already_loaded"
    assert already_loaded_payload["loaded_toolkits"] == ["research"]
    assert unloaded_payload["status"] == "unloaded"
    assert unloaded_payload["loaded_toolkits"] == []
    assert unloaded_payload["takes_effect"] == "next_request"
    assert not_loaded_payload["status"] == "not_loaded"
    assert not_loaded_payload["loaded_toolkits"] == []
    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-1") == []


def test_dynamic_tools_manager_reports_unknown_not_allowed_and_conflict_statuses(tmp_path: Path) -> None:
    """load_tools should surface validation and merge failures without mutating state."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["tools"] = [{"shell": {"shell_path_prepend": "/static/bin"}}]
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["toolkits"] = {
        "development": {"tools": [{"shell": {"shell_path_prepend": "/dynamic/bin"}}]},
        "research": {"tools": ["duckduckgo"]},
    }
    config = _validated_config(tmp_path, raw)
    manager = DynamicToolsToolkit(
        agent_name="code",
        config=config,
        session_id="session-1",
    )

    unknown_payload = _tool_payload(manager.load_tools("missing"))
    not_allowed_payload = _tool_payload(manager.load_tools("research"))
    conflict_payload = _tool_payload(manager.load_tools("development"))

    assert unknown_payload["status"] == "unknown"
    assert not_allowed_payload["status"] == "not_allowed"
    assert conflict_payload["status"] == "conflict"
    assert conflict_payload["conflicting_tool"] == "shell"
    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-1") == []


def test_dynamic_tools_manager_rejects_scope_incompatible_toolkit_loads(tmp_path: Path) -> None:
    """load_tools should reject toolkits whose contents cannot run for the agent scope."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "mail": {"tools": ["homeassistant"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["mail"]
    config = _validated_config(tmp_path, raw)
    config.agents["code"].worker_scope = "user_agent"
    manager = DynamicToolsToolkit(
        agent_name="code",
        config=config,
        session_id="session-1",
    )

    payload = _tool_payload(manager.load_tools("mail"))

    assert payload["status"] == "scope_incompatible"
    assert payload["scope_label"] == "worker_scope=user_agent"
    assert payload["toolkit"] == "mail"
    assert payload["unsupported_tools"] == ["homeassistant"]
    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-1") == []


def test_dynamic_tools_manager_refuses_to_unload_sticky_initial_toolkits(tmp_path: Path) -> None:
    """initial_toolkits should remain sticky at runtime."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {
        "development": {"tools": ["sleep"]},
    }
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["agents"]["code"]["initial_toolkits"] = ["development"]
    config = _validated_config(tmp_path, raw)
    manager = DynamicToolsToolkit(
        agent_name="code",
        config=config,
        session_id="session-1",
    )

    payload = _tool_payload(manager.unload_tools("development"))

    assert payload["status"] == "sticky"
    assert payload["loaded_toolkits"] == ["development"]
    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-1") == [
        "development",
    ]


def test_create_agent_uses_session_loaded_dynamic_toolkits_and_injects_prompt_block(tmp_path: Path) -> None:
    """create_agent should rebuild tools from the current session's loaded toolkit state."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    raw["toolkits"] = {
        "research": {"description": "Search toolkit", "tools": ["sleep"]},
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    model = MagicMock()
    model.id = "gpt-4o-mini"

    with (
        patch("mindroom.model_loading.get_model_instance", return_value=model),
        patch("mindroom.agents.Agent") as mock_agent_class,
    ):
        create_agent(
            "code",
            config,
            runtime_paths,
            execution_identity=None,
            session_id="session-1",
            include_interactive_questions=False,
        )
        initial_tools = mock_agent_class.call_args.kwargs["tools"]
        initial_instructions = mock_agent_class.call_args.kwargs["instructions"]

        manager = next(tool for tool in initial_tools if tool.name == "dynamic_tools")
        assert _tool_payload(manager.load_tools("research"))["status"] == "loaded"

        create_agent(
            "code",
            config,
            runtime_paths,
            execution_identity=None,
            session_id="session-1",
            include_interactive_questions=False,
        )
        loaded_tools = mock_agent_class.call_args.kwargs["tools"]
        loaded_instructions = mock_agent_class.call_args.kwargs["instructions"]

    assert [tool.name for tool in initial_tools] == ["dynamic_tools"]
    assert any("Currently loaded: (none)" in instruction for instruction in initial_instructions)
    assert [tool.name for tool in loaded_tools] == ["sleep", "dynamic_tools"]
    assert any("Currently loaded: research" in instruction for instruction in loaded_instructions)
    loaded_instruction_text = "\n".join(str(instruction) for instruction in loaded_instructions)
    assert "load_tools" in loaded_instruction_text
    assert "unload_tools" in loaded_instruction_text
    assert "next request" in loaded_instruction_text
    assert "each member manages its own toolkit state" in loaded_instruction_text
    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id="session-1") == [
        "research",
    ]


def test_create_agent_without_session_id_skips_dynamic_tools_and_prompt_block(tmp_path: Path) -> None:
    """Sessionless builds used by delegation should not expose the dead manager tool."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    raw["toolkits"] = {
        "research": {"description": "Search toolkit", "tools": ["sleep"]},
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    model = MagicMock()
    model.id = "gpt-4o-mini"

    with (
        patch("mindroom.model_loading.get_model_instance", return_value=model),
        patch("mindroom.agents.Agent") as mock_agent_class,
    ):
        create_agent(
            "code",
            config,
            runtime_paths,
            execution_identity=None,
            session_id=None,
            include_interactive_questions=False,
            delegation_depth=1,
        )

    assert mock_agent_class.call_args.kwargs["tools"] == []
    assert not any(
        "## Dynamic Toolkits" in str(instruction) for instruction in mock_agent_class.call_args.kwargs["instructions"]
    )


def test_create_agent_reuses_saved_in_memory_toolkits_across_calls(tmp_path: Path) -> None:
    """Saving one session's loaded toolkits should affect the next create_agent call for that session."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    raw["toolkits"] = {
        "research": {"tools": ["sleep"]},
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    model = MagicMock()
    model.id = "gpt-4o-mini"

    save_loaded_toolkits_for_session(session_id="session-1", loaded_toolkits=["research"])

    with (
        patch("mindroom.model_loading.get_model_instance", return_value=model),
        patch("mindroom.agents.Agent") as mock_agent_class,
    ):
        create_agent(
            "code",
            config,
            runtime_paths,
            execution_identity=None,
            session_id="session-1",
            include_interactive_questions=False,
        )

    assert [tool.name for tool in mock_agent_class.call_args.kwargs["tools"]] == ["sleep", "dynamic_tools"]


def test_create_agent_uses_dynamic_runtime_worker_routing(tmp_path: Path) -> None:
    """Dynamically loaded worker tools should be routed through the runtime worker set."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["allowed_toolkits"] = ["development"]
    raw["agents"]["code"]["initial_toolkits"] = ["development"]
    raw["toolkits"] = {
        "development": {"tools": ["shell"]},
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    model = MagicMock()
    model.id = "gpt-4o-mini"

    def _fake_toolkit(tool_name: str, **_kwargs: object) -> MagicMock:
        toolkit = MagicMock()
        toolkit.name = tool_name
        return toolkit

    with (
        patch("mindroom.model_loading.get_model_instance", return_value=model),
        patch("mindroom.agents.build_agent_toolkit", side_effect=_fake_toolkit) as mock_build_agent_toolkit,
        patch("mindroom.agents.prepend_tool_hook_bridge", side_effect=lambda toolkit, _bridge: toolkit),
        patch("mindroom.agents.Agent"),
    ):
        create_agent(
            "code",
            config,
            runtime_paths,
            execution_identity=None,
            session_id="session-1",
            include_interactive_questions=False,
        )

    worker_tools_by_tool = {
        call.args[0]: call.kwargs["worker_tools"] for call in mock_build_agent_toolkit.call_args_list
    }
    assert worker_tools_by_tool["shell"] == ["shell"]
    assert worker_tools_by_tool["dynamic_tools"] == ["shell"]


def test_create_agent_tool_schema_count_grows_after_loading_toolkit(tmp_path: Path) -> None:
    """Dynamic loading should increase exported tool schemas on the next rebuilt request."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    raw["toolkits"] = {
        "research": {"tools": ["sleep"]},
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    before_agent = create_agent(
        "code",
        config,
        runtime_paths,
        execution_identity=None,
        session_id="session-1",
        include_interactive_questions=False,
    )
    save_loaded_toolkits_for_session(
        session_id="session-1",
        loaded_toolkits=["research"],
    )
    after_agent = create_agent(
        "code",
        config,
        runtime_paths,
        execution_identity=None,
        session_id="session-1",
        include_interactive_questions=False,
    )

    before_count = _tool_schema_count(before_agent, session_id="session-1")
    after_count = _tool_schema_count(after_agent, session_id="session-1")

    assert [tool.name for tool in before_agent.tools] == ["dynamic_tools"]
    assert [tool.name for tool in after_agent.tools] == ["sleep", "dynamic_tools"]
    assert before_count == 3
    assert after_count == 4


@pytest.mark.asyncio
async def test_ai_response_rebuilds_agent_with_loaded_dynamic_toolkits(tmp_path: Path) -> None:
    """The non-streaming AI path should rebuild agents from the session-scoped toolkit state."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["agents"]["code"]["include_default_tools"] = False
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    raw["toolkits"] = {
        "research": {"description": "Search toolkit", "tools": ["sleep"]},
    }
    config = _validated_config(tmp_path, raw)
    runtime_paths = _runtime_paths(tmp_path)
    save_loaded_toolkits_for_session(
        session_id="session-1",
        loaded_toolkits=["research"],
    )
    model = OpenAIChat(id="gpt-4o-mini", api_key="sk-test")
    prepared_execution = SimpleNamespace(
        messages=(Message(role="user", content="enhanced prompt"),),
        replay_plan=None,
        unseen_event_ids=[],
        replays_persisted_history=False,
        compaction_outcomes=[],
        compaction_decision=None,
        post_response_compaction_checks=[],
    )
    run_output = MagicMock()
    run_output.content = "ok"
    run_output.tools = None
    run_output.status = RunStatus.completed

    with (
        patch(
            "mindroom.ai.build_memory_prompt_parts",
            new_callable=AsyncMock,
            return_value=MemoryPromptParts(),
        ),
        patch(
            "mindroom.ai.prepare_agent_execution_context",
            new_callable=AsyncMock,
            return_value=prepared_execution,
        ),
        patch("mindroom.model_loading.get_model_instance", return_value=model),
        patch("mindroom.ai_runtime.cached_agent_run", new_callable=AsyncMock, return_value=run_output) as mock_run,
    ):
        response = await ai_response(
            agent_name="code",
            prompt="Use the toolkit",
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            include_interactive_questions=False,
        )

    built_agent = mock_run.call_args.args[0]

    assert response == "ok"
    assert [tool.name for tool in built_agent.tools] == ["sleep", "dynamic_tools"]
    assert any("Currently loaded: research" in str(instruction) for instruction in built_agent.instructions)


def test_create_agent_uses_scope_context_storage(tmp_path: Path) -> None:
    """The agent constructor should reuse the caller-provided history storage."""
    config = _validated_config(tmp_path, _base_config_data())
    runtime_paths = _runtime_paths(tmp_path)
    storage = create_session_storage("code", config, runtime_paths, execution_identity=None)

    try:
        agent = create_agent(
            "code",
            config,
            runtime_paths,
            execution_identity=None,
            history_storage=storage,
            include_interactive_questions=False,
        )
    finally:
        storage.close()

    assert agent.db is storage


def test_team_builder_passes_team_session_id_to_create_agent(tmp_path: Path) -> None:
    """Team member creation should share the team session id across member agents."""
    config = _validated_config(tmp_path, _base_config_data())
    runtime_paths = _runtime_paths(tmp_path)
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@user:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="team-session",
    )

    with (
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch("mindroom.teams.create_agent", return_value=MagicMock(name="CodeAgent")) as mock_create_agent,
    ):
        result = materialize_exact_team_members(
            ["code"],
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        ).agents[0]

    assert result is mock_create_agent.return_value
    assert mock_create_agent.call_args.kwargs["session_id"] == "team-session"
    assert mock_create_agent.call_args.kwargs["include_openai_compat_guidance"] is False


def test_openai_team_builder_passes_session_id_to_member_agents(tmp_path: Path) -> None:
    """OpenAI-compatible team requests should reuse one shared session id for all members."""
    config = Config(
        agents={
            "code": AgentConfig(
                display_name="Code",
                role="Write code",
                rooms=[],
            ),
        },
        models={"default": ModelConfig(provider="openai", id="gpt-4o-mini")},
        router=RouterConfig(model="default"),
        teams={
            "dev": TeamConfig(
                display_name="Dev",
                role="Development team",
                agents=["code"],
                mode="coordinate",
            ),
        },
    )
    runtime_paths = _runtime_paths(tmp_path)

    with (
        patch("mindroom.teams.create_agent", return_value=MagicMock(name="CodeAgent")) as mock_create,
        patch("mindroom.teams.resolve_agent_knowledge_access", return_value=KnowledgeResolution(knowledge=None)),
        patch(
            "mindroom.api.openai_compat.resolve_bound_team_scope_context",
            create=True,
            return_value=SimpleNamespace(scope=SimpleNamespace(scope_id="dev"), storage=MagicMock()),
        ),
        patch("agno.team.Team.__init__", return_value=None),
    ):
        _build_team(
            "dev",
            config,
            runtime_paths,
            execution_identity=None,
            session_id="openai-team-session",
        )

    assert mock_create.call_args.kwargs["session_id"] == "openai-team-session"
    assert mock_create.call_args.kwargs["include_openai_compat_guidance"] is True


def test_openai_derived_stable_session_id_preserves_dynamic_toolkit_state(tmp_path: Path) -> None:
    """Stable OpenAI-compatible session ids should reuse the same in-memory toolkit state."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {"research": {"tools": ["duckduckgo"]}}
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    config = _validated_config(tmp_path, raw)

    stable_sid = _derive_session_id("code", _mock_openai_request({"X-Session-Id": "chat-1"}))
    save_loaded_toolkits_for_session(
        session_id=stable_sid,
        loaded_toolkits=["research"],
    )

    assert get_loaded_toolkits_for_session(
        agent_name="code",
        config=config,
        session_id=_derive_session_id("code", _mock_openai_request({"X-Session-Id": "chat-1"})),
    ) == ["research"]


def test_openai_ephemeral_fallback_session_ids_do_not_share_dynamic_toolkit_state(tmp_path: Path) -> None:
    """Derived fallback ids should remain per-request and not cross-contaminate state."""
    raw = _base_config_data()
    raw["defaults"] = {"tools": []}
    raw["toolkits"] = {"research": {"tools": ["duckduckgo"]}}
    raw["agents"]["code"]["allowed_toolkits"] = ["research"]
    config = _validated_config(tmp_path, raw)

    first_sid = _derive_session_id("code", _mock_openai_request())
    second_sid = _derive_session_id("code", _mock_openai_request())
    save_loaded_toolkits_for_session(
        session_id=first_sid,
        loaded_toolkits=["research"],
    )

    assert first_sid != second_sid
    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id=first_sid) == [
        "research",
    ]
    assert get_loaded_toolkits_for_session(agent_name="code", config=config, session_id=second_sid) == []
