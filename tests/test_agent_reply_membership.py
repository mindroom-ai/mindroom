"""Tests for authoritative room-membership reply grants."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex, _agent_reply_membership_policy_signature
from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.matrix.state import MatrixState

if TYPE_CHECKING:
    from pathlib import Path

_ROUTER_ID = "@mindroom_router:example.com"
_ALICE_ID = "@alice:example.com"
_BOB_ID = "@bob:example.com"
_READINESS_EVENTS = frozenset({"agent_reply_grant_room_ready", "agent_reply_grant_room_unready"})


def _runtime_config(
    tmp_path: Path,
    *,
    member_rooms: list[str],
    aliases: dict[str, list[str]] | None = None,
    bot_accounts: list[str] | None = None,
) -> tuple[Config, RuntimePaths]:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "https://example.com"},
    )
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {
                "assistant": {
                    "display_name": "Assistant",
                    "role": "Test assistant",
                    "rooms": ["project", "secondary"],
                    "access": ResponderAccessConfig(members_of_rooms=member_rooms),
                },
            },
            "authorization": {
                "aliases": aliases or {},
            },
            "bot_accounts": bot_accounts or [],
            "router": {
                "access": {
                    "current_room_members": False,
                    "members_of_rooms": [],
                    "users": [],
                },
            },
        },
        runtime_paths,
    )
    return config, runtime_paths


def _current_room_runtime_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    """Build membership mode with only current-room conversation access."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "https://example.com"},
    )
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {
                "assistant": {
                    "display_name": "Assistant",
                    "role": "Test assistant",
                    "access": {
                        "current_room_members": True,
                        "members_of_rooms": [],
                    },
                },
            },
            "router": {
                "access": {
                    "current_room_members": False,
                    "members_of_rooms": [],
                    "users": [],
                },
            },
        },
        runtime_paths,
    )
    return config, runtime_paths


def _persist_room(runtime_paths: RuntimePaths, room_key: str, room_id: str) -> None:
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_room(room_key, room_id, f"#{room_key}:example.com", room_key.title())
    state.save(runtime_paths=runtime_paths)


def _joined_members(room_id: str, *user_ids: str) -> nio.JoinedMembersResponse:
    return nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id, None, None) for user_id in user_ids],
        room_id=room_id,
    )


def _member_event(user_id: str, membership: str, *, sender: str | None = None) -> nio.RoomMemberEvent:
    event = nio.RoomMemberEvent.from_dict(
        {
            "type": "m.room.member",
            "event_id": f"${membership}-{user_id}",
            "sender": sender or user_id,
            "state_key": user_id,
            "origin_server_ts": 1,
            "content": {"membership": membership},
            "unsigned": {"prev_content": {"membership": "join"}},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    return event


async def _refresh_while_router_applies(
    index: AgentReplyMembershipIndex,
    config: Config,
    runtime_paths: RuntimePaths,
    answers: dict[str, tuple[str, ...]],
    *,
    transitions: tuple[tuple[str, str, str], ...] = (),
) -> None:
    """Refresh while the router's sync applies (room ID, user ID, membership) transitions before Matrix answers.

    ``answers`` maps each room the router is joined to onto the members Matrix answers for it.
    """
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def joined_members(queried_room_id: str) -> nio.JoinedMembersResponse:
        query_started.set()
        await release_query.wait()
        return _joined_members(queried_room_id, *answers[queried_room_id])

    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=list(answers))
    client.joined_members.side_effect = joined_members
    refresh_task = asyncio.create_task(index.refresh(config, runtime_paths, client))
    await query_started.wait()
    for room_id, user_id, membership in transitions:
        index.apply_member_event(
            config,
            room_id,
            _member_event(user_id, membership),
            control_user_id=_ROUTER_ID,
        )
    release_query.set()
    await refresh_task


@pytest.mark.asyncio
async def test_refresh_canonicalizes_authoritative_joined_members(tmp_path: Path) -> None:
    """Removing alias canonicalization would deny a joined bridged user."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(
        tmp_path,
        member_rooms=["project"],
        aliases={"@alice:example.com": ["@telegram_alice:example.com"]},
    )
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@telegram_alice:example.com")
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)
    assert index.is_allowed("@telegram_alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_configured_bot_alias_cannot_prove_human_grant_room_membership(tmp_path: Path) -> None:
    """A configured bot in a grant room must not impersonate its canonical human alias."""
    room_id = "!project:example.com"
    owner_id = "@owner:example.com"
    bot_id = "@bridgebot:example.com"
    config, runtime_paths = _runtime_config(
        tmp_path,
        member_rooms=["project"],
        aliases={owner_id: [bot_id]},
        bot_accounts=[bot_id],
    )
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, bot_id)
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert not index.is_allowed(owner_id, ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_managed_agent_alias_cannot_prove_human_grant_room_membership(tmp_path: Path) -> None:
    """A managed agent in a grant room must not impersonate its canonical human alias."""
    room_id = "!project:example.com"
    owner_id = "@owner:example.com"
    managed_agent_id = "@mindroom_assistant:example.com"
    config, runtime_paths = _runtime_config(
        tmp_path,
        member_rooms=["project"],
        aliases={owner_id: [managed_agent_id]},
    )
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, managed_agent_id)
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert not index.is_allowed(owner_id, ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_refresh_uses_any_ready_joined_room(tmp_path: Path) -> None:
    """Changing the room combination from any-of to all-of would deny a valid grant."""
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project", "secondary"])
    _persist_room(runtime_paths, "project", "!project:example.com")
    _persist_room(runtime_paths, "secondary", "!secondary:example.com")
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(
        rooms=["!project:example.com", "!secondary:example.com"],
    )
    client.joined_members.side_effect = [
        _joined_members("!project:example.com"),
        _joined_members("!secondary:example.com", "@alice:example.com"),
    ]
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert index.is_allowed("@alice:example.com", ["project", "secondary"], config, runtime_paths)


@pytest.mark.asyncio
async def test_ready_room_can_grant_while_another_room_awaits_retry(tmp_path: Path) -> None:
    """One failed grant room must not revoke an independently authoritative any-of grant."""
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project", "secondary"])
    _persist_room(runtime_paths, "project", "!project:example.com")
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!project:example.com"])
    client.joined_members.return_value = _joined_members("!project:example.com", "@alice:example.com")
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert index.needs_refresh(config)
    assert index.is_allowed("@alice:example.com", ["project", "secondary"], config, runtime_paths)


@pytest.mark.asyncio
async def test_unresolved_grant_room_fails_closed(tmp_path: Path) -> None:
    """Authorizing an unresolved display name would violate stable room-ID resolution."""
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!project:example.com"])
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_not_joined_to_grant_room_fails_closed(tmp_path: Path) -> None:
    """A persisted room ID must not grant when the control-plane client has departed."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[])
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_joined_members_snapshot_fails_closed(tmp_path: Path) -> None:
    """A failed authoritative snapshot must not reuse or invent membership."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersError(
        "M_FORBIDDEN",
        "forbidden",
        room_id=room_id,
    )
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["invite", "leave", "ban"])
async def test_non_join_transition_revokes_ready_member(tmp_path: Path, membership: str) -> None:
    """Treating any non-join membership as active would preserve access after revocation."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.apply_member_event(
        config,
        room_id,
        _member_event("@alice:example.com", membership),
        control_user_id="@mindroom_router:example.com",
    )

    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_join_transition_adds_member_to_ready_snapshot(tmp_path: Path) -> None:
    """Ignoring live joins would require a restart before delegated access works."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id)
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.apply_member_event(
        config,
        room_id,
        _member_event("@alice:example.com", "join"),
        control_user_id="@mindroom_router:example.com",
    )

    assert index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_alias_departure_preserves_other_joined_identity_for_canonical_user(tmp_path: Path) -> None:
    """Leaving through one alias must not revoke a canonical identity that is still joined."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(
        tmp_path,
        member_rooms=["project"],
        aliases={"@alice:example.com": ["@telegram_alice:example.com"]},
    )
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(
        room_id,
        "@alice:example.com",
        "@telegram_alice:example.com",
    )
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.apply_member_event(
        config,
        room_id,
        _member_event("@telegram_alice:example.com", "leave"),
        control_user_id="@mindroom_router:example.com",
    )

    assert index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_control_client_departure_marks_grant_room_unready(tmp_path: Path) -> None:
    """A kicked control client can no longer vouch for remaining room membership."""
    room_id = "!project:example.com"
    router_user_id = "@mindroom_router:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, router_user_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.apply_member_event(
        config,
        room_id,
        _member_event(router_user_id, "leave"),
        control_user_id=router_user_id,
    )

    assert index.needs_refresh(config)
    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_authoritative_control_departure_fences_inflight_initial_snapshot(tmp_path: Path) -> None:
    """A leave-section departure must prevent an older first snapshot from publishing."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        query_started.set()
        await release_query.wait()
        return _joined_members(room_id, "@alice:example.com")

    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.side_effect = joined_members
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="startup")
    refresh_task = asyncio.create_task(index.refresh(config, runtime_paths, client))
    await query_started.wait()

    try:
        assert index.mark_room_unready(
            config,
            runtime_paths,
            room_id,
            reason="control_client_departed",
        )
    finally:
        release_query.set()
        await refresh_task

    assert index.needs_refresh(config)
    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_unrelated_room_transition_during_refresh_leaves_grants_ready(tmp_path: Path) -> None:
    """A transition in a room no grant reads must not leave the refreshed grants pending."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        query_started.set()
        await release_query.wait()
        return _joined_members(room_id, "@alice:example.com")

    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.side_effect = joined_members
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="reload")
    refresh_task = asyncio.create_task(index.refresh(config, runtime_paths, client))
    await query_started.wait()

    index.apply_member_event(
        config,
        "!unrelated:example.com",
        _member_event("@bob:example.com", "leave"),
        control_user_id="@mindroom_router:example.com",
    )
    release_query.set()
    await refresh_task

    assert index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)
    assert not index.needs_refresh(config)


def test_transition_cannot_make_unready_room_authoritative(tmp_path: Path) -> None:
    """A single live join must not replace the missing full membership baseline."""
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    index = AgentReplyMembershipIndex()

    index.apply_member_event(
        config,
        "!project:example.com",
        _member_event("@alice:example.com", "join"),
        control_user_id="@mindroom_router:example.com",
    )

    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_room_grant_policy_change_invalidates_old_snapshot(tmp_path: Path) -> None:
    """An old ready snapshot must not authorize under a changed grant-room policy."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)
    changed_config, _ = _runtime_config(
        tmp_path,
        member_rooms=["secondary"],
    )

    assert not index.is_allowed("@alice:example.com", ["secondary"], changed_config, runtime_paths)


@pytest.mark.asyncio
async def test_alias_policy_change_reuses_raw_membership_snapshot(tmp_path: Path) -> None:
    """Alias edits should take effect at lookup without rebuilding Matrix membership."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(
        tmp_path,
        member_rooms=["project"],
        aliases={"@alice:example.com": ["@bridge:example.com"]},
    )
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@bridge:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)
    changed_config, _ = _runtime_config(
        tmp_path,
        member_rooms=["project"],
        aliases={"@bob:example.com": ["@bridge:example.com"]},
    )

    assert not index.needs_refresh(changed_config)
    assert not index.is_allowed("@alice:example.com", ["project"], changed_config, runtime_paths)
    assert index.is_allowed("@bob:example.com", ["project"], changed_config, runtime_paths)


@pytest.mark.asyncio
async def test_refresh_adopts_changed_policy_without_external_invalidation(tmp_path: Path) -> None:
    """A direct config replacement must not leave refresh permanently fail closed."""
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    changed_config, _ = _runtime_config(tmp_path, member_rooms=["secondary"])
    _persist_room(runtime_paths, "project", "!project:example.com")
    _persist_room(runtime_paths, "secondary", "!secondary:example.com")
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(
        rooms=["!project:example.com", "!secondary:example.com"],
    )
    client.joined_members.side_effect = [
        _joined_members("!project:example.com", "@alice:example.com"),
        _joined_members("!secondary:example.com", "@bob:example.com"),
    ]
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    await index.refresh(changed_config, runtime_paths, client)

    assert not index.needs_refresh(changed_config)
    assert index.is_allowed("@bob:example.com", ["secondary"], changed_config, runtime_paths)
    assert not index.is_allowed("@alice:example.com", ["secondary"], changed_config, runtime_paths)


@pytest.mark.asyncio
async def test_invalidation_revokes_ready_members_until_refresh(tmp_path: Path) -> None:
    """A router reconnect must not retain grants from an uncertain prior connection."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.invalidate(config, reason="sync_restart")

    assert index.needs_refresh(config)
    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


def test_current_room_only_invalidation_requests_authoritative_refresh(tmp_path: Path) -> None:
    """Invalidating a current-room-only policy must not leave an empty snapshot clean."""
    config, _runtime_paths = _current_room_runtime_config(tmp_path)
    index = AgentReplyMembershipIndex()

    index.invalidate(config, reason="sync_restart")

    assert index.needs_refresh(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["live", "unready"])
@pytest.mark.parametrize("named_room", [False, True])
async def test_new_current_room_transition_requests_authoritative_refresh(
    tmp_path: Path,
    transition: str,
    named_room: bool,
) -> None:
    """A newly joined room absent from the snapshot must become fail-closed refresh work."""
    config, runtime_paths = _current_room_runtime_config(tmp_path)
    room_id = "!new-room:example.com"
    control_user_id = "@mindroom_router:example.com"
    if named_room:
        config.agents["assistant"].rooms = ["project"]
        config.router.access = ResponderAccessConfig(current_room_members=False, members_of_rooms=["project"])
        _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[])
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)
    assert index.needs_refresh(config) is named_room
    assert not index.grants_pending(config, joined_rooms=(), current_room_id=room_id)

    if transition == "live":
        with capture_logs() as logs:
            changed = index.apply_member_event(
                config,
                room_id,
                _member_event(control_user_id, "join"),
                control_user_id=control_user_id,
            )
        # A room outside every grant room is logged as a new current room; the named room's unready row is unchanged.
        assert [
            (log["room_key"], log["room_id"])
            for log in logs
            if log["event"] == "agent_reply_grant_room_membership_transition"
        ] == ([] if named_room else [(None, room_id)])
    else:
        changed = index.mark_room_unready(config, runtime_paths, room_id, reason="recovered_membership")

    assert changed
    assert index.needs_refresh(config)
    assert not index.is_current_room_member(control_user_id, room_id, config, runtime_paths)
    assert index.grants_pending(config, joined_rooms=(), current_room_id=room_id)
    assert not index.grants_pending(config, joined_rooms=(), current_room_id="!still-absent:example.com")


@pytest.mark.asyncio
async def test_current_room_refresh_reuses_ready_room_snapshots(tmp_path: Path) -> None:
    """One newly relevant room must not re-query every ready current-room snapshot."""
    config, runtime_paths = _current_room_runtime_config(tmp_path)
    first_room_id = "!first:example.com"
    second_room_id = "!second:example.com"
    new_room_id = "!new:example.com"
    control_user_id = "@mindroom_router:example.com"
    client = AsyncMock()
    client.joined_rooms.side_effect = [
        nio.JoinedRoomsResponse(rooms=[first_room_id, second_room_id]),
        nio.JoinedRoomsResponse(rooms=[first_room_id, second_room_id, new_room_id]),
    ]
    client.joined_members.side_effect = [
        _joined_members(first_room_id, "@alice:example.com"),
        _joined_members(second_room_id, "@bob:example.com"),
        _joined_members(new_room_id, control_user_id),
    ]
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.apply_member_event(
        config,
        new_room_id,
        _member_event(control_user_id, "join"),
        control_user_id=control_user_id,
    )
    await index.refresh(config, runtime_paths, client)

    assert [call.args[0] for call in client.joined_members.await_args_list] == [
        first_room_id,
        second_room_id,
        new_room_id,
    ]
    assert index.is_current_room_member("@alice:example.com", first_room_id, config, runtime_paths)
    assert index.is_current_room_member("@bob:example.com", second_room_id, config, runtime_paths)
    assert index.is_current_room_member(control_user_id, new_room_id, config, runtime_paths)


@pytest.mark.asyncio
async def test_control_departure_marks_current_room_snapshot_unready(tmp_path: Path) -> None:
    """Current-room entries without managed keys must fail closed on control departure."""
    config, runtime_paths = _current_room_runtime_config(tmp_path)
    room_id = "!current:example.com"
    control_user_id = "@mindroom_router:example.com"
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, control_user_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)
    assert index.is_current_room_member("@alice:example.com", room_id, config, runtime_paths)

    changed = index.mark_room_unready(
        config,
        runtime_paths,
        room_id,
        reason="control_client_departed",
    )

    assert changed
    assert index.needs_refresh(config)
    assert not index.is_current_room_member("@alice:example.com", room_id, config, runtime_paths)


@pytest.mark.asyncio
async def test_invalidation_during_refresh_prevents_stale_publication(tmp_path: Path) -> None:
    """A late pre-reconnect snapshot must not restore revoked grants."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    first_query_started = asyncio.Event()
    release_first_query = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        if not first_query_started.is_set():
            first_query_started.set()
            await release_first_query.wait()
            return _joined_members(room_id, "@alice:example.com")
        return _joined_members(room_id)

    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.side_effect = joined_members
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="startup")
    refresh_task = asyncio.create_task(index.refresh(config, runtime_paths, client))
    await first_query_started.wait()

    index.invalidate(config, reason="reconnect")
    release_first_query.set()
    await refresh_task

    assert client.joined_members.await_count == 1
    assert index.needs_refresh(config)
    assert not index.is_allowed("@alice:example.com", ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_live_departure_during_refresh_revokes_newly_resolved_room_grant(tmp_path: Path) -> None:
    """A departure during first resolution must not survive in the roster Matrix may have answered before it."""
    room_id = "!project:example.com"
    empty_config, runtime_paths = _runtime_config(tmp_path, member_rooms=[])
    config, _ = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    index = AgentReplyMembershipIndex()
    index.invalidate(empty_config, reason="startup")
    index.invalidate(config, reason="config_reload")

    await _refresh_while_router_applies(
        index,
        config,
        runtime_paths,
        {room_id: (_ALICE_ID,)},
        transitions=((room_id, _ALICE_ID, "leave"),),
    )

    assert not index.needs_refresh(config)
    assert not index.is_allowed(_ALICE_ID, ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_old_policy_refresh_cannot_publish_after_policy_replacement(tmp_path: Path) -> None:
    """A refresh for an old config must not overwrite a newer fail-closed policy."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        query_started.set()
        await release_query.wait()
        return _joined_members(room_id, "@alice:example.com")

    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.side_effect = joined_members
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="startup")
    refresh_task = asyncio.create_task(index.refresh(config, runtime_paths, client))
    await query_started.wait()

    changed_config, _ = _runtime_config(tmp_path, member_rooms=["secondary"])
    index.invalidate(changed_config, reason="config_reload")
    release_query.set()
    await refresh_task

    assert index.snapshot.policy_signature == _agent_reply_membership_policy_signature(changed_config)
    assert index.needs_refresh(changed_config)
    assert not index.is_allowed("@alice:example.com", ["secondary"], changed_config, runtime_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("other_names_room", [False, True])
async def test_current_room_absence_is_independent_of_other_named_grants(
    tmp_path: Path,
    other_names_room: bool,
) -> None:
    """Another responder's unready named row cannot override proven current-room absence."""
    config, paths = _current_room_runtime_config(tmp_path)
    config.agents["assistant"].rooms = ["project"]
    config.router.access = ResponderAccessConfig(
        current_room_members=False,
        members_of_rooms=["project"] if other_names_room else [],
    )
    room_id = "!project:example.com"
    _persist_room(paths, "project", room_id)
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[])
    index = AgentReplyMembershipIndex()
    await index.refresh(config, paths, client)

    assert not index.grants_pending(config, joined_rooms=(), current_room_id=room_id)
    if other_names_room:
        assert index.grants_pending(config, joined_rooms=["project"], current_room_id=None)

    # Presence with an unresolved membership query remains uncertain.
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = nio.JoinedMembersError("unavailable", "M_UNKNOWN")
    await index.refresh(config, paths, client)
    assert index.grants_pending(config, joined_rooms=(), current_room_id=room_id)

    # Unknown joined-room discovery cannot prove absence.
    client.joined_rooms.return_value = nio.JoinedRoomsError("unavailable", "M_UNKNOWN")
    await index.refresh(config, paths, client)
    assert index.grants_pending(config, joined_rooms=(), current_room_id=room_id)
    assert index.needs_refresh(config)


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_join_during_refresh_does_not_discard_the_refreshed_grant(tmp_path: Path) -> None:
    """Room setup's own joins, which Matrix's answer already holds, reach the router during its refresh and must not void it."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="uncertain_sync_response")

    await _refresh_while_router_applies(
        index,
        config,
        runtime_paths,
        {room_id: (_ROUTER_ID, _ALICE_ID)},
        transitions=((room_id, _ALICE_ID, "join"),),
    )

    assert is_sender_allowed_for_agent_reply_in_room(
        _ALICE_ID,
        "assistant",
        config,
        room_id,
        runtime_paths,
        index,
        require_resolved_membership=True,
    )
    assert not index.needs_refresh(config)


@pytest.mark.asyncio
async def test_grant_room_is_logged_ready_only_when_its_snapshot_is_published(tmp_path: Path) -> None:
    """Readiness logs must match the published grant rooms: none from a voided refresh, and none for current rooms."""
    room_id = "!project:example.com"
    lobby_room_id = "!lobby:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project", "secondary"])
    config.router.access = ResponderAccessConfig(current_room_members=True, members_of_rooms=[])
    _persist_room(runtime_paths, "project", room_id)
    query_started = asyncio.Event()
    release_query = asyncio.Event()

    async def joined_members(queried_room_id: str) -> nio.JoinedMembersResponse:
        query_started.set()
        await release_query.wait()
        return _joined_members(queried_room_id, _ALICE_ID)

    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id, lobby_room_id])
    client.joined_members.side_effect = joined_members
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="startup")
    refresh_task = asyncio.create_task(index.refresh(config, runtime_paths, client))
    await query_started.wait()

    with capture_logs() as superseded_logs:
        index.invalidate(config, reason="uncertain_sync_response")
        release_query.set()
        await refresh_task
    with capture_logs() as published_logs:
        await index.refresh(config, runtime_paths, client)

    assert [log for log in superseded_logs if log["event"] == "agent_reply_grant_room_ready"] == []
    assert [
        (log["event"], log["room_key"], log.get("reason"), log.get("member_count"))
        for log in published_logs
        if log["event"] in _READINESS_EVENTS
    ] == [
        ("agent_reply_grant_room_unready", "secondary", "managed_room_unresolved", None),
        ("agent_reply_grant_room_ready", "project", None, 1),
    ]
    assert index.is_allowed(_ALICE_ID, ["project"], config, runtime_paths)
    assert index.is_current_room_member(_ALICE_ID, lobby_room_id, config, runtime_paths)


@pytest.mark.asyncio
async def test_departure_during_first_refresh_is_not_lost(tmp_path: Path) -> None:
    """A refresh with nothing published yet must still apply a departure Matrix may have answered before."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    index = AgentReplyMembershipIndex()

    await _refresh_while_router_applies(
        index,
        config,
        runtime_paths,
        {room_id: (_ALICE_ID,)},
        transitions=((room_id, _ALICE_ID, "leave"),),
    )

    assert not index.is_allowed(_ALICE_ID, ["project"], config, runtime_paths)


@pytest.mark.asyncio
async def test_transition_that_changes_no_grant_reports_no_change(tmp_path: Path) -> None:
    """Redundant transitions must not start reply-authorization reconciliation."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, _ALICE_ID)
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    for user_id, membership in ((_ALICE_ID, "join"), (_BOB_ID, "leave")):
        assert not index.apply_member_event(
            config,
            room_id,
            _member_event(user_id, membership),
            control_user_id=_ROUTER_ID,
        )
    assert index.apply_member_event(
        config,
        room_id,
        _member_event(_BOB_ID, "join"),
        control_user_id=_ROUTER_ID,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "next_answer",
    [(_ROUTER_ID, _BOB_ID), (_ROUTER_ID, _BOB_ID, _ALICE_ID)],
    ids=["kicked-before-the-answer", "rejoined-after-the-answer"],
)
async def test_join_the_answer_lacks_waits_for_the_next_refresh(
    tmp_path: Path,
    next_answer: tuple[str, ...],
) -> None:
    """A recorded join Matrix's answer lacks may predate a kick the answer holds or follow the answer, so only the next refresh may grant it."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    index = AgentReplyMembershipIndex()
    await _refresh_while_router_applies(index, config, runtime_paths, {room_id: (_ROUTER_ID, _BOB_ID)})
    index.invalidate(config, reason="sync_restart")

    with capture_logs() as logs:
        await _refresh_while_router_applies(
            index,
            config,
            runtime_paths,
            {room_id: (_ROUTER_ID, _BOB_ID)},
            transitions=((room_id, _ALICE_ID, "join"),),
        )

    assert not index.is_allowed(_ALICE_ID, ["project"], config, runtime_paths)
    assert index.needs_refresh(config)
    assert [(log["event"], log.get("reason")) for log in logs if log["event"] in _READINESS_EVENTS] == [
        ("agent_reply_grant_room_unready", "live_join_unconfirmed"),
    ]

    await _refresh_while_router_applies(index, config, runtime_paths, {room_id: next_answer})

    assert not index.needs_refresh(config)
    assert index.is_allowed(_BOB_ID, ["project"], config, runtime_paths)
    assert index.is_allowed(_ALICE_ID, ["project"], config, runtime_paths) is (_ALICE_ID in next_answer)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memberships", "answer", "alice_allowed", "ready"),
    [
        pytest.param(("join", "leave"), (_ROUTER_ID, _ALICE_ID), False, True, id="join-leave-answer-has-her"),
        pytest.param(("join", "leave"), (_ROUTER_ID,), False, True, id="join-leave-answer-lacks-her"),
        pytest.param(("leave", "join"), (_ROUTER_ID, _ALICE_ID), True, True, id="leave-join-answer-has-her"),
        pytest.param(("leave", "join"), (_ROUTER_ID,), False, False, id="leave-join-answer-lacks-her"),
    ],
)
async def test_one_users_transitions_during_refresh_replay_in_arrival_order(
    tmp_path: Path,
    memberships: tuple[str, str],
    answer: tuple[str, ...],
    alice_allowed: bool,
    ready: bool,
) -> None:
    """Her last recorded transition decides the replay, and a final join still needs Matrix's answer to hold her."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="startup")

    await _refresh_while_router_applies(
        index,
        config,
        runtime_paths,
        {room_id: answer},
        transitions=tuple((room_id, _ALICE_ID, membership) for membership in memberships),
    )

    assert index.is_allowed(_ALICE_ID, ["project"], config, runtime_paths) is alice_allowed
    assert index.needs_refresh(config) is not ready


@pytest.mark.asyncio
async def test_join_the_answer_lacks_keeps_a_ready_rooms_published_grants(tmp_path: Path) -> None:
    """A ready room already holds the router's view of that join, so a refresh it overlaps must not revoke the room."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    index = AgentReplyMembershipIndex()
    await _refresh_while_router_applies(index, config, runtime_paths, {room_id: (_ROUTER_ID, _ALICE_ID)})

    await _refresh_while_router_applies(
        index,
        config,
        runtime_paths,
        {room_id: (_ROUTER_ID, _ALICE_ID)},
        transitions=((room_id, _BOB_ID, "join"),),
    )

    assert not index.needs_refresh(config)
    assert index.is_allowed(_ALICE_ID, ["project"], config, runtime_paths)
    assert index.is_allowed(_BOB_ID, ["project"], config, runtime_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("current_room", [False, True], ids=["grant-room", "current-room"])
async def test_replayed_control_client_departure_logs_its_room_unready(tmp_path: Path, current_room: bool) -> None:
    """A departure the router made itself, which no unready mark voids the refresh for, must still deny and say why."""
    room_id = "!project:example.com"
    if current_room:
        config, runtime_paths = _current_room_runtime_config(tmp_path)
    else:
        config, runtime_paths = _runtime_config(tmp_path, member_rooms=["project"])
        _persist_room(runtime_paths, "project", room_id)
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="startup")

    with capture_logs() as logs:
        await _refresh_while_router_applies(
            index,
            config,
            runtime_paths,
            {room_id: (_ROUTER_ID, _ALICE_ID)},
            transitions=((room_id, _ROUTER_ID, "leave"),),
        )

    assert not index.is_allowed(_ALICE_ID, ["project"], config, runtime_paths)
    assert not index.is_current_room_member(_ALICE_ID, room_id, config, runtime_paths)
    assert index.needs_refresh(config)
    assert [(log["event"], log.get("reason")) for log in logs if log["event"] in _READINESS_EVENTS] == [
        ("agent_reply_grant_room_unready", "control_client_departed"),
    ]


@pytest.mark.asyncio
async def test_current_room_replay_checks_each_room_against_its_own_answer(tmp_path: Path) -> None:
    """Current-room rows share no room key, so each must keep its own answer, and a join that answer lacks must not grant."""
    config, runtime_paths = _current_room_runtime_config(tmp_path)
    first_room_id = "!first:example.com"
    second_room_id = "!second:example.com"
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="sync_restart")

    await _refresh_while_router_applies(
        index,
        config,
        runtime_paths,
        {first_room_id: (_ROUTER_ID, _ALICE_ID), second_room_id: (_ROUTER_ID,)},
        transitions=((second_room_id, _BOB_ID, "join"),),
    )

    assert index.is_current_room_member(_ALICE_ID, first_room_id, config, runtime_paths)
    assert not index.is_current_room_member(_BOB_ID, second_room_id, config, runtime_paths)
    assert index.grants_pending(config, joined_rooms=(), current_room_id=second_room_id)
    assert index.needs_refresh(config)


@pytest.mark.asyncio
async def test_room_the_router_joins_during_refresh_stays_pending(tmp_path: Path) -> None:
    """A room the router joins after Matrix listed its rooms may be joined, so its messages must wait instead of being denied."""
    config, runtime_paths = _current_room_runtime_config(tmp_path)
    lobby_room_id = "!lobby:example.com"
    new_room_id = "!new:example.com"
    index = AgentReplyMembershipIndex()
    index.invalidate(config, reason="sync_restart")

    await _refresh_while_router_applies(
        index,
        config,
        runtime_paths,
        {lobby_room_id: (_ROUTER_ID, _ALICE_ID)},
        transitions=((new_room_id, _ROUTER_ID, "join"),),
    )

    assert index.is_current_room_member(_ALICE_ID, lobby_room_id, config, runtime_paths)
    assert index.needs_refresh(config)
    assert index.grants_pending(config, joined_rooms=(), current_room_id=new_room_id)
