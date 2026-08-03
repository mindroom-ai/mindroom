"""Tests for Matrix room-member hook emission."""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_obligations import DispatchCallbackKind
from mindroom.entity_resolution import mindroom_user_id
from mindroom.hooks import EVENT_ROOM_MEMBER_JOINED, HookRegistry, RoomMemberJoinedContext, hook
from mindroom.matrix import room_member_joins
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncCheckpoint, SyncTrustState
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, install_runtime_cache_support, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.sync_continuity_helpers import load_sync_checkpoint

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.dispatch_obligations.storage import DispatchObligation, DispatchObligationKey


def _plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}"),
        plugin_order=0,
    )


def _room(room_id: str = "!lobby:localhost") -> MagicMock:
    room = MagicMock()
    room.room_id = room_id
    room.canonical_alias = "#lobby:localhost"
    return room


def _router_user() -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )


def _room_member_event(
    *,
    event_id: str = "$join",
    user_id: str = "@alice:localhost",
    sender: str | None = None,
    membership: str = "join",
    prev_membership: str | None = "leave",
    display_name: str | None = "Alice",
    avatar_url: str | None = "mxc://localhost/alice",
) -> nio.RoomMemberEvent:
    content: dict[str, object] = {"membership": membership}
    if display_name is not None:
        content["displayname"] = display_name
    if avatar_url is not None:
        content["avatar_url"] = avatar_url
    raw_event: dict[str, object] = {
        "type": "m.room.member",
        "event_id": event_id,
        "sender": sender or user_id,
        "state_key": user_id,
        "origin_server_ts": 1,
        "content": content,
    }
    if prev_membership is not None:
        raw_event["unsigned"] = {"prev_content": {"membership": prev_membership}}
    event = nio.RoomMemberEvent.from_dict(raw_event)
    assert isinstance(event, nio.RoomMemberEvent)
    return event


def _sync_response_with_state(
    room_id: str,
    events: list[object],
    *,
    timeline_events: list[object] | None = None,
    timeline_limited: bool = False,
) -> nio.SyncResponse:
    response = MagicMock()
    response.__class__ = nio.SyncResponse
    response.next_batch = "s_next"
    response.unrecovered_room_ids = frozenset()
    response.rooms = SimpleNamespace(
        invite={},
        join={
            room_id: SimpleNamespace(
                state=events,
                timeline=SimpleNamespace(events=timeline_events or [], limited=timeline_limited),
            ),
        },
        leave={},
    )
    return cast("nio.SyncResponse", response)


def _router_bot(
    tmp_path: Path,
    *,
    bot_accounts: list[str] | None = None,
    mindroom_user: dict[str, str] | None = None,
) -> AgentBot:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config(bot_accounts=bot_accounts or [], mindroom_user=mindroom_user), runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames={ROUTER_AGENT_NAME: "mindroom_router"})
    bot = AgentBot(_router_user(), tmp_path, config=config, runtime_paths=runtime_paths)
    install_runtime_cache_support(bot)
    bot.client = MagicMock()
    bot.client.homeserver = "http://localhost:8008"
    bot._first_sync_done = True
    bot._room_member_join_hooks_armed = True
    return bot


def _agent_bot(tmp_path: Path) -> AgentBot:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(Config(), runtime_paths)
    agent_user = AgentMatrixUser(
        agent_name="helper",
        user_id="@mindroom_helper:localhost",
        display_name="Helper",
        password=TEST_PASSWORD,
    )
    return install_runtime_cache_support(AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths))


def test_room_member_joined_is_a_builtin_hook_event() -> None:
    """room:member_joined should be accepted as a built-in hook event."""

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        del ctx

    registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    assert registry.has_hooks(EVENT_ROOM_MEMBER_JOINED)


def test_router_registers_room_member_callback_after_initial_sync(tmp_path: Path) -> None:
    """The router should start listening for member events only after startup sync."""
    bot = _router_bot(tmp_path)

    bot._register_room_member_callback_after_initial_sync()
    bot._register_room_member_callback_after_initial_sync()

    bot.client.add_event_admission_callback.assert_not_called()
    bot.client.add_event_callback.assert_called_once()
    assert bot.client.add_event_callback.call_args.args[1] is nio.RoomMemberEvent


def test_non_router_does_not_register_room_member_callback(tmp_path: Path) -> None:
    """Non-router bots should not register duplicate member-event callbacks."""
    bot = _agent_bot(tmp_path)
    bot.client = MagicMock()

    bot._register_room_member_callback_after_initial_sync()

    bot.client.add_event_admission_callback.assert_not_called()
    bot.client.add_event_callback.assert_not_called()


@pytest.mark.asyncio
async def test_router_emits_room_member_joined_once_per_room_user(tmp_path: Path) -> None:
    """The router should emit one onboarding hook per room/user pair."""
    seen: list[RoomMemberJoinedContext] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx)

    bot = _router_bot(tmp_path)
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()

    await bot._on_room_member(room, _room_member_event(event_id="$join1"))
    await bot._on_room_member(room, _room_member_event(event_id="$join2"))

    assert len(seen) == 1
    context = seen[0]
    assert context.agent_name == ROUTER_AGENT_NAME
    assert context.room_id == "!lobby:localhost"
    assert context.event_id == "$join1"
    assert context.user_id == "@alice:localhost"
    assert context.sender_id == "@alice:localhost"
    assert context.membership == "join"
    assert context.prev_membership == "leave"
    assert context.display_name == "Alice"
    assert context.avatar_url == "mxc://localhost/alice"
    assert context.matrix_admin is not None


def test_room_member_marker_returns_normally_or_raises_without_boolean_status(tmp_path: Path) -> None:
    """A duplicate marker is successful idempotence, not a false write result."""
    bot = _router_bot(tmp_path)
    join = room_member_joins._room_member_join_from_event(
        _room(),
        _room_member_event(),
        config=bot.config,
        runtime_paths=bot.runtime_paths,
    )
    assert join is not None

    first_result = room_member_joins._record_room_member_join_seen(
        bot.runtime_paths.storage_root,
        join,
    )
    duplicate_result = room_member_joins._record_room_member_join_seen(
        bot.runtime_paths.storage_root,
        join,
    )

    assert first_result is None
    assert duplicate_result is None


def test_room_member_marker_fsyncs_payload_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed hook marker must survive the same crash as its certified checkpoint."""
    bot = _router_bot(tmp_path)
    join = room_member_joins._room_member_join_from_event(
        _room(),
        _room_member_event(),
        config=bot.config,
        runtime_paths=bot.runtime_paths,
    )
    assert join is not None
    fsynced_directory_flags: list[bool] = []

    def track_fsync(file_descriptor: int) -> None:
        fsynced_directory_flags.append(stat.S_ISDIR(os.fstat(file_descriptor).st_mode))

    monkeypatch.setattr("mindroom.durable_write.os.fsync", track_fsync)

    room_member_joins._record_room_member_join_seen(
        bot.runtime_paths.storage_root,
        join,
    )

    assert fsynced_directory_flags == [False, True]


@pytest.mark.asyncio
async def test_cancelled_room_member_hook_does_not_suppress_durable_retry(tmp_path: Path) -> None:
    """The room/user de-dup marker must follow completed hook emission."""
    attempts = 0
    entered = asyncio.Event()
    blocker = asyncio.Event()

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(_ctx: RoomMemberJoinedContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await blocker.wait()

    bot = _router_bot(tmp_path)
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()
    event = _room_member_event(event_id="$retry")

    first = asyncio.create_task(bot._on_room_member(room, event))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await bot._on_room_member(room, event)

    assert attempts == 2


@pytest.mark.asyncio
async def test_room_member_joined_supports_router_agent_scope(tmp_path: Path) -> None:
    """room:member_joined hooks should support router agent scoping."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED, agents=[ROUTER_AGENT_NAME])
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.user_id)

    bot = _router_bot(tmp_path)
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    await bot._on_room_member(_room(), _room_member_event())

    assert seen == ["@alice:localhost"]


@pytest.mark.asyncio
async def test_router_emits_live_room_member_join_without_previous_membership(tmp_path: Path) -> None:
    """Live member joins can omit unsigned previous membership."""
    seen: list[RoomMemberJoinedContext] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx)

    bot = _router_bot(tmp_path)
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    await bot._on_room_member(_room(), _room_member_event(event_id="$sso-autojoin", prev_membership=None))

    assert len(seen) == 1
    assert seen[0].event_id == "$sso-autojoin"
    assert seen[0].prev_membership is None


@pytest.mark.asyncio
async def test_router_emits_room_member_joined_from_sync_state_after_initial_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live joins delivered through sync room state should trigger onboarding hooks."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(_sync_response_with_state(room.room_id, [_room_member_event(event_id="$state-join")]))

    assert seen == ["$state-join"]


@pytest.mark.asyncio
async def test_cancelled_sync_state_member_hook_is_directly_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classic sync callback cancellation must leave transport-neutral exact work."""
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(_ctx: RoomMemberJoinedContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            await release.wait()

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )
    sync_task = asyncio.create_task(
        bot._on_sync_response(
            _sync_response_with_state(room.room_id, [_room_member_event(event_id="$state-retry")]),
        ),
    )
    await entered.wait()

    sync_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sync_task
    assert bot._dispatch_obligation_store.has_pending(
        "$state-retry",
        DispatchCallbackKind.ROOM_LIFECYCLE,
    )
    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None

    await bot._dispatch_obligation_runner.recover_pending()

    assert attempts == 2
    assert not bot._dispatch_obligation_store.has_pending(
        "$state-retry",
        DispatchCallbackKind.ROOM_LIFECYCLE,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_token", [None, "s_before_marker_failure"])
async def test_sync_state_marker_failure_blocks_checkpoint_certification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry_token: str | None,
) -> None:
    """A baseline marker must reach disk before its source sync position can advance."""

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(_ctx: RoomMemberJoinedContext) -> None:
        pass

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_after_marker_failure"
    if retry_token is not None:
        bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
        bot._sync_cache_trust.checkpoint = SyncCheckpoint(retry_token)
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    def failing_write(
        path: Path,
        payload: object,
        *,
        indent: int,
        trailing_newline: bool,
    ) -> None:
        del path, payload, indent, trailing_newline
        message = "marker unavailable"
        raise OSError(message)

    monkeypatch.setattr(room_member_joins, "write_json_file_durable", failing_write)

    with pytest.raises(RuntimeError, match="room-member join tracking"):
        await bot._on_sync_response(
            _sync_response_with_state(
                room.room_id,
                [_room_member_event(event_id="$snapshot", prev_membership=None)],
            ),
        )

    assert load_sync_checkpoint(tmp_path, bot.agent_name) is None
    assert bot.client.next_batch == "s_after_marker_failure"
    assert bot._sync_cache_trust.rewind_is_deferred_until_recovery()


@pytest.mark.asyncio
async def test_sync_room_lifecycle_persist_failure_rewinds_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct lifecycle acceptance failure must invoke the shared rewind exactly once."""

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(_ctx: RoomMemberJoinedContext) -> None:
        pass

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_after_failure"
    bot._sync_cache_trust.state = SyncTrustState.CERTIFIED
    bot._sync_cache_trust.checkpoint = SyncCheckpoint("s_before_failure")
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    def fail_create(_obligation: object) -> object:
        message = "dispatch database unavailable"
        raise OSError(message)

    persist_failure = MagicMock(wraps=bot._rewind_sync_after_pre_certification_failure)
    bot._dispatch_obligation_runner.on_persist_failure = persist_failure
    monkeypatch.setattr(bot._dispatch_obligation_store, "create_pending", fail_create)

    with pytest.raises(OSError, match="dispatch database unavailable"):
        await bot._on_sync_response(
            _sync_response_with_state(
                room.room_id,
                [_room_member_event(event_id="$lifecycle-persist-failure")],
            ),
        )

    persist_failure.assert_called_once_with()
    assert bot.client.next_batch == "s_before_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("record_only", [False, True])
async def test_sync_state_baseline_markers_batch_one_worker_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    record_only: bool,
) -> None:
    """Full-state baseline recording must write once without blocking the event loop."""

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(_ctx: RoomMemberJoinedContext) -> None:
        pass

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    event_loop_thread = threading.get_ident()
    save_threads: list[int] = []
    original_save = room_member_joins._save_room_member_joins

    def tracked_save(path: Path, seen: dict[str, set[str]]) -> None:
        save_threads.append(threading.get_ident())
        original_save(path, seen)

    monkeypatch.setattr(room_member_joins, "_save_room_member_joins", tracked_save)
    events = [
        _room_member_event(event_id="$alice", user_id="@alice:localhost", prev_membership=None),
        _room_member_event(event_id="$bob", user_id="@bob:localhost", prev_membership=None),
    ]

    await bot._emit_room_member_joined_sync_state_hooks(
        _sync_response_with_state(room.room_id, events),
        record_only=record_only,
    )

    assert len(save_threads) == 1
    assert save_threads[0] != event_loop_thread
    assert room_member_joins._room_member_join_is_seen(
        bot.runtime_paths.storage_root,
        room_id=room.room_id,
        user_id="@alice:localhost",
    )
    assert room_member_joins._room_member_join_is_seen(
        bot.runtime_paths.storage_root,
        room_id=room.room_id,
        user_id="@bob:localhost",
    )


@pytest.mark.asyncio
async def test_sync_state_marker_update_waits_for_live_marker_lock(tmp_path: Path) -> None:
    """Sync baseline and live completion markers must serialize through one bot lock."""
    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    await bot._room_member_join_lock.acquire()
    marker_task = asyncio.create_task(
        bot._emit_room_member_joined_sync_state_hooks(
            _sync_response_with_state(
                room.room_id,
                [_room_member_event(event_id="$baseline", prev_membership=None)],
            ),
            record_only=True,
        ),
    )
    try:
        await asyncio.sleep(0.05)
        assert not marker_task.done()
    finally:
        bot._room_member_join_lock.release()
        await marker_task

    assert room_member_joins._room_member_join_is_seen(
        bot.runtime_paths.storage_root,
        room_id=room.room_id,
        user_id="@alice:localhost",
    )


@pytest.mark.asyncio
async def test_sync_state_lifecycle_dispatch_does_not_hold_marker_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lifecycle callback owns live de-dup, so sync orchestration cannot lock around dispatch."""
    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    dispatched: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(_ctx: RoomMemberJoinedContext) -> None:
        pass

    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    async def dispatch(
        _room: nio.MatrixRoom,
        event: nio.Event,
        callback_kind: DispatchCallbackKind,
    ) -> None:
        assert not bot._room_member_join_lock.locked()
        assert callback_kind is DispatchCallbackKind.ROOM_LIFECYCLE
        dispatched.append(event.event_id)

    monkeypatch.setattr(bot._dispatch_obligation_runner, "dispatch", dispatch)

    await bot._emit_room_member_joined_sync_state_hooks(
        _sync_response_with_state(room.room_id, [_room_member_event(event_id="$dispatch")]),
    )

    assert dispatched == ["$dispatch"]


@pytest.mark.asyncio
async def test_router_emits_room_member_joined_from_first_restored_token_sync_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first sync after a restored certified token should emit missed live joins."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot._first_sync_done = False
    bot._room_member_join_hooks_armed = False
    bot._sync_cache_trust.state = SyncTrustState.PENDING
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_restored"
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    bot._emit_agent_lifecycle_event = AsyncMock()
    bot._maybe_start_startup_thread_prewarm = MagicMock()
    bot._maybe_start_deferred_overdue_task_drain = MagicMock()
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(
        _sync_response_with_state(
            room.room_id,
            [],
            timeline_events=[_room_member_event(event_id="$catchup-join", prev_membership=None)],
        ),
    )

    assert seen == ["$catchup-join"]


@pytest.mark.asyncio
async def test_router_ignores_restored_token_first_sync_full_state_member_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restored-token first-sync state is full state, not a live join stream."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot._first_sync_done = False
    bot._room_member_join_hooks_armed = False
    bot._sync_cache_trust.state = SyncTrustState.PENDING
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_restored"
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    bot._emit_agent_lifecycle_event = AsyncMock()
    bot._maybe_start_startup_thread_prewarm = MagicMock()
    bot._maybe_start_deferred_overdue_task_drain = MagicMock()
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(
        _sync_response_with_state(
            room.room_id,
            [_room_member_event(event_id="$full-state-existing-member")],
        ),
    )

    assert seen == []

    await bot._on_room_member(
        room,
        _room_member_event(event_id="$profile-update", prev_membership=None),
    )

    assert seen == []


@pytest.mark.asyncio
async def test_router_ignores_restored_token_timeline_profile_update_for_existing_member(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restored-token timeline member updates should not onboard users already in state."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot._first_sync_done = False
    bot._room_member_join_hooks_armed = False
    bot._sync_cache_trust.state = SyncTrustState.PENDING
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_restored"
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    bot._emit_agent_lifecycle_event = AsyncMock()
    bot._maybe_start_startup_thread_prewarm = MagicMock()
    bot._maybe_start_deferred_overdue_task_drain = MagicMock()
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(
        _sync_response_with_state(
            room.room_id,
            [_room_member_event(event_id="$existing-member", prev_membership=None)],
            timeline_events=[_room_member_event(event_id="$profile-update", prev_membership=None)],
        ),
    )

    assert seen == []


@pytest.mark.asyncio
async def test_router_ignores_sync_state_member_snapshot_without_previous_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync state snapshots without a membership transition should not trigger onboarding."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(
        _sync_response_with_state(
            room.room_id,
            [_room_member_event(event_id="$snapshot-join", prev_membership=None)],
        ),
    )

    assert seen == []

    await bot._on_room_member(
        room,
        _room_member_event(event_id="$profile-update", prev_membership=None),
    )

    assert seen == []


@pytest.mark.asyncio
async def test_router_ignores_limited_sync_state_member_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limited sync state is not a live join stream."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True, limited_room_ids=(room.room_id,))),
    )

    await bot._on_sync_response(
        _sync_response_with_state(
            room.room_id,
            [_room_member_event(event_id="$limited-state")],
            timeline_limited=True,
        ),
    )

    assert seen == []


@pytest.mark.asyncio
async def test_limited_state_snapshot_does_not_settle_delayed_lifecycle_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot marker must not overtake exact pending lifecycle work."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    event = _room_member_event(event_id="$delayed-lifecycle")
    obligation = await bot._dispatch_obligation_runner.persist(
        room,
        event,
        DispatchCallbackKind.ROOM_LIFECYCLE,
    )
    assert obligation is not None

    lookup_started = threading.Event()
    release_lookup = threading.Event()
    pending_for = bot._dispatch_obligation_store.pending_for

    def delayed_pending_for(key: DispatchObligationKey) -> DispatchObligation | None:
        lookup_started.set()
        assert release_lookup.wait(timeout=1.0)
        return pending_for(key)

    monkeypatch.setattr(bot._dispatch_obligation_store, "pending_for", delayed_pending_for)
    callback = bot._dispatch_obligation_runner.task_wrapper(
        DispatchCallbackKind.ROOM_LIFECYCLE,
        owner=bot._runtime_view,
    )
    await callback(room, event)
    assert await asyncio.to_thread(lookup_started.wait, 1.0)

    await bot._emit_room_member_joined_sync_state_hooks(
        _sync_response_with_state(
            room.room_id,
            [_room_member_event(event_id="$profile-snapshot", prev_membership="join")],
            timeline_limited=True,
        ),
    )
    release_lookup.set()
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert seen == ["$delayed-lifecycle"]
    assert not bot._dispatch_obligation_store.has_pending(
        event.event_id,
        DispatchCallbackKind.ROOM_LIFECYCLE,
    )


@pytest.mark.asyncio
async def test_unknown_pos_resync_does_not_emit_room_member_joined_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tokenless resync after M_UNKNOWN_POS should not onboard existing members."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_rejected"
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)
    assert (
        bot._dispatch_obligation_runner._admission_kind(
            _room_member_event(event_id="$timeline-snapshot"),
        )
        is None
    )
    await bot._on_sync_response(
        _sync_response_with_state(
            room.room_id,
            [_room_member_event(event_id="$state-snapshot")],
        ),
    )

    assert seen == []

    await bot._on_room_member(room, _room_member_event(event_id="$live", user_id="@bob:localhost"))

    assert seen == ["$live"]


@pytest.mark.asyncio
async def test_registered_room_member_callback_uses_delivery_time_arming_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued recovery-sync member events should not emit after hooks re-arm."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_rejected"
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    bot._register_room_member_callback_after_initial_sync()
    room_member_admission = bot._dispatch_obligation_runner._admit_source_event
    room_member_callback = bot.client.add_event_callback.call_args.args[0]
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)
    timeline_event = _room_member_event(event_id="$timeline-snapshot")
    await room_member_admission(
        room,
        timeline_event,
        nio.TimelineEventProvenance.HISTORY,
    )
    await room_member_callback(room, timeline_event)
    await bot._on_sync_response(_sync_response_with_state(room.room_id, []))
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert seen == []

    live_event = _room_member_event(event_id="$live", user_id="@bob:localhost")
    await room_member_admission(
        room,
        live_event,
        nio.TimelineEventProvenance.LIVE,
    )
    await room_member_callback(room, live_event)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert seen == ["$live"]


@pytest.mark.asyncio
async def test_member_callback_runs_exact_pending_lifecycle_obligation(
    tmp_path: Path,
) -> None:
    """A member callback may retry its exact durable lifecycle work."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot.client.rooms = {room.room_id: room}
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    bot._first_sync_done = True
    bot._room_member_join_hooks_armed = True
    bot._register_room_member_callback_after_initial_sync()
    room_member_callback = bot.client.add_event_callback.call_args.args[0]
    event = _room_member_event(event_id="$pending-member")
    obligation = await bot._dispatch_obligation_runner.persist(
        room,
        event,
        DispatchCallbackKind.ROOM_LIFECYCLE,
    )
    assert obligation is not None

    await room_member_callback(room, event)
    await wait_for_background_tasks(timeout=1.0, owner=bot._runtime_view)

    assert seen == ["$pending-member"]
    assert not bot._dispatch_obligation_store.has_pending(
        "$pending-member",
        DispatchCallbackKind.ROOM_LIFECYCLE,
    )


@pytest.mark.asyncio
async def test_uncertain_first_sync_reset_does_not_emit_room_member_joined_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tokenless resync after first-sync uncertainty should not onboard existing members."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    room = _room()
    bot._first_sync_done = False
    bot._room_member_join_hooks_armed = False
    bot._sync_cache_trust.state = SyncTrustState.PENDING
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_restored"
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot._conversation_cache,
        "cache_sync_timeline_for_certification",
        AsyncMock(
            side_effect=[
                SyncCacheWriteResult(complete=False),
                SyncCacheWriteResult(complete=True),
            ],
        ),
    )

    await bot._on_sync_response(_sync_response_with_state(room.room_id, []))
    assert bot.client.next_batch is None

    assert (
        bot._dispatch_obligation_runner._admission_kind(
            _room_member_event(event_id="$timeline-snapshot"),
        )
        is None
    )
    await bot._on_sync_response(
        _sync_response_with_state(
            room.room_id,
            [_room_member_event(event_id="$state-snapshot")],
        ),
    )

    assert seen == []

    await bot._on_room_member(room, _room_member_event(event_id="$live", user_id="@bob:localhost"))

    assert seen == ["$live"]


@pytest.mark.asyncio
async def test_room_member_joined_save_failure_remains_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed terminal tracking must surface so the durable obligation stays pending."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()

    def failing_write(
        path: Path,
        payload: object,
        *,
        indent: int,
        trailing_newline: bool,
    ) -> None:
        del path, payload, indent, trailing_newline
        raise OSError

    monkeypatch.setattr(room_member_joins, "write_json_file_durable", failing_write)

    with pytest.raises(RuntimeError, match="Failed to persist completed room-member join") as exc_info:
        await bot._on_room_member(room, _room_member_event(event_id="$join"))

    assert isinstance(exc_info.value.__cause__, OSError)
    assert seen == ["$join"]
    assert not (bot.runtime_paths.storage_root / "tracking" / "room_member_joins.json").exists()


@pytest.mark.asyncio
async def test_room_member_joined_deduplicates_concurrent_same_user_marking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent duplicate joins should still emit one hook payload."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.event_id)

    bot = _router_bot(tmp_path)
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()
    save_started = threading.Event()
    release_save = threading.Event()
    original_save = room_member_joins._save_room_member_joins

    def delayed_save(path: Path, seen: dict[str, set[str]]) -> None:
        save_started.set()
        assert release_save.wait(timeout=2.0)
        original_save(path, seen)

    monkeypatch.setattr(room_member_joins, "_save_room_member_joins", delayed_save)

    first_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
    try:
        first_task = asyncio.create_task(
            bot._on_room_member(room, _room_member_event(event_id="$join1")),
        )
        assert await asyncio.to_thread(save_started.wait, 2.0)
        second_task = asyncio.create_task(
            bot._on_room_member(room, _room_member_event(event_id="$join2")),
        )
        await asyncio.sleep(0.05)
        release_save.set()

        await asyncio.gather(first_task, second_task)
    finally:
        release_save.set()
        pending = [task for task in (first_task, second_task) if task is not None and not task.done()]
        if pending:
            await asyncio.wait(pending, timeout=1.0)
        pending = [task for task in (first_task, second_task) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    assert seen == ["$join1"]


def test_room_member_join_admission_ignores_initial_sync_history(tmp_path: Path) -> None:
    """Initial sync history must not enter the delayed live-callback boundary."""
    bot = _router_bot(tmp_path)
    bot._first_sync_done = False

    assert bot._dispatch_obligation_runner._admission_kind(_room_member_event()) is None


@pytest.mark.asyncio
async def test_room_member_joined_ignores_bot_accounts_and_agents(tmp_path: Path) -> None:
    """Configured bots and internal MindRoom users should not trigger human onboarding hooks."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.user_id)

    bot = _router_bot(
        tmp_path,
        bot_accounts=["@bridge:localhost"],
        mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
    )
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    internal_user_id = mindroom_user_id(bot.config, bot.runtime_paths)
    assert internal_user_id is not None

    await bot._on_room_member(_room(), _room_member_event(event_id="$bridge", user_id="@bridge:localhost"))
    await bot._on_room_member(_room(), _room_member_event(event_id="$agent", user_id="@mindroom_router:localhost"))
    await bot._on_room_member(_room(), _room_member_event(event_id="$internal", user_id=internal_user_id))

    assert seen == []


@pytest.mark.asyncio
async def test_non_router_bots_do_not_emit_room_member_joined(tmp_path: Path) -> None:
    """Only the router should emit room-member join hooks."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.user_id)

    bot = _agent_bot(tmp_path)
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    await bot._on_room_member(cast("nio.MatrixRoom", _room()), _room_member_event())

    assert seen == []
