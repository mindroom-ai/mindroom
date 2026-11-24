"""Tests for the fail-closed cross-room thread fork authorization gate."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import ORIGINAL_SENDER_KEY, RuntimePaths
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.matrix.thread_fork_access import authorize_cross_room_fork_hop
from mindroom.matrix.thread_forks import CrossRoomForkDecision, CrossRoomForkHop
from tests.conftest import bind_runtime_paths, make_visible_message, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_SOURCE = "!source:localhost"
_TARGET = "!target:localhost"
_AGENT = "@mindroom_general:localhost"
_OTHER_AGENT = "@mindroom_neon:localhost"
_ALICE = "@alice:localhost"
_ALICE_BRIDGE = "@alice_tg:localhost"
_BOB = "@bob:localhost"
_BOT_ACCOUNT = "@bridge_bot:localhost"


def _setup(tmp_path: Path, *, sync_mode: str = "classic") -> tuple[Config, RuntimePaths]:
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            models={"default": ModelConfig(provider="openai", id="gpt-4o-mini")},
            agents={
                "general": AgentConfig(display_name="General", rooms=[_SOURCE, _TARGET]),
                "neon": AgentConfig(display_name="Neon", rooms=[_TARGET]),
            },
            authorization=AuthorizationConfig(
                aliases={_ALICE: [_ALICE_BRIDGE]},
            ),
            bot_accounts=[_BOT_ACCOUNT],
            matrix_sync=MatrixSyncConfig(mode=sync_mode),  # type: ignore[arg-type]
        ),
        paths,
    )
    return config, paths


def _room(
    room_id: str,
    *,
    joined: tuple[str, ...] = (_AGENT, _ALICE),
    invited: tuple[str, ...] = (),
    encrypted: bool = False,
    history_visibility: str = "shared",
    members_synced: bool = True,
    invited_member_count: int | None = None,
) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id, _AGENT)
    room.encrypted = encrypted
    room.history_visibility = history_visibility
    room.members_synced = members_synced
    for user_id in joined:
        room.add_member(user_id, user_id, None)
    for user_id in invited:
        room.add_member(user_id, user_id, None, invited=True)
    if invited_member_count is not None:
        room.summary = nio.RoomSummary(
            invited_member_count=invited_member_count,
            joined_member_count=len(joined),
        )
    return room


def _client(rooms: dict[str, nio.MatrixRoom], *, user_id: str = _AGENT) -> AsyncMock:
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = user_id
    client.rooms = rooms
    client.olm = MagicMock()
    client.joined_members = AsyncMock(return_value=nio.JoinedMembersError("forbidden"))
    client.room_get_state = AsyncMock(return_value=nio.RoomGetStateError("forbidden"))
    client.room_get_state_event = AsyncMock(return_value=nio.RoomGetStateEventError("forbidden"))
    return client


def _hop(
    *,
    target_room_id: str = _TARGET,
    child_room_id: str = _TARGET,
    parent_room_id: str = _SOURCE,
    sender: str = _ALICE,
    content: dict[str, object] | None = None,
) -> CrossRoomForkHop:
    return CrossRoomForkHop(
        target_room_id=target_room_id,
        child_room_id=child_room_id,
        parent_room_id=parent_room_id,
        fork_root=make_visible_message(event_id="$fork_root", sender=sender, content=content),
        fork_point_event_id="$fork_point",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_encrypted", "target_encrypted"),
    [(False, False), (False, True), (True, True)],
)
async def test_authorized_encryption_and_audience_subset_allowed(
    tmp_path: Path,
    source_encrypted: bool,
    target_encrypted: bool,
) -> None:
    """Matching or upgraded encryption with subset human audience (ignoring agents/bots) is ALLOWED."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE, _BOB), encrypted=source_encrypted)
    target = _room(
        _TARGET,
        joined=(_AGENT, _OTHER_AGENT, _BOT_ACCOUNT, _ALICE_BRIDGE),
        invited=(_BOB,),
        encrypted=target_encrypted,
    )
    client = _client({_SOURCE: source, _TARGET: target})

    for nonblocking in (True, False):
        decision = await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(sender=_ALICE_BRIDGE),
            requester_id=_ALICE_BRIDGE,
            nonblocking=nonblocking,
            target_room=target,
        )
        assert decision is CrossRoomForkDecision.ALLOWED


@pytest.mark.asyncio
async def test_encrypted_source_to_unencrypted_target_and_unknown_encryption_denied(tmp_path: Path) -> None:
    """Encrypted -> unencrypted and unknown encryption states fail closed."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, encrypted=True)
    target = _room(_TARGET, encrypted=False)
    client = _client({_SOURCE: source, _TARGET: target})

    for nonblocking in (True, False):
        assert (
            await authorize_cross_room_fork_hop(
                client=client,
                config=config,
                runtime_paths=paths,
                hop=_hop(),
                requester_id=_ALICE,
                nonblocking=nonblocking,
            )
            is CrossRoomForkDecision.DENIED
        )

    # Unknown target encryption in strict mode when target room is not cached and state fetch errors.
    client_uncached_target = _client({_SOURCE: _room(_SOURCE, encrypted=False)})
    assert (
        await authorize_cross_room_fork_hop(
            client=client_uncached_target,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.DENIED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("contact_room_id", [_SOURCE, _TARGET])
async def test_contact_room_source_or_target_denied(tmp_path: Path, contact_room_id: str) -> None:
    """Contact rooms are never allowed as cross-room fork source or target."""
    config, paths = _setup(tmp_path)
    record = ContactRecord(
        id="c1",
        status="active",
        requester=ContactParty(kind="agent", id="general", owner=_ALICE),
        target=ContactParty(kind="person", id=_BOB, owner=_BOB),
        reason="test",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id="!origin:localhost",
        origin_thread_id=None,
        room_id=contact_room_id,
    )
    update_contacts(paths, lambda book: book.contacts.append(record))
    client = _client({_SOURCE: _room(_SOURCE), _TARGET: _room(_TARGET)})

    assert (
        await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.DENIED
    )


@pytest.mark.asyncio
async def test_agent_not_joined_to_parent_room_denied(tmp_path: Path) -> None:
    """Reading agent must be currently joined to parent room."""
    config, paths = _setup(tmp_path)

    # Parent absent from client.rooms and joined_members fails.
    client_missing = _client({_TARGET: _room(_TARGET)})
    assert (
        await authorize_cross_room_fork_hop(
            client=client_missing,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=True,
        )
        is CrossRoomForkDecision.DENIED
    )
    assert (
        await authorize_cross_room_fork_hop(
            client=client_missing,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.DENIED
    )

    # Parent in client.rooms but agent not in joined members.
    client_stale = _client({_SOURCE: _room(_SOURCE, joined=(_ALICE,)), _TARGET: _room(_TARGET)})
    assert (
        await authorize_cross_room_fork_hop(
            client=client_stale,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.DENIED
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("requester_id", [None, _AGENT, _BOT_ACCOUNT, _BOB])
async def test_requester_validation_and_membership_denied(tmp_path: Path, requester_id: str | None) -> None:
    """Missing, non-human, or unjoined human requesters fail closed."""
    config, paths = _setup(tmp_path)
    client = _client({_SOURCE: _room(_SOURCE, joined=(_AGENT, _ALICE)), _TARGET: _room(_TARGET)})

    assert (
        await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=requester_id,
            nonblocking=False,
        )
        is CrossRoomForkDecision.DENIED
    )


@pytest.mark.asyncio
async def test_fork_root_author_checks_and_original_sender_trust(tmp_path: Path) -> None:
    """Fork root author must be a joined human; ORIGINAL_SENDER_KEY is only trusted from managed senders."""
    config, paths = _setup(tmp_path)
    client = _client(
        {
            _SOURCE: _room(_SOURCE, joined=(_AGENT, _ALICE)),
            _TARGET: _room(_TARGET, joined=(_AGENT, _ALICE)),
        },
    )

    # Unjoined human author spoofing ORIGINAL_SENDER_KEY of joined Alice -> DENIED.
    assert (
        await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(sender=_BOB, content={ORIGINAL_SENDER_KEY: _ALICE}),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.DENIED
    )

    # Trusted managed relay without ORIGINAL_SENDER_KEY -> DENIED.
    assert (
        await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(sender=_AGENT),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.DENIED
    )

    # Trusted managed relay with joined human ORIGINAL_SENDER_KEY -> ALLOWED.
    assert (
        await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(sender=_AGENT, content={ORIGINAL_SENDER_KEY: _ALICE}),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.ALLOWED
    )

    # Same-room ancestor edge inside _SOURCE (child_room_id == parent_room_id) does not require old author membership.
    assert (
        await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(child_room_id=_SOURCE, parent_room_id=_SOURCE, sender=_BOB),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.ALLOWED
    )


@pytest.mark.asyncio
async def test_audience_boundary_denies_joined_outsider_invited_outsider_and_world_readable(tmp_path: Path) -> None:
    """Target room with any outsider human (joined or invited) or world_readable history fails closed."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE))

    for target in (
        _room(_TARGET, joined=(_AGENT, _ALICE, _BOB)),
        _room(_TARGET, joined=(_AGENT, _ALICE), invited=(_BOB,)),
        _room(_TARGET, joined=(_AGENT, _ALICE), history_visibility="world_readable"),
    ):
        client = _client({_SOURCE: source, _TARGET: target})
        assert (
            await authorize_cross_room_fork_hop(
                client=client,
                config=config,
                runtime_paths=paths,
                hop=_hop(),
                requester_id=_ALICE,
                nonblocking=False,
            )
            is CrossRoomForkDecision.DENIED
        )


@pytest.mark.asyncio
async def test_nonblocking_defers_on_incomplete_cache_and_strict_resolves(tmp_path: Path) -> None:
    """Incomplete cached membership or sliding-sync unseen invites DEFER in nonblocking and resolve in strict."""
    config, paths = _setup(tmp_path, sync_mode="sliding")

    # Parent membership incomplete in nonblocking -> DEFERRED; strict refreshes joined_members -> ALLOWED.
    source_partial = _room(_SOURCE, joined=(_AGENT,), members_synced=False)
    target_synced = _room(_TARGET, joined=(_AGENT, _ALICE), members_synced=True)
    client = _client({_SOURCE: source_partial, _TARGET: target_synced})
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            room_id=_SOURCE,
            members=[nio.RoomMember(_AGENT, "General", None), nio.RoomMember(_ALICE, "Alice", None)],
        ),
    )
    assert (
        await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=True,
        )
        is CrossRoomForkDecision.DEFERRED
    )
    assert (
        await authorize_cross_room_fork_hop(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.ALLOWED
    )

    # Sliding sync target room with unseen invite (invited_member_count=1, invited_users empty):
    # nonblocking -> DEFERRED; strict reads room_get_state and denies outsider invite!
    source_full = _room(_SOURCE, joined=(_AGENT, _ALICE), members_synced=True)
    target_unseen_invite = _room(
        _TARGET,
        joined=(_AGENT, _ALICE),
        members_synced=False,
        invited_member_count=1,
    )
    client_sliding = _client({_SOURCE: source_full, _TARGET: target_unseen_invite})
    client_sliding.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            room_id=_TARGET,
            members=[nio.RoomMember(_AGENT, "General", None), nio.RoomMember(_ALICE, "Alice", None)],
        ),
    )
    client_sliding.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            room_id=_TARGET,
            events=[
                {"type": "m.room.member", "state_key": _AGENT, "content": {"membership": "join"}},
                {"type": "m.room.member", "state_key": _ALICE, "content": {"membership": "join"}},
                {"type": "m.room.member", "state_key": _BOB, "content": {"membership": "invite"}},
            ],
        ),
    )
    assert (
        await authorize_cross_room_fork_hop(
            client=client_sliding,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=True,
        )
        is CrossRoomForkDecision.DEFERRED
    )
    assert (
        await authorize_cross_room_fork_hop(
            client=client_sliding,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
        is CrossRoomForkDecision.DENIED
    )
