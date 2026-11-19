"""Tests for the bot:ready lifecycle hook event."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID

import nio
import pytest
from nio import AuthenticatedToDeviceEvent, TimelineEventProvenance
from nio.durable import RecordKind, SyncBatch, SyncRecord
from nio.durable.model import OwnMembership

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.calls import CallsConfig, RealtimeCallProfile
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ROUTER_AGENT_NAME, SOURCE_KIND_KEY
from mindroom.event_journal import (
    AdmissionFacts,
    IngestionBatchAdmission,
    IngestionBatchSequenceError,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
    RoomMembershipPosition,
)
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    AgentLifecycleContext,
    HookRegistry,
    hook,
    service_bot_ready_emission,
)
from mindroom.matrix.durable_ingestion import validate_ingestion_batch
from mindroom.matrix.invited_rooms_store import invited_rooms_path, remember_invited_room
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.matrix_rtc.call_manager import CallManager
from mindroom.orchestration.runtime import sync_forever_with_restart
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.room_steward import membership_observers
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from mindroom.tool_system.plugins import config_plugins
from tests.access_schema_support import joined_members_response, refresh_memberships
from tests.bot_helpers import _hook_plugin as _plugin
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    SETTLE_TIMEOUT_SECONDS,
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    install_bots,
    install_runtime_journal_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    start_call_manager,
    test_runtime_paths,
)
from tests.journal_helpers import membership_position
from tests.journal_membership_helpers import admit_room_membership
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from tests.runtime_fakes import ReadyRuntime


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )


def _agent_bot(tmp_path: Path, *, agent_name: str = "code", orchestrator: ReadyRuntime | None = None) -> AgentBot:
    """Return one bot in a ready runtime of its own, or in ``orchestrator`` beside the bots already there."""
    config = _config(tmp_path)
    return install_runtime_journal_support(
        make_test_agent_bot(
            AgentMatrixUser(
                agent_name=agent_name,
                password=TEST_PASSWORD,
                display_name=agent_name.title(),
                user_id=f"@mindroom_{agent_name}:localhost",
            ),
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!room:localhost"],
            orchestrator=orchestrator,
        ),
    )


def _router_bot_with_orchestrator(tmp_path: Path) -> tuple[AgentBot, ReadyRuntime]:
    """Return a router bot whose runtime's grant invalidations, grant refreshes, and ready lifecycle the test observes."""
    bot = _agent_bot(tmp_path, agent_name="router")
    orchestrator = cast("ReadyRuntime", bot.orchestrator)
    membership_sync = orchestrator.agent_reply_membership_sync
    membership_sync.invalidate_observer = MagicMock(  # type: ignore[method-assign]
        wraps=membership_sync.invalidate_observer,
    )
    orchestrator.refresh_agent_reply_memberships = AsyncMock()
    orchestrator.handle_bot_ready = AsyncMock()
    return bot, orchestrator


def _open_observer(bot: AgentBot) -> None:
    """Let the bot's account observe room members, as after the first settled frame of its receive loop."""
    membership_sync = bot.orchestrator.agent_reply_membership_sync
    membership_sync.observer_loop_started(bot.config, bot.matrix_id.full_id)
    membership_sync.observer_frame_settled(bot.matrix_id.full_id)


def _invalidations(orchestrator: ReadyRuntime) -> MagicMock:
    """Return the observed invalidations of a bot's own rooms in a runtime from `_router_bot_with_orchestrator`."""
    return cast("MagicMock", orchestrator.agent_reply_membership_sync.invalidate_observer)


async def _router_with_call_manager(tmp_path: Path) -> tuple[AgentBot, MagicMock]:
    """Return a router bot from `_router_bot_with_orchestrator` that grants through room ``grant``, with its call manager."""
    bot, _orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    return bot, await start_call_manager(bot)


def _grant_room_client(bot: AgentBot, room_id: str, *member_ids: str) -> AsyncMock:
    """Save ``room_id`` as room ``grant`` and return a client joined to it with the bot and ``member_ids``."""
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    save_matrix_state(state, bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id, None, None) for user_id in (bot.agent_user.user_id, *member_ids)],
        room_id=room_id,
    )
    return client


def _thread_root_event(
    event_id: str,
    *,
    body: str,
    origin_server_ts: int,
    room_id: str = "!room:localhost",
) -> nio.RoomMessageText:
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": body, "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": room_id,
            "type": "m.room.message",
        },
    )
    assert isinstance(event, nio.RoomMessageText)
    return event


_CONSUMER_GENERATION = UUID("20000000-0000-4000-8000-000000000001")
_STREAM_ID = UUID("20000000-0000-4000-8000-000000000002")
_FRESH_SEMANTIC_FACTS = AdmissionFacts(receipt_new=True, semantic_event_new=True)
_FRESH_RECEIPT_FACTS = AdmissionFacts(receipt_new=True, semantic_event_new=False)
_REPLAY_FACTS = AdmissionFacts(receipt_new=False, semantic_event_new=False)


def _validated_timeline_admission(
    bot: AgentBot,
    room_id: str,
    event: dict[str, object],
) -> IngestionRecordAdmission:
    return validate_ingestion_batch(
        SyncBatch(
            _STREAM_ID,
            1,
            (SyncRecord(RecordKind.TIMELINE, room_id, event, provenance=TimelineEventProvenance.LIVE),),
        ),
        account_id=bot.matrix_id.full_id,
    ).records[0]


def _validated_reported_membership_admission(
    bot: AgentBot,
    room_id: str,
    *,
    previous_membership: str | None,
    membership: str,
    previous_epoch: int,
) -> IngestionRecordAdmission:
    epoch = previous_epoch + int(previous_membership == "join" and membership != "join")
    return validate_ingestion_batch(
        SyncBatch(
            _STREAM_ID,
            1,
            (
                SyncRecord(
                    RecordKind.ROOM_LIFECYCLE,
                    room_id,
                    {},
                    membership=OwnMembership(
                        previous_membership,
                        membership,
                        previous_epoch,
                        epoch,
                    ),
                ),
            ),
        ),
        account_id=bot.matrix_id.full_id,
    ).records[0]


def _history_loss_admission(room_id: str) -> IngestionRecordAdmission:
    return IngestionRecordAdmission(IngestionRecordDisposition.HISTORY_LOSS, room_id=room_id)


async def _complete_frame(bot: AgentBot) -> None:
    """Drive runtime side effects through the durable completion owner."""
    await bot._on_ingestion_frame_completion(True)


@pytest.mark.asyncio
async def test_turn_recovery_cleans_ledger_after_reading_unsettled_sources(tmp_path: Path) -> None:
    """Startup cleanup must run after recovery and preserve every raw unsettled source."""
    bot = _agent_bot(tmp_path)
    call_order: list[str] = []
    unsettled_source_event_ids = frozenset({"$pending"})
    bot._journal_dispatcher.drain_once = AsyncMock(
        side_effect=lambda: (call_order.append("recover"), 0)[1],
    )
    bot._journal_dispatcher.unsettled_event_ids = AsyncMock(
        side_effect=lambda: (call_order.append("unsettled"), unsettled_source_event_ids)[1],
    )
    bot._turn_store.cleanup = AsyncMock(side_effect=lambda **_kwargs: call_order.append("cleanup"))

    await bot.recover_pending_turn_journal_events()

    assert call_order == ["recover", "unsettled", "cleanup"]
    bot._journal_dispatcher.drain_once.assert_awaited_once_with()
    bot._turn_store.cleanup.assert_awaited_once_with(
        unsettled_source_event_ids=unsettled_source_event_ids,
    )


@pytest.mark.asyncio
async def test_turn_recovery_propagates_post_recovery_cleanup_failure(tmp_path: Path) -> None:
    """Ledger pruning failure must remain visible to the orchestrator retry owner."""
    bot = _agent_bot(tmp_path)
    bot._journal_dispatcher.drain_once = AsyncMock(return_value=0)
    bot._journal_dispatcher.unsettled_event_ids = AsyncMock(return_value=frozenset())
    bot._turn_store.cleanup = AsyncMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        await bot.recover_pending_turn_journal_events()

    bot._journal_dispatcher.drain_once.assert_awaited_once_with()
    bot._turn_store.cleanup.assert_awaited_once_with(unsettled_source_event_ids=frozenset())


@pytest.mark.asyncio
async def test_fleet_turn_recovery_releases_every_ready_bot_before_the_first_drain(tmp_path: Path) -> None:
    """One expensive recovery must not keep later bots' durable turns parked."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path), config=_config(tmp_path))
    router_bot = MagicMock()
    code_bot = MagicMock()
    router_bot.running = code_bot.running = True
    router_bot.first_sync_complete = code_bot.first_sync_complete = True
    router_released = asyncio.Event()
    code_released = asyncio.Event()
    router_recovery_started = asyncio.Event()
    finish_router_recovery = asyncio.Event()

    def release_router() -> None:
        router_released.set()

    def release_code() -> None:
        code_released.set()

    async def recover_router() -> None:
        release_router()
        router_recovery_started.set()
        await finish_router_recovery.wait()

    async def recover_code() -> None:
        release_code()

    router_bot.release_pending_turn_journal_replay = release_router
    code_bot.release_pending_turn_journal_replay = release_code
    router_bot.recover_pending_turn_journal_events = recover_router
    code_bot.recover_pending_turn_journal_events = recover_code
    install_bots(orchestrator, {"router": router_bot, "code": code_bot})

    recovery = asyncio.create_task(orchestrator.turn_recovery.recover())
    await router_recovery_started.wait()
    await asyncio.sleep(0)
    try:
        assert router_released.is_set()
        assert code_released.is_set()
    finally:
        finish_router_recovery.set()
        await recovery


@pytest.mark.asyncio
async def test_bot_ready_fires_on_first_sync_response(tmp_path: Path) -> None:
    """bot:ready should fire when the first sync response is received."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired_events: list[str] = []

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        fired_events.append(ctx.event_name)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert fired_events == ["bot:ready"]


@pytest.mark.asyncio
async def test_call_reconciliation_runs_once_per_sync_loop(tmp_path: Path) -> None:
    """Calls reconcile after each sync-loop's first successful response."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    call_manager = await start_call_manager(bot)

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
    ):
        bot.mark_sync_loop_started()
        await _complete_frame(bot)
        await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)
        await _complete_frame(bot)
        await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)

        bot.mark_sync_loop_started()
        await _complete_frame(bot)
        await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)

    assert call_manager.reconcile_joined_rooms.await_count == 2
    assert [request.args for request in call_manager.reconcile_joined_rooms.await_args_list] == [(), ()]


@pytest.mark.asyncio
async def test_bot_observes_room_members_only_while_its_receive_loop_has_caught_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh picks a bot only between a receive loop's first settled frame and that loop's end.

    A failed loop's bot is still running and synced once, yet during the retry sleep and until the next loop
    catches up nothing applies its room's membership changes.
    """
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.running = True
    bot.prepare_for_sync_shutdown = AsyncMock()  # type: ignore[method-assign]
    observers: dict[str, list[str]] = {}
    original_sleep = asyncio.sleep

    def observe(moment: str) -> None:
        observers[moment] = [client.user_id for client in membership_observers({bot.agent_name: bot})[0]]

    async def receive() -> None:
        loop = 1 if "loop 1 start" not in observers else 2
        observe(f"loop {loop} start")
        with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
            await _complete_frame(bot)
        observe(f"loop {loop} settled")
        if loop == 1:
            message = "transport disconnected"
            raise OSError(message)
        bot.running = False

    async def retry_sleep(delay: float) -> None:
        if delay == 17:
            observe("retry sleep")
        else:
            await original_sleep(delay)

    monkeypatch.setattr(bot, "sync_forever", receive)
    monkeypatch.setattr("mindroom.orchestration.runtime.retry_delay_seconds", lambda *_args, **_kwargs: 17)
    monkeypatch.setattr("mindroom.orchestration.runtime.asyncio.sleep", retry_sleep)

    await asyncio.wait_for(sync_forever_with_restart(bot, max_retries=2), 5)

    own_id = [bot.matrix_id.full_id]
    assert observers == {
        "loop 1 start": [],
        "loop 1 settled": own_id,
        "retry sleep": [],
        "loop 2 start": [],
        "loop 2 settled": own_id,
    }
    # Only the loop that was open loses its rooms: no start, catch-up or second close invalidates anything.
    assert [invalidation.kwargs["reason"] for invalidation in _invalidations(orchestrator).call_args_list] == [
        "sync_failure",
    ]


@pytest.mark.asyncio
async def test_receive_loop_child_exit_fences_grants_before_the_sibling_is_drained(tmp_path: Path) -> None:
    """When the source or the pump ends, the account's grants are revoked before the other one's cleanup is awaited."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    room_id, member = "!dm:localhost", "@alice:localhost"
    client = bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.device_id = "DEVICE"
    client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[room_id]))
    client.joined_members = AsyncMock(return_value=joined_members_response(room_id, client.user_id, member))
    index = orchestrator.agent_reply_memberships
    in_cleanup, release = asyncio.Event(), asyncio.Event()

    async def failing_source() -> None:
        message = "transport disconnected"
        raise OSError(message)

    async def held_pump(*_args: object, **_kwargs: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            in_cleanup.set()
            await release.wait()

    bot._ingestion_session = MagicMock(run=failing_source)
    bot.mark_sync_loop_started()
    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)
    await refresh_memberships(index, bot.config, bot.runtime_paths, client)
    assert index.is_current_room_member(member, room_id, bot.config, bot.runtime_paths)

    with patch("mindroom.bot.run_ingestion_pump", held_pump):
        receive_loop = asyncio.create_task(bot.sync_forever())
        try:
            await asyncio.wait_for(in_cleanup.wait(), 5)

            assert not receive_loop.done()
            assert not index.is_current_room_member(member, room_id, bot.config, bot.runtime_paths)
            assert membership_observers({bot.agent_name: bot})[0] == []
            # A frame the draining pump still completes must not reopen the closed loop.
            with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
                await _complete_frame(bot)
            assert membership_observers({bot.agent_name: bot})[0] == []
        finally:
            release.set()
        with pytest.raises(OSError, match="transport disconnected"):
            await asyncio.wait_for(receive_loop, 5)


@pytest.mark.asyncio
async def test_a_bot_that_starts_running_withdraws_proven_absence_before_its_started_hooks(tmp_path: Path) -> None:
    """From the moment a bot runs, its rooms are in no census: a room no row covers waits, with no other refresh."""
    bot = _agent_bot(tmp_path)
    index = bot.orchestrator.agent_reply_memberships
    covered, uncovered, member = "!covered:localhost", "!dm:localhost", "@alice:localhost"
    other = make_matrix_client_mock(user_id="@mindroom_other:localhost")
    other.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[covered]))
    other.joined_members = AsyncMock(return_value=joined_members_response(covered, other.user_id, member))
    await refresh_memberships(index, bot.config, bot.runtime_paths, other)
    assert not index.grants_pending(bot.config, joined_rooms=[], current_room_id=uncovered)
    epoch = index._epoch
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    in_started_hook, release = asyncio.Event(), asyncio.Event()

    async def held_started_hook(*_args: object, **_kwargs: object) -> None:
        in_started_hook.set()
        await release.wait()

    with (
        patch.object(bot, "_open_owned_matrix_client", AsyncMock(return_value=bot.client)),
        patch.object(bot, "refresh_presence", AsyncMock()),
        patch.object(bot, "_emit_agent_lifecycle_event", held_started_hook),
    ):
        starting = asyncio.create_task(bot.start())
        try:
            await asyncio.wait_for(in_started_hook.wait(), 5)

            assert bot.running
            assert index.grants_pending(bot.config, joined_rooms=[], current_room_id=uncovered)
            # Nothing was revoked and no refresh in flight was voided.
            assert index._epoch == epoch
            assert index.is_current_room_member(member, covered, bot.config, bot.runtime_paths)
        finally:
            release.set()
            await asyncio.wait_for(starting, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["armed", "open"])
@pytest.mark.parametrize("entry", ["process_shutdown", "stop"])
async def test_shutdown_entry_closes_the_bots_observation_before_any_teardown(
    tmp_path: Path,
    state: str,
    entry: str,
) -> None:
    """The process shutdown latch and a direct stop close the account at once: a suspended frame opens nothing."""
    bot, _orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.running = True
    bot.mark_sync_loop_started()
    in_frame, release_frame, in_stop_hook, release_stop_hook = (asyncio.Event() for _ in range(4))
    stopping: asyncio.Task[None] | None = None

    async def held_room_update(_rooms: object) -> None:
        in_frame.set()
        await release_frame.wait()

    async def held_stop_hook(*_args: object, **_kwargs: object) -> None:
        in_stop_hook.set()
        await release_stop_hook.wait()

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        if state == "open":
            await _complete_frame(bot)
        with patch.object(bot._room_lifecycle, "observe_trusted_sync_rooms", held_room_update):
            frame = asyncio.create_task(_complete_frame(bot))
            try:
                await asyncio.wait_for(in_frame.wait(), 5)
                if entry == "process_shutdown":
                    bot.begin_process_shutdown()
                else:
                    with patch.object(bot, "_emit_agent_lifecycle_event", held_stop_hook):
                        stopping = asyncio.create_task(bot.stop(shutdown_intent=SYNC_RESTART_SHUTDOWN))
                        await asyncio.wait_for(in_stop_hook.wait(), 5)

                assert membership_observers({bot.agent_name: bot})[0] == []
                release_frame.set()
                await asyncio.wait_for(frame, 5)
                assert membership_observers({bot.agent_name: bot})[0] == []
            finally:
                release_frame.set()
                release_stop_hook.set()
                await asyncio.wait_for(asyncio.gather(frame, *filter(None, [stopping]), return_exceptions=True), 5)


@pytest.mark.asyncio
async def test_quiesced_ingestion_ends_the_bots_observation_before_its_drain(tmp_path: Path) -> None:
    """A bot being stopped or replaced stops observing, and its rooms fail closed, before its ingestion drains."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.running = True
    bot.mark_sync_loop_started()
    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)
    assert membership_observers({bot.agent_name: bot})[0] == [bot.client]
    _invalidations(orchestrator).reset_mock()
    draining = asyncio.Event()
    drained: list[tuple[bool, int]] = []

    async def drain() -> None:
        drained.append((bot.observes_room_members, _invalidations(orchestrator).call_count))
        draining.set()

    bot._ingestion_session = MagicMock(quiesce=drain)

    await bot.quiesce_matrix_ingestion()

    assert drained == [(False, 1)]
    _invalidations(orchestrator).assert_called_once_with(bot.config, bot.matrix_id.full_id, reason="observer_stopping")
    assert membership_observers({bot.agent_name: bot})[0] == []


@pytest.mark.asyncio
async def test_second_bots_frame_completes_while_the_refresh_another_frame_asked_for_is_open(tmp_path: Path) -> None:
    """A frame asks for the grant rebuild and goes on: no bot's frame waits for another account's Matrix reads."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    other = _agent_bot(tmp_path, agent_name="other", orchestrator=orchestrator)
    answered = asyncio.Event()
    orchestrator.refresh_agent_reply_memberships.side_effect = answered.wait
    completed: list[str] = []

    try:
        with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
            for frame_bot in (bot, other):
                frame_bot.client = make_matrix_client_mock(user_id=frame_bot.agent_user.user_id)
                frame_bot.mark_sync_loop_started()
                await asyncio.wait_for(_complete_frame(frame_bot), SETTLE_TIMEOUT_SECONDS)
                completed.append(frame_bot.agent_name)
                await asyncio.sleep(0)

        assert completed == ["router", "other"]
        orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()
        assert orchestrator.handle_bot_ready.await_count == 2
    finally:
        answered.set()
        await asyncio.wait_for(orchestrator.agent_reply_membership_sync.close(), SETTLE_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_router_limited_sync_invalidates_then_rebuilds_room_backed_grants(tmp_path: Path) -> None:
    """A limited timeline must discard its uncertain baseline before taking a new authoritative snapshot."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.mark_sync_loop_started()
    _open_observer(bot)
    admission = _history_loss_admission("!project:localhost")
    bot._before_ingestion_admission(admission, TimelineEventProvenance.LIVE)

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
    ):
        await _complete_frame(bot)

    _invalidations(orchestrator).assert_called_once_with(
        bot.config,
        bot.matrix_id.full_id,
        reason="uncertain_sync_response",
    )
    await asyncio.sleep(0)  # the frame asked for the refresh, which runs in a task of its own
    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


def test_router_limited_sync_invalidates_before_timeline_admission(tmp_path: Path) -> None:
    """The Matrix client's pre-admission hook must fail room grants closed immediately."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    _open_observer(bot)
    admission = _history_loss_admission("!project:localhost")

    bot._before_ingestion_admission(admission, TimelineEventProvenance.LIVE)

    _invalidations(orchestrator).assert_called_once_with(
        bot.config,
        bot.matrix_id.full_id,
        reason="uncertain_sync_response",
    )


@pytest.mark.asyncio
async def test_router_departure_revokes_grant_before_timeline_admission(
    tmp_path: Path,
) -> None:
    """A final router departure must close its grant before sibling timeline events are admitted."""
    room_id = "!grant:localhost"
    second_room_id = "!second-grant:localhost"
    sender_id = "@alice:localhost"
    bot, _orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant", "second-grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.add_room("second-grant", second_room_id, "#second-grant:localhost", "Second Grant")
    save_matrix_state(state, bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id, second_room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(bot.agent_user.user_id, None, None),
            nio.RoomMember(sender_id, None, None),
        ],
        room_id=room_id,
    )
    await refresh_memberships(bot._runtime_view.agent_reply_memberships, bot.config, bot.runtime_paths, client)
    departure = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    second_departure = _validated_reported_membership_admission(
        bot,
        second_room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )

    bot._before_ingestion_admission(departure, TimelineEventProvenance.LIVE)
    bot._before_ingestion_admission(second_departure, TimelineEventProvenance.LIVE)
    await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)

    assert not bot._runtime_view.agent_reply_memberships.is_allowed(
        sender_id,
        ["grant", "second-grant"],
        bot.config,
        bot.runtime_paths,
    )
    assert bot._runtime_view.agent_reply_memberships.needs_refresh(bot.config)


@pytest.mark.asyncio
async def test_router_authoritative_departure_revokes_grant_before_membership_fence(
    tmp_path: Path,
) -> None:
    """A leave section must synchronously fail the grant room closed."""
    room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, call_manager = await _router_with_call_manager(tmp_path)
    client = _grant_room_client(bot, room_id, sender_id)
    bot.client = client
    index = bot._runtime_view.agent_reply_memberships
    await refresh_memberships(index, bot.config, bot.runtime_paths, client)
    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    bot._before_ingestion_admission(admission, TimelineEventProvenance.LIVE)

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert index.needs_refresh(bot.config)

    principal = bot.journal_principal()
    await principal.load_or_create_ingestion_consumer(new_generation=_CONSUMER_GENERATION)
    await principal.bind_ingestion_stream(
        generation=_CONSUMER_GENERATION,
        stream_id=_STREAM_ID,
    )
    assert (await principal.admit_ingestion_batch(IngestionBatchAdmission(_STREAM_ID, 1, (admission,)))).record_facts[
        0
    ] == _FRESH_RECEIPT_FACTS

    call_manager.revoke_reply_authorization.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_router_leave_then_rejoin_in_one_sync_requires_grant_refresh(tmp_path: Path) -> None:
    """Any router continuity gap must require a fresh authoritative grant snapshot."""
    room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, call_manager = await _router_with_call_manager(tmp_path)
    client = _grant_room_client(bot, room_id, sender_id)
    bot.client = client
    index = bot._runtime_view.agent_reply_memberships
    await refresh_memberships(index, bot.config, bot.runtime_paths, client)

    departure = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    rejoin = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="leave",
        membership="join",
        previous_epoch=1,
    )

    bot._before_ingestion_admission(departure, TimelineEventProvenance.LIVE)
    bot._before_ingestion_admission(rejoin, TimelineEventProvenance.LIVE)
    await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert index.needs_refresh(bot.config)
    call_manager.revoke_reply_authorization.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_router_final_invite_revokes_grant_before_timeline_admission(
    tmp_path: Path,
) -> None:
    """A final invite means the router can no longer vouch for the old roster."""
    room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, _orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    client = _grant_room_client(bot, room_id, sender_id)
    index = bot._runtime_view.agent_reply_memberships
    await refresh_memberships(index, bot.config, bot.runtime_paths, client)
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="invite",
        previous_epoch=0,
    )

    bot._before_ingestion_admission(admission, TimelineEventProvenance.LIVE)
    await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert index.needs_refresh(bot.config)


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["leave", "ban", "invite"])
async def test_grant_user_revocation_waits_for_durable_live_admission(
    tmp_path: Path,
    membership: str,
) -> None:
    """An ordinary revocation takes effect at its accepted durable LIVE event."""
    grant_room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, call_manager = await _router_with_call_manager(tmp_path)
    client = _grant_room_client(bot, grant_room_id, sender_id)
    index = bot._runtime_view.agent_reply_memberships
    await refresh_memberships(index, bot.config, bot.runtime_paths, client)
    member_event = _member_event(
        "$membership",
        user_id=sender_id,
        membership=membership,
        ts=2,
    )
    admission = _validated_timeline_admission(
        bot,
        grant_room_id,
        member_event,
    )

    bot._before_ingestion_admission(admission, TimelineEventProvenance.LIVE)
    await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert not index.needs_refresh(bot.config)
    call_manager.revoke_reply_authorization.assert_not_awaited()

    await bot._after_ingestion_admission(
        admission,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    call_manager.revoke_reply_authorization.assert_awaited_once_with()
    assert await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)
    call_manager.reconcile_reply_authorization.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_grant_user_join_waits_for_durable_timeline_admission(
    tmp_path: Path,
) -> None:
    """The pre-admission scan must never grant access from a positive transition."""
    room_id = "!grant:localhost"
    sender_id = "@bob:localhost"
    bot, call_manager = await _router_with_call_manager(tmp_path)
    client = _grant_room_client(bot, room_id)
    index = bot._runtime_view.agent_reply_memberships
    await refresh_memberships(index, bot.config, bot.runtime_paths, client)
    member_event = _member_event("$join", user_id=sender_id, membership="join", ts=1)
    admission = _validated_timeline_admission(
        bot,
        room_id,
        member_event,
    )

    bot._before_ingestion_admission(admission, TimelineEventProvenance.LIVE)
    await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    call_manager.revoke_reply_authorization.assert_not_awaited()

    await bot._after_ingestion_admission(
        admission,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)


@pytest.mark.asyncio
async def test_recovered_membership_does_not_grant_reply_access(tmp_path: Path) -> None:
    """Recovered membership history must not grant authority without a current roster."""
    room_id = "!grant:localhost"
    sender_id = "@bob:localhost"
    bot, call_manager = await _router_with_call_manager(tmp_path)
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    save_matrix_state(state, bot.runtime_paths)
    index = bot._runtime_view.agent_reply_memberships
    admission = _validated_timeline_admission(
        bot,
        room_id,
        _member_event("$join", user_id=sender_id, membership="join", ts=1),
    )

    await bot._after_ingestion_admission(
        admission,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.RECOVERED,
    )

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)
    call_manager.reconcile_reply_authorization.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_activity_tracks_fresh_receipts_and_ignores_unsettled_replay(tmp_path: Path) -> None:
    """A new receipt marks projected room activity once even when the Matrix event is a duplicate."""
    bot = _agent_bot(tmp_path)
    room_id = "!room:localhost"
    activity = MagicMock()
    cast("ReadyRuntime", bot.orchestrator).observe_room_activity = activity
    admission = _validated_timeline_admission(
        bot,
        room_id,
        {
            "content": {"body": "hello", "msgtype": "m.text"},
            "event_id": "$message",
            "origin_server_ts": 1,
            "sender": "@alice:localhost",
            "type": "m.room.message",
        },
    )

    await bot._after_ingestion_admission(
        admission,
        _FRESH_RECEIPT_FACTS,
        TimelineEventProvenance.LIVE,
    )
    await bot._after_ingestion_admission(
        admission,
        _REPLAY_FACTS,
        TimelineEventProvenance.LIVE,
    )

    activity.assert_called_once_with(room_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["leave", "ban"])
async def test_grant_user_join_then_revoke_applies_in_durable_order(
    tmp_path: Path,
    membership: str,
) -> None:
    """Accepted LIVE transitions update grants in their durable event order."""
    room_id = "!grant:localhost"
    sender_id = "@bob:localhost"
    bot, call_manager = await _router_with_call_manager(tmp_path)
    client = _grant_room_client(bot, room_id)
    index = bot._runtime_view.agent_reply_memberships
    await refresh_memberships(index, bot.config, bot.runtime_paths, client)
    join_event = _member_event("$join", user_id=sender_id, membership="join", ts=1)
    revoke_event = _member_event("$revoke", user_id=sender_id, membership=membership, ts=2)
    join_admission = _validated_timeline_admission(
        bot,
        room_id,
        join_event,
    )
    revoke_admission = _validated_timeline_admission(
        bot,
        room_id,
        revoke_event,
    )

    bot._before_ingestion_admission(join_admission, TimelineEventProvenance.LIVE)
    bot._before_ingestion_admission(revoke_admission, TimelineEventProvenance.LIVE)
    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)

    await bot._after_ingestion_admission(
        join_admission,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.LIVE,
    )
    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    await bot._after_ingestion_admission(
        revoke_admission,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert call_manager.revoke_reply_authorization.await_count == 2

    later_join = _validated_timeline_admission(
        bot,
        room_id,
        _member_event("$later-join", user_id=sender_id, membership="join", ts=3),
    )
    await bot._after_ingestion_admission(
        later_join,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert call_manager.revoke_reply_authorization.await_count == 3

    await bot._after_ingestion_admission(
        later_join,
        _REPLAY_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert call_manager.revoke_reply_authorization.await_count == 3
    assert await wait_for_background_tasks(timeout=SETTLE_TIMEOUT_SECONDS, owner=bot._runtime_view)
    assert call_manager.reconcile_reply_authorization.await_count == 3


@pytest.mark.asyncio
async def test_call_manager_registers_call_and_room_membership_callbacks(tmp_path: Path) -> None:
    """Call admission is rechecked for call-state and underlying room-member changes."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)
    call_manager = MagicMock()

    with patch("mindroom.bot.maybe_build_call_manager", new=AsyncMock(return_value=call_manager)):
        await bot._register_call_manager_callbacks(client)

    assert bot._call_manager is call_manager
    client.add_event_callback.assert_not_called()
    client.add_to_device_callback.assert_called_once_with(ANY, AuthenticatedToDeviceEvent)


@pytest.mark.asyncio
async def test_published_config_updates_existing_call_manager(tmp_path: Path) -> None:
    """An unchanged call bot should observe authorization-only hot reloads."""
    bot = _agent_bot(tmp_path)
    call_manager = await start_call_manager(bot)
    new_config = _config(tmp_path)

    bot.orchestrator.config = new_config
    bot.apply_published_config()

    call_manager.update_config.assert_called_once_with(new_config)


@pytest.mark.asyncio
async def test_call_manager_room_callbacks_run_without_legacy_delivery_provenance(
    tmp_path: Path,
) -> None:
    """Owned settlement already gates callback eligibility before call fanout."""
    bot = _agent_bot(tmp_path)
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    membership_event = nio.RoomMemberEvent.from_dict(
        {
            "event_id": "$historical-member",
            "sender": "@owner:localhost",
            "origin_server_ts": 1,
            "type": "m.room.member",
            "state_key": "@other-member:localhost",
            "content": {"membership": "leave"},
        },
    )
    assert isinstance(membership_event, nio.RoomMemberEvent)
    call_event = nio.UnknownEvent(
        {
            "event_id": "$historical-call",
            "sender": "@owner:localhost",
            "origin_server_ts": 1,
        },
        "org.matrix.msc3401.call.member",
    )

    call_manager = await start_call_manager(bot)

    await bot._journal_dispatcher.callbacks.on_room_lifecycle(room, membership_event)
    rtc_callback = bot._journal_dispatcher.callbacks.on_rtc
    assert rtc_callback is not None
    await rtc_callback(room, call_event)

    call_manager.on_room_membership_event.assert_awaited_once_with(room, membership_event)
    call_manager.on_room_event.assert_awaited_once_with(room, call_event)


@pytest.mark.asyncio
async def test_room_membership_cleanup_registers_without_call_runtime(tmp_path: Path) -> None:
    """Persisted ad-hoc ownership is cleaned even when voice dependencies are absent."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)

    with patch("mindroom.bot.maybe_build_call_manager", new=AsyncMock(return_value=None)):
        await bot._register_call_manager_callbacks(client)

    assert bot._call_manager is None
    client.add_event_callback.assert_not_called()
    client.add_to_device_callback.assert_not_called()


@pytest.mark.asyncio
async def test_call_manager_reads_invited_rooms_of_running_call_agents_under_the_published_config(
    tmp_path: Path,
) -> None:
    """The running call manager reads the invited-room record of each calls-enabled agent the runtime runs, live."""
    bot = _agent_bot(tmp_path)
    other = _agent_bot(tmp_path, agent_name="other", orchestrator=cast("ReadyRuntime", bot.orchestrator))
    assert dict(bot.orchestrator.agent_bots) == {"code": bot, "other": other}
    build = AsyncMock(return_value=MagicMock(spec=CallManager))
    with patch("mindroom.bot.maybe_build_call_manager", new=build):
        await bot._register_call_manager_callbacks(MagicMock(spec=nio.AsyncClient))
    invited_call_rooms = build.await_args.kwargs["get_invited_rooms_by_agent"]
    assert invited_call_rooms() == {}

    config = bot.config.model_copy(deep=True)
    config.agents["code"].accept_invites = True
    config.agents["other"] = AgentConfig(display_name="Other", accept_invites=True)
    config.agents["absent"] = AgentConfig(display_name="Absent", accept_invites=True)
    config.calls = CallsConfig(
        enabled=True,
        profiles={
            "voice": RealtimeCallProfile(
                backend="realtime",
                model="gpt-realtime",
                credentials_service="openai",
                voice="marin",
            ),
        },
        agents={"code": "voice", "other": "voice", "absent": "voice"},
    )
    bot.orchestrator.config = config
    remember_invited_room(invited_rooms_path(bot.runtime_paths, "absent"), "!code-call:localhost")
    remember_invited_room(invited_rooms_path(bot.runtime_paths, "code"), "!code-call:localhost")
    remember_invited_room(invited_rooms_path(bot.runtime_paths, "other"), "!other-call:localhost")

    assert invited_call_rooms() == {
        "code": frozenset({"!code-call:localhost"}),
        "other": frozenset({"!other-call:localhost"}),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backend_available", [False, True])
async def test_presence_uses_voice_backend_availability(
    tmp_path: Path,
    backend_available: bool,
) -> None:
    """Presence advertises calls only when the constructed manager can answer them."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    call_manager = await start_call_manager(bot)
    call_manager.voice_backend_available = backend_available

    with (
        patch("mindroom.bot.build_agent_status_message", return_value="status") as build_status,
        patch("mindroom.bot.set_presence_status", new_callable=AsyncMock) as set_presence,
    ):
        await bot.refresh_presence()

    build_status.assert_called_once_with(
        bot.agent_name,
        bot.config,
        config_plugins(bot.config, bot.runtime_paths).tools,
        voice_calls_available=backend_available,
    )
    set_presence.assert_awaited_once_with(bot.client, "status")


def _member_event(event_id: str, *, user_id: str, membership: str, ts: int) -> dict[str, object]:
    """Return one member event setting a user's membership in a room."""
    return {
        "content": {"membership": membership},
        "event_id": event_id,
        "origin_server_ts": ts,
        "sender": "@admin:localhost",
        "state_key": user_id,
        "type": "m.room.member",
    }


@pytest.mark.asyncio
async def test_replayed_truncated_leave_cannot_fence_a_rejoined_membership(tmp_path: Path) -> None:
    """A stale batch cannot invalidate a rejoin when the leave had no event."""
    bot = _agent_bot(tmp_path)
    room_id = "!departed:localhost"
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    principal = bot.journal_principal()
    await principal.load_or_create_ingestion_consumer(new_generation=_CONSUMER_GENERATION)
    await principal.bind_ingestion_stream(
        generation=_CONSUMER_GENERATION,
        stream_id=_STREAM_ID,
    )

    assert (await principal.admit_ingestion_batch(IngestionBatchAdmission(_STREAM_ID, 1, (admission,)))).record_facts[
        0
    ] == _FRESH_RECEIPT_FACTS
    await admit_room_membership(bot.journal_principal(), room_id, "join")
    with pytest.raises(IngestionBatchSequenceError):
        await principal.admit_ingestion_batch(IngestionBatchAdmission(_STREAM_ID, 1, (admission,)))

    assert await membership_position(principal, room_id) == RoomMembershipPosition("join", 1)


@pytest.mark.asyncio
async def test_replayed_departure_cannot_leave_a_confirmed_join_fenced(tmp_path: Path) -> None:
    """A duplicated old leave report, redelivered after the rejoin it preceded, cannot invalidate that rejoin."""
    bot = _agent_bot(tmp_path)
    room_id = "!rejoined:localhost"
    departure = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
    )
    rejoin = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="leave",
        membership="join",
        previous_epoch=1,
    )
    batch = IngestionBatchAdmission(_STREAM_ID, 1, (departure, rejoin))
    principal = bot.journal_principal()
    await principal.load_or_create_ingestion_consumer(new_generation=_CONSUMER_GENERATION)
    await principal.bind_ingestion_stream(
        generation=_CONSUMER_GENERATION,
        stream_id=_STREAM_ID,
    )

    assert (await principal.admit_ingestion_batch(batch)).record_facts == (_FRESH_RECEIPT_FACTS, _FRESH_RECEIPT_FACTS)
    after_rejoin = await membership_position(principal, room_id)
    assert after_rejoin == RoomMembershipPosition("join", 1)

    assert (await principal.admit_ingestion_batch(batch)).record_facts == (_REPLAY_FACTS, _REPLAY_FACTS)
    assert await membership_position(principal, room_id) == after_rejoin


@pytest.mark.asyncio
async def test_bot_ready_fires_only_once(tmp_path: Path) -> None:
    """bot:ready should fire only on the first sync, not on subsequent syncs."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired_count = 0

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        nonlocal fired_count
        fired_count += 1

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)
        await _complete_frame(bot)
        await _complete_frame(bot)

    assert fired_count == 1


@pytest.mark.asyncio
async def test_orchestrator_ready_notification_retries_after_failure(tmp_path: Path) -> None:
    """A transient readiness failure must retry after the first sync was recorded."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    orchestrator = cast("ReadyRuntime", bot.orchestrator)
    orchestrator.handle_bot_ready = AsyncMock(side_effect=[RuntimeError("transient recovery failure"), None])

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(RuntimeError, match="transient recovery failure"),
    ):
        await _complete_frame(bot)

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert bot.first_sync_complete
    assert orchestrator.handle_bot_ready.await_count == 2


@pytest.mark.asyncio
async def test_bot_ready_fires_after_agent_started(tmp_path: Path) -> None:
    """bot:ready must fire after agent:started since it depends on sync being established."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    event_order: list[str] = []

    @hook(EVENT_AGENT_STARTED)
    async def on_started(_ctx: AgentLifecycleContext) -> None:
        event_order.append("agent:started")

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        event_order.append("bot:ready")

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
        [_plugin("test-plugin", [on_started, on_ready])],
    )

    # agent:started fires during start() setup
    await bot._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)

    # bot:ready fires on first sync
    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert event_order == ["agent:started", "bot:ready"]


@pytest.mark.asyncio
async def test_bot_ready_hook_can_send_messages(tmp_path: Path) -> None:
    """Hooks on bot:ready should be able to send messages through the bound sender."""
    bot = _agent_bot(tmp_path, agent_name="router")
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)

    captured_content: dict[str, object] = {}

    async def mock_send(_client: object, _room_id: str, content: dict[str, object], **_kwargs: object) -> object:
        captured_content.update(content)
        return delivered_matrix_event("$hook-event", content)

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        await ctx.send_message("!room:localhost", "I'm ready!")

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.hooks.sender.send_matrix_message", side_effect=mock_send),
    ):
        await _complete_frame(bot)

    assert captured_content[SOURCE_KIND_KEY] == "hook"
    assert captured_content["com.mindroom.hook_source"] == "test-plugin:bot:ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", [EVENT_AGENT_STARTED, EVENT_AGENT_STOPPED])
async def test_lifecycle_hooks_query_and_put_room_state_through_the_bot(tmp_path: Path, event_name: str) -> None:
    """Lifecycle hook contexts carry the bot's room-state helpers; the router fallback is tested on HookContextSupport."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.room_get_state_event.return_value = MagicMock(content={"name": "Agent Lobby"})

    results: list[tuple[dict[str, object] | None, bool]] = []

    @hook(event_name)
    async def on_lifecycle(ctx: AgentLifecycleContext) -> None:
        query_result = await ctx.query_room_state("!room:localhost", "m.room.name", "")
        put_result = await ctx.put_room_state(
            "!room:localhost",
            "com.mindroom.thread.tags",
            "$thread",
            {"tags": {"queued": True}},
        )
        results.append((query_result, put_result))

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_lifecycle])])

    await bot._emit_agent_lifecycle_event(event_name)

    assert results == [({"name": "Agent Lobby"}, True)]
    bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    bot.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$thread",
    )


@pytest.mark.asyncio
async def test_bot_ready_does_not_fire_during_sync_shutdown(tmp_path: Path) -> None:
    """bot:ready must not fire if sync is shutting down."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired = False

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        nonlocal fired
        fired = True

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])
    bot._runtime_view.begin_draining()

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert not fired


@pytest.mark.asyncio
async def test_bot_ready_fires_after_shutdown_clears(tmp_path: Path) -> None:
    """bot:ready must fire after shutdown suppresses and then clears (restart recovery)."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired_count = 0

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        nonlocal fired_count
        fired_count += 1

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        # First sync arrives during shutdown — bot:ready suppressed
        bot._runtime_view.begin_draining()
        await _complete_frame(bot)
        assert fired_count == 0

        # Shutdown clears (restart)
        bot.mark_sync_loop_started()

        # Next sync — bot:ready must fire now
        await _complete_frame(bot)
        assert fired_count == 1

        # Subsequent syncs must not re-fire
        await _complete_frame(bot)
        assert fired_count == 1


@pytest.mark.asyncio
async def test_bot_ready_context_has_correct_entity_info(tmp_path: Path) -> None:
    """bot:ready context should carry the agent's name, type, and rooms."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    captured_ctx: list[AgentLifecycleContext] = []

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        captured_ctx.append(ctx)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert len(captured_ctx) == 1
    ctx = captured_ctx[0]
    assert ctx.entity_name == "code"
    assert ctx.matrix_user_id == "@mindroom_code:localhost"
    assert "!room:localhost" in ctx.rooms
    assert ctx.joined_room_ids == ("!room:localhost",)


@pytest.mark.asyncio
async def test_agent_stopped_hooks_run_while_the_stopping_generation_is_still_live(tmp_path: Path) -> None:
    """agent:stopped hooks see their registry live and the stop reason; once stop returns, that generation's hooks end."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    seen: list[tuple[str | None, bool]] = []
    contexts: list[AgentLifecycleContext] = []

    @hook(EVENT_AGENT_STOPPED)
    async def on_stopped(ctx: AgentLifecycleContext) -> None:
        seen.append((ctx.stop_reason, ctx.is_active()))
        contexts.append(ctx)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_stopped])])

    await bot.stop(shutdown_intent=SYNC_RESTART_SHUTDOWN)

    assert seen == [("restart", True)]
    assert not contexts[0].is_active()
    assert not bot.hook_registry.has_hooks(EVENT_AGENT_STOPPED)


@pytest.mark.asyncio
async def test_lifecycle_context_preserves_configured_rooms_and_exposes_joined_room_ids(tmp_path: Path) -> None:
    """Lifecycle hooks should keep configured rooms separate from resolved Matrix room IDs."""
    bot = _agent_bot(tmp_path)
    bot.config.agents["code"].rooms = ["lobby", "!room:localhost"]
    bot.rooms = ["!room:localhost"]
    bot.client = AsyncMock()

    captured_ctx: list[AgentLifecycleContext] = []

    @hook(EVENT_AGENT_STARTED)
    async def on_started(ctx: AgentLifecycleContext) -> None:
        captured_ctx.append(ctx)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_started])])

    await bot._emit_agent_lifecycle_event(EVENT_AGENT_STARTED)

    assert len(captured_ctx) == 1
    assert captured_ctx[0].rooms == ("lobby", "!room:localhost")
    assert captured_ctx[0].joined_room_ids == ("!room:localhost",)


@pytest.mark.asyncio
async def test_bot_ready_context_includes_joined_rooms_from_first_sync(tmp_path: Path) -> None:
    """bot:ready should expose rooms learned from the first sync response."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    bot.client.rooms = {"!joined:localhost": MagicMock()}

    captured_ctx: list[AgentLifecycleContext] = []

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        captured_ctx.append(ctx)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert len(captured_ctx) == 1
    assert captured_ctx[0].rooms == ("!room:localhost",)
    assert captured_ctx[0].joined_room_ids == ("!room:localhost", "!joined:localhost")


def _two_bot_runtime(tmp_path: Path) -> tuple[ReadyRuntime, AgentBot, AgentBot]:
    """Return a runtime with no owner account and two agents' bots, each with its own joined rooms."""
    code = _agent_bot(tmp_path)
    orchestrator = cast("ReadyRuntime", code.orchestrator)
    general = _agent_bot(tmp_path, agent_name="general", orchestrator=orchestrator)
    for bot, joined in ((code, "!code-only:localhost"), (general, "!general-only:localhost")):
        bot.client = AsyncMock()
        bot.client.rooms = {"!room:localhost": MagicMock(), joined: MagicMock()}
    return orchestrator, code, general


async def _emit_service_bot_ready(orchestrator: ReadyRuntime) -> None:
    """Capture and emit the runtime's one service bot:ready, which a hook listens for."""
    emission = service_bot_ready_emission(orchestrator)
    assert emission is not None
    await emission()


@pytest.mark.asyncio
async def test_service_bot_ready_fires_once_per_start(tmp_path: Path) -> None:
    """A service-scoped bot:ready hook runs on the runtime's one emission, and on no bot's own bot:ready."""
    orchestrator, code, general = _two_bot_runtime(tmp_path)
    calls: list[tuple[str, str]] = []

    @hook(EVENT_BOT_READY, agents=(ROUTER_AGENT_NAME,))
    async def once_per_process(ctx: AgentLifecycleContext) -> None:
        calls.append(("service", ctx.entity_name))

    @hook(EVENT_BOT_READY)
    async def per_bot(ctx: AgentLifecycleContext) -> None:
        calls.append(("agent", ctx.entity_name))

    orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
        [_plugin("test-plugin", [once_per_process, per_bot])],
    )

    await code._emit_agent_lifecycle_event(EVENT_BOT_READY)
    await _emit_service_bot_ready(orchestrator)
    await general._emit_agent_lifecycle_event(EVENT_BOT_READY)

    assert calls == [("agent", "code"), ("service", ROUTER_AGENT_NAME), ("agent", "general")]


@pytest.mark.asyncio
async def test_service_context_lists_union_of_joined_rooms(tmp_path: Path) -> None:
    """The service bot:ready names the router entity, every configured room, and only the rooms a bot is in."""
    orchestrator, code, _general = _two_bot_runtime(tmp_path)
    # A configured room given by its ID that no bot has joined is configured, not joined.
    orchestrator.config.agents["code"].rooms.append("!unjoined:localhost")
    code.rooms.append("!unjoined:localhost")
    captured: list[AgentLifecycleContext] = []

    @hook(EVENT_BOT_READY, scope="service")
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        captured.append(ctx)

    orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    await _emit_service_bot_ready(orchestrator)

    [ctx] = captured
    assert (ctx.entity_name, ctx.entity_type) == (ROUTER_AGENT_NAME, "router")
    assert ctx.rooms == ("!room:localhost", "!unjoined:localhost")
    assert ctx.joined_room_ids == ("!room:localhost", "!code-only:localhost", "!general-only:localhost")
    assert ctx.is_active()
