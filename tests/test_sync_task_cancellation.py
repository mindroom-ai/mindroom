"""Test that sync tasks are properly cancelled when agents are restarted."""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

import pytest
from nio.ingest.model import TransportKind
from nio.store._sync_journal_values import _FrameCompletion
from structlog.testing import capture_logs

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.cancellation import (
    SYNC_RESTART_CANCEL_MSG,
    USER_STOP_CANCEL_MSG,
    cancel_failure_reason,
    cancel_message_for_source,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import RuntimePaths
from mindroom.matrix.client_session import MindRoomAsyncClient
from mindroom.matrix.health import (
    SyncCacheWriteProgress,
    _track_matrix_sync_cache_write,
    get_matrix_sync_cache_write_progress,
    get_matrix_sync_health_snapshot,
    mark_matrix_ingestion_progress,
    mark_matrix_sync_loop_started,
    mark_matrix_sync_success,
    reset_matrix_sync_health,
)
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.sync_loop import (
    _sliding_sync_lists,
    _sliding_sync_room_subscriptions,
)
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration import runtime as runtime_helpers
from mindroom.orchestration.config_updates import ConfigUpdatePlan, build_config_update_plan
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
    matrix_sync_cache_write_grace_seconds,
    matrix_sync_startup_timeout_seconds,
    stop_entities,
    sync_forever_with_restart,
)
from mindroom.orchestrator import _MultiAgentOrchestrator, _run_shutdown_step
from mindroom.response_delivery import RecoveryOutcome
from mindroom.response_runner import ResponseRunner, ResponseShutdownTimeoutError
from mindroom.runtime_shutdown import (
    ENTITY_REMOVED_SHUTDOWN,
    GENERIC_SHUTDOWN,
    ORDERLY_SHUTDOWN,
    SYNC_RESTART_SHUTDOWN,
    SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS,
    RuntimeShutdownIntent,
    ShutdownBudget,
    shutdown_intent_for_entity,
)
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
    write_config_yaml,
)


async def _complete_frame(bot: AgentBot, index: int = 0) -> None:
    """Drive runtime side effects through the durable completion owner."""
    await bot._on_ingestion_frame_completion(
        _FrameCompletion(
            UUID(f"30000000-0000-4000-8000-{index + 1:012d}"),
            TransportKind.CLASSIC,
            0,
            index,
            index * 2 + 1,
            index * 2 + 2,
        ),
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
        self._matrix_ingestion_quiesce_requested = False
        self._durable_ingestion_progress_generation: int | None = None
        self.sync_calls = 0
        self.first_call_cancelled = False
        self.first_call_cancel_args: tuple[object, ...] = ()
        self.prepare_for_sync_shutdown_calls = 0
        self.prepare_for_sync_shutdown_cancel_messages: list[str | None] = []
        self.runtime_paths = _fake_runtime_paths(**env_overrides)

    def mark_sync_loop_started(self) -> None:
        self._sync_shutting_down = False

    def reset_watchdog_clock(self) -> None:
        self._last_sync_monotonic = None

    def seconds_since_last_sync_activity(self) -> float | None:
        if self._last_sync_monotonic is None:
            return None
        return time.monotonic() - self._last_sync_monotonic

    def sync_cache_write_progress(self) -> SyncCacheWriteProgress | None:
        return get_matrix_sync_cache_write_progress(self.agent_name)

    def durable_ingestion_progress_generation(self) -> int | None:
        return self._durable_ingestion_progress_generation

    @property
    def in_flight_response_count(self) -> int:
        """Return the fake bot's active response count."""
        return 0

    async def sync_forever(self) -> None:
        self.sync_calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            if self.sync_calls == 1:
                self.first_call_cancelled = True
                self.first_call_cancel_args = exc.args
            raise

    async def _quiesce_matrix_ingestion(self) -> None:
        """Mirror the real bot's no-session clean-stop boundary."""

    async def prepare_for_sync_shutdown(
        self,
        *,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> None:
        self._sync_shutting_down = True
        self.prepare_for_sync_shutdown_calls += 1
        self.prepare_for_sync_shutdown_cancel_messages.append(shutdown_intent.cancel_source)


class _ResponseOwningBot(_FakeBot):
    """Fake sync transport plus independently owned response tasks."""

    def __init__(self) -> None:
        super().__init__()
        self.live_sync_count = 0
        self.max_live_sync_count = 0
        self.sync_starts: asyncio.Queue[int] = asyncio.Queue()
        self.sync_releases: dict[int, asyncio.Event] = {}
        self.response_finish = asyncio.Event()
        self.response_tasks: list[asyncio.Task[None]] = []
        self.response_cancel_sources: list[str] = []
        self.response_completions = 0

    @property
    def in_flight_response_count(self) -> int:
        """Return response tasks whose single owner has not settled them."""
        return sum(not task.done() for task in self.response_tasks)

    def start_responses(self, count: int) -> None:
        """Start deterministic responses blocked on one shared completion event."""
        self.response_tasks.extend(
            asyncio.create_task(self._run_response(), name=f"owned_response_{index}") for index in range(count)
        )

    async def _run_response(self) -> None:
        try:
            await self.response_finish.wait()
        except asyncio.CancelledError as exc:
            self.response_cancel_sources.append(classify_cancel_source(exc))
            raise
        self.response_completions += 1

    async def sync_forever(self) -> None:
        self.sync_calls += 1
        iteration = self.sync_calls
        release = self.sync_releases.setdefault(iteration, asyncio.Event())
        self.live_sync_count += 1
        self.max_live_sync_count = max(self.max_live_sync_count, self.live_sync_count)
        await self.sync_starts.put(iteration)
        try:
            await release.wait()
        finally:
            self.live_sync_count -= 1

    async def prepare_for_sync_shutdown(
        self,
        *,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> None:
        await super().prepare_for_sync_shutdown(shutdown_intent=shutdown_intent)
        cancel_message = cancel_message_for_source(shutdown_intent.cancel_source)
        active_responses = [task for task in self.response_tasks if not task.done()]
        for task in active_responses:
            if cancel_message is None:
                task.cancel()
            else:
                task.cancel(msg=cancel_message)
        await asyncio.gather(*active_responses, return_exceptions=True)


def _install_deterministic_stalls(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stall_count: int,
) -> None:
    """Replace watchdog timing with a fixed number of immediate stalls."""
    remaining_stalls = stall_count

    async def watch(
        _bot: _FakeBot,
        sync_task: asyncio.Task[object],
        watchdog_cancelled_sync: asyncio.Event,
    ) -> None:
        nonlocal remaining_stalls
        if remaining_stalls == 0:
            await sync_task
            return
        remaining_stalls -= 1
        watchdog_cancelled_sync.set()
        sync_task.cancel(msg=SYNC_RESTART_CANCEL_MSG)
        await asyncio.gather(sync_task, return_exceptions=True)
        msg = "Matrix sync loop stalled"
        raise _MatrixSyncStalledError(msg)

    monkeypatch.setattr(_SyncIteration, "_watch", staticmethod(watch))
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", lambda: 0.0)


async def _finish_responses_and_stop_transport(
    bot: _ResponseOwningBot,
    *,
    final_sync_iteration: int,
    supervisor: asyncio.Task[None],
) -> None:
    """Complete responses once, then stop the final receive loop cleanly."""
    bot.response_finish.set()
    await asyncio.gather(*bot.response_tasks)
    bot.running = False
    bot.sync_releases[final_sync_iteration].set()
    await supervisor


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
async def test_sync_forever_cancels_iteration_before_checkpoint_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync callbacks must be stopped before shutdown drain can certify a checkpoint."""
    bot = _FakeBot()
    call_order: list[str] = []

    async def prepare_for_sync_shutdown(**_kwargs: object) -> None:
        call_order.append("prepare")

    class FakeIteration:
        async def wait(self) -> None:
            bot.running = False

        async def cancel(self, *, shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN) -> None:
            assert shutdown_intent == GENERIC_SHUTDOWN
            call_order.append("cancel")

    bot.prepare_for_sync_shutdown = prepare_for_sync_shutdown
    monkeypatch.setattr(_SyncIteration, "start", lambda _bot: FakeIteration())

    await sync_forever_with_restart(bot)

    assert call_order == ["cancel", "prepare"]


@pytest.mark.asyncio
async def test_sync_supervisor_does_not_restart_quiesced_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean-stop source return must not start a replacement receive loop."""
    bot = _FakeBot()
    starts = 0

    class FakeIteration:
        async def wait(self) -> None:
            bot._matrix_ingestion_quiesce_requested = True

        async def cancel(
            self,
            *,
            shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
        ) -> None:
            assert shutdown_intent == GENERIC_SHUTDOWN

    def start_iteration(_bot: _FakeBot) -> FakeIteration:
        nonlocal starts
        starts += 1
        return FakeIteration()

    monkeypatch.setattr(_SyncIteration, "start", start_iteration)
    monkeypatch.setattr(
        runtime_helpers,
        "retry_delay_seconds",
        lambda *_args, **_kwargs: 0.0,
    )

    with capture_logs() as logs:
        await sync_forever_with_restart(bot, max_retries=2)

    assert starts == 1
    assert not any(entry["event"] in {"sync_loop_returned_while_bot_running", "restarting_sync_loop"} for entry in logs)


@pytest.mark.asyncio
async def test_sync_watchdog_waits_for_quiesced_source_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A watchdog tick must not cancel the final source commit."""
    bot = _FakeBot()
    source_started = asyncio.Event()
    allow_source_commit = asyncio.Event()
    source_committed = asyncio.Event()
    source_cancelled = asyncio.Event()
    watchdog_polled = asyncio.Event()
    release_watchdog_poll = asyncio.Event()

    async def request_quiesce_during_poll(_delay: float) -> None:
        bot._matrix_ingestion_quiesce_requested = True
        watchdog_polled.set()
        await release_watchdog_poll.wait()

    async def held_source_commit() -> None:
        source_started.set()
        try:
            await allow_source_commit.wait()
        except asyncio.CancelledError:
            source_cancelled.set()
            raise
        source_committed.set()

    monkeypatch.setattr(runtime_helpers.asyncio, "sleep", request_quiesce_during_poll)
    bot.sync_forever = held_source_commit
    supervisor = asyncio.create_task(sync_forever_with_restart(bot, max_retries=1))

    try:
        await asyncio.wait_for(source_started.wait(), timeout=1)
        await asyncio.wait_for(watchdog_polled.wait(), timeout=1)
        release_watchdog_poll.set()
        done, _ = await asyncio.wait({supervisor}, timeout=0.05)
        assert not done
        assert not source_cancelled.is_set()
        allow_source_commit.set()
        release_watchdog_poll.set()
        await asyncio.wait_for(supervisor, timeout=1)
    finally:
        allow_source_commit.set()
        if not supervisor.done():
            supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)

    assert source_committed.is_set()
    assert not source_cancelled.is_set()


@pytest.mark.asyncio
async def test_ingestion_quiesce_marks_supervisor_stop_intent() -> None:
    """The terminal source barrier owns the one supervisor-stop write."""
    bot = object.__new__(AgentBot)
    bot._matrix_ingestion_quiesce_requested = False
    bot._ingestion_session = AsyncMock()

    await AgentBot._quiesce_matrix_ingestion(bot)

    assert bot._matrix_ingestion_quiesce_requested is True
    bot._ingestion_session._quiesce.assert_awaited_once()


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
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", lambda: 0.0)

    await sync_forever_with_restart(bot, max_retries=2)

    assert bot.first_call_cancelled is True
    assert bot.first_call_cancel_args == (SYNC_RESTART_CANCEL_MSG,)
    assert bot.sync_calls == 1  # sync_forever called once, then sync_then_stop stopped
    assert bot.prepare_for_sync_shutdown_calls == 1
    assert bot.prepare_for_sync_shutdown_cancel_messages == [None]


@pytest.mark.asyncio
async def test_watchdog_restart_preserves_one_active_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """One stalled receive loop must not cancel, retry, or duplicate an active response."""
    bot = _ResponseOwningBot()
    bot.start_responses(1)
    _install_deterministic_stalls(monkeypatch, stall_count=1)

    with capture_logs() as logs:
        supervisor = asyncio.create_task(sync_forever_with_restart(bot, max_retries=3))
        assert await bot.sync_starts.get() == 1
        assert await bot.sync_starts.get() == 2

        assert bot.live_sync_count == 1
        assert bot.in_flight_response_count == 1
        assert not bot.response_tasks[0].done()
        assert bot.response_cancel_sources == []

        await _finish_responses_and_stop_transport(bot, final_sync_iteration=2, supervisor=supervisor)

    assert bot.response_completions == 1
    assert bot.response_cancel_sources == []
    restart_logs = [entry for entry in logs if entry["event"] == "matrix_sync_transport_restart"]
    assert len(restart_logs) == 1
    assert restart_logs[0]["active_response_count"] == 1
    assert restart_logs[0]["restart_reason_category"] == "watchdog_stall"
    assert restart_logs[0]["resulting_action"] == "restart_receive_loop"
    # Attribute the flapping entity without naming any Matrix conversation.
    assert restart_logs[0]["agent"] == "test_agent"
    assert not {"room_id", "event_id", "user_id"} & restart_logs[0].keys()


@pytest.mark.asyncio
async def test_watchdog_restart_preserves_several_active_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """One receive-loop restart must leave every active response with its original owner."""
    bot = _ResponseOwningBot()
    bot.start_responses(3)
    _install_deterministic_stalls(monkeypatch, stall_count=1)

    supervisor = asyncio.create_task(sync_forever_with_restart(bot, max_retries=3))
    assert await bot.sync_starts.get() == 1
    assert await bot.sync_starts.get() == 2

    assert bot.in_flight_response_count == 3
    assert all(not task.done() for task in bot.response_tasks)
    assert bot.response_cancel_sources == []

    await _finish_responses_and_stop_transport(bot, final_sync_iteration=2, supervisor=supervisor)

    assert bot.response_completions == 3
    assert bot.response_cancel_sources == []


@pytest.mark.asyncio
async def test_repeated_watchdog_restarts_keep_one_sync_loop_and_response_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated stalls must replace receive loops serially without touching responses."""
    bot = _ResponseOwningBot()
    bot.start_responses(2)
    _install_deterministic_stalls(monkeypatch, stall_count=3)

    supervisor = asyncio.create_task(sync_forever_with_restart(bot, max_retries=5))
    assert [await bot.sync_starts.get() for _ in range(4)] == [1, 2, 3, 4]

    assert bot.sync_calls == 4
    assert bot.live_sync_count == 1
    assert bot.max_live_sync_count == 1
    assert bot.in_flight_response_count == 2
    assert bot.response_cancel_sources == []

    await _finish_responses_and_stop_transport(bot, final_sync_iteration=4, supervisor=supervisor)

    assert bot.live_sync_count == 0
    assert bot.response_completions == 2
    assert bot.response_cancel_sources == []


@pytest.mark.asyncio
async def test_config_reload_cancellation_keeps_interruption_and_retry_semantics() -> None:
    """Full bot replacement must still cancel and queue recovery for active responses."""
    bot = _ResponseOwningBot()
    bot.start_responses(1)

    supervisor = asyncio.create_task(sync_forever_with_restart(bot))
    assert await bot.sync_starts.get() == 1
    supervisor.cancel(msg=SYNC_RESTART_CANCEL_MSG)
    await supervisor
    assert bot.live_sync_count == 0
    assert bot.in_flight_response_count == 1

    await bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    assert bot.in_flight_response_count == 0
    assert bot.response_completions == 0
    assert bot.response_cancel_sources == ["sync_restart"]
    assert bot.prepare_for_sync_shutdown_cancel_messages == ["sync_restart"]


@pytest.mark.asyncio
async def test_process_shutdown_cancellation_stays_prompt_without_sync_retry() -> None:
    """Process shutdown must still cancel active work without mislabeling it as transport recovery."""
    bot = _ResponseOwningBot()
    bot.start_responses(1)

    supervisor = asyncio.create_task(sync_forever_with_restart(bot))
    assert await bot.sync_starts.get() == 1
    supervisor.cancel()
    await supervisor
    assert bot.live_sync_count == 0
    assert bot.in_flight_response_count == 1

    await bot.prepare_for_sync_shutdown(shutdown_intent=ORDERLY_SHUTDOWN)

    assert bot.in_flight_response_count == 0
    assert bot.response_completions == 0
    assert bot.response_cancel_sources == ["interrupted"]
    assert bot.prepare_for_sync_shutdown_cancel_messages == [None]


@pytest.mark.asyncio
async def test_process_shutdown_signals_responses_before_coalescing_drain() -> None:
    """Busy response cleanup starts while the shared shutdown budget is still available."""
    runner = ResponseRunner(deps=MagicMock())
    response_started = asyncio.Event()

    async def response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    response_task = runner.track_inbox_response(
        response(),
        name="test_early_process_shutdown_response",
        recovery_proof_ready=lambda: True,
    )
    await asyncio.wait_for(response_started.wait(), timeout=1.0)
    cancellation_seen_at_coalescing: list[bool] = []
    drain_result = SimpleNamespace(
        completed=True,
        released_reservation_count=0,
        cancelled_unready_count=0,
        failed_ready_count=0,
        dropped_ready_count=0,
        dispatch_failure_count=0,
        dispatch_cancelled_count=0,
        admission_deferred_count=1,
    )

    async def drain_coalescing(**_kwargs: object) -> SimpleNamespace:
        cancellation_seen_at_coalescing.append(response_task.cancelling() > 0)
        return drain_result

    bot = object.__new__(AgentBot)
    bot.agent_user = AgentMatrixUser(
        agent_name="busy",
        user_id="@mindroom_busy:localhost",
        display_name="Busy",
        password=TEST_PASSWORD,
    )
    bot._runtime_view = MagicMock(client=None)
    bot._sync_shutting_down = False
    bot._sync_shutdown_budget = None
    bot._delivery_recovery_wake = MagicMock()
    bot._response_runner = runner
    bot._coalescing_gate = MagicMock(drain_all=AsyncMock(side_effect=drain_coalescing))
    bot.logger = MagicMock()

    with patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(return_value=True)):
        await bot.prepare_for_sync_shutdown(shutdown_intent=ORDERLY_SHUTDOWN)

    assert cancellation_seen_at_coalescing == [True]
    assert response_task.cancelled()
    assert (
        call(
            "coalescing_admission_deferred_for_durable_recovery",
            agent_name="busy",
            admission_deferred_count=1,
        )
        in bot.logger.info.call_args_list
    )


@pytest.mark.asyncio
async def test_process_shutdown_fences_matrix_transport_before_response_drain() -> None:
    """Orderly owner drain cannot start another Matrix request."""
    request_count = 0
    session_closed = False

    class ProbeSession:
        async def request(self, *_args: object, **_kwargs: object) -> object:
            nonlocal request_count
            request_count += 1
            return SimpleNamespace(status=200)

        async def close(self) -> None:
            nonlocal session_closed
            session_closed = True

    client = MindRoomAsyncClient("https://example.org", "@mindroom_busy:example.org")
    client.client_session = ProbeSession()  # type: ignore[assignment]
    drain_result = SimpleNamespace(
        completed=True,
        released_reservation_count=0,
        cancelled_unready_count=0,
        failed_ready_count=0,
        dropped_ready_count=0,
        dispatch_failure_count=0,
        dispatch_cancelled_count=0,
        admission_deferred_count=0,
    )

    async def drain_coalescing(**_kwargs: object) -> SimpleNamespace:
        with pytest.raises(
            RuntimeError,
            match="transport is fenced for process shutdown",
        ):
            await client.send("GET", "/_matrix/client/v3/account/whoami")
        return drain_result

    bot = object.__new__(AgentBot)
    bot.agent_user = AgentMatrixUser(
        agent_name="busy",
        user_id="@mindroom_busy:localhost",
        display_name="Busy",
        password=TEST_PASSWORD,
    )
    bot._runtime_view = MagicMock(client=client)
    bot._sync_shutting_down = False
    bot._sync_shutdown_budget = None
    bot._delivery_recovery_wake = MagicMock()
    bot._response_runner = ResponseRunner(deps=MagicMock())
    bot._coalescing_gate = MagicMock(drain_all=AsyncMock(side_effect=drain_coalescing))
    bot.logger = MagicMock()

    try:
        with patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(return_value=True)):
            await bot.prepare_for_sync_shutdown(shutdown_intent=ORDERLY_SHUTDOWN)
    finally:
        await client.close()

    assert session_closed is True
    assert request_count == 0


@pytest.mark.asyncio
async def test_cancellation_during_replacement_leaves_no_sync_or_response_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation after one replacement starts must settle every owned task."""
    bot = _ResponseOwningBot()
    bot.start_responses(2)
    _install_deterministic_stalls(monkeypatch, stall_count=1)

    supervisor = asyncio.create_task(sync_forever_with_restart(bot, max_retries=3))
    assert await bot.sync_starts.get() == 1
    assert await bot.sync_starts.get() == 2
    supervisor.cancel()
    await supervisor

    assert bot.live_sync_count == 0
    assert bot.in_flight_response_count == 2
    await bot.prepare_for_sync_shutdown()

    assert bot.in_flight_response_count == 0
    assert all(task.done() for task in bot.response_tasks)
    assert bot.response_cancel_sources == ["interrupted", "interrupted"]
    assert not [
        task
        for task in asyncio.all_tasks()
        if not task.done() and task.get_name() in {"matrix_sync_test_agent", "matrix_sync_watchdog_test_agent"}
    ]


@pytest.mark.asyncio
async def test_replacement_start_failure_is_visible_bounded_and_preserves_response_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed replacement start must exhaust visibly without duplicating response ownership."""
    bot = _ResponseOwningBot()
    bot.start_responses(1)
    start_calls = 0
    cleanup_calls = 0

    class StalledIteration:
        async def wait(self) -> None:
            msg = "Matrix sync loop stalled"
            raise _MatrixSyncStalledError(msg)

        async def cancel(self, *, shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN) -> None:
            nonlocal cleanup_calls
            assert shutdown_intent == GENERIC_SHUTDOWN
            cleanup_calls += 1

    def start_iteration(_bot: _ResponseOwningBot) -> StalledIteration:
        nonlocal start_calls
        start_calls += 1
        if start_calls == 1:
            return StalledIteration()
        msg = "replacement sync start failed"
        raise RuntimeError(msg)

    logger = MagicMock()
    monkeypatch.setattr(_SyncIteration, "start", start_iteration)
    monkeypatch.setattr(runtime_helpers, "logger", logger)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", lambda: 0.0)

    await sync_forever_with_restart(bot, max_retries=2)

    assert start_calls == 2
    assert cleanup_calls == 1
    assert bot.prepare_for_sync_shutdown_calls == 0
    assert bot.in_flight_response_count == 1
    assert not bot.response_tasks[0].done()
    assert bot.response_cancel_sources == []
    logger.exception.assert_any_call(
        "sync_loop_failed",
        agent="test_agent",
        retry_count=2,
    )
    logger.error.assert_any_call(
        "sync_loop_retries_exhausted",
        agent="test_agent",
        retry_count=2,
        max_retries=2,
        restart_reason_category="sync_failure",
    )

    bot.response_finish.set()
    await asyncio.gather(*bot.response_tasks)
    assert bot.response_completions == 1


@pytest.mark.asyncio
async def test_stalled_restart_waits_with_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """A watchdog-driven restart must add jitter so stalled loops don't restart as one herd."""
    bot = _FakeBot()
    original_mark = bot.mark_sync_loop_started

    def arm_and_mark() -> None:
        original_mark()
        bot._last_sync_monotonic = time.monotonic()

    bot.mark_sync_loop_started = arm_and_mark
    original_sync = bot.sync_forever

    async def sync_then_stop() -> None:
        if bot.sync_calls > 0:
            bot.running = False
            return
        await original_sync()

    bot.sync_forever = sync_then_stop

    jitter_calls: list[float] = []

    def fake_jitter() -> float:
        jitter_calls.append(0.0)
        return 0.0

    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", fake_jitter)

    await sync_forever_with_restart(bot, max_retries=2)

    assert len(jitter_calls) == 1


@pytest.mark.asyncio
async def test_failed_restart_does_not_add_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ordinary sync failures keep the plain backoff without stall jitter."""
    bot = _FakeBot()

    async def fail_once() -> None:
        bot.sync_calls += 1
        if bot.sync_calls > 1:
            bot.running = False
            return
        msg = "deliberate test error"
        raise RuntimeError(msg)

    bot.sync_forever = fail_once

    jitter_calls: list[float] = []

    def fake_jitter() -> float:
        jitter_calls.append(0.0)
        return 0.0

    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_STARTUP_GRACE_SECONDS", 5.0)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", fake_jitter)

    await sync_forever_with_restart(bot, max_retries=2)

    assert jitter_calls == []
    assert bot.prepare_for_sync_shutdown_cancel_messages == [None]


def test_stalled_restart_jitter_spreads_restarts() -> None:
    """Many stalled loops must restart over a spread window, not in one tick."""
    delays = [runtime_helpers._stalled_restart_jitter_seconds() for _ in range(27)]
    assert all(0.0 <= delay <= 10.0 for delay in delays)
    assert max(delays) - min(delays) > 1.0


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
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", lambda: 0.0)

    bot.sync_forever = sync_then_stop

    await sync_forever_with_restart(bot, max_retries=2)

    assert watch_calls == 2
    assert bot.first_call_cancelled is True
    assert bot.first_call_cancel_args == (SYNC_RESTART_CANCEL_MSG,)
    assert bot.sync_calls == 1
    assert bot.prepare_for_sync_shutdown_calls == 1


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
async def test_classify_cancel_source_unknown_returns_interrupted() -> None:
    """Untagged cancellations should surface as generic interruptions."""
    assert classify_cancel_source(asyncio.CancelledError()) == "interrupted"


@pytest.mark.asyncio
async def test_cancel_failure_reason_matches_cancel_source() -> None:
    """Failure reasons should stay aligned with the shared cancel provenance mapping."""
    assert cancel_failure_reason("user_stop") == "cancelled_by_user"
    assert cancel_failure_reason("sync_restart") == "sync_restart_cancelled"
    assert cancel_failure_reason("interrupted") == "interrupted"


def test_cancel_message_for_source() -> None:
    """Task-cancel sources should map to canonical asyncio cancel messages."""
    assert cancel_message_for_source("sync_restart") == SYNC_RESTART_CANCEL_MSG
    assert cancel_message_for_source("user_stop") == USER_STOP_CANCEL_MSG
    assert cancel_message_for_source(None) is None


@pytest.mark.parametrize(
    ("failure_reason", "expected_cancel_source"),
    [
        ("cancelled_by_user", "user_stop"),
        ("sync_restart_cancelled", "sync_restart"),
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


def test_shutdown_intent_for_restarted_entity() -> None:
    """Restarted entities should use sync-restart cancellation provenance."""
    intent = shutdown_intent_for_entity("agent1", restart_entities={"agent1", "agent2"})

    assert intent == SYNC_RESTART_SHUTDOWN
    assert intent.stop_reason == "restart"
    assert intent.cancel_source == "sync_restart"


def test_shutdown_intent_for_removed_entity() -> None:
    """Removed entities should not look like sync restarts."""
    intent = shutdown_intent_for_entity("removed", restart_entities={"agent1"})

    assert intent == ENTITY_REMOVED_SHUTDOWN
    assert intent.stop_reason == "entity_removed"
    assert intent.cancel_source is None


def test_generic_shutdown_has_no_restart_provenance() -> None:
    """Generic shutdown should not carry a stop reason or cancellation source."""
    assert RuntimeShutdownIntent(stop_reason=None, cancel_source=None) == GENERIC_SHUTDOWN


def test_orderly_shutdown_preserves_public_stop_reason() -> None:
    """Orderly process shutdown should keep lifecycle hook metadata without cancellation provenance."""
    assert RuntimeShutdownIntent(stop_reason="shutdown", cancel_source=None) == ORDERLY_SHUTDOWN


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
async def test_sync_forever_with_restart_preserves_runtime_before_retry_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receive-loop restart must not tear down response runtime before backoff."""
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

    async def prepare_for_sync_shutdown(**_kwargs: object) -> None:
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

    assert call_order == ["retry_delay", "prepare"]


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


def test_sync_cache_write_progress_registry_clears_after_failure() -> None:
    """A failed durable phase must not leave watchdog and health exempt forever."""
    reset_matrix_sync_health()
    try:
        with suppress(RuntimeError), _track_matrix_sync_cache_write("failed_agent"):
            msg = "cache write failed"
            raise RuntimeError(msg)

        assert get_matrix_sync_cache_write_progress("failed_agent") is None
    finally:
        reset_matrix_sync_health()


@pytest.mark.parametrize("raw", ["not-a-number", "nan", "inf", "-inf", "0", "-1"])
def test_sync_cache_write_grace_rejects_non_finite_or_non_positive(raw: str) -> None:
    """An invalid grace must not disable the bounded backstop."""
    with pytest.raises(ValueError, match="must be a finite positive number"):
        matrix_sync_cache_write_grace_seconds(
            _fake_runtime_paths(MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS=raw),
        )


@pytest.mark.parametrize("grace_seconds", [math.nan, math.inf, -math.inf, 0.0, -1.0])
def test_health_rejects_invalid_cache_write_grace(grace_seconds: float) -> None:
    """Every health caller must preserve the finite cache-write backstop."""
    with pytest.raises(ValueError, match="cache_write_grace_seconds must be a finite positive number"):
        get_matrix_sync_health_snapshot(cache_write_grace_seconds=grace_seconds)


def test_health_reports_shared_cache_write_progress_past_grace() -> None:
    """Health must stop excusing a durable phase after the shared grace expires."""
    recent_sync_time = datetime.now(UTC) - timedelta(seconds=10)
    reset_matrix_sync_health()
    try:
        mark_matrix_sync_loop_started("wedged_agent")
        mark_matrix_sync_success("wedged_agent", recent_sync_time)
        with _track_matrix_sync_cache_write("wedged_agent"):
            progress = get_matrix_sync_cache_write_progress("wedged_agent")
            assert progress is not None

            snapshot = get_matrix_sync_health_snapshot(
                cache_write_grace_seconds=5.0,
                now_monotonic=progress.started_monotonic + 6.0,
            )

        assert snapshot.stale_entities == ("wedged_agent",)
    finally:
        reset_matrix_sync_health()


def test_health_defers_only_bounded_recent_owned_ingestion_progress() -> None:
    """Committed Work progress keeps health live without granting immunity."""
    stale_sync_time = datetime.now(UTC) - timedelta(seconds=300)
    reset_matrix_sync_health()
    try:
        mark_matrix_sync_loop_started("draining_agent")
        mark_matrix_sync_success("draining_agent", stale_sync_time)
        mark_matrix_ingestion_progress("draining_agent", now_monotonic=100.0)
        mark_matrix_ingestion_progress("draining_agent", now_monotonic=102.0)

        healthy = get_matrix_sync_health_snapshot(
            cache_write_grace_seconds=5.0,
            now_monotonic=103.0,
        )
        past_grace = get_matrix_sync_health_snapshot(
            cache_write_grace_seconds=5.0,
            now_monotonic=106.0,
        )

        assert healthy.stale_entities == ()
        assert past_grace.stale_entities == ("draining_agent",)
    finally:
        reset_matrix_sync_health()


@pytest.mark.asyncio
async def test_watchdog_defers_to_shared_cache_write_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow durable phase must outlive the ordinary transport timeout."""
    bot = _FakeBot(MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS="1")
    cache_write_finished = asyncio.Event()

    async def sync_with_slow_cache_write() -> None:
        bot.sync_calls += 1
        bot._last_sync_monotonic = time.monotonic()
        try:
            with _track_matrix_sync_cache_write(bot.agent_name):
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            bot.first_call_cancelled = True
            raise
        cache_write_finished.set()
        bot.running = False

    bot.sync_forever = sync_with_slow_cache_write
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.005)

    reset_matrix_sync_health()
    try:
        await sync_forever_with_restart(bot, max_retries=1)
    finally:
        reset_matrix_sync_health()

    assert cache_write_finished.is_set()
    assert bot.first_call_cancelled is False
    assert bot.sync_calls == 1


@pytest.mark.asyncio
async def test_watchdog_defers_while_owned_ingestion_commits_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blocked Frame may drain Work longer than the ordinary sync timeout."""
    bot = _FakeBot(MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS="1")
    progress_finished = asyncio.Event()

    async def sync_with_durable_progress() -> None:
        bot.sync_calls += 1
        bot._last_sync_monotonic = time.monotonic()
        bot._durable_ingestion_progress_generation = 0
        try:
            for generation in range(1, 11):
                await asyncio.sleep(0.01)
                bot._durable_ingestion_progress_generation = generation
        except asyncio.CancelledError:
            bot.first_call_cancelled = True
            raise
        progress_finished.set()
        bot.running = False

    bot.sync_forever = sync_with_durable_progress
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.005)

    reset_matrix_sync_health()
    try:
        await sync_forever_with_restart(bot, max_retries=1)
    finally:
        reset_matrix_sync_health()

    assert progress_finished.is_set()
    assert bot.first_call_cancelled is False
    assert bot.sync_calls == 1


@pytest.mark.asyncio
async def test_watchdog_cancels_owned_ingestion_progress_past_finite_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continuous commits cannot exempt an incomplete Frame forever."""
    bot = _FakeBot(MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS="0.04")

    async def sync_with_unbounded_durable_progress() -> None:
        bot.sync_calls += 1
        bot._last_sync_monotonic = time.monotonic()
        bot._durable_ingestion_progress_generation = 0
        generation = 0
        try:
            while True:
                await asyncio.sleep(0.005)
                generation += 1
                bot._durable_ingestion_progress_generation = generation
        except asyncio.CancelledError:
            bot.first_call_cancelled = True
            raise

    bot.sync_forever = sync_with_unbounded_durable_progress
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", lambda: 0.0)

    reset_matrix_sync_health()
    try:
        await sync_forever_with_restart(bot, max_retries=1)
    finally:
        reset_matrix_sync_health()

    assert bot.first_call_cancelled is True
    assert bot.sync_calls == 1


@pytest.mark.asyncio
async def test_watchdog_cancels_shared_cache_write_past_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged durable phase must still be cancelled after its finite grace."""
    bot = _FakeBot(MINDROOM_MATRIX_SYNC_CACHE_WRITE_GRACE_SECONDS="0.04")

    async def sync_with_wedged_cache_write() -> None:
        bot.sync_calls += 1
        bot._last_sync_monotonic = time.monotonic()
        try:
            with _track_matrix_sync_cache_write(bot.agent_name):
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            bot.first_call_cancelled = True
            raise

    bot.sync_forever = sync_with_wedged_cache_write
    monkeypatch.setattr(runtime_helpers, "MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runtime_helpers, "_MATRIX_SYNC_WATCHDOG_POLL_INTERVAL_SECONDS", 0.005)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", lambda: 0.0)

    reset_matrix_sync_health()
    try:
        with capture_logs() as logs:
            await sync_forever_with_restart(bot, max_retries=1)
    finally:
        reset_matrix_sync_health()

    assert bot.first_call_cancelled is True
    stall_logs = [entry for entry in logs if entry["event"] == "matrix_sync_watchdog_stalled"]
    assert [entry["restart_reason_category"] for entry in stall_logs] == ["cache_write_grace_exhausted"]


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
async def test_sync_iteration_cancel_preserves_generic_shutdown_source() -> None:
    """Generic sync cleanup must not relabel child callbacks as sync-restart cancellations."""
    bot = _FakeBot()
    cancel_args: list[tuple[object, ...]] = []

    async def blocked_sync() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            cancel_args.append(exc.args)
            raise

    task = asyncio.create_task(blocked_sync())
    await asyncio.sleep(0)

    await _SyncIteration(bot=bot, sync_task=task, watchdog_task=None).cancel(shutdown_intent=GENERIC_SHUTDOWN)

    assert cancel_args == [()]


@pytest.mark.asyncio
async def test_sync_iteration_cancel_preserves_restart_shutdown_source() -> None:
    """Restart cleanup should still mark child sync callbacks with restart provenance."""
    bot = _FakeBot()
    cancel_args: list[tuple[object, ...]] = []

    async def blocked_sync() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            cancel_args.append(exc.args)
            raise

    task = asyncio.create_task(blocked_sync())
    await asyncio.sleep(0)

    await _SyncIteration(bot=bot, sync_task=task, watchdog_task=None).cancel(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    assert cancel_args == [(SYNC_RESTART_CANCEL_MSG,)]


@pytest.mark.asyncio
async def test_ingestion_frame_completion_marks_sync_success(tmp_path: Path) -> None:
    """A completed durable frame must feed the watchdog clock and first-sync lifecycle."""
    bot = _sliding_response_bot(tmp_path)
    bot._first_sync_done = False

    with patch.object(
        bot,
        "_run_sync_response_side_effects",
        new=AsyncMock(),
    ):
        await _complete_frame(bot)

    assert bot.last_sync_time is not None
    assert bot._first_sync_done is True


@pytest.mark.asyncio
async def test_delivery_recovery_asks_the_outbox_on_every_sync_response(
    tmp_path: Path,
) -> None:
    """Owed answers must not wait for a restart, whatever left them owed.

    The first sync response can arrive while a room is still unrecovered, and
    nio refuses ordinary sends into one. A pass can also raise before it
    reports anything, and a live send can be refused long after the first
    sync. Each of those leaves a row unacknowledged, and a flag armed by only
    some of them loses the answers the others produced.
    """
    bot = _sliding_response_bot(tmp_path)
    bot._first_sync_done = False
    outcomes = [
        RuntimeError("the first pass never reported"),
        RecoveryOutcome(recovered=0, failed=1),
        RecoveryOutcome(recovered=1, failed=0),
        RecoveryOutcome(recovered=0, failed=0),
    ]
    recover = AsyncMock(side_effect=outcomes)

    with (
        patch.object(bot, "_delivery_gateway", new=SimpleNamespace(recover_deliveries=recover)),
        patch.object(bot, "_emit_agent_lifecycle_event", new=AsyncMock()),
        patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
        patch("mindroom.bot._DELIVERY_RECOVERY_RETRY_INITIAL_DELAY_SECONDS", 0.0),
        patch("mindroom.bot._DELIVERY_RECOVERY_RETRY_MAX_DELAY_SECONDS", 0.0),
    ):
        # A pass that raises or reports an incomplete recovery retries through
        # the durable outbox before releasing the owner task.
        await bot._run_sync_response_side_effects(first_sync_response=True)
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
        # And a delivery refused after every earlier pass completed is still
        # found, because the outbox is asked rather than a flag.
        await bot._run_sync_response_side_effects(first_sync_response=False)
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert recover.await_count == len(outcomes)


@pytest.mark.asyncio
async def test_sync_response_returns_while_delivery_recovery_waits_for_the_next_response(
    tmp_path: Path,
) -> None:
    """A parked recovery pass cannot block the next receive-loop callback."""
    bot = _sliding_response_bot(tmp_path)
    recovery_started = asyncio.Event()
    next_response_started = asyncio.Event()
    first_response_returned = asyncio.Event()

    async def recover_deliveries() -> RecoveryOutcome:
        recovery_started.set()
        await next_response_started.wait()
        return RecoveryOutcome(recovered=0, failed=0)

    async def drive_responses() -> None:
        await _complete_frame(bot)
        first_response_returned.set()
        next_response_started.set()
        await _complete_frame(bot, 1)

    with patch.object(
        bot,
        "_delivery_gateway",
        new=SimpleNamespace(recover_deliveries=recover_deliveries),
    ):
        driver = asyncio.create_task(drive_responses())
        try:
            await recovery_started.wait()
            await asyncio.sleep(0)
            assert first_response_returned.is_set()
        finally:
            next_response_started.set()
            await driver
            assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)


@pytest.mark.asyncio
async def test_delivery_recovery_coalesces_sync_wakes_without_overlapping_passes(
    tmp_path: Path,
) -> None:
    """Several responses during one recovery pass request one follow-up pass."""
    bot = _sliding_response_bot(tmp_path)
    first_pass_started = asyncio.Event()
    allow_first_pass_finish = asyncio.Event()
    second_pass_started = asyncio.Event()
    pass_count = 0
    active_count = 0
    max_active_count = 0

    async def recover_deliveries() -> RecoveryOutcome:
        nonlocal active_count, max_active_count, pass_count
        pass_count += 1
        active_count += 1
        max_active_count = max(max_active_count, active_count)
        try:
            if pass_count == 1:
                first_pass_started.set()
                await allow_first_pass_finish.wait()
            else:
                second_pass_started.set()
            return RecoveryOutcome(recovered=0, failed=0)
        finally:
            active_count -= 1

    with patch.object(
        bot,
        "_delivery_gateway",
        new=SimpleNamespace(recover_deliveries=recover_deliveries),
    ):
        await _complete_frame(bot)
        await first_pass_started.wait()
        await _complete_frame(bot, 1)
        await _complete_frame(bot, 2)

        allow_first_pass_finish.set()
        await second_pass_started.wait()
        assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert pass_count == 2
    assert max_active_count == 1


@pytest.mark.asyncio
async def test_sync_shutdown_cancels_the_owned_delivery_recovery(tmp_path: Path) -> None:
    """The existing owner drain cancels a parked delivery-recovery task."""
    bot = _sliding_response_bot(tmp_path)
    recovery_started = asyncio.Event()
    recovery_cancelled = asyncio.Event()

    async def recover_deliveries() -> RecoveryOutcome:
        recovery_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            recovery_cancelled.set()
            raise

    async def expire_owner_drain(
        *,
        timeout: float | None = None,  # noqa: ASYNC109
        owner: object | None = None,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> bool:
        assert timeout is not None
        assert 0.0 < timeout <= SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS
        return await wait_for_background_tasks(
            timeout=0,
            owner=owner,
            shutdown_intent=shutdown_intent,
        )

    with (
        patch.object(
            bot,
            "_delivery_gateway",
            new=SimpleNamespace(recover_deliveries=recover_deliveries),
        ),
        patch("mindroom.bot.wait_for_background_tasks", new=expire_owner_drain),
    ):
        await _complete_frame(bot)
        await recovery_started.wait()
        await bot.prepare_for_sync_shutdown()

    assert recovery_cancelled.is_set()


def test_shutdown_budget_shares_one_deadline_across_two_window_drain() -> None:
    """Sequential shutdown stages consume one policy budget."""
    with patch(
        "mindroom.runtime_shutdown.time.monotonic",
        side_effect=(100.0, 102.0, 104.0, 105.0),
    ):
        budget = ShutdownBudget.start(5.0)

        assert budget.remaining_seconds() == 3.0
        assert budget.per_window_seconds(windows=2) == 0.5
        assert budget.remaining_seconds() == 0.0


@pytest.mark.asyncio
async def test_prepare_then_stop_reuses_one_total_shutdown_budget() -> None:
    """Busy cleanup stages and a later stop share one five-second deadline."""
    now = [100.0]
    background_timeouts: list[float | None] = []
    coalescing_timeouts: list[float | None] = []
    response_timeouts: list[float | None] = []

    async def wait_for_background(
        timeout: float | None = None,  # noqa: ASYNC109
        **_kwargs: object,
    ) -> bool:
        background_timeouts.append(timeout)
        if len(background_timeouts) == 1:
            now[0] += 2.0
            return True
        return False

    drain_result = SimpleNamespace(
        completed=True,
        released_reservation_count=0,
        cancelled_unready_count=0,
        failed_ready_count=0,
        dropped_ready_count=0,
        dispatch_failure_count=0,
        dispatch_cancelled_count=0,
        admission_deferred_count=0,
    )

    async def drain_coalescing(
        *,
        shutdown_budget: ShutdownBudget,
        **_kwargs: object,
    ) -> SimpleNamespace:
        coalescing_timeouts.append(shutdown_budget.remaining_seconds())
        if coalescing_timeouts[-1] > 0.0:
            now[0] += 2.0
        return drain_result

    async def drain_responses(
        *,
        cancel_after_seconds: float | None,
        **_kwargs: object,
    ) -> bool:
        response_timeouts.append(cancel_after_seconds)
        assert cancel_after_seconds is not None
        now[0] += cancel_after_seconds * 2
        return False

    bot = object.__new__(AgentBot)
    bot.agent_user = AgentMatrixUser(
        agent_name="budget",
        user_id="@mindroom_budget:localhost",
        display_name="Budget",
        password=TEST_PASSWORD,
    )
    bot._runtime_view = MagicMock(client=None)
    bot.running = True
    bot.last_sync_time = None
    bot._last_sync_monotonic = None
    bot._first_sync_done = True
    bot._orchestrator_ready_handled = True
    bot._sync_shutting_down = False
    bot._sync_shutdown_budget = None
    bot._delivery_recovery_wake = MagicMock()
    bot._response_runner = MagicMock(
        refuse_pending_admissions=MagicMock(),
        drain_inbox_responses=AsyncMock(side_effect=drain_responses),
        pending_inbox_response_count=0,
        incomplete_inbox_responses_recoverable=True,
        in_flight_response_count=0,
    )
    bot._coalescing_gate = MagicMock(
        drain_all=AsyncMock(side_effect=drain_coalescing),
    )
    bot._emit_agent_lifecycle_event = AsyncMock()
    bot._call_manager = None
    bot._journal_dispatcher = MagicMock(stop=AsyncMock())
    bot._ingestion_session = None
    bot._own_journal = None
    bot.logger = MagicMock()

    with (
        patch("mindroom.runtime_shutdown.time.monotonic", side_effect=lambda: now[0]),
        patch("mindroom.bot.wait_for_background_tasks", side_effect=wait_for_background),
    ):
        await bot.prepare_for_sync_shutdown()
        await bot.stop()

    assert background_timeouts == [SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS, 0.0, 0.0, 0.0]
    assert coalescing_timeouts == [3.0, 0.0]
    assert response_timeouts == [0.5, 0.0]


@pytest.mark.asyncio
async def test_stop_does_not_close_runtime_resources_under_live_response_owner() -> None:
    """A failed response drain cannot be followed by client/store teardown."""
    shutdown_failure = RuntimeError("response tasks did not stop within bounded cleanup")
    client = AsyncMock()
    journal_dispatcher = MagicMock(stop=AsyncMock())
    ingestion_session = MagicMock(close=AsyncMock())
    journal = MagicMock(close=AsyncMock())
    bot = object.__new__(AgentBot)
    bot.agent_user = AgentMatrixUser(
        agent_name="busy",
        user_id="@mindroom_busy:localhost",
        display_name="Busy",
        password=TEST_PASSWORD,
    )
    bot._runtime_view = MagicMock(client=client)
    bot.running = True
    bot.last_sync_time = None
    bot._last_sync_monotonic = None
    bot._first_sync_done = True
    bot._orchestrator_ready_handled = True
    bot._sync_shutting_down = False
    bot._emit_agent_lifecycle_event = AsyncMock()
    bot._call_manager = None
    bot._response_runner = MagicMock(pending_inbox_response_count=1)
    bot.prepare_for_sync_shutdown = AsyncMock(side_effect=shutdown_failure)
    bot._journal_dispatcher = journal_dispatcher
    bot._ingestion_session = ingestion_session
    bot._own_journal = journal
    bot.logger = MagicMock()

    with pytest.raises(RuntimeError, match="response tasks did not stop") as raised:
        await bot.stop(shutdown_intent=ORDERLY_SHUTDOWN)

    assert raised.value is shutdown_failure
    journal_dispatcher.stop.assert_not_awaited()
    ingestion_session.close.assert_not_awaited()
    journal.close.assert_not_awaited()
    client.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_orderly_stop_defers_saturated_response_timeouts_without_tracebacks() -> None:
    """Expected response handoffs must not synchronously format one traceback per bot."""
    bots: list[AgentBot] = []
    failures: list[ResponseShutdownTimeoutError] = []
    for index in range(16):
        failure = ResponseShutdownTimeoutError(
            "response tasks did not stop within bounded cleanup",
        )
        bot = object.__new__(AgentBot)
        bot.agent_user = AgentMatrixUser(
            agent_name=f"saturated_{index:02d}",
            user_id=f"@mindroom_saturated_{index:02d}:localhost",
            display_name=f"Saturated {index:02d}",
            password=TEST_PASSWORD,
        )
        bot._runtime_view = MagicMock(client=AsyncMock())
        bot.running = True
        bot.last_sync_time = None
        bot._last_sync_monotonic = None
        bot._first_sync_done = True
        bot._orchestrator_ready_handled = True
        bot._sync_shutting_down = False
        bot._emit_agent_lifecycle_event = AsyncMock()
        bot._call_manager = None
        bot._response_runner = MagicMock(pending_inbox_response_count=index % 3 + 1)
        bot.prepare_for_sync_shutdown = AsyncMock(side_effect=failure)
        bot._journal_dispatcher = MagicMock(stop=AsyncMock())
        bot._ingestion_session = MagicMock(close=AsyncMock())
        bot._own_journal = MagicMock(close=AsyncMock())
        bot.logger = MagicMock()
        bot.logger.exception.side_effect = AssertionError(
            "expected response timeout formatted a synchronous traceback",
        )
        bots.append(bot)
        failures.append(failure)

    results = await asyncio.gather(
        *(bot.stop(shutdown_intent=ORDERLY_SHUTDOWN) for bot in bots),
        return_exceptions=True,
    )

    assert results == failures
    for bot in bots:
        assert bot.deferred_stop_required
        bot.logger.exception.assert_not_called()
        bot.logger.warning.assert_called_once_with(
            "Deferred resource release after bounded response shutdown",
            error_type="ResponseShutdownTimeoutError",
            resource="sync shutdown preparation",
        )
        bot._journal_dispatcher.stop.assert_not_awaited()
        bot._ingestion_session.close.assert_not_awaited()
        bot._own_journal.close.assert_not_awaited()
        bot._runtime_view.client.close.assert_not_awaited()


def test_matrix_sync_change_restarts_existing_entities() -> None:
    """Changing matrix_sync must restart running bots so sync loops pick up the new transport."""
    plan = build_config_update_plan(
        current_config=Config(),
        new_config=Config(matrix_sync=MatrixSyncConfig(mode="sliding")),
        configured_entities={"router", "code"},
        existing_entities={"router", "code"},
        agent_bots={},
    )

    assert plan.entities_to_restart == {"router", "code"}


def test_sliding_sync_required_state_is_not_shared_between_requests() -> None:
    """Sliding sync request builders should not reuse mutable required_state lists."""
    lists = _sliding_sync_lists(timeline_limit=7)
    room_subscriptions = _sliding_sync_room_subscriptions(["!alpha:localhost", "!beta:localhost"], timeline_limit=7)

    list_required_state = lists["mindroom"]["required_state"]
    alpha_required_state = room_subscriptions["!alpha:localhost"]["required_state"]
    beta_required_state = room_subscriptions["!beta:localhost"]["required_state"]

    assert list_required_state == alpha_required_state == beta_required_state
    assert list_required_state is not alpha_required_state
    assert alpha_required_state is not beta_required_state
    alpha_required_state.append(["m.room.power_levels", ""])
    assert ["m.room.power_levels", ""] not in beta_required_state
    assert ["m.room.power_levels", ""] not in _sliding_sync_lists(timeline_limit=7)["mindroom"]["required_state"]


def _sliding_response_bot(tmp_path: Path) -> AgentBot:
    """Build one real bot for Sliding response lifecycle tests."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    rooms=["!room:localhost"],
                ),
            },
            models={
                "default": ModelConfig(
                    provider="test",
                    id="test-model",
                ),
            },
            matrix_sync=MatrixSyncConfig(mode="sliding"),
        ),
        runtime_paths,
    )
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="code",
            password=TEST_PASSWORD,
            display_name="Code",
            user_id="@mindroom_code:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot._first_sync_done = True
    return bot


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
    await stop_entities(entities_to_restart, agent_bots, sync_tasks, restart_entities=entities_to_restart)

    assert task1.cancelled()
    assert task2.cancelled()
    assert not task3.cancelled()

    mock_bot1.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    mock_bot2.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    mock_bot1.stop.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    mock_bot2.stop.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    assert "agent1" not in agent_bots
    assert "agent2" not in agent_bots
    assert "agent3" in agent_bots

    assert "agent1" not in sync_tasks
    assert "agent2" not in sync_tasks
    assert "agent3" in sync_tasks

    task3.cancel()
    await asyncio.gather(task3, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_entities_uses_generic_shutdown_for_removed_entities() -> None:
    """Removed entities must not enqueue sync-restart resume work."""
    restart_bot = AsyncMock()
    restart_bot.prepare_for_sync_shutdown = AsyncMock()
    restart_bot.stop = AsyncMock()
    removed_bot = AsyncMock()
    removed_bot.prepare_for_sync_shutdown = AsyncMock()
    removed_bot.stop = AsyncMock()
    agent_bots = {"restart": restart_bot, "removed": removed_bot}
    sync_tasks = {
        "restart": asyncio.create_task(asyncio.sleep(60)),
        "removed": asyncio.create_task(asyncio.sleep(60)),
    }
    shutdown_intents: list[tuple[str, RuntimeShutdownIntent]] = []

    async def fake_cancel_sync_task(
        entity_name: str,
        _sync_tasks: dict[str, asyncio.Task],
        *,
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> None:
        shutdown_intents.append((entity_name, shutdown_intent))
        task = _sync_tasks.pop(entity_name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with patch("mindroom.orchestration.runtime.cancel_sync_task", side_effect=fake_cancel_sync_task):
        await stop_entities({"restart", "removed"}, agent_bots, sync_tasks, restart_entities={"restart"})

    assert sorted(shutdown_intents) == [
        ("removed", ENTITY_REMOVED_SHUTDOWN),
        ("restart", SYNC_RESTART_SHUTDOWN),
    ]
    removed_bot.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)
    removed_bot.stop.assert_awaited_once_with(shutdown_intent=ENTITY_REMOVED_SHUTDOWN)
    restart_bot.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    restart_bot.stop.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)


@pytest.mark.asyncio
async def test_agent_bot_stop_preserves_restart_shutdown_intent() -> None:
    """AgentBot.stop() must keep restart provenance for final drains."""
    bot = object.__new__(AgentBot)
    bot.agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@mindroom_test_agent:localhost",
        display_name="Test Agent",
        password=TEST_PASSWORD,
    )
    bot._runtime_view = BotRuntimeState(
        client=None,
        config=MagicMock(spec=Config),
        runtime_paths=_fake_runtime_paths(),
        enable_streaming=True,
        orchestrator=None,
    )
    # This bot is hand-built with object.__new__, so it only has what the test
    # sets. stop() releases the journal lane, which a real bot always has.
    bot._journal_dispatcher = MagicMock(stop=AsyncMock())
    bot._journal_store = MagicMock(close=AsyncMock())
    # Owned rather than borrowed, so stop() closes it -- which is what this
    # test's shutdown-intent assertions run through.
    bot._own_journal = MagicMock(close=AsyncMock())
    bot._ingestion_session = None
    bot.storage_path = Path("/nonexistent/storage")
    bot.logger = MagicMock()
    bot.prepare_for_sync_shutdown = AsyncMock()
    bot._response_runner = MagicMock(pending_inbox_response_count=0)
    bot._emit_agent_lifecycle_event = AsyncMock()
    bot._call_manager = None

    await AgentBot.stop(bot, shutdown_intent=SYNC_RESTART_SHUTDOWN)

    bot._emit_agent_lifecycle_event.assert_awaited_once_with("agent:stopped", stop_reason="restart")
    bot.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)


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
        stop_entities(
            {"agent1"},
            {"agent1": bot},
            {"agent1": supervisor_task},
            restart_entities={"agent1"},
        ),
        timeout=2.0,
    )
    elapsed = time.monotonic() - started_at

    assert elapsed <= 2.0
    assert supervisor_task.done()
    assert bot.prepare_for_sync_shutdown_calls == 1
    assert bot.prepare_for_sync_shutdown_cancel_messages == ["sync_restart"]
    bot.stop.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)


@pytest.mark.asyncio
async def test_stop_entities_cancels_sync_tasks_before_checkpoint_shutdown() -> None:
    """Restart teardown should stop sync callbacks before checkpoint drain can certify."""
    call_order: list[tuple[str, str]] = []
    shutdown_intents: list[tuple[str, RuntimeShutdownIntent]] = []

    mock_bot1 = AsyncMock()
    mock_bot1._quiesce_matrix_ingestion = AsyncMock(
        side_effect=lambda: call_order.append(("quiesce", "agent1")),
    )
    mock_bot1.prepare_for_sync_shutdown = AsyncMock(
        side_effect=lambda **_kwargs: call_order.append(("prepare", "agent1")),
    )
    mock_bot1.stop = AsyncMock(side_effect=lambda **_: call_order.append(("stop", "agent1")))

    mock_bot2 = AsyncMock()
    mock_bot2._quiesce_matrix_ingestion = AsyncMock(
        side_effect=lambda: call_order.append(("quiesce", "agent2")),
    )
    mock_bot2.prepare_for_sync_shutdown = AsyncMock(
        side_effect=lambda **_kwargs: call_order.append(("prepare", "agent2")),
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
        shutdown_intent: RuntimeShutdownIntent = GENERIC_SHUTDOWN,
    ) -> None:
        call_order.append(("cancel", entity_name))
        shutdown_intents.append((entity_name, shutdown_intent))
        task = _sync_tasks.pop(entity_name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    with patch("mindroom.orchestration.runtime.cancel_sync_task", side_effect=fake_cancel_sync_task):
        await stop_entities({"agent1", "agent2"}, agent_bots, sync_tasks, restart_entities={"agent1", "agent2"})

    quiesce_indexes = [index for index, item in enumerate(call_order) if item[0] == "quiesce"]
    cancel_indexes = [index for index, item in enumerate(call_order) if item[0] == "cancel"]
    prepare_indexes = [index for index, item in enumerate(call_order) if item[0] == "prepare"]

    assert quiesce_indexes
    assert prepare_indexes
    assert cancel_indexes
    assert max(quiesce_indexes) < min(cancel_indexes)
    assert max(cancel_indexes) < min(prepare_indexes)
    assert sorted(shutdown_intents) == [
        ("agent1", SYNC_RESTART_SHUTDOWN),
        ("agent2", SYNC_RESTART_SHUTDOWN),
    ]
    mock_bot1.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    mock_bot2.prepare_for_sync_shutdown.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)


@pytest.mark.asyncio
async def test_stop_entities_quiesces_all_sources_concurrently() -> None:
    """One slow source barrier must not prevent another from starting."""
    entered: set[str] = set()
    both_entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_quiesce(entity_name: str) -> None:
        entered.add(entity_name)
        if entered == {"agent1", "agent2"}:
            both_entered.set()
        await release.wait()

    agent_bots: dict[str, AsyncMock] = {}
    for entity_name in ("agent1", "agent2"):
        bot = AsyncMock()

        async def named_quiesce(name: str = entity_name) -> None:
            await hold_quiesce(name)

        bot._quiesce_matrix_ingestion = AsyncMock(side_effect=named_quiesce)
        bot.prepare_for_sync_shutdown = AsyncMock()
        bot.stop = AsyncMock()
        agent_bots[entity_name] = bot
    sync_tasks = {entity_name: asyncio.create_task(asyncio.sleep(60)) for entity_name in agent_bots}
    stopping = asyncio.create_task(
        stop_entities(
            set(agent_bots),
            agent_bots,
            sync_tasks,
            restart_entities={"agent1", "agent2"},
        ),
    )

    try:
        await asyncio.wait_for(both_entered.wait(), timeout=2)
        release.set()
        await asyncio.wait_for(stopping, timeout=2)
    finally:
        release.set()
        if not stopping.done():
            stopping.cancel()
        await asyncio.gather(stopping, return_exceptions=True)
        for task in sync_tasks.values():
            task.cancel()
        await asyncio.gather(*sync_tasks.values(), return_exceptions=True)

    assert entered == {"agent1", "agent2"}


@pytest.mark.asyncio
async def test_stop_entities_finishes_cleanup_after_cancellation_during_quiesce() -> None:
    """Caller cancellation is reported only after durable ownership is released."""
    quiesce_entered = asyncio.Event()
    release_quiesce = asyncio.Event()
    bot = AsyncMock()

    async def hold_quiesce() -> None:
        quiesce_entered.set()
        await release_quiesce.wait()

    bot._quiesce_matrix_ingestion = AsyncMock(side_effect=hold_quiesce)
    bot.prepare_for_sync_shutdown = AsyncMock()
    bot.stop = AsyncMock()
    sync_task = asyncio.create_task(asyncio.sleep(60))
    agent_bots = {"agent1": bot}
    sync_tasks = {"agent1": sync_task}
    stopping = asyncio.create_task(
        stop_entities(
            {"agent1"},
            agent_bots,
            sync_tasks,
            restart_entities={"agent1"},
        ),
    )

    await asyncio.wait_for(quiesce_entered.wait(), timeout=2)
    stopping.cancel()
    await asyncio.sleep(0)
    release_quiesce.set()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert sync_task.cancelled()
    bot.prepare_for_sync_shutdown.assert_awaited_once()
    bot.stop.assert_awaited_once()
    assert agent_bots == {"agent1": bot}
    assert sync_tasks == {}


@pytest.mark.asyncio
async def test_stop_entities_cleans_up_before_reporting_source_quiesce_failure() -> None:
    """A failed source barrier must not strand live sync/store ownership."""
    failure = RuntimeError("source quiesce failed")
    bot = AsyncMock()
    bot._quiesce_matrix_ingestion = AsyncMock(side_effect=failure)
    bot.prepare_for_sync_shutdown = AsyncMock()
    bot.stop = AsyncMock()
    sync_task = asyncio.create_task(asyncio.sleep(60))
    agent_bots = {"agent1": bot}
    sync_tasks = {"agent1": sync_task}

    with pytest.raises(RuntimeError, match="source quiesce failed") as raised:
        await stop_entities(
            {"agent1"},
            agent_bots,
            sync_tasks,
            restart_entities={"agent1"},
        )

    assert raised.value is failure
    assert sync_task.cancelled()
    bot.prepare_for_sync_shutdown.assert_awaited_once_with(
        shutdown_intent=SYNC_RESTART_SHUTDOWN,
    )
    bot.stop.assert_awaited_once_with(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    assert agent_bots == {"agent1": bot}
    assert sync_tasks == {}


@pytest.mark.asyncio
async def test_stop_entities_prioritizes_quiesce_failure_after_cleanup_failures() -> None:
    """Every cleanup stage runs while the source-barrier error stays primary."""
    quiesce_failure = RuntimeError("source quiesce failed")
    cleanup_failure = RuntimeError("cleanup failed")
    bot = AsyncMock()
    bot._quiesce_matrix_ingestion = AsyncMock(side_effect=quiesce_failure)
    bot.prepare_for_sync_shutdown = AsyncMock(side_effect=cleanup_failure)
    bot.stop = AsyncMock(side_effect=cleanup_failure)
    sync_task = asyncio.create_task(asyncio.sleep(60))
    agent_bots = {"agent1": bot}
    sync_tasks = {"agent1": sync_task}

    async def failing_cancel(
        entity_name: str,
        tasks: dict[str, asyncio.Task],
        **_kwargs: object,
    ) -> None:
        task = tasks.pop(entity_name)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise cleanup_failure

    with (
        patch(
            "mindroom.orchestration.runtime.cancel_sync_task",
            side_effect=failing_cancel,
        ),
        pytest.raises(RuntimeError, match="source quiesce failed") as raised,
    ):
        await stop_entities(
            {"agent1"},
            agent_bots,
            sync_tasks,
            restart_entities={"agent1"},
        )

    assert raised.value is quiesce_failure
    assert sync_task.cancelled()
    bot.prepare_for_sync_shutdown.assert_awaited_once()
    bot.stop.assert_awaited_once()
    assert agent_bots == {"agent1": bot}
    assert sync_tasks == {}


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
        mock_bot.matrix_id = MatrixID.parse("@mindroom_test_agent:localhost")
        mock_bot.start = AsyncMock()
        mock_bot.rooms = []
        mock_create_bot.return_value = mock_bot

        # Create config with one agent
        config = MagicMock(spec=Config)
        config.agents = {"test_agent": MagicMock()}
        config.teams = {}
        config.mcp_servers = {}
        config.plugins = []
        config.event_journal = MagicMock()
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
    config.event_journal = MagicMock()
    orchestrator.config = config

    router_bot = AsyncMock()
    router_bot.agent_name = "router"
    router_bot.matrix_id = MatrixID.parse("@mindroom_router:localhost")
    router_bot.running = True
    router_bot.stop = AsyncMock()

    general_bot = AsyncMock()
    general_bot.agent_name = "general"
    general_bot.matrix_id = MatrixID.parse("@mindroom_general:localhost")
    general_bot.running = True
    general_bot.stop = AsyncMock()

    orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

    async def completed_sync_supervisor() -> None:
        return None

    sync_tasks_started = asyncio.Event()

    def start_completed_sync_task(entity_name: str, _bot: object) -> None:
        orchestrator._sync_tasks[entity_name] = asyncio.create_task(completed_sync_supervisor())
        if set(orchestrator._sync_tasks) == {"router", "general"}:
            sync_tasks_started.set()

    with (
        patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
        patch.object(orchestrator, "_start_router_bot", new=AsyncMock(return_value=router_bot)),
        patch.object(
            orchestrator,
            "_start_entities_once",
            new=AsyncMock(return_value=EntityStartResults(started_bots=[general_bot])),
        ),
        patch.object(orchestrator, "_setup_rooms_and_memberships", new=AsyncMock()),
        patch.object(orchestrator, "_recover_stale_streams_after_restart", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator, "_start_sync_task", side_effect=start_completed_sync_task),
    ):
        runtime_task = asyncio.create_task(orchestrator._start_runtime())
        try:
            await asyncio.wait_for(sync_tasks_started.wait(), timeout=1.0)
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
async def test_start_runtime_starts_sync_before_startup_maintenance_completes(tmp_path: Path) -> None:
    """Initial sync loops must not wait for room reconciliation or restart maintenance."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

    config = MagicMock(spec=Config)
    config.agents = {"general": MagicMock()}
    config.teams = {}
    config.mcp_servers = {}
    config.event_journal = MagicMock()
    orchestrator.config = config

    router_bot = AsyncMock()
    router_bot.agent_name = "router"
    router_bot.matrix_id = MatrixID.parse("@mindroom_router:localhost")
    router_bot.running = True
    router_bot.stop = AsyncMock()

    general_bot = AsyncMock()
    general_bot.agent_name = "general"
    general_bot.matrix_id = MatrixID.parse("@mindroom_general:localhost")
    general_bot.running = True
    general_bot.stop = AsyncMock()

    orchestrator.agent_bots = {"router": router_bot, "general": general_bot}

    setup_started = asyncio.Event()
    setup_can_finish = asyncio.Event()
    sync_started_by_entity = {
        "router": asyncio.Event(),
        "general": asyncio.Event(),
    }
    call_order: list[str] = []

    async def blocked_setup(_: list[object]) -> None:
        call_order.append("setup_started")
        setup_started.set()
        await setup_can_finish.wait()
        call_order.append("setup_finished")

    def start_sync_task(entity_name: str, _bot: object) -> None:
        call_order.append(f"sync_started:{entity_name}")
        sync_started_by_entity[entity_name].set()

    with (
        patch("mindroom.orchestrator.wait_for_matrix_homeserver", new=AsyncMock()),
        patch.object(orchestrator, "_start_router_bot", new=AsyncMock(return_value=router_bot)),
        patch.object(
            orchestrator,
            "_start_entities_once",
            new=AsyncMock(return_value=EntityStartResults(started_bots=[general_bot])),
        ),
        patch.object(orchestrator, "_setup_rooms_and_memberships", side_effect=blocked_setup),
        patch.object(orchestrator, "_recover_stale_streams_after_restart", new=AsyncMock()),
        patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        patch.object(orchestrator, "_start_sync_task", side_effect=start_sync_task),
    ):
        runtime_task = asyncio.create_task(orchestrator._start_runtime())
        try:
            await asyncio.wait_for(setup_started.wait(), timeout=1.0)
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in sync_started_by_entity.values())),
                timeout=1.0,
            )

            assert "setup_finished" not in call_order
            assert {"sync_started:router", "sync_started:general"} <= set(call_order)
        finally:
            setup_can_finish.set()
            await orchestrator.stop()
            if not runtime_task.done():
                runtime_task.cancel()
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(runtime_task, timeout=1.0)


@pytest.mark.asyncio
async def test_update_config_replays_cancelled_startup_maintenance_and_runs_approval_cleanup(tmp_path: Path) -> None:
    """Hot reload during startup maintenance must not lose one-shot restart cleanup."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    current_config = Config()
    new_config = Config()

    plan = ConfigUpdatePlan(
        new_config=new_config,
        changed_mcp_servers=set(),
        configured_entities=set(),
        entities_to_restart=set(),
        new_entities=set(),
        removed_entities=set(),
        mindroom_user_changed=False,
        matrix_room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )

    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    orchestrator.agent_bots = {"router": router_bot}
    orchestrator.config = current_config
    orchestrator.running = True
    orchestrator._startup_maintenance.startup_cutoff_ms = 123456

    maintenance_started = asyncio.Event()
    maintenance_released = asyncio.Event()
    replayed: list[tuple[list[object], object, int]] = []

    async def blocked_startup_maintenance() -> None:
        maintenance_started.set()
        await maintenance_released.wait()

    old_maintenance_task = asyncio.create_task(blocked_startup_maintenance())
    try:
        orchestrator._startup_maintenance.task = old_maintenance_task
        await asyncio.wait_for(maintenance_started.wait(), timeout=1.0)

        def replay_startup_maintenance(bots: list[object], config: object, *, startup_cutoff_ms: int) -> None:
            replayed.append((bots, config, startup_cutoff_ms))

        with (
            patch("mindroom.orchestration.config_lifecycle.load_config", return_value=new_config),
            patch("mindroom.orchestration.config_lifecycle.build_config_update_plan", return_value=plan),
            patch.object(orchestrator, "_stop_entities_before_mcp_sync", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
            patch.object(orchestrator, "_update_unchanged_bots", new=AsyncMock()),
            patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
            patch.object(orchestrator._startup_maintenance, "start", side_effect=replay_startup_maintenance),
            patch.object(
                orchestrator._approval_transport,
                "mark_startup_runtime_support_ready",
                new=AsyncMock(),
            ) as mark_startup_runtime_support_ready,
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is False
        assert old_maintenance_task.cancelled()
        assert replayed == [([router_bot], new_config, 123456)]
        mark_startup_runtime_support_ready.assert_awaited_once()
    finally:
        maintenance_released.set()
        if not old_maintenance_task.done():
            old_maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await old_maintenance_task


def test_running_startup_maintenance_bots_returns_router_first(tmp_path: Path) -> None:
    """Startup maintenance replay should keep router before other running bots."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

    router_bot = MagicMock()
    router_bot.running = True
    general_bot = MagicMock()
    general_bot.running = True
    stopped_bot = MagicMock()
    stopped_bot.running = False

    orchestrator.agent_bots = {
        "general": general_bot,
        "stopped": stopped_bot,
        "router": router_bot,
    }

    assert orchestrator._running_startup_maintenance_bots() == [router_bot, general_bot]


@pytest.mark.asyncio
@pytest.mark.requires_matrix  # Requires real Matrix server for sync task management
@pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
async def test_orchestrator_update_config_cancels_old_tasks(tmp_path: Path) -> None:
    """Test that update_config properly cancels old sync tasks."""
    with (
        patch("mindroom.orchestration.config_lifecycle.load_config") as mock_load_config,
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
        old_config.event_journal = MagicMock()
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
        new_config.event_journal = MagicMock()
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
        await orchestrator.config_reload._update_config()

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
        mock_existing_bot.matrix_id = MatrixID.parse("@mindroom_general:localhost")
        mock_router_bot = AsyncMock()
        mock_router_bot.matrix_id = MatrixID.parse("@mindroom_router:localhost")
        orchestrator.agent_bots = {"general": mock_existing_bot, "router": mock_router_bot}

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

        def make_bot(
            _entity_name: str,
            agent_user: AgentMatrixUser,
            *_args: object,
            **_kwargs: object,
        ) -> AsyncMock:
            bot = AsyncMock()
            bot.matrix_id = agent_user.matrix_id
            bot.try_start = AsyncMock(return_value=True)
            bot.sync_forever = AsyncMock()
            created_bots.append(bot)
            return bot

        mock_create_bot.side_effect = make_bot

        # --- act ---
        try:
            await orchestrator.config_reload._update_config()
        finally:
            for task in list(orchestrator._sync_tasks.values()):
                task.cancel()
            await asyncio.gather(*orchestrator._sync_tasks.values(), return_exceptions=True)

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
    shutdown_order: list[str] = []

    async def track_catalog_drain(*_args: object, **_kwargs: object) -> None:
        shutdown_order.append("catalog_drain")

    with (
        patch("mindroom.orchestrator.cancel_sync_task") as mock_cancel,
        patch(
            "mindroom.orchestrator.wait_for_background_tasks",
            new=AsyncMock(side_effect=track_catalog_drain),
        ) as mock_wait,
    ):
        orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

        # Track which tasks are cancelled
        cancelled = []

        async def track_cancel(name: str, tasks: dict) -> None:
            shutdown_order.append("sync_cancel")
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
        mock_bot2 = AsyncMock()
        mock_bot2.running = True

        async def track_source_quiesce() -> None:
            shutdown_order.append("source_quiesce")

        mock_bot1._quiesce_matrix_ingestion = AsyncMock(side_effect=track_source_quiesce)
        mock_bot2._quiesce_matrix_ingestion = AsyncMock(side_effect=track_source_quiesce)

        async def track_entity_stop(*_args: object, **_kwargs: object) -> None:
            shutdown_order.append("entity_teardown")

        mock_bot1.stop = AsyncMock(side_effect=track_entity_stop)
        mock_bot2.stop = AsyncMock(side_effect=track_entity_stop)

        orchestrator.agent_bots = {
            "agent1": mock_bot1,
            "router": mock_bot2,
        }

        async def track_mcp_stop() -> None:
            shutdown_order.append("mcp_teardown")

        with patch.object(orchestrator, "_stop_mcp_manager", new=AsyncMock(side_effect=track_mcp_stop)):
            await orchestrator.stop()

        # Verify all tasks were cancelled
        assert set(cancelled) == {"agent1", "router"}

        # Verify sync_tasks dict is empty
        assert len(orchestrator._sync_tasks) == 0

        # Verify bots were stopped with public shutdown metadata and no restart cancellation source.
        mock_bot1.stop.assert_awaited_once_with(shutdown_intent=ORDERLY_SHUTDOWN)
        mock_bot2.stop.assert_awaited_once_with(shutdown_intent=ORDERLY_SHUTDOWN)
        mock_wait.assert_has_awaits(
            [
                call(
                    5.0,
                    owner=orchestrator._mcp_catalog_change_task_owner,
                    shutdown_intent=ORDERLY_SHUTDOWN,
                ),
                call(
                    5.0,
                    owner=orchestrator._dispatch_recovery_task_owner,
                    shutdown_intent=ORDERLY_SHUTDOWN,
                ),
            ],
        )
        assert mock_wait.await_count == 2
        quiesce_indexes = [index for index, action in enumerate(shutdown_order) if action == "source_quiesce"]
        cancel_indexes = [index for index, action in enumerate(shutdown_order) if action == "sync_cancel"]
        assert len(quiesce_indexes) == 2
        assert len(cancel_indexes) == 2
        assert max(quiesce_indexes) < min(cancel_indexes)
        assert shutdown_order.index("catalog_drain") < shutdown_order.index("mcp_teardown")
        assert shutdown_order.index("catalog_drain") < shutdown_order.index("entity_teardown")


@pytest.mark.asyncio
async def test_shutdown_step_logs_the_exact_incomplete_phase() -> None:
    """A stuck shutdown await leaves a non-sensitive exact phase boundary."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_step() -> None:
        entered.set()
        await release.wait()

    with patch("mindroom.orchestrator.logger.info") as log_info:
        task = asyncio.create_task(_run_shutdown_step("config_reload", blocked_step()))
        await asyncio.wait_for(entered.wait(), timeout=1)

        log_info.assert_any_call(
            "orchestrator_shutdown_phase_started",
            shutdown_phase="config_reload",
        )
        assert (
            call(
                "orchestrator_shutdown_phase_completed",
                shutdown_phase="config_reload",
            )
            not in log_info.call_args_list
        )

        release.set()
        await task

        log_info.assert_any_call(
            "orchestrator_shutdown_phase_completed",
            shutdown_phase="config_reload",
        )


@pytest.mark.asyncio
async def test_orchestrator_stop_prioritizes_quiesce_failure_after_cleanup_failures(
    tmp_path: Path,
) -> None:
    """Orderly stop releases every resource before surfacing its barrier error."""
    quiesce_failure = RuntimeError("source quiesce failed")
    cleanup_failure = RuntimeError("cleanup failed")
    bot = AsyncMock()
    bot.running = True
    bot._quiesce_matrix_ingestion = AsyncMock(side_effect=quiesce_failure)
    bot.stop = AsyncMock(side_effect=cleanup_failure)
    journal = AsyncMock()
    journal.close = AsyncMock(side_effect=asyncio.CancelledError())

    async def failing_cancel(
        entity_name: str,
        tasks: dict[str, object],
    ) -> None:
        tasks.pop(entity_name)
        raise cleanup_failure

    with (
        patch("mindroom.orchestrator.cancel_sync_task", side_effect=failing_cancel),
        patch(
            "mindroom.orchestrator.wait_for_background_tasks",
            new=AsyncMock(),
        ),
    ):
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=orchestrator_runtime_paths(tmp_path),
        )
        orchestrator.agent_bots = {"agent1": bot}
        orchestrator._sync_tasks = {"agent1": MagicMock()}
        orchestrator._open_journal = journal

        with pytest.raises(RuntimeError, match="source quiesce failed") as raised:
            await orchestrator.stop()

    assert raised.value is quiesce_failure
    assert orchestrator._sync_tasks == {}
    bot.stop.assert_awaited_once_with(shutdown_intent=ORDERLY_SHUTDOWN)
    journal.close.assert_awaited_once()
    assert orchestrator._open_journal is None


@pytest.mark.asyncio
async def test_orchestrator_stop_retains_shared_journal_while_response_owner_is_live(
    tmp_path: Path,
) -> None:
    """A live response owner keeps the shared journal open for its bounded unwind."""
    response_failure = ResponseShutdownTimeoutError("response still owns runtime resources")
    bot = AsyncMock()
    bot.running = True
    bot._quiesce_matrix_ingestion = AsyncMock()
    bot.pending_response_owner_count = 1
    bot.stop = AsyncMock(side_effect=response_failure)
    journal = AsyncMock()
    journal.close = AsyncMock()

    with patch(
        "mindroom.orchestrator.wait_for_background_tasks",
        new=AsyncMock(),
    ):
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=orchestrator_runtime_paths(tmp_path),
        )
        orchestrator.agent_bots = {"agent1": bot}
        orchestrator._open_journal = journal

        with pytest.raises(ResponseShutdownTimeoutError) as raised:
            await orchestrator.stop()

    assert raised.value is response_failure
    journal.close.assert_not_awaited()
    assert orchestrator._open_journal is journal


@pytest.mark.asyncio
async def test_orchestrator_stop_retries_response_cleanup_after_late_owner_release(
    tmp_path: Path,
) -> None:
    """A late response unwind finishes releases before the timeout is surfaced."""
    response_failure = ResponseShutdownTimeoutError("response still owns runtime resources")
    bot = AsyncMock()
    bot.running = True
    bot._quiesce_matrix_ingestion = AsyncMock()
    bot.pending_response_owner_count = 1
    bot.deferred_stop_required = True
    owner_released = asyncio.Event()

    async def fail_bounded_stop(*, shutdown_intent: RuntimeShutdownIntent) -> None:
        assert shutdown_intent is ORDERLY_SHUTDOWN
        bot.pending_response_owner_count = 0
        asyncio.get_running_loop().call_soon(owner_released.set)
        raise response_failure

    async def finish_deferred_stop(
        *,
        shutdown_intent: RuntimeShutdownIntent,
        timeout_seconds: float,
    ) -> None:
        assert shutdown_intent is ORDERLY_SHUTDOWN
        assert timeout_seconds == 15.0
        assert bot.pending_response_owner_count == 0
        await owner_released.wait()
        bot.deferred_stop_required = False

    bot.stop = AsyncMock(side_effect=fail_bounded_stop)
    bot.finish_deferred_stop = AsyncMock(side_effect=finish_deferred_stop)
    journal = AsyncMock()
    journal.close = AsyncMock()

    with patch(
        "mindroom.orchestrator.wait_for_background_tasks",
        new=AsyncMock(),
    ):
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=orchestrator_runtime_paths(tmp_path),
        )
        orchestrator.agent_bots = {"agent1": bot}
        orchestrator._open_journal = journal

        await orchestrator.stop()

    bot.stop.assert_awaited_once_with(shutdown_intent=ORDERLY_SHUTDOWN)
    bot.finish_deferred_stop.assert_awaited_once_with(
        shutdown_intent=ORDERLY_SHUTDOWN,
        timeout_seconds=15.0,
    )
    journal.close.assert_awaited_once()
    assert orchestrator._open_journal is None


@pytest.mark.asyncio
async def test_deferred_agent_stop_waits_for_retained_proof_before_resources() -> None:
    """The real bot finalizer cannot release resources ahead of its proof owner."""
    runner = ResponseRunner(deps=MagicMock())
    response_started = asyncio.Event()
    proof_started = asyncio.Event()
    proof_cancelled = asyncio.Event()
    release_proof = asyncio.Event()

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    async def retained_proof() -> bool:
        proof_started.set()
        while not release_proof.is_set():
            try:
                await release_proof.wait()
            except asyncio.CancelledError:
                proof_cancelled.set()
        return True

    response_task = runner.track_inbox_response(
        interrupted_response(),
        name="test_deferred_agent_stop_response",
        recovery_proof_ready=retained_proof,
    )
    await response_started.wait()
    runner.begin_process_shutdown()
    with pytest.raises(ResponseShutdownTimeoutError, match="recovery proof"):
        await runner.drain_inbox_responses(
            cancel_after_seconds=0.01,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )
    await proof_started.wait()
    await proof_cancelled.wait()

    bot = object.__new__(AgentBot)
    bot._deferred_stop_required = True
    bot._response_runner = runner
    bot._release_stopped_resources = AsyncMock()
    bot.logger = MagicMock()
    finalizing = asyncio.create_task(
        AgentBot.finish_deferred_stop(
            bot,
            shutdown_intent=ORDERLY_SHUTDOWN,
            timeout_seconds=0.1,
        ),
    )
    await asyncio.sleep(0)

    assert not finalizing.done()
    bot._release_stopped_resources.assert_not_awaited()

    release_proof.set()
    await asyncio.wait_for(finalizing, timeout=0.1)

    bot._release_stopped_resources.assert_awaited_once_with([])
    assert not bot.deferred_stop_required
    assert runner.pending_inbox_response_count == 0
    await asyncio.gather(response_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_orchestrator_deferred_stop_keeps_journal_open_for_resistant_owner(
    tmp_path: Path,
) -> None:
    """No shutdown deadline may close the shared journal under a live owner."""
    response_failure = ResponseShutdownTimeoutError("response still owns runtime resources")
    release_owner = asyncio.Event()
    finalizer_entered = asyncio.Event()
    bot = AsyncMock()
    bot.running = True
    bot._quiesce_matrix_ingestion = AsyncMock()
    bot.pending_response_owner_count = 1
    bot.deferred_stop_required = True
    bot.stop = AsyncMock(side_effect=response_failure)

    async def finish_deferred_stop(
        *,
        shutdown_intent: RuntimeShutdownIntent,
        timeout_seconds: float,
    ) -> None:
        assert shutdown_intent is ORDERLY_SHUTDOWN
        assert timeout_seconds == 15.0
        finalizer_entered.set()
        await release_owner.wait()
        bot.pending_response_owner_count = 0
        bot.deferred_stop_required = False

    bot.finish_deferred_stop = AsyncMock(side_effect=finish_deferred_stop)
    journal = AsyncMock()
    journal.close = AsyncMock()

    with patch(
        "mindroom.orchestrator.wait_for_background_tasks",
        new=AsyncMock(),
    ):
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=orchestrator_runtime_paths(tmp_path),
        )
        orchestrator.agent_bots = {"agent1": bot}
        orchestrator._open_journal = journal
        stopping = asyncio.create_task(orchestrator.stop())
        await finalizer_entered.wait()
        await asyncio.sleep(0.02)

        assert not stopping.done()
        journal.close.assert_not_awaited()

        release_owner.set()
        await stopping

    journal.close.assert_awaited_once()
    assert orchestrator._open_journal is None


@pytest.mark.asyncio
async def test_deferred_agent_stop_deadline_keeps_resources_under_live_proof() -> None:
    """The explicit finalization deadline fails without releasing live owners."""
    runner = ResponseRunner(deps=MagicMock())
    response_started = asyncio.Event()
    proof_started = asyncio.Event()
    release_proof = asyncio.Event()

    async def interrupted_response() -> None:
        response_started.set()
        await asyncio.Event().wait()

    async def resistant_proof() -> bool:
        proof_started.set()
        while not release_proof.is_set():
            with suppress(asyncio.CancelledError):
                await release_proof.wait()
        return True

    response_task = runner.track_inbox_response(
        interrupted_response(),
        name="test_deferred_agent_stop_deadline_response",
        recovery_proof_ready=resistant_proof,
    )
    await response_started.wait()
    runner.begin_process_shutdown()
    with pytest.raises(ResponseShutdownTimeoutError, match="recovery proof"):
        await runner.drain_inbox_responses(
            cancel_after_seconds=0.01,
            shutdown_intent=ORDERLY_SHUTDOWN,
        )

    bot = object.__new__(AgentBot)
    bot._deferred_stop_required = True
    bot._response_runner = runner
    bot._release_stopped_resources = AsyncMock()
    bot.logger = MagicMock()

    try:
        with pytest.raises(ResponseShutdownTimeoutError, match="recovery proof"):
            await asyncio.wait_for(
                AgentBot.finish_deferred_stop(
                    bot,
                    shutdown_intent=ORDERLY_SHUTDOWN,
                    timeout_seconds=0.02,
                ),
                timeout=0.08,
            )
        assert proof_started.is_set()
        bot._release_stopped_resources.assert_not_awaited()
        assert bot.deferred_stop_required
    finally:
        release_proof.set()
        await asyncio.gather(response_task, return_exceptions=True)
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_orchestrator_retains_shared_journal_for_generic_failure_until_response_owner_stops(
    tmp_path: Path,
) -> None:
    """Live ownership, not the surfaced error type, controls shared-journal close."""
    preparation_failure = RuntimeError("preparation failed before response drain")
    bot = AsyncMock()
    bot.running = True
    bot._quiesce_matrix_ingestion = AsyncMock()
    bot.pending_response_owner_count = 1
    stop_calls = 0

    async def fail_then_stop(*, shutdown_intent: RuntimeShutdownIntent) -> None:
        nonlocal stop_calls
        assert shutdown_intent is ORDERLY_SHUTDOWN
        stop_calls += 1
        if stop_calls == 1:
            raise preparation_failure
        bot.pending_response_owner_count = 0

    bot.stop = AsyncMock(side_effect=fail_then_stop)
    journal = AsyncMock()
    journal.close = AsyncMock()

    with patch(
        "mindroom.orchestrator.wait_for_background_tasks",
        new=AsyncMock(),
    ):
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=orchestrator_runtime_paths(tmp_path),
        )
        orchestrator.agent_bots = {"agent1": bot}
        orchestrator._open_journal = journal

        with pytest.raises(RuntimeError, match="preparation failed before response drain") as raised:
            await orchestrator.stop()

        assert raised.value is preparation_failure
        journal.close.assert_not_awaited()
        assert orchestrator._open_journal is journal

        await orchestrator.stop()

    journal.close.assert_awaited_once()
    assert orchestrator._open_journal is None


@pytest.mark.asyncio
async def test_orchestrator_stop_finishes_journal_close_after_cancellation(
    tmp_path: Path,
) -> None:
    """Caller cancellation cannot strand the shared journal after quiescence."""
    quiesce_failure = RuntimeError("source quiesce failed")
    close_entered = asyncio.Event()
    release_close = asyncio.Event()
    close_completed = asyncio.Event()
    bot = AsyncMock()
    bot.running = True
    bot._quiesce_matrix_ingestion = AsyncMock(side_effect=quiesce_failure)
    bot.stop = AsyncMock()
    journal = AsyncMock()

    async def hold_close() -> None:
        close_entered.set()
        await release_close.wait()
        close_completed.set()

    journal.close = AsyncMock(side_effect=hold_close)
    with patch(
        "mindroom.orchestrator.wait_for_background_tasks",
        new=AsyncMock(),
    ):
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=orchestrator_runtime_paths(tmp_path),
        )
        orchestrator.agent_bots = {"agent1": bot}
        orchestrator._open_journal = journal
        stopping = asyncio.create_task(orchestrator.stop())

        await asyncio.wait_for(close_entered.wait(), timeout=2)
        stopping.cancel()
        await asyncio.sleep(0)
        release_close.set()
        with pytest.raises(RuntimeError, match="source quiesce failed") as raised:
            await stopping

    assert raised.value is quiesce_failure
    assert close_completed.is_set()
    journal.close.assert_awaited_once()
    assert orchestrator._open_journal is None


@pytest.mark.asyncio
async def test_orchestrator_stop_finishes_cleanup_after_cancellation_during_quiesce(
    tmp_path: Path,
) -> None:
    """A source-barrier failure stays primary while shutdown drains cancellation."""
    quiesce_failure = RuntimeError("source quiesce failed")
    quiesce_entered = asyncio.Event()
    release_quiesce = asyncio.Event()
    bot = AsyncMock()
    bot.running = True

    async def hold_then_fail_quiesce() -> None:
        quiesce_entered.set()
        await release_quiesce.wait()
        raise quiesce_failure

    bot._quiesce_matrix_ingestion = AsyncMock(side_effect=hold_then_fail_quiesce)
    bot.stop = AsyncMock()
    journal = AsyncMock()
    journal.close = AsyncMock()
    with patch(
        "mindroom.orchestrator.wait_for_background_tasks",
        new=AsyncMock(),
    ):
        orchestrator = _MultiAgentOrchestrator(
            runtime_paths=orchestrator_runtime_paths(tmp_path),
        )
        orchestrator.agent_bots = {"agent1": bot}
        orchestrator._sync_tasks = {"agent1": MagicMock()}
        orchestrator._open_journal = journal
        stopping = asyncio.create_task(orchestrator.stop())

        await asyncio.wait_for(quiesce_entered.wait(), timeout=2)
        stopping.cancel()
        await asyncio.sleep(0)
        release_quiesce.set()
        with pytest.raises(RuntimeError, match="source quiesce failed") as raised:
            await stopping

    assert raised.value is quiesce_failure
    assert orchestrator._sync_tasks == {}
    bot.stop.assert_awaited_once_with(shutdown_intent=ORDERLY_SHUTDOWN)
    journal.close.assert_awaited_once()
    assert orchestrator._open_journal is None


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
    monkeypatch.setattr(runtime_helpers, "_stalled_restart_jitter_seconds", lambda: 0.0)

    await sync_forever_with_restart(bot, max_retries=3)

    # First sync killed by watchdog, second sync completed normally.
    assert bot.first_call_cancelled is True
    assert iteration == 2
    assert bot.sync_calls == 2


@pytest.mark.asyncio
async def test_clean_sync_return_while_running_restarts(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clean sync_forever return is only a shutdown if the bot stopped.

    nio can return from sync_forever without raising even though the bot is
    still marked running. The supervisor must not treat that as intentional
    shutdown, otherwise the entity stays present but stops syncing forever.
    """
    bot = _FakeBot()

    async def return_once_then_stop() -> None:
        bot.sync_calls += 1
        if bot.sync_calls == 1:
            return
        bot.running = False

    bot.sync_forever = return_once_then_stop

    retry_attempts: list[int] = []

    def fake_retry_delay(attempt: int, **_kwargs: float) -> float:
        retry_attempts.append(attempt)
        return 0.0

    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", fake_retry_delay)

    await sync_forever_with_restart(bot, max_retries=3)

    assert bot.sync_calls == 2
    assert bot.prepare_for_sync_shutdown_calls == 1
    assert bot.prepare_for_sync_shutdown_cancel_messages == [None]
    assert retry_attempts == [1]


@pytest.mark.asyncio
async def test_running_bot_logs_when_sync_retries_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry exhaustion should be visible if the bot is still logically running."""
    bot = _FakeBot()

    async def clean_return() -> None:
        bot.sync_calls += 1

    bot.sync_forever = clean_return
    logger = MagicMock()

    monkeypatch.setattr(runtime_helpers, "logger", logger)
    monkeypatch.setattr(runtime_helpers, "retry_delay_seconds", lambda *_args, **_kwargs: 0.0)

    await sync_forever_with_restart(bot, max_retries=2)

    assert bot.running is True
    assert bot.sync_calls == 2
    assert bot.prepare_for_sync_shutdown_calls == 0
    logger.error.assert_called_once_with(
        "sync_loop_retries_exhausted",
        agent="test_agent",
        retry_count=2,
        max_retries=2,
        restart_reason_category="unexpected_sync_return",
    )


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

    with capture_logs() as logs:
        await sync_forever_with_restart(bot, max_retries=1)

    cleanup_warnings = [entry for entry in logs if entry["event"] == "sync_iteration_cleanup_failed"]
    assert len(cleanup_warnings) == 1
    assert cleanup_warnings[0]["agent"] == "test_agent"
    assert cleanup_warnings[0]["exc_info"] is True
