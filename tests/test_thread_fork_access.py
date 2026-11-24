"""Tests for the fail-closed cross-room thread fork authorization gate."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.config.models import ModelConfig
from mindroom.constants import FORK_CONTENT_KEY, ORIGINAL_SENDER_KEY, RuntimePaths
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.matrix.state import MatrixState
from mindroom.matrix.thread_diagnostics import is_thread_history_degraded
from mindroom.matrix.thread_fork_access import authorize_cross_room_fork_access
from mindroom.matrix.thread_forks import CrossRoomForkDecision, CrossRoomForkHop, with_fork_prefix
from mindroom.matrix.thread_history_result import thread_history_result
from tests.conftest import bind_runtime_paths, make_visible_message, test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state

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


def _setup(
    tmp_path: Path,
    *,
    sync_mode: str = "classic",
    persist_other_agent: bool = True,
) -> tuple[Config, RuntimePaths]:
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
    if not persist_other_agent:
        state = MatrixState.load(runtime_paths=paths)
        state.accounts.pop("agent_neon", None)
        save_matrix_state(state, paths)
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

    async def _room_state(room_id: str) -> nio.RoomGetStateResponse | nio.RoomGetStateError:
        if room_id not in rooms:
            return nio.RoomGetStateError("forbidden")
        room = rooms[room_id]
        events: list[dict[str, object]] = []
        if room.history_visibility != "shared":
            events.append(
                {
                    "type": "m.room.history_visibility",
                    "state_key": "",
                    "origin_server_ts": 0,
                    "content": {"history_visibility": room.history_visibility},
                },
            )
        return nio.RoomGetStateResponse(room_id=room_id, events=events)

    async def _state_event(
        room_id: str,
        event_type: str,
        state_key: str = "",
    ) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
        if event_type == "m.room.history_visibility" and room_id in rooms:
            return nio.RoomGetStateEventResponse(
                {"history_visibility": rooms[room_id].history_visibility},
                event_type,
                state_key,
                room_id,
            )
        return nio.RoomGetStateEventError("forbidden")

    client.room_get_state = AsyncMock(side_effect=_room_state)
    client.room_get_state_event = AsyncMock(side_effect=_state_event)
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


def _member_state_event(
    user_id: str,
    *,
    membership: str,
    origin_server_ts: int,
    replaces_state: str | None = None,
) -> dict[str, object]:
    event: dict[str, object] = {
        "type": "m.room.member",
        "state_key": user_id,
        "sender": user_id,
        "origin_server_ts": origin_server_ts,
        "content": {"membership": membership},
    }
    if replaces_state is not None:
        event["unsigned"] = {"replaces_state": replaces_state}
    return event


@pytest.mark.asyncio
async def test_nonblocking_always_defers_without_matrix_calls(tmp_path: Path) -> None:
    """Any cross-room hop in nonblocking mode defers immediately to the single strict policy path."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE))
    target = _room(_TARGET, joined=(_AGENT, _ALICE))
    client = _client({_SOURCE: source, _TARGET: target})

    for requester_id in (_ALICE, _BOB, None):
        auth = await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=requester_id,
            nonblocking=True,
            target_room=target,
        )
        assert auth.decision is CrossRoomForkDecision.DEFERRED
        assert auth.allow_segment is None
    client.room_get_state.assert_not_awaited()
    client.room_get_state_event.assert_not_awaited()
    client.joined_members.assert_not_awaited()


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
    """Matching or upgraded encryption with subset audience (ignoring persisted managed entities) is ALLOWED."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE, _BOB, _BOT_ACCOUNT), encrypted=source_encrypted)
    target = _room(
        _TARGET,
        joined=(_AGENT, _OTHER_AGENT, _BOT_ACCOUNT, _ALICE_BRIDGE),
        invited=(_BOB,),
        encrypted=target_encrypted,
    )
    client = _client({_SOURCE: source, _TARGET: target})

    auth = await authorize_cross_room_fork_access(
        client=client,
        config=config,
        runtime_paths=paths,
        hop=_hop(sender=_ALICE_BRIDGE),
        requester_id=_ALICE_BRIDGE,
        nonblocking=False,
        target_room=target,
    )
    assert auth.decision is CrossRoomForkDecision.ALLOWED


@pytest.mark.asyncio
async def test_unpersisted_generated_username_is_not_exempt_from_parent_membership(tmp_path: Path) -> None:
    """An unowned account matching a generated agent username is never exempted from parent-room membership."""
    config, paths = _setup(tmp_path, persist_other_agent=False)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE))
    target = _room(_TARGET, joined=(_AGENT, _OTHER_AGENT, _ALICE))
    client = _client({_SOURCE: source, _TARGET: target})

    # `neon` has no persisted account, so `@mindroom_neon:localhost` is NOT exempted -> DENIED.
    auth_unpersisted = await authorize_cross_room_fork_access(
        client=client,
        config=config,
        runtime_paths=paths,
        hop=_hop(),
        requester_id=_ALICE,
        nonblocking=False,
    )
    assert auth_unpersisted.decision is CrossRoomForkDecision.DENIED

    # When `neon` is persisted under a renamed account, ONLY the persisted account is exempted;
    # the unowned synthetic `@mindroom_neon:localhost` still fails closed!
    state = MatrixState.load(runtime_paths=paths)
    state.add_account("agent_neon", "renamed_neon", "pw", domain="localhost")
    save_matrix_state(state, paths)

    auth_synthetic = await authorize_cross_room_fork_access(
        client=client,
        config=config,
        runtime_paths=paths,
        hop=_hop(),
        requester_id=_ALICE,
        nonblocking=False,
    )
    assert auth_synthetic.decision is CrossRoomForkDecision.DENIED

    target_with_persisted = _room(_TARGET, joined=(_AGENT, "@renamed_neon:localhost", _ALICE))
    client_persisted = _client({_SOURCE: source, _TARGET: target_with_persisted})
    auth_persisted = await authorize_cross_room_fork_access(
        client=client_persisted,
        config=config,
        runtime_paths=paths,
        hop=_hop(),
        requester_id=_ALICE,
        nonblocking=False,
    )
    assert auth_persisted.decision is CrossRoomForkDecision.ALLOWED


@pytest.mark.asyncio
async def test_encrypted_source_to_unencrypted_target_and_unknown_encryption_denied(tmp_path: Path) -> None:
    """Encrypted -> unencrypted and unknown encryption states fail closed."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, encrypted=True)
    target = _room(_TARGET, encrypted=False)
    client = _client({_SOURCE: source, _TARGET: target})

    assert (
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED

    # Unknown target encryption when target room is not cached and state fetch errors.
    client_uncached_target = _client({_SOURCE: _room(_SOURCE, encrypted=False)})
    assert (
        await authorize_cross_room_fork_access(
            client=client_uncached_target,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED


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
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED


@pytest.mark.asyncio
async def test_agent_not_joined_to_parent_room_denied(tmp_path: Path) -> None:
    """Reading agent must be currently joined to parent room."""
    config, paths = _setup(tmp_path)

    # Parent absent from client.rooms and joined_members fails.
    client_missing = _client({_TARGET: _room(_TARGET)})
    assert (
        await authorize_cross_room_fork_access(
            client=client_missing,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED

    # Parent in client.rooms but agent not in joined members.
    client_stale = _client({_SOURCE: _room(_SOURCE, joined=(_ALICE,)), _TARGET: _room(_TARGET)})
    assert (
        await authorize_cross_room_fork_access(
            client=client_stale,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED


@pytest.mark.asyncio
@pytest.mark.parametrize("requester_id", [None, _AGENT, _BOT_ACCOUNT, _BOB])
async def test_requester_validation_and_membership_denied(tmp_path: Path, requester_id: str | None) -> None:
    """Missing, non-human, or unjoined human requesters fail closed."""
    config, paths = _setup(tmp_path)
    client = _client({_SOURCE: _room(_SOURCE, joined=(_AGENT, _ALICE)), _TARGET: _room(_TARGET)})

    assert (
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=requester_id,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED


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
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(sender=_BOB, content={ORIGINAL_SENDER_KEY: _ALICE}),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED

    # Trusted managed relay without ORIGINAL_SENDER_KEY -> DENIED.
    assert (
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(sender=_AGENT),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED

    # Trusted managed relay with joined human ORIGINAL_SENDER_KEY -> ALLOWED.
    assert (
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(sender=_AGENT, content={ORIGINAL_SENDER_KEY: _ALICE}),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.ALLOWED

    # Same-room ancestor edge inside _SOURCE (child_room_id == parent_room_id) does not require old author membership.
    assert (
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(child_room_id=_SOURCE, parent_room_id=_SOURCE, sender=_BOB),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.ALLOWED


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
            await authorize_cross_room_fork_access(
                client=client,
                config=config,
                runtime_paths=paths,
                hop=_hop(),
                requester_id=_ALICE,
                nonblocking=False,
            )
        ).decision is CrossRoomForkDecision.DENIED


@pytest.mark.asyncio
async def test_strict_resolves_incomplete_cache_and_sliding_sync_unseen_invites(tmp_path: Path) -> None:
    """Strict mode refreshes incomplete parent membership and checks room_get_state for unseen sliding-sync invites."""
    config, paths = _setup(tmp_path, sync_mode="sliding")

    # Parent membership incomplete -> strict refreshes joined_members -> ALLOWED.
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
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.ALLOWED

    # Sliding sync target room with unseen invite (invited_member_count=1, invited_users empty):
    # strict reads room_get_state and denies outsider invite!
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
        await authorize_cross_room_fork_access(
            client=client_sliding,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED


@pytest.mark.asyncio
async def test_non_managed_bot_in_target_must_be_in_parent(tmp_path: Path) -> None:
    """Non-managed bots (configured bot_accounts or unknown external relays) are checked as room audience."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE))

    for bot_id in (_BOT_ACCOUNT, "@external_relay:localhost"):
        target_with_bot = _room(_TARGET, joined=(_AGENT, _OTHER_AGENT, _ALICE, bot_id))
        client = _client({_SOURCE: source, _TARGET: target_with_bot})
        assert (
            await authorize_cross_room_fork_access(
                client=client,
                config=config,
                runtime_paths=paths,
                hop=_hop(),
                requester_id=_ALICE,
                nonblocking=False,
            )
        ).decision is CrossRoomForkDecision.DENIED

        # Once the same non-managed bot is also joined in source, the hop is ALLOWED.
        source_with_bot = _room(_SOURCE, joined=(_AGENT, _ALICE, bot_id))
        client_both = _client({_SOURCE: source_with_bot, _TARGET: target_with_bot})
        assert (
            await authorize_cross_room_fork_access(
                client=client_both,
                config=config,
                runtime_paths=paths,
                hop=_hop(),
                requester_id=_ALICE,
                nonblocking=False,
            )
        ).decision is CrossRoomForkDecision.ALLOWED


@pytest.mark.asyncio
async def test_sliding_sync_reads_destination_history_visibility_authoritatively(
    tmp_path: Path,
) -> None:
    """Strict mode reads authoritative destination history_visibility instead of trusting sliding-sync cache defaults."""
    config, paths = _setup(tmp_path, sync_mode="sliding")
    # Cached rooms both report nio's default "shared", yet _TARGET is world_readable on server.
    source = _room(_SOURCE, joined=(_AGENT, _ALICE), members_synced=True)
    target = _room(_TARGET, joined=(_AGENT, _ALICE), members_synced=True)
    client = _client({_SOURCE: source, _TARGET: target})

    async def server_visibility(
        room_id: str,
        event_type: str,
        state_key: str = "",
    ) -> nio.RoomGetStateEventResponse | nio.RoomGetStateEventError:
        if event_type == "m.room.history_visibility" and room_id == _TARGET:
            return nio.RoomGetStateEventResponse(
                {"history_visibility": "world_readable"},
                event_type,
                state_key,
                room_id,
            )
        return nio.RoomGetStateEventError("forbidden")

    client.room_get_state_event = AsyncMock(side_effect=server_visibility)

    # Strict reads authoritative state -> world_readable DENIED!
    assert (
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED

    # M_NOT_FOUND on destination defaults to Matrix's "shared" -> ALLOWED in strict mode.
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError("not found", status_code="M_NOT_FOUND"),
    )
    assert (
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.ALLOWED

    # Forbidden/error reading visibility fails closed -> DENIED.
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventError("forbidden", status_code="M_FORBIDDEN"),
    )
    assert (
        await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED


@pytest.mark.asyncio
async def test_network_errors_on_state_and_membership_calls_fail_closed(tmp_path: Path) -> None:
    """Network exceptions from room_get_state, room_get_state_event, and joined_members fail closed as DENIED."""
    config, paths = _setup(tmp_path, sync_mode="sliding")

    # 1) room_get_state_event raises -> DENIED (never crashes turn).
    client_state_event_err = _client(
        {_SOURCE: _room(_SOURCE, joined=(_AGENT, _ALICE)), _TARGET: _room(_TARGET, joined=(_AGENT, _ALICE))},
    )
    client_state_event_err.room_get_state_event = AsyncMock(side_effect=OSError("connection reset"))
    assert (
        await authorize_cross_room_fork_access(
            client=client_state_event_err,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED

    # 2) room_get_state raises (parent visibility or unseen invites fallback) -> DENIED.
    target_unseen = _room(_TARGET, joined=(_AGENT, _ALICE), members_synced=False, invited_member_count=1)
    client_state_err = _client({_SOURCE: _room(_SOURCE, joined=(_AGENT, _ALICE)), _TARGET: target_unseen})
    client_state_err.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse(
            room_id=_TARGET,
            members=[nio.RoomMember(_AGENT, "General", None), nio.RoomMember(_ALICE, "Alice", None)],
        ),
    )
    client_state_err.room_get_state = AsyncMock(side_effect=OSError("timeout"))
    assert (
        await authorize_cross_room_fork_access(
            client=client_state_err,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED

    # 3) joined_members raises when parent room is not cached -> DENIED.
    client_members_err = _client({_TARGET: _room(_TARGET, joined=(_AGENT, _ALICE))})
    client_members_err.joined_members = AsyncMock(side_effect=OSError("dns failed"))
    assert (
        await authorize_cross_room_fork_access(
            client=client_members_err,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    ).decision is CrossRoomForkDecision.DENIED


@pytest.mark.asyncio
async def test_output_room_checked_independently_from_fork_target_room(tmp_path: Path) -> None:
    """When output_room_id differs from hop.target_room_id, encryption, visibility, and audience check output_room_id."""
    config, paths = _setup(tmp_path)
    output_room_id = "!output-c:localhost"
    source = _room(_SOURCE, joined=(_AGENT, _ALICE), encrypted=True)
    target_b = _room(_TARGET, joined=(_AGENT, _ALICE), encrypted=True)

    # Unencrypted output room C -> DENIED even though target B is encrypted.
    client_unenc = _client(
        {
            _SOURCE: source,
            _TARGET: target_b,
            output_room_id: _room(output_room_id, joined=(_AGENT, _ALICE), encrypted=False),
        },
    )
    assert (
        await authorize_cross_room_fork_access(
            client=client_unenc,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
            target_room=target_b,
            output_room_id=output_room_id,
            output_room=client_unenc.rooms[output_room_id],
        )
    ).decision is CrossRoomForkDecision.DENIED

    # Output room C has an outsider -> DENIED even though target B only has Alice.
    client_outsider = _client(
        {
            _SOURCE: source,
            _TARGET: target_b,
            output_room_id: _room(output_room_id, joined=(_AGENT, _ALICE, _BOB), encrypted=True),
        },
    )
    assert (
        await authorize_cross_room_fork_access(
            client=client_outsider,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
            target_room=target_b,
            output_room_id=output_room_id,
            output_room=client_outsider.rooms[output_room_id],
        )
    ).decision is CrossRoomForkDecision.DENIED

    # Output room C is world_readable -> DENIED.
    client_world = _client(
        {
            _SOURCE: source,
            _TARGET: target_b,
            output_room_id: _room(
                output_room_id,
                joined=(_AGENT, _ALICE),
                encrypted=True,
                history_visibility="world_readable",
            ),
        },
    )
    assert (
        await authorize_cross_room_fork_access(
            client=client_world,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
            target_room=target_b,
            output_room_id=output_room_id,
            output_room=client_world.rooms[output_room_id],
        )
    ).decision is CrossRoomForkDecision.DENIED

    # Output room C is encrypted, private, and subset audience -> ALLOWED.
    client_ok = _client(
        {
            _SOURCE: source,
            _TARGET: target_b,
            output_room_id: _room(output_room_id, joined=(_AGENT, _ALICE), encrypted=True),
        },
    )
    assert (
        await authorize_cross_room_fork_access(
            client=client_ok,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
            target_room=target_b,
            output_room_id=output_room_id,
            output_room=client_ok.rooms[output_room_id],
        )
    ).decision is CrossRoomForkDecision.ALLOWED


@pytest.mark.asyncio
async def test_parent_shared_visibility_makes_zero_read_room_history_calls(tmp_path: Path) -> None:
    """Common-case parent rooms with default or pre-message 'shared' visibility never call read_room_history."""
    config, paths = _setup(tmp_path)
    client = _client(
        {
            _SOURCE: _room(_SOURCE, joined=(_AGENT, _ALICE), history_visibility="shared"),
            _TARGET: _room(_TARGET, joined=(_AGENT, _ALICE), history_visibility="shared"),
        },
    )
    with patch("mindroom.matrix.thread_fork_access.read_room_history") as mock_read_history:
        auth = await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
    assert auth.decision is CrossRoomForkDecision.ALLOWED
    assert auth.allow_segment is None
    mock_read_history.assert_not_called()

    # Explicit m.room.history_visibility='shared' set before the message also skips read_room_history.
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            room_id=_SOURCE,
            events=[
                {
                    "type": "m.room.history_visibility",
                    "state_key": "",
                    "origin_server_ts": 100,
                    "content": {"history_visibility": "shared"},
                },
            ],
        ),
    )
    with patch("mindroom.matrix.thread_fork_access.read_room_history") as mock_read_history:
        auth_explicit = await authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=_hop(),
            requester_id=_ALICE,
            nonblocking=False,
        )
        assert auth_explicit.decision is CrossRoomForkDecision.ALLOWED
        assert auth_explicit.allow_segment is not None
        segment_decision = await auth_explicit.allow_segment(
            [make_visible_message(event_id="$p1", sender=_ALICE, timestamp=1500, body="after shared")],
        )
    assert segment_decision is CrossRoomForkDecision.ALLOWED
    mock_read_history.assert_not_called()


@pytest.mark.asyncio
async def test_parent_visibility_relaxed_from_joined_to_shared_after_message_denies_late_joiner(
    tmp_path: Path,
) -> None:
    """Switching a parent room from 'joined' to 'shared' after a message still blocks members who joined later."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE, _BOB), history_visibility="shared")
    target = _room(_TARGET, joined=(_AGENT, _ALICE, _BOB), history_visibility="shared")
    client = _client({_SOURCE: source, _TARGET: target})

    fork_root = make_visible_message(
        event_id="$fork",
        sender=_ALICE,
        timestamp=3000,
        content={
            "body": "forked",
            FORK_CONTENT_KEY: {"room_id": _SOURCE, "thread_id": "$p1", "event_id": "$p2"},
        },
    )
    parent_messages = [
        make_visible_message(event_id="$p1", sender=_ALICE, timestamp=1500, body="sent while joined"),
        make_visible_message(event_id="$p2", thread_id="$p1", sender=_BOB, timestamp=2500, body="after shared"),
    ]

    async def read_thread(_room_id: str, _thread_id: str) -> object:
        return thread_history_result(parent_messages, is_full_history=True)

    # Visibility changed from joined -> shared at ts=1800 (after $p1 at ts=1500); Bob joined at ts=2000 -> DENIED.
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            room_id=_SOURCE,
            events=[
                {
                    "type": "m.room.history_visibility",
                    "state_key": "",
                    "origin_server_ts": 1800,
                    "content": {"history_visibility": "shared"},
                },
                _member_state_event(_ALICE, membership="join", origin_server_ts=1000),
                _member_state_event(_BOB, membership="join", origin_server_ts=2000),
            ],
        ),
    )
    denied = await with_fork_prefix(
        thread_history_result([fork_root], is_full_history=True),
        room_id=_TARGET,
        thread_id="$fork",
        read_thread=read_thread,  # type: ignore[arg-type]
        authorize_cross_room=lambda hop: authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=hop,
            requester_id=_ALICE,
            nonblocking=False,
        ),
        logger=MagicMock(),
    )
    assert [m.event_id for m in denied] == ["$fork"]
    assert denied.is_full_history is True
    assert not is_thread_history_degraded(denied)

    # If Bob was already joined at ts=1500 (at or before $p1), the same relaxed-visibility hop is ALLOWED.
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            room_id=_SOURCE,
            events=[
                {
                    "type": "m.room.history_visibility",
                    "state_key": "",
                    "origin_server_ts": 1800,
                    "content": {"history_visibility": "shared"},
                },
                _member_state_event(_ALICE, membership="join", origin_server_ts=1000),
                _member_state_event(_BOB, membership="join", origin_server_ts=1500),
            ],
        ),
    )
    allowed = await with_fork_prefix(
        thread_history_result([fork_root], is_full_history=True),
        room_id=_TARGET,
        thread_id="$fork",
        read_thread=read_thread,  # type: ignore[arg-type]
        authorize_cross_room=lambda hop: authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=hop,
            requester_id=_ALICE,
            nonblocking=False,
        ),
        logger=MagicMock(),
    )
    assert [m.event_id for m in allowed] == ["$p1", "$p2", "$fork"]
    assert allowed.is_full_history is True
    assert not is_thread_history_degraded(allowed)

    # A 'shared' visibility state event missing origin_server_ts fails closed as DENIED.
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            room_id=_SOURCE,
            events=[
                {
                    "type": "m.room.history_visibility",
                    "state_key": "",
                    "content": {"history_visibility": "shared"},
                },
            ],
        ),
    )
    missing_ts = await authorize_cross_room_fork_access(
        client=client,
        config=config,
        runtime_paths=paths,
        hop=_hop(),
        requester_id=_ALICE,
        nonblocking=False,
    )
    assert missing_ts.decision is CrossRoomForkDecision.DENIED


@pytest.mark.asyncio
async def test_parent_invited_visibility_allows_member_invited_before_thread_start(tmp_path: Path) -> None:
    """An 'invited' personal parent room passes when the member was invited before the thread started."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE), history_visibility="invited")
    target = _room(_TARGET, joined=(_AGENT, _ALICE), history_visibility="shared")
    client = _client({_SOURCE: source, _TARGET: target})

    # Alice was invited at ts=1000, joined at ts=2000; parent thread starts at ts=1500.
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            room_id=_SOURCE,
            events=[
                {
                    "type": "m.room.history_visibility",
                    "state_key": "",
                    "origin_server_ts": 500,
                    "content": {"history_visibility": "invited"},
                },
                _member_state_event(_ALICE, membership="join", origin_server_ts=2000, replaces_state="$alice-invite"),
            ],
        ),
    )
    invite_source = {
        "event_id": "$alice-invite",
        **_member_state_event(_ALICE, membership="invite", origin_server_ts=1000),
    }
    client.room_get_event = AsyncMock(return_value=nio.RoomGetEventResponse.from_dict(invite_source))

    fork_root = make_visible_message(
        event_id="$fork",
        sender=_ALICE,
        timestamp=3000,
        content={
            "body": "forked",
            FORK_CONTENT_KEY: {"room_id": _SOURCE, "thread_id": "$p1", "event_id": "$p2"},
        },
    )
    parent_messages = [
        make_visible_message(event_id="$p1", sender=_AGENT, timestamp=1500, body="early personal message"),
        make_visible_message(event_id="$p2", thread_id="$p1", sender=_ALICE, timestamp=2500, body="reply"),
    ]

    async def read_thread(_room_id: str, _thread_id: str) -> object:
        return thread_history_result(parent_messages, is_full_history=True)

    result = await with_fork_prefix(
        thread_history_result([fork_root], is_full_history=True),
        room_id=_TARGET,
        thread_id="$fork",
        read_thread=read_thread,  # type: ignore[arg-type]
        authorize_cross_room=lambda hop: authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=hop,
            requester_id=_ALICE,
            nonblocking=False,
        ),
        logger=MagicMock(),
    )
    assert [m.event_id for m in result] == ["$p1", "$p2", "$fork"]
    assert result.is_full_history is True
    assert not is_thread_history_degraded(result)


@pytest.mark.asyncio
async def test_parent_joined_visibility_denies_late_joiner_cleanly_without_degrading(tmp_path: Path) -> None:
    """A 'joined' parent room denies the hop cleanly without degrading when an audience member joined after thread start."""
    config, paths = _setup(tmp_path)
    source = _room(_SOURCE, joined=(_AGENT, _ALICE, _BOB), history_visibility="joined")
    target = _room(_TARGET, joined=(_AGENT, _ALICE, _BOB), history_visibility="shared")
    client = _client({_SOURCE: source, _TARGET: target})

    # Alice joined at ts=1000, Bob joined at ts=2000; parent thread starts at ts=1500.
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            room_id=_SOURCE,
            events=[
                {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "joined"}},
                _member_state_event(_ALICE, membership="join", origin_server_ts=1000),
                _member_state_event(_BOB, membership="join", origin_server_ts=2000),
            ],
        ),
    )

    fork_root = make_visible_message(
        event_id="$fork",
        sender=_ALICE,
        timestamp=3000,
        content={
            "body": "forked",
            FORK_CONTENT_KEY: {"room_id": _SOURCE, "thread_id": "$p1", "event_id": "$p2"},
        },
    )
    parent_messages = [
        make_visible_message(event_id="$p1", sender=_ALICE, timestamp=1500, body="before bob joined"),
        make_visible_message(event_id="$p2", thread_id="$p1", sender=_BOB, timestamp=2500, body="after bob joined"),
    ]

    async def read_thread(_room_id: str, _thread_id: str) -> object:
        return thread_history_result(parent_messages, is_full_history=True)

    denied = await with_fork_prefix(
        thread_history_result([fork_root], is_full_history=True),
        room_id=_TARGET,
        thread_id="$fork",
        read_thread=read_thread,  # type: ignore[arg-type]
        authorize_cross_room=lambda hop: authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=hop,
            requester_id=_ALICE,
            nonblocking=False,
        ),
        logger=MagicMock(),
    )
    assert [m.event_id for m in denied] == ["$fork"]
    assert denied.is_full_history is True
    assert not is_thread_history_degraded(denied)

    # Once Bob joined at or before ts=1500, the same hop succeeds.
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse(
            room_id=_SOURCE,
            events=[
                {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "joined"}},
                _member_state_event(_ALICE, membership="join", origin_server_ts=1000),
                _member_state_event(_BOB, membership="join", origin_server_ts=1500),
            ],
        ),
    )
    allowed = await with_fork_prefix(
        thread_history_result([fork_root], is_full_history=True),
        room_id=_TARGET,
        thread_id="$fork",
        read_thread=read_thread,  # type: ignore[arg-type]
        authorize_cross_room=lambda hop: authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=hop,
            requester_id=_ALICE,
            nonblocking=False,
        ),
        logger=MagicMock(),
    )
    assert [m.event_id for m in allowed] == ["$p1", "$p2", "$fork"]
    assert allowed.is_full_history is True
    assert not is_thread_history_degraded(allowed)

    # Network failure during read_room_history denies cleanly without crashing or degrading.
    client.room_get_state = AsyncMock(
        side_effect=[
            nio.RoomGetStateResponse(
                room_id=_SOURCE,
                events=[
                    {"type": "m.room.history_visibility", "state_key": "", "content": {"history_visibility": "joined"}},
                ],
            ),
            OSError("state read failed"),
        ],
    )
    errored = await with_fork_prefix(
        thread_history_result([fork_root], is_full_history=True),
        room_id=_TARGET,
        thread_id="$fork",
        read_thread=read_thread,  # type: ignore[arg-type]
        authorize_cross_room=lambda hop: authorize_cross_room_fork_access(
            client=client,
            config=config,
            runtime_paths=paths,
            hop=hop,
            requester_id=_ALICE,
            nonblocking=False,
        ),
        logger=MagicMock(),
    )
    assert [m.event_id for m in errored] == ["$fork"]
    assert errored.is_full_history is True
    assert not is_thread_history_degraded(errored)
