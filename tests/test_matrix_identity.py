"""Tests for unified Matrix ID handling."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom import constants as constants_mod
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.identity import (
    MatrixID,
    _ThreadStateKey,
    agent_username_localpart,
    extract_agent_name,
    is_agent_id,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _bind_runtime_paths(config: Config, tmp_path: Path) -> Config:
    return bind_runtime_paths(config, test_runtime_paths(tmp_path))


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

    def test_from_agent(self, tmp_path: Path) -> None:
        """Test creating MatrixID from agent name."""
        mid = MatrixID.from_agent(
            "calculator",
            "localhost",
            runtime_paths_for(_bind_runtime_paths(self.config, tmp_path)),
        )
        assert mid.username == "mindroom_calculator"
        assert mid.domain == "localhost"
        assert mid.full_id == "@mindroom_calculator:localhost"

    def test_agent_name_extraction(self, tmp_path: Path) -> None:
        """Test extracting agent name."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        domain = self.config.get_domain(runtime_paths_for(self.config))

        # Valid agent
        mid = MatrixID.parse(f"@mindroom_calculator:{domain}")
        assert mid.agent_name(self.config, runtime_paths_for(self.config)) == "calculator"

        # Not an agent
        mid = MatrixID.parse(f"@user:{domain}")
        assert mid.agent_name(self.config, runtime_paths_for(self.config)) is None

        # Agent prefix but not in config
        mid = MatrixID.parse(f"@mindroom_unknown:{domain}")
        assert mid.agent_name(self.config, runtime_paths_for(self.config)) is None

    def test_parse_router(self, tmp_path: Path) -> None:
        """Test parsing a router agent ID."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        domain = self.config.get_domain(runtime_paths_for(self.config))
        mid = MatrixID.parse(f"@mindroom_router:{domain}")
        assert mid.username == "mindroom_router"
        assert mid.domain == domain
        assert mid.full_id == f"@mindroom_router:{domain}"
        assert mid.agent_name(self.config, runtime_paths_for(self.config)) == "router"

    def test_namespaced_agent_localpart_and_parsing(self, tmp_path: Path) -> None:
        """Agent IDs should use and require configured namespace suffixes."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=config_path,
            process_env={"MINDROOM_NAMESPACE": "a1b2c3d4"},
        )
        config = Config(
            agents=self.config.agents,
            teams=self.config.teams,
            room_models=self.config.room_models,
            models=self.config.models,
        )
        config = Config.validate_with_runtime(config.model_dump(exclude_none=True), runtime_paths)
        domain = config.get_domain(runtime_paths)

        assert agent_username_localpart("calculator", runtime_paths=runtime_paths) == "mindroom_calculator_a1b2c3d4"
        namespaced_id = MatrixID.parse(f"@mindroom_calculator_a1b2c3d4:{domain}")
        assert namespaced_id.agent_name(config, runtime_paths) == "calculator"

        legacy_id = MatrixID.parse(f"@mindroom_calculator:{domain}")
        assert legacy_id.agent_name(config, runtime_paths) is None


class TestThreadStateKey:
    """Test the ThreadStateKey class."""

    def test_parse_state_key(self) -> None:
        """Test parsing a state key."""
        key = _ThreadStateKey.parse("$thread123:calculator")
        assert key.thread_id == "$thread123"
        assert key.agent_name == "calculator"
        assert key.key == "$thread123:calculator"

    def test_parse_invalid_state_key(self) -> None:
        """Test parsing invalid state keys."""
        with pytest.raises(ValueError, match="Invalid state key"):
            _ThreadStateKey.parse("invalid")

    def test_create_state_key(self) -> None:
        """Test creating a state key."""
        key = _ThreadStateKey("$thread456", "general")
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

    def test_is_agent_id(self, tmp_path: Path) -> None:
        """Test quick agent ID check."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        domain = self.config.get_domain(runtime_paths_for(self.config))
        runtime_paths = runtime_paths_for(self.config)
        assert is_agent_id(f"@mindroom_calculator:{domain}", self.config, runtime_paths) is True
        assert is_agent_id(f"@mindroom_general:{domain}", self.config, runtime_paths) is True
        assert is_agent_id(f"@user:{domain}", self.config, runtime_paths) is False
        # Note: is_agent_id expects valid Matrix IDs - invalid IDs should never reach this function
        assert is_agent_id(f"@mindroom_unknown:{domain}", self.config, runtime_paths) is False

    def test_extract_agent_name(self, tmp_path: Path) -> None:
        """Test agent name extraction."""
        self.config = _bind_runtime_paths(self.config, tmp_path)
        domain = self.config.get_domain(runtime_paths_for(self.config))
        runtime_paths = runtime_paths_for(self.config)
        assert extract_agent_name(f"@mindroom_calculator:{domain}", self.config, runtime_paths) == "calculator"
        assert extract_agent_name(f"@mindroom_general:{domain}", self.config, runtime_paths) == "general"
        assert extract_agent_name(f"@user:{domain}", self.config, runtime_paths) is None
        assert extract_agent_name("invalid", self.config, runtime_paths) is None
