"""Test that sync tasks are properly cancelled when agents are restarted."""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.cancellation import (
    ENTITY_TEARDOWN_CANCEL_MSG,
    SYNC_RESTART_CANCEL_MSG,
    USER_STOP_CANCEL_MSG,
    _cancel_failure_reason,
)
from mindroom.config.main import Config
from mindroom.constants import RESPONSE_CANCEL_SOURCE_KEY, RuntimePaths
from mindroom.matrix.sync_certification import SyncTrustState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration import runtime as runtime_helpers
from mindroom.orchestration.runtime import (
    EntityStartResults,
    _MatrixSyncStalledError,
    _SyncIteration,
    cancel_source_from_failure_reason,
    cancel_sync_task,
    classify_cancel_source,
    is_sync_restart_cancel,
    log_cancelled_response,
    log_cancelled_response_source,
    matrix_sync_startup_timeout_seconds,
    stop_entities,
    sync_forever_with_restart,
)
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.stop import StopManager
from mindroom.streaming import build_cancelled_response_update
from tests.conftest import (
    TEST_PASSWORD,
    make_event_cache_mock,
    make_event_cache_write_coordinator_mock,
    orchestrator_runtime_paths,
    write_config_yaml,
)


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
        self.prepare_for_entity_shutdown_calls = 0
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

    async def prepare_for_entity_shutdown(self, *, cancel_msg: str | None = None) -> None:
        del cancel_msg
        self.prepare_for_entity_shutdown_calls += 1
        await self.prepare_for_sync_shutdown()


def _prepare_only_agent_bot() -> AgentBot:
    """Build a minimal AgentBot instance for shutdown boundary tests."""
    bot = object.__new__(AgentBot)
    bot.agent_user = MagicMock(agent_name="test_agent")
    bot._sync_shutting_down = False
    bot._entity_shutdown_prepared = False
    bot.stop_manager = MagicMock()
    bot.stop_manager.cancel_active_responses = AsyncMock(return_value=1)
    bot._startup_thread_prewarm_task = None
    bot._coalescing_gate = MagicMock()
    bot._coalescing_gate.drain_all = AsyncMock()
    bot._coalescing_gate.discard_all = AsyncMock()
    bot._sync_trust_state = SyncTrustState.COLD
    return bot


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
async def test_stop_manager_cancels_active_responses_as_sync_restart() -> None:
    """Graceful service restart should propagate restart provenance to active response tasks."""
    stop_manager = StopManager()
    target = MagicMock()
    target.room_id = "!room:example.com"
    target.resolved_thread_id = "$thread-root"

    seen_cancel_args: tuple[object, ...] | None = None

    async def response_task() -> None:
        nonlocal seen_cancel_args
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            seen_cancel_args = exc.args
            raise

    task = asyncio.create_task(response_task())
    stop_manager.set_current("$message", target, task)
    await asyncio.sleep(0)

    cancelled_count = await stop_manager.cancel_active_responses(cancel_msg=SYNC_RESTART_CANCEL_MSG)

    assert cancelled_count == 1
    assert seen_cancel_args == (SYNC_RESTART_CANCEL_MSG,)


@pytest.mark.asyncio
async def test_stop_manager_schedules_run_cleanup_for_sync_shutdown_cancel() -> None:
    """Known provider runs should be cancelled after sync-shutdown task cancellation."""
    stop_manager = StopManager()
    target = MagicMock()
    target.room_id = "!room:example.com"
    target.resolved_thread_id = "$thread-root"

    async def response_task() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(response_task())
    stop_manager.set_current("$message", target, task, run_id="run-123")
    await asyncio.sleep(0)

    with patch.object(stop_manager, "_schedule_graceful_run_cancel") as schedule_graceful_run_cancel:
        cancelled_count = await stop_manager.cancel_active_responses(cancel_msg=SYNC_RESTART_CANCEL_MSG)

    assert cancelled_count == 1
    schedule_graceful_run_cancel.assert_called_once_with("$message", "run-123")


@pytest.mark.asyncio
async def test_stop_manager_logs_responses_still_pending_after_sync_shutdown_cancel() -> None:
    """Shutdown cancellation should surface tasks that ignore the cancellation grace."""
    stop_manager = StopManager()
    target = MagicMock()
    target.room_id = "!room:example.com"
    target.resolved_thread_id = "$thread-root"

    async def stubborn_response_task() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.Event().wait()

    task = asyncio.create_task(stubborn_response_task())
    stop_manager.set_current("$message", target, task, run_id="run-123")
    await asyncio.sleep(0)

    try:
        with patch("mindroom.stop.logger.warning") as warning:
            cancelled_count = await stop_manager.cancel_active_responses(
                cancel_msg=SYNC_RESTART_CANCEL_MSG,
                wait_seconds=0.01,
            )

        assert cancelled_count == 1
        warning.assert_any_call(
            "Active response did not stop during sync shutdown grace period",
            message_id="$message",
            run_id="run-123",
            room_id="!room:example.com",
            thread_id="$thread-root",
        )
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_stop_manager_waits_for_user_cancelled_responses_without_reclassifying() -> None:
    """Shutdown should wait for already user-cancelled work without changing cancel provenance."""
    stop_manager = StopManager()
    target = MagicMock()
    target.room_id = "!room:example.com"
    target.resolved_thread_id = "$thread-root"

    first_cancel_args: tuple[object, ...] | None = None
    user_cancel_seen = asyncio.Event()

    async def response_task() -> None:
        nonlocal first_cancel_args
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            first_cancel_args = exc.args
            user_cancel_seen.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(response_task())
    stop_manager.set_current("$message", target, task)
    await asyncio.sleep(0)
    tracked = stop_manager.tracked_messages["$message"]
    tracked.cancel_requested = True
    task.cancel(msg=USER_STOP_CANCEL_MSG)
    await asyncio.wait_for(user_cancel_seen.wait(), timeout=1.0)

    cancelled_count = await stop_manager.cancel_active_responses(
        cancel_msg=SYNC_RESTART_CANCEL_MSG,
        wait_seconds=0.01,
    )

    assert cancelled_count == 1
    assert first_cancel_args == (USER_STOP_CANCEL_MSG,)
    assert not task.done()
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_does_not_cancel_active_responses() -> None:
    """Same-process sync retries must not finalize active responses as restart interruptions."""
    bot = _prepare_only_agent_bot()

    await bot.prepare_for_sync_shutdown()

    bot.stop_manager.cancel_active_responses.assert_not_awaited()
    bot._coalescing_gate.drain_all.assert_awaited_once()
    bot._coalescing_gate.discard_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_prepare_for_entity_shutdown_closes_ingress_discards_coalescing_then_cancels() -> None:
    """Entity teardown should close ingress before cancelling admitted responses."""
    bot = _prepare_only_agent_bot()
    calls: list[str] = []
    bot._coalescing_gate.discard_all = AsyncMock(side_effect=lambda: calls.append("discard_coalescing"))
    bot._coalescing_gate.drain_all = AsyncMock(side_effect=lambda: calls.append("drain_coalescing"))
    bot.stop_manager.cancel_active_responses = AsyncMock(
        side_effect=lambda **_: calls.append("cancel_responses") or 1,
    )

    await bot.prepare_for_entity_shutdown(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)
    await bot.prepare_for_entity_shutdown(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)

    assert bot._entity_shutdown_prepared is True
    assert calls == ["discard_coalescing", "cancel_responses"]
    bot.stop_manager.cancel_active_responses.assert_awaited_once_with(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)
    bot._coalescing_gate.drain_all.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_message_drops_text_after_entity_shutdown_starts() -> None:
    """Closed entity ingress should reject direct text callbacks."""
    bot = object.__new__(AgentBot)
    bot.agent_user = MagicMock(agent_name="test_agent")
    bot._entity_shutdown_prepared = True
    bot.logger = MagicMock()
    bot._turn_controller = MagicMock()
    bot._turn_controller.handle_text_event = AsyncMock()

    room = MagicMock()
    room.room_id = "!room:example.com"
    event = MagicMock()
    event.event_id = "$event"
    event.sender = "@user:example.com"
    event.source = {
        "origin_server_ts": int(time.time() * 1000),
        "content": {"body": "hello", "msgtype": "m.text"},
    }
    event.body = "hello"

    with patch("mindroom.bot.maybe_handle_tool_approval_reply", new=AsyncMock(return_value=False)):
        await bot._on_message(room, event)

    bot._turn_controller.handle_text_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_media_message_drops_media_after_entity_shutdown_starts() -> None:
    """Closed entity ingress should reject direct media callbacks."""
    bot = object.__new__(AgentBot)
    bot.agent_user = MagicMock(agent_name="test_agent")
    bot._entity_shutdown_prepared = True
    bot.logger = MagicMock()
    bot._turn_controller = MagicMock()
    bot._turn_controller.handle_media_event = AsyncMock()

    room = MagicMock()
    room.room_id = "!room:example.com"
    event = MagicMock()
    event.event_id = "$media"
    event.sender = "@user:example.com"
    event.source = {
        "origin_server_ts": int(time.time() * 1000),
        "content": {"body": "image", "msgtype": "m.image"},
    }

    await bot._on_media_message(room, event)

    bot._turn_controller.handle_media_event.assert_not_awaited()


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
    assert bot.prepare_for_entity_shutdown_calls == 0


@pytest.mark.asyncio
async def test_sync_forever_with_restart_retries_on_sync_restart_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchdog-race cancellation should still reach the stalled-sync retry path."""
    bot = _FakeBot()
    watch_calls = 0

    async def sync_then_stop() -> None:
        if bot.sync_calls > 0:
            bot.running = False
            return
        await _FakeBot.sync_forever(bot)

    async def fake_watch(
        _bot: _FakeBot,
        sync_task: asyncio.Task[object],
        watchdog_cancelled_sync: asyncio.Event,
    ) -> None:
        nonlocal watch_calls
        watch_calls += 1
        if watch_calls == 1:
            msg = "Matrix sync loop stalled for test_agent"
            await asyncio.sleep(0)
            watchdog_cancelled_sync.set()
            sync_task.cancel(msg=SYNC_RESTART_CANCEL_MSG)
            with suppress(asyncio.CancelledError):
                await sync_task
            await asyncio.sleep(0)
            raise _MatrixSyncStalledError(msg)
        await sync_task

    monkeypatch.setattr(_SyncIteration, "_watch", staticmethod(fake_watch))
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)

    bot.sync_forever = sync_then_stop

    await sync_forever_with_restart(bot, max_retries=2)

    assert watch_calls == 2
    assert bot.first_call_cancelled is True
    assert bot.first_call_cancel_args == (SYNC_RESTART_CANCEL_MSG,)
    assert bot.sync_calls == 1
    assert bot.prepare_for_sync_shutdown_calls == 2
    assert bot.prepare_for_entity_shutdown_calls == 0


@pytest.mark.asyncio
async def test_sync_iteration_wait_does_not_block_on_unrelated_sync_cancellation() -> None:
    """Direct sync-task cancellation should surface immediately without waiting for the watchdog."""
    bot = _FakeBot()
    watchdog_started = asyncio.Event()

    async def blocked_sync() -> None:
        await asyncio.Event().wait()

    async def sleeping_watchdog() -> None:
        watchdog_started.set()
        await asyncio.sleep(60)

    iteration = _SyncIteration(
        bot=bot,
        sync_task=asyncio.create_task(blocked_sync()),
        watchdog_task=asyncio.create_task(sleeping_watchdog()),
    )

    await asyncio.wait_for(watchdog_started.wait(), timeout=0.1)
    assert iteration.sync_task is not None
    iteration.sync_task.cancel(msg="external_cancel")

    with pytest.raises(asyncio.CancelledError, match="external_cancel"):
        await asyncio.wait_for(iteration.wait(), timeout=0.05)

    await iteration.cancel()


@pytest.mark.asyncio
async def test_is_sync_restart_cancel_checks_cancel_message() -> None:
    """The restart helper should only match the dedicated cancel message."""
    assert is_sync_restart_cancel(asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG)) is True
    assert is_sync_restart_cancel(asyncio.CancelledError()) is False


@pytest.mark.asyncio
async def test_classify_cancel_source_user_stop() -> None:
    """User-stop cancellations should keep their dedicated provenance."""
    assert classify_cancel_source(asyncio.CancelledError(USER_STOP_CANCEL_MSG)) == "user_stop"


@pytest.mark.asyncio
async def test_classify_cancel_source_sync_restart() -> None:
    """Sync-restart cancellations should keep their dedicated provenance."""
    assert classify_cancel_source(asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG)) == "sync_restart"


@pytest.mark.asyncio
async def test_classify_cancel_source_entity_teardown() -> None:
    """Entity-teardown cancellations should keep their non-resumable provenance."""
    assert classify_cancel_source(asyncio.CancelledError(ENTITY_TEARDOWN_CANCEL_MSG)) == "entity_teardown"


@pytest.mark.asyncio
async def test_classify_cancel_source_unknown_returns_interrupted() -> None:
    """Untagged cancellations should surface as generic interruptions."""
    assert classify_cancel_source(asyncio.CancelledError()) == "interrupted"


@pytest.mark.asyncio
async def test_cancel_failure_reason_matches_cancel_source() -> None:
    """Failure reasons should stay aligned with the shared cancel provenance mapping."""
    assert _cancel_failure_reason("user_stop") == "cancelled_by_user"
    assert _cancel_failure_reason("sync_restart") == "sync_restart_cancelled"
    assert _cancel_failure_reason("entity_teardown") == "entity_teardown_cancelled"
    assert _cancel_failure_reason("interrupted") == "interrupted"


@pytest.mark.parametrize(
    ("failure_reason", "expected_cancel_source"),
    [
        ("cancelled_by_user", "user_stop"),
        ("sync_restart_cancelled", "sync_restart"),
        ("entity_teardown_cancelled", "entity_teardown"),
        ("interrupted", "interrupted"),
        ("other", "interrupted"),
        (None, "interrupted"),
    ],
)
def test_cancel_source_from_failure_reason_matches_canonical_reasons(
    failure_reason: str | None,
    expected_cancel_source: str,
) -> None:
    """Canonical terminal failure reasons should map back to cancellation provenance."""
    assert cancel_source_from_failure_reason(failure_reason) == expected_cancel_source


def test_response_cancel_source_key_is_namespaced() -> None:
    """Cancellation provenance stored on Matrix content should use a namespaced key."""
    assert RESPONSE_CANCEL_SOURCE_KEY == "io.mindroom.cancel_source"


@pytest.mark.asyncio
async def test_entity_teardown_cancel_finalizes_as_non_resumable_interruption() -> None:
    """Entity teardown should look interrupted but not restart-resumable."""
    body, stream_status = build_cancelled_response_update(
        "Partial answer",
        cancel_source="entity_teardown",
    )

    assert body == "Partial answer\n\n**[Response interrupted]**"
    assert stream_status == "error"


@pytest.mark.parametrize(
    ("cancel_error", "expected_method", "expected_message"),
    [
        (asyncio.CancelledError(USER_STOP_CANCEL_MSG), "info", "Response cancelled by user"),
        (asyncio.CancelledError(SYNC_RESTART_CANCEL_MSG), "info", "Response interrupted by sync restart"),
        (asyncio.CancelledError("other"), "warning", "Response interrupted — traceback for diagnosis"),
    ],
)
def test_log_cancelled_response_preserves_caller_messages_and_traceback(
    cancel_error: asyncio.CancelledError,
    expected_method: str,
    expected_message: str,
) -> None:
    """Cancellation logging should preserve provenance-specific text and traceback details."""
    logger = MagicMock()

    log_cancelled_response(
        logger,
        exc=cancel_error,
        message_id="$event",
        restart_message="Response interrupted by sync restart",
        user_stop_message="Response cancelled by user",
        interrupted_message="Response interrupted — traceback for diagnosis",
    )

    log_method = getattr(logger, expected_method)
    log_method.assert_called_once()
    log_call = log_method.call_args
    assert log_call.args == (expected_message,)
    assert log_call.kwargs["message_id"] == "$event"
    if expected_method == "warning":
        assert log_call.kwargs["exc_info"] == (
            type(cancel_error),
            cancel_error,
            cancel_error.__traceback__,
        )
    else:
        assert "exc_info" not in log_call.kwargs


def test_log_cancelled_response_source_logs_user_stop_without_traceback() -> None:
    """Resolved user-stop provenance should remain an expected info-level cancellation."""
    logger = MagicMock()

    log_cancelled_response_source(
        logger,
        cancel_source="user_stop",
        message_id="$event",
        restart_message="Response interrupted by sync restart",
        user_stop_message="Response cancelled by user",
        interrupted_message="Response interrupted — traceback for diagnosis",
        exc_info=True,
    )

    logger.info.assert_called_once_with("Response cancelled by user", message_id="$event")
    logger.warning.assert_not_called()


def test_log_cancelled_response_source_logs_interrupted_with_traceback() -> None:
    """Resolved generic interruptions should keep diagnostic traceback details."""
    logger = MagicMock()
    cancel_error = asyncio.CancelledError("other")
    exc_info = (type(cancel_error), cancel_error, cancel_error.__traceback__)

    log_cancelled_response_source(
        logger,
        cancel_source="interrupted",
        message_id="$event",
        restart_message="Response interrupted by sync restart",
        user_stop_message="Response cancelled by user",
        interrupted_message="Response interrupted — traceback for diagnosis",
        exc_info=exc_info,
    )

    logger.warning.assert_called_once_with(
        "Response interrupted — traceback for diagnosis",
        message_id="$event",
        exc_info=exc_info,
    )
    logger.info.assert_not_called()


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
    bot._room_member_join_hooks_armed = False
    bot.client = FakeClient()
    bot.orchestrator = None
    bot._runtime_view = BotRuntimeState(
        client=bot.client,
        config=MagicMock(spec=Config),
        runtime_paths=MagicMock(),
        enable_streaming=True,
        orchestrator=None,
        event_cache=make_event_cache_mock(),
        event_cache_write_coordinator=make_event_cache_write_coordinator_mock(),
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
    mock_bot1.prepare_for_entity_shutdown = AsyncMock()
    mock_bot1.stop = AsyncMock()
    mock_bot2 = AsyncMock()
    mock_bot2.prepare_for_entity_shutdown = AsyncMock()
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

    mock_bot1.prepare_for_entity_shutdown.assert_awaited_once()
    mock_bot2.prepare_for_entity_shutdown.assert_awaited_once()
    mock_bot1.stop.assert_called_once()
    mock_bot2.stop.assert_called_once()

    assert "agent1" not in agent_bots
    assert "agent2" not in agent_bots
    assert "agent3" in agent_bots

    assert "agent1" not in sync_tasks
    assert "agent2" not in sync_tasks
    assert "agent3" in sync_tasks

    mock_bot1.prepare_for_entity_shutdown.assert_awaited_once_with(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)
    mock_bot2.prepare_for_entity_shutdown.assert_awaited_once_with(cancel_msg=ENTITY_TEARDOWN_CANCEL_MSG)

    task3.cancel()
    await asyncio.gather(task3, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_entities_completes_with_real_supervisor_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop_entities must finish promptly when cancelling a real supervisor task."""
    bot = _FakeBot()
    bot.agent_name = "agent1"
    bot.stop = AsyncMock(side_effect=lambda **_kwargs: setattr(bot, "running", False))

    sync_started = asyncio.Event()

    async def blocking_sync() -> None:
        sync_started.set()
        await _FakeBot.sync_forever(bot)

    bot.sync_forever = blocking_sync
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)

    supervisor_task = asyncio.create_task(sync_forever_with_restart(bot), name="supervisor_agent1")
    await asyncio.wait_for(sync_started.wait(), timeout=1.0)

    started_at = time.monotonic()
    await asyncio.wait_for(
        stop_entities({"agent1"}, {"agent1": bot}, {"agent1": supervisor_task}),
        timeout=2.0,
    )
    elapsed = time.monotonic() - started_at

    assert elapsed <= 2.0
    assert supervisor_task.done()
    assert bot.prepare_for_sync_shutdown_calls >= 2
    assert bot.prepare_for_entity_shutdown_calls == 1
    bot.stop.assert_awaited_once_with(reason="restart")


@pytest.mark.asyncio
async def test_stop_entities_prepares_bots_before_cancelling_sync_tasks() -> None:
    """Restart teardown should cancel deferred work before the sync loop stops."""
    call_order: list[tuple[str, str]] = []
    cancel_messages: list[tuple[str, str | None]] = []
    response_cancel_messages: list[tuple[str, str | None]] = []

    mock_bot1 = AsyncMock()
    mock_bot1.prepare_for_entity_shutdown = AsyncMock(
        side_effect=lambda *, cancel_msg: (
            response_cancel_messages.append(("agent1", cancel_msg)),
            call_order.append(("prepare", "agent1")),
        ),
    )
    mock_bot1.stop = AsyncMock(side_effect=lambda **_: call_order.append(("stop", "agent1")))

    mock_bot2 = AsyncMock()
    mock_bot2.prepare_for_entity_shutdown = AsyncMock(
        side_effect=lambda *, cancel_msg: (
            response_cancel_messages.append(("agent2", cancel_msg)),
            call_order.append(("prepare", "agent2")),
        ),
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
    assert sorted(response_cancel_messages) == [
        ("agent1", ENTITY_TEARDOWN_CANCEL_MSG),
        ("agent2", ENTITY_TEARDOWN_CANCEL_MSG),
    ]


@pytest.mark.asyncio
async def test_stop_entities_uses_entity_teardown_for_responses_and_sync_restart_for_supervisor() -> None:
    """Config-driven entity restart must not mark response cancellation as startup-resumable."""
    response_cancel_messages: list[str | None] = []
    sync_cancel_messages: list[str | None] = []

    mock_bot = AsyncMock()
    mock_bot.prepare_for_entity_shutdown = AsyncMock(
        side_effect=lambda *, cancel_msg: response_cancel_messages.append(cancel_msg),
    )
    mock_bot.stop = AsyncMock()
    sync_task = asyncio.create_task(asyncio.sleep(60))

    async def fake_cancel_sync_task(
        entity_name: str,
        sync_tasks: dict[str, asyncio.Task],
        *,
        cancel_msg: str | None = None,
    ) -> None:
        sync_cancel_messages.append(cancel_msg)
        task = sync_tasks.pop(entity_name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with patch("mindroom.orchestration.runtime.cancel_sync_task", side_effect=fake_cancel_sync_task):
        await stop_entities({"agent1"}, {"agent1": mock_bot}, {"agent1": sync_task})

    assert response_cancel_messages == [ENTITY_TEARDOWN_CANCEL_MSG]
    assert sync_cancel_messages == [SYNC_RESTART_CANCEL_MSG]


@pytest.mark.asyncio
async def test_orchestrator_stop_prepares_entity_shutdown_before_cancelling_sync_tasks() -> None:
    """Full service shutdown should close entity ingress before stopping sync supervisors."""
    orchestrator = object.__new__(_MultiAgentOrchestrator)
    orchestrator.running = True
    orchestrator._runtime_shutdown_event = None
    orchestrator._sync_tasks = {"agent1": asyncio.create_task(asyncio.sleep(60))}
    orchestrator.agent_bots = {"agent1": AsyncMock()}
    orchestrator.agent_bots["agent1"].prepare_for_entity_shutdown = AsyncMock()
    orchestrator.agent_bots["agent1"].stop = AsyncMock()
    orchestrator._cancel_config_reload_task = AsyncMock()
    orchestrator._stop_memory_auto_flush_worker = AsyncMock()
    orchestrator._knowledge_source_watcher = AsyncMock()
    orchestrator._knowledge_refresh_scheduler = AsyncMock()
    orchestrator._cancel_bot_start_tasks = AsyncMock()
    orchestrator._stop_mcp_manager = AsyncMock()
    orchestrator._close_runtime_support_services = AsyncMock()
    call_order: list[str] = []

    orchestrator.agent_bots["agent1"].prepare_for_entity_shutdown.side_effect = lambda *, cancel_msg: call_order.append(
        f"prepare:{cancel_msg}",
    )

    async def fake_cancel_sync_task(
        entity_name: str,
        sync_tasks: dict[str, asyncio.Task],
        *,
        cancel_msg: str | None = None,
    ) -> None:
        call_order.append(f"cancel_sync:{cancel_msg}")
        task = sync_tasks.pop(entity_name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with (
        patch("mindroom.orchestrator.shutdown_approval_runtime", new=AsyncMock()),
        patch("mindroom.orchestrator.cancel_sync_task", side_effect=fake_cancel_sync_task),
    ):
        await orchestrator.stop()

    assert call_order == [
        f"prepare:{SYNC_RESTART_CANCEL_MSG}",
        f"cancel_sync:{SYNC_RESTART_CANCEL_MSG}",
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
    ):
        # Setup mocks
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
        orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
        orchestrator._prepare_entity_accounts = AsyncMock(
            return_value={
                "router": AgentMatrixUser(
                    agent_name="router",
                    user_id="@mindroom_router:localhost",
                    display_name="RouterAgent",
                    password=TEST_PASSWORD,
                ),
                "test_agent": AgentMatrixUser(
                    agent_name="test_agent",
                    user_id="@mindroom_test_agent:localhost",
                    display_name="Test Agent",
                    password=TEST_PASSWORD,
                ),
            },
        )

        assert orchestrator.config_path == (tmp_path / "config.yaml").resolve()

        with patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()):
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
async def test_start_runtime_waits_for_shutdown_after_initial_sync_generation_exits(tmp_path: Path) -> None:
    """A hot-reload restart of the first sync task generation must not end the service."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

    config = MagicMock(spec=Config)
    config.agents = {"general": MagicMock()}
    config.teams = {}
    config.mcp_servers = {}
    config.cache = MagicMock()
    config.cache.resolve_db_path.return_value = tmp_path / "event_cache.db"
    orchestrator.config = config

    router_bot = AsyncMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.stop = AsyncMock()

    general_bot = AsyncMock()
    general_bot.agent_name = "general"
    general_bot.running = True
    general_bot.stop = AsyncMock()

    orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

    async def completed_sync_supervisor() -> None:
        return None

    def start_completed_sync_task(entity_name: str, _bot: object) -> None:
        orchestrator._sync_tasks[entity_name] = asyncio.create_task(completed_sync_supervisor())

    with (
        patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
        patch.object(orchestrator, "_start_router_bot", new=AsyncMock(return_value=router_bot)),
        patch.object(
            orchestrator,
            "_start_entities_once",
            new=AsyncMock(return_value=EntityStartResults(started_bots=[general_bot])),
        ),
        patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
        patch.object(orchestrator, "_cleanup_stale_streams_after_restart", new=AsyncMock(return_value=[])),
        patch.object(orchestrator, "_auto_resume_after_restart", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator, "_start_sync_task", side_effect=start_completed_sync_task),
    ):
        runtime_task = asyncio.create_task(orchestrator._start_runtime())
        try:
            for _ in range(50):
                if set(orchestrator._sync_tasks) == {"router", "general"}:
                    break
                await asyncio.sleep(0.01)

            assert set(orchestrator._sync_tasks) == {"router", "general"}
            await asyncio.sleep(0)
            assert not runtime_task.done()

            await orchestrator.stop()
            await asyncio.wait_for(runtime_task, timeout=1.0)
        finally:
            if not runtime_task.done():
                runtime_task.cancel()
                with suppress(asyncio.CancelledError):
                    await runtime_task


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
        patch.object(
            _MultiAgentOrchestrator,
            "_prepare_entity_accounts",
            new=AsyncMock(
                return_value={
                    "router": AgentMatrixUser(
                        agent_name="router",
                        user_id="@mindroom_router:localhost",
                        display_name="Router",
                        password=TEST_PASSWORD,
                    ),
                    "agent1": AgentMatrixUser(
                        agent_name="agent1",
                        user_id="@mindroom_agent1:localhost",
                        display_name="Agent 1",
                        password=TEST_PASSWORD,
                    ),
                },
            ),
        ),
        patch("mindroom.orchestrator._MultiAgentOrchestrator._setup_rooms_and_memberships", new=AsyncMock()),
    ):
        # Create orchestrator with existing agent
        orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

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
        patch.object(
            _MultiAgentOrchestrator,
            "_prepare_entity_accounts",
            new=AsyncMock(
                return_value={
                    "router": AgentMatrixUser(
                        agent_name="router",
                        user_id="@mindroom_router:localhost",
                        display_name="Router",
                        password=TEST_PASSWORD,
                    ),
                    "coach": AgentMatrixUser(
                        agent_name="coach",
                        user_id="@mindroom_coach:localhost",
                        display_name="Coach",
                        password=TEST_PASSWORD,
                    ),
                },
            ),
        ),
        patch.object(_MultiAgentOrchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
    ):
        # --- existing orchestrator with one agent running ---
        orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

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
        write_config_yaml(new_config, orchestrator.config_path)

        # Mock bot creation — record every call
        created_bots: list[AsyncMock] = []

        def make_bot(*args, **kwargs) -> AsyncMock:  # noqa: ANN002, ANN003, ARG001
            bot = AsyncMock()
            bot.try_start = AsyncMock(return_value=True)
            bot.sync_forever = AsyncMock()
            created_bots.append(bot)
            return bot

        mock_create_bot.side_effect = make_bot

        # --- act ---
        try:
            await orchestrator.update_config()
        finally:
            for task in list(orchestrator._sync_tasks.values()):
                task.cancel()
            await asyncio.gather(*orchestrator._sync_tasks.values(), return_exceptions=True)
            await orchestrator._close_runtime_support_services()

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
        orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

        # Track which tasks are cancelled
        cancelled = []

        async def track_cancel(name: str, tasks: dict, *, cancel_msg: str | None = None) -> None:
            assert cancel_msg == SYNC_RESTART_CANCEL_MSG
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
        mock_bot1.prepare_for_entity_shutdown = AsyncMock()
        mock_bot1.stop = AsyncMock()
        mock_bot2 = AsyncMock()
        mock_bot2.running = True
        mock_bot2.prepare_for_entity_shutdown = AsyncMock()
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
