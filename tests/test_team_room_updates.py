"""Tests for team room update functionality."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.entity_rooms import get_rooms_for_entity
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import orchestrator_runtime_paths, published_config_load


def _mock_running_bot(entity_name: str, agent_user: object, config: Config) -> MagicMock:
    """Return one distinct running bot double with its configured rooms."""
    bot = MagicMock()
    bot.pending_response_owner_count = 0
    bot.pending_response_phase_counts = {}
    bot.deferred_stop_phase = None
    bot.deferred_stop_required = False
    bot.agent_name = entity_name
    bot.agent_user = agent_user
    bot.config = config
    bot.rooms = get_rooms_for_entity(entity_name, config)
    bot.running = True
    bot.start = AsyncMock()
    bot.stop = AsyncMock()
    bot.sync_forever = AsyncMock()
    bot.prepare_for_sync_shutdown = AsyncMock()
    bot.quiesce_matrix_ingestion = AsyncMock()
    bot.refresh_presence = AsyncMock()
    bot.mark_sync_loop_started = MagicMock()
    bot.reset_watchdog_clock = MagicMock()
    return bot


class TestTeamRoomUpdates:
    """Test team room configuration updates."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
    async def test_team_room_change_reconciles_live_bot(self, tmp_path: Path) -> None:
        """Changing a team's rooms should preserve bots and reconcile memberships live."""
        # Create initial config
        initial_config_data: dict[str, Any] = {
            "agents": {
                "agent1": {
                    "display_name": "Agent1",
                    "role": "Test",
                    "tools": [],
                    "instructions": [],
                    "rooms": ["room1"],
                    "model": "default",
                },
            },
            "teams": {
                "team1": {
                    "display_name": "Team1",
                    "role": "Test team",
                    "agents": ["agent1"],
                    "rooms": ["room1", "room2"],
                    "model": "default",
                    "mode": "coordinate",
                },
            },
            "defaults": {"markdown": True},
            "models": {"default": {"provider": "ollama", "id": "test-model", "host": None, "api_key": None}},
            "router": {"model": "default"},
        }

        config1 = Config.model_validate(initial_config_data)

        def prepared_accounts(
            _config: Config,
            _paths: object,
            entity_names: list[str],
            **_kwargs: object,
        ) -> dict[str, MagicMock]:
            return {name: MagicMock(user_id=f"@{name}:localhost", agent_name=name) for name in entity_names}

        with (
            patch("mindroom.orchestrator.prepare_managed_accounts", new=AsyncMock(side_effect=prepared_accounts)),
            patch("mindroom.orchestration.rooms.RoomSetup.setup", new=AsyncMock()) as setup_rooms,
        ):
            orchestrator = _MultiAgentOrchestrator(
                runtime_paths=orchestrator_runtime_paths(tmp_path),
                config=config1,
            )

            with patch("mindroom.orchestrator.create_bot_for_entity") as mock_create_bot:
                created_bots: dict[str, MagicMock] = {}

                def create_mock_bot(
                    entity_name: str,
                    agent_user: object,
                    owner: _MultiAgentOrchestrator,
                ) -> MagicMock:
                    bot = _mock_running_bot(entity_name, agent_user, owner.config)
                    created_bots[entity_name] = bot
                    return bot

                mock_create_bot.side_effect = create_mock_bot

                try:
                    await orchestrator.initialize()
                    orchestrator.running = True
                    original_bots = dict(orchestrator.agent_bots)

                    async def reconcile_rooms(bots: list[MagicMock]) -> None:
                        active_config = orchestrator.config
                        for bot in bots:
                            bot.rooms = get_rooms_for_entity(bot.agent_name, active_config)

                    setup_rooms.side_effect = reconcile_rooms
                    setup_rooms.reset_mock()
                    for bot in created_bots.values():
                        bot.stop.reset_mock()

                    # Update config with different rooms for the team
                    updated_config_data = initial_config_data.copy()
                    updated_config_data["teams"]["team1"]["rooms"] = ["room2", "room3", "room4"]
                    config2 = Config.model_validate(updated_config_data)
                    orchestrator.config_reload.load_config = lambda: published_config_load(config2)

                    # Update config
                    updated = await orchestrator.config_reload._update_config()

                    # Verify the existing team and router bots were reconciled in place.
                    assert updated is False
                    assert orchestrator.agent_bots == original_bots
                    assert orchestrator.agent_bots["agent1"].rooms == ["room1"]
                    assert orchestrator.agent_bots["team1"].rooms == ["room2", "room3", "room4"]
                    assert set(orchestrator.agent_bots["router"].rooms) == {
                        "room1",
                        "room2",
                        "room3",
                        "room4",
                    }
                    for bot in created_bots.values():
                        bot.stop.assert_not_awaited()
                    assert mock_create_bot.call_count == 3
                    setup_rooms.assert_awaited_once()
                    reconciled_bots = setup_rooms.await_args.args[0]
                    assert {bot.agent_name for bot in reconciled_bots} == {"router", "team1"}
                finally:
                    await orchestrator.stop()
