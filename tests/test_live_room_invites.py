"""Boundary tests for live-only Matrix invite ownership."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.client_room_admin import RoomJoinOutcome
from mindroom.matrix.invited_rooms_store import invited_rooms_path, load_invited_rooms
from mindroom.matrix.rooms import leave_rooms as leave_matrix_rooms
from mindroom.matrix.sync_loop import OwnRoomMembership, own_membership_from_sync
from mindroom.matrix.users import AgentMatrixUser
from mindroom.membership_models import ReportedDeparture
from tests.bot_helpers import FencedRoomRecorder, make_test_agent_bot
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
    bot._reply_membership_sync = AgentReplyMembershipSync(bot._runtime_view.agent_reply_memberships)
    bot.client = AsyncMock()
    bot.client.rooms = {}
    invited_room = nio.MatrixInvitedRoom(ROOM_ID, bot.agent_user.user_id)
    invited_room.inviter = INVITER_ID
    bot.client.invited_rooms = {ROOM_ID: invited_room}
    return bot, invited_room


def _accepted_path(config: Config) -> Path:
    return invited_rooms_path(runtime_paths_for(config).storage_root, ROUTER_AGENT_NAME)


def _departure_membership() -> OwnRoomMembership:
    return OwnRoomMembership(
        joined_room_ids=frozenset(),
        left_room_ids=frozenset({ROOM_ID}),
        invited_room_ids=frozenset(),
        departures=(ReportedDeparture(room_id=ROOM_ID, observation_id="departure"),),
    )


def _departure_sync_response(sync_mode: str = "classic") -> nio.SyncResponse | nio.SlidingSyncResponse:
    if sync_mode == "sliding":
        response = nio.SlidingSyncResponse.from_dict(
            {
                "pos": "s_overlapping_departure",
                "rooms": {ROOM_ID: {"membership": "leave", "timeline": []}},
            },
        )
        assert isinstance(response, nio.SlidingSyncResponse)
        return response
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": "s_overlapping_departure",
            "rooms": {
                "join": {},
                "invite": {},
                "leave": {ROOM_ID: {"timeline": {"events": []}, "state": {"events": []}}},
            },
        },
    )
    assert isinstance(response, nio.SyncResponse)
    return response


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
    bot._room_lifecycle._send_invite_welcome.assert_awaited_once_with(ROOM_ID, INVITER_ID)
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
async def test_joined_sync_cache_removal_does_not_revoke_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Nio's normal joined-sync cache transition cannot reject a successful join."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )

    async def joined_members_after_sync(_client: object, _room_id: str) -> set[str]:
        bot.client.invited_rooms.pop(ROOM_ID)
        return {INVITER_ID, bot.agent_user.user_id}

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        joined_members_after_sync,
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_not_awaited()
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_mutated_replacement_cannot_hide_behind_joined_sync_cache_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A replacement observed before normal cache removal invalidates the old attempt."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )

    async def replacement_then_joined_sync(_client: object, _room_id: str) -> set[str]:
        room.inviter = "@replacement:localhost"
        bot.client.invited_rooms.pop(ROOM_ID)
        return {INVITER_ID, bot.agent_user.user_id}

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        replacement_then_joined_sync,
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == set()


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
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

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
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == set()


@pytest.mark.asyncio
async def test_new_configured_owner_prevents_invite_compensating_leave(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A config update that owns the joined room defeats older invite rejection."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )

    async def configure_before_denial(_client: object, _room_id: str) -> set[str]:
        bot.rooms = [ROOM_ID]
        return {bot.agent_user.user_id}

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        configure_before_denial,
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)
    configured_setup = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "_on_configured_room_joined", configured_setup)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_not_awaited()
    configured_setup.assert_awaited_once_with(ROOM_ID)


@pytest.mark.asyncio
async def test_configured_reconciliation_waits_for_invite_ownership_before_reading_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured setup cannot use membership observed before invite compensation settles."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        join_room,
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={bot.agent_user.user_id}),
    )
    fence_started = asyncio.Event()
    release_fence = asyncio.Event()

    async def delayed_fence(_room_id: str) -> None:
        fence_started.set()
        await release_fence.wait()

    monkeypatch.setattr(bot._room_lifecycle, "_ensure_join_decrypt_notice_fence", delayed_fence)
    left = False

    async def current_joined_rooms(_client: object) -> list[str]:
        return [] if left else [ROOM_ID]

    async def leave_current_room(_client: object, _room_id: str) -> bool:
        nonlocal left
        left = True
        return True

    joined_rooms = AsyncMock(side_effect=current_joined_rooms)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", joined_rooms)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", AsyncMock(side_effect=leave_current_room))
    configured_setup = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "_on_configured_room_joined", configured_setup)

    invite = asyncio.create_task(bot._room_lifecycle.handle_invite(room, INVITER_ID))
    await asyncio.wait_for(fence_started.wait(), timeout=1)
    bot.rooms = [ROOM_ID]
    configured = asyncio.create_task(bot.join_configured_rooms())
    await asyncio.sleep(0)

    configured_setup.assert_not_awaited()
    joined_rooms.assert_not_awaited()

    release_fence.set()
    await invite
    await configured

    configured_setup.assert_awaited_once_with(ROOM_ID)
    assert join_room.await_count == 2


@pytest.mark.asyncio
async def test_replaced_live_invite_survives_rejected_old_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Compensating an old join cannot reject the newer Matrix invite."""
    replacement_inviter = "@replacement:localhost"
    config = bind_runtime_paths(
        Config(
            administrators=[INVITER_ID, replacement_inviter],
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
    server_membership = "invite"

    async def join_after_replacement(_client: object, _room_id: str) -> RoomJoinOutcome:
        nonlocal server_membership
        room.inviter = replacement_inviter
        server_membership = "join"
        return RoomJoinOutcome.JOINED

    async def leave_current_membership(_client: object, _room_id: str) -> bool:
        nonlocal server_membership
        server_membership = "leave"
        return True

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(side_effect=join_after_replacement),
    )
    leave_room = AsyncMock(side_effect=leave_current_membership)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_not_awaited()
    assert server_membership == "join"
    assert load_invited_rooms(_accepted_path(config)) == set()
    assert bot.client.invited_rooms == {ROOM_ID: room}
    assert room.inviter == replacement_inviter

    await bot._room_lifecycle.handle_invite(room, replacement_inviter)

    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_unauthorized_replacement_cannot_retain_joined_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A replacement from a denied inviter cannot suppress compensation."""
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
    server_membership = "invite"

    async def join_after_replacement(_client: object, _room_id: str) -> RoomJoinOutcome:
        nonlocal server_membership
        room.inviter = replacement_inviter
        server_membership = "join"
        return RoomJoinOutcome.JOINED

    async def leave_current_membership(_client: object, _room_id: str) -> bool:
        nonlocal server_membership
        server_membership = "leave"
        return True

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(side_effect=join_after_replacement),
    )
    leave_room = AsyncMock(side_effect=leave_current_membership)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert server_membership == "leave"
    assert load_invited_rooms(_accepted_path(config)) == set()


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
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

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
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

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
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)
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
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

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
async def test_welcome_does_not_hold_invite_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Best-effort welcome delivery cannot block later room reconciliation."""
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
    welcome_started = asyncio.Event()
    release_welcome = asyncio.Event()
    ownership_acquired = asyncio.Event()

    async def delayed_welcome(_room_id: str, _sender: str) -> None:
        welcome_started.set()
        await release_welcome.wait()

    async def acquire_room_ownership() -> None:
        async with bot._room_lifecycle.invite_ownership(ROOM_ID):
            ownership_acquired.set()

    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", delayed_welcome)
    invite = asyncio.create_task(bot._room_lifecycle.handle_invite(room, INVITER_ID))
    await asyncio.wait_for(welcome_started.wait(), timeout=1)
    later_owner = asyncio.create_task(acquire_room_ownership())
    try:
        await asyncio.wait_for(ownership_acquired.wait(), timeout=1)
    finally:
        release_welcome.set()
        await invite
        await later_owner


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
async def test_joined_sync_restores_accepted_room_setup_after_unavailable_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A trusted joined snapshot retries accepted-room setup missed at startup."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}
    bot._room_lifecycle._remember_invited_room(ROOM_ID)
    joined_rooms = AsyncMock(side_effect=[None, [ROOM_ID]])
    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", joined_rooms)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    joined_setup = AsyncMock()
    monkeypatch.setattr(bot._room_lifecycle, "_on_configured_room_joined", joined_setup)

    await bot.join_configured_rooms()
    join_room.assert_not_awaited()
    joined_setup.assert_not_awaited()

    await bot._apply_own_room_membership_before_invites(
        OwnRoomMembership(
            joined_room_ids=frozenset({ROOM_ID}),
            left_room_ids=frozenset(),
            invited_room_ids=frozenset(),
            departures=(),
        ),
    )
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert joined_rooms.await_count == 2
    joined_setup.assert_awaited_once_with(ROOM_ID)


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
async def test_cleanup_keeps_authoritative_inventory_when_ownership_recheck_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A transient second inventory failure cannot abandon a room already found unowned."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(side_effect=[[ROOM_ID], None]),
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

    await bot.leave_unconfigured_rooms()

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)


@pytest.mark.asyncio
async def test_unowned_cleanup_rechecks_ownership_after_inflight_invite(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup cannot leave a room while its invite acceptance owns the room lock."""
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
    join_setup_started = asyncio.Event()
    release_join_setup = asyncio.Event()

    async def delayed_join_setup(_room_id: str) -> None:
        join_setup_started.set()
        await release_join_setup.wait()

    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        on_room_joined=delayed_join_setup,
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=[ROOM_ID]),
    )
    leave_rooms = AsyncMock()
    monkeypatch.setattr("mindroom.bot_room_lifecycle.leave_rooms", leave_rooms)
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    invite = asyncio.create_task(bot._room_lifecycle.handle_invite(room, INVITER_ID))
    await asyncio.wait_for(join_setup_started.wait(), timeout=1)
    cleanup = asyncio.create_task(bot.leave_unconfigured_rooms())
    await asyncio.sleep(0)
    release_join_setup.set()
    await invite
    await cleanup

    leave_rooms.assert_not_awaited()
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_confirmed_local_leave_immediately_revokes_durable_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A successful local leave cannot wait for a later sync to forget the room."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot._room_lifecycle._remember_invited_room(ROOM_ID)
    bot.config.router.accept_invites = False
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=[ROOM_ID]),
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)

    await bot.leave_unconfigured_rooms()

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    assert load_invited_rooms(_accepted_path(config)) == set()


@pytest.mark.asyncio
async def test_entity_removal_cleanup_uses_confirmed_leave_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Entity removal cannot bypass accepted-room revocation after leaving."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot._room_lifecycle._remember_invited_room(ROOM_ID)
    monkeypatch.setattr(
        "mindroom.bot.get_joined_rooms",
        AsyncMock(return_value=[ROOM_ID]),
    )
    monkeypatch.setattr(
        "mindroom.matrix.rooms.is_dm_room",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "mindroom.matrix.rooms.leave_room",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(bot, "stop", AsyncMock())

    await bot.cleanup()

    assert load_invited_rooms(_accepted_path(config)) == set()


@pytest.mark.asyncio
async def test_entity_removal_attempts_every_room_after_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One confirmed-leave cleanup error cannot strand later rooms."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    first_room = "!first:localhost"
    second_room = "!second:localhost"
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.filter_non_dm_rooms",
        AsyncMock(return_value=[first_room, second_room]),
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)
    cleanup_calls: list[str] = []
    first_error = OSError("first cleanup failed")

    async def cleanup_room(room_id: str) -> None:
        cleanup_calls.append(room_id)
        if room_id == first_room:
            raise first_error

    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        on_room_left=cleanup_room,
    )

    with pytest.raises(OSError, match="first cleanup failed"):
        await bot._room_lifecycle.leave_non_dm_rooms_for_cleanup([first_room, second_room])

    assert [awaited.args[1] for awaited in leave_room.await_args_list] == [first_room, second_room]
    assert cleanup_calls == [first_room, second_room]


@pytest.mark.asyncio
async def test_confirmed_leave_fences_departure_after_join_fence_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each confirmed-leave cleanup step runs even when an earlier step fails."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.invited_rooms = {}
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(ROOM_ID,)),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=[ROOM_ID]),
    )
    monkeypatch.setattr(
        "mindroom.matrix.rooms.leave_room",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        bot._room_lifecycle,
        "_clear_join_decrypt_notice_fence",
        AsyncMock(side_effect=OSError("continuity save failed")),
    )
    on_room_left = AsyncMock()
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        on_room_left=on_room_left,
    )

    with pytest.raises(OSError, match="continuity save failed"):
        await bot.leave_unconfigured_rooms()

    on_room_left.assert_awaited_once_with(ROOM_ID)


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

    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", delayed_failed_leave)

    invite = asyncio.create_task(bot._room_lifecycle.handle_invite(room, INVITER_ID))
    await asyncio.wait_for(leave_started.wait(), timeout=1)
    await bot._room_lifecycle.observe_trusted_sync_rooms([ROOM_ID])
    release_leave.set()
    await invite

    assert bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)


@pytest.mark.asyncio
async def test_compensating_leave_restores_fence_before_network_leave(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A joined sync cannot leave rejection cleanup unfenced during its leave."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )

    async def deny_after_joined_sync(_client: object, _room_id: str) -> set[str]:
        await bot._room_lifecycle.observe_trusted_sync_rooms([ROOM_ID])
        return {bot.agent_user.user_id}

    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        deny_after_joined_sync,
    )
    leave_started = asyncio.Event()
    release_leave = asyncio.Event()

    async def delayed_failed_leave(_client: object, _room_id: str) -> bool:
        leave_started.set()
        await release_leave.wait()
        return False

    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", delayed_failed_leave)

    invite = asyncio.create_task(bot._room_lifecycle.handle_invite(room, INVITER_ID))
    await asyncio.wait_for(leave_started.wait(), timeout=1)

    assert bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)

    release_leave.set()
    await invite


@pytest.mark.asyncio
async def test_leave_cancellation_remains_primary_when_cleanup_fails() -> None:
    """Protected cleanup failure is retained without swallowing caller cancellation."""
    client = AsyncMock()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_error = OSError("accepted-room delete failed")

    async def failing_cleanup(_room_id: str) -> None:
        cleanup_started.set()
        await release_cleanup.wait()
        raise cleanup_error

    with patch("mindroom.matrix.rooms.leave_room", AsyncMock(return_value=True)):
        leave = asyncio.create_task(
            leave_matrix_rooms(client, [ROOM_ID], on_room_left=failing_cleanup),
        )
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)
        leave.cancel()
        await asyncio.sleep(0)
        release_cleanup.set()

        with pytest.raises(asyncio.CancelledError) as cancellation:
            await leave

    assert isinstance(cancellation.value.__cause__, OSError)


@pytest.mark.asyncio
async def test_repeated_cancellation_cannot_interrupt_compensating_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A confirmed compensating leave finishes cleanup before cancellation escapes."""
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
    monkeypatch.setattr(
        "mindroom.matrix.rooms.leave_room",
        AsyncMock(return_value=True),
    )
    clear_started = asyncio.Event()
    release_clear = asyncio.Event()

    async def delayed_clear(_room_id: str) -> None:
        clear_started.set()
        await release_clear.wait()

    monkeypatch.setattr(bot._room_lifecycle, "_clear_join_decrypt_notice_fence", delayed_clear)
    on_room_left = AsyncMock()
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        on_room_left=on_room_left,
    )

    invite = asyncio.create_task(bot._room_lifecycle.handle_invite(room, INVITER_ID))
    await asyncio.wait_for(clear_started.wait(), timeout=1)
    invite.cancel()
    await asyncio.sleep(0)
    invite.cancel()
    release_clear.set()

    with pytest.raises(asyncio.CancelledError):
        await invite

    on_room_left.assert_awaited_once_with(ROOM_ID)


@pytest.mark.asyncio
async def test_cleanup_cancellation_waits_for_contended_room_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancellation cannot skip a room while another operation owns its lock."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.filter_non_dm_rooms",
        AsyncMock(return_value=[ROOM_ID]),
    )
    leave_room = AsyncMock(return_value=True)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", leave_room)
    on_room_left = AsyncMock()
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        on_room_left=on_room_left,
    )

    async with bot._room_lifecycle.invite_ownership(ROOM_ID):
        cleanup = asyncio.create_task(bot._room_lifecycle.leave_non_dm_rooms_for_cleanup([ROOM_ID]))
        await asyncio.sleep(0)
        cleanup.cancel()
        await asyncio.sleep(0)
        cleanup.cancel()
        await asyncio.sleep(0)
        assert not cleanup.done()

    with pytest.raises(asyncio.CancelledError):
        await cleanup

    leave_room.assert_awaited_once_with(bot.client, ROOM_ID)
    on_room_left.assert_awaited_once_with(ROOM_ID)


@pytest.mark.asyncio
async def test_configured_owner_releases_failed_leave_fence_protection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Configured ownership lets the next trusted joined sync settle an old fence."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.rooms = [ROOM_ID]
    bot._room_lifecycle._join_fence_protected_room_ids.add(ROOM_ID)
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(ROOM_ID,)),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_joined_rooms",
        AsyncMock(return_value=[ROOM_ID]),
    )
    bot._room_lifecycle.deps = replace(
        bot._room_lifecycle.deps,
        on_configured_room_joined=AsyncMock(),
    )

    await bot.join_configured_rooms()
    await bot._room_lifecycle.observe_trusted_sync_rooms([ROOM_ID])

    assert not bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)


@pytest.mark.asyncio
async def test_accepted_owner_releases_failed_leave_fence_protection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Accepted invite ownership lets the next trusted joined sync settle an old fence."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    bot._room_lifecycle._join_fence_protected_room_ids.add(ROOM_ID)
    bot._room_lifecycle.apply_continuity_record(
        bot._sync_continuity_store.update_join_fences(add=(ROOM_ID,)),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await bot._room_lifecycle.handle_invite(room, INVITER_ID)
    await bot._room_lifecycle.observe_trusted_sync_rooms([ROOM_ID])

    assert not bot._room_lifecycle.decrypt_notice_is_fenced(ROOM_ID)


@pytest.mark.parametrize("sync_mode", ["classic", "sliding"])
@pytest.mark.asyncio
async def test_departure_admitted_during_invite_join_abandons_that_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    sync_mode: str,
) -> None:
    """An ownership loss overlapping a join makes that attempt fail closed."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.room_leave = AsyncMock(return_value=nio.RoomLeaveResponse())
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_join(_client: object, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", delayed_join)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    await bot.reconcile_live_invites()
    await asyncio.wait_for(join_started.wait(), timeout=1)
    bot._before_sync_response_admission(_departure_sync_response(sync_mode))
    departure = asyncio.create_task(bot._apply_own_room_membership_before_invites(_departure_membership()))
    await asyncio.sleep(0)
    assert not departure.done()

    release_join.set()
    await departure
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert load_invited_rooms(_accepted_path(config)) == set()
    bot.client.room_leave.assert_awaited_once_with(ROOM_ID)


@pytest.mark.asyncio
async def test_same_invite_snapshot_during_join_is_superseded_by_that_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An invite snapshot captured before join completion cannot revoke the later join."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot.client.room_leave = AsyncMock(return_value=nio.RoomLeaveResponse())
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_join(_client: object, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.JOINED

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", delayed_join)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    await bot.reconcile_live_invites()
    await asyncio.wait_for(join_started.wait(), timeout=1)
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": "s_same_invite",
            "rooms": {
                "join": {},
                "invite": {ROOM_ID: {"invite_state": {"events": []}}},
                "leave": {},
            },
        },
    )
    assert isinstance(response, nio.SyncResponse)
    bot._before_sync_response_admission(response)
    membership = own_membership_from_sync(response, self_user_id=bot.agent_user.user_id)
    application = asyncio.create_task(bot._apply_own_room_membership_before_invites(membership))
    await asyncio.sleep(0)
    assert not application.done()

    release_join.set()
    await application
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    bot.client.room_leave.assert_not_awaited()
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}


@pytest.mark.asyncio
async def test_departure_admitted_while_invite_waits_for_room_owner_prevents_join(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ownership loss invalidates invite work already queued on the room lock."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    ownership = bot._room_lifecycle.invite_ownership(ROOM_ID)
    await ownership.__aenter__()
    try:
        await bot.reconcile_live_invites()
        await asyncio.sleep(0)
        bot._before_sync_response_admission(_departure_sync_response())
        departure = asyncio.create_task(bot._apply_own_room_membership_before_invites(_departure_membership()))
    finally:
        await ownership.__aexit__(None, None, None)

    await departure
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    join_room.assert_not_awaited()
    assert load_invited_rooms(_accepted_path(config)) == set()


@pytest.mark.asyncio
async def test_departure_admission_pauses_remaining_invite_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pass cannot start later stale invite work after sync pre-admission."""
    config = _router_config(tmp_path)
    bot, first_room = _router_bot(config)
    first_room_id = "!first:localhost"
    second_room_id = "!second:localhost"
    first_room.room_id = first_room_id
    second_room = nio.MatrixInvitedRoom(second_room_id, bot.agent_user.user_id)
    second_room.inviter = INVITER_ID
    bot.client.invited_rooms = {
        first_room_id: first_room,
        second_room_id: second_room,
    }
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    original_handle_invite = bot._room_lifecycle.handle_invite

    async def block_first_invite(room: nio.MatrixRoom, sender: str) -> None:
        if room is first_room:
            first_started.set()
            await release_first.wait()
            return
        await original_handle_invite(room, sender)

    monkeypatch.setattr(bot._room_lifecycle, "handle_invite", block_first_invite)
    join_room = AsyncMock(return_value=RoomJoinOutcome.JOINED)
    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", join_room)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())

    await bot.reconcile_live_invites()
    await asyncio.wait_for(first_started.wait(), timeout=1)
    response = nio.SyncResponse.from_dict(
        {
            "next_batch": "s_second_departed",
            "rooms": {
                "join": {},
                "invite": {},
                "leave": {second_room_id: {"timeline": {"events": []}, "state": {"events": []}}},
            },
        },
    )
    assert isinstance(response, nio.SyncResponse)
    bot._before_sync_response_admission(response)
    release_first.set()
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    join_room.assert_not_awaited()
    assert bot._live_invite_reconciliation_pending
    await bot._apply_own_room_membership_before_invites(
        OwnRoomMembership(
            joined_room_ids=frozenset(),
            left_room_ids=frozenset({second_room_id}),
            invited_room_ids=frozenset(),
            departures=(ReportedDeparture(room_id=second_room_id, observation_id="second-departed"),),
        ),
    )
    bot._sync_response_applying = False
    bot._schedule_live_invite_reconciliation()
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)
    join_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_departure_overlapping_later_failed_join_revokes_prior_acceptance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the exact successful join overlapped by a response can supersede it."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.get_room_members",
        AsyncMock(return_value={INVITER_ID, bot.agent_user.user_id}),
    )
    monkeypatch.setattr(bot._room_lifecycle, "_send_invite_welcome", AsyncMock())
    monkeypatch.setattr(
        "mindroom.bot_room_lifecycle.join_room",
        AsyncMock(return_value=RoomJoinOutcome.JOINED),
    )
    await bot._room_lifecycle.handle_invite(room, INVITER_ID)
    assert load_invited_rooms(_accepted_path(config)) == {ROOM_ID}

    replacement_room = nio.MatrixInvitedRoom(ROOM_ID, bot.agent_user.user_id)
    replacement_room.inviter = INVITER_ID
    bot.client.invited_rooms = {ROOM_ID: replacement_room}
    join_started = asyncio.Event()
    release_join = asyncio.Event()

    async def delayed_failed_join(_client: object, _room_id: str) -> RoomJoinOutcome:
        join_started.set()
        await release_join.wait()
        return RoomJoinOutcome.RETRYABLE_FAILURE

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", delayed_failed_join)
    await bot.reconcile_live_invites()
    await asyncio.wait_for(join_started.wait(), timeout=1)
    bot._before_sync_response_admission(_departure_sync_response())
    departure = asyncio.create_task(bot._apply_own_room_membership_before_invites(_departure_membership()))
    await asyncio.sleep(0)
    assert not departure.done()

    release_join.set()
    await departure
    assert await wait_for_background_tasks(timeout=1, owner=bot._runtime_view)

    assert load_invited_rooms(_accepted_path(config)) == set()


@pytest.mark.asyncio
async def test_authoritative_departure_revokes_live_and_accepted_ownership(tmp_path: Path) -> None:
    """A Matrix departure is the single boundary that revokes both room owners."""
    config = _router_config(tmp_path)
    bot, room = _router_bot(config)
    bot._room_lifecycle._remember_invited_room(ROOM_ID)

    await bot._apply_own_room_membership(
        _departure_membership(),
    )

    assert ROOM_ID not in bot.client.invited_rooms
    assert load_invited_rooms(_accepted_path(config)) == set()
    assert room.inviter == INVITER_ID


@pytest.mark.asyncio
async def test_authoritative_invite_revokes_old_acceptance_but_preserves_live_evidence(tmp_path: Path) -> None:
    """A final invite proves the old accepted membership ended without consuming the new invite."""
    config = _router_config(tmp_path)
    bot, live_invite = _router_bot(config)
    bot._room_lifecycle._remember_invited_room(ROOM_ID)
    recorder = FencedRoomRecorder()
    bot._membership_fence.store = recorder
    bot._call_manager = AsyncMock()

    await bot._apply_own_room_membership(
        OwnRoomMembership(
            joined_room_ids=frozenset(),
            left_room_ids=frozenset(),
            invited_room_ids=frozenset({ROOM_ID}),
            authoritative_invited_room_ids=frozenset({ROOM_ID}),
            departures=(ReportedDeparture(room_id=ROOM_ID, observation_id="invite"),),
        ),
    )

    assert load_invited_rooms(_accepted_path(config)) == set()
    assert bot.client.invited_rooms == {ROOM_ID: live_invite}
    assert recorder.fenced_room_ids == [ROOM_ID]
    bot._call_manager.on_sync_room_membership.assert_awaited_once_with(
        joined_room_ids=frozenset(),
        left_room_ids=frozenset({ROOM_ID}),
    )


@pytest.mark.asyncio
async def test_authoritative_departure_revokes_durable_ownership_when_invites_disabled(tmp_path: Path) -> None:
    """Disabling future invites cannot preserve a room after confirmed departure."""
    config = _router_config(tmp_path)
    bot, _room = _router_bot(config)
    bot._room_lifecycle._remember_invited_room(ROOM_ID)
    bot.config.router.accept_invites = False

    await bot._apply_own_room_membership(
        OwnRoomMembership(
            joined_room_ids=frozenset(),
            left_room_ids=frozenset({ROOM_ID}),
            invited_room_ids=frozenset(),
            departures=(ReportedDeparture(room_id=ROOM_ID, observation_id="departure"),),
        ),
    )

    assert load_invited_rooms(_accepted_path(config)) == set()


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
