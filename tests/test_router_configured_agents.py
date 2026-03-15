"""Test that router only suggests agents configured for the room."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from mindroom.authorization import get_available_agents_in_room
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.thread_utils import get_configured_agents_for_room
from tests.conftest import bind_runtime_paths, orchestrator_runtime_paths, runtime_paths_for


class TestRouterAgentSelection:
    """Test router agent selection logic."""

    @staticmethod
    def _bind_runtime(config: Config) -> Config:
        runtime_root = Path(tempfile.mkdtemp())
        return bind_runtime_paths(
            config,
            orchestrator_runtime_paths(runtime_root, config_path=runtime_root / "config.yaml"),
        )

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = self._bind_runtime(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="Calculator",
                        rooms=["#math:localhost", "#general:localhost"],
                    ),
                    "research": AgentConfig(
                        display_name="Research Assistant",
                        rooms=["#research:localhost", "#general:localhost"],
                    ),
                    "writer": AgentConfig(
                        display_name="Writer",
                        rooms=["#writing:localhost"],  # NOT in general room
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="test", id="test-model")},
            ),
        )

    def test_get_configured_agents_returns_only_configured(self) -> None:
        """Test that get_configured_agents_for_room only returns configured agents."""
        runtime_paths = runtime_paths_for(self.config)
        # Test general room - should have calculator and research
        configured = get_configured_agents_for_room("#general:localhost", self.config, runtime_paths)
        configured_names = [mid.agent_name(self.config, runtime_paths) for mid in configured]
        assert configured_names == ["calculator", "research"]
        assert "writer" not in configured_names

        # Test math room - should only have calculator
        configured = get_configured_agents_for_room("#math:localhost", self.config, runtime_paths)
        configured_names = [mid.agent_name(self.config, runtime_paths) for mid in configured]
        assert configured_names == ["calculator"]
        assert "research" not in configured_names
        assert "writer" not in configured_names

        # Test writing room - should only have writer
        configured = get_configured_agents_for_room("#writing:localhost", self.config, runtime_paths)
        configured_names = [mid.agent_name(self.config, runtime_paths) for mid in configured]
        assert configured_names == ["writer"]
        assert "calculator" not in configured_names
        assert "research" not in configured_names

        # Test non-existent room - should have no agents
        configured = get_configured_agents_for_room("#unknown:localhost", self.config, runtime_paths)
        assert configured == []

    def test_get_available_agents_returns_all_in_room(self) -> None:
        """Test that get_available_agents_in_room returns all agents present."""
        runtime_paths = runtime_paths_for(self.config)
        # Mock room with agents that are both configured and not configured
        room = MagicMock()
        room.room_id = "#general:localhost"
        room.users = {
            "@mindroom_calculator:localhost": None,  # Configured for this room
            "@mindroom_research:localhost": None,  # Configured for this room
            "@mindroom_writer:localhost": None,  # NOT configured but present
            "@user:localhost": None,  # Regular user
        }

        # Should return ALL agents in the room (for individual response logic)
        available = get_available_agents_in_room(room, self.config, runtime_paths)
        available_names = [mid.agent_name(self.config, runtime_paths) for mid in available]
        assert "calculator" in available_names
        assert "research" in available_names
        assert "writer" in available_names  # Present but not configured

    def test_router_should_use_configured_agents_only(self) -> None:
        """Test that router should only consider configured agents."""
        runtime_paths = runtime_paths_for(self.config)
        room = MagicMock()
        room.room_id = "#general:localhost"
        room.users = {
            "@mindroom_calculator:localhost": None,  # Configured
            "@mindroom_research:localhost": None,  # Configured
            "@mindroom_writer:localhost": None,  # NOT configured but present
        }

        # For routing decisions, use configured agents only
        configured = get_configured_agents_for_room(room.room_id, self.config, runtime_paths)
        configured_names = [mid.agent_name(self.config, runtime_paths) for mid in configured]
        assert configured_names == ["calculator", "research"]
        assert "writer" not in configured_names

        # But for individual response decisions, consider all agents
        available = get_available_agents_in_room(room, self.config, runtime_paths)
        assert len(available) == 3  # All agents in room

    def test_router_excludes_itself(self) -> None:
        """Test that router agent is excluded from available agents."""
        config_with_router = self._bind_runtime(
            Config(
                agents={
                    "calculator": AgentConfig(
                        display_name="Calculator",
                        rooms=["#general:localhost"],
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="test", id="test-model")},
            ),
        )

        room = MagicMock()
        room.room_id = "#general:localhost"
        room.users = {
            "@mindroom_calculator:localhost": None,
            "@mindroom_router:localhost": None,  # Router is present in the room
        }

        # Router should be excluded from configured agents (it's not in config.agents)
        runtime_paths = runtime_paths_for(config_with_router)
        configured = get_configured_agents_for_room(room.room_id, config_with_router, runtime_paths)
        configured_names = [mid.agent_name(config_with_router, runtime_paths) for mid in configured]
        assert configured_names == ["calculator"]
        assert "router" not in configured_names

        # Router should be excluded from available agents in room
        available = get_available_agents_in_room(room, config_with_router, runtime_paths)
        available_names = [mid.agent_name(config_with_router, runtime_paths) for mid in available]
        assert available_names == ["calculator"]
        assert "router" not in available_names
