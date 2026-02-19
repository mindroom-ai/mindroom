"""Tests for MindRoom agent functionality."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from agno.agent import Agent
from agno.learn import LearningMachine, LearningMode, UserMemoryConfig, UserProfileConfig
from pydantic import ValidationError

from mindroom.agents import _CULTURE_MANAGER_CACHE, create_agent
from mindroom.config import AgentConfig, Config, CultureConfig, KnowledgeBaseConfig, ModelConfig


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
def test_agent_memory_dir_is_loaded_into_role(mock_storage: MagicMock, tmp_path: Path) -> None:  # noqa: ARG001
    """MEMORY.md plus yesterday/today memory files should be prepended to role context."""
    config = Config.from_yaml()
    config.timezone = "UTC"

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("Long-term memory summary.", encoding="utf-8")

    today = datetime.now(ZoneInfo(config.timezone)).date()
    yesterday = today - timedelta(days=1)
    (memory_dir / f"{today.isoformat()}.md").write_text("Today's memory note.", encoding="utf-8")
    (memory_dir / f"{yesterday.isoformat()}.md").write_text("Yesterday memory note.", encoding="utf-8")

    config.agents["general"].memory_dir = str(memory_dir)

    agent = create_agent("general", config=config, storage_path=tmp_path)

    assert "## Memory Context" in agent.role
    assert "### MEMORY.md" in agent.role
    assert "Long-term memory summary." in agent.role
    assert f"### {yesterday.isoformat()}.md" in agent.role
    assert "Yesterday memory note." in agent.role
    assert f"### {today.isoformat()}.md" in agent.role
    assert "Today's memory note." in agent.role


@patch("mindroom.agents.SqliteDb")
def test_agent_preload_cap_truncates_daily_before_memory_and_personality(
    mock_storage: MagicMock,  # noqa: ARG001
    tmp_path: Path,
) -> None:
    """Preload cap should drop oldest daily files before MEMORY.md and context files."""
    config = Config.from_yaml()
    config.timezone = "UTC"
    config.defaults.max_preload_chars = 620

    soul_path = tmp_path / "SOUL.md"
    soul_path.write_text("START_SOUL " + "P" * 120 + " END_SOUL", encoding="utf-8")

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("START_MEM " + "M" * 120 + " END_MEM", encoding="utf-8")
    today = datetime.now(ZoneInfo(config.timezone)).date()
    yesterday = today - timedelta(days=1)
    (memory_dir / f"{yesterday.isoformat()}.md").write_text("Y" * 140, encoding="utf-8")
    (memory_dir / f"{today.isoformat()}.md").write_text("T" * 140, encoding="utf-8")

    config.agents["general"].context_files = [str(soul_path)]
    config.agents["general"].memory_dir = str(memory_dir)

    agent = create_agent("general", config=config, storage_path=tmp_path)

    assert "[Content truncated - " in agent.role
    assert f"### {yesterday.isoformat()}.md" not in agent.role
    assert f"### {today.isoformat()}.md" in agent.role
    assert "### MEMORY.md" in agent.role
    assert "### SOUL.md" in agent.role
    # Verify trimming preserves the start (identity) and removes from the end
    assert "START_SOUL" in agent.role
    assert "START_MEM" in agent.role


@patch("mindroom.agents.SqliteDb")
def test_agent_missing_context_file_is_ignored(mock_storage: MagicMock, tmp_path: Path) -> None:  # noqa: ARG001
    """Missing context files should not prevent agent creation."""
    config = Config.from_yaml()
    config.agents["general"].context_files = [str(tmp_path / "does-not-exist.md")]

    agent = create_agent("general", config=config, storage_path=tmp_path)

    assert "## Personality Context" not in agent.role
    assert "does-not-exist.md" not in agent.role


@patch("mindroom.agents.SqliteDb")
def test_agent_missing_memory_dir_is_ignored(mock_storage: MagicMock, tmp_path: Path) -> None:  # noqa: ARG001
    """Missing memory directories should not prevent agent creation."""
    config = Config.from_yaml()
    config.agents["general"].memory_dir = str(tmp_path / "missing-memory-dir")

    agent = create_agent("general", config=config, storage_path=tmp_path)

    assert "## Memory Context" not in agent.role


def test_agent_relative_context_paths_resolve_from_config_dir(tmp_path: Path) -> None:
    """Relative context paths should resolve from the config directory, not CWD."""
    config = Config.from_yaml()
    config.timezone = "UTC"

    config_dir = tmp_path / "cfg"
    config_dir.mkdir(parents=True, exist_ok=True)
    soul_path = config_dir / "SOUL.md"
    soul_path.write_text("Relative soul context.", encoding="utf-8")
    memory_dir = config_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "MEMORY.md").write_text("Relative memory context.", encoding="utf-8")

    config.agents["general"].context_files = ["SOUL.md"]
    config.agents["general"].memory_dir = "memory"

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
    assert "Relative memory context." in agent.role


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
