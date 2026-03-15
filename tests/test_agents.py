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
from mindroom.config.agent import AgentConfig, CultureConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials import CredentialsManager, load_scoped_credentials
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_state_root_path,
    agent_workspace_root_path,
    resolve_agent_owned_path,
    resolve_agent_state_storage_path,
    resolve_unscoped_worker_key,
    resolve_worker_key,
    shared_storage_root,
    tool_execution_identity,
    visible_agent_state_roots_for_worker_key,
    worker_root_path,
)

_BOUND_RUNTIME_PATHS: dict[int, RuntimePaths] = {}

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import WorkerScope


def _runtime_paths(storage_path: Path, *, config_path: Path | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=config_path or storage_path / "config.yaml",
        storage_path=storage_path,
    )


def _bind_runtime_paths(config: Config, runtime_paths: RuntimePaths) -> Config:
    bound = Config.validate_with_runtime(config.model_dump(exclude_none=True), runtime_paths)
    _BOUND_RUNTIME_PATHS[id(bound)] = runtime_paths
    return bound


def _runtime_for_config(config: Config) -> RuntimePaths:
    runtime_paths = _BOUND_RUNTIME_PATHS.get(id(config))
    if runtime_paths is not None:
        return runtime_paths
    return resolve_runtime_paths(config_path=Path("config.yaml"), storage_path=Path("mindroom_data"))


def _create_agent_for_test(agent_name: str, config: Config, **kwargs: object) -> Agent:
    """Create an agent with the test config's explicit runtime context."""
    return create_agent(agent_name, config, _runtime_for_config(config), **kwargs)


@patch("mindroom.agents.SqliteDb")
def test_get_agent_calculator(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the calculator agent is created correctly."""
    config = Config.from_yaml()
    agent = _create_agent_for_test("calculator", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "CalculatorAgent"


@patch("mindroom.agents.SqliteDb")
def test_get_agent_general(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the general agent is created correctly."""
    config = Config.from_yaml()
    agent = _create_agent_for_test("general", config=config)
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

    agent = _create_agent_for_test("general", config=config)

    assert agent_prompts.HIDDEN_TOOL_CALLS_PROMPT in agent.instructions


@patch("mindroom.agents.SqliteDb")
def test_scheduler_tool_enabled_by_default(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """All agents should get the scheduler tool even when not explicitly configured."""
    config = Config.from_yaml()
    config.agents["summary"].tools = []

    agent = _create_agent_for_test("summary", config=config)
    tool_names = [tool.name for tool in agent.tools]

    assert "scheduler" in tool_names


@patch("mindroom.agents.SqliteDb")
def test_configurable_default_tools_are_applied(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """defaults.tools should be merged into every agent's configured tools."""
    config = Config.from_yaml()
    config.defaults.tools = ["scheduler", "calculator"]
    config.agents["summary"].tools = []

    agent = _create_agent_for_test("summary", config=config)
    tool_names = [tool.name for tool in agent.tools]

    assert "scheduler" in tool_names
    assert "calculator" in tool_names


@patch("mindroom.agents.SqliteDb")
def test_default_tools_do_not_duplicate_agent_tools(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """An agent tool already present should not be duplicated by defaults.tools."""
    config = Config.from_yaml()
    config.defaults.tools = ["scheduler"]
    config.agents["summary"].tools = ["scheduler"]

    agent = _create_agent_for_test("summary", config=config)
    tool_names = [tool.name for tool in agent.tools]

    assert tool_names.count("scheduler") == 1


@patch("mindroom.agents.SqliteDb")
def test_agent_include_default_tools_false_skips_config_defaults(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Agent include_default_tools=False should skip defaults.tools entirely."""
    config = Config.from_yaml()
    config.defaults.tools = ["scheduler", "calculator"]
    config.agents["summary"].tools = []
    config.agents["summary"].include_default_tools = False

    agent = _create_agent_for_test("summary", config=config)
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

    _create_agent_for_test("summary", config=config)

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
        credentials_manager: object | None = None,
        tool_init_overrides: dict[str, object] | None = None,
        runtime_overrides: dict[str, object] | None = None,
        worker_tools_override: list[str] | None = None,
        worker_scope: WorkerScope | None = None,
        routing_agent_name: str | None = None,
    ) -> MagicMock:
        del (
            credentials_manager,
            tool_init_overrides,
            runtime_overrides,
            worker_tools_override,
            worker_scope,
            routing_agent_name,
        )
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

    agent = _create_agent_for_test("summary", config=config)

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

    _create_agent_for_test("summary", config=config)

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
    """Workspace-relative memory_file_path should point tools at the canonical workspace."""
    mock_get_tool_by_name.return_value = MagicMock()

    workspace = agent_workspace_root_path(tmp_path, "general") / "mind_data"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("Canonical workspace.\n", encoding="utf-8")
    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = "mind_data"
    config.agents["general"].tools = ["coding", "shell", "duckduckgo"]
    config.agents["general"].include_default_tools = False

    _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    overrides_by_tool = {
        call.args[0]: call.kwargs.get("tool_init_overrides") for call in mock_get_tool_by_name.call_args_list
    }
    assert overrides_by_tool["coding"] == {"base_dir": str(workspace)}
    assert overrides_by_tool["shell"] == {"base_dir": str(workspace)}
    assert overrides_by_tool["duckduckgo"] is None


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_keeps_tool_default_base_dir_without_memory_workspace(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
) -> None:
    """Agents without memory_file_path should not be forced into an auto-created workspace."""
    mock_get_tool_by_name.return_value = MagicMock()

    config = Config.from_yaml()
    config.agents["general"].memory_file_path = None
    config.agents["general"].tools = ["coding", "shell", "duckduckgo"]
    config.agents["general"].include_default_tools = False

    _create_agent_for_test("general", config=config)

    overrides_by_tool = {
        call.args[0]: call.kwargs.get("tool_init_overrides") for call in mock_get_tool_by_name.call_args_list
    }
    assert overrides_by_tool["coding"] is None
    assert overrides_by_tool["shell"] is None
    assert overrides_by_tool["duckduckgo"] is None


@patch("mindroom.agents.load_plugins")
def test_create_agent_threads_config_path_to_plugin_loading(
    mock_load_plugins: MagicMock,
    tmp_path: Path,
) -> None:
    """Agent creation should resolve relative plugin paths from the active config file."""
    config_path = tmp_path / "cfg" / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = Config.from_yaml()
    runtime_paths = _runtime_paths(tmp_path, config_path=config_path)

    with patch("mindroom.agents.SqliteDb"):
        _create_agent_for_test("general", config=_bind_runtime_paths(config, runtime_paths))

    mock_load_plugins.assert_called_once_with(config)


def test_create_agent_rejects_absolute_memory_file_workspace(tmp_path: Path) -> None:
    """Absolute memory_file_path should fail fast instead of creating copied state."""
    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"

    with pytest.raises(ValidationError, match="workspace-relative"):
        config.agents["general"].memory_file_path = str(tmp_path / "external" / "mind_data")


def test_create_agent_rejects_env_var_memory_file_workspace() -> None:
    """Env-var memory_file_path should fail fast instead of becoming a literal workspace subdir."""
    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"

    with pytest.raises(ValidationError, match="env-variable references"):
        config.agents["general"].memory_file_path = "${MINDROOM_STORAGE_PATH}/mind_data"


def test_create_agent_rejects_absolute_context_files(tmp_path: Path) -> None:
    """Absolute context_files should fail fast instead of creating copied state."""
    config = Config.from_yaml()

    with pytest.raises(ValidationError, match="workspace-relative"):
        config.agents["general"].context_files = [str(tmp_path / "SOUL.md")]


def test_create_agent_rejects_env_var_context_files() -> None:
    """Env-var context_files should fail fast instead of becoming literal workspace segments."""
    config = Config.from_yaml()

    with pytest.raises(ValidationError, match="env-variable references"):
        config.agents["general"].context_files = ["${MINDROOM_STORAGE_PATH}/SOUL.md"]


def test_create_agent_rejects_bare_env_var_context_files() -> None:
    """Agent-owned paths should reject bare `$NAME/...` forms too."""
    config = Config.from_yaml()

    with pytest.raises(ValidationError, match="env-variable references"):
        config.agents["general"].context_files = ["$MINDROOM_STORAGE_PATH/SOUL.md"]


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_applies_agent_workspace_override_for_worker_routed_scoped_tools(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
    tmp_path: Path,
) -> None:
    """Worker-routed scoped tools should receive the same workspace override as local tools."""
    mock_get_tool_by_name.return_value = MagicMock()

    workspace = agent_workspace_root_path(tmp_path, "general") / "mind_data"
    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = "mind_data"
    config.agents["general"].tools = ["coding", "shell"]
    config.agents["general"].include_default_tools = False
    config.agents["general"].worker_scope = "user"
    config.agents["general"].worker_tools = ["coding"]

    _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    overrides_by_tool = {
        call.args[0]: call.kwargs.get("tool_init_overrides") for call in mock_get_tool_by_name.call_args_list
    }
    assert overrides_by_tool["coding"] == {"base_dir": str(workspace)}
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

    _create_agent_for_test("summary", config=config)

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

    agent = _create_agent_for_test("summary", config=config)
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
    agent = _create_agent_for_test("code", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "CodeAgent"


@patch("mindroom.agents.SqliteDb")
def test_get_agent_shell(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the shell agent is created correctly."""
    config = Config.from_yaml()
    agent = _create_agent_for_test("shell", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "ShellAgent"


@patch("mindroom.agents.SqliteDb")
def test_get_agent_summary(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the summary agent is created correctly."""
    config = Config.from_yaml()
    agent = _create_agent_for_test("summary", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "SummaryAgent"


def test_get_agent_unknown() -> None:
    """Tests that an unknown agent raises a ValueError."""
    config = Config.from_yaml()
    with pytest.raises(ValueError, match="Unknown agent: unknown"):
        _create_agent_for_test("unknown", config=config)


@patch("mindroom.agents.SqliteDb")
def test_get_agent_learning_can_be_disabled(mock_storage: MagicMock) -> None:
    """Tests that learning can be disabled per agent."""
    config = Config.from_yaml()
    config.agents["general"].learning = False
    agent = _create_agent_for_test("general", config=config)
    assert isinstance(agent, Agent)
    assert agent.learning is False
    assert mock_storage.call_count == 1


@patch("mindroom.agents.SqliteDb")
def test_get_agent_learning_defaults_fallback_when_agent_setting_omitted(mock_storage: MagicMock) -> None:
    """Tests that defaults.learning is used when per-agent learning is omitted."""
    config = Config.from_yaml()
    config.defaults.learning = False
    config.agents["general"].learning = None

    agent = _create_agent_for_test("general", config=config)

    assert isinstance(agent, Agent)
    assert agent.learning is False
    # Learning storage should not be created when defaults disable learning.
    assert mock_storage.call_count == 1


@patch("mindroom.agents.SqliteDb")
def test_get_agent_learning_agentic_mode(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that learning mode can be configured as agentic."""
    config = Config.from_yaml()
    config.agents["general"].learning_mode = "agentic"
    agent = _create_agent_for_test("general", config=config)
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

    agent = _create_agent_for_test("general", config=config)

    assert isinstance(agent, Agent)
    assert isinstance(agent.learning, LearningMachine)
    assert isinstance(agent.learning.user_profile, UserProfileConfig)
    assert agent.learning.user_profile.mode is LearningMode.AGENTIC
    assert isinstance(agent.learning.user_memory, UserMemoryConfig)
    assert agent.learning.user_memory.mode is LearningMode.AGENTIC
    assert mock_storage.call_count == 2


@patch("mindroom.agents.SqliteDb")
def test_get_agent_uses_storage_path_for_sessions_and_learning(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Session and learning databases should live under the canonical agent state root."""
    config = Config.from_yaml()
    _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    agent_root = agent_state_root_path(tmp_path, "general")
    db_files = [Path(str(call.kwargs["db_file"])) for call in mock_storage.call_args_list]
    assert agent_root / "sessions" / "general.db" in db_files
    assert agent_root / "learning" / "general.db" in db_files


@patch("mindroom.agents.SqliteDb")
def test_get_agent_uses_worker_storage_for_sessions_and_learning(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Worker scope should not change the canonical session and learning paths."""
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
        _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    agent_root = agent_state_root_path(tmp_path, "general")
    db_files = [Path(str(call.kwargs["db_file"])) for call in mock_storage.call_args_list]
    assert agent_root / "sessions" / "general.db" in db_files
    assert agent_root / "learning" / "general.db" in db_files


@patch("mindroom.agents.SqliteDb")
def test_get_agent_uses_shared_worker_storage_without_execution_identity(
    mock_storage: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared scope should still use canonical agent state before a live request context exists."""
    config = Config.from_yaml()
    config.agents["general"].worker_scope = "shared"
    monkeypatch.setenv("CUSTOMER_ID", "tenant-123")

    _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

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

    agent_root = agent_state_root_path(tmp_path, "general")
    db_files = [Path(str(call.kwargs["db_file"])) for call in mock_storage.call_args_list]
    assert agent_root / "sessions" / "general.db" in db_files
    assert agent_root / "learning" / "general.db" in db_files


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
        credentials_manager: object | None = None,
        tool_init_overrides: dict[str, object] | None = None,
        runtime_overrides: dict[str, object] | None = None,
        worker_tools_override: list[str] | None = None,
        worker_scope: WorkerScope | None = None,
        routing_agent_name: str | None = None,
    ) -> MagicMock:
        del tool_init_overrides, runtime_overrides, worker_tools_override
        credentials = load_scoped_credentials(
            tool_name,
            worker_scope=worker_scope,
            routing_agent_name=routing_agent_name,
            credentials_manager=cast("CredentialsManager", credentials_manager),
        )
        if not isinstance(credentials, dict) or "api_key" not in credentials:
            msg = "API key required"
            raise ValueError(msg)
        tool = MagicMock()
        tool.name = tool_name
        return tool

    monkeypatch.setattr("mindroom.agents.get_tool_by_name", _get_tool_by_name)

    agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    assert [tool.name for tool in agent.tools] == ["credentialed_toolkit"]


def test_resolve_worker_key_rejects_unknown_scope() -> None:
    """Unknown worker scopes should fail loudly instead of silently falling back."""
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


def test_resolve_agent_owned_path_resolves_workspace_relative_path(tmp_path: Path) -> None:
    """Agent-owned paths should resolve directly inside the canonical workspace."""
    resolved = resolve_agent_owned_path(
        "mind_data/SOUL.md",
        agent_name="general",
        base_storage_path=tmp_path,
    )

    assert resolved.is_relative_to(agent_state_root_path(tmp_path, "general"))
    assert resolved == agent_workspace_root_path(tmp_path, "general") / "mind_data" / "SOUL.md"


def test_agent_owned_validation_matches_runtime_resolution(tmp_path: Path) -> None:
    """Validation and runtime resolution should share the same normalization contract."""
    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = "./mind_data"
    config.agents["general"].context_files = ["./mind_data/SOUL.md"]

    validated_workspace = config.agents["general"].memory_file_path
    validated_context = config.agents["general"].context_files[0]

    assert validated_workspace == "mind_data"
    assert validated_context == "mind_data/SOUL.md"
    assert (
        resolve_agent_owned_path(
            validated_context,
            agent_name="general",
            base_storage_path=tmp_path,
        )
        == agent_workspace_root_path(tmp_path, "general") / "mind_data" / "SOUL.md"
    )


def test_resolve_worker_key_encodes_tenant_parts_that_would_break_round_tripping(tmp_path: Path) -> None:
    """Worker keys should stay parseable even when tenant/account identifiers contain ':'."""
    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id="tenant:west",
    )

    worker_key = resolve_worker_key("shared", execution_identity, agent_name="general")

    assert worker_key == "v1:tenant_west:shared:general"
    assert visible_agent_state_roots_for_worker_key(tmp_path, worker_key) == (
        agent_state_root_path(tmp_path, "general"),
    )


def test_shared_storage_root_does_not_peel_false_positive_agents_parent(tmp_path: Path) -> None:
    """A storage root nested under a directory named `agents` should remain unchanged."""
    storage_root = tmp_path / "agents" / "mindroom_data"

    assert shared_storage_root(storage_root) == storage_root.resolve()


def test_resolve_agent_state_storage_path_accepts_pre_resolved_agent_root(tmp_path: Path) -> None:
    """Already-resolved canonical agent roots should not gain an extra `agents/<name>` layer."""
    agent_root = agent_state_root_path(tmp_path, "general")

    assert resolve_agent_state_storage_path(agent_name="general", base_storage_path=agent_root) == agent_root


def test_resolve_agent_owned_path_rejects_absolute_paths(tmp_path: Path) -> None:
    """Agent-owned paths must not point outside the canonical workspace."""
    with pytest.raises(ValueError, match="workspace-relative"):
        resolve_agent_owned_path(
            str(tmp_path / "external" / "SOUL.md"),
            agent_name="general",
            base_storage_path=tmp_path,
        )


def test_resolve_agent_owned_path_rejects_path_traversal(tmp_path: Path) -> None:
    """Agent-owned paths must stay inside the canonical agent workspace."""
    with pytest.raises(ValueError, match="stay within the agent workspace"):
        resolve_agent_owned_path(
            "../escape.md",
            agent_name="general",
            base_storage_path=tmp_path,
        )


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_reads_canonical_context_files_and_reloads_from_agent_root(
    mock_storage: MagicMock,  # noqa: ARG001
    mock_get_tool_by_name: MagicMock,
    tmp_path: Path,
) -> None:
    """Context files should be read live from the canonical agent root across scopes."""
    mock_get_tool_by_name.return_value = MagicMock()

    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = "mind_data"
    config.agents["general"].context_files = ["mind_data/SOUL.md"]
    config.agents["general"].tools = ["coding"]
    config.agents["general"].include_default_tools = False
    config.agents["general"].worker_scope = "user"
    config.agents["general"].worker_tools = ["coding"]

    canonical_workspace = agent_workspace_root_path(tmp_path, "general") / "mind_data"
    canonical_workspace.mkdir(parents=True, exist_ok=True)
    canonical_soul = canonical_workspace / "SOUL.md"
    canonical_soul.write_text("Canonical soul context.", encoding="utf-8")

    execution_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
    )

    with tool_execution_identity(execution_identity):
        agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    assert "Canonical soul context." in agent.role
    assert mock_get_tool_by_name.call_args is not None
    assert mock_get_tool_by_name.call_args.kwargs["tool_init_overrides"] == {"base_dir": str(canonical_workspace)}

    canonical_soul.write_text("Updated canonical soul context.", encoding="utf-8")

    with tool_execution_identity(execution_identity):
        updated_agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    assert "Updated canonical soul context." in updated_agent.role

    canonical_soul.unlink()

    with tool_execution_identity(execution_identity):
        deleted_agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    assert not canonical_soul.exists()
    assert "Canonical soul context." not in deleted_agent.role
    assert "Updated canonical soul context." not in deleted_agent.role


@patch("mindroom.agents.SqliteDb")
def test_create_agent_scaffolds_default_mind_workspace_under_runtime_storage_root(
    _mock_storage: MagicMock,  # noqa: PT019
    tmp_path: Path,
) -> None:
    """The default starter Mind profile should materialize its workspace under the active runtime root."""
    runtime_storage = tmp_path / "runtime-storage"
    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                role="Personal assistant",
                model="default",
                rooms=["personal"],
                tools=[],
                include_default_tools=False,
                learning=False,
                memory_backend="file",
                memory_file_path="mind_data",
                context_files=[
                    "mind_data/SOUL.md",
                    "mind_data/AGENTS.md",
                    "mind_data/USER.md",
                    "mind_data/IDENTITY.md",
                    "mind_data/TOOLS.md",
                    "mind_data/HEARTBEAT.md",
                ],
                knowledge_bases=["mind_memory"],
            ),
        },
        knowledge_bases={
            "mind_memory": KnowledgeBaseConfig(
                path="${MINDROOM_STORAGE_PATH}/agents/mind/workspace/mind_data/memory",
                watch=True,
            ),
        },
        models={"default": ModelConfig(provider="openai", id="gpt-4")},
    )

    agent = _create_agent_for_test("mind", config=_bind_runtime_paths(config, _runtime_paths(runtime_storage)))

    workspace = runtime_storage / "agents" / "mind" / "workspace" / "mind_data"
    assert (workspace / "SOUL.md").exists()
    assert (workspace / "AGENTS.md").exists()
    assert (workspace / "USER.md").exists()
    assert (workspace / "IDENTITY.md").exists()
    assert (workspace / "TOOLS.md").exists()
    assert (workspace / "HEARTBEAT.md").exists()
    assert (workspace / "MEMORY.md").exists()
    assert "## Personality Context" in agent.role


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_uses_unscoped_kubernetes_worker_workspace_for_dedicated_tools(
    mock_storage: MagicMock,
    mock_get_tool_by_name: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kubernetes-backed unscoped agents should still use the canonical agent workspace."""
    mock_get_tool_by_name.return_value = MagicMock()

    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = "./mind_data"
    config.agents["general"].tools = ["coding"]
    config.agents["general"].include_default_tools = False

    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("MINDROOM_WORKER_BACKEND", "kubernetes")
    monkeypatch.setenv("CUSTOMER_ID", "tenant-123")
    runtime_paths = _runtime_paths(tmp_path, config_path=config_dir / "config.yaml")

    _create_agent_for_test("general", config=_bind_runtime_paths(config, runtime_paths))

    agent_root = agent_state_root_path(tmp_path, "general")
    canonical_workspace = agent_workspace_root_path(tmp_path, "general") / "mind_data"
    db_files = [Path(str(call.kwargs["db_file"])) for call in mock_storage.call_args_list]
    assert agent_root / "sessions" / "general.db" in db_files
    assert agent_root / "learning" / "general.db" in db_files
    assert mock_get_tool_by_name.call_args is not None
    assert mock_get_tool_by_name.call_args.kwargs["tool_init_overrides"] == {"base_dir": str(canonical_workspace)}


@patch("mindroom.agents.get_tool_by_name")
@patch("mindroom.agents.SqliteDb")
def test_create_agent_uses_mounted_dedicated_worker_root_for_unscoped_agent_state(
    mock_storage: MagicMock,
    mock_get_tool_by_name: MagicMock,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dedicated worker runtime roots should not change the canonical agent-owned paths."""
    mock_get_tool_by_name.return_value = MagicMock()

    config = Config.from_yaml()
    config.agents["general"].memory_backend = "file"
    config.agents["general"].memory_file_path = "./mind_data"
    config.agents["general"].tools = ["coding"]
    config.agents["general"].include_default_tools = False

    shared_root = tmp_path / "shared-storage"
    worker_key = resolve_unscoped_worker_key(agent_name="general")
    dedicated_root = worker_root_path(shared_root, worker_key)
    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("MINDROOM_WORKER_BACKEND", raising=False)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_KEY", worker_key)
    monkeypatch.setenv("MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT", str(dedicated_root))
    runtime_paths = _runtime_paths(shared_root, config_path=config_dir / "config.yaml")

    _create_agent_for_test("general", config=_bind_runtime_paths(config, runtime_paths))

    agent_root = agent_state_root_path(shared_root, "general")
    canonical_workspace = agent_workspace_root_path(shared_root, "general") / "mind_data"
    db_files = [Path(str(call.kwargs["db_file"])) for call in mock_storage.call_args_list]
    assert agent_root / "sessions" / "general.db" in db_files
    assert agent_root / "learning" / "general.db" in db_files
    assert not any(path.is_relative_to(dedicated_root) for path in db_files)
    assert mock_get_tool_by_name.call_args is not None
    assert mock_get_tool_by_name.call_args.kwargs["tool_init_overrides"] == {"base_dir": str(canonical_workspace)}


@patch("mindroom.agents.SqliteDb")
def test_agent_context_files_are_loaded_into_role(mock_storage: MagicMock, tmp_path: Path) -> None:  # noqa: ARG001
    """Context files should load directly from the canonical workspace."""
    config = Config.from_yaml()
    workspace = agent_workspace_root_path(tmp_path, "general")
    soul_path = workspace / "SOUL.md"
    user_path = workspace / "USER.md"
    workspace.mkdir(parents=True, exist_ok=True)
    soul_path.write_text("Core personality directive.", encoding="utf-8")
    user_path.write_text("User preference: concise answers.", encoding="utf-8")

    config.agents["general"].context_files = ["SOUL.md", "USER.md"]

    agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    assert "## Personality Context" in agent.role
    assert "### SOUL.md" in agent.role
    assert "Core personality directive." in agent.role
    assert "### USER.md" in agent.role
    assert "User preference: concise answers." in agent.role
    soul_path.write_text("Canonical soul directive.", encoding="utf-8")

    updated_agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    assert "Canonical soul directive." in updated_agent.role


@patch("mindroom.agents.SqliteDb")
def test_agent_preload_cap_truncates_context_files_in_order(
    mock_storage: MagicMock,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Preload cap should drop earlier context files before later ones."""
    config = Config.from_yaml()
    config.defaults.max_preload_chars = 420

    workspace = agent_workspace_root_path(tmp_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    first_path = workspace / "FIRST.md"
    second_path = workspace / "SECOND.md"
    first_path.write_text("FIRST_START " + "A" * 220 + " FIRST_END", encoding="utf-8")
    second_path.write_text("SECOND_START " + "B" * 220 + " SECOND_END", encoding="utf-8")

    config.agents["general"].context_files = ["FIRST.md", "SECOND.md"]

    agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    assert "[Content truncated - " in agent.role
    assert "### FIRST.md" not in agent.role
    assert "### SECOND.md" in agent.role
    assert "SECOND_START" in agent.role


@patch("mindroom.agents.SqliteDb")
def test_agent_missing_context_file_is_ignored(mock_storage: MagicMock, tmp_path: Path) -> None:  # noqa: ARG001
    """Missing context files should not prevent agent creation."""
    config = Config.from_yaml()
    config.agents["general"].context_files = ["does-not-exist.md"]

    agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))

    assert "## Personality Context" not in agent.role
    assert "does-not-exist.md" not in agent.role


def test_agent_relative_context_paths_resolve_from_workspace_not_cwd(tmp_path: Path) -> None:
    """Relative context paths should resolve from the canonical workspace, not CWD."""
    config = Config.from_yaml()
    workspace = agent_workspace_root_path(tmp_path, "general")
    workspace.mkdir(parents=True, exist_ok=True)
    soul_path = workspace / "SOUL.md"
    soul_path.write_text("Relative soul context.", encoding="utf-8")

    config.agents["general"].context_files = ["SOUL.md"]

    original_cwd = Path.cwd()
    other_cwd = tmp_path / "other"
    other_cwd.mkdir(parents=True, exist_ok=True)
    os.chdir(other_cwd)
    try:
        with patch("mindroom.agents.SqliteDb"):
            agent = _create_agent_for_test("general", config=_bind_runtime_paths(config, _runtime_paths(tmp_path)))
    finally:
        os.chdir(original_cwd)

    assert "Relative soul context." in agent.role


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

    assert config.agents["general"].memory_file_path == "openclaw_data"


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
    runtime_paths = _runtime_paths(tmp_path)
    with patch("mindroom.ai.get_model_instance", return_value=model):
        _create_agent_for_test(
            "agent_one",
            config=_bind_runtime_paths(config, runtime_paths),
            include_interactive_questions=False,
        )
        _create_agent_for_test(
            "agent_two",
            config=config,
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
    runtime_paths = _runtime_paths(tmp_path)
    with patch("mindroom.ai.get_model_instance", return_value=model) as mock_get_model_instance:
        _create_agent_for_test(
            "agent_one",
            config=_bind_runtime_paths(config, runtime_paths),
            include_interactive_questions=False,
        )

    mock_get_model_instance.assert_called_once_with(config, "m1")
    assert mock_agent_class.call_count == 1
    assert mock_storage.call_count >= 2
    assert mock_culture_manager_class.call_args is not None
    assert mock_culture_manager_class.call_args.kwargs["model"] is model
