"""Test that sync tasks are properly cancelled when agents are restarted."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import MultiAgentOrchestrator, _cancel_sync_task, _stop_entities
from mindroom.config import Config


@pytest.mark.asyncio
async def test_cancel_sync_task() -> None:
    """Test the _cancel_sync_task helper function."""

    # Create a real cancelled task for testing
    async def dummy_coro() -> None:
        await asyncio.sleep(1)

    task = asyncio.create_task(dummy_coro())
    sync_tasks = {"agent1": task}

    # Cancel the task
    await _cancel_sync_task("agent1", sync_tasks)

    # Verify task was cancelled and removed
    assert task.cancelled()
    assert "agent1" not in sync_tasks


@pytest.mark.asyncio
async def test_cancel_sync_task_missing_entity() -> None:
    """Test _cancel_sync_task with non-existent entity."""
    sync_tasks = {}

    # Should not raise error for missing entity
    await _cancel_sync_task("non_existent", sync_tasks)

    assert len(sync_tasks) == 0


@pytest.mark.asyncio
async def test_stop_entities_cancels_sync_tasks() -> None:
    """Test that _stop_entities properly cancels sync tasks."""
    # Use patch to mock _cancel_sync_task since we tested it separately
    with patch("mindroom.bot._cancel_sync_task") as mock_cancel:
        mock_cancel.side_effect = lambda name, tasks: tasks.pop(name, None)

        # Create mock bots
        mock_bot1 = AsyncMock()
        mock_bot1.stop = AsyncMock()
        mock_bot2 = AsyncMock()
        mock_bot2.stop = AsyncMock()

        agent_bots = {
            "agent1": mock_bot1,
            "agent2": mock_bot2,
            "agent3": AsyncMock(),  # Not being stopped
        }

        sync_tasks = {
            "agent1": MagicMock(),
            "agent2": MagicMock(),
            "agent3": MagicMock(),  # Not being stopped
        }

        # Stop agents 1 and 2
        entities_to_restart = {"agent1", "agent2"}
        await _stop_entities(entities_to_restart, agent_bots, sync_tasks)

        # Verify cancel was called for the right entities
        assert mock_cancel.call_count == 2
        mock_cancel.assert_any_call("agent1", sync_tasks)
        mock_cancel.assert_any_call("agent2", sync_tasks)

        # Verify bots were stopped
        mock_bot1.stop.assert_called_once()
        mock_bot2.stop.assert_called_once()

        # Verify entities were removed from agent_bots
        assert "agent1" not in agent_bots
        assert "agent2" not in agent_bots

        # Verify agent3 was not touched
        assert "agent3" in agent_bots
        assert "agent3" in sync_tasks


@pytest.mark.asyncio
async def test_orchestrator_tracks_sync_tasks() -> None:
    """Test that MultiAgentOrchestrator properly tracks sync tasks."""
    with (
        patch("mindroom.bot.create_bot_for_entity") as mock_create_bot,
        patch("mindroom.bot._sync_forever_with_restart"),
        patch("mindroom.bot.ensure_all_rooms_exist") as mock_ensure_rooms,
        patch("mindroom.bot.ensure_user_in_rooms") as mock_ensure_user,
        patch("mindroom.bot.create_agent_user") as mock_create_user,
    ):
        # Setup mocks
        mock_create_user.return_value = MagicMock()
        mock_ensure_rooms.return_value = {}
        mock_ensure_user.return_value = None

        # Create mock bot
        mock_bot = AsyncMock()
        mock_bot.agent_name = "test_agent"
        mock_bot.start = AsyncMock()
        mock_bot.rooms = []
        mock_create_bot.return_value = mock_bot

        # Create config with one agent
        config = MagicMock(spec=Config)
        config.agents = {"test_agent": MagicMock()}
        config.teams = {}
        config.get_all_configured_rooms.return_value = []

        # Create orchestrator
        orchestrator = MultiAgentOrchestrator(storage_path=MagicMock())
        orchestrator.config = config

        # Initialize bots
        await orchestrator.initialize()

        # Manually simulate what start() does for sync tasks
        # (We can't actually run start() because it would block on gather())
        mock_task = MagicMock(spec=asyncio.Task)
        orchestrator._sync_tasks["test_agent"] = mock_task
        orchestrator._sync_tasks["router"] = MagicMock(spec=asyncio.Task)

        # Verify tasks are tracked
        assert len(orchestrator._sync_tasks) == 2
        assert "test_agent" in orchestrator._sync_tasks
        assert "router" in orchestrator._sync_tasks


@pytest.mark.asyncio
@pytest.mark.requires_matrix  # Requires real Matrix server for sync task management
@pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
async def test_orchestrator_update_config_cancels_old_tasks() -> None:
    """Test that update_config properly cancels old sync tasks."""
    with (
        patch("mindroom.bot.Config.from_yaml") as mock_from_yaml,
        patch("mindroom.bot._identify_entities_to_restart") as mock_identify,
        patch("mindroom.bot._stop_entities") as mock_stop_entities,
        patch("mindroom.bot.create_bot_for_entity") as mock_create_bot,
        patch("mindroom.bot._sync_forever_with_restart"),
        patch("mindroom.bot._create_temp_user") as mock_create_temp_user,
        patch("mindroom.bot.MultiAgentOrchestrator._setup_rooms_and_memberships", new=AsyncMock()),
    ):
        # Create orchestrator with existing agent
        orchestrator = MultiAgentOrchestrator(storage_path=MagicMock())

        # Setup existing config and bot
        old_config = MagicMock(spec=Config)
        old_config.agents = {"agent1": MagicMock()}
        old_config.teams = {}
        old_config.authorization = MagicMock()
        old_config.authorization.global_users = []
        orchestrator.config = old_config

        mock_existing_bot = AsyncMock()
        mock_existing_bot.config = old_config
        orchestrator.agent_bots = {"agent1": mock_existing_bot}

        # Track a sync task for the existing agent
        mock_existing_task = MagicMock(spec=asyncio.Task)
        orchestrator._sync_tasks = {"agent1": mock_existing_task}

        # Setup new config (agent1 needs restart)
        new_config = MagicMock(spec=Config)
        new_config.agents = {"agent1": MagicMock()}
        new_config.teams = {}
        new_config.authorization = MagicMock()
        new_config.authorization.global_users = []  # Add this for the logging
        mock_from_yaml.return_value = new_config

        # Agent1 needs to be restarted
        mock_identify.return_value = {"agent1"}

        # Setup new bot creation
        mock_new_bot = AsyncMock()
        mock_new_bot.start = AsyncMock()
        mock_create_bot.return_value = mock_new_bot
        mock_create_temp_user.return_value = MagicMock()

        # Run update_config
        await orchestrator.update_config()

        # Verify _stop_entities was called with sync_tasks dict
        mock_stop_entities.assert_called_once_with(
            {"agent1"},
            orchestrator.agent_bots,
            orchestrator._sync_tasks,
        )


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_new_agent_not_started_twice() -> None:
    """Regression: a brand-new agent must only be started once.

    Before the fix, _get_changed_agents treated a new agent (old=None,
    new=exists) as "changed", so the agent appeared in both
    entities_to_restart AND new_entities.  update_config processed both
    sets, creating two bot instances with two sync loops for the same
    agent — causing duplicate replies.
    """
    with (
        patch("mindroom.bot.Config.from_yaml") as mock_from_yaml,
        patch("mindroom.bot.create_bot_for_entity") as mock_create_bot,
        patch("mindroom.bot._sync_forever_with_restart"),
        patch("mindroom.bot._stop_entities"),
        patch("mindroom.bot._create_temp_user") as mock_create_temp_user,
        patch.object(MultiAgentOrchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
    ):
        # --- existing orchestrator with one agent running ---
        orchestrator = MultiAgentOrchestrator(storage_path=MagicMock())

        old_config = Config(
            agents={  # type: ignore[arg-type]
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},  # type: ignore[arg-type]
        )
        orchestrator.config = old_config

        mock_existing_bot = AsyncMock()
        mock_existing_bot.config = old_config
        orchestrator.agent_bots = {"general": mock_existing_bot, "router": AsyncMock()}
        orchestrator._sync_tasks = {
            "general": MagicMock(spec=asyncio.Task),
            "router": MagicMock(spec=asyncio.Task),
        }

        # --- new config adds "coach" ---
        new_config = Config(
            agents={  # type: ignore[arg-type]
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
                "coach": {
                    "display_name": "Coach",
                    "role": "Personal coaching",
                    "model": "default",
                    "rooms": ["lobby", "personal"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},  # type: ignore[arg-type]
        )
        mock_from_yaml.return_value = new_config

        # Mock bot creation — record every call
        created_bots: list[AsyncMock] = []

        def make_bot(*args, **kwargs) -> AsyncMock:  # noqa: ANN002, ANN003, ARG001
            bot = AsyncMock()
            bot.try_start = AsyncMock(return_value=True)
            bot.sync_forever = AsyncMock()
            created_bots.append(bot)
            return bot

        mock_create_bot.side_effect = make_bot
        mock_create_temp_user.return_value = MagicMock()

        # --- act ---
        await orchestrator.update_config()

        # --- assert: create_bot_for_entity called exactly once for "coach" ---
        coach_calls = [c for c in mock_create_bot.call_args_list if c[0][0] == "coach"]
        assert len(coach_calls) == 1, (
            f"Expected create_bot_for_entity to be called once for 'coach', but was called {len(coach_calls)} times"
        )

        # Also verify only one sync task is tracked for coach
        assert "coach" in orchestrator._sync_tasks


@pytest.mark.asyncio
async def test_orchestrator_stop_cancels_all_tasks() -> None:
    """Test that stop() cancels all sync tasks."""
    with patch("mindroom.bot._cancel_sync_task") as mock_cancel:
        orchestrator = MultiAgentOrchestrator(storage_path=MagicMock())

        # Track which tasks are cancelled
        cancelled = []

        async def track_cancel(name: str, tasks: dict) -> None:
            cancelled.append(name)
            tasks.pop(name, None)

        mock_cancel.side_effect = track_cancel

        orchestrator._sync_tasks = {
            "agent1": MagicMock(),
            "router": MagicMock(),
        }

        # Create mock bots
        mock_bot1 = AsyncMock()
        mock_bot1.running = True
        mock_bot1.stop = AsyncMock()
        mock_bot2 = AsyncMock()
        mock_bot2.running = True
        mock_bot2.stop = AsyncMock()

        orchestrator.agent_bots = {
            "agent1": mock_bot1,
            "router": mock_bot2,
        }

        # Stop orchestrator
        await orchestrator.stop()

        # Verify all tasks were cancelled
        assert set(cancelled) == {"agent1", "router"}

        # Verify sync_tasks dict is empty
        assert len(orchestrator._sync_tasks) == 0

        # Verify bots were stopped
        mock_bot1.stop.assert_called_once()
        mock_bot2.stop.assert_called_once()
