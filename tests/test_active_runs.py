"""Tests for per-agent ``io.mindroom.active_runs`` room-state publishing and pause boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.active_runs import ActiveRunPublisher, pause_current_active_run
from mindroom.agent_cli.cli_approval_waits import CliApprovalWaits, CliWaiters
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import EventJournalStore
from mindroom.matrix.client_room_admin import _MANAGED_ROOM_EVENT_POWER_LEVELS
from mindroom.matrix.event_types import ACTIVE_RUNS_EVENT_TYPE
from mindroom.message_target import MessageTarget
from mindroom.participation import ParticipationGate
from mindroom.reply_turn.request import response_typing_indicator
from mindroom.response_turn import ResponsePausedForApproval
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN, SYNC_RESTART_SHUTDOWN
from mindroom.secret_request_cards import _job_awaits_requester
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_jobs.runtime import BackgroundOutcome, ToolJobRuntime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.response_runner_helpers import _bot, _plain_request, _target

if TYPE_CHECKING:
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
