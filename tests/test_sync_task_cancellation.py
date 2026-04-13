"""Test that sync tasks are properly cancelled when agents are restarted."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.orchestration import runtime as runtime_helpers
from mindroom.orchestration.runtime import (
    SYNC_RESTART_CANCEL_MSG,
    _SyncIteration,
    cancel_sync_task,
    is_sync_restart_cancel,
    matrix_sync_startup_timeout_seconds,
    stop_entities,
    sync_forever_with_restart,
)
from mindroom.orchestrator import MultiAgentOrchestrator
from tests.conftest import orchestrator_runtime_paths


def _fake_runtime_paths(**env_overrides: str) -> RuntimePaths:
    """Build a minimal ``RuntimePaths`` for watchdog tests."""
    fake = Path("/var/empty/mindroom-test")
    return RuntimePaths(
        config_path=fake / "config.yaml",
        config_dir=fake,
        env_path=fake / ".env",
        storage_root=fake / "data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", **env_overrides},
    )


class _FakeBot:
    """Minimal bot stub for watchdog tests."""

    def __init__(self, **env_overrides: str) -> None:
        self.agent_name = "test_agent"
        self.running = True
        self.last_sync_time = None
        self._last_sync_monotonic: float | None = None
        self._first_sync_done = False
        self._sync_shutting_down = False
        self.sync_calls = 0
        self.first_call_cancelled = False
        self.first_call_cancel_args: tuple[object, ...] = ()
        self.prepare_for_sync_shutdown_calls = 0
        self.runtime_paths = _fake_runtime_paths(**env_overrides)

    def mark_sync_loop_started(self) -> None:
        self._sync_shutting_down = False

    def reset_watchdog_clock(self) -> None:
        self._last_sync_monotonic = None

    def seconds_since_last_sync_activity(self) -> float | None:
        if self._last_sync_monotonic is None:
            return None
        return time.monotonic() - self._last_sync_monotonic

    async def sync_forever(self) -> None:
        self.sync_calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            if self.sync_calls == 1:
                self.first_call_cancelled = True
                self.first_call_cancel_args = exc.args
            raise

    async def prepare_for_sync_shutdown(self) -> None:
        self._sync_shutting_down = True
        self.prepare_for_sync_shutdown_calls += 1


@pytest.mark.asyncio
async def test_cancel_sync_task() -> None:
    """Test the cancel_sync_task helper function."""

    # Create a real cancelled task for testing
    async def dummy_coro() -> None:
        await asyncio.sleep(1)

    task = asyncio.create_task(dummy_coro())
    sync_tasks = {"agent1": task}

    # Cancel the task
    await cancel_sync_task("agent1", sync_tasks)

    # Verify task was cancelled and removed
    assert task.cancelled()
    assert "agent1" not in sync_tasks


@pytest.mark.asyncio
async def test_cancel_sync_task_missing_entity() -> None:
    """Test cancel_sync_task with non-existent entity."""
    sync_tasks = {}

    # Should not raise error for missing entity
    await cancel_sync_task("non_existent", sync_tasks)

    assert len(sync_tasks) == 0


@pytest.mark.asyncio
async def test_sync_forever_with_restart_restarts_stalled_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Watchdog should cancel and restart a sync loop that stops making progress."""
    bot = _FakeBot()
    bot.agent_name = "stalled_agent"

    # Arm the monotonic clock so the steady-state watchdog fires.
    original_mark = bot.mark_sync_loop_started

    def arm_and_mark() -> None:
        original_mark()
        bot._last_sync_monotonic = time.monotonic()

    bot.mark_sync_loop_started = arm_and_mark

    # On 2nd call, stop the bot so the loop exits cleanly.
    original_sync = bot.sync_forever

    async def sync_then_stop() -> None:
        if bot.sync_calls > 0:
            # 2nd call — stop immediately
            bot.running = False
            return
        await original_sync()

    bot.sync_forever = sync_then_stop

    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)

    await sync_forever_with_restart(bot, max_retries=2)

    assert bot.first_call_cancelled is True
    assert bot.first_call_cancel_args == (SYNC_RESTART_CANCEL_MSG,)
    assert bot.sync_calls == 1  # sync_forever called once, then sync_then_stop stopped
    assert bot.prepare_for_sync_shutdown_calls == 2


@pytest.mark.asyncio
async def test_is_sync_restart_cancel_checks_cancel_message() -> None:
    """The restart helper should only match the dedicated cancel message."""
    assert is_sync_restart_cancel(asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG)) is True
    assert is_sync_restart_cancel(asyncio.CancelledError()) is False


@pytest.mark.asyncio
async def test_sync_forever_with_restart_cancels_deferred_work_before_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart backoff should only happen after deferred overdue drain cleanup."""
    bot = _FakeBot()
    call_order: list[str] = []
    call_count = 0

    async def fail_once_then_stop() -> None:
        nonlocal call_count
        bot.sync_calls += 1
        call_count += 1
        if call_count == 1:
            msg = "sync failed once"
            raise RuntimeError(msg)
        bot.running = False

    async def prepare_for_sync_shutdown() -> None:
        bot.prepare_for_sync_shutdown_calls += 1
        call_order.append("prepare")

    bot.sync_forever = fail_once_then_stop
    bot.prepare_for_sync_shutdown = prepare_for_sync_shutdown

    def fake_retry_delay(*_args: object, **_kwargs: object) -> float:
        call_order.append("retry_delay")
        return 0.0

    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", fake_retry_delay)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 5.0)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)

    await sync_forever_with_restart(bot, max_retries=2)

    assert call_order[:2] == ["prepare", "retry_delay"]


@pytest.mark.asyncio
async def test_slow_first_sync_not_killed_by_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first sync that takes >120s but <600s must NOT be cancelled."""
    bot = _FakeBot()

    # Simulate a slow first sync: after a delay, arm the watchdog clock
    # (as would happen when _on_sync_response fires).
    sync_started = asyncio.Event()

    async def slow_first_sync() -> None:
        bot.sync_calls += 1
        sync_started.set()
        # Simulate a long first sync that eventually succeeds.
        await asyncio.sleep(0.08)
        # First SyncResponse arrives — arm watchdog.
        bot._last_sync_monotonic = time.monotonic()
        # Then finish normally.
        bot.running = False

    bot.sync_forever = slow_first_sync

    # Steady-state timeout is 0.03s, but startup timeout is 0.5s.
    # The 0.08s first sync should survive.
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)

    await sync_forever_with_restart(bot, max_retries=-1)

    assert bot.first_call_cancelled is False
    assert bot.sync_calls == 1


@pytest.mark.asyncio
async def test_startup_timeout_kills_stuck_first_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first sync that never completes should be killed by the startup timeout."""
    bot = _FakeBot()

    async def stuck_first_sync() -> None:
        bot.sync_calls += 1
        try:
            await asyncio.Event().wait()  # Never completes
        except asyncio.CancelledError:
            bot.first_call_cancelled = True
            raise

    bot.sync_forever = stuck_first_sync

    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 0.03)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)

    await sync_forever_with_restart(bot, max_retries=1)

    assert bot.first_call_cancelled is True


@pytest.mark.asyncio
async def test_sync_error_updates_watchdog_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """SyncError responses should keep the watchdog alive (loop is retrying, not stalled)."""
    bot = _FakeBot()
    error_callback_fired = False

    async def sync_with_errors() -> None:
        bot.sync_calls += 1
        # Simulate _on_sync_error callback updating monotonic clock.
        bot._last_sync_monotonic = time.monotonic()
        # Keep refreshing to simulate ongoing error responses.
        for _ in range(10):
            await asyncio.sleep(0.01)
            bot._last_sync_monotonic = time.monotonic()
        nonlocal error_callback_fired
        error_callback_fired = True
        bot.running = False

    bot.sync_forever = sync_with_errors

    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)

    await sync_forever_with_restart(bot, max_retries=-1)

    assert error_callback_fired
    assert bot.first_call_cancelled is False


@pytest.mark.asyncio
async def test_sync_iteration_wait_prioritizes_sync_failure() -> None:
    """The sync task failure should win if both child tasks finish together."""
    bot = _FakeBot()

    async def raise_sync_error() -> None:
        msg = "sync failed"
        raise RuntimeError(msg)

    async def watchdog_returns() -> None:
        return

    iteration = _SyncIteration(
        bot=bot,
        sync_task=asyncio.create_task(raise_sync_error()),
        watchdog_task=asyncio.create_task(watchdog_returns()),
    )
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="sync failed"):
        await iteration.wait()

    await iteration.cancel()


@pytest.mark.asyncio
async def test_sync_iteration_cancel_logs_non_cancelled_errors() -> None:
    """Non-CancelledError exceptions should be logged, not silently swallowed."""
    bot = _FakeBot()

    async def raise_runtime_error() -> None:
        msg = "unexpected error"
        raise RuntimeError(msg)

    task = asyncio.create_task(raise_runtime_error())
    await asyncio.sleep(0)  # Let the task run

    # Should not raise — the error is logged and suppressed.
    await _SyncIteration(bot=bot, sync_task=task, watchdog_task=None).cancel()


@pytest.mark.asyncio
async def test_full_state_stays_enabled_until_first_sync_response() -> None:
    """A cancelled first sync must keep requesting full state on retry."""
    full_state_values: list[bool] = []

    class FakeClient:
        async def sync_forever(self, *, timeout: int, full_state: bool) -> None:  # noqa: ASYNC109, ARG002
            full_state_values.append(full_state)
            await asyncio.Event().wait()

    bot = MagicMock(spec=AgentBot)
    bot._first_sync_done = False
    bot._sync_shutting_down = False
    bot.client = FakeClient()

    first_task = asyncio.create_task(AgentBot.sync_forever(bot))
    await asyncio.sleep(0)
    first_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_task

    second_task = asyncio.create_task(AgentBot.sync_forever(bot))
    await asyncio.sleep(0)
    second_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second_task

    assert full_state_values == [True, True]


@pytest.mark.asyncio
async def test_full_state_only_after_successful_first_sync() -> None:
    """sync_forever should stop requesting full state after a successful first sync."""
    full_state_values: list[bool] = []

    class FakeClient:
        next_batch = "token123"

        async def sync_forever(self, *, timeout: int, full_state: bool) -> None:  # noqa: ASYNC109, ARG002
            full_state_values.append(full_state)

        def add_response_callback(self, *args: object) -> None:
            pass

        def add_event_callback(self, *args: object) -> None:
            pass

    bot = MagicMock(spec=AgentBot)
    bot.agent_name = "test_agent"
    bot.last_sync_time = None
    bot._first_sync_done = False
    bot._sync_shutting_down = False
    bot.client = FakeClient()
    bot._runtime_view = BotRuntimeState(
        client=bot.client,
        config=MagicMock(spec=Config),
        enable_streaming=True,
        orchestrator=None,
        event_cache=None,
        event_cache_write_coordinator=None,
    )

    # Call the real sync_forever method
    await AgentBot.sync_forever(bot)
    await AgentBot._on_sync_response(bot, MagicMock())
    await AgentBot.sync_forever(bot)

    assert full_state_values == [True, False]


@pytest.mark.asyncio
async def test_stop_entities_cancels_sync_tasks() -> None:
    """Test that stop_entities properly cancels sync tasks."""

    async def sync_loop() -> None:
        await asyncio.sleep(60)

    task1 = asyncio.create_task(sync_loop())
    task2 = asyncio.create_task(sync_loop())
    task3 = asyncio.create_task(sync_loop())

    mock_bot1 = AsyncMock()
    mock_bot1.prepare_for_sync_shutdown = AsyncMock()
    mock_bot1.stop = AsyncMock()
    mock_bot2 = AsyncMock()
    mock_bot2.prepare_for_sync_shutdown = AsyncMock()
    mock_bot2.stop = AsyncMock()

    agent_bots = {
        "agent1": mock_bot1,
        "agent2": mock_bot2,
        "agent3": AsyncMock(),
    }
    sync_tasks = {
        "agent1": task1,
        "agent2": task2,
        "agent3": task3,
    }

    entities_to_restart = {"agent1", "agent2"}
    await stop_entities(entities_to_restart, agent_bots, sync_tasks)

    assert task1.cancelled()
    assert task2.cancelled()
    assert not task3.cancelled()

    mock_bot1.prepare_for_sync_shutdown.assert_awaited_once()
    mock_bot2.prepare_for_sync_shutdown.assert_awaited_once()
    mock_bot1.stop.assert_called_once()
    mock_bot2.stop.assert_called_once()

    assert "agent1" not in agent_bots
    assert "agent2" not in agent_bots
    assert "agent3" in agent_bots

    assert "agent1" not in sync_tasks
    assert "agent2" not in sync_tasks
    assert "agent3" in sync_tasks

    task3.cancel()
    await asyncio.gather(task3, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_entities_prepares_bots_before_cancelling_sync_tasks() -> None:
    """Restart teardown should cancel deferred work before the sync loop stops."""
    call_order: list[tuple[str, str]] = []
    cancel_messages: list[tuple[str, str | None]] = []

    mock_bot1 = AsyncMock()
    mock_bot1.prepare_for_sync_shutdown = AsyncMock(
        side_effect=lambda: call_order.append(("prepare", "agent1")),
    )
    mock_bot1.stop = AsyncMock(side_effect=lambda **_: call_order.append(("stop", "agent1")))

    mock_bot2 = AsyncMock()
    mock_bot2.prepare_for_sync_shutdown = AsyncMock(
        side_effect=lambda: call_order.append(("prepare", "agent2")),
    )
    mock_bot2.stop = AsyncMock(side_effect=lambda **_: call_order.append(("stop", "agent2")))

    agent_bots = {
        "agent1": mock_bot1,
        "agent2": mock_bot2,
    }
    sync_tasks = {
        "agent1": asyncio.create_task(asyncio.sleep(60)),
        "agent2": asyncio.create_task(asyncio.sleep(60)),
    }

    async def fake_cancel_sync_task(
        entity_name: str,
        _sync_tasks: dict[str, asyncio.Task],
        *,
        cancel_msg: str | None = None,
    ) -> None:
        call_order.append(("cancel", entity_name))
        cancel_messages.append((entity_name, cancel_msg))
        task = _sync_tasks.pop(entity_name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with patch("mindroom.orchestration.runtime.cancel_sync_task", side_effect=fake_cancel_sync_task):
        await stop_entities({"agent1", "agent2"}, agent_bots, sync_tasks)

    prepare_indexes = [index for index, item in enumerate(call_order) if item[0] == "prepare"]
    cancel_indexes = [index for index, item in enumerate(call_order) if item[0] == "cancel"]

    assert prepare_indexes
    assert cancel_indexes
    assert max(prepare_indexes) < min(cancel_indexes)
    assert sorted(cancel_messages) == [
        ("agent1", SYNC_RESTART_CANCEL_MSG),
        ("agent2", SYNC_RESTART_CANCEL_MSG),
    ]


@pytest.mark.asyncio
async def test_orchestrator_tracks_sync_tasks(tmp_path: Path) -> None:
    """Test that MultiAgentOrchestrator properly tracks sync tasks."""
    with (
        patch("mindroom.orchestrator.load_config") as mock_load_config,
        patch("mindroom.orchestrator.create_bot_for_entity") as mock_create_bot,
        patch("mindroom.orchestrator.sync_forever_with_restart"),
        patch("mindroom.orchestrator.ensure_all_rooms_exist") as mock_ensure_rooms,
        patch("mindroom.orchestrator.ensure_user_in_rooms") as mock_ensure_user,
        patch("mindroom.orchestrator.create_agent_user") as mock_create_user,
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
        config.mcp_servers = {}
        config.plugins = []
        config.cache = MagicMock()
        config.cache.resolve_db_path.return_value = tmp_path / "event_cache.db"
        config.mindroom_user = None
        config.get_all_configured_rooms.return_value = []
        mock_load_config.return_value = config

        # Create orchestrator
        orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

        assert orchestrator.config_path == (tmp_path / "config.yaml").resolve()

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
async def test_orchestrator_update_config_cancels_old_tasks(tmp_path: Path) -> None:
    """Test that update_config properly cancels old sync tasks."""
    with (
        patch("mindroom.orchestrator.load_config") as mock_load_config,
        patch("mindroom.orchestration.config_updates._identify_entities_to_restart") as mock_identify,
        patch("mindroom.orchestrator.stop_entities") as mock_stop_entities,
        patch("mindroom.orchestrator.create_bot_for_entity") as mock_create_bot,
        patch("mindroom.orchestrator.sync_forever_with_restart"),
        patch("mindroom.orchestrator.create_temp_user") as mock_create_temp_user,
        patch("mindroom.orchestrator.MultiAgentOrchestrator._setup_rooms_and_memberships", new=AsyncMock()),
    ):
        # Create orchestrator with existing agent
        orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

        # Setup existing config and bot
        old_config = MagicMock(spec=Config)
        old_config.agents = {"agent1": MagicMock()}
        old_config.teams = {}
        old_config.mcp_servers = {}
        old_config.cache = MagicMock()
        old_config.cache.resolve_db_path.return_value = tmp_path / "event_cache-old.db"
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
        new_config.mcp_servers = {}
        new_config.cache = MagicMock()
        new_config.cache.resolve_db_path.return_value = tmp_path / "event_cache-new.db"
        new_config.authorization = MagicMock()
        new_config.authorization.global_users = []  # Add this for the logging
        mock_load_config.return_value = new_config

        # Agent1 needs to be restarted
        mock_identify.return_value = {"agent1"}

        # Setup new bot creation
        mock_new_bot = AsyncMock()
        mock_new_bot.start = AsyncMock()
        mock_create_bot.return_value = mock_new_bot
        mock_create_temp_user.return_value = MagicMock()

        # Run update_config
        await orchestrator.update_config()

        # Verify stop_entities was called with sync_tasks dict
        mock_stop_entities.assert_called_once_with(
            {"agent1"},
            orchestrator.agent_bots,
            orchestrator._sync_tasks,
        )


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_new_agent_not_started_twice(tmp_path: Path) -> None:
    """Regression: a brand-new agent must only be started once.

    Before the fix, _get_changed_agents treated a new agent (old=None,
    new=exists) as "changed", so the agent appeared in both
    entities_to_restart AND new_entities.  update_config processed both
    sets, creating two bot instances with two sync loops for the same
    agent — causing duplicate replies.
    """
    with (
        patch("mindroom.orchestrator.create_bot_for_entity") as mock_create_bot,
        patch("mindroom.orchestrator.sync_forever_with_restart"),
        patch("mindroom.orchestrator.stop_entities"),
        patch("mindroom.orchestrator.create_temp_user") as mock_create_temp_user,
        patch.object(MultiAgentOrchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
    ):
        # --- existing orchestrator with one agent running ---
        orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

        old_config = Config(
            agents={
                "general": {
                    "display_name": "GeneralAgent",
                    "role": "General assistant",
                    "model": "default",
                    "rooms": ["lobby"],
                },
            },
            models={"default": {"provider": "test", "id": "test-model"}},
        )
        orchestrator.config = old_config

        mock_existing_bot = AsyncMock()
        mock_existing_bot.config = old_config
        orchestrator.agent_bots = {"general": mock_existing_bot, "router": AsyncMock()}

        async def existing_sync_loop() -> None:
            await asyncio.sleep(60)

        general_task = asyncio.create_task(existing_sync_loop())
        router_task = asyncio.create_task(existing_sync_loop())
        orchestrator._sync_tasks = {
            "general": general_task,
            "router": router_task,
        }

        # --- new config adds "coach" ---
        new_config = Config(
            agents={
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
            models={"default": {"provider": "test", "id": "test-model"}},
        )
        new_config.save_to_yaml(orchestrator.config_path)

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
        try:
            await orchestrator.update_config()
        finally:
            for task in [general_task, router_task]:
                task.cancel()
            await asyncio.gather(general_task, router_task, return_exceptions=True)

        # --- assert: create_bot_for_entity called exactly once for "coach" ---
        coach_calls = [c for c in mock_create_bot.call_args_list if c[0][0] == "coach"]
        assert len(coach_calls) == 1, (
            f"Expected create_bot_for_entity to be called once for 'coach', but was called {len(coach_calls)} times"
        )

        # Also verify only one sync task is tracked for coach
        assert "coach" in orchestrator._sync_tasks


@pytest.mark.asyncio
async def test_orchestrator_stop_cancels_all_tasks(tmp_path: Path) -> None:
    """Test that stop() cancels all sync tasks."""
    with patch("mindroom.orchestrator.cancel_sync_task") as mock_cancel:
        orchestrator = MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

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


# ---------------------------------------------------------------------------
# Fix 1: Env bypass — matrix_sync_startup_timeout_seconds uses RuntimePaths
# ---------------------------------------------------------------------------


def test_sync_startup_timeout_uses_runtime_paths() -> None:
    """The sync startup timeout must resolve via RuntimePaths, not os.environ."""
    rp = _fake_runtime_paths(MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS="42")
    assert matrix_sync_startup_timeout_seconds(rp) == 42.0


def test_sync_startup_timeout_default() -> None:
    """Without the env var, the default (600s) should be returned."""
    rp = _fake_runtime_paths()
    assert matrix_sync_startup_timeout_seconds(rp) == 600.0


def test_sync_startup_timeout_rejects_negative() -> None:
    """A negative value must raise ValueError."""
    rp = _fake_runtime_paths(MINDROOM_MATRIX_SYNC_STARTUP_TIMEOUT_SECONDS="-1")
    with pytest.raises(ValueError, match="must be a positive number"):
        matrix_sync_startup_timeout_seconds(rp)


# ---------------------------------------------------------------------------
# Fix 2: Coroutine leak on watchdog creation failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_coroutine_closed_on_create_task_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """If asyncio.create_task raises while creating the watchdog, the coroutine must be closed."""
    bot = _FakeBot()
    call_count = 0
    original_create_task = asyncio.create_task

    def failing_create_task(*args: object, **kwargs: object) -> asyncio.Task:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # Second create_task call (watchdog) fails
            msg = "simulated create_task failure"
            raise RuntimeError(msg)
        return original_create_task(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", failing_create_task)

    with pytest.raises(RuntimeError, match="simulated create_task failure"):
        _SyncIteration.start(bot)

    # No RuntimeWarning about unawaited coroutines should be produced.
    # The sync_task created by the first create_task was cancelled.


# ---------------------------------------------------------------------------
# Fix 3: Stale monotonic clock on restart
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_resets_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a watchdog-triggered restart, the new sync must get the full startup timeout.

    Regression: previously _last_sync_monotonic kept the stale value from the
    first iteration, so the watchdog immediately saw the new sync as stale.
    """
    bot = _FakeBot()

    # Track iterations: on iteration 1 stall immediately; on iteration 2 take
    # 80ms before the first callback, then complete.
    iteration = 0

    async def sync_impl() -> None:
        nonlocal iteration
        iteration += 1
        bot.sync_calls += 1
        if iteration == 1:
            # First sync stalls forever — watchdog should kill it.
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                bot.first_call_cancelled = True
                raise
        else:
            # Second sync: slow start, but within startup timeout.
            await asyncio.sleep(0.08)
            bot._last_sync_monotonic = time.monotonic()
            bot.running = False

    bot.sync_forever = sync_impl

    # Arm the monotonic clock on iteration 1 so the steady-state watchdog fires.
    original_mark = bot.mark_sync_loop_started

    def arm_and_mark() -> None:
        original_mark()
        if iteration == 0:
            bot._last_sync_monotonic = time.monotonic()

    bot.mark_sync_loop_started = arm_and_mark

    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)

    await sync_forever_with_restart(bot, max_retries=3)

    # First sync killed by watchdog, second sync completed normally.
    assert bot.first_call_cancelled is True
    assert iteration == 2
    assert bot.sync_calls == 2


# ---------------------------------------------------------------------------
# R4 Fix 1: Immediate sync_forever() failure must retry, not exit cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_immediate_sync_failure_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """If sync_forever() raises immediately, the loop must retry instead of breaking.

    Regression: asyncio.wait could return both tasks in `done` when sync_forever
    raises before the watchdog's first sleep.  The old code checked watchdog_task
    first, treated it as a clean stop, and broke without retrying.
    """
    bot = _FakeBot()
    call_count = 0

    async def failing_sync() -> None:
        nonlocal call_count
        bot.sync_calls += 1
        call_count += 1
        if call_count < 3:
            msg = "immediate sync failure"
            raise RuntimeError(msg)
        # Third call: stop cleanly.
        bot.running = False

    bot.sync_forever = failing_sync

    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 0.5)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)

    await sync_forever_with_restart(bot, max_retries=5)

    # Must have retried (3 calls total: 2 failures + 1 clean exit).
    assert call_count == 3


# ---------------------------------------------------------------------------
# R4 Fix 2: Single sync failure must not produce duplicate cleanup logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_failure_no_duplicate_cleanup_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single sync failure should produce exactly 1 cleanup warning, not 2+.

    Regression: _cancel_sync_iteration_tasks was called in except AND finally,
    causing the same task exception to be logged twice.
    """
    bot = _FakeBot()

    async def fail_once() -> None:
        bot.sync_calls += 1
        # Delay slightly so the watchdog task is still running (not in done).
        await asyncio.sleep(0.01)
        msg = "deliberate test error"
        raise RuntimeError(msg)

    bot.sync_forever = fail_once

    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 5.0)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)

    with caplog.at_level("WARNING", logger="mindroom.orchestration.runtime"):
        await sync_forever_with_restart(bot, max_retries=1)

    cleanup_warnings = [r for r in caplog.records if "Suppressed error during sync iteration cleanup" in r.message]
    assert len(cleanup_warnings) <= 1, f"Expected at most 1 cleanup warning, got {len(cleanup_warnings)}"
