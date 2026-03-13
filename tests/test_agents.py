"""Tests for MindRoom agent functionality."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock, patch

import pytest
from agno.agent import Agent
from agno.learn import LearningMachine, LearningMode, UserMemoryConfig, UserProfileConfig
from pydantic import ValidationError

from mindroom import agent_prompts
from mindroom.agents import _CULTURE_MANAGER_CACHE, create_agent
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentPrivateKnowledgeConfig, CultureConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.credentials import CredentialsManager, load_scoped_credentials
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_key,
    tool_execution_identity,
    worker_root_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.worker_routing import WorkerScope


@patch("mindroom.agents.SqliteDb")
def test_get_agent_calculator(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the calculator agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("calculator", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "CalculatorAgent"


@patch("mindroom.agents.SqliteDb")
def test_get_agent_general(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the general agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("general", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "GeneralAgent"
    assert isinstance(agent.learning, LearningMachine)
    assert isinstance(agent.learning.user_profile, UserProfileConfig)
    assert agent.learning.user_profile.mode is LearningMode.ALWAYS
    assert isinstance(agent.learning.user_memory, UserMemoryConfig)
    assert agent.learning.user_memory.mode is LearningMode.ALWAYS


@patch("mindroom.agents.SqliteDb")
def test_hidden_tool_calls_prompt_is_injected(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Agents with hidden tool calls get a prompt hint to avoid narrating tool usage."""
    config = Config.from_yaml()
    config.agents["general"].show_tool_calls = False

    agent = create_agent("general", config=config)

    assert agent_prompts.HIDDEN_TOOL_CALLS_PROMPT in agent.instructions


@patch("mindroom.agents.SqliteDb")
def test_scheduler_tool_enabled_by_default(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """All agents should get the scheduler tool even when not explicitly configured."""
    config = Config.from_yaml()
    config.agents["summary"].tools = []

    agent = create_agent("summary", config=config)
    tool_names = [tool.name for tool in agent.tools]

    assert "scheduler" in tool_names


@patch("mindroom.agents.SqliteDb")
def test_configurable_default_tools_are_applied(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """defaults.tools should be merged into every agent's configured tools."""
    config = Config.from_yaml()
    config.defaults.tools = ["scheduler", "calculator"]
    config.agents["summary"].tools = []

    agent = create_agent("summary", config=config)
    tool_names = [tool.name for tool in agent.tools]

    assert "scheduler" in tool_names
    assert "calculator" in tool_names


@patch("mindroom.agents.SqliteDb")
def test_default_tools_do_not_duplicate_agent_tools(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """An agent tool already present should not be duplicated by defaults.tools."""
    config = Config.from_yaml()
    config.defaults.tools = ["scheduler"]
    config.agents["summary"].tools = ["scheduler"]

    agent = create_agent("summary", config=config)
    tool_names = [tool.name for tool in agent.tools]

    assert tool_names.count("scheduler") == 1


@patch("mindroom.agents.SqliteDb")
def test_agent_include_default_tools_false_skips_config_defaults(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Agent include_default_tools=False should skip defaults.tools entirely."""
    config = Config.from_yaml()
    config.defaults.tools = ["scheduler", "calculator"]
    config.agents["summary"].tools = []
    config.agents["summary"].include_default_tools = False

    agent = create_agent("summary", config=config)
    tool_names = [tool.name for tool in agent.tools]

    assert "scheduler" not in tool_names
    assert "calculator" not in tool_names


def test_openclaw_compat_expands_to_implied_tools() -> None:
    """openclaw_compat should stay in the list and bring its implied tools."""
    config = Config.from_yaml()
    config.agents["summary"].tools = ["openclaw_compat"]
    config.agents["summary"].include_default_tools = False

    assert config.get_agent_tools("summary") == [
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
    ]


def test_openclaw_compat_expansion_dedupes_preserving_order() -> None:
    """Implied tool expansion should preserve first-seen order while deduping entries."""
    config = Config.from_yaml()
    config.agents["summary"].tools = [
        "browser",
        "openclaw_compat",
        "shell",
        "coding",
        "browser",
    ]
    config.defaults.tools = ["openclaw_compat", "python", "scheduler"]

    assert config.get_agent_tools("summary") == [
        "browser",
        "openclaw_compat",
        "shell",
        "coding",
        "python",
        "scheduler",
        "duckduckgo",
        "website",
        "subagents",
        "matrix_message",
        "attachments",
    ]


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_uses_native_tool_lookups_for_openclaw_compat(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
) -> None:
    """Agent construction should look up openclaw_compat and all its implied tools."""
    mock_get_tool_by_name.return_value = MagicMock()
    config = Config.from_yaml()
    config.agents["summary"].tools = ["openclaw_compat"]
    config.agents["summary"].include_default_tools = False

    create_agent("summary", config=config)

    looked_up_tools = [call.args[0] for call in mock_get_tool_by_name.call_args_list]
    assert looked_up_tools == config.get_agent_tools("summary")


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_continues_when_implied_tool_import_fails(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
) -> None:
    """Optional dependency import failures should not abort agent creation with implied tools."""

    def _lookup_tool(
        name: str,
        *,
        tool_init_overrides: dict[str, object] | None = None,
        worker_tools_override: list[str] | None = None,
        worker_scope: WorkerScope | None = None,
        routing_agent_name: str | None = None,
    ) -> MagicMock:
        del tool_init_overrides, worker_tools_override, worker_scope, routing_agent_name
        if name == "browser":
            missing_dependency_message = "No module named 'playwright'"
            raise ImportError(missing_dependency_message)
        tool = MagicMock()
        tool.name = name
        return tool

    mock_get_tool_by_name.side_effect = _lookup_tool

    config = Config.from_yaml()
    config.agents["summary"].tools = ["openclaw_compat"]
    config.agents["summary"].include_default_tools = False

    agent = create_agent("summary", config=config)

    tool_names = [tool.name for tool in agent.tools]
    assert "browser" not in tool_names
    assert "openclaw_compat" in tool_names
    assert "shell" in tool_names
    assert "matrix_message" in tool_names


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_expands_openclaw_compat_for_worker_tool_overrides(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
) -> None:
    """Worker override list should receive expanded tool names including openclaw_compat."""
    mock_get_tool_by_name.return_value = MagicMock()
    config = Config.from_yaml()
    config.agents["summary"].tools = ["openclaw_compat"]
    config.agents["summary"].include_default_tools = False
    config.agents["summary"].worker_tools = ["openclaw_compat"]

    create_agent("summary", config=config)

    expected_worker_tools = config.expand_tool_names(["openclaw_compat"])
    worker_overrides = [call.kwargs["worker_tools_override"] for call in mock_get_tool_by_name.call_args_list]
    assert worker_overrides
    assert all(override == expected_worker_tools for override in worker_overrides)


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_uses_memory_file_workspace_for_base_dir_tools(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
    tmp_path: Path,
) -> None:
    """base_dir-aware tools should use the agent workspace from memory_file_path."""
    mock_get_tool_by_name.return_value = MagicMock()

    workspace = tmp_path / "mind_data"
    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = str(workspace)
    config.agents["general"].tools = ["coding", "shell", "duckduckgo"]
    config.agents["general"].include_default_tools = False

    create_agent("general", config=config)

    assert workspace.is_dir()
    overrides_by_tool = {
        call.args[0]: call.kwargs.get("tool_init_overrides") for call in mock_get_tool_by_name.call_args_list
    }
    assert overrides_by_tool["coding"] == {"base_dir": str(workspace)}
    assert overrides_by_tool["shell"] == {"base_dir": str(workspace)}
    assert overrides_by_tool["duckduckgo"] is None


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_resolves_relative_memory_file_workspace_from_config_dir(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
    tmp_path: Path,
) -> None:
    """Relative memory_file_path should resolve from the config directory, not CWD."""
    mock_get_tool_by_name.return_value = MagicMock()

    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = "./mind_data"
    config.agents["general"].tools = ["coding"]
    config.agents["general"].include_default_tools = False

    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    expected_workspace = config_dir / "mind_data"

    original_cwd = Path.cwd()
    other_cwd = tmp_path / "other"
    other_cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(other_cwd)
    try:
        with patch("mindroom.constants.CONFIG_PATH", config_dir / "config.yaml"):
            create_agent("general", config=config)
    finally:
        os.chdir(original_cwd)

    assert mock_get_tool_by_name.call_args is not None
    assert mock_get_tool_by_name.call_args.kwargs["tool_init_overrides"] == {"base_dir": str(expected_workspace)}
    assert expected_workspace.is_dir()


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_skips_agent_workspace_override_for_worker_routed_scoped_tools(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
    tmp_path: Path,
) -> None:
    """Worker-routed scoped tools should use worker workspace instead of the agent memory path."""
    mock_get_tool_by_name.return_value = MagicMock()

    workspace = tmp_path / "mind_data"
    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = str(workspace)
    config.agents["general"].tools = ["coding", "shell"]
    config.agents["general"].include_default_tools = False
    config.agents["general"].worker_scope = "user"
    config.agents["general"].worker_tools = ["coding"]

    create_agent("general", config=config)

    assert workspace.is_dir()
    overrides_by_tool = {
        call.args[0]: call.kwargs.get("tool_init_overrides") for call in mock_get_tool_by_name.call_args_list
    }
    assert overrides_by_tool["coding"] is None
    assert overrides_by_tool["shell"] == {"base_dir": str(workspace)}


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_uses_default_worker_tool_policy_when_unset(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
) -> None:
    """Agent creation should pass the built-in default worker-routing policy when worker_tools is omitted."""
    mock_get_tool_by_name.return_value = MagicMock()
    config = Config.from_yaml()
    config.agents["summary"].tools = ["openclaw_compat"]
    config.agents["summary"].include_default_tools = False
    config.agents["summary"].worker_tools = None

    create_agent("summary", config=config)

    worker_overrides = [call.kwargs["worker_tools_override"] for call in mock_get_tool_by_name.call_args_list]
    assert worker_overrides
    assert all(override == ["shell", "coding"] for override in worker_overrides)


@patch("mindroom.agents.SqliteDb")
def test_openclaw_compat_implies_matrix_message_tool(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """openclaw_compat should stay in the list and imply matrix_message."""
    config = Config.from_yaml()
    config.agents["summary"].tools = ["openclaw_compat"]
    config.agents["summary"].include_default_tools = False

    effective_tools = config.get_agent_tools("summary")
    assert "openclaw_compat" in effective_tools
    assert "matrix_message" in effective_tools

    agent = create_agent("summary", config=config)
    tool_names = [tool.name for tool in agent.tools]
    assert "matrix_message" in tool_names


def test_openclaw_compat_implied_matrix_message_does_not_duplicate() -> None:
    """Implied matrix_message should not duplicate explicit configuration."""
    config = Config.from_yaml()
    config.agents["summary"].tools = ["openclaw_compat", "matrix_message"]
    config.agents["summary"].include_default_tools = False

    effective_tools = config.get_agent_tools("summary")
    assert effective_tools.count("matrix_message") == 1


def test_matrix_message_implies_attachments_tool() -> None:
    """matrix_message should automatically include attachments via implied tools."""
    config = Config.from_yaml()
    config.agents["summary"].tools = ["matrix_message"]
    config.agents["summary"].include_default_tools = False

    effective_tools = config.get_agent_tools("summary")
    assert effective_tools == ["matrix_message", "attachments"]


def test_matrix_message_implied_attachments_does_not_duplicate() -> None:
    """Explicit attachments should not duplicate implied attachments."""
    config = Config.from_yaml()
    config.agents["summary"].tools = ["matrix_message", "attachments"]
    config.agents["summary"].include_default_tools = False

    effective_tools = config.get_agent_tools("summary")
    assert effective_tools.count("attachments") == 1


@patch("mindroom.agents.SqliteDb")
def test_get_agent_code(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the code agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("code", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "CodeAgent"


@patch("mindroom.agents.SqliteDb")
def test_get_agent_shell(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the shell agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("shell", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "ShellAgent"


@patch("mindroom.agents.SqliteDb")
def test_get_agent_summary(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the summary agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("summary", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "SummaryAgent"


def test_get_agent_unknown() -> None:
    """Tests that an unknown agent raises a ValueError."""
    config = Config.from_yaml()
    with pytest.raises(ValueError, match="Unknown agent: unknown"):
        create_agent("unknown", config=config)


@patch("mindroom.agents.SqliteDb")
def test_get_agent_learning_can_be_disabled(mock_storage: MagicMock) -> None:
    """Tests that learning can be disabled per agent."""
    config = Config.from_yaml()
    config.agents["general"].learning = False
    agent = create_agent("general", config=config)
    assert isinstance(agent, Agent)
    assert agent.learning is False
    assert mock_storage.call_count == 1


@patch("mindroom.agents.SqliteDb")
def test_get_agent_learning_defaults_fallback_when_agent_setting_omitted(mock_storage: MagicMock) -> None:
    """Tests that defaults.learning is used when per-agent learning is omitted."""
    config = Config.from_yaml()
    config.defaults.learning = False
    config.agents["general"].learning = None

    agent = create_agent("general", config=config)

    assert isinstance(agent, Agent)
    assert agent.learning is False
    # Learning storage should not be created when defaults disable learning.
    assert mock_storage.call_count == 1


@patch("mindroom.agents.SqliteDb")
def test_get_agent_learning_agentic_mode(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that learning mode can be configured as agentic."""
    config = Config.from_yaml()
    config.agents["general"].learning_mode = "agentic"
    agent = create_agent("general", config=config)
    assert isinstance(agent, Agent)
    assert isinstance(agent.learning, LearningMachine)
    assert isinstance(agent.learning.user_profile, UserProfileConfig)
    assert agent.learning.user_profile.mode is LearningMode.AGENTIC
    assert isinstance(agent.learning.user_memory, UserMemoryConfig)
    assert agent.learning.user_memory.mode is LearningMode.AGENTIC


@patch("mindroom.agents.SqliteDb")
def test_get_agent_learning_inherits_defaults(mock_storage: MagicMock) -> None:
    """Tests that learning mode falls back to defaults when agent config is None."""
    config = Config.from_yaml()
    # Agent has no explicit learning settings (None), defaults say enabled + agentic.
    config.agents["general"].learning = None
    config.agents["general"].learning_mode = None
    config.defaults.learning = True
    config.defaults.learning_mode = "agentic"

    agent = create_agent("general", config=config)

    assert isinstance(agent, Agent)
    assert isinstance(agent.learning, LearningMachine)
    assert isinstance(agent.learning.user_profile, UserProfileConfig)
    assert agent.learning.user_profile.mode is LearningMode.AGENTIC
    assert isinstance(agent.learning.user_memory, UserMemoryConfig)
    assert agent.learning.user_memory.mode is LearningMode.AGENTIC
    assert mock_storage.call_count == 2


@patch("mindroom.agents.SqliteDb")
def test_get_agent_uses_storage_path_for_sessions_and_learning(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that session and learning databases are created under the resolved storage path."""
    config = Config.from_yaml()
    create_agent("general", config=config, storage_path=tmp_path)

    db_files = [Path(str(call.kwargs["db_file"])) for call in mock_storage.call_args_list]
    assert tmp_path / "sessions" / "general.db" in db_files
    assert tmp_path / "learning" / "general.db" in db_files


@patch("mindroom.agents.SqliteDb")
def test_get_agent_uses_worker_storage_for_sessions_and_learning(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Worker-scoped agents should keep session and learning DBs inside the resolved worker root."""
    config = Config.from_yaml()
    config.agents["general"].worker_scope = "user"
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )
    worker_key = resolve_worker_key("user", execution_identity, agent_name="general")
    assert worker_key is not None

    with tool_execution_identity(execution_identity):
        create_agent("general", config=config, storage_path=tmp_path)

    worker_root = worker_root_path(tmp_path, worker_key)
    db_files = [Path(str(call.kwargs["db_file"])) for call in mock_storage.call_args_list]
    assert worker_root / "sessions" / "general.db" in db_files
    assert worker_root / "learning" / "general.db" in db_files


@patch("mindroom.agents.SqliteDb")
def test_get_agent_uses_shared_worker_storage_without_execution_identity(
    mock_storage: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared-scope agents should resolve worker-owned state even before a live request context exists."""
    config = Config.from_yaml()
    config.agents["general"].worker_scope = "shared"
    monkeypatch.setenv("CUSTOMER_ID", "tenant-123")

    create_agent("general", config=config, storage_path=tmp_path)

    shared_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id="tenant-123",
        account_id=None,
    )
    worker_key = resolve_worker_key("shared", shared_identity, agent_name="general")
    assert worker_key is not None

    worker_root = worker_root_path(tmp_path, worker_key)
    db_files = [Path(str(call.kwargs["db_file"])) for call in mock_storage.call_args_list]
    assert worker_root / "sessions" / "general.db" in db_files
    assert worker_root / "learning" / "general.db" in db_files


@patch("mindroom.agents.SqliteDb")
def test_create_agent_loads_shared_worker_scoped_tool_credentials_without_execution_identity(
    mock_storage: MagicMock,  # noqa: ARG001
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared worker credentials should be available during agent construction outside request context."""
    config = Config.from_yaml()
    config.defaults.tools = []
    config.agents["general"].tools = ["credentialed_toolkit"]
    config.agents["general"].worker_scope = "shared"
    monkeypatch.setenv("CUSTOMER_ID", "tenant-123")

    credentials_manager = CredentialsManager(tmp_path / "credentials")
    shared_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id="tenant-123",
        account_id=None,
    )
    worker_key = resolve_worker_key("shared", shared_identity, agent_name="general")
    assert worker_key is not None
    credentials_manager.for_worker(worker_key).save_credentials(
        "credentialed_toolkit",
        {"api_key": "worker-key", "_source": "ui"},
    )

    def _get_tool_by_name(
        tool_name: str,
        *,
        tool_init_overrides: dict[str, object] | None = None,
        worker_tools_override: list[str] | None = None,
        worker_scope: WorkerScope | None = None,
        routing_agent_name: str | None = None,
    ) -> MagicMock:
        del tool_init_overrides, worker_tools_override
        credentials = load_scoped_credentials(
            tool_name,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
            credentials_manager=credentials_manager,
        )
        if not isinstance(credentials, dict) or "api_key" not in credentials:
            msg = "API key required"
            raise ValueError(msg)
        tool = MagicMock()
        tool.name = tool_name
        return tool

    monkeypatch.setattr("mindroom.agents.get_tool_by_name", _get_tool_by_name)

    agent = create_agent("general", config=config, storage_path=tmp_path)

    assert [tool.name for tool in agent.tools] == ["credentialed_toolkit"]


def test_resolve_worker_key_rejects_unknown_scope() -> None:
    """Unknown worker scopes should fail loudly instead of silently acting like room_thread."""
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    with pytest.raises(ValueError, match="Unknown worker scope"):
        resolve_worker_key(cast("WorkerScope", "bogus"), execution_identity)


@patch("mindroom.agents.SqliteDb")
def test_agent_context_files_are_loaded_into_role(mock_storage: MagicMock, tmp_path: Path) -> None:  # noqa: ARG001
    """Configured context files should be prepended to role context."""
    config = Config.from_yaml()
    soul_path = tmp_path / "SOUL.md"
    user_path = tmp_path / "USER.md"
    soul_path.write_text("Core personality directive.", encoding="utf-8")
    user_path.write_text("User preference: concise answers.", encoding="utf-8")

    config.agents["general"].context_files = [str(soul_path), str(user_path)]

    agent = create_agent("general", config=config, storage_path=tmp_path)

    assert "## Personality Context" in agent.role
    assert "### SOUL.md" in agent.role
    assert "Core personality directive." in agent.role
    assert "### USER.md" in agent.role
    assert "User preference: concise answers." in agent.role


@patch("mindroom.agents.SqliteDb")
def test_agent_preload_cap_truncates_context_files_in_order(
    mock_storage: MagicMock,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Preload cap should drop earlier context files before later ones."""
    config = Config.from_yaml()
    config.defaults.max_preload_chars = 420

    first_path = tmp_path / "FIRST.md"
    second_path = tmp_path / "SECOND.md"
    first_path.write_text("FIRST_START " + "A" * 220 + " FIRST_END", encoding="utf-8")
    second_path.write_text("SECOND_START " + "B" * 220 + " SECOND_END", encoding="utf-8")

    config.agents["general"].context_files = [str(first_path), str(second_path)]

    agent = create_agent("general", config=config, storage_path=tmp_path)

    assert "[Content truncated - " in agent.role
    assert "### FIRST.md" not in agent.role
    assert "### SECOND.md" in agent.role
    assert "SECOND_START" in agent.role


@patch("mindroom.agents.SqliteDb")
def test_agent_missing_context_file_is_ignored(mock_storage: MagicMock, tmp_path: Path) -> None:  # noqa: ARG001
    """Missing context files should not prevent agent creation."""
    config = Config.from_yaml()
    config.agents["general"].context_files = [str(tmp_path / "does-not-exist.md")]

    agent = create_agent("general", config=config, storage_path=tmp_path)

    assert "## Personality Context" not in agent.role
    assert "does-not-exist.md" not in agent.role


def test_agent_relative_context_paths_resolve_from_config_dir(tmp_path: Path) -> None:
    """Relative context paths should resolve from the config directory, not CWD."""
    config = Config.from_yaml()

    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    soul_path = config_dir / "SOUL.md"
    soul_path.write_text("Relative soul context.", encoding="utf-8")

    config.agents["general"].context_files = ["SOUL.md"]

    original_cwd = Path.cwd()
    other_cwd = tmp_path / "other"
    other_cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(other_cwd)
    try:
        with patch("mindroom.agents.SqliteDb"), patch("mindroom.constants.CONFIG_PATH", config_dir / "config.yaml"):
            agent = create_agent("general", config=config, storage_path=tmp_path)
    finally:
        os.chdir(original_cwd)

    assert "Relative soul context." in agent.role


@patch("mindroom.agents.SqliteDb")
def test_create_agent_private_root_loads_requester_context_from_isolated_workspace(
    mock_storage: MagicMock,  # noqa: ARG001
    tmp_path: Path,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Private per-user roots should copy their configured template and isolate private context files."""
    config = Config.from_yaml()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    template_dir = build_private_template_dir(
        "cfg/mind_template",
        files={
            "USER.md": "Template user.\n",
            "MEMORY.md": "# Memory\n",
            "memory/notes.md": "Private note.\n",
        },
    )
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir="./mind_template",
        context_files=["USER.md"],
    )

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-bob",
    )

    with patch("mindroom.constants.CONFIG_PATH", config_dir / "config.yaml"):
        assert template_dir == (config_dir / "mind_template").resolve()

        with tool_execution_identity(alice_identity):
            create_agent("general", config=config, storage_path=tmp_path)
            alice_worker_key = resolve_worker_key("user", alice_identity)
            assert alice_worker_key is not None
            alice_workspace = worker_root_path(tmp_path, alice_worker_key) / "mind_data"
            assert (alice_workspace / "USER.md").exists()
            assert (alice_workspace / "MEMORY.md").exists()
            (alice_workspace / "USER.md").write_text("Alice private root context.", encoding="utf-8")
            alice_agent = create_agent("general", config=config, storage_path=tmp_path)

        with tool_execution_identity(bob_identity):
            bob_agent = create_agent("general", config=config, storage_path=tmp_path)
            bob_worker_key = resolve_worker_key("user", bob_identity)
            assert bob_worker_key is not None
            bob_workspace = worker_root_path(tmp_path, bob_worker_key) / "mind_data"

    assert alice_workspace != bob_workspace
    assert "Alice private root context." in alice_agent.role
    assert (bob_workspace / "USER.md").exists()
    assert "Alice private root context." not in bob_agent.role


@patch("mindroom.agents.SqliteDb")
def test_create_agent_private_template_dir_does_not_imply_context_files(
    mock_storage: MagicMock,  # noqa: ARG001
    tmp_path: Path,
    build_private_template_dir: Callable[..., Path],
) -> None:
    """Private template directories should not implicitly load Mind-style context files."""
    config = Config.from_yaml()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    build_private_template_dir(
        "cfg/mind_template",
        files={
            "USER.md": "Template user.\n",
            "MEMORY.md": "# Memory\n",
        },
    )
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir="./mind_template",
    )

    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    with patch("mindroom.constants.CONFIG_PATH", config_dir / "config.yaml"), tool_execution_identity(identity):
        agent = create_agent("general", config=config, storage_path=tmp_path)

    assert "Template user." not in agent.role


def test_config_rejects_unknown_agent_knowledge_base_assignment() -> None:
    """Agents must not reference unknown knowledge bases."""
    with pytest.raises(ValidationError, match="Agents reference unknown knowledge bases: calculator -> research"):
        Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    knowledge_bases=["research"],
                ),
            },
            knowledge_bases={},
        )


def test_config_rejects_legacy_agent_knowledge_base_field() -> None:
    """Legacy singular knowledge_base field must fail fast to avoid silent drops."""
    with pytest.raises(
        ValidationError,
        match=re.escape("Agent field 'knowledge_base' was removed. Use 'knowledge_bases' (list) instead."),
    ):
        Config(
            agents={
                "calculator": {
                    "display_name": "CalculatorAgent",
                    "knowledge_base": "research",
                },
            },
            knowledge_bases={
                "research": KnowledgeBaseConfig(
                    path="./knowledge_docs/research",
                    watch=False,
                ),
            },
        )


def test_config_rejects_legacy_agent_memory_dir_field() -> None:
    """Legacy memory_dir field must fail fast to avoid silent drops."""
    with pytest.raises(
        ValidationError,
        match=re.escape("Agent field 'memory_dir' was removed. Use 'context_files' and memory.backend=file instead."),
    ):
        Config(
            agents={
                "calculator": {
                    "display_name": "CalculatorAgent",
                    "memory_dir": "./memory",
                },
            },
        )


def test_config_rejects_legacy_agent_sandbox_tools_field() -> None:
    """Legacy sandbox_tools field must fail fast to avoid silent drops."""
    with pytest.raises(
        ValidationError,
        match=re.escape("Agent field 'sandbox_tools' was removed. Use 'worker_tools' instead."),
    ):
        Config(
            agents={
                "calculator": {
                    "display_name": "CalculatorAgent",
                    "sandbox_tools": ["shell"],
                },
            },
        )


def test_config_rejects_legacy_defaults_sandbox_tools_field() -> None:
    """Legacy defaults.sandbox_tools field must fail fast to avoid silent drops."""
    with pytest.raises(
        ValidationError,
        match=re.escape("defaults.sandbox_tools was removed. Use defaults.worker_tools instead."),
    ):
        Config(
            defaults={
                "sandbox_tools": ["shell"],
            },
            agents={
                "calculator": {
                    "display_name": "CalculatorAgent",
                },
            },
        )


def test_config_rejects_duplicate_agent_knowledge_base_assignment() -> None:
    """Each agent knowledge base assignment should be unique."""
    with pytest.raises(ValidationError, match="Duplicate knowledge bases are not allowed: research"):
        Config(
            agents={
                "calculator": AgentConfig(
                    display_name="CalculatorAgent",
                    knowledge_bases=["research", "research"],
                ),
            },
            knowledge_bases={
                "research": KnowledgeBaseConfig(
                    path="./knowledge_docs/research",
                    watch=False,
                ),
            },
        )


def test_config_resolves_per_agent_memory_backend_override() -> None:
    """Per-agent memory backend overrides should take precedence over global defaults."""
    config = Config(
        agents={
            "general": AgentConfig(display_name="General"),
            "writer": AgentConfig(display_name="Writer", memory_backend="file"),
        },
        memory={"backend": "mem0"},
    )

    assert config.get_agent_memory_backend("general") == "mem0"
    assert config.get_agent_memory_backend("writer") == "file"


def test_config_reports_mixed_memory_backend_usage() -> None:
    """Config helper methods should report effective mixed backend usage."""
    config = Config(
        agents={
            "general": AgentConfig(display_name="General", memory_backend="file"),
            "writer": AgentConfig(display_name="Writer", memory_backend="mem0"),
        },
        memory={"backend": "mem0"},
    )

    assert config.uses_file_memory() is True
    assert config.uses_mem0_memory() is True


def test_config_rejects_memory_file_path_when_effective_backend_is_mem0() -> None:
    """memory_file_path should fail fast unless effective backend resolves to file."""
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "agents.<name>.memory_file_path requires effective file memory backend; invalid agents: general",
        ),
    ):
        Config(
            agents={
                "general": AgentConfig(display_name="General", memory_file_path="./openclaw_data"),
            },
            memory={"backend": "mem0"},
        )


def test_config_accepts_memory_file_path_with_file_backend_override() -> None:
    """memory_file_path is valid when effective backend resolves to file."""
    config = Config(
        agents={
            "general": AgentConfig(
                display_name="General",
                memory_backend="file",
                memory_file_path="./openclaw_data",
            ),
        },
        memory={"backend": "mem0"},
    )

    assert config.agents["general"].memory_file_path == "./openclaw_data"


def test_config_accepts_valid_agent_knowledge_base_assignment() -> None:
    """Agent knowledge base assignment is valid when the base is configured."""
    config = Config(
        agents={
            "calculator": AgentConfig(
                display_name="CalculatorAgent",
                knowledge_bases=["research"],
            ),
        },
        knowledge_bases={
            "research": KnowledgeBaseConfig(
                path="./knowledge_docs/research",
                watch=False,
            ),
        },
    )

    assert config.agents["calculator"].knowledge_bases == ["research"]


def test_config_rejects_reserved_private_knowledge_base_prefix() -> None:
    """Top-level knowledge base IDs must not collide with synthetic private IDs."""
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "knowledge_bases keys must not use the reserved private prefix '__agent_private__:'; "
            "invalid keys: __agent_private__:mind",
        ),
    ):
        Config(
            agents={
                "mind": AgentConfig(display_name="Mind"),
            },
            knowledge_bases={
                "__agent_private__:mind": KnowledgeBaseConfig(path="./company_docs"),
            },
        )


def test_config_private_knowledge_requires_path_without_template_default() -> None:
    """Private knowledge needs an explicit path whenever it is enabled."""
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "agents.<name>.private.knowledge.path is required when private.knowledge is enabled; invalid agents: mind",
        ),
    ):
        Config(
            agents={
                "mind": AgentConfig(
                    display_name="Mind",
                    private=AgentPrivateConfig(
                        per="user",
                        knowledge=AgentPrivateKnowledgeConfig(watch=False),
                    ),
                ),
            },
        )


def test_config_private_and_shared_knowledge_coexist() -> None:
    """Agents can combine requester-private knowledge with shared top-level knowledge bases."""
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                private=AgentPrivateConfig(
                    per="user",
                    template_dir="./mind_template",
                    knowledge=AgentPrivateKnowledgeConfig(path="memory"),
                ),
                knowledge_bases=["company_docs"],
            ),
        },
        knowledge_bases={
            "company_docs": KnowledgeBaseConfig(path="./company_docs"),
        },
    )

    private_base_id = config.get_agent_private_knowledge_base_id("mind")
    assert private_base_id is not None
    assert config.get_agent_knowledge_base_ids("mind") == ["company_docs", private_base_id]
    private_config = config.get_knowledge_base_config(private_base_id)
    assert private_config.path == "memory"


def test_template_dir_does_not_imply_private_knowledge() -> None:
    """Copying from a template directory alone should not create a private knowledge base."""
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                private=AgentPrivateConfig(
                    per="user",
                    template_dir="./mind_template",
                ),
            ),
        },
    )

    assert config.get_agent_private_knowledge_base_id("mind") is None
    assert config.get_agent_knowledge_base_ids("mind") == []


def test_get_private_knowledge_base_agent_requires_active_private_knowledge() -> None:
    """Synthetic private base IDs should resolve only while private knowledge is actually active."""
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                private=AgentPrivateConfig(
                    per="user",
                    template_dir="./mind_template",
                    knowledge=AgentPrivateKnowledgeConfig(path="memory"),
                ),
            ),
            "assistant": AgentConfig(display_name="Assistant"),
        },
        knowledge_bases={
            "company_docs": KnowledgeBaseConfig(path="./company_docs"),
        },
    )

    assert config.get_private_knowledge_base_agent("__agent_private__:mind") == "mind"
    assert config.get_private_knowledge_base_agent("__agent_private__:assistant") is None
    assert config.get_private_knowledge_base_agent("__agent_private__:missing") is None


def test_config_rejects_duplicate_default_tools() -> None:
    """Default tools should be unique."""
    with pytest.raises(ValidationError, match="Duplicate default tools are not allowed: scheduler"):
        Config(
            defaults={"tools": ["scheduler", "scheduler"]},
        )


def test_config_rejects_culture_with_unknown_agent() -> None:
    """Culture assignments must reference configured agents."""
    with pytest.raises(ValidationError, match="Cultures reference unknown agents: engineering -> missing_agent"):
        Config(
            agents={
                "calculator": AgentConfig(display_name="CalculatorAgent"),
            },
            cultures={
                "engineering": CultureConfig(
                    description="Engineering standards",
                    agents=["missing_agent"],
                    mode="automatic",
                ),
            },
        )


def test_config_rejects_agents_in_multiple_cultures() -> None:
    """An agent can belong to at most one culture."""
    with pytest.raises(
        ValidationError,
        match="Agents cannot belong to multiple cultures: calculator -> engineering, support",
    ):
        Config(
            agents={
                "calculator": AgentConfig(display_name="CalculatorAgent"),
            },
            cultures={
                "engineering": CultureConfig(agents=["calculator"]),
                "support": CultureConfig(agents=["calculator"]),
            },
        )


def test_config_accepts_valid_culture_assignment() -> None:
    """Config should expose culture assignment helpers for valid culture definitions."""
    config = Config(
        agents={
            "calculator": AgentConfig(display_name="CalculatorAgent"),
            "summary": AgentConfig(display_name="SummaryAgent"),
        },
        cultures={
            "engineering": CultureConfig(
                description="Shared engineering practices",
                agents=["calculator", "summary"],
                mode="automatic",
            ),
        },
    )

    assignment = config.get_agent_culture("calculator")
    assert assignment is not None
    culture_name, culture_config = assignment
    assert culture_name == "engineering"
    assert culture_config.mode == "automatic"
    assert config.get_agent_culture("unknown") is None


@patch("mindroom.agents.SqliteDb")
@patch("mindroom.agents.CultureManager")
@patch("mindroom.agents.Agent")
def test_create_agent_shares_culture_manager_for_same_culture(
    mock_agent_class: MagicMock,
    mock_culture_manager_class: MagicMock,
    mock_storage: MagicMock,
    tmp_path: Path,
) -> None:
    """Agents in the same culture should share one CultureManager and culture DB."""
    _CULTURE_MANAGER_CACHE.clear()
    config = Config(
        agents={
            "agent_one": AgentConfig(
                display_name="Agent One",
                role="First",
                learning=False,
                include_default_tools=False,
            ),
            "agent_two": AgentConfig(
                display_name="Agent Two",
                role="Second",
                learning=False,
                include_default_tools=False,
            ),
        },
        cultures={
            "engineering": CultureConfig(
                description="Engineering best practices",
                agents=["agent_one", "agent_two"],
                mode="automatic",
            ),
        },
        models={
            "default": ModelConfig(provider="openai", id="gpt-4o-mini"),
        },
    )

    model = MagicMock()
    model.id = "gpt-4o-mini"
    with patch("mindroom.ai.get_model_instance", return_value=model):
        create_agent(
            "agent_one",
            config=config,
            storage_path=tmp_path,
            include_interactive_questions=False,
        )
        create_agent(
            "agent_two",
            config=config,
            storage_path=tmp_path,
            include_interactive_questions=False,
        )

    assert mock_culture_manager_class.call_count == 1
    first_kwargs = mock_agent_class.call_args_list[0].kwargs
    second_kwargs = mock_agent_class.call_args_list[1].kwargs

    assert first_kwargs["culture_manager"] is second_kwargs["culture_manager"]
    assert first_kwargs["add_culture_to_context"] is True
    assert first_kwargs["update_cultural_knowledge"] is True
    assert first_kwargs["enable_agentic_culture"] is False

    culture_db_calls = [
        call
        for call in mock_storage.call_args_list
        if str(call.kwargs.get("db_file", "")).endswith("/culture/engineering.db")
    ]
    assert len(culture_db_calls) == 1


@patch("mindroom.agents.SqliteDb")
@patch("mindroom.agents.CultureManager")
@patch("mindroom.agents.Agent")
def test_create_agent_culture_uses_agent_model_when_default_missing(
    mock_agent_class: MagicMock,
    mock_culture_manager_class: MagicMock,
    mock_storage: MagicMock,
    tmp_path: Path,
) -> None:
    """Culture manager should not require models.default when an agent model is configured."""
    _CULTURE_MANAGER_CACHE.clear()
    config = Config(
        agents={
            "agent_one": AgentConfig(
                display_name="Agent One",
                role="First",
                model="m1",
                learning=False,
                include_default_tools=False,
            ),
        },
        cultures={
            "engineering": CultureConfig(
                description="Engineering best practices",
                agents=["agent_one"],
                mode="automatic",
            ),
        },
        models={
            "m1": ModelConfig(provider="openai", id="gpt-4o-mini"),
        },
    )

    model = MagicMock()
    model.id = "gpt-4o-mini"
    with patch("mindroom.ai.get_model_instance", return_value=model) as mock_get_model_instance:
        create_agent(
            "agent_one",
            config=config,
            storage_path=tmp_path,
            include_interactive_questions=False,
        )

    mock_get_model_instance.assert_called_once_with(config, "m1")
    assert mock_agent_class.call_count == 1
    assert mock_storage.call_count >= 2
    assert mock_culture_manager_class.call_args is not None
    assert mock_culture_manager_class.call_args.kwargs["model"] is model
