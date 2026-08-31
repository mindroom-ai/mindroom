"""Boundary tests for live-only Matrix invite ownership."""

from __future__ import annotations

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
from mindroom.matrix.users import AgentMatrixUser
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
    assert load_invited_rooms(accepted_path) == set()


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
