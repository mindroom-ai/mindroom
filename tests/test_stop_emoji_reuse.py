"""Test that the 🛑 emoji can be reused for other purposes when not stopping generation."""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.matrix.users import AgentMatrixUser
from mindroom.stop import StopManager
from tests.conftest import bind_runtime_paths, orchestrator_runtime_paths, runtime_paths_for


@pytest.mark.asyncio
async def test_stop_emoji_only_stops_during_generation(tmp_path: Path) -> None:
    """Test that 🛑 reaction only acts as stop button during message generation."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create the bot with a config that has empty reply permissions
    config = MagicMock()
    config.authorization.agent_reply_permissions = {}

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={},
        ),
        rooms=["!test:example.com"],
    )

    # Set up the bot with necessary mocks
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    bot.handled_turn_ledger = MagicMock()
    bot.handled_turn_ledger.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.stop_manager = StopManager()
    bot._send_response = AsyncMock(return_value="$stopping:example.com")
    bot._generate_response = AsyncMock(return_value="$response:example.com")

    # Create a room and reaction event
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a 🛑 reaction event
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    # Mock interactive.handle_reaction to simulate it being an interactive question
    with patch("mindroom.bot.interactive.handle_reaction") as mock_handle_reaction:
        mock_handle_reaction.return_value = ("stop_option", None)  # Simulate selecting a stop option

        # Case 1: Message is NOT being generated - should handle as interactive
        await bot._on_reaction(room, reaction_event)

        # Should have called interactive.handle_reaction since message wasn't being tracked
        mock_handle_reaction.assert_called_once()

        # Reset the mock
        mock_handle_reaction.reset_mock()

        # Case 2: Message IS being generated - should handle as stop button
        # Track a message as being generated
        task = MagicMock()  # Use MagicMock instead of AsyncMock for the task
        task.done = MagicMock(return_value=False)  # done() is a regular method, not async
        bot.stop_manager.set_current(
            message_id="$message:example.com",
            room_id="!test:example.com",
            task=task,
        )

        # Process the same reaction again
        await bot._on_reaction(room, reaction_event)

        # Should NOT have called interactive.handle_reaction since it was handled as stop
        mock_handle_reaction.assert_not_called()

        # The task should have been cancelled
        task.cancel.assert_called_once()


@pytest.mark.asyncio
async def test_stop_emoji_prefers_graceful_agno_cancel_when_run_id_present(tmp_path: Path) -> None:
    """Tracked Agno runs should use graceful cancellation before hard task cancellation."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = MagicMock()
    config.authorization.agent_reply_permissions = {}

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={},
        ),
        rooms=["!test:example.com"],
    )

    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    bot.handled_turn_ledger = MagicMock()
    bot.handled_turn_ledger.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.stop_manager = StopManager()
    bot._send_response = AsyncMock(return_value="$stopping:example.com")

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    task = MagicMock()
    task.done = MagicMock(return_value=False)
    bot.stop_manager.set_current(
        message_id="$message:example.com",
        room_id="!test:example.com",
        task=task,
        run_id="run-123",
    )

    with patch.object(bot.stop_manager, "_schedule_graceful_run_cancel") as mock_schedule_cancel:
        await bot._on_reaction(room, reaction_event)

    mock_schedule_cancel.assert_called_once_with("$message:example.com")
    task.cancel.assert_not_called()
    bot._send_response.assert_awaited_once_with(
        "!test:example.com",
        "$message:example.com",
        "⏹️ Stopping generation...",
        None,
    )


@pytest.mark.asyncio
async def test_stop_manager_force_cancels_task_when_run_never_becomes_cancellable() -> None:
    """A stop request must hard-cancel quickly when the Agno run is not live yet."""
    stop_manager = StopManager(graceful_cancel_fallback_seconds=0.01)
    completed = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def response_that_would_complete() -> None:
        try:
            await asyncio.sleep(0.1)
            completed.set()
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    task = asyncio.create_task(response_that_would_complete())

    stop_manager.set_current(
        message_id="$message:example.com",
        room_id="!test:example.com",
        task=task,
        run_id="run-123",
    )

    with patch("mindroom.stop.acancel_run", new=AsyncMock(return_value=False)):
        assert await stop_manager.handle_stop_reaction("$message:example.com") is True
        await asyncio.wait_for(task_cancelled.wait(), timeout=0.2)

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not completed.is_set()


@pytest.mark.asyncio
async def test_stop_manager_force_cancels_task_when_graceful_cancel_errors() -> None:
    """Cancellation-manager failures must not disable the hard-cancel fallback."""
    stop_manager = StopManager(graceful_cancel_fallback_seconds=0.01)
    started = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def hung_response() -> None:
        started.set()
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    task = asyncio.create_task(hung_response())
    await started.wait()

    stop_manager.set_current(
        message_id="$message:example.com",
        room_id="!test:example.com",
        task=task,
        run_id="run-123",
    )

    with patch("mindroom.stop.acancel_run", new=AsyncMock(side_effect=RuntimeError("redis down"))):
        assert await stop_manager.handle_stop_reaction("$message:example.com") is True
        await asyncio.wait_for(task_cancelled.wait(), timeout=0.2)

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stop_manager_force_cancels_task_when_graceful_cancel_hangs() -> None:
    """Graceful cancellation should still force-cancel the task when Agno never stops it."""
    stop_manager = StopManager(graceful_cancel_fallback_seconds=0.01)
    started = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def hung_response() -> None:
        started.set()
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    task = asyncio.create_task(hung_response())
    await started.wait()

    stop_manager.set_current(
        message_id="$message:example.com",
        room_id="!test:example.com",
        task=task,
        run_id="run-123",
    )

    with patch("mindroom.stop.acancel_run", new=AsyncMock(return_value=True)):
        assert await stop_manager.handle_stop_reaction("$message:example.com") is True
        await asyncio.wait_for(task_cancelled.wait(), timeout=0.2)

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stop_manager_force_cancels_task_when_cancellation_manager_hangs() -> None:
    """A hung cancellation-manager call must not block the hard-cancel fallback."""
    stop_manager = StopManager(graceful_cancel_fallback_seconds=0.01)
    started = asyncio.Event()
    task_cancelled = asyncio.Event()

    async def hung_response() -> None:
        started.set()
        try:
            await asyncio.sleep(999)
        except asyncio.CancelledError:
            task_cancelled.set()
            raise

    async def hanging_cancel_run(_run_id: str) -> bool:
        await asyncio.sleep(999)
        return True

    task = asyncio.create_task(hung_response())
    await started.wait()

    stop_manager.set_current(
        message_id="$message:example.com",
        room_id="!test:example.com",
        task=task,
        run_id="run-123",
    )

    with patch("mindroom.stop.acancel_run", new=hanging_cancel_run):
        assert await stop_manager.handle_stop_reaction("$message:example.com") is True
        await asyncio.wait_for(task_cancelled.wait(), timeout=0.2)

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_stop_manager_retries_until_run_becomes_cancellable() -> None:
    """The stop probe should retry briefly when the Agno run is not live on the first probe."""
    stop_manager = StopManager(graceful_cancel_fallback_seconds=0.2)
    allow_task_to_finish = asyncio.Event()
    task_completed = asyncio.Event()
    cancel_attempts: list[str] = []

    async def graceful_response() -> None:
        await allow_task_to_finish.wait()
        task_completed.set()

    async def fake_acancel_run(run_id: str) -> bool:
        cancel_attempts.append(run_id)
        if len(cancel_attempts) == 1:
            return False
        allow_task_to_finish.set()
        return True

    task = asyncio.create_task(graceful_response())

    stop_manager.set_current(
        message_id="$message:example.com",
        room_id="!test:example.com",
        task=task,
        run_id="run-123",
    )

    with patch("mindroom.stop.acancel_run", new=fake_acancel_run):
        assert await stop_manager.handle_stop_reaction("$message:example.com") is True
        await asyncio.wait_for(task_completed.wait(), timeout=0.2)

    await asyncio.wait_for(task, timeout=0.2)
    assert cancel_attempts == ["run-123", "run-123"]
    assert not task.cancelled()


@pytest.mark.asyncio
async def test_stop_manager_reprobes_when_retry_updates_run_id() -> None:
    """A stop request should follow a fresh retry run_id instead of waiting for hard cancellation."""
    stop_manager = StopManager(graceful_cancel_fallback_seconds=0.2)
    keep_running = asyncio.Event()
    first_cancel_attempt = asyncio.Event()
    second_cancel_attempt = asyncio.Event()
    cancel_attempts: list[str] = []

    async def graceful_response() -> None:
        await keep_running.wait()

    async def fake_acancel_run(run_id: str) -> bool:
        cancel_attempts.append(run_id)
        if run_id == "run-123":
            first_cancel_attempt.set()
        if run_id == "run-456":
            second_cancel_attempt.set()
            keep_running.set()
        return True

    task = asyncio.create_task(graceful_response())

    stop_manager.set_current(
        message_id="$message:example.com",
        room_id="!test:example.com",
        task=task,
        run_id="run-123",
    )

    with patch("mindroom.stop.acancel_run", new=fake_acancel_run):
        assert await stop_manager.handle_stop_reaction("$message:example.com") is True
        await asyncio.wait_for(first_cancel_attempt.wait(), timeout=0.2)
        stop_manager.update_run_id("$message:example.com", "run-456")
        await asyncio.wait_for(second_cancel_attempt.wait(), timeout=0.2)

    await asyncio.wait_for(task, timeout=0.2)
    if stop_manager.cleanup_tasks:
        await asyncio.gather(*stop_manager.cleanup_tasks, return_exceptions=True)

    assert cancel_attempts == ["run-123", "run-456"]


@pytest.mark.asyncio
async def test_stop_emoji_from_agent_falls_through(tmp_path: Path) -> None:
    """Test that 🛑 reactions from agents fall through to other handlers."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create the bot with a config that has empty reply permissions
    config = MagicMock()
    config.authorization.agent_reply_permissions = {}

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path,
            process_env={},
        ),
        rooms=["!test:example.com"],
    )

    # Set up the bot
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"
    bot.handled_turn_ledger = MagicMock()
    bot.logger = MagicMock()
    bot.stop_manager = StopManager()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a 🛑 reaction from ANOTHER AGENT
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@mindroom_helper:example.com",  # Another agent
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    # Mock extract_agent_name to return that this is an agent
    with (
        patch("mindroom.bot.extract_agent_name", return_value="helper"),
        patch("mindroom.bot.interactive.handle_reaction") as mock_handle_reaction,
        patch("mindroom.bot.config_confirmation.get_pending_change", return_value=None),
    ):
        mock_handle_reaction.return_value = None  # No interactive result

        # Track a message as being generated
        task = MagicMock()  # Use MagicMock instead of AsyncMock for the task
        task.done = MagicMock(return_value=False)  # done() is a regular method, not async
        bot.stop_manager.set_current(
            message_id="$message:example.com",
            room_id="!test:example.com",
            task=task,
        )

        # Process the reaction from an agent
        await bot._on_reaction(room, reaction_event)

        # Should have called interactive.handle_reaction (fell through)
        mock_handle_reaction.assert_called_once()

        # Task should NOT have been cancelled (agents can't stop generation)
        task.cancel.assert_not_called()


@pytest.mark.asyncio
async def test_stop_reaction_blocked_by_reply_permissions(tmp_path: Path) -> None:
    """Disallowed senders must not trigger stop or send confirmation via 🛑 reaction."""
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    config = bind_runtime_paths(
        Config(
            agents={
                "test_agent": {
                    "display_name": "Test Agent",
                    "rooms": ["!test:example.com"],
                },
            },
            authorization={
                "default_room_access": True,
                "agent_reply_permissions": {"test_agent": ["@alice:example.com"]},
            },
        ),
        orchestrator_runtime_paths(tmp_path, config_path=tmp_path / "config.yaml"),
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:example.com"],
    )
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@mindroom_test_agent:example.com"
    bot.handled_turn_ledger = MagicMock()
    bot.logger = MagicMock()
    bot.stop_manager = StopManager()

    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@mindroom_test_agent:example.com")

    # Track a message as being generated
    task = MagicMock()
    task.done = MagicMock(return_value=False)
    bot.stop_manager.set_current(
        message_id="$message:example.com",
        room_id="!test:example.com",
        task=task,
    )

    # Disallowed sender reacts with stop emoji
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$message:example.com",
                    "key": "🛑",
                },
            },
            "event_id": "$reaction_bob:example.com",
            "sender": "@bob:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )

    bot._send_response = AsyncMock()

    with patch("mindroom.bot.is_authorized_sender", return_value=True):
        await bot._on_reaction(room, reaction_event)

    # Task should NOT have been cancelled — sender is disallowed
    task.cancel.assert_not_called()
    # No confirmation message should have been sent
    bot._send_response.assert_not_called()
