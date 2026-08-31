"""Boundary tests for live-only Matrix invite ownership."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.access import ResponderAccessConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.invited_rooms_store import invited_rooms_path, load_invited_rooms
from mindroom.matrix.sync_loop import OwnRoomMembership
from mindroom.matrix.users import AgentMatrixUser
from mindroom.membership_models import ReportedDeparture
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths

ROOM_ID = "!invited:localhost"
INVITER_ID = "@owner:localhost"
pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def _router_config(tmp_path: Path, *, accept_invites: bool = True) -> Config:
    return bind_runtime_paths(
        Config(
            router=RouterConfig(
                model="default",
                accept_invites=accept_invites,
                access=ResponderAccessConfig(
                    current_room_members=True,
                    members_of_rooms=[],
                ),
            ),
        ),
        test_runtime_paths(tmp_path),
    )


def _router_bot(config: Config):  # noqa: ANN202
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password=TEST_PASSWORD,
        ),
        storage_path=runtime_paths_for(config).storage_root,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    bot.client.rooms = {}
    invited_room = nio.MatrixInvitedRoom(ROOM_ID, bot.agent_user.user_id)
    invited_room.inviter = INVITER_ID
    bot.client.invited_rooms = {ROOM_ID: invited_room}
    return bot, invited_room


def _accepted_path(config: Config) -> Path:
    return invited_rooms_path(runtime_paths_for(config).storage_root, ROUTER_AGENT_NAME)


@pytest.mark.asyncio
async def test_live_inviter_can_satisfy_current_room_members(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The exact cached inviter may authorize this invite, then must still be joined."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    join_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_invite_reconciliation_uses_only_current_matrix_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Enabling invites reconsiders a live invite without any durable pending record."""
    config = _router_config(tmp_path, accept_invites=False)
    bot, _room = _router_bot(config)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await bot._room_lifecycle.reconcile_invites()
    join_room.assert_not_awaited()

    bot.config.router.accept_invites = True
    await bot._room_lifecycle.reconcile_invites()

    join_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_raised_join_failure_consumes_exact_live_invite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A raised join failure cannot retry without a fresh Matrix invite."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    join_room = AsyncMock(side_effect=OSError("join unavailable"))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    with pytest.raises(OSError, match="join unavailable"):
        await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    assert bot.client.invited_rooms == {}
    assert bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)
    await bot._room_lifecycle.reconcile_invites()
    assert join_room.await_count == 1


@pytest.mark.asyncio
async def test_postjoin_cancellation_consumes_exact_live_invite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation after joining cannot reuse the invite that owned the attempt."""
    config = bind_runtime_paths(
        Config(
            administrators=[INVITER_ID],
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(
                    current_room_members=False,
                    members_of_rooms=[],
                ),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot, room = _router_bot(config)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        on_room_joined=AsyncMock(side_effect=asyncio.CancelledError),
    )

    with pytest.raises(asyncio.CancelledError):
        await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    assert bot.client.invited_rooms == {}
    assert bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)
    await bot._room_lifecycle.reconcile_invites()
    assert join_room.await_count == 1


@pytest.mark.asyncio
async def test_policy_is_rechecked_after_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A policy revocation during join prevents acceptance and triggers one leave."""
    config = bind_runtime_paths(
        Config(
            administrators=[INVITER_ID],
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(
                    current_room_members=False,
                    members_of_rooms=[],
                ),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot, room = _router_bot(config)

    async def join_and_revoke(_client: object, _room_id: str) -> RoomJoinOutcome:
        bot.config.administrators = []
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", AsyncMock(side_effect=join_and_revoke))
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == set()
    assert ROOM_ID not in bot.client.invited_rooms


@pytest.mark.asyncio
async def test_policy_revocation_during_postjoin_setup_prevents_persistence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The final policy check follows the last awaited acceptance setup step."""
    config = bind_runtime_paths(
        Config(
            administrators=[INVITER_ID],
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(
                    current_room_members=False,
                    members_of_rooms=[],
                ),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )

    async def revoke_policy(_room_id: str) -> None:
        bot.config.administrators = []

    bot._room_lifecycle.deps = replace(bot._room_lifecycle.deps, on_room_joined=revoke_policy)
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == set()


@pytest.mark.asyncio
async def test_replaced_live_invite_cannot_be_accepted_or_deleted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An old join attempt cannot consume newer Matrix invite ownership."""
    config = bind_runtime_paths(
        Config(
            administrators=[INVITER_ID],
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(
                    current_room_members=False,
                    members_of_rooms=[],
                ),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot, room = _router_bot(config)
    replacement_inviter = "@replacement:localhost"

    async def join_after_replacement(_client: object, _room_id: str) -> RoomJoinOutcome:
        room.inviter = replacement_inviter
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(side_effect=join_after_replacement),
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == set()
    assert bot.client.invited_rooms == {ROOM_ID: room}
    assert room.inviter == replacement_inviter


@pytest.mark.asyncio
async def test_current_room_member_must_still_be_joined_after_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The pre-join member exception expires unless the inviter remains joined."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={bot.agent_user.user_id}),
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == set()


@pytest.mark.asyncio
async def test_postjoin_member_lookup_failure_compensates_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ordinary post-join failure gets one leave without durable retry work."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(side_effect=OSError("members unavailable")),
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == set()
    assert ROOM_ID not in bot.client.invited_rooms


@pytest.mark.asyncio
async def test_ordinary_access_does_not_depend_on_postjoin_member_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Static access remains authoritative without querying joined members."""
    config = bind_runtime_paths(
        Config(
            administrators=[INVITER_ID],
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(
                    current_room_members=False,
                    members_of_rooms=[],
                ),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    get_room_members = AsyncMock(side_effect=OSError("members unavailable"))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_room_members", get_room_members)
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_room", leave_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    get_room_members.assert_not_awaited()
    leave_room.assert_not_awaited()
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_acceptance_persistence_failure_is_terminal_for_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed accepted-room save leaves once and cannot be retried from stale cache."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.save_invited_rooms", lambda *_args: False)
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert ROOM_ID not in bot.client.invited_rooms
    await bot._room_lifecycle.reconcile_invites()
    assert leave_room.await_count == 1


@pytest.mark.asyncio
async def test_welcome_failure_does_not_revoke_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Welcome delivery is best-effort after the acceptance boundary."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(
        bot._room_lifecycle,
        "_send_invite_welcome",
        AsyncMock(side_effect=OSError("send failed")),
    )

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_absent_accepted_room_is_not_rejoined(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Accepted storage preserves membership but never grants a future join."""
    config = _router_config(tmp_path)
    accepted_path = _accepted_path(config)
    accepted_path.parent.mkdir(parents=True, exist_ok=True)
    accepted_path.write_text(f'[\n  "{ROOM_ID}"\n]\n', encoding="utf-8")
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[]))
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    await bot.join_configured_rooms()

    join_room.assert_not_awaited()
    assert load_invited_rooms(accepted_path) == {ROOM_ID}


@pytest.mark.asyncio
async def test_stale_joined_snapshot_cannot_erase_concurrent_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A joined-room snapshot must not prune acceptance recorded after that snapshot."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}

    async def stale_joined_rooms(_client: object) -> list[str]:
        bot._room_lifecycle._remember_invited_room(ROOM_ID)
        return []

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", stale_joined_rooms)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)

    await bot.join_configured_rooms()

    join_room.assert_not_awaited()
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_stale_accepted_read_cannot_resurrect_departed_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An off-thread snapshot cannot mutate ownership after departure wins."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}
    bot._room_lifecycle._remember_invited_room(ROOM_ID)
    read_started = threading.Event()
    release_read = threading.Event()
    original_load = load_invited_rooms

    def delayed_load(path: Path) -> set[str]:
        room_ids = original_load(path)
        read_started.set()
        release_read.wait(timeout=1)
        return room_ids

    monkeypatch.setattr("mindroom.bot_room_lifecycle.load_invited_rooms", delayed_load)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[ROOM_ID]))

    cleanup = asyncio.create_task(bot._room_lifecycle._rooms_to_leave())
    await asyncio.wait_for(asyncio.to_thread(read_started.wait), timeout=1)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.load_invited_rooms", original_load)
    bot._room_lifecycle.forget_invited_room(ROOM_ID)
    release_read.set()
    await cleanup

    assert bot._room_lifecycle.invited_rooms == set()
    assert original_load(_accepted_path(config)) == set()


@pytest.mark.asyncio
async def test_unowned_two_member_room_is_left(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Room ownership never comes from the two-member DM heuristic."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}
    joined_room = nio.MatrixRoom(ROOM_ID, bot.agent_user.user_id)
    joined_room.users = {
        bot.agent_user.user_id: AsyncMock(),
        INVITER_ID: AsyncMock(),
    }
    bot.client.rooms = {ROOM_ID: joined_room}
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[ROOM_ID]))
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

    await bot.leave_unconfigured_rooms()

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)


@pytest.mark.asyncio
async def test_unowned_leave_clears_join_fence_before_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Final departure cannot leave a stale decrypt-notice fence behind."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(ROOM_ID,)),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[ROOM_ID]))
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", AsyncMock(return_value=True))
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        on_room_left=AsyncMock(side_effect=OSError("cleanup failed")),
    )

    with pytest.raises(OSError, match="cleanup failed"):
        await bot.leave_unconfigured_rooms()

    assert not bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)


@pytest.mark.asyncio
async def test_failed_unowned_leave_keeps_join_fence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed Matrix leave cannot make a still-joined room appear unfenced."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(ROOM_ID,)),
    )
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", AsyncMock(return_value=[ROOM_ID]))
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", AsyncMock(return_value=False))

    await bot.leave_unconfigured_rooms()

    assert bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)


@pytest.mark.asyncio
async def test_trusted_sync_cannot_clear_fence_during_failed_compensating_leave(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A still-joined rejected invite remains fenced after its leave fails."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={bot.agent_user.user_id}),
    )
    leave_started = asyncio.Event()
    release_leave = asyncio.Event()

    async def delayed_failed_leave(_client: object, _room_id: str) -> bool:
        leave_started.set()
        await release_leave.wait()
        return False

    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_room", delayed_failed_leave)

    invite = asyncio.create_task(bot._room_lifecycle.handle_invite(room, INVITER_ID))
    await asyncio.wait_for(leave_started.wait(), timeout=1)
    await bot._room_lifecycle.observe_trusted_sync_rooms([ROOM_ID])
    release_leave.set()
    await invite

    assert bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)


@pytest.mark.asyncio
async def test_authoritative_departure_revokes_live_and_accepted_ownership(tmp_path: Path) -> None:
    """A Matrix departure is the single boundary that revokes both room owners."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    bot._room_lifecycle._remember_invited_room(ROOM_ID)

    await bot._apply_own_room_membership(
        OwnRoomMembership(
            joined_room_ids=frozenset(),
            left_room_ids=frozenset({ROOM_ID}),
            invited_room_ids=frozenset(),
            departures=(ReportedDeparture(room_id=ROOM_ID, observation_id="departure"),),
        ),
    )

    assert ROOM_ID not in bot.client.invited_rooms
    assert load_invited_rooms(_accepted_path(config)) == set()
    assert room.inviter == INVITER_ID


@pytest.mark.asyncio
async def test_new_acceptance_during_departure_fence_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Departure settles old ownership before a later invite can acquire it."""
    config = bind_runtime_paths(
        Config(
            administrators=[INVITER_ID],
            router=RouterConfig(
                model="default",
                accept_invites=True,
                access=ResponderAccessConfig(
                    current_room_members=False,
                    members_of_rooms=[],
                ),
            ),
        ),
        test_runtime_paths(tmp_path),
    )
    bot, _room = _router_bot(config)
    bot._room_lifecycle._remember_invited_room(ROOM_ID)
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(ROOM_ID,)),
    )
    fence_started = asyncio.Event()
    release_fence = asyncio.Event()

    async def delayed_fence(_fence: object, _departures: object) -> None:
        fence_started.set()
        await release_fence.wait()

    monkeypatch.setattr(type(bot._membership_fence), "fence_reported_departures", delayed_fence)
    departure = asyncio.create_task(
        bot._apply_own_room_membership(
            OwnRoomMembership(
                joined_room_ids=frozenset(),
                left_room_ids=frozenset({ROOM_ID}),
                invited_room_ids=frozenset(),
                departures=(ReportedDeparture(room_id=ROOM_ID, observation_id="departure"),),
            ),
        ),
    )
    await asyncio.wait_for(fence_started.wait(), timeout=1)
    assert load_invited_rooms(_accepted_path(config)) == set()

    new_invite = nio.MatrixInvitedRoom(ROOM_ID, bot.agent_user.user_id)
    new_invite.inviter = INVITER_ID
    bot.client.invited_rooms[ROOM_ID] = new_invite
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    invite = asyncio.create_task(bot._room_lifecycle.handle_invite(new_invite, INVITER_ID))
    await asyncio.sleep(0)
    join_room.assert_not_awaited()

    release_fence.set()
    await departure
    await invite

    join_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}
    assert bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)
