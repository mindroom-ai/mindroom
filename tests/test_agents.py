"""Tests for MindRoom agent functionality."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

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
            "agent_one": AgentConfig(display_name="Agent One", role="First", learning=False),
            "agent_two": AgentConfig(display_name="Agent Two", role="Second", learning=False),
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
            include_default_tools=False,
            include_interactive_questions=False,
        )
        create_agent(
            "agent_two",
            config=config,
            storage_path=tmp_path,
            include_default_tools=False,
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
            "agent_one": AgentConfig(display_name="Agent One", role="First", model="m1", learning=False),
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
            include_default_tools=False,
            include_interactive_questions=False,
        )

    mock_get_model_instance.assert_called_once_with(config, "m1")
    assert mock_agent_class.call_count == 1
    assert mock_storage.call_count >= 2
    assert mock_culture_manager_class.call_args is not None
    assert mock_culture_manager_class.call_args.kwargs["model"] is model
