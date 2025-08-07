"""Tests for unified Matrix ID handling."""

import pytest

from mindroom.matrix import MatrixID, ThreadStateKey, extract_agent_name, is_agent_id, parse_matrix_id


class TestMatrixID:
    """Test the MatrixID class."""

    def test_parse_valid_matrix_id(self):
        """Test parsing a valid Matrix ID."""
        mid = MatrixID.parse("@mindroom_calculator:localhost")
        assert mid.username == "mindroom_calculator"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_calculator:localhost"

    def test_parse_invalid_matrix_id(self):
        """Test parsing invalid Matrix IDs."""
        with pytest.raises(ValueError):
            MatrixID.parse("invalid")

        with pytest.raises(ValueError):
            MatrixID.parse("@nodomainpart")

    def test_from_agent(self):
        """Test creating MatrixID from agent name."""
        mid = MatrixID.from_agent("calculator", "localhost")
        assert mid.username == "mindroom_calculator"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_calculator:localhost"

    def test_is_agent(self):
        """Test agent detection."""
        agent_id = MatrixID.parse("@mindroom_calculator:localhost")
        assert agent_id.is_agent is True

        user_id = MatrixID.parse("@user:localhost")
        assert user_id.is_agent is False

    def test_is_mindroom_domain(self):
        """Test mindroom.space domain detection."""
        mindroom_id = MatrixID.parse("@mindroom_calculator:mindroom.space")
        assert mindroom_id.is_mindroom_domain is True

        other_id = MatrixID.parse("@mindroom_calculator:localhost")
        assert other_id.is_mindroom_domain is False

    def test_agent_name_extraction(self):
        """Test extracting agent name."""
        # Valid agent
        mid = MatrixID.parse("@mindroom_calculator:localhost")
        assert mid.agent_name == "calculator"

        # Not an agent
        mid = MatrixID.parse("@user:localhost")
        assert mid.agent_name is None

        # Agent prefix but not in config
        mid = MatrixID.parse("@mindroom_unknown:localhost")
        assert mid.agent_name is None

    def test_parse_router(self):
        """Test parsing a router agent ID."""
        mid = MatrixID.parse("@mindroom_router:localhost")
        assert mid.username == "mindroom_router"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_router:localhost"
        assert mid.is_agent is True
        assert mid.agent_name == "router"


class TestThreadStateKey:
    """Test the ThreadStateKey class."""

    def test_parse_state_key(self):
        """Test parsing a state key."""
        key = ThreadStateKey.parse("$thread123:calculator")
        assert key.thread_id == "$thread123"
        assert key.agent_name == "calculator"
        assert key.key == "$thread123:calculator"

    def test_parse_invalid_state_key(self):
        """Test parsing invalid state keys."""
        with pytest.raises(ValueError):
            ThreadStateKey.parse("invalid")

    def test_create_state_key(self):
        """Test creating a state key."""
        key = ThreadStateKey("$thread456", "general")
        assert key.thread_id == "$thread456"
        assert key.agent_name == "general"
        assert key.key == "$thread456:general"


class TestHelperFunctions:
    """Test helper functions."""

    def test_is_agent_id(self):
        """Test quick agent ID check."""
        assert is_agent_id("@mindroom_calculator:localhost") is True
        assert is_agent_id("@mindroom_general:localhost") is True
        assert is_agent_id("@user:localhost") is False
        assert is_agent_id("invalid") is False
        assert is_agent_id("@mindroom_unknown:localhost") is False

    def test_extract_agent_name(self):
        """Test agent name extraction."""
        assert extract_agent_name("@mindroom_calculator:localhost") == "calculator"
        assert extract_agent_name("@mindroom_general:localhost") == "general"
        assert extract_agent_name("@user:localhost") is None
        assert extract_agent_name("invalid") is None

    def test_parse_matrix_id_caching(self):
        """Test that parsing is cached."""
        # First call
        mid1 = parse_matrix_id("@mindroom_calculator:localhost")
        # Second call should return same object (cached)
        mid2 = parse_matrix_id("@mindroom_calculator:localhost")
        assert mid1 is mid2
