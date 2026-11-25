"""Tests for per-agent ``io.mindroom.active_runs`` room-state publishing and pause boundaries."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom import interactive
from mindroom.active_runs import _CURRENT_LEASE, ActiveRunPublisher, pause_current_active_run
from mindroom.agent_cli.cli_approval_waits import CliApprovalWaits, CliWaiters
from mindroom.coalescing.ingress_lanes import IngressRetryError
from mindroom.config.participation import ParticipationConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import EventJournalStore
from mindroom.final_delivery import Completed, TurnProgress
from mindroom.logging_config import get_logger
from mindroom.matrix.client_room_admin import _MANAGED_ROOM_EVENT_POWER_LEVELS
from mindroom.matrix.event_types import ACTIVE_RUNS_EVENT_TYPE
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.participation import ParticipationGate
from mindroom.reply_turn.request import ResponseRequest, response_typing_indicator
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.response_runner import ResponseRunner, _LockedTurn, _TeamResponseRequest
from mindroom.response_sources import Owned, Unclaimed, UnclaimedReason
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN, SYNC_RESTART_SHUTDOWN
from mindroom.secret_request_cards import _job_awaits_requester
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_jobs.runtime import BackgroundOutcome, ToolJobRuntime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.turns.facts import IngressFacts
from mindroom.turns.interactive_selection import InteractiveSelections, InteractiveSelectionsDeps
from mindroom.voice import ingress as voice_ingress_module
from mindroom.voice.ingress import VoiceIngress, VoiceIngressDeps
from tests.approval_continuation_helpers import approval_continuation
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import TEST_PASSWORD, test_runtime_paths, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _config, _envelope, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


class _FakeClient:
    """Minimal Matrix client recording ``room_put_state`` and ``room_get_state_event`` calls."""

    def __init__(self) -> None:
        self.rooms: dict[str, object] = {}  # Intentionally empty to prove no client.rooms gate.
        self.puts: list[tuple[str, str, str, dict[str, Any]]] = []
        self.put_event = asyncio.Event()
        self.put_handler: Any = None
        self.get_responses: dict[str, Any] = {}
        self.get_calls: list[str] = []
        self.room_typing = AsyncMock(return_value=nio.RoomTypingResponse("!room:localhost"))

    async def room_put_state(
        self,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        state_key: str = "",
    ) -> object:
        self.puts.append((room_id, event_type, state_key, content))
        self.put_event.set()
        if self.put_handler is not None:
            return await self.put_handler(room_id, event_type, content, state_key)
        return nio.RoomPutStateResponse(event_id=f"$state-{len(self.puts)}", room_id=room_id)

    async def room_get_state_event(self, room_id: str, event_type: str, state_key: str = "") -> object:
        self.get_calls.append(room_id)
        response = self.get_responses.get(room_id)
        if callable(response):
            return await response(room_id, event_type, state_key)
        if response is not None:
            return response
        return nio.RoomGetStateEventError("Not found", status_code="M_NOT_FOUND")


async def _wait_for_puts(client: _FakeClient, count: int) -> None:
    async with asyncio.timeout(2.0):
        while len(client.puts) < count:
            client.put_event.clear()
            await client.put_event.wait()


async def _wait_workers_done(publisher: ActiveRunPublisher) -> None:
    async with asyncio.timeout(2.0):
        while publisher._workers:
            await asyncio.gather(*list(publisher._workers.values()), return_exceptions=True)


def _threads_of(put: tuple[str, str, str, dict[str, Any]]) -> list[str | None]:
    return [entry["thread_id"] for entry in put[3]["threads"]]


def test_wire_contract_event_type_and_power_level() -> None:
    """The event type constant and managed-room power level match the wire contract."""
    assert ACTIVE_RUNS_EVENT_TYPE == "io.mindroom.active_runs"
    assert _MANAGED_ROOM_EVENT_POWER_LEVELS[ACTIVE_RUNS_EVENT_TYPE] == 0


@pytest.mark.asyncio
async def test_track_lifecycle_dedupe_room_mode_order_and_byte_cap() -> None:
    """Track publishes threaded and room-mode runs oldest-first, deduplicated, and byte-capped."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")

    async with publisher.track("!r:localhost", "$t1"):
        await _wait_for_puts(client, 1)
        room_id, event_type, state_key, content = client.puts[0]
        assert (room_id, event_type, state_key) == ("!r:localhost", ACTIVE_RUNS_EVENT_TYPE, "@bot:localhost")
        assert content["version"] == 1
        assert content["ttl_ms"] == 180_000
        assert isinstance(content["updated_at"], int)
        assert _threads_of(client.puts[0]) == ["$t1"]

        async with publisher.track("!r:localhost", None), publisher.track("!r:localhost", "$t1"):
            await _wait_for_puts(client, 2)
            assert _threads_of(client.puts[1]) == ["$t1", None]

    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []
    assert not publisher._workers

    # Byte cap keeps oldest-first threads under the 27,000-byte budget.
    long_threads = [f"${i:04d}-" + ("x" * 200) for i in range(200)]
    for tid in long_threads:
        publisher._leases.setdefault("!cap:localhost", []).append(
            SimpleNamespace(thread_id=tid, pauses=0),  # type: ignore[arg-type]
        )
    capped = publisher._desired_threads("!cap:localhost")
    assert 0 < len(capped) < len(long_threads)
    assert capped == long_threads[: len(capped)]


@pytest.mark.asyncio
async def test_cancelling_reply_task_still_clears_active_run() -> None:
    """Cancelling a reply task still completes the bot-owned clearing state write."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")
    entered = asyncio.Event()

    async def reply() -> None:
        async with publisher.track("!r:localhost", "$t1"):
            entered.set()
            await asyncio.Future()

    task = asyncio.create_task(reply())
    await entered.wait()
    await _wait_for_puts(client, 1)
    assert _threads_of(client.puts[0]) == ["$t1"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_pause_current_active_run_supports_nesting_and_noop_outside_track() -> None:
    """Nested foreground pauses keep the run paused until all waits exit, and no-op outside track."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")

    with pause_current_active_run():
        assert client.puts == []

    async with publisher.track("!r:localhost", "$t1"):
        await _wait_for_puts(client, 1)
        assert _threads_of(client.puts[-1]) == ["$t1"]

        with pause_current_active_run():
            await _wait_for_puts(client, 2)
            assert _threads_of(client.puts[-1]) == []
            with pause_current_active_run():
                await asyncio.sleep(0.01)
                assert len(client.puts) == 2
            # Still paused by outer context.
            await asyncio.sleep(0.01)
            assert _threads_of(client.puts[-1]) == []

        await _wait_for_puts(client, 3)
        assert _threads_of(client.puts[-1]) == ["$t1"]

    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_coalescing_bursts_and_heartbeat_and_idle_exit() -> None:
    """Bursts coalesce while a write is in flight, active runs heartbeat, and idle workers exit."""
    client = _FakeClient()
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def slow_first(room_id: str, _et: str, _content: dict[str, Any], _sk: str) -> object:
        if len(client.puts) == 1:
            first_started.set()
            await release_first.wait()
        return nio.RoomPutStateResponse(event_id=f"$s-{len(client.puts)}", room_id=room_id)

    client.put_handler = slow_first
    publisher = ActiveRunPublisher(
        get_client=lambda: client,
        user_id="@bot:localhost",
        heartbeat_seconds=0.05,
    )

    async with publisher.track("!r:localhost", "$t1"):
        await first_started.wait()
        # Burst of transitions while PUT #1 is in flight must coalesce into one write.
        async with publisher.track("!r:localhost", "$t2"):
            pass
        async with publisher.track("!r:localhost", "$t3"):
            release_first.set()
            await _wait_for_puts(client, 2)
            assert _threads_of(client.puts[1]) == ["$t1", "$t3"]
            # Heartbeat refreshes updated_at while active.
            await _wait_for_puts(client, 3)
            assert _threads_of(client.puts[2]) == ["$t1", "$t3"]
            assert client.puts[2][3]["updated_at"] >= client.puts[1][3]["updated_at"]

    await _wait_workers_done(publisher)
    idle_count = len(client.puts)
    assert _threads_of(client.puts[-1]) == []
    await asyncio.sleep(0.08)
    assert len(client.puts) == idle_count


@pytest.mark.asyncio
async def test_forbidden_refused_recovers_on_later_change_and_transient_error_retries() -> None:
    """M_FORBIDDEN stops retrying the refused snapshot but recovers when permission is later granted."""
    client = _FakeClient()
    allow_forbidden_room = False
    attempts = 0

    async def handler(room_id: str, _et: str, _content: dict[str, Any], _sk: str) -> object:
        nonlocal attempts
        if room_id == "!forbidden:localhost" and not allow_forbidden_room:
            return nio.RoomPutStateError("Forbidden", status_code="M_FORBIDDEN")
        if room_id == "!retry:localhost":
            attempts += 1
            if attempts == 1:
                raise OSError
        return nio.RoomPutStateResponse(event_id="$ok", room_id=room_id)

    client.put_handler = handler
    publisher = ActiveRunPublisher(
        get_client=lambda: client,
        user_id="@bot:localhost",
        heartbeat_seconds=0.03,
        retry_backoffs=(0.01, 0.02),
    )

    # M_FORBIDDEN records desired snapshot without retrying or heartbeating while unchanged.
    async with publisher.track("!forbidden:localhost", "$t1"):
        await _wait_for_puts(client, 1)
        await asyncio.sleep(0.06)
        assert len([p for p in client.puts if p[0] == "!forbidden:localhost"]) == 1
        assert "!forbidden:localhost" in publisher._refused
    await _wait_workers_done(publisher)
    assert len([p for p in client.puts if p[0] == "!forbidden:localhost"]) == 2

    # Once an admin grants permission, a new reply succeeds and clears _refused.
    allow_forbidden_room = True
    async with publisher.track("!forbidden:localhost", "$t2"):
        await _wait_for_puts(client, 3)
        assert "!forbidden:localhost" not in publisher._refused
        assert publisher._published["!forbidden:localhost"] == ["$t2"]
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []

    # Transient failure retries and succeeds.
    before = len(client.puts)
    async with publisher.track("!retry:localhost", "$t1"):
        await _wait_for_puts(client, before + 2)
        assert publisher._published["!retry:localhost"] == ["$t1"]
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_shutdown_idle_noop_active_clear_and_reopen() -> None:
    """Idle shutdown does nothing, active shutdown clears rooms concurrently, and reopen resumes."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")

    # Idle shutdown makes zero writes.
    await publisher.shutdown()
    assert client.puts == []
    publisher.reopen()

    async with publisher.track("!r1:localhost", "$t1"), publisher.track("!r2:localhost", None):
        await _wait_for_puts(client, 2)
        await publisher.shutdown(timeout_seconds=1.0)
        cleared = {put[0]: _threads_of(put) for put in client.puts[2:]}
        assert cleared == {"!r1:localhost": [], "!r2:localhost": []}

    # Reopen clears published/swept caches so an in-process restart reconciles stale state.
    publisher._swept.add("!r1:localhost")
    publisher.reopen()
    assert not publisher._published
    assert not publisher._swept
    async with publisher.track("!r1:localhost", "$t3"):
        await _wait_for_puts(client, 5)
        assert _threads_of(client.puts[-1]) == ["$t3"]
    await _wait_workers_done(publisher)


@pytest.mark.asyncio
async def test_startup_sweep_clears_stale_skips_clean_and_respects_live_runs() -> None:
    """Startup sweep clears stale non-empty rooms with bounded concurrency without clobbering live runs."""
    client = _FakeClient()
    in_flight_read = asyncio.Event()
    finish_read = asyncio.Event()
    max_concurrent = 0
    current = 0

    async def slow_stale_read(room_id: str, _et: str, _sk: str) -> object:
        nonlocal current, max_concurrent
        current += 1
        max_concurrent = max(max_concurrent, current)
        try:
            if room_id == "!raced:localhost":
                in_flight_read.set()
                await finish_read.wait()
            else:
                await asyncio.sleep(0.01)
            return nio.RoomGetStateEventResponse(
                content={"version": 1, "threads": [{"thread_id": "$stale"}]},
                event_type=ACTIVE_RUNS_EVENT_TYPE,
                state_key="@bot:localhost",
                room_id=room_id,
            )
        finally:
            current -= 1

    client.get_responses = {
        "!stale1:localhost": slow_stale_read,
        "!stale2:localhost": slow_stale_read,
        "!stale3:localhost": slow_stale_read,
        "!stale4:localhost": slow_stale_read,
        "!stale5:localhost": slow_stale_read,
        "!raced:localhost": slow_stale_read,
        "!empty:localhost": nio.RoomGetStateEventResponse(
            content={"version": 1, "threads": []},
            event_type=ACTIVE_RUNS_EVENT_TYPE,
            state_key="@bot:localhost",
            room_id="!empty:localhost",
        ),
    }

    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")
    rooms = [
        "!stale1:localhost",
        "!stale2:localhost",
        "!stale3:localhost",
        "!stale4:localhost",
        "!stale5:localhost",
        "!raced:localhost",
        "!empty:localhost",
        "!missing:localhost",
    ]
    publisher.schedule_startup_sweep(rooms)
    # Scheduling again skips already-swept rooms.
    publisher.schedule_startup_sweep(rooms)

    await in_flight_read.wait()
    async with publisher.track("!raced:localhost", "$live"):
        await _wait_for_puts(client, 1)
        finish_read.set()
        await asyncio.gather(*list(publisher._sweep_tasks))
        # Live run in !raced:localhost was NOT clobbered by stale sweep.
        assert publisher._published["!raced:localhost"] == ["$live"]

    await _wait_workers_done(publisher)
    assert max_concurrent <= 4
    assert len(client.get_calls) == len(rooms)
    cleared_rooms = {p[0] for p in client.puts if _threads_of(p) == []}
    assert {
        "!stale1:localhost",
        "!stale2:localhost",
        "!stale3:localhost",
        "!stale4:localhost",
        "!stale5:localhost",
    } <= cleared_rooms
    assert "!empty:localhost" not in cleared_rooms
    assert "!missing:localhost" not in cleared_rooms


@pytest.mark.asyncio
async def test_response_typing_indicator_hooks_gating_pause_and_participation() -> None:
    """response_typing_indicator reuses visibility gating, clears on pause, and supports participation."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")
    req = _plain_request(_target(thread_id="$root"))

    # 1. Silent schedule does not publish active runs.
    silent = replace(
        req,
        response_envelope=replace(
            req.response_envelope,
            origin=replace(req.response_envelope.origin, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
        ),
    )
    async with response_typing_indicator(
        cast("nio.AsyncClient", client),
        silent,
        response_run_id="r0",
        active_runs=publisher,
    ):
        await asyncio.sleep(0.01)
    assert client.puts == []

    # 2. Contact turn sends contact_receipt and does not publish active runs.
    contact_req = replace(req, contact=cast("Any", object()))
    receipt = AsyncMock()
    async with response_typing_indicator(
        cast("nio.AsyncClient", client),
        contact_req,
        response_run_id="r1",
        contact_receipt=receipt,
        active_runs=publisher,
    ):
        await asyncio.sleep(0.01)
    receipt.assert_awaited_once()
    assert client.puts == []

    # 3. Visible turn publishes and clears on ResponsePausedForApproval.
    async def run_and_pause() -> None:
        async with response_typing_indicator(
            cast("nio.AsyncClient", client),
            req,
            response_run_id="r2",
            active_runs=publisher,
        ):
            await _wait_for_puts(client, 1)
            assert _threads_of(client.puts[-1]) == ["$root"]
            raise ResponsePausedForApproval(cast("Any", SimpleNamespace(run_id="run-1")))

    with pytest.raises(ResponsePausedForApproval):
        await run_and_pause()
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []

    # 4. Participation-gated turn waits for approval AND still pauses via ContextVar in caller task.
    gate = ParticipationGate()
    before = len(client.puts)
    async with response_typing_indicator(
        cast("nio.AsyncClient", client),
        req,
        response_run_id="r3",
        participation=gate,
        active_runs=publisher,
    ):
        await asyncio.sleep(0.02)
        assert len(client.puts) == before
        gate.approve_existing_response()
        await _wait_for_puts(client, before + 1)
        assert _threads_of(client.puts[-1]) == ["$root"]
        with pause_current_active_run():
            await _wait_for_puts(client, before + 2)
            assert _threads_of(client.puts[-1]) == []
        await _wait_for_puts(client, before + 3)
        assert _threads_of(client.puts[-1]) == ["$root"]
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_foreground_human_wait_boundaries_pause_active_run() -> None:
    """Foreground secret-card and CLI approval waits pause the active run while detached jobs do not."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")

    # 1. Secret request card foreground wait pauses; detached background job context does not pause caller directly.
    async with publisher.track("!r:localhost", "$t1"):
        await _wait_for_puts(client, 1)
        async with _job_awaits_requester(cast("Any", object())):
            await _wait_for_puts(client, 2)
            assert _threads_of(client.puts[-1]) == []
        await _wait_for_puts(client, 3)
        assert _threads_of(client.puts[-1]) == ["$t1"]

        # Inside a background job without a runtime, caller active run is NOT paused directly.
        with job_control_context(JobControl(job_id="job-detached")):
            async with _job_awaits_requester(cast("Any", SimpleNamespace(storage_root="/nonexistent"))):
                await asyncio.sleep(0.01)
                assert _threads_of(client.puts[-1]) == ["$t1"]

    # 2. CLI approval wait pauses while awaiting waiter.wait().
    states = iter(
        [
            None,
            SimpleNamespace(state="waiting", failure=None, calls=()),
            SimpleNamespace(state="ready", failure=None, calls=()),
        ],
    )
    store = SimpleNamespace(approval_continuation_for_source=AsyncMock(side_effect=lambda _s: next(states)))
    waits = CliApprovalWaits(
        store=cast("Any", store),
        claim=AsyncMock(return_value=SimpleNamespace(calls=())),
        advance=AsyncMock(),
        exit=AsyncMock(),
        waiters=CliWaiters(),
    )
    waiter = asyncio.Event()
    paused_attempt = SimpleNamespace(requirements=(), continuation_count=0)

    async with publisher.track("!r:localhost", "$t-cli"):
        before = len(client.puts)
        await _wait_for_puts(client, before + 1)
        wait_task = asyncio.create_task(
            waits.wait(
                cast("Any", paused_attempt),
                waiter=waiter,
                source="$src",
                target=MessageTarget.resolve("!r:localhost", "$t-cli", "$src"),
                publish=AsyncMock(),
                authorize=AsyncMock(return_value=True),
            ),
        )
        await _wait_for_puts(client, before + 2)
        assert _threads_of(client.puts[-1]) == []
        waiter.set()
        await wait_task
        await _wait_for_puts(client, before + 3)
        assert _threads_of(client.puts[-1]) == ["$t-cli"]

    await _wait_workers_done(publisher)


@pytest.mark.asyncio
async def test_tool_job_runtime_wait_pauses_while_job_awaits_approval(tmp_path: Path) -> None:
    """ToolJobRuntime.wait pauses the caller's active run only while the awaited job awaits approval."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")
    journal = EventJournalStore.open_sqlite(tmp_path / "journal.sqlite3")
    store = journal.tool_jobs("test")
    await store.take_ownership()
    runtime = ToolJobRuntime(
        store,
        grant=lambda _job: "allowed",
        authorize_execution=lambda _o, _t, _a: None,
        cancel=AsyncMock(),
    )
    await runtime.recover()
    owner = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!r:localhost",
        thread_id="$t-job",
        resolved_thread_id="$t-job",
        session_id="!r:localhost:$t-job",
    )
    job_started = asyncio.Event()
    release_approval = asyncio.Event()
    finish_job = asyncio.Event()

    async def op() -> BackgroundOutcome:
        job_started.set()
        await runtime.set_awaiting_approval("job-1", awaiting=True)
        await release_approval.wait()
        await runtime.set_awaiting_approval("job-1", awaiting=False)
        await finish_job.wait()
        return BackgroundOutcome("completed", "done")

    try:
        async with publisher.track("!r:localhost", "$t-job"):
            await _wait_for_puts(client, 1)
            _, claim = await runtime.start(
                "job-1",
                tool_name="shell",
                depth=0,
                adapter={"tool_call_id": "call-1"},
                owner=owner,
                operation=op,
            )
            await job_started.wait()
            wait_task = asyncio.create_task(
                runtime.wait(
                    "job-1",
                    owner=owner,
                    depth=0,
                    claim=claim,
                    pause_on_human_wait=pause_current_active_run,
                ),
            )
            await _wait_for_puts(client, 2)
            assert _threads_of(client.puts[-1]) == []
            release_approval.set()
            await _wait_for_puts(client, 3)
            assert _threads_of(client.puts[-1]) == ["$t-job"]
            finish_job.set()
            waited = await wait_task
            assert waited.job.status == "completed"
    finally:
        await runtime.shutdown()
        await journal.close()
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_bot_prepare_for_sync_shutdown_fence_and_restart_clear(tmp_path: Path) -> None:
    """Process shutdown fences transport before yielding; non-fenced shutdown clears active runs."""
    bot = _bot(tmp_path / "process")
    assert bot._response_runner.active_runs is bot._active_runs
    assert bot._approval_runner.active_runs is bot._active_runs

    events: list[str] = []
    client = _FakeClient()
    client.begin_process_shutdown_transport_fence = lambda: events.append("fence")  # type: ignore[attr-defined]
    bot.client = cast("nio.AsyncClient", client)

    bot._personal_room_lifecycle.cancel_reconciliation = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda **_kw: events.append("first_await"),
    )
    async with bot._active_runs.track("!r:localhost", "$t1"):
        await _wait_for_puts(client, 1)
        await bot.prepare_for_sync_shutdown(shutdown_intent=ORDERLY_SHUTDOWN)
    assert events == ["fence", "first_await"]
    assert len(client.puts) == 1
    assert not bot._active_runs._published
    assert not bot._active_runs._swept

    # Non-process shutdown (sync restart) best-effort clears active runs to [].
    restart_bot = _bot(tmp_path / "restart")
    restart_client = _FakeClient()
    restart_bot.client = cast("nio.AsyncClient", restart_client)
    async with restart_bot._active_runs.track("!r:localhost", "$t2"):
        await _wait_for_puts(restart_client, 1)
        await restart_bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)
    assert [_threads_of(put) for put in restart_client.puts] == [["$t2"], []]


_VOICE_PREPARED = PreparedIngress(
    sender="@user:localhost",
    event_id="$voice-1",
    body="hi",
    source={"content": {"msgtype": "m.text", "body": "hi"}},
    payload_trusted=True,
)


def _gated_voice_prepare(
    exc: type[BaseException] | None = None,
) -> tuple[asyncio.Event, asyncio.Event, Callable[..., Awaitable[PreparedIngress]]]:
    started, release = asyncio.Event(), asyncio.Event()

    async def _prepare(*_a: object, **_kw: object) -> PreparedIngress:
        started.set()
        await release.wait()
        if exc is not None:
            raise exc
        return _VOICE_PREPARED

    return started, release, _prepare


async def _admit_voice(
    tmp_path: Path,
    *,
    active_runs: ActiveRunPublisher | None,
    prepare_impl: Callable[..., Awaitable[PreparedIngress]],
    event_id: str = "$voice-1",
    thread_id: str | None = "$root",
    voice_enabled: bool = True,
    prepared: object | None = None,
    may_be_for_me: bool = True,
    can_publish_active_run: Callable[[str], bool] | None = None,
) -> asyncio.Task[object]:
    config = _config(tmp_path)
    config.voice.enabled = voice_enabled
    resolver = SimpleNamespace(
        coalescing_thread_id=AsyncMock(return_value=thread_id),
        build_message_target=lambda *, room_id, thread_id, reply_to_event_id, **_kw: MessageTarget.resolve(
            room_id=room_id,
            thread_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            thread_start_root_event_id=reply_to_event_id if thread_id is None else None,
        ),
        build_envelope=lambda *, event, target, **_kw: _envelope(target, source_event_id=event.event_id),
    )
    readiness = SimpleNamespace(
        prepared_source=AsyncMock(return_value=prepared),
        prepare=AsyncMock(side_effect=prepare_impl),
    )
    extra_deps: dict[str, Any] = (
        {"can_publish_active_run": can_publish_active_run} if can_publish_active_run is not None else {}
    )
    deps = VoiceIngressDeps(
        runtime=cast("Any", SimpleNamespace(config=config)),
        logger=get_logger("test.voice_active_runs"),
        runtime_paths=test_runtime_paths(tmp_path),
        matrix_id=MatrixID.parse("@bot:localhost"),
        resolver=cast("Any", resolver),
        response_runner=cast("Any", SimpleNamespace(lifecycle_coordinator=ResponseLifecycleCoordinator())),
        delivery_gateway=cast("Any", SimpleNamespace()),
        readiness=cast("Any", readiness),
        turn_policy=cast("Any", SimpleNamespace(not_for_this_agent=lambda *_a: not may_be_for_me)),
        active_runs=active_runs,
        **extra_deps,
    )
    tasks: list[asyncio.Task[object]] = []
    event = SimpleNamespace(
        event_id=event_id,
        sender="@user:localhost",
        source={"content": {"msgtype": "m.audio", "body": "clip.ogg"}},
        server_timestamp=1_000,
    )
    facts = IngressFacts(physical_sender="@user:localhost", source_kind="message", lane_requester_id="@user:localhost")
    await VoiceIngress(deps).admit(
        nio.MatrixRoom("!room:localhost", "@bot:localhost"),
        cast("Any", event),
        facts,
        dispatch_timing=None,
        ticket=cast("Any", SimpleNamespace(admit_ready=lambda _k, task, **_kw: tasks.append(task))),
    )
    return tasks[0]


@pytest.mark.asyncio
async def test_delayed_release_cancel_shutdown_and_error() -> None:
    """Delayed release clears immediately on error and cancels cleanly on cancel() and shutdown()."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")

    async def fail_tracked() -> None:
        async with publisher.track("!r:localhost", "$t1") as lease:
            await _wait_for_puts(client, 1)
            lease.release_delay_seconds = 5.0
            raise RuntimeError

    with pytest.raises(RuntimeError):
        await fail_tracked()
    assert _CURRENT_LEASE.get() is None
    assert not publisher._pending_releases
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []

    async with publisher.track("!r:localhost", "$t2") as lease:
        await _wait_for_puts(client, 3)
        lease.release_delay_seconds = 0.04
    assert len(publisher._pending_releases) == 1
    before_cancel = len(client.puts)
    publisher.cancel()
    assert not publisher._pending_releases
    assert not publisher._leases
    await asyncio.sleep(0.07)
    assert len(client.puts) == before_cancel

    publisher.reopen()
    async with publisher.track("!r:localhost", "$t3") as lease:
        await _wait_for_puts(client, before_cancel + 1)
        lease.release_delay_seconds = 5.0
    await publisher.shutdown(timeout_seconds=1.0)
    assert not publisher._pending_releases
    assert not publisher._leases
    assert _threads_of(client.puts[-1]) == []
    after_shutdown = len(client.puts)
    publisher.reopen()
    await asyncio.sleep(0.02)
    assert len(client.puts) == after_shutdown


@pytest.mark.asyncio
async def test_voice_ingress_active_run_tracking_handoff_and_immediate_clear(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VoiceIngress publishes during transcription, bridges replies, and clears immediately otherwise."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")
    monkeypatch.setattr(voice_ingress_module, "_VOICE_HANDOFF_GRACE_SECONDS", 0.06)

    # 1. Thread reply publishes in-flight and bridges into reply lease without an intermediate [] write.
    started, release, prepare = _gated_voice_prepare()
    task = await _admit_voice(tmp_path / "t1", active_runs=publisher, prepare_impl=prepare, thread_id="$root")
    await started.wait()
    await _wait_for_puts(client, 1)
    assert _threads_of(client.puts[0]) == ["$root"]
    release.set()
    await task
    assert _CURRENT_LEASE.get() is None
    assert len(publisher._pending_releases) == 1
    async with publisher.track("!room:localhost", "$root"):
        await asyncio.sleep(0.09)
        assert not publisher._pending_releases
        assert len(client.puts) == 1
    await _wait_workers_done(publisher)
    assert [_threads_of(p) for p in client.puts] == [["$root"], []]

    # 2. New root voice message publishes its own event ID and clears once handoff grace expires.
    started, release, prepare = _gated_voice_prepare()
    task = await _admit_voice(
        tmp_path / "t2",
        active_runs=publisher,
        prepare_impl=prepare,
        event_id="$v-root",
        thread_id=None,
    )
    await started.wait()
    await _wait_for_puts(client, 3)
    assert _threads_of(client.puts[-1]) == ["$v-root"]
    release.set()
    await task
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []

    # 3. may_be_for_me=False, prepare errors, and cancellation clear immediately (0 grace).
    monkeypatch.setattr(voice_ingress_module, "_VOICE_HANDOFF_GRACE_SECONDS", 5.0)
    for name, may_be_for_me, exc_type in (
        ("other", False, None),
        ("err", True, IngressRetryError),
        ("cancel", True, asyncio.CancelledError),
    ):
        started, release, prepare = _gated_voice_prepare(exc_type)
        before = len(client.puts)
        task = await _admit_voice(
            tmp_path / name,
            active_runs=publisher,
            prepare_impl=prepare,
            may_be_for_me=may_be_for_me,
        )
        await started.wait()
        await _wait_for_puts(client, before + 1)
        if exc_type is asyncio.CancelledError:
            task.cancel()
        else:
            release.set()
        if exc_type is not None:
            with pytest.raises(exc_type):
                await task
        else:
            await task
        assert not publisher._pending_releases
        await _wait_workers_done(publisher)
        assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_voice_ingress_active_run_skip_conditions_and_bot_wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VoiceIngress skips publishing when checkpointed, voice-disabled, contact-room, or router-owned."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")
    skip_cases: tuple[dict[str, Any], ...] = (
        {"active_runs": publisher, "prepared": SimpleNamespace(thread_id="$root", coalescing_thread_id="$root")},
        {"active_runs": publisher, "voice_enabled": False},
        {"active_runs": publisher, "can_publish_active_run": lambda _r: False},
        {"active_runs": None},
    )
    for idx, kwargs in enumerate(skip_cases):
        await (
            await _admit_voice(tmp_path / f"skip-{idx}", prepare_impl=AsyncMock(return_value=_VOICE_PREPARED), **kwargs)
        )
    await _wait_workers_done(publisher)
    assert client.puts == []
    assert not publisher._pending_releases

    bot = _bot(tmp_path / "agent-bot")
    assert bot._voice_ingress.deps.active_runs is bot._active_runs
    assert bot._voice_ingress.deps.can_publish_active_run("!room:localhost") is True
    monkeypatch.setattr("mindroom.bot.contact_room", lambda *_a: (object(), "a"))
    assert bot._voice_ingress.deps.can_publish_active_run("!contact:localhost") is False
    router = make_test_agent_bot(
        AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            password=TEST_PASSWORD,
            display_name="Router",
            user_id="@mindroom_router:localhost",
        ),
        _config(tmp_path / "router-bot"),
        rooms=["!room:localhost"],
    )
    assert router._voice_ingress.deps.active_runs is None


async def _assert_pause_toggles(client: _FakeClient, expected_thread: str | None) -> None:
    before = len(client.puts)
    with pause_current_active_run():
        await _wait_for_puts(client, before + 1)
        assert _threads_of(client.puts[-1]) == []
    await _wait_for_puts(client, before + 2)
    assert _threads_of(client.puts[-1]) == [expected_thread]


def _runner_with_publisher(tmp_path: Path) -> tuple[ResponseRunner, _FakeClient, ActiveRunPublisher]:
    bot = _bot(tmp_path)
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@mindroom_general:localhost")
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.active_runs = publisher
    runner.approvals.active_runs = publisher
    return runner, client, publisher


def _patch_turn_prep(runner: ResponseRunner, req: ResponseRequest, **overrides: object) -> ExitStack:
    stack = ExitStack()
    defaults: dict[str, object] = {
        "_admit_locked_turn": AsyncMock(return_value=req),
        "_send_placeholder": AsyncMock(return_value=None),
        "_prepare_claimed_request": AsyncMock(return_value=req),
        "_prepare_agent_turn": AsyncMock(return_value=_LockedTurn.__new__(_LockedTurn)),
        "_generate_and_settle": AsyncMock(),
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        stack.enter_context(patch.object(runner, name, new=value))
    stack.enter_context(
        patch.object(runner.deps.request_preparer, "with_system_enrichment", new=AsyncMock(return_value=req)),
    )
    return stack


@pytest.mark.asyncio
async def test_reentrant_track_refcounts_single_lease_pause_and_deferred_activation() -> None:
    """Nested scopes share one lease so pauses clear immediately and refcount releases once."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")

    async with publisher.track("!r:localhost", "$t1") as outer:
        await _wait_for_puts(client, 1)
        assert outer.activations == 1
        assert publisher._leases["!r:localhost"] == [outer]
        async with (
            publisher.track("!r:localhost", "$t1", active=False) as mid,
            publisher.deferred_activation(mid) as act,
        ):
            assert mid is outer
            act()
            act()  # Idempotent.
            assert outer.activations == 2
            async with publisher.track("!r:localhost", "$t1", active=False) as inner, publisher.activate(inner):
                assert inner is outer
                assert outer.activations == 3
                assert publisher._leases["!r:localhost"] == [outer]
                await _assert_pause_toggles(client, "$t1")
            assert outer.activations == 2
        assert outer.activations == 1
        async with (
            publisher.track("!r:localhost", "$t1", active=False) as unused,
            publisher.deferred_activation(unused),
        ):
            assert unused is outer
        assert outer.activations == 1
    await _wait_workers_done(publisher)
    assert outer.activations == 0
    assert _threads_of(client.puts[-1]) == []

    # Rescuing a pending delayed release re-activates without appending a duplicate lease.
    async with publisher.track("!r:localhost", "$t2", active=False) as lease:
        async with publisher.activate(lease):
            await _wait_for_puts(client, 5)
            lease.release_delay_seconds = 5.0
        assert lease in publisher._pending_releases
        async with publisher.activate(lease):
            assert lease not in publisher._pending_releases
            assert publisher._leases["!r:localhost"] == [lease]
            lease.release_delay_seconds = 0.0
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_response_runner_active_run_from_placeholder_through_finalization(tmp_path: Path) -> None:
    """_run_locked_turn publishes before placeholder, shares lease with typing, and clears cleanly."""
    runner, client, publisher = _runner_with_publisher(tmp_path)
    req = _plain_request(_target(thread_id="$root"))
    assert not req.quiet
    seen: list[object] = []

    async def send_placeholder(*_a: object, **_kw: object) -> str:
        await _wait_for_puts(client, 1)
        assert _threads_of(client.puts[-1]) == ["$root"]
        seen.append(_CURRENT_LEASE.get())
        return "$placeholder"

    async def prepare_claimed(request: ResponseRequest, **_kw: object) -> ResponseRequest:
        assert _CURRENT_LEASE.get() is seen[0]
        return request

    async def generate(_turn: object) -> None:
        async with response_typing_indicator(
            cast("nio.AsyncClient", client),
            req,
            response_run_id="r1",
            active_runs=publisher,
        ):
            assert _CURRENT_LEASE.get() is seen[0]
            await _assert_pause_toggles(client, "$root")
        await asyncio.sleep(0.01)
        assert _threads_of(client.puts[-1]) == ["$root"]

    with _patch_turn_prep(
        runner,
        req,
        _send_placeholder=send_placeholder,
        _prepare_claimed_request=prepare_claimed,
        _generate_and_settle=generate,
    ):
        assert await runner._run_locked_turn(req, team=None, participation=None) == Owned()
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []

    # Approved continued reply + ResponsePausedForApproval, and team turns, publish early and clear cleanly.
    before = len(client.puts)
    continued_req = replace(req, existing_event_id="$existing", participation=ParticipationConfig())
    continued_gate = ParticipationGate()
    continued_gate.approve_existing_response()

    async def pause_generate(_turn: object) -> None:
        await _wait_for_puts(client, before + 1)
        assert _threads_of(client.puts[-1]) == ["$root"]
        raise ResponsePausedForApproval(cast("Any", SimpleNamespace(run_id="r-pause")))

    with (
        _patch_turn_prep(runner, continued_req, _generate_and_settle=pause_generate),
        pytest.raises(ResponsePausedForApproval),
    ):
        await runner._run_locked_turn(continued_req, team=None, participation=continued_gate)
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []

    before_team = len(client.puts)
    team_req = _TeamResponseRequest(member_names=("general",), team_mode="coordinate", rejection_message="rejected")

    async def check_team(*_a: object, **_kw: object) -> None:
        await _wait_for_puts(client, before_team + 1)
        assert _threads_of(client.puts[-1]) == ["$root"]

    with _patch_turn_prep(runner, req, _settle_team_rejection=check_team):
        assert await runner._run_locked_turn(req, team=team_req, participation=None) == Owned()
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_response_runner_active_run_gates_non_visible_and_unapproved_participation(tmp_path: Path) -> None:
    """Empty, refused, silent, contact, and unapproved participation turns skip early active-run publication."""
    runner, client, publisher = _runner_with_publisher(tmp_path)
    req = _plain_request(_target(thread_id="$root"))
    silent_req = replace(
        req,
        response_envelope=replace(
            req.response_envelope,
            origin=replace(req.response_envelope.origin, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
        ),
    )
    contact_req = replace(req, contact=cast("Any", (SimpleNamespace(status="active"), "a")))
    assert silent_req.quiet
    assert contact_req.quiet

    with (
        patch.object(
            runner,
            "_finalize_without_running_locked",
            new=AsyncMock(return_value=Unclaimed(UnclaimedReason.NOTHING_TO_RUN)),
        ),
        patch.object(runner, "_admit_locked_turn", new=AsyncMock(return_value=Unclaimed(UnclaimedReason.REVOKED))),
    ):
        await runner._run_locked_turn(replace(req, prompt="   "), team=None, participation=None)
        await runner._run_locked_turn(req, team=None, participation=None)
    for gated in (silent_req, contact_req):
        with patch.object(runner, "_runs_nothing", return_value=None), _patch_turn_prep(runner, gated):
            await runner._run_locked_turn(gated, team=None, participation=None)
    assert client.puts == []

    part_req = replace(req, participation=ParticipationConfig())
    declined_gate = ParticipationGate()

    async def generate_declined(_turn: object) -> None:
        async with response_typing_indicator(
            cast("nio.AsyncClient", client),
            part_req,
            response_run_id="rp-decline",
            participation=declined_gate,
            active_runs=publisher,
        ):
            declined_gate.decline("stay_silent")
            await asyncio.sleep(0.01)

    with _patch_turn_prep(runner, part_req, _generate_and_settle=generate_declined):
        await runner._run_locked_turn(part_req, team=None, participation=declined_gate)
    await _wait_workers_done(publisher)
    assert client.puts == []

    gate = ParticipationGate()

    async def generate_participation(_turn: object) -> None:
        await asyncio.sleep(0.01)
        assert client.puts == []
        outer = _CURRENT_LEASE.get()
        async with response_typing_indicator(
            cast("nio.AsyncClient", client),
            part_req,
            response_run_id="rp",
            participation=gate,
            active_runs=publisher,
        ):
            assert _CURRENT_LEASE.get() is outer
            gate.approve_existing_response()
            await _wait_for_puts(client, 1)
            assert _threads_of(client.puts[-1]) == ["$root"]
            await _assert_pause_toggles(client, "$root")
        puts_after_typing = len(client.puts)
        await asyncio.sleep(0.01)
        assert len(client.puts) == puts_after_typing
        assert _threads_of(client.puts[-1]) == ["$root"]

    with _patch_turn_prep(runner, part_req, _generate_and_settle=generate_participation):
        await runner._run_locked_turn(part_req, team=None, participation=gate)
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []


@pytest.mark.asyncio
async def test_interactive_selection_active_run_coverage_skip_and_bot_wiring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """InteractiveSelections tracks ack + payload + nested response with one lease and matches bot wiring."""
    client = _FakeClient()
    publisher = ActiveRunPublisher(get_client=lambda: client, user_id="@bot:localhost")
    room = nio.MatrixRoom("!room:localhost", "@bot:localhost")
    target = _target(thread_id="$root", reply_to_event_id="$q1")
    selection = interactive.InteractiveSelection("$q1", "Choose", "1", "A", "a", "$root")
    facts = IngressFacts(physical_sender="@user:localhost", source_kind="message", lane_requester_id="@user:localhost")
    ack_lease: list[object] = []

    async def deliver_ack(*_a: object, **_kw: object) -> tuple[str, str]:
        await _wait_for_puts(client, 1)
        assert _threads_of(client.puts[-1]) == ["$root"]
        ack_lease.append(_CURRENT_LEASE.get())
        return "$ack-1", "span-1"

    async def build_payload(_req: object) -> object:
        assert _CURRENT_LEASE.get() is ack_lease[0]
        return SimpleNamespace(prompt="selected a", model_prompt=None, attachment_ids=())

    async def generate_response(req: ResponseRequest) -> Owned:
        async with (
            publisher.track(req.room_id, req.thread_id, active=False) as lease,
            publisher.deferred_activation(lease) as act,
        ):
            assert lease is ack_lease[0]
            act()
            await _assert_pause_toggles(client, "$root")
        return Owned()

    def build_selections(**kw: object) -> InteractiveSelections:
        record = SimpleNamespace(source_event_ids=("$sel-1",), discovery_event_ids=("$q1",))
        terminal = bool(kw.get("terminal", False))
        return InteractiveSelections(
            InteractiveSelectionsDeps(
                logger=get_logger("test.interactive_active_runs"),
                runtime_paths=test_runtime_paths(tmp_path),
                agent_name="general",
                resolver=cast("Any", SimpleNamespace(fetch_thread_history=AsyncMock(return_value=[]))),
                normalizer=cast(
                    "Any",
                    SimpleNamespace(
                        build_dispatch_payload_with_attachments=AsyncMock(side_effect=kw.get("payload", build_payload)),
                    ),
                ),
                ingress=cast("Any", SimpleNamespace()),
                response_runner=cast(
                    "Any",
                    SimpleNamespace(
                        placeholder_ai_run_content=lambda *_a, **_kw: None,
                        generate_response=AsyncMock(side_effect=kw.get("generate", generate_response)),
                    ),
                ),
                delivery_gateway=cast("Any", SimpleNamespace(settle_unclaimed_reply=AsyncMock())),
                turn_store=cast(
                    "Any",
                    SimpleNamespace(
                        is_handled=AsyncMock(return_value=terminal),
                        attach_response_context=lambda r, **_kw: r,
                        response_history_scope=lambda _a: SimpleNamespace(),
                        record_pending_turn=AsyncMock(return_value=SimpleNamespace(record=record, deleted_sources=())),
                        build_run_metadata=lambda _r: None,
                    ),
                ),
                visible_responses=cast(
                    "Any",
                    SimpleNamespace(
                        deliver_selection_acknowledgement=AsyncMock(side_effect=kw.get("ack", deliver_ack)),
                    ),
                ),
                retry_dispatch_sources=lambda *_a: None,
                settle_dispatch_sources=AsyncMock(),
                dispatch_source_is_terminal=AsyncMock(return_value=terminal),
                active_runs=cast("ActiveRunPublisher | None", kw.get("active_runs", publisher)),
                can_publish_active_run=cast("Callable[[str], bool]", kw.get("can_publish", lambda _r: True)),
            ),
        )

    assert await build_selections()._execute_admitted_interactive_selection(
        room,
        selection=selection,
        facts=facts,
        source_event_id="$sel-1",
        response_target=target,
    )
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []

    before_fail = len(client.puts)

    async def fail_ack(*_a: object, **_kw: object) -> tuple[None, None]:
        await _wait_for_puts(client, before_fail + 1)
        return None, None

    with pytest.raises(RuntimeError, match="no durable terminal outcome"):
        await build_selections(ack=fail_ack)._execute_admitted_interactive_selection(
            room,
            selection=selection,
            facts=facts,
            source_event_id="$sel-fail",
            response_target=target,
        )
    await _wait_workers_done(publisher)
    assert len(client.puts) == before_fail + 2
    assert _threads_of(client.puts[-1]) == []

    before_skip = len(client.puts)
    skip_payload = AsyncMock(return_value=SimpleNamespace(prompt="a", model_prompt=None, attachment_ids=()))
    skip_gen = AsyncMock(return_value=Owned())
    for sel in (
        build_selections(
            can_publish=lambda _r: False,
            ack=AsyncMock(return_value=("$ack", "sp")),
            payload=skip_payload,
            generate=skip_gen,
        ),
        build_selections(
            active_runs=None,
            ack=AsyncMock(return_value=("$ack", "sp")),
            payload=skip_payload,
            generate=skip_gen,
        ),
        build_selections(terminal=True),
    ):
        await sel._execute_admitted_interactive_selection(
            room,
            selection=selection,
            facts=facts,
            source_event_id="$sel-skip",
            response_target=target,
        )
    await _wait_workers_done(publisher)
    assert len(client.puts) == before_skip

    bot = _bot(tmp_path / "agent-bot")
    assert bot._interactive_selections.deps.active_runs is bot._active_runs
    assert bot._interactive_selections.deps.can_publish_active_run("!room:localhost") is True
    monkeypatch.setattr("mindroom.bot.contact_room", lambda *_a: (object(), "a"))
    assert bot._interactive_selections.deps.can_publish_active_run("!contact:localhost") is False
    router = make_test_agent_bot(
        AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            password=TEST_PASSWORD,
            display_name="Router",
            user_id="@mindroom_router:localhost",
        ),
        _config(tmp_path / "router-bot"),
        rooms=["!room:localhost"],
    )
    assert router._interactive_selections.deps.active_runs is None


@pytest.mark.asyncio
async def test_approval_continuation_tracks_active_run_from_refresh_through_settlement(tmp_path: Path) -> None:
    """Approval continuation publishes from refresh through settlement, shares pause lease, and skips silent schedules."""
    runner, client, publisher = _runner_with_publisher(tmp_path)
    approvals = runner.approvals
    target = _target(thread_id="$root", reply_to_event_id="$source")
    claimed = approval_continuation(approval_id="approval-1", response_event_id="$orig")
    refresh_lease: list[object] = []

    async def fake_refresh(_req: ResponseRequest) -> None:
        await _wait_for_puts(client, 1)
        assert _threads_of(client.puts[-1]) == ["$root"]
        refresh_lease.append(_CURRENT_LEASE.get())

    async def fake_settle(request: ResponseRequest, *, progress: TurnProgress, **_kw: object) -> None:
        async with response_typing_indicator(
            cast("nio.AsyncClient", client),
            request,
            response_run_id="r1",
            active_runs=publisher,
        ):
            assert _CURRENT_LEASE.get() is refresh_lease[0]
            await _assert_pause_toggles(client, "$root")
        await asyncio.sleep(0.01)
        assert _threads_of(client.puts[-1]) == ["$root"]
        progress.ending = Completed()

    with (
        patch.object(approvals.locked, "refresh_mid_turn_context_for_approval", new=fake_refresh),
        patch.object(approvals.locked, "run_and_settle_locked_response", new=fake_settle),
        patch.object(approvals, "_facts", new=AsyncMock()),
        patch.object(
            approvals,
            "_run_step",
            new=AsyncMock(side_effect=lambda *_a, **_kw: _threads_of(client.puts[-1]) == ["$root"]),
        ),
        patch("mindroom.approval.continuation_runner.next_step", return_value=object()),
    ):
        assert await approvals._run_claimed_approval_lifecycle(claimed, target=target) == Completed()
    await _wait_workers_done(publisher)
    assert _threads_of(client.puts[-1]) == []

    before_silent = len(client.puts)
    silent_claimed = replace(claimed, origin=replace(claimed.origin, source_kind=SILENT_SCHEDULE_SOURCE_KIND))

    async def silent_settle(_req: ResponseRequest, *, progress: TurnProgress, **_kw: object) -> None:
        progress.ending = Completed()

    with (
        patch.object(approvals.locked, "refresh_mid_turn_context_for_approval", new=AsyncMock()),
        patch.object(approvals.locked, "run_and_settle_locked_response", new=silent_settle),
        patch.object(approvals, "_facts", new=AsyncMock()),
        patch.object(approvals, "_run_step", new=AsyncMock(return_value=True)),
        patch("mindroom.approval.continuation_runner.next_step", return_value=object()),
    ):
        await approvals._run_claimed_approval_lifecycle(silent_claimed, target=target)
    await _wait_workers_done(publisher)
    assert len(client.puts) == before_silent
