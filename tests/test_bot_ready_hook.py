"""Tests for the bot:ready lifecycle hook event."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AgentReplyPermission
from mindroom.config.calls import CallsConfig, RealtimeCallProfile
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME, SOURCE_KIND_KEY
from mindroom.event_journal import EventClass, EventKind
from mindroom.hooks import (
    EVENT_AGENT_STARTED,
    EVENT_AGENT_STOPPED,
    EVENT_BOT_READY,
    AgentLifecycleContext,
    HookRegistry,
    hook,
)
from mindroom.matrix import journal_ingress
from mindroom.matrix.state import MatrixState
from mindroom.matrix.sync_loop import OwnRoomMembership
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.matrix.users import AgentMatrixUser
from mindroom.membership_models import ReportedDeparture
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    delivered_matrix_event,
    install_call_manager_mock,
    install_runtime_journal_support,
    make_matrix_client_mock,
    membership_epoch_is_active,
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


def _limited_classic_sync_response(next_batch: str) -> nio.SyncResponse:
    """Return a Classic response whose room timeline cannot prove full membership continuity."""
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": next_batch,
            "rooms": {
                "invite": {},
                "leave": {},
                "join": {
                    "!project:localhost": {
                        "state": {"events": []},
                        "timeline": {
                            "events": [],
                            "limited": True,
                            "prev_batch": "s-before-gap",
                        },
                    },
                },
            },
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


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


def _sliding_response_with_room_membership(
    room_id: str,
    *,
    membership: str,
) -> nio.SlidingSyncResponse:
    response = nio.SlidingSyncResponse.from_dict(
        {
            "pos": f"s-after-{membership}",
            "rooms": {
                room_id: {
                    "membership": membership,
                    "timeline": [],
                },
            },
        },
    )
    assert isinstance(response, nio.SlidingSyncResponse)
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


def _observe_provenance(event_id: str, provenance: nio.TimelineEventProvenance) -> None:
    """Set the delivery provenance the call-runtime callbacks read."""
    journal_ingress._DELIVERY_PROVENANCE.set((event_id, provenance))


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
        await bot._on_sync_response(_empty_classic_sync_response("s-first-post-snapshot-refresh"))

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
        await bot._on_sync_response(_empty_classic_sync_response("s-first-membership-refresh"))

    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_router_limited_sync_invalidates_then_rebuilds_room_backed_grants(tmp_path: Path) -> None:
    """A limited timeline must discard its uncertain baseline before taking a new authoritative snapshot."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.mark_sync_loop_started()
    orchestrator.invalidate_agent_reply_memberships.reset_mock()
    orchestrator.refresh_agent_reply_memberships.reset_mock()

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch.object(bot, "_maybe_start_deferred_overdue_task_drain"),
    ):
        await bot._on_sync_response(_limited_classic_sync_response("s-after-gap"))

    orchestrator.invalidate_agent_reply_memberships.assert_called_once_with(reason="uncertain_sync_response")
    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


def test_router_limited_sync_invalidates_before_timeline_admission(tmp_path: Path) -> None:
    """The Matrix client's pre-admission hook must fail room grants closed immediately."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    response = _limited_classic_sync_response("s-before-gap-timeline")

    bot._before_sync_response_admission(response)

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
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant", "second-grant"]),
    }
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
    response = (
        _sync_response_with_room_membership_section(room_id, membership="leave")
        if transport == "classic"
        else _sliding_response_with_room_membership(room_id, membership="leave")
    )
    if isinstance(response, nio.SyncResponse):
        response.rooms.leave[second_room_id] = response.rooms.leave[room_id]
    else:
        response.rooms[second_room_id] = nio.SlidingSyncRoom(membership="leave")

    bot._before_sync_response_admission(response)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert not bot._runtime_view.agent_reply_memberships.is_allowed(
        sender_id,
        ["grant", "second-grant"],
        bot.config.authorization,
    )
    assert bot._runtime_view.agent_reply_memberships.needs_refresh(bot.config.authorization)


@pytest.mark.asyncio
async def test_failed_membership_refresh_is_backed_off_between_sync_responses(tmp_path: Path) -> None:
    """An unavailable grant room must not cause one Matrix API refresh for every incoming message."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant"]),
    }
    bot._runtime_view.agent_reply_memberships.invalidate(bot.config, reason="test")

    with patch("mindroom.agent_reply_membership_sync.time.monotonic", return_value=100.0):
        await bot._refresh_agent_reply_memberships_if_needed()
        await bot._refresh_agent_reply_memberships_if_needed()

    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_repeated_membership_invalidation_preserves_refresh_backoff(tmp_path: Path) -> None:
    """Repeated uncertain responses must not bypass the bounded refresh retry delay."""
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant"]),
    }
    bot._runtime_view.agent_reply_memberships.invalidate(bot.config, reason="test")

    with patch("mindroom.agent_reply_membership_sync.time.monotonic", return_value=100.0):
        await bot._refresh_agent_reply_memberships_if_needed()
        bot._invalidate_agent_reply_memberships(reason="uncertain_sync_response")
        await bot._refresh_agent_reply_memberships_if_needed()

    orchestrator.refresh_agent_reply_memberships.assert_awaited_once_with()


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
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant"]),
    }
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
    assert index.is_allowed(sender_id, ["grant"], bot.config.authorization)
    fence_started = asyncio.Event()
    release_fence = asyncio.Event()

    async def delayed_fence(*_args: object) -> None:
        fence_started.set()
        await release_fence.wait()

    response = (
        _sync_response_with_room_membership_section(room_id, membership="leave")
        if transport == "classic"
        else _sliding_response_with_room_membership(room_id, membership="leave")
    )
    apply_membership = (
        bot._apply_own_room_membership_from_sync
        if isinstance(response, nio.SyncResponse)
        else bot._apply_own_room_membership_from_sliding_sync
    )
    with patch.object(
        type(bot._membership_fence),
        "fence_reported_departures",
        side_effect=delayed_fence,
    ):
        apply_task = asyncio.create_task(apply_membership(response))
        await asyncio.wait_for(fence_started.wait(), timeout=1)
        try:
            assert not index.is_allowed(sender_id, ["grant"], bot.config.authorization)
            assert index.needs_refresh(bot.config.authorization)
        finally:
            release_fence.set()
            await apply_task

    orchestrator.revoke_reply_authorized_calls.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_router_leave_then_rejoin_in_one_sync_requires_grant_refresh(tmp_path: Path) -> None:
    """Any router continuity gap must require a fresh authoritative grant snapshot."""
    room_id = "!grant:localhost"
    sender_id = "@alice:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    orchestrator.revoke_reply_authorized_calls = AsyncMock()
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant"]),
    }
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

    await bot._apply_own_room_membership(
        OwnRoomMembership(
            joined_room_ids=frozenset({room_id}),
            left_room_ids=frozenset(),
            invited_room_ids=frozenset(),
            departures=(
                ReportedDeparture(
                    room_id=room_id,
                    observation_id="$leave",
                    rejoined_after=True,
                ),
            ),
        ),
    )

    assert not index.is_allowed(sender_id, ["grant"], bot.config.authorization)
    assert index.needs_refresh(bot.config.authorization)
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
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant"]),
    }
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
    if transport == "classic":
        response = _empty_classic_sync_response("s-after-invite")
        response.rooms.invite[room_id] = nio.InviteInfo(invite_state=[])
    else:
        response = _sliding_response_with_room_membership(room_id, membership="invite")

    bot._before_sync_response_admission(response)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config.authorization)
    assert index.needs_refresh(bot.config.authorization)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
@pytest.mark.parametrize("membership", ["leave", "ban", "invite"])
async def test_grant_user_revocation_precedes_cross_room_timeline_admission(
    tmp_path: Path,
    transport: str,
    membership: str,
) -> None:
    """A sibling-room message cannot race ahead of a grant-room revocation."""
    grant_room_id = "!grant:localhost"
    target_room_id = "!target:localhost"
    sender_id = "@alice:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    orchestrator.revoke_reply_authorized_calls = AsyncMock()
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant"]),
    }
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
    message_event = {
        "content": {"body": "same sync", "msgtype": "m.text"},
        "event_id": "$message",
        "origin_server_ts": 1,
        "sender": sender_id,
        "type": "m.room.message",
    }
    if transport == "classic":
        response = nio.SyncResponse.from_dict(
            {
                "next_batch": "s-cross-room-revoke",
                "rooms": {
                    "invite": {},
                    "leave": {},
                    "join": {
                        target_room_id: {
                            "state": {"events": []},
                            "timeline": {"events": [message_event], "limited": False},
                        },
                        grant_room_id: {
                            "state": {"events": []},
                            "timeline": {"events": [member_event], "limited": False},
                        },
                    },
                },
            },
        )
        assert isinstance(response, nio.SyncResponse)
    else:
        response = nio.SlidingSyncResponse.from_dict(
            {
                "pos": "s-cross-room-revoke",
                "rooms": {
                    target_room_id: {"membership": "join", "timeline": [message_event]},
                    grant_room_id: {"membership": "join", "timeline": [member_event]},
                },
            },
        )
        assert isinstance(response, nio.SlidingSyncResponse)

    bot._before_sync_response_admission(response)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config.authorization)
    assert not index.needs_refresh(bot.config.authorization)
    orchestrator.revoke_reply_authorized_calls.assert_awaited_once_with()


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
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant"]),
    }
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
    if transport == "classic":
        response = nio.SyncResponse.from_dict(
            {
                "next_batch": "s-join",
                "rooms": {
                    "invite": {},
                    "leave": {},
                    "join": {
                        room_id: {
                            "state": {"events": []},
                            "timeline": {"events": [member_event], "limited": False},
                        },
                    },
                },
            },
        )
        assert isinstance(response, nio.SyncResponse)
    else:
        response = nio.SlidingSyncResponse.from_dict(
            {
                "pos": "s-join",
                "rooms": {room_id: {"membership": "join", "timeline": [member_event]}},
            },
        )
        assert isinstance(response, nio.SlidingSyncResponse)

    bot._before_sync_response_admission(response)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert not index.is_allowed(sender_id, ["grant"], bot.config.authorization)
    orchestrator.revoke_reply_authorized_calls.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["classic", "sliding"])
@pytest.mark.parametrize("membership", ["leave", "ban"])
async def test_grant_user_join_superseded_in_same_sync_never_temporarily_grants(
    tmp_path: Path,
    transport: str,
    membership: str,
) -> None:
    """A batch's known final revocation must fence its earlier positive event."""
    room_id = "!grant:localhost"
    sender_id = "@bob:localhost"
    bot, orchestrator = _router_bot_with_orchestrator(tmp_path)
    bot.config.authorization.agent_reply_permissions = {
        "router": AgentReplyPermission(joined_rooms=["grant"]),
    }
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
    if transport == "classic":
        response = nio.SyncResponse.from_dict(
            {
                "next_batch": "s-join-then-revoke",
                "rooms": {
                    "invite": {},
                    "leave": {},
                    "join": {
                        room_id: {
                            "state": {"events": []},
                            "timeline": {
                                "events": [join_event, revoke_event],
                                "limited": False,
                            },
                        },
                    },
                },
            },
        )
        assert isinstance(response, nio.SyncResponse)
        live_join_event, live_revoke_event = response.rooms.join[room_id].timeline.events
    else:
        response = nio.SlidingSyncResponse.from_dict(
            {
                "pos": "s-join-then-revoke",
                "rooms": {
                    room_id: {
                        "membership": "join",
                        "timeline": [join_event, revoke_event],
                    },
                },
            },
        )
        assert isinstance(response, nio.SlidingSyncResponse)
        live_join_event, live_revoke_event = response.rooms[room_id].timeline
    assert isinstance(live_join_event, nio.RoomMemberEvent)
    assert isinstance(live_revoke_event, nio.RoomMemberEvent)

    orchestrator.reconcile_reply_authorized_calls = AsyncMock()
    bot._before_sync_response_admission(response)
    assert not index.is_allowed(sender_id, ["grant"], bot.config.authorization)

    await bot._apply_live_reply_membership_transition(room_id, live_join_event)
    assert not index.is_allowed(sender_id, ["grant"], bot.config.authorization)
    await bot._apply_live_reply_membership_transition(room_id, live_revoke_event)

    assert not index.is_allowed(sender_id, ["grant"], bot.config.authorization)
    orchestrator.reconcile_reply_authorized_calls.assert_not_awaited()

    bot._router_reply_membership_sync.finish_response(response)
    later_join = nio.RoomMemberEvent.from_dict(
        _departure_member_event("$later-join", user_id=sender_id, membership="join", ts=3),
    )
    assert isinstance(later_join, nio.RoomMemberEvent)
    await bot._apply_live_reply_membership_transition(room_id, later_join)

    assert index.is_allowed(sender_id, ["grant"], bot.config.authorization)
    orchestrator.reconcile_reply_authorized_calls.assert_awaited_once_with()


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


def test_bot_config_setter_updates_existing_call_manager(tmp_path: Path) -> None:
    """An unchanged call bot should observe authorization-only hot reloads."""
    bot = _agent_bot(tmp_path)
    call_manager = MagicMock()
    install_call_manager_mock(bot, call_manager)
    new_config = _config(tmp_path)

    bot.config = new_config

    call_manager.update_config.assert_called_once_with(new_config)


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
    _observe_provenance(membership_event.event_id, nio.TimelineEventProvenance.HISTORY)
    await membership_callback(room, membership_event)
    _observe_provenance(call_event.event_id, nio.TimelineEventProvenance.HISTORY)
    await call_callback(room, call_event)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    call_manager.on_room_membership_event.assert_not_awaited()
    call_manager.on_room_event.assert_not_awaited()

    _observe_provenance(membership_event.event_id, nio.TimelineEventProvenance.LIVE)
    await membership_callback(room, membership_event)
    _observe_provenance(call_event.event_id, nio.TimelineEventProvenance.LIVE)
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
    _observe_provenance(membership_event.event_id, nio.TimelineEventProvenance.HISTORY)
    await membership_callback(room, membership_event)
    _observe_provenance(membership_event.event_id, nio.TimelineEventProvenance.LIVE)
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
    await bot._journal_dispatcher.admit_out_of_band(
        room,
        membership_event,
        EventKind.ROOM_LIFECYCLE,
        EventClass.ACTIONABLE,
    )

    with patch("mindroom.bot.maybe_build_call_manager", return_value=call_manager):
        bot._register_call_manager_callbacks(client)

    membership_callback = client.add_event_callback.call_args_list[0].args[0]
    _observe_provenance(membership_event.event_id, nio.TimelineEventProvenance.HISTORY)
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
async def test_a_kick_after_a_rejoin_is_not_absorbed_by_the_earlier_leaves_report(
    tmp_path: Path,
) -> None:
    """Two departures in one sync interval are two departures, not one room id.

    The bot left the room itself, so it is owed exactly one sync report for
    that leave. It came back, and was then removed again before the next sync.
    The response shows the room once, and offered to the fence as a room id it
    is one observation -- absorbed as the report the first leave was owed,
    leaving the kick to invalidate nothing at all. Everything the second
    membership built then survives into a membership that has no right to it.
    """
    bot = _agent_bot(tmp_path)
    room_id = "!departed:localhost"
    user_id = bot.agent_user.user_id
    await bot._membership_fence.fence_local_departure(room_id)
    await bot._membership_fence.note_membership_restarted(room_id)
    epoch_after_rejoin = await bot._journal_principal().membership_epoch(room_id)
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": "s-after-kick",
            "rooms": {
                "invite": {},
                "join": {},
                "leave": {
                    room_id: {
                        "state": {"events": []},
                        "timeline": {
                            "events": [
                                _departure_member_event("$leave", user_id=user_id, membership="leave", ts=1),
                                {
                                    "content": {"membership": "join"},
                                    "event_id": "$rejoin",
                                    "origin_server_ts": 2,
                                    "sender": user_id,
                                    "state_key": user_id,
                                    "type": "m.room.member",
                                },
                                _departure_member_event("$kick", user_id=user_id, membership="leave", ts=3),
                            ],
                            "limited": False,
                            "prev_batch": "s-before-kick",
                        },
                    },
                },
            },
        },
    )

    await bot._apply_own_room_membership_from_sync(response)

    assert await bot._journal_principal().membership_epoch(room_id) == epoch_after_rejoin + 1


@pytest.mark.asyncio
async def test_replaying_one_sync_response_fences_its_departures_once(
    tmp_path: Path,
) -> None:
    """A response whose checkpoint could not advance is presented again as it was."""
    bot = _agent_bot(tmp_path)
    room_id = "!departed:localhost"
    response = _sync_response_with_room_membership_section(room_id, membership="leave")

    await bot._apply_own_room_membership_from_sync(response)
    await bot._apply_own_room_membership_from_sync(response)

    assert await bot._journal_principal().membership_epoch(room_id) == 1


@pytest.mark.asyncio
async def test_replayed_truncated_leave_cannot_fence_a_rejoined_membership(tmp_path: Path) -> None:
    """The response token identifies a leave whose timeline omits its event."""
    bot = _agent_bot(tmp_path)
    room_id = "!departed:localhost"
    response = _sync_response_with_room_membership_section(room_id, membership="leave")

    await bot._apply_own_room_membership_from_sync(response)
    await bot._membership_fence.note_membership_restarted(room_id)
    epoch_after_rejoin = await bot._journal_principal().membership_epoch(room_id)
    await bot._apply_own_room_membership_from_sync(response)

    assert await bot._journal_principal().membership_epoch(room_id) == epoch_after_rejoin


@pytest.mark.asyncio
async def test_replayed_departure_cannot_leave_a_confirmed_join_fenced(tmp_path: Path) -> None:
    """A duplicated old leave report cannot invalidate a confirmed rejoin."""
    bot = _agent_bot(tmp_path)
    room_id = "!rejoined:localhost"
    user_id = bot.agent_user.user_id
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
                                _departure_member_event("$leave", user_id=user_id, membership="leave", ts=1),
                                {
                                    "content": {"membership": "join"},
                                    "event_id": "$rejoin",
                                    "origin_server_ts": 2,
                                    "sender": user_id,
                                    "state_key": user_id,
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
    await bot._membership_fence.fence_local_departure(room_id)
    await bot._membership_fence.note_membership_restarted(room_id)
    epoch_after_rejoin = await bot._journal_principal().membership_epoch(room_id)
    await bot._apply_own_room_membership_from_sync(response)
    await bot._apply_own_room_membership_from_sync(response)

    assert await bot._journal_principal().membership_epoch(room_id) == epoch_after_rejoin


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["leave", "ban"])
async def test_joined_sync_timeline_departure_fences_even_when_a_rejoin_follows(
    tmp_path: Path,
    membership: str,
) -> None:
    """A departure only the timeline reports must still fence the room.

    The room's final membership in this response is `join`, so the join/leave
    sections alone say nothing happened. The projection built before the
    departure describes a membership that ended, and the epoch is what drops it.
    """
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
    await bot._apply_own_room_membership_from_sync(response)

    principal = bot._journal_principal()
    assert await principal.membership_epoch(room_id) == 1
    assert await membership_epoch_is_active(principal, room_id, 1)


@pytest.mark.asyncio
async def test_sync_join_section_reaches_call_manager(
    tmp_path: Path,
) -> None:
    """A room in the sync join section can clear departed call state."""
    bot = _agent_bot(tmp_path)
    client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    room_id = "!configured-call:localhost"
    bot.client = client
    call_manager = MagicMock()
    call_manager.on_sync_room_membership = AsyncMock()
    install_call_manager_mock(bot, call_manager)

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
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
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

    with (
        patch("mindroom.bot.mark_matrix_sync_success", return_value=datetime.now(UTC)),
        patch("mindroom.hooks.sender.send_matrix_message", side_effect=mock_send),
    ):
        await bot._on_sync_response(_empty_classic_sync_response("s-ready-hook"))

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
