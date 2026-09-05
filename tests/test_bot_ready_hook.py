"""Tests for the bot:ready lifecycle hook event."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID

import nio
import pytest
from nio.ingest import (
    EventRecord,
    RecordKind,
    RecordOrigin,
    TimelineEventProvenance,
    TransportKind,
)
from nio.ingest.serialization import batch_from_records
from nio.store._sync_journal_values import _FrameCompletion

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.calls import CallsConfig, RealtimeCallProfile
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME, SOURCE_KIND_KEY
from mindroom.event_journal import (
    AdmissionFacts,
    IngestionBatchAdmission,
    IngestionRecordDisposition,
)
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    AgentLifecycleContext,
    HookRegistry,
    hook,
)
from mindroom.matrix.durable_ingestion import validate_ingestion_batch
from mindroom.matrix.state import MatrixState
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    install_call_manager_mock,
    install_runtime_journal_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )


def _agent_bot(tmp_path: Path, *, agent_name: str = "code") -> AgentBot:
    config = _config(tmp_path)
    memberships = AgentReplyMembershipIndex()
    return install_runtime_journal_support(
        AgentBot(
            agent_user=AgentMatrixUser(
                agent_name=agent_name,
                password=TEST_PASSWORD,
                display_name=agent_name.title(),
                user_id=f"@mindroom_{agent_name}:localhost",
            ),
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!room:localhost"],
            agent_reply_memberships=memberships,
            agent_reply_membership_sync=(
                AgentReplyMembershipSync(memberships) if agent_name == ROUTER_AGENT_NAME else None
            ),
        ),
    )


def _router_bot_with_orchestrator(tmp_path: Path) -> tuple[AgentBot, MagicMock]:
    """Return a router bot wired to a narrow mocked orchestrator lifecycle."""
    bot = _agent_bot(tmp_path, agent_name="router")
    orchestrator = MagicMock()
    orchestrator.invalidate_agent_reply_memberships = MagicMock(
        side_effect=lambda *, reason: bot._router_reply_membership_sync.invalidate(bot.config, reason=reason),
    )
    orchestrator.refresh_agent_reply_memberships = AsyncMock()
    orchestrator.revoke_reply_authorized_calls = AsyncMock()
    orchestrator.reconcile_reply_authorized_calls = AsyncMock()
    orchestrator.reconcile_pending_invites = AsyncMock()
    orchestrator.handle_bot_ready = AsyncMock()
    bot.orchestrator = orchestrator
    return bot, orchestrator


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
_DEVICE_ID = "READY-HOOK-DEVICE"
_FRESH_SEMANTIC_FACTS = AdmissionFacts(receipt_new=True, semantic_event_new=True)
_FRESH_RECEIPT_FACTS = AdmissionFacts(receipt_new=True, semantic_event_new=False)
_REPLAY_FACTS = AdmissionFacts(receipt_new=False, semantic_event_new=False)


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _validated_timeline_admission(
    bot: AgentBot,
    room_id: str,
    event: dict[str, object],
    *,
    transport: TransportKind,
    sequence: int,
) -> IngestionBatchAdmission:
    """Convert one exact current-nio LIVE record into MindRoom's admission value."""
    event_id = event["event_id"]
    assert isinstance(event_id, str)
    record = EventRecord(
        f"timeline:{sequence}:{event_id}",
        RecordKind.TIMELINE,
        RecordOrigin(transport, 1, 1, sequence),
        room_id,
        0,
        sequence,
        event_id,
        TimelineEventProvenance.LIVE,
        _canonical_json(event),
        None,
    )
    batch = batch_from_records(
        account_id=bot.matrix_id.full_id,
        device_id=_DEVICE_ID,
        consumer_generation=_CONSUMER_GENERATION,
        stream_id=_STREAM_ID,
        sequence=sequence,
        created_revision=sequence + 1,
        records=(record,),
    )
    return validate_ingestion_batch(
        batch,
        account_id=bot.matrix_id.full_id,
        device_id=_DEVICE_ID,
    )


def _validated_reported_membership_admission(
    bot: AgentBot,
    room_id: str,
    *,
    previous_membership: str | None,
    membership: str,
    previous_epoch: int,
    transport: TransportKind = TransportKind.CLASSIC,
    sequence: int = 0,
    event_id: str | None = None,
) -> IngestionBatchAdmission:
    """Convert one exact current-nio reported room transition into an admission."""
    membership_epoch = previous_epoch + int(previous_membership == "join" and membership != "join")
    source_kind = "section" if event_id is None else "timeline"
    record_id = f"membership:{sequence}:{event_id or membership}"
    source = {
        "event_id": event_id,
        "membership": membership,
        "membership_epoch": membership_epoch,
        "membership_provenance": "reported",
        "previous_membership": previous_membership,
        "previous_membership_epoch": previous_epoch,
        "source_kind": source_kind,
        "source_record_id": None if event_id is None else record_id,
        "timeline_provenance": None if event_id is None else "live",
    }
    record = EventRecord(
        record_id,
        RecordKind.ROOM_LIFECYCLE,
        RecordOrigin(transport, 1, 1, sequence),
        room_id,
        membership_epoch,
        sequence,
        None,
        None,
        _canonical_json(source),
        None,
    )
    batch = batch_from_records(
        account_id=bot.matrix_id.full_id,
        device_id=_DEVICE_ID,
        consumer_generation=_CONSUMER_GENERATION,
        stream_id=_STREAM_ID,
        sequence=sequence,
        created_revision=sequence + 1,
        records=(record,),
    )
    return validate_ingestion_batch(
        batch,
        account_id=bot.matrix_id.full_id,
        device_id=_DEVICE_ID,
    )


def _history_loss_admission(room_id: str) -> IngestionBatchAdmission:
    """Return the normalized durable fact emitted for an unresolved room gap."""
    return IngestionBatchAdmission(
        schema_version=1,
        consumer_generation=_CONSUMER_GENERATION,
        stream_id=_STREAM_ID,
        sequence=0,
        sha256=b"\0" * 32,
        record_id="history-loss",
        disposition=IngestionRecordDisposition.HISTORY_LOSS,
        source=None,
        room_id=room_id,
        previous_membership=None,
        membership=None,
        previous_membership_epoch=None,
        membership_epoch=None,
        event=None,
        projected=None,
    )


def _plugin(name: str, callbacks: list[object]) -> object:
    return type(
        "PluginStub",
        (),
        {
            "name": name,
            "discovered_hooks": tuple(callbacks),
            "entry_config": PluginEntryConfig(path=f"./plugins/{name}"),
            "plugin_order": 0,
        },
    )()


async def _complete_frame(bot: AgentBot, index: int = 0) -> None:
    """Drive runtime side effects through the durable completion owner."""
    await bot._on_ingestion_frame_completion(
        _FrameCompletion(
            UUID(f"10000000-0000-4000-8000-{index + 1:012d}"),
            TransportKind.CLASSIC,
            0,
            index,
            index * 2 + 1,
            index * 2 + 2,
        ),
    )


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
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path)
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
    orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

    recovery = asyncio.create_task(orchestrator._recover_ready_turn_journal_events())
    await router_recovery_started.wait()
    await asyncio.sleep(0)
    try:
        assert router_released.is_set()
        assert code_released.is_set()
    finally:
        finish_router_recovery.set()
        await recovery


@pytest.mark.asyncio
async def test_bot_ready_releases_later_ready_bot_while_serial_recovery_is_blocked(tmp_path: Path) -> None:
    """A later first sync must release replay without waiting for an earlier drain."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path)
    router_bot = MagicMock()
    code_bot = MagicMock()
    router_bot.agent_name = "router"
    code_bot.agent_name = "code"
    router_bot.running = code_bot.running = True
    router_bot.first_sync_complete = True
    code_bot.first_sync_complete = False
    router_recovery_started = asyncio.Event()
    finish_router_recovery = asyncio.Event()
    code_released = asyncio.Event()

    async def recover_router() -> None:
        router_recovery_started.set()
        await finish_router_recovery.wait()

    router_bot.recover_pending_turn_journal_events = AsyncMock(side_effect=recover_router)
    code_bot.recover_pending_turn_journal_events = AsyncMock()
    code_bot.release_pending_turn_journal_replay = code_released.set
    orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

    orchestrator._schedule_ready_turn_dispatch_recovery()
    await router_recovery_started.wait()
    try:
        code_bot.first_sync_complete = True
        await orchestrator.handle_bot_ready(code_bot)

        assert code_released.is_set()
    finally:
        finish_router_recovery.set()
        await wait_for_background_tasks(
            timeout=1.0,
            owner=orchestrator._dispatch_recovery_task_owner,
        )


@pytest.mark.asyncio
async def test_fleet_turn_recovery_reloads_current_bot_before_each_serial_drain(tmp_path: Path) -> None:
    """A reload during an earlier drain must not recover a retired bot generation."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.config = _config(tmp_path)
    router_bot = MagicMock()
    retired_code_bot = MagicMock()
    replacement_code_bot = MagicMock()
    for bot in (router_bot, retired_code_bot, replacement_code_bot):
        bot.running = True
        bot.first_sync_complete = True
        bot.release_pending_turn_journal_replay = MagicMock()
        bot.recover_pending_turn_journal_events = AsyncMock()
    router_recovery_started = asyncio.Event()
    finish_router_recovery = asyncio.Event()

    async def recover_router() -> None:
        router_recovery_started.set()
        await finish_router_recovery.wait()

    router_bot.recover_pending_turn_journal_events = AsyncMock(side_effect=recover_router)
    orchestrator.agent_bots = {"router": router_bot, "code": retired_code_bot}

    recovery = asyncio.create_task(orchestrator._recover_ready_turn_journal_events())
    await router_recovery_started.wait()
    orchestrator.agent_bots["code"] = replacement_code_bot
    finish_router_recovery.set()
    await recovery

    retired_code_bot.release_pending_turn_journal_replay.assert_called_once_with()
    retired_code_bot.recover_pending_turn_journal_events.assert_not_awaited()
    replacement_code_bot.recover_pending_turn_journal_events.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_bot_ready_fires_on_first_sync_response(tmp_path: Path) -> None:
    """bot:ready should fire when the first sync response is received."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    fired_events: list[str] = []

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        fired_events.append(ctx.event_name)

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert fired_events == ["bot:ready"]


@pytest.mark.asyncio
async def test_call_reconciliation_runs_once_per_sync_loop(tmp_path: Path) -> None:
    """Calls reconcile after each sync-loop's first successful response."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    call_manager = MagicMock()
    call_manager.reconcile_joined_rooms = AsyncMock()
    install_call_manager_mock(bot, call_manager)

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
    ):
        bot.mark_sync_loop_started()
        await _complete_frame(bot)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
        await _complete_frame(bot, 1)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        bot.mark_sync_loop_started()
        await _complete_frame(bot, 2)
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert call_manager.reconcile_joined_rooms.await_count == 2


def test_router_sync_loop_start_revokes_room_backed_grants(tmp_path: Path) -> None:
    """A reconnect generation must fail closed before its first response arrives."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)

    bot.mark_sync_loop_started()

    orchestrator.invalidate_agent_reply_memberships.assert_called_once_with(reason="sync_loop_started")


def test_router_prepared_startup_snapshot_survives_only_the_first_sync_start(tmp_path: Path) -> None:
    """The pre-sync snapshot must reach first admission, while reconnect still fails closed."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)

    bot.preserve_reply_memberships_on_next_sync_start()
    bot.mark_sync_loop_started()
    orchestrator.invalidate_agent_reply_memberships.assert_not_called()

    bot.mark_sync_loop_started()
    orchestrator.invalidate_agent_reply_memberships.assert_called_once_with(reason="sync_loop_started")


@pytest.mark.asyncio
async def test_router_prepared_startup_snapshot_refreshes_after_the_first_sync(tmp_path: Path) -> None:
    """The first response closes the gap between the pre-sync snapshot and receive start."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)

    bot.preserve_reply_memberships_on_next_sync_start()
    bot.mark_sync_loop_started()

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
    ):
        await _complete_frame(bot)

    orchestrator.invalidate_agent_reply_memberships.assert_not_called()
    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_router_first_response_refreshes_room_backed_grants(tmp_path: Path) -> None:
    """The first successful response in each receive generation rebuilds grants."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.mark_sync_loop_started()

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
    ):
        await _complete_frame(bot)

    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_router_limited_sync_invalidates_then_rebuilds_room_backed_grants(tmp_path: Path) -> None:
    """A limited timeline must discard its uncertain baseline before taking a new authoritative snapshot."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.mark_sync_loop_started()
    orchestrator.invalidate_agent_reply_memberships.reset_mock()
    orchestrator.refresh_agent_reply_memberships.reset_mock()
    admission = _history_loss_admission("!project:localhost")
    bot._before_ingestion_admission(admission)

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
    ):
        await _complete_frame(bot)

    orchestrator.invalidate_agent_reply_memberships.assert_called_once_with(reason="uncertain_sync_response")
    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


def test_router_limited_sync_invalidates_before_timeline_admission(tmp_path: Path) -> None:
    """The Matrix client's pre-admission hook must fail room grants closed immediately."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    admission = _history_loss_admission("!project:localhost")

    bot._before_ingestion_admission(admission)

    orchestrator.invalidate_agent_reply_memberships.assert_called_once_with(reason="uncertain_sync_response")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_router_departure_revokes_grant_before_timeline_admission(
    tmp_path: Path,
    transport: str,
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
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id, second_room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(bot.agent_user.user_id, None, None),
            nio.RoomMember(sender_id, None, None),
        ],
        room_id=room_id,
    )
    await bot._runtime_view.agent_reply_memberships.refresh(bot.config, bot.runtime_paths, client)
    departure = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
        transport=TransportKind(transport),
    )
    second_departure = _validated_reported_membership_admission(
        bot,
        second_room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
        transport=TransportKind(transport),
        sequence=1,
    )

    bot._before_ingestion_admission(departure)
    bot._before_ingestion_admission(second_departure)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert not bot._runtime_view.agent_reply_memberships.is_allowed(
        sender_id,
        ["grant", "second-grant"],
        bot.config,
        bot.runtime_paths,
    )
    assert bot._runtime_view.agent_reply_memberships.needs_refresh(bot.config)


@pytest.mark.asyncio
async def test_failed_membership_refresh_is_backed_off_between_sync_responses(tmp_path: Path) -> None:
    """An unavailable grant room must not cause one Matrix API refresh for every incoming message."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    bot._runtime_view.agent_reply_memberships.invalidate(bot.config, reason="test")

    with patch("mindroom.agent_reply_membership_sync.time.monotonic", return_value=100.0):
        await bot._refresh_agent_reply_memberships_if_needed()
        await bot._refresh_agent_reply_memberships_if_needed()

    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_repeated_membership_invalidation_preserves_refresh_backoff(tmp_path: Path) -> None:
    """Repeated uncertain responses must not bypass the bounded refresh retry delay."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    bot._runtime_view.agent_reply_memberships.invalidate(bot.config, reason="test")

    with patch("mindroom.agent_reply_membership_sync.time.monotonic", return_value=100.0):
        await bot._refresh_agent_reply_memberships_if_needed()
        bot._invalidate_agent_reply_memberships(reason="uncertain_sync_response")
        await bot._refresh_agent_reply_memberships_if_needed()

    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_detached_router_invalidation_during_post_refresh_effects_revokes_and_skips_positive(
    tmp_path: Path,
) -> None:
    """A detached router must reopen revocation before awaiting refresh effects."""
    room_id = "!grant:localhost"
    bot = _agent_bot(tmp_path, agent_name=ROUTER_AGENT_NAME)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(bot.agent_user.user_id, None, None)],
        room_id=room_id,
    )
    bot.client = client
    effects_started = asyncio.Event()
    release_effects = asyncio.Event()

    async def block_post_refresh_effects() -> None:
        effects_started.set()
        await release_effects.wait()

    bot.reconcile_pending_invites = AsyncMock()  # type: ignore[method-assign]
    bot.revoke_reply_authorized_calls = AsyncMock(side_effect=block_post_refresh_effects)  # type: ignore[method-assign]
    bot.schedule_reply_authorized_call_revocation = MagicMock()  # type: ignore[method-assign]
    bot.schedule_reply_authorized_call_reconciliation = MagicMock()  # type: ignore[method-assign]
    bot._invalidate_agent_reply_memberships(reason="initial_gap")
    bot.schedule_reply_authorized_call_revocation.reset_mock()

    refresh_task = asyncio.create_task(bot._refresh_agent_reply_memberships_if_needed())
    try:
        await asyncio.wait_for(effects_started.wait(), timeout=1.0)
        bot._invalidate_agent_reply_memberships(reason="uncertain_sync_response")
    finally:
        release_effects.set()
        await refresh_task

    bot.schedule_reply_authorized_call_revocation.assert_called_once_with()
    bot.schedule_reply_authorized_call_reconciliation.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_router_authoritative_departure_revokes_grant_before_membership_fence(
    tmp_path: Path,
    transport: str,
) -> None:
    """Classic and sliding leave sections must synchronously fail the grant room closed."""
    room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    orchestrator.revoke_reply_authorized_calls = AsyncMock()
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(bot.agent_user.user_id, None, None),
            nio.RoomMember(sender_id, None, None),
        ],
        room_id=room_id,
    )
    bot.client = client
    index = bot._runtime_view.agent_reply_memberships
    await index.refresh(bot.config, bot.runtime_paths, client)
    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
        transport=TransportKind(transport),
    )
    bot._before_ingestion_admission(admission)

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert index.needs_refresh(bot.config)

    principal = bot.journal_principal()
    await principal.load_or_create_ingestion_consumer(new_generation=_CONSUMER_GENERATION)
    await principal.bind_ingestion_stream(
        generation=_CONSUMER_GENERATION,
        stream_id=_STREAM_ID,
    )
    assert await principal.admit_ingestion_batch(admission) == _FRESH_RECEIPT_FACTS

    orchestrator.revoke_reply_authorized_calls.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_router_leave_then_rejoin_in_one_sync_requires_grant_refresh(tmp_path: Path) -> None:
    """Any router continuity gap must require a fresh authoritative grant snapshot."""
    room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    orchestrator.revoke_reply_authorized_calls = AsyncMock()
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(bot.agent_user.user_id, None, None),
            nio.RoomMember(sender_id, None, None),
        ],
        room_id=room_id,
    )
    bot.client = client
    index = bot._runtime_view.agent_reply_memberships
    await index.refresh(bot.config, bot.runtime_paths, client)

    departure = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
        event_id="$leave",
    )
    rejoin = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="leave",
        membership="join",
        previous_epoch=1,
        sequence=1,
        event_id="$rejoin",
    )

    bot._before_ingestion_admission(departure)
    bot._before_ingestion_admission(rejoin)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert index.needs_refresh(bot.config)
    orchestrator.revoke_reply_authorized_calls.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_router_final_invite_revokes_grant_before_timeline_admission(
    tmp_path: Path,
    transport: str,
) -> None:
    """A final invite means the router can no longer vouch for the old roster."""
    room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, _orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(bot.agent_user.user_id, None, None),
            nio.RoomMember(sender_id, None, None),
        ],
        room_id=room_id,
    )
    index = bot._runtime_view.agent_reply_memberships
    await index.refresh(bot.config, bot.runtime_paths, client)
    admission = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="invite",
        previous_epoch=0,
        transport=TransportKind(transport),
    )

    bot._before_ingestion_admission(admission)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert index.needs_refresh(bot.config)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
@pytest.mark.parametrize("membership", ["leave", "ban", "invite"])
async def test_grant_user_revocation_waits_for_durable_live_admission(
    tmp_path: Path,
    transport: str,
    membership: str,
) -> None:
    """An ordinary revocation takes effect at its accepted durable LIVE event."""
    grant_room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    orchestrator.revoke_reply_authorized_calls = AsyncMock()
    orchestrator.reconcile_reply_authorized_calls = AsyncMock()
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", grant_room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[grant_room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(bot.agent_user.user_id, None, None),
            nio.RoomMember(sender_id, None, None),
        ],
        room_id=grant_room_id,
    )
    index = bot._runtime_view.agent_reply_memberships
    await index.refresh(bot.config, bot.runtime_paths, client)
    member_event = _departure_member_event(
        "$membership",
        user_id=sender_id,
        membership=membership,
        ts=2,
    )
    admission = _validated_timeline_admission(
        bot,
        grant_room_id,
        member_event,
        transport=TransportKind(transport),
        sequence=0,
    )

    bot._before_ingestion_admission(admission)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert not index.needs_refresh(bot.config)
    orchestrator.revoke_reply_authorized_calls.assert_not_awaited()

    await bot._after_ingestion_admission(
        admission,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    orchestrator.reconcile_reply_authorized_calls.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
async def test_grant_user_join_waits_for_durable_timeline_admission(
    tmp_path: Path,
    transport: str,
) -> None:
    """The pre-admission scan must never grant access from a positive transition."""
    room_id = "!grant:localhost"
    sender_id = "@bob:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    orchestrator.revoke_reply_authorized_calls = AsyncMock()
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(bot.agent_user.user_id, None, None)],
        room_id=room_id,
    )
    index = bot._runtime_view.agent_reply_memberships
    await index.refresh(bot.config, bot.runtime_paths, client)
    member_event = _departure_member_event("$join", user_id=sender_id, membership="join", ts=1)
    admission = _validated_timeline_admission(
        bot,
        room_id,
        member_event,
        transport=TransportKind(transport),
        sequence=0,
    )

    bot._before_ingestion_admission(admission)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    orchestrator.revoke_reply_authorized_calls.assert_not_awaited()

    await bot._after_ingestion_admission(
        admission,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)


@pytest.mark.asyncio
async def test_live_membership_replay_retries_an_unfinished_reconciliation(tmp_path: Path) -> None:
    """A committed LIVE transition must retry effects left pending by a failed first pass."""
    room_id = "!grant:localhost"
    sender_id = "@bob:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(bot.agent_user.user_id, None, None)],
        room_id=room_id,
    )
    index = bot._runtime_view.agent_reply_memberships
    await index.refresh(bot.config, bot.runtime_paths, client)
    admission = _validated_timeline_admission(
        bot,
        room_id,
        _departure_member_event("$join", user_id=sender_id, membership="join", ts=1),
        transport=TransportKind.CLASSIC,
        sequence=0,
    )
    orchestrator.reconcile_reply_authorized_calls = AsyncMock(
        side_effect=[RuntimeError("reconciliation interrupted"), None],
    )

    with pytest.raises(RuntimeError, match="reconciliation interrupted"):
        await bot._after_ingestion_admission(
            admission,
            _FRESH_SEMANTIC_FACTS,
            TimelineEventProvenance.LIVE,
        )

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)

    await bot._after_ingestion_admission(
        admission,
        _REPLAY_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
    assert orchestrator.reconcile_pending_invites.await_count == 2
    assert orchestrator.reconcile_reply_authorized_calls.await_count == 2


@pytest.mark.asyncio
async def test_recovered_membership_does_not_change_live_reply_grants(tmp_path: Path) -> None:
    """Recovered membership history must not mutate the router's current grant snapshot."""
    room_id = "!grant:localhost"
    sender_id = "@bob:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    index = bot._runtime_view.agent_reply_memberships
    admission = _validated_timeline_admission(
        bot,
        room_id,
        _departure_member_event("$join", user_id=sender_id, membership="join", ts=1),
        transport=TransportKind.CLASSIC,
        sequence=0,
    )

    await bot._after_ingestion_admission(
        admission,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.RECOVERED,
    )

    assert not index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    orchestrator.reconcile_pending_invites.assert_not_awaited()
    orchestrator.reconcile_reply_authorized_calls.assert_not_awaited()


@pytest.mark.asyncio
async def test_room_activity_tracks_fresh_receipts_and_ignores_unsettled_replay(tmp_path: Path) -> None:
    """A new receipt marks projected room activity once even when the Matrix event is a duplicate."""
    bot = _agent_bot(tmp_path)
    room_id = "!room:localhost"
    activity = MagicMock()
    bot._room_activity_observer = activity
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
        transport=TransportKind.CLASSIC,
        sequence=0,
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
@pytest.mark.parametrize("transport", ["classic", "sliding"])
@pytest.mark.parametrize("membership", ["leave", "ban"])
async def test_grant_user_join_then_revoke_applies_in_durable_order(
    tmp_path: Path,
    transport: str,
    membership: str,
) -> None:
    """Accepted LIVE transitions update grants in their durable event order."""
    room_id = "!grant:localhost"
    sender_id = "@bob:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.router.access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", room_id, "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(bot.agent_user.user_id, None, None)],
        room_id=room_id,
    )
    index = bot._runtime_view.agent_reply_memberships
    await index.refresh(bot.config, bot.runtime_paths, client)
    join_event = _departure_member_event("$join", user_id=sender_id, membership="join", ts=1)
    revoke_event = _departure_member_event("$revoke", user_id=sender_id, membership=membership, ts=2)
    join_admission = _validated_timeline_admission(
        bot,
        room_id,
        join_event,
        transport=TransportKind(transport),
        sequence=0,
    )
    revoke_admission = _validated_timeline_admission(
        bot,
        room_id,
        revoke_event,
        transport=TransportKind(transport),
        sequence=1,
    )

    orchestrator.reconcile_reply_authorized_calls = AsyncMock()
    bot._before_ingestion_admission(join_admission)
    bot._before_ingestion_admission(revoke_admission)
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
    assert orchestrator.reconcile_reply_authorized_calls.await_count == 2

    later_join = _validated_timeline_admission(
        bot,
        room_id,
        _departure_member_event("$later-join", user_id=sender_id, membership="join", ts=3),
        transport=TransportKind(transport),
        sequence=2,
    )
    await bot._after_ingestion_admission(
        later_join,
        _FRESH_SEMANTIC_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert orchestrator.reconcile_reply_authorized_calls.await_count == 3

    await bot._after_ingestion_admission(
        later_join,
        _REPLAY_FACTS,
        TimelineEventProvenance.LIVE,
    )

    assert index.is_allowed(sender_id, ["grant"], bot.config, bot.runtime_paths)
    assert orchestrator.reconcile_reply_authorized_calls.await_count == 3


def test_call_manager_registers_call_and_room_membership_callbacks(tmp_path: Path) -> None:
    """Call admission is rechecked for call-state and underlying room-member changes."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)
    call_manager = MagicMock()

    with patch("mindroom.bot.maybe_build_call_manager", return_value=call_manager):
        bot._register_call_manager_callbacks(client)

    assert bot._call_manager is call_manager
    client.add_event_callback.assert_not_called()
    client.add_to_device_callback.assert_called_once_with(ANY, AuthenticatedToDeviceEvent)


def test_bot_config_setter_updates_existing_call_manager(tmp_path: Path) -> None:
    """An unchanged call bot should observe authorization-only hot reloads."""
    bot = _agent_bot(tmp_path)
    call_manager = MagicMock()
    install_call_manager_mock(bot, call_manager)
    new_config = _config(tmp_path)

    bot.config = new_config

    call_manager.update_config.assert_called_once_with(new_config)


@pytest.mark.asyncio
async def test_call_manager_room_callbacks_run_without_legacy_delivery_provenance(
    tmp_path: Path,
) -> None:
    """Owned settlement already gates callback eligibility before call fanout."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)
    call_manager = MagicMock()
    call_manager.on_room_membership_event = AsyncMock()
    call_manager.on_room_event = AsyncMock()
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

    with patch("mindroom.bot.maybe_build_call_manager", return_value=call_manager):
        bot._register_call_manager_callbacks(client)

    await bot._journal_dispatcher.callbacks.on_room_lifecycle(room, membership_event)
    rtc_callback = bot._journal_dispatcher.callbacks.on_rtc
    assert rtc_callback is not None
    await rtc_callback(room, call_event)

    call_manager.on_room_membership_event.assert_awaited_once_with(room, membership_event)
    call_manager.on_room_event.assert_awaited_once_with(room, call_event)


def test_room_membership_cleanup_registers_without_call_runtime(tmp_path: Path) -> None:
    """Persisted ad-hoc ownership is cleaned even when voice dependencies are absent."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)

    with patch("mindroom.bot.maybe_build_call_manager", return_value=None):
        bot._register_call_manager_callbacks(client)

    assert bot._call_manager is None
    client.add_event_callback.assert_not_called()
    client.add_to_device_callback.assert_not_called()


def test_call_admission_reads_live_invites_from_managed_agents(tmp_path: Path) -> None:
    """Call admission gets one live snapshot from each managed calls-enabled agent."""
    bot = _agent_bot(tmp_path)
    other = _agent_bot(tmp_path, agent_name="other")
    bot.config.agents["other"] = AgentConfig(display_name="Other")
    bot.config.calls = CallsConfig(
        enabled=True,
        profiles={
            "voice": RealtimeCallProfile(
                backend="realtime",
                model="gpt-realtime",
                credentials_service="openai",
                voice="marin",
            ),
        },
        agents={"code": "voice", "other": "voice"},
    )
    bot.orchestrator = MagicMock(agent_bots={"code": bot, "other": other})
    bot._room_lifecycle.invited_rooms.add("!code-call:localhost")
    other._room_lifecycle.invited_rooms.add("!other-call:localhost")

    assert bot._invited_call_rooms_by_agent() == {
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
    install_call_manager_mock(bot, MagicMock(voice_backend_available=backend_available))

    with (
        patch("mindroom.bot.build_agent_status_message", return_value="status") as build_status,
        patch("mindroom.bot.set_presence_status", new_callable=AsyncMock) as set_presence,
    ):
        await bot._set_presence_with_model_info()

    build_status.assert_called_once_with(
        bot.agent_name,
        bot.config,
        voice_calls_available=backend_available,
    )
    set_presence.assert_awaited_once_with(bot.client, "status")


def _departure_member_event(event_id: str, *, user_id: str, membership: str, ts: int) -> dict[str, object]:
    """Return one member event ending this account's stay in a room."""
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
    """The response token identifies a leave whose timeline omits its event."""
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

    assert await principal.admit_ingestion_batch(admission) == _FRESH_RECEIPT_FACTS
    await bot.journal_principal().note_membership_restarted(room_id)
    epoch_after_rejoin = await principal.membership_epoch(room_id)
    assert await principal.admit_ingestion_batch(admission) == _REPLAY_FACTS

    assert await principal.membership_epoch(room_id) == epoch_after_rejoin


@pytest.mark.asyncio
async def test_replayed_departure_cannot_leave_a_confirmed_join_fenced(tmp_path: Path) -> None:
    """A duplicated old leave report cannot invalidate a confirmed rejoin."""
    bot = _agent_bot(tmp_path)
    room_id = "!rejoined:localhost"
    departure = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="join",
        membership="leave",
        previous_epoch=0,
        event_id="$leave",
    )
    rejoin = _validated_reported_membership_admission(
        bot,
        room_id,
        previous_membership="leave",
        membership="join",
        previous_epoch=1,
        sequence=1,
        event_id="$rejoin",
    )
    principal = bot.journal_principal()
    await principal.load_or_create_ingestion_consumer(new_generation=_CONSUMER_GENERATION)
    await principal.bind_ingestion_stream(
        generation=_CONSUMER_GENERATION,
        stream_id=_STREAM_ID,
    )

    assert await principal.admit_ingestion_batch(departure) == _FRESH_RECEIPT_FACTS
    assert await principal.admit_ingestion_batch(departure) == _REPLAY_FACTS
    assert await principal.admit_ingestion_batch(rejoin) == _FRESH_RECEIPT_FACTS
    epoch_after_rejoin = await principal.membership_epoch(room_id)

    assert await principal.membership_epoch(room_id) == epoch_after_rejoin


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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)
        await _complete_frame(bot, 1)
        await _complete_frame(bot, 2)

    assert fired_count == 1


@pytest.mark.asyncio
async def test_orchestrator_ready_notification_retries_after_failure(tmp_path: Path) -> None:
    """A transient readiness failure must retry after the first sync was recorded."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    orchestrator = MagicMock()
    orchestrator.handle_bot_ready = AsyncMock(side_effect=[RuntimeError("transient recovery failure"), None])
    bot.orchestrator = orchestrator

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        pytest.raises(RuntimeError, match="transient recovery failure"),
    ):
        await _complete_frame(bot)

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot, 1)

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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_started, on_ready])])

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
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.config = bot.config
    orchestrator.agent_bots = {"router": bot}
    bot.orchestrator = orchestrator

    captured_content: dict[str, object] = {}

    async def mock_send(_client: object, _room_id: str, content: dict[str, object], **_kwargs: object) -> object:
        captured_content.update(content)
        return delivered_matrix_event("$hook-event", content)

    @hook(EVENT_BOT_READY)
    async def on_ready(ctx: AgentLifecycleContext) -> None:
        await ctx.send_message("!room:localhost", "I'm ready!")

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.hooks.sender.send_matrix_message", side_effect=mock_send),
    ):
        await _complete_frame(bot)

    assert captured_content[SOURCE_KIND_KEY] == "hook"
    assert captured_content["com.mindroom.hook_source"] == "test-plugin:bot:ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", [EVENT_AGENT_STARTED, EVENT_AGENT_STOPPED])
async def test_lifecycle_hooks_prefer_bot_room_state_helpers_before_router_fallback(
    tmp_path: Path,
    event_name: str,
) -> None:
    """Lifecycle hooks should query room state with the current bot before falling back to the router."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.room_get_state_event.return_value = MagicMock(content={"name": "Agent Lobby"})
    bot.client.room_put_state.return_value = object()
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = AsyncMock(spec=nio.AsyncClient)
    router_bot.client.room_get_state_event.return_value = MagicMock(content={"name": "Router Lobby"})
    router_bot.client.room_put_state.return_value = object()
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": router_bot, "code": bot}
    bot.orchestrator = orchestrator

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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_lifecycle])])

    await bot._emit_agent_lifecycle_event(event_name)

    assert results == [({"name": "Agent Lobby"}, True)]
    bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    bot.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$thread",
    )
    router_bot.client.room_get_state_event.assert_not_awaited()
    router_bot.client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_name", [EVENT_AGENT_STARTED, EVENT_AGENT_STOPPED])
async def test_lifecycle_hooks_fallback_to_router_room_state_helpers_when_bot_cannot_access_room(
    tmp_path: Path,
    event_name: str,
) -> None:
    """Lifecycle hooks should fall back to the router when the current bot cannot access room state."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.room_get_state_event.return_value = nio.RoomGetStateEventError(message="forbidden")
    bot.client.room_put_state.return_value = nio.RoomPutStateError(message="forbidden")
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = AsyncMock(spec=nio.AsyncClient)
    router_bot.client.room_get_state_event.return_value = MagicMock(content={"name": "Router Lobby"})
    router_bot.client.room_put_state.return_value = object()
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": router_bot, "code": bot}
    bot.orchestrator = orchestrator

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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_lifecycle])])

    await bot._emit_agent_lifecycle_event(event_name)

    assert results == [({"name": "Router Lobby"}, True)]
    bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    bot.client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"queued": True}},
        state_key="$thread",
    )
    router_bot.client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    router_bot.client.room_put_state.assert_awaited_once_with(
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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])
    bot._sync_shutting_down = True

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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        # First sync arrives during shutdown — bot:ready suppressed
        bot._sync_shutting_down = True
        await _complete_frame(bot)
        assert fired_count == 0

        # Shutdown clears (restart)
        bot.mark_sync_loop_started()

        # Next sync — bot:ready must fire now
        await _complete_frame(bot, 1)
        assert fired_count == 1

        # Subsequent syncs must not re-fire
        await _complete_frame(bot, 2)
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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert len(captured_ctx) == 1
    ctx = captured_ctx[0]
    assert ctx.entity_name == "code"
    assert ctx.matrix_user_id == "@mindroom_code:localhost"
    assert "!room:localhost" in ctx.rooms
    assert ctx.joined_room_ids == ("!room:localhost",)


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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_started])])

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

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await _complete_frame(bot)

    assert len(captured_ctx) == 1
    assert captured_ctx[0].rooms == ("!room:localhost",)
    assert captured_ctx[0].joined_room_ids == ("!room:localhost", "!joined:localhost")


@pytest.mark.asyncio
async def test_non_router_hook_sender_prefers_current_bot_client(tmp_path: Path) -> None:
    """Non-router bots should send hook messages with their own Matrix client when available."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()
    bot.client.user_id = "@mindroom_code:localhost"
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = AsyncMock()
    router_bot.client.user_id = "@mindroom_router:localhost"
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.agent_bots = {"router": router_bot, "code": bot}
    bot.orchestrator = orchestrator

    sent_clients: list[object] = []

    async def mock_send(client: object, _room_id: str, content: dict[str, object], **_kwargs: object) -> object:
        sent_clients.append(client)
        return delivered_matrix_event("$hook-event", content)

    sender = bot._hook_context_support.message_sender()
    assert sender is not None

    with patch("mindroom.hooks.sender.send_matrix_message", side_effect=mock_send):
        event_id = await sender("!room:localhost", "hello", None, "test-plugin:bot:ready", None)

    assert event_id == "$hook-event"
    assert sent_clients == [bot.client]
