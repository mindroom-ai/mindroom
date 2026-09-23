"""Application membership lookup reuse and invalidation without projection writes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import nio
import pytest
from nio.durable import RecordKind, SyncBatch, SyncRecord

from mindroom.authorization import cached_joined_member_ids, ensure_room_membership_synced
from mindroom.event_journal import EventJournalStore
from mindroom.matrix.durable_ingestion import consume_one_ingestion_batch
from mindroom.matrix.member_display_names import room_member_display_names
from mindroom.matrix.room_membership import invalidate_membership_lookups, room_membership_is_complete
from tests.test_durable_ingestion_admission import ACCOUNT, ROOM, Session, principal_for

if TYPE_CHECKING:
    from pathlib import Path

PEER = "@peer:example.org"


@pytest.mark.asyncio
async def test_lookup_reuses_application_members_without_rewriting_projection() -> None:
    """Concurrent and repeated turns share one lookup; nio retains its own members."""
    room = nio.MatrixRoom(ROOM, ACCOUNT)
    room.add_member(ACCOUNT, "Bot", None)
    room.add_member("@stale:example.org", "Stale", None)
    client = AsyncMock(spec=nio.AsyncClient)
    requests: list[str] = []

    async def joined_members(room_id: str) -> nio.JoinedMembersResponse:
        requests.append(room_id)
        await asyncio.sleep(0)
        return nio.JoinedMembersResponse(
            [nio.RoomMember(ACCOUNT, "Bot", None), nio.RoomMember(PEER, "Peer", None)],
            ROOM,
        )

    client.joined_members.side_effect = joined_members
    assert all(await asyncio.gather(*(ensure_room_membership_synced(client, room, sender_id=PEER) for _ in range(4))))
    assert await ensure_room_membership_synced(client, room, sender_id=PEER)
    assert requests == [ROOM]
    assert set(room.users) == {ACCOUNT, "@stale:example.org"}
    assert not room.members_synced
    assert cached_joined_member_ids(room) == {ACCOUNT, PEER}
    assert room_member_display_names(room) == {ACCOUNT: "Bot", PEER: "Peer"}


@pytest.mark.asyncio
async def test_projection_change_requires_a_new_membership_lookup() -> None:
    """A new producer membership invalidates the application lookup before reuse."""
    room = nio.MatrixRoom(ROOM, ACCOUNT)
    room.add_member(ACCOUNT, "Bot", None)
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_members.side_effect = [
        nio.JoinedMembersResponse([nio.RoomMember(ACCOUNT, "Bot", None), nio.RoomMember(PEER, "Peer", None)], ROOM),
        nio.JoinedMembersResponse([nio.RoomMember(ACCOUNT, "Bot", None)], ROOM),
    ]
    assert await ensure_room_membership_synced(client, room, sender_id=PEER)
    room.add_member("@new:example.org", "New", None)
    assert await ensure_room_membership_synced(client, room, sender_id=PEER)
    assert cached_joined_member_ids(room) == {ACCOUNT}
    assert set(room.users) == {ACCOUNT, "@new:example.org"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [RecordKind.STATE, RecordKind.TIMELINE])
async def test_membership_record_invalidates_peer_absent_from_projection(tmp_path: Path, kind: RecordKind) -> None:
    """A partial projection may never have held the departed member being invalidated."""
    room = nio.MatrixRoom(ROOM, ACCOUNT)
    room.add_member(ACCOUNT, "Bot", None)
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_members.side_effect = [
        nio.JoinedMembersResponse([nio.RoomMember(ACCOUNT, "Bot", None), nio.RoomMember(PEER, "Peer", None)], ROOM),
        nio.JoinedMembersResponse([nio.RoomMember(ACCOUNT, "Bot", None)], ROOM),
    ]
    assert await ensure_room_membership_synced(client, room, sender_id=PEER)
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    stream = uuid4()
    principal = await principal_for(store, stream)
    record = SyncRecord(
        kind,
        ROOM,
        {
            "type": "m.room.member",
            "state_key": PEER,
            "sender": PEER,
            "event_id": "$leave",
            "origin_server_ts": 1,
            "content": {"membership": "leave"},
        },
        provenance=nio.TimelineEventProvenance.LIVE,
    )
    try:
        await consume_one_ingestion_batch(Session(SyncBatch(stream, 1, (record,))), principal, account_id=ACCOUNT)
        assert await ensure_room_membership_synced(client, room, sender_id=PEER)
        assert cached_joined_member_ids(room) == {ACCOUNT}
        assert set(room.users) == {ACCOUNT}
        assert not room.members_synced
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_lookup_cannot_install_over_a_changed_projection() -> None:
    """A membership transition during HTTP lookup leaves the partial projection authoritative."""
    room = nio.MatrixRoom(ROOM, ACCOUNT)
    room.add_member(ACCOUNT, "Bot", None)
    client = AsyncMock(spec=nio.AsyncClient)

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        room.add_member("@new:example.org", "New", None)
        return nio.JoinedMembersResponse([nio.RoomMember(PEER, "Peer", None)], ROOM)

    client.joined_members.side_effect = joined_members
    assert not await ensure_room_membership_synced(client, room, sender_id=PEER)
    assert cached_joined_member_ids(room) == {ACCOUNT, "@new:example.org"}
    assert not room.members_synced


@pytest.mark.asyncio
async def test_inflight_lookup_is_rejected_after_membership_invalidation() -> None:
    """A leave for a member absent from nio's projection invalidates an awaited lookup."""
    room = nio.MatrixRoom(ROOM, ACCOUNT)
    room.add_member(ACCOUNT, "Bot", None)
    client = AsyncMock(spec=nio.AsyncClient)
    started = asyncio.Event()
    release = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        started.set()
        await release.wait()
        return nio.JoinedMembersResponse([nio.RoomMember(PEER, "Peer", None)], ROOM)

    client.joined_members.side_effect = joined_members
    lookup = asyncio.create_task(ensure_room_membership_synced(client, room, sender_id=PEER))
    await started.wait()
    invalidate_membership_lookups(
        [
            SyncRecord(
                RecordKind.STATE,
                ROOM,
                {"type": "m.room.member", "state_key": PEER, "content": {"membership": "leave"}},
            ),
        ],
        account_id=ACCOUNT,
    )
    release.set()
    assert not await lookup
    assert cached_joined_member_ids(room) == {ACCOUNT}
    assert not room_membership_is_complete(room)


@pytest.mark.asyncio
async def test_snapshots_are_scoped_to_account_and_room_lifetime() -> None:
    """A transition for one account cannot clear another's lookup or populate a replacement room."""
    room = nio.MatrixRoom(ROOM, ACCOUNT)
    other_room = nio.MatrixRoom(ROOM, "@other:example.org")
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_members.return_value = nio.JoinedMembersResponse([nio.RoomMember(PEER, "Peer", None)], ROOM)
    assert await ensure_room_membership_synced(client, room, sender_id=PEER)
    assert await ensure_room_membership_synced(client, other_room, sender_id=PEER)
    invalidate_membership_lookups([SyncRecord(RecordKind.LOSS, ROOM, {})], account_id=ACCOUNT)
    assert not room_membership_is_complete(room)
    assert room_membership_is_complete(other_room)
    assert cached_joined_member_ids(other_room) == {PEER}
    replacement = nio.MatrixRoom(ROOM, other_room.own_user_id)
    assert not room_membership_is_complete(replacement)
    assert not cached_joined_member_ids(replacement)


@pytest.mark.asyncio
async def test_queued_lookup_can_refresh_after_inflight_lookup_is_invalidated() -> None:
    """A waiting turn must fetch and use fresh members after rejecting an older response."""
    room = nio.MatrixRoom(ROOM, ACCOUNT)
    room.add_member(ACCOUNT, "Bot", None)
    client = AsyncMock(spec=nio.AsyncClient)
    started = asyncio.Event()
    release = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        started.set()
        await release.wait()
        return nio.JoinedMembersResponse([nio.RoomMember(PEER, "Peer", None)], ROOM)

    client.joined_members.side_effect = joined_members
    first = asyncio.create_task(ensure_room_membership_synced(client, room, sender_id=PEER))
    await started.wait()
    second = asyncio.create_task(ensure_room_membership_synced(client, room, sender_id=PEER))
    await asyncio.sleep(0)
    invalidate_membership_lookups([SyncRecord(RecordKind.LOSS, ROOM, {})], account_id=ACCOUNT)
    release.set()
    assert await asyncio.gather(first, second) == [False, True]
    assert client.joined_members.await_count == 2
    assert cached_joined_member_ids(room) == {PEER}
