"""Tests for authoritative room-membership reply grants."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex, _agent_reply_membership_policy_signature
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.matrix.state import MatrixState

if TYPE_CHECKING:
    from pathlib import Path


def _runtime_config(
    tmp_path: Path,
    *,
    joined_rooms: list[str],
    aliases: dict[str, list[str]] | None = None,
) -> tuple[Config, RuntimePaths]:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "https://example.com"},
    )
    config = Config.validate_with_runtime(
        {
            "agents": {
                "assistant": {
                    "display_name": "Assistant",
                    "role": "Test assistant",
                    "rooms": ["project", "secondary"],
                },
            },
            "authorization": {
                "aliases": aliases or {},
                "agent_reply_permissions": {
                    "assistant": {"joined_rooms": joined_rooms},
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


@pytest.mark.asyncio
async def test_refresh_canonicalizes_authoritative_joined_members(tmp_path: Path) -> None:
    """Removing alias canonicalization would deny a joined bridged user."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(
        tmp_path,
        joined_rooms=["project"],
        aliases={"@alice:example.com": ["@telegram_alice:example.com"]},
    )
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@telegram_alice:example.com")
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert index.is_allowed("@alice:example.com", ["project"], config.authorization)
    assert index.is_allowed("@telegram_alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_refresh_uses_any_ready_joined_room(tmp_path: Path) -> None:
    """Changing the room combination from any-of to all-of would deny a valid grant."""
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project", "secondary"])
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

    assert index.is_allowed("@alice:example.com", ["project", "secondary"], config.authorization)


@pytest.mark.asyncio
async def test_ready_room_can_grant_while_another_room_awaits_retry(tmp_path: Path) -> None:
    """One failed grant room must not revoke an independently authoritative any-of grant."""
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project", "secondary"])
    _persist_room(runtime_paths, "project", "!project:example.com")
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!project:example.com"])
    client.joined_members.return_value = _joined_members("!project:example.com", "@alice:example.com")
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert index.needs_refresh(config.authorization)
    assert index.is_allowed("@alice:example.com", ["project", "secondary"], config.authorization)


@pytest.mark.asyncio
async def test_unresolved_grant_room_fails_closed(tmp_path: Path) -> None:
    """Authorizing an unresolved display name would violate stable room-ID resolution."""
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!project:example.com"])
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_router_not_joined_to_grant_room_fails_closed(tmp_path: Path) -> None:
    """A persisted room ID must not grant when the control-plane client has departed."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[])
    index = AgentReplyMembershipIndex()

    await index.refresh(config, runtime_paths, client)

    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_joined_members_snapshot_fails_closed(tmp_path: Path) -> None:
    """A failed authoritative snapshot must not reuse or invent membership."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
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

    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["invite", "leave", "ban"])
async def test_non_join_transition_revokes_ready_member(tmp_path: Path, membership: str) -> None:
    """Treating any non-join membership as active would preserve access after revocation."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.apply_member_event(
        config,
        runtime_paths,
        room_id,
        _member_event("@alice:example.com", membership),
        control_user_id="@mindroom_router:example.com",
    )

    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_join_transition_adds_member_to_ready_snapshot(tmp_path: Path) -> None:
    """Ignoring live joins would require a restart before delegated access works."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id)
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.apply_member_event(
        config,
        runtime_paths,
        room_id,
        _member_event("@alice:example.com", "join"),
        control_user_id="@mindroom_router:example.com",
    )

    assert index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_alias_departure_preserves_other_joined_identity_for_canonical_user(tmp_path: Path) -> None:
    """Leaving through one alias must not revoke a canonical identity that is still joined."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(
        tmp_path,
        joined_rooms=["project"],
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
        runtime_paths,
        room_id,
        _member_event("@telegram_alice:example.com", "leave"),
        control_user_id="@mindroom_router:example.com",
    )

    assert index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_control_client_departure_marks_grant_room_unready(tmp_path: Path) -> None:
    """A kicked control client can no longer vouch for remaining room membership."""
    room_id = "!project:example.com"
    router_user_id = "@mindroom_router:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, router_user_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.apply_member_event(
        config,
        runtime_paths,
        room_id,
        _member_event(router_user_id, "leave"),
        control_user_id=router_user_id,
    )

    assert index.needs_refresh(config.authorization)
    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_authoritative_control_departure_fences_inflight_initial_snapshot(tmp_path: Path) -> None:
    """A leave-section departure must prevent an older first snapshot from publishing."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
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
        assert index.mark_control_room_unready(
            config,
            runtime_paths,
            room_id,
            reason="control_client_departed",
        )
    finally:
        release_query.set()
        await refresh_task

    assert index.needs_refresh(config.authorization)
    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_unrelated_membership_event_does_not_fence_inflight_refresh(tmp_path: Path) -> None:
    """Only transitions from configured grant rooms can obsolete their snapshot."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
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
        runtime_paths,
        "!unrelated:example.com",
        _member_event("@bob:example.com", "leave"),
        control_user_id="@mindroom_router:example.com",
    )
    release_query.set()
    await refresh_task

    assert index.is_allowed("@alice:example.com", ["project"], config.authorization)


def test_transition_cannot_make_unready_room_authoritative(tmp_path: Path) -> None:
    """A single live join must not replace the missing full membership baseline."""
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    index = AgentReplyMembershipIndex()

    index.apply_member_event(
        config,
        runtime_paths,
        "!project:example.com",
        _member_event("@alice:example.com", "join"),
        control_user_id="@mindroom_router:example.com",
    )

    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_policy_signature_change_invalidates_old_snapshot(tmp_path: Path) -> None:
    """An old ready snapshot must not authorize under changed alias or room policy."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)
    changed_config, _ = _runtime_config(
        tmp_path,
        joined_rooms=["project"],
        aliases={"@bob:example.com": ["@bridge_bob:example.com"]},
    )

    assert not index.is_allowed("@alice:example.com", ["project"], changed_config.authorization)


@pytest.mark.asyncio
async def test_refresh_adopts_changed_policy_without_external_invalidation(tmp_path: Path) -> None:
    """A direct config replacement must not leave refresh permanently fail closed."""
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    changed_config, _ = _runtime_config(tmp_path, joined_rooms=["secondary"])
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

    assert not index.needs_refresh(changed_config.authorization)
    assert index.is_allowed("@bob:example.com", ["secondary"], changed_config.authorization)
    assert not index.is_allowed("@alice:example.com", ["secondary"], changed_config.authorization)


@pytest.mark.asyncio
async def test_invalidation_revokes_ready_members_until_refresh(tmp_path: Path) -> None:
    """A router reconnect must not retain grants from an uncertain prior connection."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
    _persist_room(runtime_paths, "project", room_id)
    client = AsyncMock()
    client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[room_id])
    client.joined_members.return_value = _joined_members(room_id, "@alice:example.com")
    index = AgentReplyMembershipIndex()
    await index.refresh(config, runtime_paths, client)

    index.invalidate(config, reason="sync_restart")

    assert index.needs_refresh(config.authorization)
    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_invalidation_during_refresh_prevents_stale_publication(tmp_path: Path) -> None:
    """A late pre-reconnect snapshot must not restore revoked grants."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
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
    assert index.needs_refresh(config.authorization)
    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_live_transition_fences_refresh_for_newly_resolved_room(tmp_path: Path) -> None:
    """A transition during first resolution must fence the in-flight membership roster."""
    room_id = "!project:example.com"
    empty_config, runtime_paths = _runtime_config(tmp_path, joined_rooms=[])
    config, _ = _runtime_config(tmp_path, joined_rooms=["project"])
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
    index.invalidate(empty_config, reason="startup")
    index.invalidate(config, reason="config_reload")
    refresh_task = asyncio.create_task(index.refresh(config, runtime_paths, client))
    await query_started.wait()

    index.apply_member_event(
        config,
        runtime_paths,
        room_id,
        _member_event("@alice:example.com", "leave"),
        control_user_id="@mindroom_router:example.com",
    )
    release_query.set()
    await refresh_task

    assert client.joined_members.await_count == 1
    assert index.needs_refresh(config.authorization)
    assert not index.is_allowed("@alice:example.com", ["project"], config.authorization)


@pytest.mark.asyncio
async def test_old_policy_refresh_cannot_publish_after_policy_replacement(tmp_path: Path) -> None:
    """A refresh for an old config must not overwrite a newer fail-closed policy."""
    room_id = "!project:example.com"
    config, runtime_paths = _runtime_config(tmp_path, joined_rooms=["project"])
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

    changed_config, _ = _runtime_config(tmp_path, joined_rooms=["secondary"])
    index.invalidate(changed_config, reason="config_reload")
    release_query.set()
    await refresh_task

    assert index.snapshot.policy_signature == _agent_reply_membership_policy_signature(changed_config.authorization)
    assert index.needs_refresh(changed_config.authorization)
    assert not index.is_allowed("@alice:example.com", ["secondary"], changed_config.authorization)
