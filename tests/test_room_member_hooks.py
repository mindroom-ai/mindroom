"""Tests for Matrix room-member hook emission."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import MagicMock

import nio
import pytest

import mindroom.hooks as hook_api
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import mindroom_user_id
from mindroom.event_journal import EventClass, EventKind, membership_hooks
from mindroom.hooks import (
    EVENT_ROOM_MEMBER_JOINED,
    EVENT_ROOM_MEMBER_LEFT,
    HookRegistry,
    RoomMemberJoinedContext,
    RoomMemberLeftContext,
    hook,
)
from mindroom.matrix.users import AgentMatrixUser
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    make_matrix_client_mock,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts
from tests.journal_helpers import admit_dispatch_event

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import PrincipalStore


def _plugin(name: str, callbacks: list[object]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        discovered_hooks=tuple(callbacks),
        discovered_automations=(),
        entry_config=PluginEntryConfig(path=f"./plugins/{name}"),
        plugin_order=0,
    )


def _room(room_id: str = "!lobby:localhost") -> MagicMock:
    room = MagicMock()
    room.room_id = room_id
    room.canonical_alias = "#lobby:localhost"
    return room


# The entity that stewards every room of the running case: the owner account, or an agent in rooms without the owner.
_STEWARD = ROUTER_AGENT_NAME


@pytest.fixture(autouse=True, params=[ROUTER_AGENT_NAME, "helper"], ids=["owner_is_steward", "agent_is_steward"])
def _steward(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every case with the owner account as steward, and with an agent as steward and no owner in the room."""
    monkeypatch.setattr(sys.modules[__name__], "_STEWARD", request.param)


def _user(name: str) -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=name,
        user_id=f"@mindroom_{name}:localhost",
        display_name=name.title(),
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
    prev_display_name: str | None = None,
    prev_avatar_url: str | None = None,
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
        prev_content: dict[str, object] = {"membership": prev_membership}
        if prev_display_name is not None:
            prev_content["displayname"] = prev_display_name
        if prev_avatar_url is not None:
            prev_content["avatar_url"] = prev_avatar_url
        raw_event["unsigned"] = {"prev_content": prev_content}
    event = nio.RoomMemberEvent.from_dict(raw_event)
    assert isinstance(event, nio.RoomMemberEvent)
    return event


def _steward_bot(
    tmp_path: Path,
    *,
    bot_accounts: list[str] | None = None,
    mindroom_user: dict[str, str] | None = None,
) -> AgentBot:
    """Build the bot that stewards every room of the running case."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper")},
            bot_accounts=bot_accounts or [],
            mindroom_user=mindroom_user,
        ),
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={ROUTER_AGENT_NAME: "mindroom_router", "helper": "mindroom_helper"},
    )
    bot = make_test_agent_bot(_user(_STEWARD), config=config, runtime_paths=runtime_paths)
    bot.orchestrator.steward = _STEWARD
    return _connect(bot)


def _connect(bot: AgentBot) -> AgentBot:
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.homeserver = "http://localhost:8008"
    bot._first_sync_done = True
    return bot


def _other_bot(steward: AgentBot) -> AgentBot:
    """Build the case's other entity, which stewards no room, in the steward's runtime."""
    name = "helper" if _STEWARD == ROUTER_AGENT_NAME else ROUTER_AGENT_NAME
    return _connect(make_test_agent_bot(_user(name), orchestrator=steward.orchestrator))


async def _dispatch_member(bot: AgentBot, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
    """Provide the durable source that production callbacks always receive."""
    await admit_dispatch_event(bot._journal_dispatcher, room, event, EventKind.ROOM_LIFECYCLE, EventClass.ACTIONABLE)
    await bot._on_room_member(room, event)


def test_room_member_joined_is_a_builtin_hook_event() -> None:
    """room:member_joined should be accepted as a built-in hook event."""

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        del ctx

    registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    assert registry.has_hooks(EVENT_ROOM_MEMBER_JOINED)


def test_room_member_left_is_a_builtin_hook_event() -> None:
    """room:member_left should be accepted as a built-in hook event."""
    event_name = hook_api.EVENT_ROOM_MEMBER_LEFT
    context_type = hook_api.RoomMemberLeftContext

    @hook(event_name)
    async def left(ctx: object) -> None:
        assert isinstance(ctx, context_type)

    registry = HookRegistry.from_plugins([_plugin("personal-room-reinvite", [left])])

    assert registry.has_hooks(event_name)


@pytest.mark.asyncio
async def test_router_emits_room_member_joined_once_per_room_user(tmp_path: Path) -> None:
    """The router should emit one onboarding hook per room/user pair."""
    seen: list[RoomMemberJoinedContext] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx)

    bot = _steward_bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()

    await _dispatch_member(bot, room, _room_member_event(event_id="$join1"))
    await _dispatch_member(bot, room, _room_member_event(event_id="$join2"))

    assert len(seen) == 1
    context = seen[0]
    assert context.agent_name == _STEWARD
    assert context.room_id == "!lobby:localhost"
    assert context.event_id == "$join1"
    assert context.user_id == "@alice:localhost"
    assert context.sender_id == "@alice:localhost"
    assert context.membership == "join"
    assert context.prev_membership == "leave"
    assert context.display_name == "Alice"
    assert context.avatar_url == "mxc://localhost/alice"
    # The homeserver admin API stays owner-backed, so a room without the owner account has none.
    assert (context.matrix_admin is not None) == (_STEWARD == ROUTER_AGENT_NAME)


@pytest.mark.asyncio
async def test_router_emits_room_member_left_for_human_self_leave(tmp_path: Path) -> None:
    """The router should expose an exact human join-to-leave self-transition."""
    seen: list[RoomMemberLeftContext] = []

    @hook(EVENT_ROOM_MEMBER_LEFT)
    async def left(ctx: RoomMemberLeftContext) -> None:
        seen.append(ctx)

    bot = _steward_bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
        [_plugin("personal-room-reinvite", [left])],
    )

    await _dispatch_member(
        bot,
        _room("!personal:localhost"),
        _room_member_event(
            event_id="$leave1",
            membership="leave",
            prev_membership="join",
            display_name=None,
            avatar_url=None,
            prev_display_name="Alice",
            prev_avatar_url="mxc://localhost/alice",
        ),
    )

    assert len(seen) == 1
    context = seen[0]
    assert context.agent_name == _STEWARD
    assert context.room_id == "!personal:localhost"
    assert context.event_id == "$leave1"
    assert context.user_id == "@alice:localhost"
    assert context.sender_id == "@alice:localhost"
    assert context.membership == "leave"
    assert context.prev_membership == "join"
    assert context.display_name == "Alice"
    assert context.avatar_url == "mxc://localhost/alice"
    # The homeserver admin API stays owner-backed, so a room without the owner account has none.
    assert (context.matrix_admin is not None) == (_STEWARD == ROUTER_AGENT_NAME)


@pytest.mark.asyncio
async def test_router_dispatches_recovered_room_member_self_leave_once(tmp_path: Path) -> None:
    """An unsettled recovered leave remains owed work and settles exactly once."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_LEFT)
    async def left(ctx: RoomMemberLeftContext) -> None:
        seen.append(ctx.event_id)

    bot = _steward_bot(tmp_path)
    room = _room("!personal:localhost")
    bot.client.rooms = {room.room_id: room}
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
        [_plugin("personal-room-reinvite", [left])],
    )
    event = _room_member_event(
        event_id="$recovered-leave",
        membership="leave",
        prev_membership="join",
    )
    await admit_dispatch_event(
        bot._journal_dispatcher,
        room,
        event,
        EventKind.ROOM_LIFECYCLE,
        EventClass.ACTIONABLE,
    )

    await bot._journal_dispatcher.drain_once()
    await bot._journal_dispatcher.drain_once()

    assert seen == ["$recovered-leave"]
    assert not await bot._journal_dispatcher.store.is_pending("$recovered-leave")


@pytest.mark.asyncio
async def test_router_ignores_non_human_or_non_self_room_member_leaves(tmp_path: Path) -> None:
    """Joins, kicks, bans, uncertain history, and configured bots are not self-leaves."""
    seen: list[RoomMemberLeftContext] = []

    @hook(EVENT_ROOM_MEMBER_LEFT)
    async def left(ctx: RoomMemberLeftContext) -> None:
        seen.append(ctx)

    bot = _steward_bot(tmp_path, bot_accounts=["@service:localhost"])
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins(
        [_plugin("personal-room-reinvite", [left])],
    )
    room = _room("!personal:localhost")

    events = (
        _room_member_event(event_id="$join", membership="join", prev_membership="leave"),
        _room_member_event(
            event_id="$kick",
            membership="leave",
            prev_membership="join",
            sender="@admin:localhost",
        ),
        _room_member_event(
            event_id="$ban",
            membership="ban",
            prev_membership="join",
            sender="@admin:localhost",
        ),
        _room_member_event(event_id="$unknown", membership="leave", prev_membership=None),
        _room_member_event(
            event_id="$service-leave",
            user_id="@service:localhost",
            membership="leave",
            prev_membership="join",
        ),
    )
    for event in events:
        await _dispatch_member(bot, room, event)

    assert seen == []


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

    bot = _steward_bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()
    event = _room_member_event(event_id="$retry")

    first = asyncio.create_task(_dispatch_member(bot, room, event))
    await entered.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await _dispatch_member(bot, room, event)

    assert attempts == 2


@pytest.mark.asyncio
async def test_room_member_joined_supports_router_agent_scope(tmp_path: Path) -> None:
    """room:member_joined hooks should support router agent scoping."""
    if _STEWARD != ROUTER_AGENT_NAME:
        pytest.skip("the router alias names the owner account until hooks gain room scope")
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED, agents=[ROUTER_AGENT_NAME])
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.user_id)

    bot = _steward_bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    await _dispatch_member(bot, _room(), _room_member_event())

    assert seen == ["@alice:localhost"]


@pytest.mark.asyncio
async def test_router_emits_live_room_member_join_without_previous_membership(tmp_path: Path) -> None:
    """Live member joins can omit unsigned previous membership."""
    seen: list[RoomMemberJoinedContext] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx)

    bot = _steward_bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    await _dispatch_member(bot, _room(), _room_member_event(event_id="$sso-autojoin", prev_membership=None))

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

    bot = _steward_bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()

    def failing_write(*_args: object) -> None:
        message = "marker write interrupted"
        raise OSError(message)

    event = _room_member_event(event_id="$join")
    with monkeypatch.context() as failure:
        failure.setattr(membership_hooks, "mark_completed", failing_write)
        with pytest.raises(OSError, match="marker write interrupted"):
            await _dispatch_member(bot, room, event)

    assert seen == ["$join"]
    assert await bot.journal_principal().is_pending(event.event_id)
    await _dispatch_member(bot, room, event)
    assert seen == ["$join", "$join"]


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

    bot = _steward_bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()
    save_started = asyncio.Event()
    release_save = asyncio.Event()
    principal_type = type(bot.journal_principal())
    original_save = principal_type.mark_room_member_join_completed

    async def delayed_save(store: PrincipalStore, room_id: str, user_id: str) -> None:
        save_started.set()
        await release_save.wait()
        await original_save(store, room_id, user_id)

    monkeypatch.setattr(principal_type, "mark_room_member_join_completed", delayed_save)

    first_task: asyncio.Task[None] | None = None
    second_task: asyncio.Task[None] | None = None
    try:
        first_task = asyncio.create_task(
            _dispatch_member(bot, room, _room_member_event(event_id="$join1")),
        )
        await asyncio.wait_for(save_started.wait(), timeout=2.0)
        second_task = asyncio.create_task(
            _dispatch_member(bot, room, _room_member_event(event_id="$join2")),
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

    bot = _steward_bot(
        tmp_path,
        bot_accounts=["@bridge:localhost"],
        mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
    )
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    internal_user_id = mindroom_user_id(bot.config, bot.runtime_paths)
    assert internal_user_id is not None

    await _dispatch_member(bot, _room(), _room_member_event(event_id="$bridge", user_id="@bridge:localhost"))
    await _dispatch_member(bot, _room(), _room_member_event(event_id="$agent", user_id=bot.agent_user.user_id))
    await _dispatch_member(bot, _room(), _room_member_event(event_id="$internal", user_id=internal_user_id))

    assert seen == []


@pytest.mark.asyncio
async def test_non_steward_bots_do_not_emit_room_member_joined(tmp_path: Path) -> None:
    """Only the room's steward should emit room-member join hooks."""
    seen: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        seen.append(ctx.user_id)

    bot = _other_bot(_steward_bot(tmp_path))
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])

    await _dispatch_member(bot, cast("nio.MatrixRoom", _room()), _room_member_event())

    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_name", "membership", "prev_membership"),
    [(EVENT_ROOM_MEMBER_JOINED, "join", "leave"), (EVENT_ROOM_MEMBER_LEFT, "leave", "join")],
)
async def test_member_joined_runs_once_when_steward_changes(
    tmp_path: Path,
    event_name: str,
    membership: str,
    prev_membership: str,
) -> None:
    """A bot that becomes the room's steward does not run a hook again for an event the earlier steward took."""
    seen: list[str] = []

    @hook(event_name)
    async def changed(ctx: RoomMemberJoinedContext | RoomMemberLeftContext) -> None:
        seen.append(ctx.agent_name)

    first = _steward_bot(tmp_path)
    second = _other_bot(first)
    first.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [changed])])
    room = cast("nio.MatrixRoom", _room())
    event = _room_member_event(event_id="$change", membership=membership, prev_membership=prev_membership)

    await _dispatch_member(first, room, event)
    first.orchestrator.steward = second.agent_name
    await _dispatch_member(second, room, event)

    assert seen == [_STEWARD]


@pytest.mark.asyncio
async def test_same_steward_retry_still_runs(tmp_path: Path) -> None:
    """The steward that took an event's claim runs the hook again when the event is delivered to it again."""
    attempts = 0

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(_ctx: RoomMemberJoinedContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise asyncio.CancelledError

    bot = _steward_bot(tmp_path)
    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = _room()
    event = _room_member_event(event_id="$retry")

    with pytest.raises(asyncio.CancelledError):
        await _dispatch_member(bot, room, event)
    await _dispatch_member(bot, room, event)

    assert attempts == 2


_CHANGES = pytest.mark.parametrize(
    ("event_name", "membership", "prev_membership"),
    [(EVENT_ROOM_MEMBER_JOINED, "join", "leave"), (EVENT_ROOM_MEMBER_LEFT, "leave", "join")],
)


def _recording(bot: AgentBot, event_name: str) -> list[str]:
    """Register a hook for one membership event and return the names of the entities that ran it."""
    seen: list[str] = []

    @hook(event_name)
    async def changed(ctx: RoomMemberJoinedContext | RoomMemberLeftContext) -> None:
        seen.append(ctx.agent_name)

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [changed])])
    return seen


@pytest.mark.asyncio
@_CHANGES
async def test_hook_runs_on_the_steward_elected_when_the_event_was_first_dispatched(
    tmp_path: Path,
    event_name: str,
    membership: str,
    prev_membership: str,
) -> None:
    """Another bot that dispatches the event first claims it for the steward, which runs it even after a handover."""
    steward = _steward_bot(tmp_path)
    other = _other_bot(steward)
    seen = _recording(steward, event_name)
    room = cast("nio.MatrixRoom", _room())
    event = _room_member_event(event_id="$change", membership=membership, prev_membership=prev_membership)

    await _dispatch_member(other, room, event)
    assert seen == []
    steward.orchestrator.steward = other.agent_name
    await _dispatch_member(steward, room, event)
    await _dispatch_member(other, room, event)

    assert seen == [_STEWARD]


@pytest.mark.asyncio
async def test_claimed_hook_cancelled_before_it_ran_is_retried_by_its_owner_after_a_handover(tmp_path: Path) -> None:
    """The bot that holds an event's claim runs the hook on its retry, though another bot is the steward by then."""
    attempts: list[str] = []

    @hook(EVENT_ROOM_MEMBER_JOINED)
    async def joined(ctx: RoomMemberJoinedContext) -> None:
        attempts.append(ctx.agent_name)
        if len(attempts) == 1:
            raise asyncio.CancelledError

    steward = _steward_bot(tmp_path)
    other = _other_bot(steward)
    steward.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_plugin("onboarding", [joined])])
    room = cast("nio.MatrixRoom", _room())
    event = _room_member_event(event_id="$retry")

    with pytest.raises(asyncio.CancelledError):
        await _dispatch_member(steward, room, event)
    steward.orchestrator.steward = other.agent_name
    await _dispatch_member(other, room, event)
    await _dispatch_member(steward, room, event)

    assert attempts == [_STEWARD, _STEWARD]


@pytest.mark.asyncio
async def test_rejoin_after_a_handover_does_not_run_the_join_hook_again(tmp_path: Path) -> None:
    """A bot that saw a user's first join as a bystander does not announce the user's rejoin once it is the steward."""
    steward = _steward_bot(tmp_path)
    other = _other_bot(steward)
    seen = _recording(steward, EVENT_ROOM_MEMBER_JOINED)
    room = cast("nio.MatrixRoom", _room())
    first_join = _room_member_event(event_id="$join1")

    await _dispatch_member(other, room, first_join)
    await _dispatch_member(steward, room, first_join)
    steward.orchestrator.steward = other.agent_name
    await _dispatch_member(other, room, _room_member_event(event_id="$join2"))

    assert seen == [_STEWARD]
