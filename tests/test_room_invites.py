"""Tests for agent self-managed room membership.

With the new self-managing agent pattern, agents handle their own room
memberships. This test module verifies that behavior.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import replace
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import nio
import pytest

from mindroom import bot_room_lifecycle
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.bot_room_lifecycle import _MAX_PENDING_INVITE_ATTEMPTS, _MAX_PENDING_INVITE_ROOMS_PER_PASS
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.event_journal import DepartureSource, RoomMembershipPosition
from mindroom.hooks.matrix_admin import build_hook_matrix_admin
from mindroom.matrix import client_room_admin, invited_rooms_store
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.invited_rooms_store import (
    invited_rooms_path,
    is_inviter_allowed,
    load_invited_rooms,
    load_pending_room_invites,
    pending_room_invites_path,
    save_invited_rooms,
    should_accept_invites,
)
from mindroom.matrix.room_cleanup import cleanup_all_orphaned_bots
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.access_schema_support import with_responder_access
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_journal_support,
    install_send_response_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids
from tests.journal_membership_helpers import admit_room_membership

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from nio.responses import Response


_DURABLE_MEMBERSHIP_GATEWAY = AgentBot.change_local_membership


@pytest.fixture(autouse=True)
def _membership_transport_for_invite_business_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep invite policy fixtures on their mocked Matrix transport."""

    async def change_membership(
        bot: AgentBot,
        room_id: str,
        target_membership: str,
        *,
        is_authorized: Callable[[], bool] | None = None,
    ) -> bool:
        client = bot.client
        assert client is not None
        if target_membership == "join":
            position = await bot.journal_principal().ingestion_membership_position(room_id)
            if is_authorized is not None and not is_authorized():
                return False
            client_rooms = client.rooms
            if (
                isinstance(client_rooms, dict)
                and room_id in client_rooms
                and (position is None or position.membership == "join")
            ):
                await admit_room_membership(bot.journal_principal(), room_id, "join")
                return True
            joined = await client_room_admin.join_room(client, room_id)
            if joined is RoomJoinOutcome.JOINED:
                client.rooms[room_id] = nio.MatrixRoom(room_id, client.user_id)
                if isinstance(client.invited_rooms, dict):
                    client.invited_rooms.pop(room_id, None)
                await admit_room_membership(bot.journal_principal(), room_id, "join")
                return True
            return False
        assert target_membership == "leave"
        left = await client_room_admin.leave_room(client, room_id)
        if left:
            await admit_room_membership(bot.journal_principal(), room_id, "leave", source=DepartureSource.LOCAL)
        return left

    monkeypatch.setattr(AgentBot, "change_local_membership", change_membership)


def _invited_rooms_path(config: Config, agent_name: str) -> Path:
    return invited_rooms_path(runtime_paths_for(config).storage_root, agent_name)


def _pending_room_invites(config: Config, agent_name: str) -> dict[str, str]:
    path = pending_room_invites_path(runtime_paths_for(config).storage_root, agent_name)
    return load_pending_room_invites(path)


def _cache_current_invite(bot: AgentBot, room_id: str, sender: str) -> nio.MatrixInvitedRoom:
    """Mirror nio's current-invite cache before delivering its callback."""
    client = bot.client
    assert client is not None
    invited_rooms = client.invited_rooms
    if not isinstance(invited_rooms, dict):
        invited_rooms = {}
    current_room = invited_rooms.get(room_id)
    if not isinstance(current_room, nio.MatrixInvitedRoom):
        current_room = nio.MatrixInvitedRoom(room_id, bot.agent_user.user_id)
        invited_rooms[room_id] = current_room
    current_room.inviter = sender
    client.invited_rooms = invited_rooms
    return current_room


async def _handle_invite(bot: AgentBot, room: nio.MatrixRoom, event: nio.InviteEvent) -> None:
    current_room = _cache_current_invite(bot, room.room_id, event.sender)
    await bot._room_lifecycle.handle_invite(current_room, event.sender)


def _router_user() -> AgentMatrixUser:
    return AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )


@pytest.mark.parametrize(
    ("policy", "sender_id", "expected"),
    [
        (True, "@anyone:anywhere.example", True),
        (False, "@owner:example.com", False),
        ([], "@owner:example.com", False),
        (["@owner:example.com"], "@owner:example.com", True),
        (["@*:trusted.example.com"], "@member:trusted.example.com", True),
        (["@owner:example.com"], "@outsider:example.com", False),
    ],
)
def test_invitation_policy_is_independent_for_every_responder(
    tmp_path: Path,
    policy: bool | list[str],
    sender_id: str,
    expected: bool,
) -> None:
    """The dedicated invite policy must decide joins without responder access."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=policy),
            agents={
                "research": AgentConfig(
                    display_name="Research",
                    accept_invites=policy,
                ),
            },
            teams={
                "reviewers": TeamConfig(
                    display_name="Reviewers",
                    role="Review work",
                    agents=["research"],
                    accept_invites=policy,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    assert is_inviter_allowed(config, runtime_paths, ROUTER_AGENT_NAME, sender_id) is expected
    assert is_inviter_allowed(config, runtime_paths, "research", sender_id) is expected
    assert is_inviter_allowed(config, runtime_paths, "reviewers", sender_id) is expected
    assert should_accept_invites(config, ROUTER_AGENT_NAME) is bool(policy)
    assert should_accept_invites(config, "research") is bool(policy)
    assert should_accept_invites(config, "reviewers") is bool(policy)


def test_invitation_policy_resolves_aliases_before_matching(tmp_path: Path) -> None:
    """An inviter alias must match the same canonical pattern as conversation access."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=["@owner:example.com"]),
            agents={
                "research": AgentConfig(
                    display_name="Research",
                    accept_invites=["@owner:example.com"],
                ),
            },
            teams={
                "reviewers": TeamConfig(
                    display_name="Reviewers",
                    role="Review work",
                    agents=["research"],
                    accept_invites=["@owner:example.com"],
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    config.authorization.aliases = {"@owner:example.com": ["@bridge-owner:example.com"]}
    runtime_paths = runtime_paths_for(config)
    entity_ids(config, runtime_paths)
    assert is_inviter_allowed(config, runtime_paths, ROUTER_AGENT_NAME, "@bridge-owner:example.com") is True
    assert is_inviter_allowed(config, runtime_paths, "research", "@bridge-owner:example.com") is True
    assert is_inviter_allowed(config, runtime_paths, "reviewers", "@bridge-owner:example.com") is True


def test_invitation_policy_does_not_resolve_configured_bot_alias(tmp_path: Path) -> None:
    """A configured bot alias must not inherit a human invitation grant."""
    human_id = "@owner:example.com"
    bot_id = "@bridgebot:example.com"
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=[human_id]),
            bot_accounts=[bot_id],
        ),
        test_runtime_paths(tmp_path),
    )
    config.authorization.aliases = {human_id: [bot_id]}

    assert is_inviter_allowed(config, runtime_paths_for(config), ROUTER_AGENT_NAME, bot_id) is False


def _live_router_invite_scenario(
    tmp_path: Path,
    *,
    room_id: str = "!invited:localhost",
    sender_id: str = "@owner:localhost",
) -> tuple[Config, AgentBot, nio.MatrixInvitedRoom, nio.InviteMemberEvent]:
    """Build one fresh router invitation scenario."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(current_room_members=True),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.rooms = {}
    room = nio.MatrixInvitedRoom(room_id, bot.agent_user.user_id)
    room.inviter = sender_id
    bot.client.invited_rooms = {room_id: room}
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": sender_id,
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)
    return config, bot, room, event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "sender_id", "accepted"),
    [
        (["@owner:localhost"], "@owner:localhost", True),
        (["@*:localhost"], "@owner:localhost", True),
        (["@other:localhost"], "@owner:localhost", False),
        ([], "@owner:localhost", False),
    ],
)
async def test_router_invitation_list_controls_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: list[str],
    sender_id: str,
    accepted: bool,
) -> None:
    """The room lifecycle must enforce the router's dedicated inviter patterns."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path, sender_id=sender_id)
    config.router.accept_invites = policy
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await _handle_invite(bot, room, event)

    if accepted:
        join_room.assert_awaited_once_with(bot.client, room.room_id)
        assert bot._room_lifecycle.invited_rooms == {room.room_id}
    else:
        join_room.assert_not_awaited()
        assert bot._room_lifecycle.invited_rooms == set()


@pytest.mark.asyncio
async def test_router_invitation_list_uses_current_inviter_at_join_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A replacement invite must own authorization before the join starts."""
    allowed_sender = "@owner:localhost"
    replacement_sender = "@outsider:localhost"
    config, bot, room, event = _live_router_invite_scenario(tmp_path, sender_id=allowed_sender)
    config.router.accept_invites = [allowed_sender]
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    update_join_fences = bot._sync_continuity_store.update_join_fences
    fence_started = threading.Event()
    release_fence = threading.Event()

    def block_fence_persistence(
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
        retain: tuple[str, ...] | None = None,
    ) -> object:
        if add:
            fence_started.set()
            assert release_fence.wait(timeout=2)
        return update_join_fences(add=add, remove=remove, retain=retain)

    monkeypatch.setattr(bot._sync_continuity_store, "update_join_fences", block_fence_persistence)
    task = asyncio.create_task(_handle_invite(bot, room, event))
    try:
        assert await asyncio.to_thread(fence_started.wait, 2)
        room.inviter = replacement_sender
    finally:
        release_fence.set()

    await task

    join_room.assert_not_awaited()
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room.room_id)


@pytest.mark.asyncio
async def test_authoritative_departure_revokes_current_invite_before_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A leave observed while invite fencing is pending must prevent the join from starting."""
    _config, bot, room, event = _live_router_invite_scenario(tmp_path)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    update_join_fences = bot._sync_continuity_store.update_join_fences
    invite_fence_started = threading.Event()
    release_invite_fence = threading.Event()

    def block_invite_fence_persistence(
        *,
        add: tuple[str, ...] = (),
        remove: tuple[str, ...] = (),
        retain: tuple[str, ...] | None = None,
    ) -> object:
        if add:
            invite_fence_started.set()
            assert release_invite_fence.wait(timeout=2)
        return update_join_fences(add=add, remove=remove, retain=retain)

    monkeypatch.setattr(bot._sync_continuity_store, "update_join_fences", block_invite_fence_persistence)
    invite_task = asyncio.create_task(_handle_invite(bot, room, event))
    try:
        assert await asyncio.to_thread(invite_fence_started.wait, 2)
        # Owned nio publishes authoritative room membership before notifying consumers.
        bot.client.invited_rooms.pop(room.room_id)

        release_invite_fence.set()
        await invite_task

        join_room.assert_not_awaited()
        client = bot.client
        assert client is not None
        assert room.room_id not in client.invited_rooms
    finally:
        release_invite_fence.set()


@pytest.mark.asyncio
async def test_denied_invites_leave_no_pending_ledger_entries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An inviter the current policy refuses cannot grow the durable pending-invite ledger."""
    config, bot, _room, _event = _live_router_invite_scenario(tmp_path, sender_id="@outsider:localhost")
    config.router.accept_invites = ["@owner:localhost"]
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    for index in range(20):
        room = nio.MatrixInvitedRoom(f"!flood-{index}:localhost", bot.agent_user.user_id)
        await _handle_invite(bot, room, MagicMock(sender="@outsider:localhost"))

    join_room.assert_not_awaited()
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    assert bot._room_lifecycle._pending_room_invites == {}


@pytest.mark.asyncio
async def test_one_failing_pending_invite_does_not_stop_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A room whose join keeps failing stays pending while later rooms are still reconciled."""
    config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    bot.client.invited_rooms = {}
    failing, joinable = "!a-failing:localhost", "!b-joinable:localhost"
    for room_id in (failing, joinable):
        _cache_current_invite(bot, room_id, "@owner:localhost")

    async def join_room(_client: object, room_id: str) -> RoomJoinOutcome:
        return RoomJoinOutcome.RETRYABLE_FAILURE if room_id == failing else RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", AsyncMock(side_effect=join_room))
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await bot._room_lifecycle.reconcile_pending_invites()

    assert bot._room_lifecycle.invited_rooms == {joinable}
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    assert set(bot._room_lifecycle._pending_invite_retries) == {failing}


@pytest.mark.asyncio
async def test_accepted_invite_ledger_writes_run_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Recording an accepted join and forgetting it after leaving never rewrite the ledger on the event loop."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path)
    loop_thread = threading.current_thread()
    save = invited_rooms_store.save_pending_room_invites
    saves_on_loop: list[bool] = []

    def observed_save(path: Path, pending_invites: dict[str, str]) -> bool:
        saves_on_loop.append(threading.current_thread() is loop_thread)
        return save(path, pending_invites)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_pending_room_invites", observed_save)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", AsyncMock(return_value=RoomJoinOutcome.JOINED))
    monkeypatch.setattr(
        bot._room_lifecycle,
        "_send_invite_welcome",
        AsyncMock(side_effect=RuntimeError("welcome unavailable")),
    )

    with pytest.raises(RuntimeError, match="welcome unavailable"):
        await _handle_invite(bot, room, event)
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {room.room_id: event.sender}
    await bot._room_lifecycle.forget_invited_room(room.room_id)

    assert saves_on_loop == [False, False]
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}


@pytest.mark.asyncio
async def test_refused_cached_invites_cost_one_ledger_write_per_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invites from refused inviters leave the ledger in one write and are never handled one by one."""
    config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    config.router.accept_invites = ["@owner:localhost"]
    bot.client.invited_rooms = {}
    refused_room_ids = [f"!refused-{index}:localhost" for index in range(200)]
    for room_id in refused_room_ids:
        _cache_current_invite(bot, room_id, "@outsider:localhost")
    # Entries an earlier release recorded before checking the inviter.
    invited_rooms_store.save_pending_room_invites(
        invited_rooms_store.pending_room_invites_path(bot.runtime_paths.storage_root, ROUTER_AGENT_NAME),
        dict.fromkeys(refused_room_ids, "@outsider:localhost"),
    )
    save = invited_rooms_store.save_pending_room_invites
    saves: list[int] = []

    def counted_save(path: Path, pending_invites: dict[str, str]) -> bool:
        saves.append(len(pending_invites))
        return save(path, pending_invites)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_pending_room_invites", counted_save)
    handle_invite = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "_handle_invite", handle_invite)

    await bot._room_lifecycle.reconcile_pending_invites()
    await bot._room_lifecycle.reconcile_pending_invites()

    assert saves == [0]
    handle_invite.assert_not_awaited()
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}


@pytest.mark.asyncio
async def test_pending_invite_whose_join_keeps_failing_backs_off_then_gives_up_until_delivered_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reconciliation retries a failing join after doubling delays and stops after five failed passes."""
    config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    bot.client.invited_rooms = {}
    room_id = "!unjoinable:localhost"
    room = _cache_current_invite(bot, room_id, "@owner:localhost")
    now = [1000.0]
    monkeypatch.setattr("mindroom.bot_room_lifecycle.monotonic", lambda: now[0])
    join_room = AsyncMock(return_value=RoomJoinOutcome.RETRYABLE_FAILURE)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    for delay in (30, 60, 120, 240):
        await bot._room_lifecycle.reconcile_pending_invites()
        attempts = join_room.await_count
        now[0] += delay - 1
        await bot._room_lifecycle.reconcile_pending_invites()
        assert join_room.await_count == attempts
        now[0] += 1
    await bot._room_lifecycle.reconcile_pending_invites()
    assert join_room.await_count == 5

    now[0] += 7200
    await bot._room_lifecycle.reconcile_pending_invites()
    assert join_room.await_count == 5
    # A failed join leaves no ledger entry, and giving up changes nothing durable.
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}

    # A freshly delivered invite starts over: its own attempt, then timed retries.
    with pytest.raises(RuntimeError, match="Failed to join invited room"):
        await bot._room_lifecycle.handle_invite(room, "@owner:localhost")
    assert join_room.await_count == 6
    await bot._room_lifecycle.reconcile_pending_invites()
    assert join_room.await_count == 6
    now[0] += 30
    await bot._room_lifecycle.reconcile_pending_invites()
    assert join_room.await_count == 7


@pytest.mark.asyncio
async def test_overlapping_reconciliations_run_one_pass_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Concurrent triggers share passes, so one failing join is attempted and counted once per pass."""
    _config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    bot.client.invited_rooms = {}
    room_id = "!unjoinable:localhost"
    _cache_current_invite(bot, room_id, "@owner:localhost")
    monkeypatch.setattr("mindroom.bot_room_lifecycle.monotonic", lambda: 1000.0)
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def failing_join(_client: object, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.RETRYABLE_FAILURE

    join_room = AsyncMock(side_effect=failing_join)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    handle_invite = bot._room_lifecycle._handle_invite
    entered: list[str] = []
    all_passes_entered = asyncio.Event()

    async def counted_handle_invite(room: nio.MatrixRoom, sender: str) -> None:
        entered.append(room.room_id)
        if len(entered) == 5:
            all_passes_entered.set()
        await handle_invite(room, sender)

    monkeypatch.setattr(bot._room_lifecycle, "_handle_invite", counted_handle_invite)

    first = asyncio.create_task(bot._room_lifecycle.reconcile_pending_invites())
    await asyncio.wait_for(join_started.wait(), timeout=2)
    later = [asyncio.create_task(bot._room_lifecycle.reconcile_pending_invites()) for _ in range(4)]
    # Overlapping passes would each queue on the room's join lock; shared passes never get there.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(all_passes_entered.wait(), timeout=0.5)
    release_join.set()
    await asyncio.wait_for(asyncio.gather(first, *later), timeout=2)

    assert join_room.await_count == 1
    assert entered == [room_id]
    retry = bot._room_lifecycle._pending_invite_retries[room_id]
    assert (retry.failures, retry.abandoned) == (1, False)


@pytest.mark.asyncio
async def test_revoked_invites_leave_no_accepted_entries_or_retry_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An invite revoked after its join failed leaves nothing behind once nio drops it."""
    config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    bot.client.invited_rooms = {}
    room_ids = [f"!revoked-{index}:localhost" for index in range(20)]
    for room_id in room_ids:
        _cache_current_invite(bot, room_id, "@owner:localhost")
    monkeypatch.setattr(
        "mindroom.matrix.client_room_admin.join_room",
        AsyncMock(return_value=RoomJoinOutcome.RETRYABLE_FAILURE),
    )
    await bot._room_lifecycle.reconcile_pending_invites()
    # Failed joins stay out of the ledger; only their retry state and inviters are kept, in memory.
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    assert set(bot._room_lifecycle._pending_invite_retries) == set(room_ids)
    assert set(bot._room_lifecycle._unconfirmed_join_inviters) == set(room_ids)

    bot.client.invited_rooms.clear()
    await bot._room_lifecycle.reconcile_pending_invites()

    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    assert bot._room_lifecycle._pending_invite_retries == {}
    assert bot._room_lifecycle._unconfirmed_join_inviters == {}
    assert bot._room_lifecycle._invite_join_locks == {}


@pytest.mark.asyncio
async def test_joined_room_whose_welcome_keeps_failing_is_retried_after_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Giving up on an unfinished accepted join lasts only for this process."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.monotonic", lambda: 1000.0)

    async def join_room(_client: object, room_id: str) -> RoomJoinOutcome:
        bot.client.invited_rooms.pop(room_id, None)
        bot.client.rooms[room_id] = nio.MatrixRoom(room_id, bot.agent_user.user_id)
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", AsyncMock(side_effect=join_room))
    failing_welcome = AsyncMock(side_effect=RuntimeError("welcome unavailable"))
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", failing_welcome)
    with pytest.raises(RuntimeError, match="welcome unavailable"):
        await _handle_invite(bot, room, event)
    for _ in range(_MAX_PENDING_INVITE_ATTEMPTS):
        await bot._room_lifecycle.reconcile_pending_invites()
        retry = bot._room_lifecycle._pending_invite_retries[room.room_id]
        bot._room_lifecycle._pending_invite_retries[room.room_id] = replace(retry, retry_at=0.0)
    assert bot._room_lifecycle._pending_invite_retries[room.room_id].abandoned
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {room.room_id: event.sender}

    restarted = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    restarted.client = bot.client
    welcome = AsyncMock()
    monkeypatch.setattr(restarted._room_lifecycle, "_send_invite_welcome", welcome)
    await restarted._room_lifecycle.reconcile_pending_invites()

    welcome.assert_awaited_once_with(room.room_id, event.sender)
    assert restarted._room_lifecycle.invited_rooms == {room.room_id}
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}


@pytest.mark.asyncio
async def test_a_burst_of_ledger_changes_uses_one_worker_thread_and_few_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Changes that queue behind an in-flight ledger write are applied together, on one worker thread at a time."""
    config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    update = bot_room_lifecycle._update_pending_room_invites
    save = invited_rooms_store.save_pending_room_invites
    guard = threading.Lock()
    threads_in_use = 0
    most_threads_in_use = 0
    saves = 0

    def observed_update(*args: object) -> dict[str, str]:
        nonlocal threads_in_use, most_threads_in_use
        with guard:
            threads_in_use += 1
            most_threads_in_use = max(most_threads_in_use, threads_in_use)
        try:
            return update(*args)
        finally:
            with guard:
                threads_in_use -= 1

    def slow_save(path: Path, pending_invites: dict[str, str]) -> bool:
        nonlocal saves
        saves += 1
        threading.Event().wait(0.02)
        return save(path, pending_invites)

    monkeypatch.setattr("mindroom.bot_room_lifecycle._update_pending_room_invites", observed_update)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_pending_room_invites", slow_save)
    room_ids = [f"!burst-{index}:localhost" for index in range(200)]

    await asyncio.gather(
        *(bot._room_lifecycle._record_accepted_invite(room_id, "@owner:localhost") for room_id in room_ids),
    )

    assert most_threads_in_use == 1
    assert saves <= 3
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == dict.fromkeys(room_ids, "@owner:localhost")
    assert bot._room_lifecycle._pending_room_invites == dict.fromkeys(room_ids, "@owner:localhost")


@pytest.mark.asyncio
async def test_cancelling_the_ledger_writer_leaves_queued_changes_for_the_next_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A writer cancelled mid-write cannot strand a change that was batched into its write."""
    config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    save = invited_rooms_store.save_pending_room_invites
    started = [threading.Event(), threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event(), threading.Event()]
    calls = 0

    def blocking_save(path: Path, pending_invites: dict[str, str]) -> bool:
        nonlocal calls
        call = min(calls, 2)
        calls += 1
        started[call].set()
        assert release[call].wait(2)
        return save(path, pending_invites)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_pending_room_invites", blocking_save)
    lifecycle = bot._room_lifecycle
    first = asyncio.create_task(lifecycle._record_accepted_invite("!first:localhost", "@owner:localhost"))
    assert await asyncio.to_thread(started[0].wait, 2)
    second = asyncio.create_task(lifecycle._record_accepted_invite("!second:localhost", "@owner:localhost"))
    third = asyncio.create_task(lifecycle._record_accepted_invite("!third:localhost", "@owner:localhost"))
    await asyncio.sleep(0)
    release[0].set()
    await asyncio.wait_for(first, timeout=2)
    # The second writer took both queued changes into one write; cancel it mid-write.
    assert await asyncio.to_thread(started[1].wait, 2)
    second.cancel()
    release[1].set()
    with pytest.raises(asyncio.CancelledError):
        await second
    release[2].set()
    await asyncio.wait_for(third, timeout=2)

    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {
        "!first:localhost": "@owner:localhost",
        "!second:localhost": "@owner:localhost",
        "!third:localhost": "@owner:localhost",
    }


@pytest.mark.asyncio
async def test_a_failed_live_join_is_retried_by_its_own_timer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A transient join failure is retried without waiting for an unrelated reconciliation trigger."""
    _config, bot, room, event = _live_router_invite_scenario(tmp_path)
    monkeypatch.setattr("mindroom.bot_room_lifecycle._PENDING_INVITE_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    joined = asyncio.Event()
    outcomes = [RoomJoinOutcome.RETRYABLE_FAILURE, RoomJoinOutcome.JOINED]

    async def join_room(_client: object, room_id: str) -> RoomJoinOutcome:
        outcome = outcomes.pop(0)
        if outcome is RoomJoinOutcome.JOINED:
            bot.client.invited_rooms.pop(room_id, None)
            bot.client.rooms[room_id] = nio.MatrixRoom(room_id, bot.agent_user.user_id)
            joined.set()
        return outcome

    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", AsyncMock(side_effect=join_room))

    with pytest.raises(RuntimeError, match="Failed to join invited room"):
        await _handle_invite(bot, room, event)
    await asyncio.wait_for(joined.wait(), timeout=2)
    assert await wait_for_background_tasks(timeout=2, owner=bot._runtime_view)

    assert bot._room_lifecycle.invited_rooms == {room.room_id}
    assert bot._room_lifecycle._pending_invite_retries == {}
    assert bot._room_lifecycle._pending_invite_retry_timer is None


@pytest.mark.asyncio
async def test_one_pass_handles_a_bounded_number_of_rooms_and_schedules_the_rest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A large backlog is worked through in bounded passes, so one pass cannot hold membership work for long."""
    _config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    bot.client.invited_rooms = {}
    for index in range(_MAX_PENDING_INVITE_ROOMS_PER_PASS + 8):
        _cache_current_invite(bot, f"!backlog-{index:03d}:localhost", "@owner:localhost")
    join_room = AsyncMock(return_value=RoomJoinOutcome.RETRYABLE_FAILURE)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    await bot._room_lifecycle.reconcile_pending_invites()
    assert join_room.await_count == _MAX_PENDING_INVITE_ROOMS_PER_PASS
    assert bot._room_lifecycle._pending_invite_retry_timer is not None
    await bot._room_lifecycle.reconcile_pending_invites()
    assert join_room.await_count == _MAX_PENDING_INVITE_ROOMS_PER_PASS + 8
    bot._room_lifecycle.cancel_pending_invite_retry()


@pytest.mark.asyncio
async def test_a_join_that_reported_failure_but_landed_is_still_finished(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The inviter of a failed join stays in memory, so a join that went through anyway is remembered and welcomed."""
    config, bot, room, event = _live_router_invite_scenario(tmp_path)
    monkeypatch.setattr(
        "mindroom.matrix.client_room_admin.join_room",
        AsyncMock(return_value=RoomJoinOutcome.RETRYABLE_FAILURE),
    )
    welcome = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", welcome)
    with pytest.raises(RuntimeError, match="Failed to join invited room"):
        await _handle_invite(bot, room, event)
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}

    # Nio later reports that the join went through after all.
    bot.client.invited_rooms.pop(room.room_id)
    bot.client.rooms[room.room_id] = nio.MatrixRoom(room.room_id, bot.agent_user.user_id)
    bot._room_lifecycle._pending_invite_retries.clear()
    await bot._room_lifecycle.reconcile_pending_invites()

    welcome.assert_awaited_once_with(room.room_id, event.sender)
    assert bot._room_lifecycle.invited_rooms == {room.room_id}
    assert bot._room_lifecycle._unconfirmed_join_inviters == {}


@pytest.mark.asyncio
async def test_invite_join_lock_is_kept_while_a_waiter_has_not_resumed(tmp_path: Path) -> None:
    """A room's lock survives the gap between its release and the next waiter resuming, then is dropped."""
    _config, bot, room, _event = _live_router_invite_scenario(tmp_path)
    lifecycle = bot._room_lifecycle
    entered = asyncio.Event()
    release = asyncio.Event()

    async def second() -> None:
        async with lifecycle._invite_join_lock(room.room_id):
            entered.set()
            await release.wait()

    async with lifecycle._invite_join_lock(room.room_id):
        waiter = asyncio.create_task(second())
        await asyncio.sleep(0)
    held = lifecycle._invite_join_locks[room.room_id]
    assert held.users == 1
    assert not entered.is_set()
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert lifecycle._invite_join_locks[room.room_id] is held
    release.set()
    await waiter
    assert lifecycle._invite_join_locks == {}


@pytest.mark.asyncio
async def test_a_pass_skips_rooms_nio_retracts_or_joins_while_it_runs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invites retracted or turned into joins during a pass are skipped, and the pass still arms its retry."""
    _config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    bot.client.invited_rooms = {}
    for room_id in ("!a:localhost", "!b:localhost", "!c:localhost"):
        _cache_current_invite(bot, room_id, "@owner:localhost")
    monkeypatch.setattr("mindroom.bot_room_lifecycle.monotonic", lambda: 1000.0)
    attempted: list[str] = []

    async def join_room(_client: object, room_id: str) -> RoomJoinOutcome:
        attempted.append(room_id)
        # While the first join is in flight, sync retracts one invite and
        # reports another room as joined through some other path.
        bot.client.invited_rooms.pop("!b:localhost", None)
        bot.client.invited_rooms.pop("!c:localhost", None)
        bot.client.rooms["!c:localhost"] = nio.MatrixRoom("!c:localhost", bot.agent_user.user_id)
        return RoomJoinOutcome.RETRYABLE_FAILURE

    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", AsyncMock(side_effect=join_room))

    await bot._room_lifecycle.reconcile_pending_invites()

    assert attempted == ["!a:localhost"]
    assert set(bot._room_lifecycle._pending_invite_retries) == {"!a:localhost"}
    assert bot._room_lifecycle._pending_invite_retry_timer is not None
    bot._room_lifecycle.cancel_pending_invite_retry()


@pytest.mark.asyncio
async def test_cancelled_retries_stay_off_until_the_sync_loop_resumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pass that fails after shutdown cancelled retries cannot re-arm the timer until retries resume."""
    _config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    bot.client.invited_rooms = {}
    _cache_current_invite(bot, "!slow:localhost", "@owner:localhost")
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def blocked_join(_client: object, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.RETRYABLE_FAILURE

    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", AsyncMock(side_effect=blocked_join))
    lifecycle = bot._room_lifecycle

    reconciliation = asyncio.create_task(lifecycle.reconcile_pending_invites())
    await asyncio.wait_for(join_started.wait(), timeout=2)
    lifecycle.cancel_pending_invite_retry()
    release_join.set()
    await asyncio.wait_for(reconciliation, timeout=2)

    assert "!slow:localhost" in lifecycle._pending_invite_retries
    assert lifecycle._pending_invite_retry_timer is None
    lifecycle._pending_invite_retry_fired()
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
    lifecycle.resume_pending_invite_retries()
    assert lifecycle._pending_invite_retry_timer is not None
    lifecycle.cancel_pending_invite_retry()


@pytest.mark.asyncio
async def test_an_unexpected_ledger_failure_fails_every_batched_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A write that fails with any error answers every change batched into it."""
    _config, bot, _room, _event = _live_router_invite_scenario(tmp_path)
    update = bot_room_lifecycle._update_pending_room_invites
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def failing_update(*args: object) -> dict[str, str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(2)
            return update(*args)
        msg = "ledger unreadable"
        raise ValueError(msg)

    monkeypatch.setattr("mindroom.bot_room_lifecycle._update_pending_room_invites", failing_update)
    lifecycle = bot._room_lifecycle
    first = asyncio.create_task(lifecycle._record_accepted_invite("!first:localhost", "@owner:localhost"))
    assert await asyncio.to_thread(started.wait, 2)
    batched = [
        asyncio.create_task(lifecycle._record_accepted_invite(f"!batched-{index}:localhost", "@owner:localhost"))
        for index in range(3)
    ]
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(first, timeout=2)
    results = await asyncio.wait_for(asyncio.gather(*batched, return_exceptions=True), timeout=2)

    assert calls == 2
    assert [type(result) for result in results] == [ValueError] * 3


@pytest.mark.asyncio
async def test_a_join_that_landed_while_reported_failed_keeps_its_inviter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A join nio already projects as done when the command reports failure is finished by the next pass."""
    _config, bot, room, event = _live_router_invite_scenario(tmp_path)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.monotonic", lambda: 1000.0)

    async def landed_but_failed(_client: object, room_id: str) -> RoomJoinOutcome:
        bot.client.invited_rooms.pop(room_id, None)
        bot.client.rooms[room_id] = nio.MatrixRoom(room_id, bot.agent_user.user_id)
        return RoomJoinOutcome.RETRYABLE_FAILURE

    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", AsyncMock(side_effect=landed_but_failed))
    welcome = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", welcome)

    await _handle_invite(bot, room, event)
    assert bot._room_lifecycle._unconfirmed_join_inviters == {room.room_id: event.sender}
    assert bot._room_lifecycle._pending_invite_retry_timer is not None
    await bot._room_lifecycle.reconcile_pending_invites()

    welcome.assert_awaited_once_with(room.room_id, event.sender)
    assert bot._room_lifecycle.invited_rooms == {room.room_id}
    bot._room_lifecycle.cancel_pending_invite_retry()


@pytest.fixture
def mock_config(tmp_path: Path) -> Config:
    """Create a mock config with agents and teams."""
    return bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    rooms=["room1", "room2"],
                ),
                "agent2": AgentConfig(
                    display_name="Agent 2",
                    role="Another test agent",
                    rooms=["room1"],
                ),
            },
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Test team",
                    agents=["agent1", "agent2"],
                    rooms=["room2"],
                ),
            },
        ),
        tmp_path,
    )


@pytest.mark.asyncio
async def test_agent_joins_configured_rooms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that agents join their configured rooms on startup."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Create the agent bot with configured rooms
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost", "!room2:localhost"],
    )
    install_runtime_journal_support(bot)

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Track which rooms were joined
    joined_rooms = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> RoomJoinOutcome:
        joined_rooms.append(room_id)
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", mock_join_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _conversation_reader: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))

    # Test that the bot joins its configured rooms
    await bot.join_configured_rooms()

    # Verify the bot joined both configured rooms
    assert len(joined_rooms) == 2
    assert "!room1:localhost" in joined_rooms
    assert "!room2:localhost" in joined_rooms


@pytest.mark.asyncio
async def test_agent_skips_rejoining_rooms_it_already_has(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Agents should skip redundant joins for rooms they are already in."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost", "!room2:localhost"],
    )
    install_runtime_journal_support(bot)

    mock_client = AsyncMock()
    mock_client.rooms = {"!room1:localhost": MagicMock()}
    bot.client = mock_client

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=["!room1:localhost"]))
    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", AsyncMock(return_value=0))

    await bot.join_configured_rooms()

    join_room.assert_awaited_once_with(mock_client, "!room2:localhost")


@pytest.mark.asyncio
async def test_startup_already_joined_reconciles_gateway_without_membership_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A journal-current startup room performs setup without fabricating a join."""
    room_id = "!room1:localhost"
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default")),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=[room_id],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {room_id: MagicMock()}
    principal = MagicMock()
    principal.ingestion_membership_position = AsyncMock(
        return_value=RoomMembershipPosition("join", 4),
    )
    session = MagicMock()
    session.wait_for_membership_idle = AsyncMock()
    session.next_batch = AsyncMock(return_value=None)
    session.change_membership = AsyncMock(return_value=True)
    bot.journal_principal = MagicMock(return_value=principal)
    bot._ingestion_session = session
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        change_membership=_DURABLE_MEMBERSHIP_GATEWAY.__get__(bot, AgentBot),
    )
    bot._post_join_room_setup = AsyncMock()
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=[room_id]),
    )

    await bot.join_configured_rooms()

    session.wait_for_membership_idle.assert_awaited_once_with()
    principal.ingestion_membership_position.assert_awaited_once_with(room_id)
    session.change_membership.assert_not_awaited()
    bot.client.join.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_leave_uses_durable_gateway_without_direct_http(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Managed cleanup delegates its one leave fate to the owned nio session."""
    room_id = "!old:localhost"
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default")),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=[],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    principal = MagicMock()
    principal.ingestion_membership_position = AsyncMock(
        return_value=RoomMembershipPosition("join", 4),
    )
    session = MagicMock()
    session.wait_for_membership_idle = AsyncMock()
    session.next_batch = AsyncMock(return_value=None)
    session.change_membership = AsyncMock(return_value=True)
    bot.journal_principal = MagicMock(return_value=principal)
    bot._ingestion_session = session
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        change_membership=_DURABLE_MEMBERSHIP_GATEWAY.__get__(bot, AgentBot),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=[room_id]),
    )
    monkeypatch.setattr(
        "mindroom.matrix.rooms.is_dm_room",
        AsyncMock(return_value=False),
    )

    await bot.leave_unconfigured_rooms()

    session.change_membership.assert_awaited_once_with(
        operation_id=UUID("46fba72c-1729-5252-a01c-ca8a8746cc6e"),
        room_id=room_id,
        previous_membership="join",
        previous_epoch=4,
        current_membership="leave",
    )
    bot.client.room_leave.assert_not_awaited()


@pytest.mark.asyncio
async def test_join_configured_rooms_retries_when_membership_inventory_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unreadable joined-room inventory must not block idempotent join recovery."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost", "!room2:localhost"],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=None))
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    await bot.join_configured_rooms()

    assert {call.args[1] for call in join_room.await_args_list} == {
        "!room1:localhost",
        "!room2:localhost",
    }
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!room1:localhost")
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!room2:localhost")


@pytest.mark.asyncio
async def test_stale_client_room_after_leave_cannot_reopen_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only authoritative server membership can reopen a locally departed room."""
    room_id = "!room1:localhost"
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=[room_id],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {room_id: MagicMock()}
    failure = RuntimeError("durable join failed before publication")
    join_room = AsyncMock(side_effect=failure)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    await admit_room_membership(bot.journal_principal(), room_id, "leave")
    with pytest.raises(RuntimeError) as raised:
        await bot.join_configured_rooms()

    assert raised.value is failure
    join_room.assert_awaited_once_with(bot.client, room_id)
    assert await bot.journal_principal().membership_position(room_id) == RoomMembershipPosition("leave", 1)
    assert bot._room_lifecycle.decrypt_notice_is_fenced(room_id)


@pytest.mark.asyncio
async def test_agent_rejoins_persisted_invited_rooms_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Persisted ad-hoc invited rooms should be reconciled during startup joins."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=True,
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    invited_path = _invited_rooms_path(config, "agent1")
    invited_path.parent.mkdir(parents=True, exist_ok=True)
    invited_path.write_text('[\n  "!invited-room:localhost"\n]\n', encoding="utf-8")

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)

    mock_client = AsyncMock()
    bot.client = mock_client

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", AsyncMock(return_value=0))

    await bot.join_configured_rooms()

    join_room.assert_awaited_once_with(mock_client, "!invited-room:localhost")


@pytest.mark.asyncio
async def test_router_accepts_agent_invite_persists_and_rejoins_on_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Router should auto-accept an internal agent invite as durable desired membership."""
    config = bind_runtime_paths(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1", role="Test agent")},
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}

    fenced_during_join: list[bool] = []

    async def join_room_while_sync_is_live(_client: object, room_id: str) -> RoomJoinOutcome:
        fenced_during_join.append(bot._room_lifecycle.decrypt_notice_is_fenced(room_id))
        return RoomJoinOutcome.JOINED

    join_room = AsyncMock(side_effect=join_room_while_sync_is_live)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    welcome_message = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", welcome_message)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@mindroom_agent1:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, "!router-invited:localhost")
    assert fenced_during_join == [True]
    welcome_message.assert_awaited_once_with("!router-invited:localhost", "@mindroom_agent1:localhost")
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!router-invited:localhost")
    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).read_text(encoding="utf-8") == (
        '[\n  "!router-invited:localhost"\n]\n'
    )

    restarted_bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(restarted_bot)
    restarted_bot.client = AsyncMock()
    restarted_bot.client.rooms = {}
    join_room.reset_mock()
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr(restarted_bot, "_post_join_room_setup", AsyncMock())

    await restarted_bot.join_configured_rooms()

    join_room.assert_awaited_once_with(restarted_bot.client, "!router-invited:localhost")


@pytest.mark.asyncio
async def test_live_invite_forbidden_join_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live invite's ambiguous forbidden join must remain retryable."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.rooms = {}
    bot.client.join = AsyncMock(return_value=nio.JoinError("forbidden", "M_FORBIDDEN"))
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )

    with pytest.raises(RuntimeError, match="Failed to join invited room"):
        await _handle_invite(
            bot,
            MagicMock(room_id="!failed:localhost", canonical_alias=None),
            MagicMock(sender="@owner:localhost"),
        )
    # Nio still holds the invite; the ledger keeps only joins in flight or joined.
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    assert bot._room_lifecycle._unconfirmed_join_inviters == {"!failed:localhost": "@owner:localhost"}
    assert bot._room_lifecycle._pending_invite_retries["!failed:localhost"].failures == 1
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!failed:localhost")


@pytest.mark.asyncio
async def test_unconfirmed_invite_join_failure_retains_retry_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The durable boolean result cannot establish a terminal join rejection."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.join = AsyncMock(return_value=nio.JoinError("bad state", "M_BAD_STATE"))
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)

    room = _cache_current_invite(bot, "!invalid-state:localhost", event.sender)
    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    bot.client.join.assert_awaited_once_with("!invalid-state:localhost")
    assert await bot._journal_dispatcher.store.pending() == ()
    assert bot._room_lifecycle.decrypt_notice_is_fenced("!invalid-state:localhost")
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    assert "!invalid-state:localhost" in bot._room_lifecycle._pending_invite_retries


@pytest.mark.asyncio
async def test_recovered_invite_waits_for_current_matrix_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An accepted entry whose room is neither invited nor joined is dropped without authorizing a join."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    bot.client.rooms = {}
    bot.client.invited_rooms = {}
    bot.client.join = AsyncMock(return_value=nio.JoinError("not invited", "M_FORBIDDEN"))
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    room_id = "!revoked-invite:localhost"
    invited_rooms_store.save_pending_room_invites(
        invited_rooms_store.pending_room_invites_path(bot.runtime_paths.storage_root, ROUTER_AGENT_NAME),
        {room_id: "@owner:localhost"},
    )

    await bot._room_lifecycle.reconcile_pending_invites()
    await bot._room_lifecycle.reconcile_pending_invites()

    bot.client.join.assert_not_awaited()
    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    assert not bot._room_lifecycle.decrypt_notice_is_fenced(room_id)


@pytest.mark.asyncio
async def test_initial_sync_invite_is_current_membership_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An invite is current membership work during initial sync."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    welcome_message = AsyncMock()
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", welcome_message)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.agent_user.user_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)
    room = _cache_current_invite(bot, "!invited:localhost", event.sender)

    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    join_room.assert_awaited_once_with(bot.client, room.room_id)
    welcome_message.assert_awaited_once_with(room.room_id, event.sender)
    assert bot._room_lifecycle.invited_rooms == {room.room_id}
    assert await bot._journal_dispatcher.store.pending() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "state_key", "membership"),
    [
        ("m.room.name", "", None),
        ("m.room.member", "@other:localhost", "invite"),
        ("m.room.member", "self", "leave"),
    ],
    ids=["room-metadata", "other-member", "non-invite-membership"],
)
async def test_only_authenticated_self_invites_start_invite_work(
    tmp_path: Path,
    event_type: str,
    state_key: str,
    membership: str | None,
) -> None:
    """Unrelated invite-state callbacks must not create join authority or work."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot._room_lifecycle.handle_invite = AsyncMock()
    room = nio.MatrixInvitedRoom("!metadata:localhost", bot.matrix_id.full_id)
    event = nio.InviteEvent.parse_event(
        {
            "type": event_type,
            "sender": "@event-sender:localhost",
            "state_key": bot.matrix_id.full_id if state_key == "self" else state_key,
            "content": {"membership": membership} if membership is not None else {"name": "Project"},
        },
    )
    assert isinstance(event, nio.InviteEvent)

    await bot._on_invite_before_sync_certification(room, event)
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
    bot._room_lifecycle.handle_invite.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_sync_callback_runs_durable_join_in_background(tmp_path: Path) -> None:
    """Durable invite admission must not hold the sync loop across network work."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id=bot.matrix_id.full_id)
    bot.client.rooms = {}
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_invite(_room: nio.MatrixRoom, _sender: str) -> None:
        join_started.set()
        await release_join.wait()

    bot._room_lifecycle.handle_invite = delayed_invite
    room = nio.MatrixRoom("!background-invite:localhost", bot.matrix_id.full_id)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.matrix_id.full_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteEvent)

    callback_task = asyncio.create_task(
        bot._on_invite_before_sync_certification(room, event),
    )
    try:
        await asyncio.wait_for(join_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert callback_task.done()
        # Nio's durable store keeps the invite for recovery, so no journal row or ledger entry is needed.
        assert _pending_room_invites(config, ROUTER_AGENT_NAME) == {}
        assert await bot._journal_dispatcher.store.pending() == ()
    finally:
        release_join.set()
        await callback_task

    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
    assert await bot._journal_dispatcher.store.pending() == ()


@pytest.mark.asyncio
async def test_join_is_not_requested_when_the_accepted_inviter_cannot_be_saved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A join whose inviter could not be kept durably would lose its completion work, so it waits for a retry."""
    _config, bot, room, event = _live_router_invite_scenario(tmp_path)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_pending_room_invites", lambda *_args: False)

    with pytest.raises(OSError, match="Failed to persist accepted room invite"):
        await _handle_invite(bot, room, event)

    join_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_invite_persistence_failure_propagates_to_sync_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Failed invited-room saves must leave invite work retryable."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "mindroom.matrix.client_room_admin.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", lambda *_args: False)
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", AsyncMock())

    with pytest.raises(OSError, match="Failed to persist invited room"):
        await _handle_invite(
            bot,
            MagicMock(room_id="!failed-save:localhost", canonical_alias=None),
            MagicMock(sender="@owner:localhost"),
        )


@pytest.mark.asyncio
async def test_router_invite_preserves_room_created_after_lifecycle_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A later invite must not overwrite a hook-created room missing from the lifecycle cache."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}

    creator_client = AsyncMock(spec=nio.AsyncClient)
    creator_client.homeserver = "http://localhost:8008"
    creator_client.user_id = bot.agent_user.user_id
    with monkeypatch.context() as patch_context:
        create_room = AsyncMock(return_value="!hook-created:localhost")
        patch_context.setattr("mindroom.hooks.matrix_admin.create_room", create_room)
        admin = build_hook_matrix_admin(
            creator_client,
            runtime_paths_for(config),
            config=config,
        )
        await admin.create_room(name="Hook-created room")

    assert bot._room_lifecycle.invited_rooms == set()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", AsyncMock())
    room = MagicMock(room_id="!later-invite:localhost", canonical_alias=None)
    event = MagicMock(sender="@owner:localhost")

    await _handle_invite(bot, room, event)

    expected_rooms = {"!hook-created:localhost", "!later-invite:localhost"}
    assert bot._room_lifecycle.invited_rooms == expected_rooms
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).read_text(encoding="utf-8") == (
        '[\n  "!hook-created:localhost",\n  "!later-invite:localhost"\n]\n'
    )


@pytest.mark.asyncio
async def test_router_cleanup_preserves_room_created_after_lifecycle_loaded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup must refresh rooms persisted by hooks after lifecycle construction."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()

    creator_client = AsyncMock(spec=nio.AsyncClient)
    creator_client.homeserver = "http://localhost:8008"
    creator_client.user_id = bot.agent_user.user_id
    with monkeypatch.context() as patch_context:
        patch_context.setattr(
            "mindroom.hooks.matrix_admin.create_room",
            AsyncMock(return_value="!hook-created:localhost"),
        )
        admin = build_hook_matrix_admin(
            creator_client,
            runtime_paths_for(config),
            config=config,
        )
        await admin.create_room(name="Hook-created room")

    assert bot._room_lifecycle.invited_rooms == set()

    left_room_ids: list[str] = []

    async def record_rooms_to_leave(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        leave_room_action: Callable[[str], Awaitable[bool]],
    ) -> list[str]:
        del leave_room_action
        left_room_ids.extend(room_ids)
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=["!hook-created:localhost", "!stale:localhost"]),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", record_rooms_to_leave)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.invited_rooms == {"!hook-created:localhost"}
    assert left_room_ids == ["!stale:localhost"]


@pytest.mark.asyncio
async def test_router_cleanup_loads_invited_rooms_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Durable refresh must not perform file I/O on the event-loop thread."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    event_loop_thread_id = threading.get_ident()
    load_thread_ids: list[int] = []

    def record_load_thread(path: Path) -> set[str]:
        load_thread_ids.append(threading.get_ident())
        return load_invited_rooms(path)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.load_invited_rooms", record_load_thread)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    assert await bot._room_lifecycle._rooms_to_leave() == []
    assert len(load_thread_ids) == 1
    assert load_thread_ids[0] != event_loop_thread_id


@pytest.mark.asyncio
async def test_router_invite_keeps_memory_after_transient_persistence_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed save must not let the next invite erase the first room from memory."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}

    attempts = 0

    def fail_first_save(path: Path, room_ids: set[str]) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return False
        return save_invited_rooms(path, room_ids)

    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", fail_first_save)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "mindroom.matrix.client_room_admin.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(bot._room_lifecycle, "send_welcome_message_if_empty", AsyncMock())

    first_room = MagicMock(room_id="!first:localhost", canonical_alias=None)
    event = MagicMock(sender="@owner:localhost")
    with pytest.raises(OSError, match="Failed to persist invited room"):
        await _handle_invite(bot, first_room, event)

    await _handle_invite(bot, first_room, event)
    await _handle_invite(
        bot,
        MagicMock(room_id="!second:localhost", canonical_alias=None),
        event,
    )

    expected_rooms = {"!first:localhost", "!second:localhost"}
    assert bot._room_lifecycle.invited_rooms == expected_rooms
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).read_text(encoding="utf-8") == (
        '[\n  "!first:localhost",\n  "!second:localhost"\n]\n'
    )


@pytest.mark.asyncio
async def test_router_deduplicates_concurrent_invite_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Duplicate invite callbacks for one room should join and welcome only once."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}

    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_join_room(_client: AsyncMock, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.JOINED

    join_room = AsyncMock(side_effect=delayed_join_room)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!router-invited:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@owner:localhost")

    first_invite = asyncio.create_task(_handle_invite(bot, room, event))
    await join_started.wait()
    second_invite = asyncio.create_task(_handle_invite(bot, room, event))
    release_join.set()

    await asyncio.gather(first_invite, second_invite)

    join_room.assert_awaited_once_with(bot.client, "!router-invited:localhost")
    bot.client.room_messages.assert_awaited_once()
    send_response.assert_awaited_once()
    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}


@pytest.mark.asyncio
async def test_router_departure_allows_fresh_reinvite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A new invitation after Nio removes a departed room can join again."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    room_id = "!router-reinvited:localhost"
    room = MagicMock(room_id=room_id, canonical_alias=None)
    event = MagicMock(sender="@owner:localhost")
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id=room_id,
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    await _handle_invite(bot, room, event)
    bot.client.rooms.pop(room_id)
    await bot._room_lifecycle.forget_invited_room(room_id)
    await admit_room_membership(bot.journal_principal(), room_id, "leave")
    await _handle_invite(bot, room, event)

    assert join_room.await_count == 2
    assert bot.client.room_messages.await_count == 2
    assert send_response.await_count == 2


@pytest.mark.asyncio
async def test_agent_forgets_persisted_invited_room_after_being_kicked(
    tmp_path: Path,
) -> None:
    """An ephemeral call room cannot be rejoined after its creator removes the agent."""
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=True,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room_id = "!agent-call:localhost"
    bot._room_lifecycle._update_invited_room(room_id, remember=True)
    await bot._room_lifecycle.forget_invited_room(room_id)

    assert bot._room_lifecycle.invited_rooms == set()
    assert _invited_rooms_path(config, "agent1").read_text(encoding="utf-8") == "[]\n"


@pytest.mark.asyncio
async def test_agent_retries_failed_persisted_invited_room_forget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed departure save must reject work until the durable room is removed."""
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=True,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room_id = "!agent-call:localhost"
    assert bot._room_lifecycle._update_invited_room(room_id, remember=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", lambda *_args: False)

    with pytest.raises(OSError, match="Failed to forget invited room"):
        await bot._room_lifecycle.forget_invited_room(room_id)

    restarted = make_test_agent_bot(
        agent_user=bot.agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    assert restarted._room_lifecycle.invited_rooms == {room_id}

    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", save_invited_rooms)
    await bot._room_lifecycle.forget_invited_room(room_id)
    restarted = make_test_agent_bot(
        agent_user=bot.agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    assert restarted._room_lifecycle.invited_rooms == set()


@pytest.mark.asyncio
async def test_cleanup_does_not_resurrect_room_pending_durable_forget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed durable forget must still make the departed room eligible for cleanup."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    invited_path = _invited_rooms_path(config, ROUTER_AGENT_NAME)
    invited_path.parent.mkdir(parents=True, exist_ok=True)
    invited_path.write_text('[\n  "!departed:localhost"\n]\n', encoding="utf-8")
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", lambda *_args: False)

    with pytest.raises(OSError, match="Failed to forget invited room"):
        await bot._room_lifecycle.forget_invited_room("!departed:localhost")

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=["!departed:localhost"]),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    assert await bot._room_lifecycle._rooms_to_leave() == ["!departed:localhost"]
    assert bot._room_lifecycle.invited_rooms == set()


@pytest.mark.asyncio
async def test_nonpersisting_agent_forget_clears_in_memory_room(tmp_path: Path) -> None:
    """Disabling invite persistence must not leave stale in-memory membership."""
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=False,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room_id = "!old-invite:localhost"
    bot._room_lifecycle.invited_rooms = {room_id}

    await bot._room_lifecycle.forget_invited_room(room_id)

    assert bot._room_lifecycle.invited_rooms == set()
    assert not _invited_rooms_path(config, "agent1").exists()


@pytest.mark.asyncio
async def test_router_duplicate_invite_retries_failed_welcome_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Duplicate invite callbacks should retry welcome delivery after a failed first send."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!router-invited:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(side_effect=[None, "$welcome"])
    install_send_response_mock(bot, send_response)

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@owner:localhost")

    with pytest.raises(RuntimeError, match="Failed to complete welcome message"):
        await _handle_invite(bot, room, event)
    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, "!router-invited:localhost")
    assert bot.client.room_messages.await_count == 2
    assert send_response.await_count == 2
    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}


@pytest.mark.asyncio
async def test_redelivered_invite_retries_a_failed_welcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A redelivered invite must retry a welcome whose delivery failed."""
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!router-invited:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(side_effect=[None, "$welcome"])
    install_send_response_mock(bot, send_response)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:localhost",
            "state_key": bot.matrix_id.full_id,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteEvent)
    room = _cache_current_invite(bot, "!router-invited:localhost", event.sender)
    await bot._on_invite_before_sync_certification(room, event)
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    # The welcome failed. An invite the bot has not finished acting on is
    # still in the next sync response, so the retry arrives as a redelivery
    # rather than from an in-process retry loop.
    await bot._on_invite_before_sync_certification(room, event)
    await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    join_room.assert_awaited_with(bot.client, room.room_id)
    assert send_response.await_count == 2
    assert not await bot._journal_dispatcher.store.pending()


@pytest.mark.asyncio
async def test_router_welcome_send_is_idempotent_for_concurrent_empty_room_checks(
    tmp_path: Path,
) -> None:
    """Concurrent empty-room checks should not emit duplicate welcome messages."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!empty:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    await asyncio.gather(
        bot._send_welcome_message_if_empty("!empty:localhost"),
        bot._send_welcome_message_if_empty("!empty:localhost"),
        bot._send_welcome_message_if_empty("!empty:localhost"),
    )

    bot.client.room_messages.assert_awaited_once()
    send_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_router_welcome_send_retries_after_delivery_failure(
    tmp_path: Path,
) -> None:
    """A failed welcome delivery should not suppress a later retry."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!empty:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(side_effect=[None, "$welcome"])
    install_send_response_mock(bot, send_response)

    assert not await bot._room_lifecycle.send_welcome_message_if_empty("!empty:localhost")
    await bot._send_welcome_message_if_empty("!empty:localhost")

    assert bot.client.room_messages.await_count == 2
    assert send_response.await_count == 2


@pytest.mark.asyncio
async def test_router_welcome_lookup_failure_propagates_for_retry(tmp_path: Path) -> None:
    """A failed history lookup must not complete invite delivery."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesError.from_dict(
            {
                "errcode": "M_UNKNOWN",
                "error": "history unavailable",
            },
            "!empty:localhost",
        ),
    )

    assert not await bot._room_lifecycle.send_welcome_message_if_empty("!empty:localhost")


@pytest.mark.asyncio
async def test_router_auto_welcome_lists_ad_hoc_present_responder(tmp_path: Path) -> None:
    """Automatic ad-hoc room welcomes should advertise live responder candidates."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Writes code",
                    access=ResponderAccessConfig(current_room_members=True),
                ),
            },
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room = nio.MatrixRoom(room_id="!adhoc:localhost", own_user_id="@mindroom_router:localhost")
    room.members_synced = False
    bot.client = AsyncMock()
    bot.client.rooms = {"!adhoc:localhost": room}
    bot.client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[nio.RoomMember("@mindroom_code:localhost", "Code", None)],
            room_id="!adhoc:localhost",
        ),
    )
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!adhoc:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    await bot._send_welcome_message_if_empty("!adhoc:localhost", "@alice:localhost")

    response_text = send_response.await_args.kwargs["response_text"]
    assert "\u2022 **Code** (alias `code`): Writes code" in response_text
    bot.client.joined_members.assert_awaited_once_with("!adhoc:localhost")


@pytest.mark.asyncio
async def test_router_startup_welcome_without_requester_omits_responder_list(tmp_path: Path) -> None:
    """Startup welcomes should not use internal bot permissions to advertise responders."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Writes code",
                ),
            },
            router=RouterConfig(model="default", accept_invites=True),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    room = nio.MatrixRoom(room_id="!startup:localhost", own_user_id="@mindroom_router:localhost")
    room.add_member("@mindroom_code:localhost", "Code", None)
    room.members_synced = True
    bot.client = AsyncMock()
    bot.client.rooms = {"!startup:localhost": room}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!startup:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)

    await bot._send_welcome_message_if_empty("!startup:localhost")

    response_text = send_response.await_args.kwargs["response_text"]
    assert "\U0001f9e0 **Available agents and teams in this room:**" not in response_text
    assert "@mindroom_code" not in response_text


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_router_invite_welcome_filters_ad_hoc_responders_for_inviter(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Invite welcomes should advertise responders visible to the inviting user."""
    config = bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    role="Writes code",
                    access=ResponderAccessConfig(users=["@alice:localhost"]),
                ),
                "research": AgentConfig(
                    display_name="Research",
                    role="Finds sources",
                    access=ResponderAccessConfig(users=["@bob:localhost"]),
                ),
            },
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(users=["@alice:localhost"]),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            members=[
                nio.RoomMember("@mindroom_code:localhost", "Code", None),
                nio.RoomMember("@mindroom_research:localhost", "Research", None),
            ],
            room_id="!adhoc:localhost",
        ),
    )
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!adhoc:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)
    monkeypatch.setattr(
        "mindroom.matrix.client_room_admin.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )

    room = MagicMock(room_id="!adhoc:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@alice:localhost")

    await _handle_invite(bot, room, event)

    response_text = send_response.await_args.kwargs["response_text"]
    assert "\u2022 **Code** (alias `code`): Writes code" in response_text
    assert "\u2022 **Research**" not in response_text


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_router_invite_welcome_requires_current_reply_authorization(
    tmp_path: Path,
) -> None:
    """Joining an invite must not let the router welcome a reply-denied inviter."""
    sender_id = "@alice:localhost"
    config = bind_runtime_paths(
        Config(
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(users=[]),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!adhoc:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)
    room = MagicMock(room_id="!adhoc:localhost", canonical_alias=None)

    await bot._send_welcome_message_if_empty(room.room_id, sender_id)

    send_response.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_router_invite_welcome_waits_for_replacement_authorization(
    tmp_path: Path,
) -> None:
    """Welcome delivery must use the policy published after a closed reload gate."""
    sender_id = "@alice:localhost"
    config = bind_runtime_paths(
        with_responder_access(
            Config(router=RouterConfig(model="default", accept_invites=True)),
            ROUTER_AGENT_NAME,
            users=[sender_id],
        ),
        test_runtime_paths(tmp_path),
    )
    denied_config = config.model_copy(deep=True)
    with_responder_access(denied_config, ROUTER_AGENT_NAME, users=[])
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!adhoc:localhost",
            chunk=[],
            start="",
            end=None,
        ),
    )
    send_response = AsyncMock(return_value="$welcome")
    install_send_response_mock(bot, send_response)
    gate = bot.admission_gate
    assert gate.close_if_idle()

    welcome_task = asyncio.create_task(
        bot._room_lifecycle.send_welcome_message_if_empty("!adhoc:localhost", sender_id),
    )
    try:
        await asyncio.sleep(0)
        assert not welcome_task.done()
        bot.config = denied_config
    finally:
        gate.reopen()
        await welcome_task

    send_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_ignores_invite_when_accept_invites_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Routers can opt out of accepting room invites."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=False)),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    room = MagicMock(room_id="!router-invited:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@owner:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_not_awaited()
    assert bot._room_lifecycle.invited_rooms == set()
    assert not _invited_rooms_path(config, ROUTER_AGENT_NAME).exists()


@pytest.mark.asyncio
async def test_router_leave_unconfigured_rooms_preserves_persisted_invited_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Router cleanup should preserve a previously accepted invited room."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    invited_rooms_path = _invited_rooms_path(config, ROUTER_AGENT_NAME)
    invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
    invited_rooms_path.write_text('[\n  "!router-invited:localhost"\n]\n', encoding="utf-8")
    bot = make_test_agent_bot(
        agent_user=_router_user(),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!configured-room:localhost"],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()

    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        leave_room_action: Callable[[str], Awaitable[bool]] | None = None,
    ) -> list[str]:
        left_room_ids.extend(room_ids)
        del leave_room_action
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(
            return_value=[
                "!configured-room:localhost",
                "!router-invited:localhost",
                "!old-room:localhost",
            ],
        ),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(),
    )

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.invited_rooms == {"!router-invited:localhost"}
    assert left_room_ids == ["!old-room:localhost"]


@pytest.mark.asyncio
async def test_orphan_cleanup_preserves_router_persisted_invited_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Orphan cleanup should not kick the router from an accepted invited room."""
    client = AsyncMock()
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default", accept_invites=True)),
        test_runtime_paths(tmp_path),
    )
    invited_rooms_path = _invited_rooms_path(config, ROUTER_AGENT_NAME)
    invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
    invited_rooms_path.write_text('[\n  "!router-invited:localhost"\n]\n', encoding="utf-8")

    monkeypatch.setattr(
        "mindroom.matrix.room_cleanup.get_joined_rooms",
        AsyncMock(return_value=["!router-invited:localhost"]),
    )
    monkeypatch.setattr(
        "mindroom.matrix.room_cleanup.get_room_members",
        AsyncMock(return_value=["@mindroom_router:localhost"]),
    )
    monkeypatch.setattr(
        "mindroom.matrix.room_cleanup.persisted_bot_user_ids",
        lambda _runtime_paths: frozenset({"@mindroom_router:localhost"}),
    )
    monkeypatch.setattr("mindroom.matrix.room_cleanup.is_dm_room", AsyncMock(return_value=False))
    client.room_kick = AsyncMock(return_value=nio.RoomKickResponse())

    result = await cleanup_all_orphaned_bots(client, config, runtime_paths_for(config))

    assert result == {}
    client.room_kick.assert_not_called()


@pytest.mark.asyncio
async def test_agent_leaves_unconfigured_rooms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:  # noqa: ARG001
    """Test that agents leave rooms they're no longer configured for."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Create the agent bot with only room1 configured
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],  # Only configured for room1
    )

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Mock joined_rooms to return both room1 and room2 (agent is in both)
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room2:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Track which rooms were left
    left_rooms = []

    async def mock_room_leave(room_id: str) -> Response:
        left_rooms.append(room_id)
        response = MagicMock()
        response.__class__ = nio.RoomLeaveResponse
        return response

    mock_client.room_leave = mock_room_leave
    install_runtime_journal_support(bot)

    # Test that the bot leaves unconfigured rooms
    await bot.leave_unconfigured_rooms()

    # Verify the bot left room2 (unconfigured) but not room1 (configured)
    assert len(left_rooms) == 1
    assert "!room2:localhost" in left_rooms


@pytest.mark.asyncio
async def test_router_preserves_root_space_when_leaving_unconfigured_rooms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The router should not leave the managed root Space during room cleanup."""
    agent_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id="@mindroom_router:localhost",
        display_name="Router",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )
    install_runtime_journal_support(bot)

    mock_client = AsyncMock()
    bot.client = mock_client

    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        leave_room_action: Callable[[str], Awaitable[bool]] | None = None,
    ) -> list[str]:
        left_room_ids.extend(room_ids)
        del leave_room_action
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=["!room1:localhost", "!space:localhost", "!room2:localhost"]),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", mock_leave_non_dm_rooms)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.matrix_state_for_runtime",
        lambda *_args, **_kwargs: MatrixState(space_room_id="!space:localhost"),
    )

    await bot.leave_unconfigured_rooms()

    assert set(left_room_ids) == {"!room2:localhost"}
    assert "!space:localhost" not in left_room_ids


@pytest.mark.asyncio
async def test_agent_manages_rooms_on_config_update(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that agents update their room memberships when configuration changes."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )

    # Start with agent configured for room1 only
    config = bind_runtime_paths(Config(router=RouterConfig(model="default")), test_runtime_paths(tmp_path))

    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room1:localhost"],
    )
    install_runtime_journal_support(bot)

    # Mock the client
    mock_client = AsyncMock()
    bot.client = mock_client

    # Track room operations
    joined_rooms = []
    left_rooms = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> RoomJoinOutcome:
        joined_rooms.append(room_id)
        return RoomJoinOutcome.JOINED

    async def mock_room_leave(room_id: str) -> Response:
        left_rooms.append(room_id)
        response = MagicMock()
        response.__class__ = nio.RoomLeaveResponse
        return response

    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", mock_join_room)
    mock_client.room_leave = mock_room_leave

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _conversation_reader: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock joined_rooms to return room1 and room3 (agent is in both)
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room3:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Update configuration: now configured for room1 and room2 (not room3)
    bot.rooms = ["!room1:localhost", "!room2:localhost"]

    # Apply room updates
    await bot.join_configured_rooms()
    await bot.leave_unconfigured_rooms()

    # Verify:
    # - Joined room2 (newly configured)
    # - Left room3 (no longer configured)
    # - Stayed in room1 (still configured)
    assert "!room2:localhost" in joined_rooms
    assert "!room3:localhost" in left_rooms
    assert "!room1:localhost" not in left_rooms  # Should stay in room1


@pytest.mark.asyncio
async def test_agent_refuses_invite_when_accept_invites_disabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Opted-out agents should reject room invites before joining."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    accept_invites=False,
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    room = MagicMock(room_id="!invited-room:localhost")
    event = MagicMock(sender="@user:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_not_awaited()
    assert not _invited_rooms_path(config, "agent1").exists()


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize("private", [False, True], ids=["shared", "requester-private"])
async def test_agent_accepts_invite_independently_of_conversation_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    private: bool,
) -> None:
    """Joining must not grant or require permission to converse with an agent."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    private=AgentPrivateConfig(per="user") if private else None,
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    room = MagicMock(room_id="!invited-room:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@intruder:localhost")
    assert not is_sender_allowed_for_responder(
        event.sender,
        "agent1",
        room.room_id,
        config,
        runtime_paths_for(config),
        bot._runtime_view.agent_reply_memberships,
    )

    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, room.room_id)
    assert bot._room_lifecycle.invited_rooms == {room.room_id}
    assert not is_sender_allowed_for_responder(
        event.sender,
        "agent1",
        room.room_id,
        config,
        runtime_paths_for(config),
        bot._runtime_view.agent_reply_memberships,
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.parametrize(
    ("policy", "access_users", "expected_join"),
    [
        (["@inviter:localhost"], [], True),
        (["@someone-else:localhost"], ["@inviter:localhost"], False),
    ],
    ids=["invite-policy-allows-access-denies", "invite-policy-denies-access-allows"],
)
async def test_team_invitation_policy_is_independent_of_conversation_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    policy: list[str],
    access_users: list[str],
    expected_join: bool,
) -> None:
    """Team invitation admission must not reuse its post-join conversation policy."""
    sender = "@inviter:localhost"
    team_name = "reviewers"
    config = bind_runtime_paths(
        Config(
            agents={"research": AgentConfig(display_name="Research")},
            teams={
                team_name: TeamConfig(
                    display_name="Reviewers",
                    role="Review work",
                    agents=["research"],
                    accept_invites=policy,
                    access=ResponderAccessConfig(
                        current_room_members=False,
                        members_of_rooms=[],
                        users=access_users,
                    ),
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    team_user = AgentMatrixUser(
        agent_name=team_name,
        user_id="@mindroom_reviewers:localhost",
        display_name="Reviewers",
        password=TEST_PASSWORD,
    )
    bot = make_test_agent_bot(
        agent_user=team_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = make_matrix_client_mock(user_id=team_user.user_id)
    bot.client.rooms = {}
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    room = MagicMock(room_id="!team-invited:localhost", canonical_alias=None)
    event = MagicMock(sender=sender)

    await _handle_invite(bot, room, event)

    assert join_room.await_count == int(expected_join)
    assert (room.room_id in bot._room_lifecycle.invited_rooms) is expected_join


@pytest.mark.asyncio
async def test_unknown_entity_refuses_invite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Entities removed from config should reject new invites."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default")),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    room = MagicMock(room_id="!invited-room:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@user:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_not_awaited()
    assert not _invited_rooms_path(config, "agent1").exists()


@pytest.mark.asyncio
async def test_agent_persists_non_dm_invited_room(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Opted-in agents should persist non-DM invited rooms after joining."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)

    room = MagicMock(room_id="!project-room:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@user:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, "!project-room:localhost")
    assert bot._room_lifecycle.invited_rooms == {"!project-room:localhost"}
    assert _invited_rooms_path(config, "agent1").read_text(encoding="utf-8") == '[\n  "!project-room:localhost"\n]\n'


@pytest.mark.asyncio
async def test_agent_invite_does_not_auto_add_router_to_ad_hoc_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ad-hoc invites should stay agent-scoped unless the router already manages the room."""
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
    )
    install_runtime_journal_support(bot)
    bot.client = make_matrix_client_mock(user_id="@mindroom_agent1:localhost")
    bot.client.rooms = {}

    router_bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths,
    )
    router_bot.client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    bot.client.rooms = {}
    router_bot.join_configured_rooms = AsyncMock()

    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.config = config
    orchestrator.agent_bots = {"agent1": bot, ROUTER_AGENT_NAME: router_bot}
    bot.orchestrator = orchestrator
    router_bot.orchestrator = orchestrator

    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    invite_router = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.is_sender_allowed_for_agent_reply_in_room",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr("mindroom.matrix.client_room_admin.join_room", join_room)
    monkeypatch.setattr("mindroom.orchestrator.invite_to_room", invite_router)

    room = MagicMock(room_id="!project-room:localhost")
    room.canonical_alias = None
    event = MagicMock(sender="@user:localhost")

    await _handle_invite(bot, room, event)

    join_room.assert_awaited_once_with(bot.client, "!project-room:localhost")
    invite_router.assert_not_awaited()
    router_bot.join_configured_rooms.assert_not_awaited()
    assert router_bot._room_lifecycle.invited_rooms == set()
    assert _invited_rooms_path(config, ROUTER_AGENT_NAME).exists() is False


@pytest.mark.asyncio
async def test_leave_unconfigured_rooms_preserves_persisted_invited_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup should preserve one previously invited non-DM room."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    rooms=["!configured-room:localhost"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    invited_rooms_path = _invited_rooms_path(config, "agent1")
    invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
    invited_rooms_path.write_text('[\n  "!invited-room:localhost"\n]\n', encoding="utf-8")
    bot = make_test_agent_bot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!configured-room:localhost"],
    )
    install_runtime_journal_support(bot)
    bot.client = AsyncMock()

    left_room_ids: list[str] = []

    async def mock_leave_non_dm_rooms(
        _client: AsyncMock,
        room_ids: list[str],
        *,
        leave_room_action: Callable[[str], Awaitable[bool]] | None = None,
    ) -> list[str]:
        left_room_ids.extend(room_ids)
        del leave_room_action
        return room_ids

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(
            return_value=[
                "!configured-room:localhost",
                "!invited-room:localhost",
                "!old-room:localhost",
            ],
        ),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_non_dm_rooms", mock_leave_non_dm_rooms)

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.invited_rooms == {"!invited-room:localhost"}
    assert left_room_ids == ["!old-room:localhost"]


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (b"\x80", "codec can't decode"),
        (b"{", "Expecting property name"),
        (b"{}", "Invalid invited-room retention file"),
        (b'["!kept:localhost", null]', "Invalid invited-room retention file"),
    ],
)
def test_corrupt_retention_stops_lifecycle_initialization(tmp_path: Path, contents: bytes, message: str) -> None:
    """Unreadable ownership records must never become an empty desired-room set."""
    agent_user = AgentMatrixUser(
        agent_name="agent1",
        user_id="@mindroom_agent1:localhost",
        display_name="Agent 1",
        password=TEST_PASSWORD,
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                ),
            },
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    invited_rooms_path = _invited_rooms_path(config, "agent1")
    invited_rooms_path.parent.mkdir(parents=True, exist_ok=True)
    invited_rooms_path.write_bytes(contents)
    with pytest.raises(ValueError, match=message):
        make_test_agent_bot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
    assert invited_rooms_path.read_bytes() == contents
