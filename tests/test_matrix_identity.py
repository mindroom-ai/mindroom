"""Tests for unified Matrix ID handling."""

from __future__ import annotations

import pytest

import mindroom.matrix.identity as matrix_identity
from mindroom.config import AgentConfig, Config, ModelConfig
from mindroom.matrix.identity import MatrixID, ThreadStateKey, agent_username_localpart, extract_agent_name, is_agent_id


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
        domain = self.config.domain

        # Valid agent
        mid = MatrixID.parse(f"@mindroom_calculator:{domain}")
        assert mid.agent_name(self.config) == "calculator"

        # Not an agent
        mid = MatrixID.parse(f"@user:{domain}")
        assert mid.agent_name(self.config) is None

        # Agent prefix but not in config
        mid = MatrixID.parse(f"@mindroom_unknown:{domain}")
        assert mid.agent_name(self.config) is None

    def test_parse_router(self) -> None:
        """Test parsing a router agent ID."""
        domain = self.config.domain
        mid = MatrixID.parse(f"@mindroom_router:{domain}")
        assert mid.username == "mindroom_router"
        assert mid.domain == domain
        assert mid.full_id == f"@mindroom_router:{domain}"
        assert mid.agent_name(self.config) == "router"

    def test_namespaced_agent_localpart_and_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Agent IDs should use and require configured namespace suffixes."""
        monkeypatch.setattr(matrix_identity, "_ACTIVE_NAMESPACE", "a1b2c3d4")
        domain = self.config.domain

        assert agent_username_localpart("calculator") == "mindroom_calculator_a1b2c3d4"
        namespaced_id = MatrixID.parse(f"@mindroom_calculator_a1b2c3d4:{domain}")
        assert namespaced_id.agent_name(self.config) == "calculator"

        legacy_id = MatrixID.parse(f"@mindroom_calculator:{domain}")
        assert legacy_id.agent_name(self.config) is None


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
        domain = self.config.domain
        assert is_agent_id(f"@mindroom_calculator:{domain}", self.config) is True
        assert is_agent_id(f"@mindroom_general:{domain}", self.config) is True
        assert is_agent_id(f"@user:{domain}", self.config) is False
        # Note: is_agent_id expects valid Matrix IDs - invalid IDs should never reach this function
        assert is_agent_id(f"@mindroom_unknown:{domain}", self.config) is False

    def test_extract_agent_name(self) -> None:
        """Test agent name extraction."""
        domain = self.config.domain
        assert extract_agent_name(f"@mindroom_calculator:{domain}", self.config) == "calculator"
        assert extract_agent_name(f"@mindroom_general:{domain}", self.config) == "general"
        assert extract_agent_name(f"@user:{domain}", self.config) is None
        assert extract_agent_name("invalid", self.config) is None
