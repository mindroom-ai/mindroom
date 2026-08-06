"""Tests for the bot:ready lifecycle hook event."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.calls import CallsConfig, RealtimeCallProfile
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import SOURCE_KIND_KEY
from mindroom.dispatch_obligations import DispatchCallbackKind
from mindroom.dispatch_obligations.storage import DispatchObligation
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    AgentLifecycleContext,
    HookRegistry,
    hook,
)
from mindroom.matrix.client_thread_history import BulkThreadRefreshStats
from mindroom.matrix.sync_certification import SyncCacheWriteResult
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.runtime_support import StartupThreadPrewarmRegistry
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    install_call_manager_mock,
    install_runtime_cache_support,
    make_matrix_client_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
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
    return install_runtime_cache_support(
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
        ),
    )


@asynccontextmanager
async def _bind_shared_runtime_support(
    orchestrator: _MultiAgentOrchestrator,
    bots_by_name: dict[str, AgentBot],
) -> AsyncIterator[None]:
    orchestrator.agent_bots = dict(bots_by_name)
    await orchestrator._runtime_support.event_cache.initialize()
    for bot in bots_by_name.values():
        orchestrator._bind_runtime_support_services(bot)
        bot.orchestrator = orchestrator
    try:
        yield
    finally:
        await orchestrator._close_runtime_support_services()


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


def _empty_classic_sync_response(next_batch: str) -> nio.SyncResponse:
    """Return one complete Classic response with no room events."""
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": next_batch,
            "device_one_time_keys_count": {},
            "device_lists": {"changed": [], "left": []},
            "rooms": {"invite": {}, "leave": {}, "join": {}},
            "to_device": {"events": []},
            "presence": {"events": []},
            "account_data": {"events": []},
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


def _bulk_refresh_stats(
    requested_threads: int,
    *,
    usable_threads: int | None = None,
) -> BulkThreadRefreshStats:
    """Return compact startup-prewarm bulk stats for tests."""
    return BulkThreadRefreshStats(
        requested_threads=requested_threads,
        usable_threads=requested_threads if usable_threads is None else usable_threads,
        missing_root_ids=frozenset(),
        room_scan_pages=1,
        scanned_event_count=requested_threads,
    )


def _sync_response_with_room_membership_section(
    room_id: str,
    *,
    membership: str,
) -> nio.SyncResponse:
    room_section = "join" if membership == "join" else "leave"
    room_info = {
        "state": {"events": []},
        "timeline": {
            "events": [],
            "limited": False,
            "prev_batch": "s-before-membership",
        },
    }
    rooms: dict[str, object] = {"join": {}, "invite": {}, "leave": {}}
    rooms[room_section] = {room_id: room_info}
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": f"s-after-{membership}",
            "rooms": rooms,
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


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


@pytest.mark.asyncio
async def test_turn_recovery_cleans_ledger_after_reading_unsettled_sources(tmp_path: Path) -> None:
    """Startup cleanup must run after recovery and preserve every raw unsettled source."""
    bot = _agent_bot(tmp_path)
    call_order: list[str] = []
    unsettled_source_event_ids = frozenset({"$pending"})
    bot._dispatch_obligation_runner.recover_pending = AsyncMock(
        side_effect=lambda **_kwargs: call_order.append("recover"),
    )
    bot._dispatch_obligation_store.unsettled_source_event_ids = MagicMock(
        side_effect=lambda: (call_order.append("unsettled"), unsettled_source_event_ids)[1],
    )
    bot._turn_store.cleanup = MagicMock(side_effect=lambda **_kwargs: call_order.append("cleanup"))

    await bot.recover_pending_turn_dispatch_obligations()

    assert call_order == ["recover", "unsettled", "cleanup"]
    bot._dispatch_obligation_runner.recover_pending.assert_awaited_once_with(turn_backed=True)
    bot._turn_store.cleanup.assert_called_once_with(
        unsettled_source_event_ids=unsettled_source_event_ids,
    )


@pytest.mark.asyncio
async def test_turn_recovery_propagates_post_recovery_cleanup_failure(tmp_path: Path) -> None:
    """Ledger pruning failure must remain visible to the orchestrator retry owner."""
    bot = _agent_bot(tmp_path)
    bot._dispatch_obligation_runner.recover_pending = AsyncMock()
    bot._dispatch_obligation_store.unsettled_source_event_ids = MagicMock(return_value=frozenset())
    bot._turn_store.cleanup = MagicMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        await bot.recover_pending_turn_dispatch_obligations()

    bot._dispatch_obligation_runner.recover_pending.assert_awaited_once_with(turn_backed=True)
    bot._turn_store.cleanup.assert_called_once_with(unsettled_source_event_ids=frozenset())


@pytest.mark.asyncio
async def test_non_turn_recovery_retries_store_enumeration_failure(tmp_path: Path) -> None:
    """A transient discovery failure must not strand accepted non-turn callbacks."""
    bot = _agent_bot(tmp_path)
    bot._dispatch_obligation_runner.recover_pending = AsyncMock(side_effect=[OSError("disk unavailable"), None])

    with patch("mindroom.bot.wait_exponential", return_value=lambda _retry_state: 0):
        await bot._recover_non_turn_dispatch_obligations()

    assert bot._dispatch_obligation_runner.recover_pending.await_count == 2
    bot._dispatch_obligation_runner.recover_pending.assert_awaited_with(turn_backed=False)


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
        await bot._on_sync_response(MagicMock())

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
        patch.object(bot, "_maybe_start_startup_thread_prewarm"),
        patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
    ):
        bot.mark_sync_loop_started()
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

        bot.mark_sync_loop_started()
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert call_manager.reconcile_joined_rooms.await_count == 2


def test_call_manager_registers_call_and_room_membership_callbacks(tmp_path: Path) -> None:
    """Call admission is rechecked for call-state and underlying room-member changes."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)
    call_manager = MagicMock()

    with patch("mindroom.bot.maybe_build_call_manager", return_value=call_manager):
        bot._register_call_manager_callbacks(client)

    assert bot._call_manager is call_manager
    assert [call.args[1] for call in client.add_event_callback.call_args_list] == [
        nio.RoomMemberEvent,
        nio.UnknownEvent,
    ]
    client.add_to_device_callback.assert_called_once_with(ANY, AuthenticatedToDeviceEvent)


@pytest.mark.asyncio
async def test_call_manager_room_callbacks_reject_cold_history(tmp_path: Path) -> None:
    """Historical room membership and call state cannot mutate the live call runtime."""
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
            "state_key": bot.agent_user.user_id,
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

    membership_callback = client.add_event_callback.call_args_list[0].args[0]
    call_callback = client.add_event_callback.call_args_list[1].args[0]
    bot._cold_history_fence.observe_event_provenance(
        membership_event.event_id,
        nio.TimelineEventProvenance.HISTORY,
    )
    await membership_callback(room, membership_event)
    bot._cold_history_fence.observe_event_provenance(
        call_event.event_id,
        nio.TimelineEventProvenance.HISTORY,
    )
    await call_callback(room, call_event)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    call_manager.on_room_membership_event.assert_not_awaited()
    call_manager.on_room_event.assert_not_awaited()

    bot._cold_history_fence.observe_event_provenance(
        membership_event.event_id,
        nio.TimelineEventProvenance.LIVE,
    )
    await membership_callback(room, membership_event)
    bot._cold_history_fence.observe_event_provenance(
        call_event.event_id,
        nio.TimelineEventProvenance.LIVE,
    )
    await call_callback(room, call_event)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    call_manager.on_room_membership_event.assert_awaited_once_with(room, membership_event)
    call_manager.on_room_event.assert_awaited_once_with(room, call_event)


@pytest.mark.asyncio
async def test_call_manager_room_callbacks_capture_cold_admission_at_delivery(tmp_path: Path) -> None:
    """Opening continuity after delivery cannot admit a callback delivered cold."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)
    call_manager = MagicMock()
    call_manager.on_room_membership_event = AsyncMock()
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    membership_event = nio.RoomMemberEvent.from_dict(
        {
            "event_id": "$historical-member",
            "sender": "@owner:localhost",
            "origin_server_ts": 1,
            "type": "m.room.member",
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "leave"},
        },
    )
    assert isinstance(membership_event, nio.RoomMemberEvent)

    with patch("mindroom.bot.maybe_build_call_manager", return_value=call_manager):
        bot._register_call_manager_callbacks(client)

    membership_callback = client.add_event_callback.call_args_list[0].args[0]
    bot._cold_history_fence.observe_event_provenance(
        membership_event.event_id,
        nio.TimelineEventProvenance.HISTORY,
    )
    await membership_callback(room, membership_event)
    bot._cold_history_fence.observe_event_provenance(
        membership_event.event_id,
        nio.TimelineEventProvenance.LIVE,
    )
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    call_manager.on_room_membership_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_room_lifecycle_does_not_admit_call_manager_mutation(tmp_path: Path) -> None:
    """A router-hook retry cannot license an unrelated call-runtime mutation."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)
    call_manager = MagicMock()
    call_manager.on_room_membership_event = AsyncMock()
    room = nio.MatrixRoom("!room:localhost", bot.agent_user.user_id)
    membership_event = nio.RoomMemberEvent.from_dict(
        {
            "event_id": "$pending-router-hook",
            "sender": "@owner:localhost",
            "origin_server_ts": 1,
            "type": "m.room.member",
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "join"},
        },
    )
    assert isinstance(membership_event, nio.RoomMemberEvent)
    bot._dispatch_obligation_store.create_pending(
        DispatchObligation(
            principal_id=bot._dispatch_obligation_store.principal_id,
            entity_name=bot._dispatch_obligation_store.entity_name,
            source_event_id=membership_event.event_id,
            callback_kind=DispatchCallbackKind.ROOM_LIFECYCLE,
            room_id=room.room_id,
            event_source=dict(membership_event.source),
        ),
    )

    with patch("mindroom.bot.maybe_build_call_manager", return_value=call_manager):
        bot._register_call_manager_callbacks(client)

    membership_callback = client.add_event_callback.call_args_list[0].args[0]
    bot._cold_history_fence.observe_event_provenance(
        membership_event.event_id,
        nio.TimelineEventProvenance.HISTORY,
    )
    await membership_callback(room, membership_event)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    call_manager.on_room_membership_event.assert_not_awaited()


def test_room_membership_cleanup_registers_without_call_runtime(tmp_path: Path) -> None:
    """Persisted ad-hoc ownership is cleaned even when voice dependencies are absent."""
    bot = _agent_bot(tmp_path)
    client = MagicMock(spec=nio.AsyncClient)

    with patch("mindroom.bot.maybe_build_call_manager", return_value=None):
        bot._register_call_manager_callbacks(client)

    assert bot._call_manager is None
    client.add_event_callback.assert_called_once_with(ANY, nio.RoomMemberEvent)
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


@pytest.mark.asyncio
async def test_sync_leave_section_forgets_invited_room_before_call_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Own departures delivered under rooms.leave reach the lifecycle cleanup path."""
    bot = _agent_bot(tmp_path)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room_id = "!agent-call:localhost"
    bot.client = client
    bot._room_lifecycle._update_invited_room(room_id, remember=True)
    call_manager = MagicMock()

    async def assert_invite_was_forgotten(**_kwargs: object) -> None:
        assert bot._room_lifecycle.invited_rooms == set()

    call_manager.on_sync_room_membership = AsyncMock(side_effect=assert_invite_was_forgotten)
    install_call_manager_mock(bot, call_manager)
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(
        _sync_response_with_room_membership_section(
            room_id,
            membership="leave",
        ),
    )

    assert bot._room_lifecycle.invited_rooms == set()
    call_manager.on_sync_room_membership.assert_awaited_once_with(
        joined_room_ids=set(),
        left_room_ids={room_id},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["leave", "ban"])
async def test_joined_sync_timeline_departure_purges_before_reopening_room(
    tmp_path: Path,
    membership: str,
) -> None:
    """A leave and rejoin compressed into one sync must purge before restoring access."""
    bot = _agent_bot(tmp_path)
    room_id = "!departed:localhost"
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": "s-after-rejoin",
            "rooms": {
                "invite": {},
                "join": {
                    room_id: {
                        "state": {"events": []},
                        "timeline": {
                            "events": [
                                {
                                    "content": {"membership": membership},
                                    "event_id": "$departure",
                                    "origin_server_ts": 1,
                                    "sender": "@admin:localhost",
                                    "state_key": bot.agent_user.user_id,
                                    "type": "m.room.member",
                                },
                                {
                                    "content": {"membership": "join"},
                                    "event_id": "$rejoin",
                                    "origin_server_ts": 2,
                                    "sender": bot.agent_user.user_id,
                                    "state_key": bot.agent_user.user_id,
                                    "type": "m.room.member",
                                },
                            ],
                            "limited": False,
                            "prev_batch": "s-before-rejoin",
                        },
                    },
                },
                "leave": {},
            },
        },
    )
    operation_order: list[str] = []
    bot._conversation_cache.purge_rooms = AsyncMock(side_effect=lambda _rooms: operation_order.append("purge"))
    bot._conversation_cache.mark_room_joined = AsyncMock(side_effect=lambda _room: operation_order.append("join"))

    await bot._apply_own_room_membership_from_sync(response)

    assert operation_order == ["purge", "join"]
    bot._conversation_cache.purge_rooms.assert_awaited_once_with({room_id})
    bot._conversation_cache.mark_room_joined.assert_awaited_once_with(room_id)


@pytest.mark.asyncio
async def test_sync_join_section_reaches_call_manager(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A room in the sync join section can clear departed call state."""
    bot = _agent_bot(tmp_path)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room_id = "!configured-call:localhost"
    bot.client = client
    call_manager = MagicMock()
    call_manager.on_sync_room_membership = AsyncMock()
    install_call_manager_mock(bot, call_manager)
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(
        _sync_response_with_room_membership_section(
            room_id,
            membership="join",
        ),
    )

    call_manager.on_sync_room_membership.assert_awaited_once_with(
        joined_room_ids={room_id},
        left_room_ids=set(),
    )


@pytest.mark.asyncio
async def test_installed_runtime_cache_support_runs_fire_and_forget_sync_cache_writes(tmp_path: Path) -> None:
    """The shared test runtime helper must preserve the coordinator's synchronous queue contract."""
    bot = _agent_bot(tmp_path)
    bot.client = AsyncMock()

    message_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "Thread reply",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
            },
            "event_id": "$thread_msg:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )
    sync_response = MagicMock()
    sync_response.__class__ = nio.SyncResponse
    sync_response.rooms = MagicMock(
        join={
            "!room:localhost": MagicMock(timeline=MagicMock(events=[message_event], limited=False)),
        },
    )

    bot._conversation_cache.cache_sync_timeline(sync_response)
    await wait_for_background_tasks(timeout=1.0, owner=bot.event_cache_write_coordinator.background_task_owner)

    bot.event_cache.store_events_batch.assert_awaited_once()


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
        await bot._on_sync_response(MagicMock())
        await bot._on_sync_response(MagicMock())
        await bot._on_sync_response(MagicMock())

    assert fired_count == 1


@pytest.mark.asyncio
async def test_bot_ready_does_not_repeat_after_classic_transport_rebuild(tmp_path: Path) -> None:
    """Rebuilding nio's transient room cache must not restart bot lifecycle."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)

    fired_count = 0

    @hook(EVENT_BOT_READY)
    async def on_ready(_ctx: AgentLifecycleContext) -> None:
        nonlocal fired_count
        fired_count += 1

    bot.hook_registry = HookRegistry.from_plugins([_plugin("test-plugin", [on_ready])])

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(_empty_classic_sync_response("s_before_rebuild"))
        await bot._reset_classic_sync_state(force=True)
        assert bot._first_sync_done is True
        await bot._on_sync_response(_empty_classic_sync_response("s_after_rebuild"))

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
        await bot._on_sync_response(MagicMock())

    with patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)):
        await bot._on_sync_response(MagicMock())

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
        await bot._on_sync_response(MagicMock())

    assert event_order == ["agent:started", "bot:ready"]


@pytest.mark.asyncio
async def test_bot_ready_hook_can_send_messages(tmp_path: Path) -> None:
    """Hooks on bot:ready should be able to send messages through the bound sender."""
    bot = _agent_bot(tmp_path, agent_name="router")
    bot.client = AsyncMock()
    bot.client.add_event_admission_callback = MagicMock()
    bot.client.add_event_callback = MagicMock()
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
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
    bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value=None)

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.hooks.sender._send_message_result", side_effect=mock_send),
    ):
        await bot._on_sync_response(MagicMock())

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
        await bot._on_sync_response(MagicMock())

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
        await bot._on_sync_response(MagicMock())
        assert fired_count == 0

        # Shutdown clears (restart)
        bot.mark_sync_loop_started()

        # Next sync — bot:ready must fire now
        await bot._on_sync_response(MagicMock())
        assert fired_count == 1

        # Subsequent syncs must not re-fire
        await bot._on_sync_response(MagicMock())
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
        await bot._on_sync_response(MagicMock())

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
        await bot._on_sync_response(MagicMock())

    assert len(captured_ctx) == 1
    assert captured_ctx[0].rooms == ("!room:localhost",)
    assert captured_ctx[0].joined_room_ids == ("!room:localhost", "!joined:localhost")


@pytest.mark.asyncio
async def test_bot_ready_starts_background_startup_thread_prewarm(tmp_path: Path) -> None:
    """bot:ready should prewarm recent thread snapshots in the background after first sync."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    bot._conversation_cache.logger = MagicMock()
    bot._conversation_cache._bulk_refresh_startup_threads = AsyncMock(return_value=_bulk_refresh_stats(2))

    thread_roots = [
        _thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1),
        _thread_root_event("$thread-b:localhost", body="Thread B", origin_server_ts=2),
    ]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, "next-token")),
        ) as mock_get_room_threads_page,
        patch.object(
            bot._conversation_cache,
            "get_dispatch_thread_snapshot",
            new=AsyncMock(side_effect=AssertionError("startup prewarm should bypass the live dispatch entrypoint")),
        ) as mock_get_dispatch_thread_snapshot,
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    mock_get_room_threads_page.assert_awaited_once_with(
        bot.client,
        "!room:localhost",
        limit=32,
    )
    bot._conversation_cache._bulk_refresh_startup_threads.assert_awaited_once_with(
        "!room:localhost",
        ("$thread-a:localhost", "$thread-b:localhost"),
    )
    mock_get_dispatch_thread_snapshot.assert_not_awaited()
    bot._conversation_cache.logger.info.assert_any_call(
        "startup_thread_prewarm_complete",
        room_id="!room:localhost",
        threads_warmed=2,
        threads_failed=0,
        elapsed_ms=ANY,
    )


@pytest.mark.asyncio
async def test_bot_ready_prefers_locally_recent_threads_for_startup_prewarm(tmp_path: Path) -> None:
    """Startup prewarm should use locally recent thread IDs before topping up from /threads."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    bot.event_cache.get_recent_room_thread_ids.return_value = [
        "$thread-local-a:localhost",
        "$thread-local-b:localhost",
    ]
    bot._conversation_cache._bulk_refresh_startup_threads = AsyncMock(return_value=_bulk_refresh_stats(4))

    thread_roots = [
        _thread_root_event("$thread-local-b:localhost", body="Thread B", origin_server_ts=2),
        _thread_root_event("$thread-api-c:localhost", body="Thread C", origin_server_ts=3),
        _thread_root_event("$thread-api-d:localhost", body="Thread D", origin_server_ts=4),
    ]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, None)),
        ) as mock_get_room_threads_page,
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    bot.event_cache.get_recent_room_thread_ids.assert_awaited_once_with("!room:localhost", limit=32)
    mock_get_room_threads_page.assert_awaited_once_with(
        bot.client,
        "!room:localhost",
        limit=32,
    )
    bot._conversation_cache._bulk_refresh_startup_threads.assert_awaited_once_with(
        "!room:localhost",
        (
            "$thread-local-a:localhost",
            "$thread-local-b:localhost",
            "$thread-api-c:localhost",
            "$thread-api-d:localhost",
        ),
    )


@pytest.mark.asyncio
async def test_bot_ready_falls_back_to_local_threads_when_threads_api_fails(tmp_path: Path) -> None:
    """Startup prewarm should still warm local thread IDs when /threads errors but local cache has entries."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    bot.event_cache.get_recent_room_thread_ids.return_value = [
        "$thread-local-a:localhost",
        "$thread-local-b:localhost",
    ]
    bot._conversation_cache._bulk_refresh_startup_threads = AsyncMock(return_value=_bulk_refresh_stats(2))
    bot._conversation_cache.logger = MagicMock()

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(side_effect=RuntimeError("threads_api_unavailable")),
        ),
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    bot._conversation_cache._bulk_refresh_startup_threads.assert_awaited_once_with(
        "!room:localhost",
        ("$thread-local-a:localhost", "$thread-local-b:localhost"),
    )
    bot._conversation_cache.logger.warning.assert_any_call(
        "startup_thread_prewarm_room_threads_failed",
        room_id="!room:localhost",
        error="threads_api_unavailable",
        local_thread_count=2,
    )


@pytest.mark.asyncio
async def test_bot_ready_skips_threads_api_when_local_recent_cache_is_sufficient(tmp_path: Path) -> None:
    """Startup prewarm should avoid /threads when local cache already supplies the full prewarm set."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    local_thread_ids = [f"$thread-{index}:localhost" for index in range(32)]
    bot.event_cache.get_recent_room_thread_ids.return_value = local_thread_ids
    bot._conversation_cache._bulk_refresh_startup_threads = AsyncMock(return_value=_bulk_refresh_stats(32))

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(side_effect=AssertionError("/threads should not be called when local recency is sufficient")),
        ),
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    bot.event_cache.get_recent_room_thread_ids.assert_awaited_once_with("!room:localhost", limit=32)
    bot._conversation_cache._bulk_refresh_startup_threads.assert_awaited_once_with(
        "!room:localhost",
        tuple(local_thread_ids),
    )


@pytest.mark.asyncio
async def test_startup_thread_prewarm_bulk_refresh_does_not_block_live_write(tmp_path: Path) -> None:
    """Startup prewarm should use one room scan without monopolizing live cache writes."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot._conversation_cache.logger = MagicMock()
    thread_ids = [f"$thread-{index}:localhost" for index in range(32)]
    bulk_refresh_started = asyncio.Event()
    release_bulk_refresh = asyncio.Event()
    competing_write_started = asyncio.Event()

    async def bulk_refresh(*_args: object, **_kwargs: object) -> BulkThreadRefreshStats:
        bulk_refresh_started.set()
        await release_bulk_refresh.wait()
        return _bulk_refresh_stats(len(thread_ids))

    async def competing_write() -> None:
        competing_write_started.set()

    with (
        patch.object(
            bot._conversation_cache,
            "_startup_thread_prewarm_ids",
            new=AsyncMock(return_value=thread_ids),
        ),
        patch(
            "mindroom.matrix.conversation_cache.thread_ids_needing_refill",
            new=AsyncMock(return_value=tuple(thread_ids)),
        ),
        patch(
            "mindroom.matrix.conversation_cache.bulk_refresh_room_thread_histories",
            new=AsyncMock(side_effect=bulk_refresh),
        ) as mock_bulk_refresh,
    ):
        prewarm_task = asyncio.create_task(
            bot._conversation_cache.prewarm_recent_room_threads(
                "!room:localhost",
                is_shutting_down=lambda: False,
            ),
        )
        competing_task: asyncio.Task[object] | None = None
        try:
            await asyncio.wait_for(bulk_refresh_started.wait(), timeout=1.0)
            competing_task = bot.event_cache_write_coordinator.queue_thread_update(
                "!room:localhost",
                thread_ids[0],
                competing_write,
                name="test_competing_startup_write",
                coordination_scope=bot.event_cache.principal_id,
            )
            await asyncio.wait_for(competing_write_started.wait(), timeout=1.0)
            assert prewarm_task.done() is False
            await competing_task
            release_bulk_refresh.set()
            assert await prewarm_task
        finally:
            release_bulk_refresh.set()
            await asyncio.gather(prewarm_task, return_exceptions=True)
            if competing_task is not None:
                await asyncio.gather(competing_task, return_exceptions=True)

    mock_bulk_refresh.assert_awaited_once_with(
        bot.client,
        "!room:localhost",
        bot.event_cache,
        thread_root_ids=tuple(thread_ids),
        caller_label="startup_thread_prewarm",
        max_scan_pages=20,
    )
    assert competing_write_started.is_set()
    bot._conversation_cache.logger.info.assert_any_call(
        "startup_thread_prewarm_complete",
        room_id="!room:localhost",
        threads_warmed=32,
        threads_failed=0,
        elapsed_ms=ANY,
    )


@pytest.mark.asyncio
async def test_startup_thread_prewarm_rechecks_shutdown_before_bulk_scan(tmp_path: Path) -> None:
    """Startup prewarm should not begin a bulk scan when shutdown starts during the cache probe."""
    bot = _agent_bot(tmp_path)
    thread_ids = ["$thread:localhost"]
    shutting_down = False

    async def probe_threads_needing_refill(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        nonlocal shutting_down
        shutting_down = True
        return tuple(thread_ids)

    with (
        patch.object(
            bot._conversation_cache,
            "_startup_thread_prewarm_ids",
            new=AsyncMock(return_value=thread_ids),
        ),
        patch(
            "mindroom.matrix.conversation_cache.thread_ids_needing_refill",
            new=AsyncMock(side_effect=probe_threads_needing_refill),
        ),
        patch.object(
            bot._conversation_cache,
            "_bulk_refresh_startup_threads",
            new=AsyncMock(),
        ) as mock_bulk_refresh,
    ):
        completed = await bot._conversation_cache.prewarm_recent_room_threads(
            "!room:localhost",
            is_shutting_down=lambda: shutting_down,
        )

    assert completed is False
    mock_bulk_refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_thread_prewarm_cache_probe_failure_is_fail_open(tmp_path: Path) -> None:
    """A cache-state read failure should release the room without aborting the startup loop."""
    bot = _agent_bot(tmp_path)
    bot._conversation_cache.logger = MagicMock()
    thread_ids = ["$thread:localhost"]

    with (
        patch.object(
            bot._conversation_cache,
            "_startup_thread_prewarm_ids",
            new=AsyncMock(return_value=thread_ids),
        ),
        patch(
            "mindroom.matrix.conversation_cache.thread_ids_needing_refill",
            new=AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch.object(
            bot._conversation_cache,
            "_bulk_refresh_startup_threads",
            new=AsyncMock(),
        ) as mock_bulk_refresh,
    ):
        completed = await bot._conversation_cache.prewarm_recent_room_threads(
            "!room:localhost",
            is_shutting_down=lambda: False,
        )

    assert completed is False
    mock_bulk_refresh.assert_not_awaited()
    bot._conversation_cache.logger.warning.assert_called_once_with(
        "startup_thread_prewarm_cache_probe_failed",
        room_id="!room:localhost",
        thread_count=1,
        error="database unavailable",
        error_type="RuntimeError",
        exc_info=True,
    )


@pytest.mark.asyncio
async def test_startup_thread_prewarm_skips_when_cache_writes_are_unavailable(tmp_path: Path) -> None:
    """Startup prewarm must not scan Matrix when its snapshots cannot be persisted."""
    bot = _agent_bot(tmp_path)
    bot._conversation_cache.logger = MagicMock()
    bot.event_cache.durable_writes_available = False
    bot._conversation_cache._startup_thread_prewarm_ids = AsyncMock(
        side_effect=AssertionError("prewarm should not enumerate threads when cache writes are unavailable"),
    )

    completed = await bot._conversation_cache.prewarm_recent_room_threads(
        "!room:localhost",
        is_shutting_down=lambda: False,
    )

    assert completed is False
    bot._conversation_cache.logger.warning.assert_called_once_with(
        "startup_thread_prewarm_skipped",
        room_id="!room:localhost",
        reason="event_cache_writes_unavailable",
    )


@pytest.mark.asyncio
async def test_startup_thread_prewarm_limits_room_work_across_bots(tmp_path: Path) -> None:
    """Startup prewarm should not let many enabled bots warm different rooms at the same time."""
    first_bot = _agent_bot(tmp_path, agent_name="router")
    second_bot = _agent_bot(tmp_path, agent_name="research")
    shared_registry = StartupThreadPrewarmRegistry()
    first_bot.startup_thread_prewarm_registry = shared_registry
    second_bot.startup_thread_prewarm_registry = shared_registry
    first_bot._get_startup_thread_prewarm_joined_rooms = AsyncMock(return_value=["!first:localhost"])
    second_bot._get_startup_thread_prewarm_joined_rooms = AsyncMock(return_value=["!second:localhost"])
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_waiting_for_slot = asyncio.Event()
    active_rooms = 0
    max_active_rooms = 0
    room_slot_attempts = 0
    warmed_rooms: list[str] = []

    original_room_slot = shared_registry.room_slot

    @asynccontextmanager
    async def observed_room_slot() -> AsyncIterator[None]:
        nonlocal room_slot_attempts
        room_slot_attempts += 1
        if room_slot_attempts == 2:
            second_waiting_for_slot.set()
        async with original_room_slot():
            yield

    async def prewarm_room(room_id: str, *, is_shutting_down: object) -> bool:
        nonlocal active_rooms, max_active_rooms
        del is_shutting_down
        active_rooms += 1
        max_active_rooms = max(max_active_rooms, active_rooms)
        warmed_rooms.append(room_id)
        if room_id == "!first:localhost":
            first_started.set()
            await release_first.wait()
        active_rooms -= 1
        return True

    first_bot._conversation_cache.prewarm_recent_room_threads = AsyncMock(side_effect=prewarm_room)
    second_bot._conversation_cache.prewarm_recent_room_threads = AsyncMock(side_effect=prewarm_room)
    shared_registry.room_slot = observed_room_slot

    first_task = asyncio.create_task(first_bot._run_startup_thread_prewarm())
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    second_task = asyncio.create_task(second_bot._run_startup_thread_prewarm())
    await asyncio.wait_for(second_waiting_for_slot.wait(), timeout=1.0)

    assert warmed_rooms == ["!first:localhost"]
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert warmed_rooms == ["!first:localhost", "!second:localhost"]
    assert max_active_rooms == 1


@pytest.mark.asyncio
async def test_startup_thread_prewarm_releases_room_claim_after_failure(tmp_path: Path) -> None:
    """Unexpected room prewarm errors should release the claim so another bot can retry."""
    bot = _agent_bot(tmp_path)
    room_id = "!room:localhost"
    registry = StartupThreadPrewarmRegistry()
    bot.startup_thread_prewarm_registry = registry
    assert await registry.try_claim(bot.event_cache.principal_id, room_id)
    bot._conversation_cache.prewarm_recent_room_threads = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await bot._prewarm_claimed_startup_thread_room(room_id)

    assert await registry.try_claim(bot.event_cache.principal_id, room_id)


@pytest.mark.asyncio
async def test_startup_thread_prewarm_bulk_refresh_waits_for_background_warm(tmp_path: Path) -> None:
    """Startup prewarm should await the full bulk scan instead of timing it out."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")

    async def slow_refresh(*_args: object, **_kwargs: object) -> BulkThreadRefreshStats:
        await asyncio.sleep(0.35)
        return _bulk_refresh_stats(1)

    with patch(
        "mindroom.matrix.conversation_cache.bulk_refresh_room_thread_histories",
        new=AsyncMock(side_effect=slow_refresh),
    ) as bulk_refresh:
        result = await bot._conversation_cache._bulk_refresh_startup_threads(
            "!room:localhost",
            ["$thread-root"],
        )

    assert result == _bulk_refresh_stats(1)
    bulk_refresh.assert_awaited_once_with(
        bot.client,
        "!room:localhost",
        bot.event_cache,
        thread_root_ids=["$thread-root"],
        caller_label="startup_thread_prewarm",
        max_scan_pages=20,
    )


@pytest.mark.asyncio
async def test_startup_thread_prewarm_bulk_failure_is_logged_only_by_caller(tmp_path: Path) -> None:
    """Awaited bulk failures should not also be logged by the background-task callback."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")

    with (
        patch(
            "mindroom.matrix.conversation_cache.bulk_refresh_room_thread_histories",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("mindroom.background_tasks.logger.exception") as background_task_log,
        pytest.raises(RuntimeError, match="boom"),
    ):
        await bot._conversation_cache._bulk_refresh_startup_threads(
            "!room:localhost",
            ["$thread-root"],
        )

    await asyncio.sleep(0)
    background_task_log.assert_not_called()


@pytest.mark.asyncio
async def test_startup_thread_prewarm_joined_rooms_failure_is_fail_open(tmp_path: Path) -> None:
    """Startup thread prewarm should log and stop cleanly when joined room lookup fails."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.side_effect = RuntimeError("boom")
    bot._conversation_cache.logger = MagicMock()

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.background_tasks.logger.exception") as mock_background_logger_exception,
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    bot._conversation_cache.logger.warning.assert_any_call(
        "startup_thread_prewarm_joined_rooms_failed",
        error="boom",
    )
    mock_background_logger_exception.assert_not_called()


@pytest.mark.asyncio
async def test_bot_ready_can_disable_startup_thread_prewarm(tmp_path: Path) -> None:
    """Per-bot config should allow startup thread prewarm to be disabled."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    rooms=["!room:localhost"],
                    startup_thread_prewarm=False,
                ),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    bot = install_runtime_cache_support(
        AgentBot(
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
        ),
    )
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(),
        ) as mock_get_room_threads_page,
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    mock_get_room_threads_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_only_ad_hoc_room_still_prewarms_when_router_exists(tmp_path: Path) -> None:
    """A non-router bot should prewarm its joined ad hoc room even when a router exists elsewhere."""
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = make_matrix_client_mock(user_id=router_bot.agent_user.user_id or "@mindroom_router:localhost")
    router_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[])
    agent_bot = _agent_bot(tmp_path)
    agent_bot.client = make_matrix_client_mock(user_id=agent_bot.agent_user.user_id or "@mindroom_code:localhost")
    agent_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!adhoc:localhost"])
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    thread_roots = [
        _thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1, room_id="!adhoc:localhost"),
    ]

    async with _bind_shared_runtime_support(orchestrator, {"router": router_bot, "code": agent_bot}):
        with (
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
            patch(
                "mindroom.matrix.conversation_cache.get_room_threads_page",
                new=AsyncMock(return_value=(thread_roots, None)),
            ) as mock_get_room_threads_page,
            patch.object(
                router_bot._conversation_cache,
                "_bulk_refresh_startup_threads",
                new=AsyncMock(return_value=_bulk_refresh_stats(1)),
            ),
            patch.object(
                agent_bot._conversation_cache,
                "_bulk_refresh_startup_threads",
                new=AsyncMock(return_value=_bulk_refresh_stats(1)),
            ),
        ):
            await router_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=router_bot._runtime_view)
            await agent_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=agent_bot._runtime_view)

    mock_get_room_threads_page.assert_awaited_once_with(
        agent_bot.client,
        "!adhoc:localhost",
        limit=32,
    )


@pytest.mark.asyncio
async def test_each_principal_claims_shared_room_startup_prewarm(tmp_path: Path) -> None:
    """Each principal must prewarm its own isolated rows in a shared room."""
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = make_matrix_client_mock(user_id=router_bot.agent_user.user_id or "@mindroom_router:localhost")
    router_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    agent_bot = _agent_bot(tmp_path)
    agent_bot.client = make_matrix_client_mock(user_id=agent_bot.agent_user.user_id or "@mindroom_code:localhost")
    agent_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    thread_roots = [_thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1)]

    async with _bind_shared_runtime_support(orchestrator, {"router": router_bot, "code": agent_bot}):
        with (
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
            patch(
                "mindroom.matrix.conversation_cache.get_room_threads_page",
                new=AsyncMock(return_value=(thread_roots, None)),
            ) as mock_get_room_threads_page,
            patch.object(
                router_bot._conversation_cache,
                "_bulk_refresh_startup_threads",
                new=AsyncMock(return_value=_bulk_refresh_stats(1)),
            ),
            patch.object(
                agent_bot._conversation_cache,
                "_bulk_refresh_startup_threads",
                new=AsyncMock(return_value=_bulk_refresh_stats(1)),
            ),
        ):
            await agent_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=agent_bot._runtime_view)
            await router_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=router_bot._runtime_view)

    assert mock_get_room_threads_page.await_args_list == [
        call(agent_bot.client, "!room:localhost", limit=32),
        call(router_bot.client, "!room:localhost", limit=32),
    ]


@pytest.mark.asyncio
async def test_room_thread_listing_failure_releases_claim_for_later_joined_bot(tmp_path: Path) -> None:
    """A room-level prewarm failure should release the claim so a later bot can retry it."""
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = make_matrix_client_mock(user_id=router_bot.agent_user.user_id or "@mindroom_router:localhost")
    router_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    router_bot._conversation_cache.logger = MagicMock()
    agent_bot = _agent_bot(tmp_path)
    agent_bot.client = make_matrix_client_mock(user_id=agent_bot.agent_user.user_id or "@mindroom_code:localhost")
    agent_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    agent_bot._conversation_cache.logger = MagicMock()
    agent_bot._conversation_cache._bulk_refresh_startup_threads = AsyncMock(return_value=_bulk_refresh_stats(1))
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    thread_roots = [_thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1)]

    async with _bind_shared_runtime_support(orchestrator, {"router": router_bot, "code": agent_bot}):
        with (
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
            patch(
                "mindroom.matrix.conversation_cache.get_room_threads_page",
                new=AsyncMock(side_effect=[RuntimeError("boom"), (thread_roots, None)]),
            ) as mock_get_room_threads_page,
        ):
            await router_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=router_bot._runtime_view)
            await agent_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=agent_bot._runtime_view)

    assert mock_get_room_threads_page.await_count == 2
    router_bot._conversation_cache.logger.warning.assert_any_call(
        "startup_thread_prewarm_room_threads_failed",
        room_id="!room:localhost",
        error="boom",
        local_thread_count=0,
    )
    agent_bot._conversation_cache._bulk_refresh_startup_threads.assert_awaited_once_with(
        "!room:localhost",
        ("$thread-a:localhost",),
    )
    assert (
        agent_bot.event_cache.principal_id,
        "!room:localhost",
    ) in agent_bot.startup_thread_prewarm_registry._claimed_rooms


@pytest.mark.asyncio
async def test_shutdown_mid_room_prewarm_releases_claim_for_later_joined_bot(tmp_path: Path) -> None:
    """A shutdown-aborted room prewarm should release the claim so a later bot can retry it."""
    router_bot = _agent_bot(tmp_path, agent_name="router")
    router_bot.client = make_matrix_client_mock(user_id=router_bot.agent_user.user_id or "@mindroom_router:localhost")
    router_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    router_bot._conversation_cache.logger = MagicMock()
    agent_bot = _agent_bot(tmp_path)
    agent_bot.client = make_matrix_client_mock(user_id=agent_bot.agent_user.user_id or "@mindroom_code:localhost")
    agent_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

    async def _abort_after_bulk_refresh(
        _room_id: str,
        thread_ids: tuple[str, ...],
    ) -> BulkThreadRefreshStats:
        router_bot._sync_shutting_down = True
        return _bulk_refresh_stats(len(thread_ids))

    router_bot._conversation_cache._bulk_refresh_startup_threads = AsyncMock(
        side_effect=_abort_after_bulk_refresh,
    )
    agent_bot._conversation_cache._bulk_refresh_startup_threads = AsyncMock(return_value=_bulk_refresh_stats(2))

    thread_roots = [
        _thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1),
        _thread_root_event("$thread-b:localhost", body="Thread B", origin_server_ts=2),
    ]

    async with _bind_shared_runtime_support(orchestrator, {"router": router_bot, "code": agent_bot}):
        with (
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
            patch(
                "mindroom.matrix.conversation_cache.get_room_threads_page",
                new=AsyncMock(return_value=(thread_roots, None)),
            ) as mock_get_room_threads_page,
        ):
            await router_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=router_bot._runtime_view)
            await agent_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=agent_bot._runtime_view)

    assert mock_get_room_threads_page.await_count == 2
    router_bot._conversation_cache._bulk_refresh_startup_threads.assert_awaited_once_with(
        "!room:localhost",
        ("$thread-a:localhost", "$thread-b:localhost"),
    )
    router_bot._conversation_cache.logger.info.assert_any_call(
        "startup_thread_prewarm_complete",
        room_id="!room:localhost",
        threads_warmed=2,
        threads_failed=0,
        elapsed_ms=ANY,
    )
    agent_bot._conversation_cache._bulk_refresh_startup_threads.assert_awaited_once_with(
        "!room:localhost",
        ("$thread-a:localhost", "$thread-b:localhost"),
    )
    assert (
        agent_bot.event_cache.principal_id,
        "!room:localhost",
    ) in agent_bot.startup_thread_prewarm_registry._claimed_rooms


@pytest.mark.asyncio
async def test_later_started_principal_warms_its_own_room_rows(tmp_path: Path) -> None:
    """A later principal must warm its isolated rows even when another principal already did."""
    first_bot = _agent_bot(tmp_path, agent_name="router")
    first_bot.client = make_matrix_client_mock(user_id=first_bot.agent_user.user_id or "@mindroom_router:localhost")
    first_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    later_bot = _agent_bot(tmp_path)
    later_bot.client = make_matrix_client_mock(user_id=later_bot.agent_user.user_id or "@mindroom_code:localhost")
    later_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    thread_roots = [_thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1)]

    async with _bind_shared_runtime_support(orchestrator, {"router": first_bot, "code": later_bot}):
        with (
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
            patch(
                "mindroom.matrix.conversation_cache.get_room_threads_page",
                new=AsyncMock(return_value=(thread_roots, None)),
            ) as mock_get_room_threads_page,
        ):
            await first_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=first_bot._runtime_view)

            later_bot._runtime_view.mark_runtime_started()
            await later_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=later_bot._runtime_view)

    assert mock_get_room_threads_page.await_count == 2
    assert mock_get_room_threads_page.await_args_list[0].args == (first_bot.client, "!room:localhost")
    assert mock_get_room_threads_page.await_args_list[0].kwargs == {"limit": 32}
    assert (
        later_bot.event_cache.principal_id,
        "!room:localhost",
    ) in later_bot.startup_thread_prewarm_registry._claimed_rooms


@pytest.mark.asyncio
async def test_disabled_bot_does_not_block_enabled_bot_from_claiming_room(tmp_path: Path) -> None:
    """A bot with startup prewarm disabled should not block another joined bot from claiming the room."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", rooms=["!room:localhost"], startup_thread_prewarm=False),
                "research": AgentConfig(display_name="Research", rooms=["!room:localhost"]),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    disabled_bot = install_runtime_cache_support(
        AgentBot(
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
        ),
    )
    enabled_bot = install_runtime_cache_support(
        AgentBot(
            agent_user=AgentMatrixUser(
                agent_name="research",
                password=TEST_PASSWORD,
                display_name="Research",
                user_id="@mindroom_research:localhost",
            ),
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!room:localhost"],
        ),
    )
    disabled_bot.client = make_matrix_client_mock(user_id=disabled_bot.agent_user.user_id or "@mindroom_code:localhost")
    disabled_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    enabled_bot.client = make_matrix_client_mock(
        user_id=enabled_bot.agent_user.user_id or "@mindroom_research:localhost",
    )
    enabled_bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    thread_roots = [_thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1)]

    async with _bind_shared_runtime_support(orchestrator, {"code": disabled_bot, "research": enabled_bot}):
        with (
            patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
            patch(
                "mindroom.matrix.conversation_cache.get_room_threads_page",
                new=AsyncMock(return_value=(thread_roots, None)),
            ) as mock_get_room_threads_page,
            patch.object(
                enabled_bot._conversation_cache,
                "_bulk_refresh_startup_threads",
                new=AsyncMock(return_value=_bulk_refresh_stats(1)),
            ),
        ):
            await disabled_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=disabled_bot._runtime_view)
            await enabled_bot._on_sync_response(MagicMock())
            await wait_for_background_tasks(timeout=1.0, owner=enabled_bot._runtime_view)

    mock_get_room_threads_page.assert_awaited_once_with(
        enabled_bot.client,
        "!room:localhost",
        limit=32,
    )


@pytest.mark.asyncio
async def test_startup_thread_prewarm_fails_open_when_bulk_refresh_fails(tmp_path: Path) -> None:
    """Startup thread prewarm should release its room claim after a bulk scan failure."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id or "@mindroom_code:localhost")
    bot.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    bot._conversation_cache.logger = MagicMock()
    bot._conversation_cache._bulk_refresh_startup_threads = AsyncMock(side_effect=RuntimeError("boom"))

    thread_roots = [
        _thread_root_event("$thread-a:localhost", body="Thread A", origin_server_ts=1),
        _thread_root_event("$thread-b:localhost", body="Thread B", origin_server_ts=2),
    ]

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch(
            "mindroom.matrix.conversation_cache.get_room_threads_page",
            new=AsyncMock(return_value=(thread_roots, None)),
        ),
    ):
        await bot._on_sync_response(MagicMock())
        await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    bot._conversation_cache._bulk_refresh_startup_threads.assert_awaited_once_with(
        "!room:localhost",
        ("$thread-a:localhost", "$thread-b:localhost"),
    )
    bot._conversation_cache.logger.warning.assert_any_call(
        "startup_thread_prewarm_bulk_failed",
        room_id="!room:localhost",
        thread_count=2,
        error="boom",
    )
    assert (
        bot.event_cache.principal_id,
        "!room:localhost",
    ) not in bot.startup_thread_prewarm_registry._claimed_rooms


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
    bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value=None)

    with patch("mindroom.hooks.sender._send_message_result", side_effect=mock_send):
        event_id = await sender("!room:localhost", "hello", None, "test-plugin:bot:ready", None)

    assert event_id == "$hook-event"
    assert sent_clients == [bot.client]
