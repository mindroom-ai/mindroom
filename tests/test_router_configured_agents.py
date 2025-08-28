"""Test that router only suggests agents configured for the room."""

from unittest.mock import MagicMock

from mindroom.config import AgentConfig, Config, ModelConfig
from mindroom.thread_utils import get_available_agents_in_room, get_configured_agents_for_room


class TestRouterAgentSelection:
    """Test router agent selection logic."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
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
        )

    def test_get_configured_agents_returns_only_configured(self) -> None:
        """Test that get_configured_agents_for_room only returns configured agents."""
        # Test general room - should have calculator and research
        configured = get_configured_agents_for_room("#general:localhost", self.config)
        assert configured == ["calculator", "research"]
        assert "writer" not in configured

        # Test math room - should only have calculator
        configured = get_configured_agents_for_room("#math:localhost", self.config)
        assert configured == ["calculator"]
        assert "research" not in configured
        assert "writer" not in configured

        # Test writing room - should only have writer
        configured = get_configured_agents_for_room("#writing:localhost", self.config)
        assert configured == ["writer"]
        assert "calculator" not in configured
        assert "research" not in configured

        # Test non-existent room - should have no agents
        configured = get_configured_agents_for_room("#unknown:localhost", self.config)
        assert configured == []

    def test_get_available_agents_returns_all_in_room(self) -> None:
        """Test that get_available_agents_in_room returns all agents present."""
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
        available = get_available_agents_in_room(room, self.config)
        assert "calculator" in available
        assert "research" in available
        assert "writer" in available  # Present but not configured

    def test_router_should_use_configured_agents_only(self) -> None:
        """Test that router should only consider configured agents."""
        room = MagicMock()
        room.room_id = "#general:localhost"
        room.users = {
            "@mindroom_calculator:localhost": None,  # Configured
            "@mindroom_research:localhost": None,  # Configured
            "@mindroom_writer:localhost": None,  # NOT configured but present
        }

        # For routing decisions, use configured agents only
        configured = get_configured_agents_for_room(room.room_id, self.config)
        assert configured == ["calculator", "research"]
        assert "writer" not in configured

        # But for individual response decisions, consider all agents
        available = get_available_agents_in_room(room, self.config)
        assert len(available) == 3  # All agents in room

    def test_router_excludes_itself(self) -> None:
        """Test that router agent is excluded from available agents."""
        # Add router to config
        config_with_router = Config(
            agents={
                "calculator": AgentConfig(
                    display_name="Calculator",
                    rooms=["#general:localhost"],
                ),
                "router": AgentConfig(
                    display_name="Router",
                    rooms=["#general:localhost"],
                ),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="test", id="test-model")},
        )

        room = MagicMock()
        room.room_id = "#general:localhost"
        room.users = {
            "@mindroom_calculator:localhost": None,
            "@mindroom_router:localhost": None,
        }

        # Router should be excluded from both functions
        configured = get_configured_agents_for_room(room.room_id, config_with_router)
        assert configured == ["calculator"]
        assert "router" not in configured

        available = get_available_agents_in_room(room, config_with_router)
        assert available == ["calculator"]
        assert "router" not in available
