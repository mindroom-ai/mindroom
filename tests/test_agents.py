"""Tests for MindRoom agent functionality."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from agno.agent import Agent

from mindroom.agents import create_agent
from mindroom.config import Config

if TYPE_CHECKING:
    from pathlib import Path


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_calculator(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the calculator agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("calculator", storage_path=tmp_path, config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "CalculatorAgent"
    expected_path = tmp_path / "state" / "agents" / "sessions" / "calculator.db"
    mock_storage.assert_called_once_with(table_name="calculator_sessions", db_file=str(expected_path))


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_general(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the general agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("general", storage_path=tmp_path, config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "GeneralAgent"
    expected_path = tmp_path / "state" / "agents" / "sessions" / "general.db"
    mock_storage.assert_called_once_with(table_name="general_sessions", db_file=str(expected_path))


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_code(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the code agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("code", storage_path=tmp_path, config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "CodeAgent"
    expected_path = tmp_path / "state" / "agents" / "sessions" / "code.db"
    mock_storage.assert_called_once_with(table_name="code_sessions", db_file=str(expected_path))


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_shell(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the shell agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("shell", storage_path=tmp_path, config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "ShellAgent"
    expected_path = tmp_path / "state" / "agents" / "sessions" / "shell.db"
    mock_storage.assert_called_once_with(table_name="shell_sessions", db_file=str(expected_path))


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_summary(mock_storage: MagicMock, tmp_path: Path) -> None:
    """Tests that the summary agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("summary", storage_path=tmp_path, config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "SummaryAgent"
    expected_path = tmp_path / "state" / "agents" / "sessions" / "summary.db"
    mock_storage.assert_called_once_with(table_name="summary_sessions", db_file=str(expected_path))


def test_get_agent_unknown(tmp_path: Path) -> None:
    """Tests that an unknown agent raises a ValueError."""
    config = Config.from_yaml()
    with pytest.raises(ValueError, match="Unknown agent: unknown"):
        create_agent("unknown", storage_path=tmp_path, config=config)
