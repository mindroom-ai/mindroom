"""Room creation and power-level reconciliation against room version 12, where creators are never listed."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import thread_tags
from mindroom.matrix import client_room_admin
from mindroom.matrix.room_reconciliation import read_room_state

ROUTER = "@router:example.com"
OWNER = "@owner:example.com"
ROOM = "!room:example.com"


def _capabilities(default_version: str) -> MagicMock:
    response = MagicMock(status=200)
    response.json = AsyncMock(return_value={"capabilities": {"m.room_versions": {"default": default_version}}})
    return response


def _creating_client(default_version: str) -> AsyncMock:
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = ROUTER
    client.access_token = "token"  # noqa: S105 - test credential
    client.send.return_value = _capabilities(default_version)
    client.room_create.return_value = nio.RoomCreateResponse(room_id=ROOM)
    return client


@pytest.mark.asyncio
async def test_a_version_12_room_is_created_without_its_creator_in_users() -> None:
    """Version 12 servers reject power levels that list the creator, who already has unlimited power."""
    client = _creating_client("12")

    await client_room_admin.create_room(client, "Personal", admin_users=[OWNER, ROUTER])

    kwargs = client.room_create.await_args.kwargs
    assert kwargs["room_version"] == "12"
    assert kwargs["initial_state"][0]["content"]["users"] == {OWNER: 100}


@pytest.mark.asyncio
async def test_an_older_room_still_lists_its_creator() -> None:
    """Before version 12 the creator needs an explicit admin level like anyone else."""
    client = _creating_client("11")

    await client_room_admin.create_room(client, "Personal", admin_users=[OWNER])

    kwargs = client.room_create.await_args.kwargs
    assert kwargs["room_version"] == "11"
    assert kwargs["initial_state"][0]["content"]["users"] == {OWNER: 100, ROUTER: 100}


def _state_events(*, version: str, sender: str, additional: list[str] | None = None) -> list[dict[str, Any]]:
    create_content: dict[str, Any] = {"room_version": version}
    if additional is not None:
        create_content["additional_creators"] = additional
    return [
        {"type": "m.room.create", "state_key": "", "sender": sender, "content": create_content},
        {
            "type": "m.room.power_levels",
            "state_key": "",
            "sender": sender,
            "content": {"users": {}, "state_default": 50},
        },
    ]


def _room_client(*, version: str, sender: str, additional: list[str] | None = None) -> AsyncMock:
    events = _state_events(version=version, sender=sender, additional=additional)
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = ROUTER
    client.room_get_state.return_value = nio.RoomGetStateResponse(events=events, room_id=ROOM)

    async def get_state_event(room_id: str, event_type: str, state_key: str = "") -> nio.RoomGetStateEventResponse:
        content = next(event["content"] for event in events if event["type"] == event_type)
        return nio.RoomGetStateEventResponse(
            content=dict(content),
            event_type=event_type,
            state_key=state_key,
            room_id=room_id,
        )

    client.room_get_state_event.side_effect = get_state_event
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict({"event_id": "$power"}, room_id=ROOM)
    return client


@pytest.mark.asyncio
async def test_a_state_snapshot_knows_a_version_12_rooms_creators() -> None:
    """The snapshot keeps the creators that only the full create event names."""
    client = _room_client(version="12", sender=OWNER, additional=[ROUTER])

    snapshot = await read_room_state(client, ROOM)

    assert snapshot is not None
    assert snapshot.creators == {OWNER, ROUTER}


@pytest.mark.asyncio
async def test_granting_admin_to_a_version_12_creator_writes_nothing() -> None:
    """A creator already has unlimited power; listing it would be rejected by the server."""
    client = _room_client(version="12", sender=OWNER)

    assert await client_room_admin.ensure_room_admin_power_levels(client, ROOM, {OWNER}) is True
    client.room_put_state.assert_not_awaited()

    assert await client_room_admin.ensure_room_admin_power_levels(client, ROOM, {OWNER, "@guest:example.com"}) is True
    content = client.room_put_state.await_args.kwargs["content"]
    assert content["users"] == {"@guest:example.com": 100}


@pytest.mark.asyncio
async def test_a_version_12_creator_counts_as_room_admin() -> None:
    """Chat-admin checks recognize the creator although power levels do not list it."""
    client = _room_client(version="12", sender=OWNER)

    assert await client_room_admin.room_admin_power_user(client, ROOM, ["@guest:example.com", OWNER]) == OWNER


@pytest.mark.asyncio
async def test_a_version_12_creator_may_write_thread_tags_at_any_state_level() -> None:
    """The thread-tags power check treats the creator's power as unlimited."""
    client = _room_client(version="12", sender=ROUTER)

    await thread_tags._assert_thread_tags_write_allowed(client, ROOM)


@pytest.mark.asyncio
async def test_an_older_creator_without_listed_power_still_cannot_write_thread_tags() -> None:
    """Before version 12 the creator gets no implicit power."""
    client = _room_client(version="11", sender=ROUTER)

    with pytest.raises(thread_tags.ThreadTagsError, match="has 0, requires 50"):
        await thread_tags._assert_thread_tags_write_allowed(client, ROOM)


@pytest.mark.asyncio
async def test_the_first_qualifying_candidate_wins_even_when_it_is_a_creator() -> None:
    """A creator listed first is returned before a later candidate with listed admin power."""
    client = _room_client(version="12", sender=OWNER)
    power = {"users": {"@bridge:example.com": 100}, "state_default": 50}

    async def get_state_event(room_id: str, event_type: str, state_key: str = "") -> nio.RoomGetStateEventResponse:
        content = power if event_type == "m.room.power_levels" else {"room_version": "12"}
        return nio.RoomGetStateEventResponse(
            content=dict(content),
            event_type=event_type,
            state_key=state_key,
            room_id=room_id,
        )

    client.room_get_state_event.side_effect = get_state_event

    assert await client_room_admin.room_admin_power_user(client, ROOM, [OWNER, "@bridge:example.com"]) == OWNER


@pytest.mark.asyncio
async def test_an_unreadable_create_event_fails_the_admin_check_closed() -> None:
    """A transport failure while reading creators denies instead of raising out of the admin check."""
    client = _room_client(version="12", sender=OWNER)
    power_response = nio.RoomGetStateEventResponse(
        content={"users": {}},
        event_type="m.room.power_levels",
        state_key="",
        room_id=ROOM,
    )
    client.room_get_state_event.side_effect = [power_response, TimeoutError("create-event read timed out")]

    assert await client_room_admin.room_admin_power_user(client, ROOM, [OWNER]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["send", "json", "status"])
async def test_an_unreadable_capabilities_probe_falls_back_to_the_server_default(failure: str) -> None:
    """Room creation proceeds without pinning a version when capabilities cannot be read."""
    client = _creating_client("12")
    if failure == "send":
        client.send.side_effect = TimeoutError("capabilities timed out")
    elif failure == "json":
        client.send.return_value.json.side_effect = ValueError("not json")
    else:
        client.send.return_value.status = 404

    assert await client_room_admin.create_room(client, "Personal", admin_users=[OWNER]) == ROOM

    kwargs = client.room_create.await_args.kwargs
    assert "room_version" not in kwargs
    assert kwargs["initial_state"][0]["content"]["users"] == {OWNER: 100, ROUTER: 100}


@pytest.mark.asyncio
@pytest.mark.parametrize("helper", ["managed", "admins"])
async def test_a_snapshot_write_rereads_power_levels_and_still_omits_creators(helper: str) -> None:
    """The fresh re-read keeps grants made since the snapshot, reuses its creators, and never lists them."""
    client = _room_client(version="12", sender=OWNER)
    snapshot = await read_room_state(client, ROOM)
    assert snapshot is not None
    fresh = {"users": {"@later:example.com": 100}, "state_default": 50, "custom": "keep"}
    client.room_get_state_event.reset_mock(side_effect=True)
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content=dict(fresh),
        event_type="m.room.power_levels",
        state_key="",
        room_id=ROOM,
    )
    targets = {OWNER, "@guest:example.com"}

    if helper == "managed":
        assert await client_room_admin.ensure_managed_room_power_levels(client, ROOM, targets, snapshot=snapshot)
    else:
        assert await client_room_admin.ensure_room_admin_power_levels(client, ROOM, targets, snapshot=snapshot)

    content = client.room_put_state.await_args.kwargs["content"]
    assert content["users"] == {"@later:example.com": 100, "@guest:example.com": 100}
    assert content["custom"] == "keep"
    client.room_get_state_event.assert_awaited_once_with(ROOM, "m.room.power_levels", "")
