"""Tests for MindRoom agent functionality."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from agno.agent import Agent

from mindroom.agents import create_agent
from mindroom.config import Config


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_calculator(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the calculator agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("calculator", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "CalculatorAgent"


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_general(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the general agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("general", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "GeneralAgent"


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_code(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the code agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("code", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "CodeAgent"


@patch("mindroom.agents.SqliteStorage")
def test_get_agent_shell(mock_storage: MagicMock) -> None:  # noqa: ARG001
    """Tests that the shell agent is created correctly."""
    config = Config.from_yaml()
    agent = create_agent("shell", config=config)
    assert isinstance(agent, Agent)
    assert agent.name == "ShellAgent"


@patch("mindroom.agents.SqliteStorage")
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
