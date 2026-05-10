"""Test responder candidate selection for configured and ad-hoc rooms."""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.authorization import get_available_responders_in_room, responder_candidate_entities_for_room
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.entity_resolution import configured_routable_entity_ids_for_room
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.state import MatrixState
from tests.conftest import bind_runtime_paths, orchestrator_runtime_paths, runtime_paths_for
from tests.identity_helpers import entity_names_for_ids, persist_entity_accounts


class TestResponderCandidateSelection:
    """Test responder candidate selection logic."""

    @staticmethod
    def _entity_names(config: Config, matrix_ids: list[MatrixID]) -> list[str | None]:
        runtime_paths = runtime_paths_for(config)
        return entity_names_for_ids(matrix_ids, config, runtime_paths)

    @staticmethod
    def _bind_runtime(config: Config) -> Config:
        runtime_root = Path(tempfile.mkdtemp())
        bound = bind_runtime_paths(
            config,
            orchestrator_runtime_paths(runtime_root, config_path=runtime_root / "config.yaml"),
        )
        persist_entity_accounts(bound, runtime_paths_for(bound))
        return bound

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

    def test_configured_routable_entities_returns_only_configured_entities(self) -> None:
        """Configured room candidates should include only configured agents and teams."""
        runtime_paths = runtime_paths_for(self.config)
        # Test general room - should have calculator and research
        configured = configured_routable_entity_ids_for_room(self.config, "#general:localhost", runtime_paths)
        configured_names = self._entity_names(self.config, configured)
        assert configured_names == ["calculator", "research"]
        assert "writer" not in configured_names

        # Test math room - should only have calculator
        configured = configured_routable_entity_ids_for_room(self.config, "#math:localhost", runtime_paths)
        configured_names = self._entity_names(self.config, configured)
        assert configured_names == ["calculator"]
        assert "research" not in configured_names
        assert "writer" not in configured_names

        # Test writing room - should only have writer
        configured = configured_routable_entity_ids_for_room(self.config, "#writing:localhost", runtime_paths)
        configured_names = self._entity_names(self.config, configured)
        assert configured_names == ["writer"]
        assert "calculator" not in configured_names
        assert "research" not in configured_names

        # Test non-existent room - should have no agents
        configured = configured_routable_entity_ids_for_room(self.config, "#unknown:localhost", runtime_paths)
        assert configured == []

    def test_get_available_responders_returns_all_in_room(self) -> None:
        """Present-room membership exposes every joined managed responder."""
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

        # Present-room membership still exposes every joined managed entity.
        available = get_available_responders_in_room(room, self.config, runtime_paths)
        available_names = self._entity_names(self.config, available)
        assert "calculator" in available_names
        assert "research" in available_names
        assert "writer" in available_names  # Present but not configured

    def test_responder_candidates_should_use_configured_entities_only(self) -> None:
        """Test that responders should only consider configured entities."""
        runtime_paths = runtime_paths_for(self.config)
        room = MagicMock()
        room.room_id = "#general:localhost"
        room.users = {
            "@mindroom_calculator:localhost": None,  # Configured
            "@mindroom_research:localhost": None,  # Configured
            "@mindroom_writer:localhost": None,  # NOT configured but present
        }

        # For responder decisions, use configured entities only.
        configured = configured_routable_entity_ids_for_room(self.config, room.room_id, runtime_paths)
        configured_names = self._entity_names(self.config, configured)
        assert configured_names == ["calculator", "research"]
        assert "writer" not in configured_names

        available = get_available_responders_in_room(room, self.config, runtime_paths)
        assert len(available) == 3  # All agents in room

    @pytest.mark.asyncio
    async def test_responder_candidates_keep_configured_rooms_static(self) -> None:
        """Responder candidates should not widen statically configured rooms."""
        runtime_paths = runtime_paths_for(self.config)
        room = MagicMock()
        room.room_id = "#general:localhost"
        room.members_synced = True
        room.users = {
            "@mindroom_calculator:localhost": None,  # Configured
            "@mindroom_research:localhost": None,  # Configured
            "@mindroom_writer:localhost": None,  # Present but not configured
            "@user:localhost": None,
        }
        client = AsyncMock()
        client.joined_members = AsyncMock()

        available = await responder_candidate_entities_for_room(
            client,
            room,
            "@user:localhost",
            self.config,
            runtime_paths,
        )

        available_names = self._entity_names(self.config, available)
        assert available_names == ["calculator", "research"]
        assert "writer" not in available_names
        client.joined_members.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_responder_candidates_use_persisted_configured_entity_ids(self) -> None:
        """Configured-room responders should follow persisted Matrix username drift."""
        runtime_paths = runtime_paths_for(self.config)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_calculator", "mindroom_calculator_oldns", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)
        room = MagicMock()
        room.room_id = "#math:localhost"
        room.members_synced = True
        room.users = {
            "@mindroom_calculator_oldns:localhost": None,
            "@actual_writer:localhost": None,
            "@user:localhost": None,
        }
        client = AsyncMock()
        client.joined_members = AsyncMock()

        available = await responder_candidate_entities_for_room(
            client,
            room,
            "@user:localhost",
            self.config,
            runtime_paths,
        )

        assert [mid.full_id for mid in available] == ["@mindroom_calculator_oldns:localhost"]
        client.joined_members.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_configured_room_responders_use_actual_ids_without_generated_prefix(self) -> None:
        """Configured rooms should materialize configured aliases to exact persisted Matrix IDs."""
        runtime_paths = runtime_paths_for(self.config)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_calculator", "actual_calculator", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)
        room = MagicMock()
        room.room_id = "#math:localhost"
        room.members_synced = True
        room.users = {
            "@mindroom_calculator:localhost": None,
            "@actual_calculator:localhost": None,
            "@user:localhost": None,
        }
        client = AsyncMock()
        client.joined_members = AsyncMock()

        available = await responder_candidate_entities_for_room(
            client,
            room,
            "@user:localhost",
            self.config,
            runtime_paths,
        )

        assert [mid.full_id for mid in available] == ["@actual_calculator:localhost"]
        assert self._entity_names(self.config, available) == ["calculator"]
        client.joined_members.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_responder_candidates_keep_team_configured_rooms_static(self) -> None:
        """Responder candidates should not widen rooms configured through teams."""
        config = self._bind_runtime(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="Calculator"),
                    "writer": AgentConfig(display_name="Writer", rooms=["#writing:localhost"]),
                },
                teams={
                    "ops": TeamConfig(
                        display_name="Ops Team",
                        role="Operations team",
                        agents=["calculator"],
                        rooms=["#ops:localhost"],
                    ),
                },
                room_models={},
                models={"default": ModelConfig(provider="test", id="test-model")},
            ),
        )
        runtime_paths = runtime_paths_for(config)
        room = MagicMock()
        room.room_id = "#ops:localhost"
        room.members_synced = True
        room.users = {
            "@mindroom_ops:localhost": None,  # Configured team
            "@mindroom_writer:localhost": None,  # Present but not configured
            "@user:localhost": None,
        }
        client = AsyncMock()
        client.joined_members = AsyncMock()

        available = await responder_candidate_entities_for_room(
            client,
            room,
            "@user:localhost",
            config,
            runtime_paths,
        )

        available_names = self._entity_names(config, available)
        assert available_names == ["ops"]
        assert "writer" not in available_names
        client.joined_members.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_responder_candidates_fall_back_to_present_agents_for_ad_hoc_room(self) -> None:
        """Responder candidates should use joined agents when no config maps to the room."""
        runtime_paths = runtime_paths_for(self.config)
        room = MagicMock()
        room.room_id = "!adhoc:localhost"
        room.members_synced = True
        room.users = {
            "@mindroom_calculator:localhost": None,
            "@mindroom_writer:localhost": None,
            "@mindroom_router:localhost": None,
            "@user:localhost": None,
        }
        client = AsyncMock()
        client.joined_members = AsyncMock()

        available = await responder_candidate_entities_for_room(
            client,
            room,
            "@user:localhost",
            self.config,
            runtime_paths,
        )

        available_names = self._entity_names(self.config, available)
        assert available_names == ["calculator", "writer"]
        assert "router" not in available_names
        client.joined_members.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ad_hoc_room_responders_start_from_present_actual_ids(self) -> None:
        """Ad-hoc rooms should map present actual managed IDs back to configured aliases."""
        runtime_paths = runtime_paths_for(self.config)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account("agent_writer", "actual_writer", "pw", domain="localhost")
        state.save(runtime_paths=runtime_paths)
        room = MagicMock()
        room.room_id = "!adhoc:localhost"
        room.members_synced = True
        room.users = {
            "@actual_writer:localhost": None,
            "@mindroom_writer:localhost": None,
            "@mindroom_router:localhost": None,
            "@user:localhost": None,
        }
        client = AsyncMock()
        client.joined_members = AsyncMock()

        available = await responder_candidate_entities_for_room(
            client,
            room,
            "@user:localhost",
            self.config,
            runtime_paths,
        )

        assert [mid.full_id for mid in available] == ["@actual_writer:localhost"]
        assert self._entity_names(self.config, available) == ["writer"]
        client.joined_members.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_responder_candidates_ad_hoc_room_respects_sender_permissions(self) -> None:
        """Ad-hoc room fallback should still apply per-agent sender allowlists."""
        runtime_paths = runtime_paths_for(self.config)
        self.config.authorization.agent_reply_permissions = {
            "calculator": ["@user:localhost"],
            "writer": ["@other:localhost"],
        }
        room = MagicMock()
        room.room_id = "!adhoc:localhost"
        room.members_synced = True
        room.users = {
            "@mindroom_calculator:localhost": None,
            "@mindroom_writer:localhost": None,
            "@user:localhost": None,
        }
        client = AsyncMock()
        client.joined_members = AsyncMock()

        available = await responder_candidate_entities_for_room(
            client,
            room,
            "@user:localhost",
            self.config,
            runtime_paths,
        )

        available_names = self._entity_names(self.config, available)
        assert available_names == ["calculator"]

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

        # Router should be excluded from configured entities
        runtime_paths = runtime_paths_for(config_with_router)
        configured = configured_routable_entity_ids_for_room(config_with_router, room.room_id, runtime_paths)
        configured_names = self._entity_names(config_with_router, configured)
        assert configured_names == ["calculator"]
        assert "router" not in configured_names

        # Router should be excluded from available agents in room
        available = get_available_responders_in_room(room, config_with_router, runtime_paths)
        available_names = self._entity_names(config_with_router, available)
        assert available_names == ["calculator"]
        assert "router" not in available_names
