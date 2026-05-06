"""Tests for Matrix room-member hook emission."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.hooks import EVENT_ROOM_MEMBER_JOINED, HookRegistry, RoomMemberJoinedContext, hook
from mindroom.matrix import room_member_joins
from mindroom.matrix.sync_certification import SyncCacheWriteResult, SyncTrustState
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


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


def _sync_response_with_state(room_id: str, events: list[object]) -> nio.SyncResponse:
    response = MagicMock()
    response.__class__ = nio.SyncResponse
    response.next_batch = "s_next"
    response.rooms = SimpleNamespace(
        join={
            room_id: SimpleNamespace(
                state=events,
                timeline=SimpleNamespace(events=[], limited=False),
            ),
        },
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
    bot = AgentBot(_router_user(), tmp_path, config=config, runtime_paths=runtime_paths)
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
    return AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths)


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

    bot.client.add_event_callback.assert_called_once()
    assert bot.client.add_event_callback.call_args.args[1] is nio.RoomMemberEvent


def test_non_router_does_not_register_room_member_callback(tmp_path: Path) -> None:
    """Non-router bots should not register duplicate member-event callbacks."""
    bot = _agent_bot(tmp_path)
    bot.client = MagicMock()

    bot._register_room_member_callback_after_initial_sync()

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
    assert context.first_join is True
    assert context.matrix_admin is not None


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
        bot,
        "_sync_cache_result_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(_sync_response_with_state(room.room_id, [_room_member_event(event_id="$state-join")]))

    assert seen == ["$state-join"]


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
        bot,
        "_sync_cache_result_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )

    await bot._on_sync_response(
        _sync_response_with_state(
            room.room_id,
            [_room_member_event(event_id="$snapshot-join", prev_membership=None)],
        ),
    )

    assert seen == []


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
        bot,
        "_sync_cache_result_for_certification",
        AsyncMock(return_value=SyncCacheWriteResult(complete=True)),
    )
    sync_error = MagicMock(spec=nio.SyncError)
    sync_error.status_code = "M_UNKNOWN_POS"

    await bot._on_sync_error(sync_error)
    await bot._on_room_member(room, _room_member_event(event_id="$timeline-snapshot"))
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
    bot._sync_trust_state = SyncTrustState.PENDING
    bot.client.rooms = {room.room_id: room}
    bot.client.next_batch = "s_restored"
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    monkeypatch.setattr(
        bot,
        "_sync_cache_result_for_certification",
        AsyncMock(
            side_effect=[
                SyncCacheWriteResult(complete=False),
                SyncCacheWriteResult(complete=True),
            ],
        ),
    )

    await bot._on_sync_response(_sync_response_with_state(room.room_id, []))
    assert bot.client.next_batch is None

    await bot._on_room_member(room, _room_member_event(event_id="$timeline-snapshot"))
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
async def test_room_member_joined_deduplicates_concurrent_same_user_marking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent duplicate joins should still produce one hook payload."""
    bot = _router_bot(tmp_path)
    room = _room()
    save_started = threading.Event()
    release_save = threading.Event()
    original_save = room_member_joins._save_room_member_joins

    def delayed_save(path: Path, seen: dict[str, set[str]]) -> None:
        save_started.set()
        assert release_save.wait(timeout=2.0)
        original_save(path, seen)

    monkeypatch.setattr(room_member_joins, "_save_room_member_joins", delayed_save)

    first_task = asyncio.create_task(
        asyncio.to_thread(
            room_member_joins.room_member_join_from_event,
            room,
            _room_member_event(event_id="$join1"),
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            storage_root=bot.runtime_paths.storage_root,
        ),
    )
    assert await asyncio.to_thread(save_started.wait, 2.0)
    second_task = asyncio.create_task(
        asyncio.to_thread(
            room_member_joins.room_member_join_from_event,
            room,
            _room_member_event(event_id="$join2"),
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            storage_root=bot.runtime_paths.storage_root,
        ),
    )
    await asyncio.sleep(0.05)
    release_save.set()

    results = await asyncio.gather(first_task, second_task)

    joins = [result for result in results if result is not None]
    assert len(joins) == 1
    assert joins[0].event_id == "$join1"


@pytest.mark.asyncio
async def test_room_member_joined_ignores_initial_sync_history(tmp_path: Path) -> None:
    """Initial sync history should not be treated as live onboarding input."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.user_id)

    bot = _router_bot(tmp_path)
    bot._first_sync_done = False
    bot.hook_registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    await bot._on_room_member(_room(), _room_member_event())

    assert seen == []


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
    internal_user_id = bot.config.get_mindroom_user_id(bot.runtime_paths)
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
