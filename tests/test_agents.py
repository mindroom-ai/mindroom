from unittest.mock import MagicMock, patch

import pytest
from agno.agent import Agent
from agno.models.ollama import Ollama

from mindroom.agents import get_agent, list_agents


@patch("mindroom.agents.base.SqliteStorage")
def test_get_agent_calculator(mock_storage: MagicMock) -> None:
    """Tests that the calculator agent is created correctly."""
    model = Ollama(id="test")
    agent = get_agent("calculator", model)
    assert isinstance(agent, Agent)
    assert agent.name == "CalculatorAgent"
    mock_storage.assert_called_once_with(table_name="calculator_sessions", db_file="tmp/calculator.db")


@patch("mindroom.agents.base.SqliteStorage")
def test_get_agent_general(mock_storage: MagicMock) -> None:
    """Tests that the general agent is created correctly."""
    model = Ollama(id="test")
    agent = get_agent("general", model)
    assert isinstance(agent, Agent)
    assert agent.name == "GeneralAgent"
    mock_storage.assert_called_once_with(table_name="general_sessions", db_file="tmp/general.db")


@patch("mindroom.agents.base.SqliteStorage")
def test_get_agent_code(mock_storage: MagicMock) -> None:
    """Tests that the code agent is created correctly."""
    model = Ollama(id="test")
    agent = get_agent("code", model)
    assert isinstance(agent, Agent)
    assert agent.name == "CodeAgent"
    mock_storage.assert_called_once_with(table_name="code_sessions", db_file="tmp/code.db")


@patch("mindroom.agents.base.SqliteStorage")
def test_get_agent_shell(mock_storage: MagicMock) -> None:
    """Tests that the shell agent is created correctly."""
    model = Ollama(id="test")
    agent = get_agent("shell", model)
    assert isinstance(agent, Agent)
    assert agent.name == "ShellAgent"
    mock_storage.assert_called_once_with(table_name="shell_sessions", db_file="tmp/shell.db")


@patch("mindroom.agents.base.SqliteStorage")
def test_get_agent_summary(mock_storage: MagicMock) -> None:
    """Tests that the summary agent is created correctly."""
    model = Ollama(id="test")
    agent = get_agent("summary", model)
    assert isinstance(agent, Agent)
    assert agent.name == "SummaryAgent"
    mock_storage.assert_called_once_with(table_name="summary_sessions", db_file="tmp/summary.db")


def test_get_agent_unknown() -> None:
    """Tests that an unknown agent raises a ValueError."""
    model = Ollama(id="test")
    with pytest.raises(ValueError) as exc_info:
        get_agent("unknown", model)
    assert "Unknown agent: unknown" in str(exc_info.value)
    assert "Available agents:" in str(exc_info.value)


def test_list_agents() -> None:
    """Tests that list_agents returns all available agents."""
    agents = list_agents()
    assert isinstance(agents, list)
    assert "calculator" in agents
    assert "general" in agents
    assert "code" in agents
    assert "shell" in agents
    assert "summary" in agents
    assert len(agents) == 5
    # Check they are sorted
    assert agents == sorted(agents)
