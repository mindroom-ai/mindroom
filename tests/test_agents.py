from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from agno.agent import Agent
from agno.models.ollama import Ollama

from mindroom.agent_loader import create_agent


@patch("mindroom.agent_loader.SqliteStorage")
def test_get_agent_calculator(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the calculator agent is created correctly."""
    model = Ollama(id="test")
    agent = create_agent("calculator", model, storage_path=tmp_path)
    assert isinstance(agent, Agent)
    assert agent.name == "CalculatorAgent"
    mock_storage.assert_called_once_with(table_name="calculator_sessions", db_file=f"{tmp_path}/calculator.db")


@patch("mindroom.agent_loader.SqliteStorage")
def test_get_agent_general(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the general agent is created correctly."""
    model = Ollama(id="test")
    agent = create_agent("general", model, storage_path=tmp_path)
    assert isinstance(agent, Agent)
    assert agent.name == "GeneralAgent"
    mock_storage.assert_called_once_with(table_name="general_sessions", db_file=f"{tmp_path}/general.db")


@patch("mindroom.agent_loader.SqliteStorage")
def test_get_agent_code(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the code agent is created correctly."""
    model = Ollama(id="test")
    agent = create_agent("code", model, storage_path=tmp_path)
    assert isinstance(agent, Agent)
    assert agent.name == "CodeAgent"
    mock_storage.assert_called_once_with(table_name="code_sessions", db_file=f"{tmp_path}/code.db")


@patch("mindroom.agent_loader.SqliteStorage")
def test_get_agent_shell(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the shell agent is created correctly."""
    model = Ollama(id="test")
    agent = create_agent("shell", model, storage_path=tmp_path)
    assert isinstance(agent, Agent)
    assert agent.name == "ShellAgent"
    mock_storage.assert_called_once_with(table_name="shell_sessions", db_file=f"{tmp_path}/shell.db")


@patch("mindroom.agent_loader.SqliteStorage")
def test_get_agent_summary(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the summary agent is created correctly."""
    model = Ollama(id="test")
    agent = create_agent("summary", model, storage_path=tmp_path)
    assert isinstance(agent, Agent)
    assert agent.name == "SummaryAgent"
    mock_storage.assert_called_once_with(table_name="summary_sessions", db_file=f"{tmp_path}/summary.db")


def test_get_agent_unknown(tmp_path: Path) -> None:
    """Tests that an unknown agent raises a ValueError."""
    model = Ollama(id="test")
    with pytest.raises(ValueError) as exc_info:
        create_agent("unknown", model, tmp_path)
    assert "Unknown agent: unknown" in str(exc_info.value)
    assert "Available agents:" in str(exc_info.value)
