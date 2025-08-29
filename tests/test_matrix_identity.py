"""Tests for unified Matrix ID handling."""

from __future__ import annotations

import pytest

from mindroom.config import AgentConfig, Config, ModelConfig
from mindroom.matrix.identity import MatrixID, ThreadStateKey, extract_agent_name, is_agent_id


class TestMatrixID:
    """Test the MatrixID class."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
                "general": AgentConfig(display_name="General", rooms=["#test:example.org"]),
                "router": AgentConfig(display_name="Router", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    def test_parse_valid_matrix_id(self) -> None:
        """Test parsing a valid Matrix ID."""
        mid = MatrixID.parse("@mindroom_calculator:localhost")
        assert mid.username == "mindroom_calculator"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_calculator:localhost"

    def test_parse_invalid_matrix_id(self) -> None:
        """Test parsing invalid Matrix IDs."""
        with pytest.raises(ValueError, match="Invalid Matrix ID"):
            MatrixID.parse("invalid")

        with pytest.raises(ValueError, match="Invalid Matrix ID, missing domain"):
            MatrixID.parse("@nodomainpart")

    def test_from_agent(self) -> None:
        """Test creating MatrixID from agent name."""
        mid = MatrixID.from_agent("calculator", "localhost")
        assert mid.username == "mindroom_calculator"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_calculator:localhost"

    def test_agent_name_extraction(self) -> None:
        """Test extracting agent name."""
        # Valid agent
        mid = MatrixID.parse("@mindroom_calculator:localhost")
        assert mid.agent_name(self.config) == "calculator"

        # Not an agent
        mid = MatrixID.parse("@user:localhost")
        assert mid.agent_name(self.config) is None

        # Agent prefix but not in config
        mid = MatrixID.parse("@mindroom_unknown:localhost")
        assert mid.agent_name(self.config) is None

    def test_parse_router(self) -> None:
        """Test parsing a router agent ID."""
        mid = MatrixID.parse("@mindroom_router:localhost")
        assert mid.username == "mindroom_router"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_router:localhost"
        assert mid.agent_name(self.config) == "router"


class TestThreadStateKey:
    """Test the ThreadStateKey class."""

    def test_parse_state_key(self) -> None:
        """Test parsing a state key."""
        key = ThreadStateKey.parse("$thread123:calculator")
        assert key.thread_id == "$thread123"
        assert key.agent_name == "calculator"
        assert key.key == "$thread123:calculator"

    def test_parse_invalid_state_key(self) -> None:
        """Test parsing invalid state keys."""
        with pytest.raises(ValueError, match="Invalid state key"):
            ThreadStateKey.parse("invalid")

    def test_create_state_key(self) -> None:
        """Test creating a state key."""
        key = ThreadStateKey("$thread456", "general")
        assert key.thread_id == "$thread456"
        assert key.agent_name == "general"
        assert key.key == "$thread456:general"


class TestHelperFunctions:
    """Test helper functions."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
                "general": AgentConfig(display_name="General", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    def test_is_agent_id(self) -> None:
        """Test quick agent ID check."""
        assert is_agent_id("@mindroom_calculator:localhost", self.config) is True
        assert is_agent_id("@mindroom_general:localhost", self.config) is True
        assert is_agent_id("@user:localhost", self.config) is False
        # Note: is_agent_id expects valid Matrix IDs - invalid IDs should never reach this function
        assert is_agent_id("@mindroom_unknown:localhost", self.config) is False

    def test_extract_agent_name(self) -> None:
        """Test agent name extraction."""
        assert extract_agent_name("@mindroom_calculator:localhost", self.config) == "calculator"
        assert extract_agent_name("@mindroom_general:localhost", self.config) == "general"
        assert extract_agent_name("@user:localhost", self.config) is None
        assert extract_agent_name("invalid", self.config) is None
