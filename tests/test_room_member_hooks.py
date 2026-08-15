"""Tests for Matrix room-member hook emission."""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import mindroom_user_id
from mindroom.hooks import EVENT_ROOM_MEMBER_JOINED, HookRegistry, RoomMemberJoinedContext, hook
from mindroom.matrix import room_member_joins
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_matrix_client_mock,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts

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
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
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
    return install_runtime_journal_support(AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths))


def test_room_member_joined_is_a_builtin_hook_event() -> None:
    """room:member_joined should be accepted as a built-in hook event."""

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        del ctx

    registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    assert registry.has_hooks(EVENT_ROOM_MEMBER_JOINED)


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
