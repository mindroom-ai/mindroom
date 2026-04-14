"""Tests for Matrix thread tag state helpers."""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.thread_tags import (
    THREAD_TAGS_EVENT_TYPE,
    ThreadTagsError,
    get_thread_tags,
    list_tagged_threads,
    normalize_thread_root_event_id,
    remove_thread_tag,
    set_thread_tag,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable


def _message_event_response(
    event_id: str,
    *,
    content: dict[str, object],
    room_id: str = "!room:localhost",
    event_type: str = "m.room.message",
) -> nio.RoomGetEventResponse:
    return nio.RoomGetEventResponse.from_dict(
        {
            "content": content,
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": room_id,
            "type": event_type,
        },
    )


def _tag_record_content(
    *,
    set_by: str = "@user:localhost",
    set_at: str = "2026-03-21T19:02:03+00:00",
    note: str | None = None,
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    content: dict[str, object] = {
        "set_by": set_by,
        "set_at": set_at,
        "data": data or {},
    }
    if note is not None:
        content["note"] = note
    return content


def _thread_tags_content(**tags: dict[str, object]) -> dict[str, object]:
    return {"tags": tags}


def _thread_tag_state_key(thread_root_id: str, tag: str) -> str:
    return json.dumps([thread_root_id, tag], separators=(",", ":"))


def _thread_tag_state_event(
    thread_root_id: str,
    tag: str,
    *,
    content: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": _thread_tag_state_key(thread_root_id, tag),
        "content": content if content is not None else _tag_record_content(),
    }


def _legacy_thread_tags_event(
    thread_root_id: str,
    *,
    content: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": thread_root_id,
        "content": content if content is not None else _thread_tags_content(resolved=_tag_record_content()),
    }


def _thread_tags_room_state_response(*events: dict[str, object]) -> nio.RoomGetStateResponse:
    return nio.RoomGetStateResponse(
        events=list(events),
        room_id="!room:localhost",
    )


def _thread_tags_room_state_from_current(
    current_events: dict[str, dict[str, object]],
) -> nio.RoomGetStateResponse:
    return _thread_tags_room_state_response(
        *[
            {
                "type": THREAD_TAGS_EVENT_TYPE,
                "state_key": state_key,
                "content": content,
            }
            for state_key, content in current_events.items()
        ],
    )


def _power_levels_response(
    *,
    users: dict[str, int],
    state_default: int = 50,
    users_default: int | None = None,
    events: dict[str, int] | None = None,
) -> nio.RoomGetStateEventResponse:
    content: dict[str, object] = {
        "users": users,
        "state_default": state_default,
    }
    if users_default is not None:
        content["users_default"] = users_default
    if events is not None:
        content["events"] = events
    return nio.RoomGetStateEventResponse(
        content=content,
        event_type="m.room.power_levels",
        state_key="",
        room_id="!room:localhost",
    )


def _thread_tags_state_response(
    thread_root_id: str,
    *,
    content: dict[str, object] | None = None,
) -> nio.RoomGetStateEventResponse:
    return nio.RoomGetStateEventResponse(
        content=content if content is not None else _thread_tags_content(resolved=_tag_record_content()),
        event_type=THREAD_TAGS_EVENT_TYPE,
        state_key=thread_root_id,
        room_id="!room:localhost",
    )


def _thread_tags_state_error(
    *,
    message: str,
    status_code: str,
) -> nio.RoomGetStateEventError:
    return nio.RoomGetStateEventError(
        message,
        status_code=status_code,
        room_id="!room:localhost",
    )


def _joined_members_response(*user_ids: str) -> nio.JoinedMembersResponse:
    return nio.JoinedMembersResponse.from_dict(
        {
            "joined": {
                user_id: {"display_name": user_id.removeprefix("@").split(":", 1)[0].title()} for user_id in user_ids
            },
        },
        room_id="!room:localhost",
    )


def _write_operation(
    client: AsyncMock,
    action: str,
) -> Awaitable[object]:
    if action == "set":
        return set_thread_tag(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "resolved",
            set_by="@alice:localhost",
        )
    return remove_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        requester_user_id="@alice:localhost",
    )


@pytest.mark.asyncio
async def test_set_thread_tag_fetches_fresh_membership_and_power_levels_even_with_synced_cache() -> None:
    """Thread-tag writes should re-check current membership and power levels from Matrix."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    room = nio.MatrixRoom("!room:localhost", "@mindroom_general:localhost")
    room.members_synced = True
    room.users = {
        "@mindroom_general:localhost": object(),
        "@alice:localhost": object(),
    }
    room.power_levels.defaults.state_default = 50
    room.power_levels.users["@mindroom_general:localhost"] = 50
    room.power_levels.users["@alice:localhost"] = 50
    client.rooms = {"!room:localhost": room}
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )

    current_events: dict[str, dict[str, object]] = {}

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        current_events[kwargs["state_key"]] = kwargs["content"]
        return nio.RoomPutStateResponse.from_dict(
            {"event_id": "$state"},
            room_id="!room:localhost",
        )

    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await set_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        set_by="@alice:localhost",
    )

    assert state.tags["resolved"].set_by == "@alice:localhost"
    client.joined_members.assert_awaited_once_with("!room:localhost")
    client.room_get_state_event.assert_awaited_once_with(
        room_id="!room:localhost",
        event_type="m.room.power_levels",
    )


@pytest.mark.asyncio
async def test_set_thread_tag_writes_state_and_returns_state() -> None:
    """Thread tags should be written to the expected room state key."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    current_events: dict[str, dict[str, object]] = {}

    async def room_get_state_event(**kwargs: object) -> object:
        assert kwargs["event_type"] == "m.room.power_levels"
        return _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        )

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        current_events[kwargs["state_key"]] = kwargs["content"]
        return nio.RoomPutStateResponse.from_dict(
            {"event_id": "$state"},
            room_id="!room:localhost",
        )

    client.room_get_state_event.side_effect = room_get_state_event
    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await set_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        set_by="@alice:localhost",
        note="Fixed in abc123",
    )

    client.room_put_state.assert_awaited_once()
    _, kwargs = client.room_put_state.await_args
    assert kwargs["room_id"] == "!room:localhost"
    assert kwargs["event_type"] == THREAD_TAGS_EVENT_TYPE
    assert kwargs["state_key"] == _thread_tag_state_key("$thread-root:localhost", "resolved")
    assert kwargs["content"]["set_by"] == "@alice:localhost"
    assert kwargs["content"]["note"] == "Fixed in abc123"
    assert kwargs["content"]["data"] == {}
    assert state.thread_root_id == "$thread-root:localhost"
    assert list(state.tags) == ["resolved"]
    assert state.tags["resolved"].set_by == "@alice:localhost"
    assert state.tags["resolved"].note == "Fixed in abc123"


@pytest.mark.asyncio
async def test_set_thread_tag_merges_existing_valid_tags_and_drops_malformed_siblings() -> None:
    """Read-merge-write should preserve valid tags and drop malformed siblings."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    current_events: dict[str, dict[str, object]] = {
        _thread_tag_state_key("$thread-root:localhost", "blocked"): _tag_record_content(
            data={"blocked_by": ["  $other:localhost  "]},
        ),
        _thread_tag_state_key("$thread-root:localhost", "waiting"): _tag_record_content(data={"waiting_on": 42}),
        _thread_tag_state_key("$thread-root:localhost", "review"): {
            "set_by": "@user:localhost",
            "set_at": "2026-03-21T19:02:03+00:00",
            "note": 42,
            "data": {},
        },
        _thread_tag_state_key("$thread-root:localhost", "custom"): {
            "set_by": "@user:localhost",
            "set_at": "2026-03-21T19:02:03+00:00",
            "data": [],
        },
        "$thread-root:localhost": _thread_tags_content(
            blocked=_tag_record_content(data={"blocked_by": ["  $other:localhost  "]}),
            **{
                "bad tag!": _tag_record_content(),
                "waiting": _tag_record_content(data={"waiting_on": 42}),
                "review": {
                    "set_by": "@user:localhost",
                    "set_at": "2026-03-21T19:02:03+00:00",
                    "note": 42,
                    "data": {},
                },
                "custom": {
                    "set_by": "@user:localhost",
                    "set_at": "2026-03-21T19:02:03+00:00",
                    "data": [],
                },
            },
        ),
    }

    async def room_get_state_event(**kwargs: object) -> object:
        assert kwargs["event_type"] == "m.room.power_levels"
        return _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        )

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        current_events[kwargs["state_key"]] = kwargs["content"]
        return nio.RoomPutStateResponse.from_dict(
            {"event_id": "$state"},
            room_id="!room:localhost",
        )

    client.room_get_state_event.side_effect = room_get_state_event
    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await set_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "priority",
        set_by="@alice:localhost",
        data={"level": "HIGH"},
    )

    assert set(state.tags) == {"blocked", "priority"}
    assert state.tags["blocked"].data == {"blocked_by": ["$other:localhost"]}
    assert state.tags["priority"].data == {"level": "high"}
    _, kwargs = client.room_put_state.await_args
    assert kwargs["state_key"] == _thread_tag_state_key("$thread-root:localhost", "priority")
    assert kwargs["content"]["data"] == {"level": "high"}


@pytest.mark.asyncio
async def test_set_thread_tag_raises_on_write_error() -> None:
    """Write failures should surface as explicit helper errors."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.side_effect = [
        _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        ),
        _thread_tags_state_error(message="missing", status_code="M_NOT_FOUND"),
    ]
    client.room_put_state.return_value = object()
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    with pytest.raises(ThreadTagsError, match="Failed to write thread tags state"):
        await set_thread_tag(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "resolved",
            set_by="@alice:localhost",
        )


@pytest.mark.asyncio
async def test_set_thread_tag_retries_when_verification_detects_concurrent_overwrite() -> None:
    """A concurrently added sibling tag should survive without forcing a retry."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    current_events: dict[str, dict[str, object]] = {}

    async def room_get_state_event(**kwargs: object) -> object:
        assert kwargs["event_type"] == "m.room.power_levels"
        return _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        )

    write_attempts = 0

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        nonlocal write_attempts

        write_attempts += 1
        current_events[kwargs["state_key"]] = kwargs["content"]
        if write_attempts == 1:
            current_events[_thread_tag_state_key("$thread-root:localhost", "blocked")] = _tag_record_content(
                data={"blocked_by": ["$other:localhost"]},
            )

        return nio.RoomPutStateResponse.from_dict(
            {"event_id": f"$state-{write_attempts}"},
            room_id="!room:localhost",
        )

    client.room_get_state_event.side_effect = room_get_state_event
    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await set_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        set_by="@alice:localhost",
    )

    assert write_attempts == 1
    assert set(state.tags) == {"blocked", "resolved"}
    assert state.tags["blocked"].data == {"blocked_by": ["$other:localhost"]}
    assert state.tags["resolved"].set_by == "@alice:localhost"
    _, final_kwargs = client.room_put_state.await_args
    assert final_kwargs["state_key"] == _thread_tag_state_key("$thread-root:localhost", "resolved")


@pytest.mark.asyncio
async def test_set_thread_tag_retries_when_verification_detects_same_tag_payload_mismatch() -> None:
    """A same-tag overwrite should retry until the full payload we wrote survives verification."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    current_events: dict[str, dict[str, object]] = {}

    async def room_get_state_event(**kwargs: object) -> object:
        assert kwargs["event_type"] == "m.room.power_levels"
        return _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        )

    write_attempts = 0

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        nonlocal write_attempts

        write_attempts += 1
        current_events[kwargs["state_key"]] = kwargs["content"]
        if write_attempts == 1:
            current_events[_thread_tag_state_key("$thread-root:localhost", "resolved")] = _tag_record_content(
                set_by="@bob:localhost",
                note="from bob",
                data={"source": "bob"},
            )

        return nio.RoomPutStateResponse.from_dict(
            {"event_id": f"$state-{write_attempts}"},
            room_id="!room:localhost",
        )

    client.room_get_state_event.side_effect = room_get_state_event
    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await set_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        set_by="@alice:localhost",
        note="from alice",
        data={"source": "alice"},
    )

    assert write_attempts == 2
    assert list(state.tags) == ["resolved"]
    assert state.tags["resolved"].set_by == "@alice:localhost"
    assert state.tags["resolved"].note == "from alice"
    assert state.tags["resolved"].data == {"source": "alice"}
    _, final_kwargs = client.room_put_state.await_args
    assert final_kwargs["content"]["set_by"] == "@alice:localhost"
    assert final_kwargs["content"]["note"] == "from alice"
    assert final_kwargs["content"]["data"] == {"source": "alice"}


@pytest.mark.asyncio
async def test_set_thread_tag_retries_when_verification_detects_lost_sibling_tag() -> None:
    """A new-format write should keep a legacy sibling tag without a merge retry."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    current_events: dict[str, dict[str, object]] = {
        "$thread-root:localhost": _thread_tags_content(
            blocked=_tag_record_content(note="original sibling"),
        ),
    }

    async def room_get_state_event(**kwargs: object) -> object:
        assert kwargs["event_type"] == "m.room.power_levels"
        return _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        )

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        current_events[kwargs["state_key"]] = kwargs["content"]
        return nio.RoomPutStateResponse.from_dict(
            {"event_id": "$state"},
            room_id="!room:localhost",
        )

    client.room_get_state_event.side_effect = room_get_state_event
    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await set_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        set_by="@alice:localhost",
    )

    assert set(state.tags) == {"blocked", "resolved"}
    assert state.tags["blocked"].note == "original sibling"


@pytest.mark.asyncio
async def test_remove_thread_tag_writes_updated_state() -> None:
    """Removing one tag should leave the remaining tag state intact."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )
    client.room_get_state.side_effect = [
        _thread_tags_room_state_response(
            _thread_tag_state_event("$thread-root:localhost", "resolved"),
            _thread_tag_state_event(
                "$thread-root:localhost",
                "blocked",
                content=_tag_record_content(data={"blocked_by": ["$other:localhost"]}),
            ),
        ),
        _thread_tags_room_state_response(
            _thread_tag_state_event("$thread-root:localhost", "resolved"),
        ),
    ]
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:localhost",
    )
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    state = await remove_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "blocked",
        requester_user_id="@alice:localhost",
    )

    assert list(state.tags) == ["resolved"]
    client.room_put_state.assert_awaited_once()
    _, kwargs = client.room_put_state.await_args
    assert kwargs["room_id"] == "!room:localhost"
    assert kwargs["event_type"] == THREAD_TAGS_EVENT_TYPE
    assert kwargs["state_key"] == _thread_tag_state_key("$thread-root:localhost", "blocked")
    assert kwargs["content"] == {}


@pytest.mark.asyncio
async def test_remove_thread_tag_writes_empty_state_for_last_tag() -> None:
    """Removing the last tag should write an empty content payload."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )
    client.room_get_state.side_effect = [
        _thread_tags_room_state_response(
            _thread_tag_state_event("$thread-root:localhost", "resolved"),
        ),
        _thread_tags_room_state_response(),
    ]
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:localhost",
    )
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    state = await remove_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        requester_user_id="@alice:localhost",
    )

    assert state.tags == {}
    client.room_put_state.assert_awaited_once_with(
        room_id="!room:localhost",
        event_type=THREAD_TAGS_EVENT_TYPE,
        content={},
        state_key=_thread_tag_state_key("$thread-root:localhost", "resolved"),
    )


@pytest.mark.asyncio
async def test_remove_thread_tag_rejects_missing_existing_state() -> None:
    """Removing should fail instead of creating a tombstone for a missing state event."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )
    client.room_get_state.return_value = _thread_tags_room_state_response()
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    with pytest.raises(ThreadTagsError, match="No thread tags state exists"):
        await remove_thread_tag(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "resolved",
            requester_user_id="@alice:localhost",
        )

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_remove_thread_tag_retries_when_verification_detects_concurrent_restore() -> None:
    """A concurrent stale write that restores the removed tag should trigger one retry."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    current_events: dict[str, dict[str, object]] = {
        _thread_tag_state_key("$thread-root:localhost", "resolved"): _tag_record_content(),
    }

    async def room_get_state_event(**kwargs: object) -> object:
        assert kwargs["event_type"] == "m.room.power_levels"
        return _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        )

    write_attempts = 0

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        nonlocal write_attempts

        write_attempts += 1
        current_events[kwargs["state_key"]] = kwargs["content"]
        if write_attempts == 1:
            current_events[_thread_tag_state_key("$thread-root:localhost", "resolved")] = _tag_record_content()
            current_events[_thread_tag_state_key("$thread-root:localhost", "blocked")] = _tag_record_content(
                data={"blocked_by": ["$other:localhost"]},
            )

        return nio.RoomPutStateResponse.from_dict(
            {"event_id": f"$state-{write_attempts}"},
            room_id="!room:localhost",
        )

    client.room_get_state_event.side_effect = room_get_state_event
    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await remove_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        requester_user_id="@alice:localhost",
    )

    assert write_attempts == 2
    assert list(state.tags) == ["blocked"]
    assert state.tags["blocked"].data == {"blocked_by": ["$other:localhost"]}
    _, final_kwargs = client.room_put_state.await_args
    assert final_kwargs["content"] == {}


@pytest.mark.asyncio
async def test_remove_thread_tag_retries_when_verification_detects_sibling_payload_change() -> None:
    """A concurrently added sibling tag should survive one remove write without a retry."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    current_events: dict[str, dict[str, object]] = {
        _thread_tag_state_key("$thread-root:localhost", "resolved"): _tag_record_content(),
    }
    write_attempts = 0

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        nonlocal write_attempts

        write_attempts += 1
        current_events[kwargs["state_key"]] = kwargs["content"]
        current_events[_thread_tag_state_key("$thread-root:localhost", "blocked")] = _tag_record_content(
            note="added concurrently",
            data={"blocked_by": ["$other:localhost"]},
        )
        return nio.RoomPutStateResponse.from_dict(
            {"event_id": "$state"},
            room_id="!room:localhost",
        )

    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await remove_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        requester_user_id="@alice:localhost",
    )

    assert list(state.tags) == ["blocked"]
    assert state.tags["blocked"].note == "added concurrently"
    assert state.tags["blocked"].data == {"blocked_by": ["$other:localhost"]}
    assert write_attempts == 1
    client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_remove_thread_tag_accepts_empty_state_after_concurrent_last_sibling_remove() -> None:
    """A post-write empty reread should be accepted when another actor removed the last sibling."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )
    client.room_get_state.side_effect = [
        _thread_tags_room_state_response(
            _thread_tag_state_event("$thread-root:localhost", "resolved"),
            _thread_tag_state_event(
                "$thread-root:localhost",
                "blocked",
                content=_tag_record_content(data={"blocked_by": ["$other:localhost"]}),
            ),
        ),
        _thread_tags_room_state_response(),
    ]
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:localhost",
    )
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    state = await remove_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "resolved",
        requester_user_id="@alice:localhost",
    )

    assert state.tags == {}
    client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_remove_thread_tag_rejects_missing_tag() -> None:
    """Removing an absent tag should be an explicit error."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )
    client.room_get_state.return_value = _thread_tags_room_state_response(
        _thread_tag_state_event("$thread-root:localhost", "resolved"),
    )
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    with pytest.raises(ThreadTagsError, match="is not set"):
        await remove_thread_tag(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "blocked",
            requester_user_id="@alice:localhost",
        )

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_thread_tags_parses_valid_state() -> None:
    """A valid room-state payload should return parsed tags for the thread."""
    client = AsyncMock()
    client.room_get_state.return_value = _thread_tags_room_state_response(
        _thread_tag_state_event(
            "$thread-root:localhost",
            "resolved",
            content=_tag_record_content(note="done"),
        ),
        _thread_tag_state_event(
            "$thread-root:localhost",
            "blocked",
            content=_tag_record_content(data={"blocked_by": ["$other:localhost"]}),
        ),
    )

    state = await get_thread_tags(
        client,
        "!room:localhost",
        "$thread-root:localhost",
    )

    assert state is not None
    assert state.thread_root_id == "$thread-root:localhost"
    assert set(state.tags) == {"resolved", "blocked"}
    assert state.tags["resolved"].note == "done"
    assert state.tags["blocked"].data == {"blocked_by": ["$other:localhost"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        _thread_tags_room_state_response(),
        _thread_tags_room_state_response(
            {
                "type": THREAD_TAGS_EVENT_TYPE,
                "state_key": _thread_tag_state_key("$thread-root:localhost", "resolved"),
                "content": {},
            },
        ),
        _thread_tags_room_state_response(
            _legacy_thread_tags_event("$thread-root:localhost", content={}),
        ),
        _thread_tags_room_state_response(
            _legacy_thread_tags_event("$thread-root:localhost", content={"tags": {}}),
        ),
        _thread_tags_room_state_response(
            _legacy_thread_tags_event("$thread-root:localhost", content={"tags": "invalid"}),
        ),
        _thread_tags_room_state_response(
            _thread_tag_state_event(
                "$thread-root:localhost",
                "resolved",
                content={"set_by": "@user:localhost", "set_at": "bad", "data": {}},
            ),
        ),
    ],
)
async def test_get_thread_tags_returns_none_for_missing_empty_and_malformed(response: object) -> None:
    """Missing, empty, or fully malformed state should be treated as untagged."""
    client = AsyncMock()
    client.room_get_state.return_value = response

    state = await get_thread_tags(
        client,
        "!room:localhost",
        "$thread-root:localhost",
    )

    assert state is None


@pytest.mark.asyncio
async def test_get_thread_tags_drops_malformed_tags_and_preserves_valid_siblings() -> None:
    """Malformed tags should be ignored without discarding valid siblings."""
    client = AsyncMock()
    client.room_get_state.return_value = _thread_tags_room_state_response(
        _thread_tag_state_event("$thread-root:localhost", "resolved"),
        _thread_tag_state_event(
            "$thread-root:localhost",
            "blocked",
            content=_tag_record_content(data={"blocked_by": "$not-a-list"}),
        ),
        _thread_tag_state_event(
            "$thread-root:localhost",
            "due",
            content=_tag_record_content(data={"deadline": "not-a-date"}),
        ),
        _thread_tag_state_event(
            "$thread-root:localhost",
            "review",
            content={
                "set_by": "@user:localhost",
                "set_at": "2026-03-21T19:02:03+00:00",
                "note": 42,
                "data": {},
            },
        ),
        _thread_tag_state_event(
            "$thread-root:localhost",
            "custom",
            content={
                "set_by": "@user:localhost",
                "set_at": "2026-03-21T19:02:03+00:00",
                "data": [],
            },
        ),
    )

    state = await get_thread_tags(
        client,
        "!room:localhost",
        "$thread-root:localhost",
    )

    assert state is not None
    assert list(state.tags) == ["resolved"]


@pytest.mark.asyncio
async def test_get_thread_tags_ignores_malformed_per_tag_overlay_and_keeps_legacy_tag() -> None:
    """A malformed per-tag overlay must not hide a valid legacy tag during migration."""
    client = AsyncMock()
    client.room_get_state.return_value = _thread_tags_room_state_response(
        _legacy_thread_tags_event(
            "$thread-root:localhost",
            content=_thread_tags_content(
                resolved=_tag_record_content(note="legacy tag"),
            ),
        ),
        _thread_tag_state_event(
            "$thread-root:localhost",
            "resolved",
            content={"set_by": "@user:localhost", "set_at": "bad", "data": {}},
        ),
    )

    state = await get_thread_tags(
        client,
        "!room:localhost",
        "$thread-root:localhost",
    )

    assert state is not None
    assert list(state.tags) == ["resolved"]
    assert state.tags["resolved"].note == "legacy tag"


@pytest.mark.asyncio
async def test_list_tagged_threads_filters_non_matching_events_and_supports_tag_filter() -> None:
    """Room-wide listing should keep only valid tag state and support tag filtering."""
    client = AsyncMock()
    client.room_get_state.return_value = _thread_tags_room_state_response(
        _thread_tag_state_event("$thread-one:localhost", "resolved"),
        _thread_tag_state_event(
            "$thread-two:localhost",
            "blocked",
            content=_tag_record_content(data={"blocked_by": ["$other:localhost"]}),
        ),
        _thread_tag_state_event(
            "$thread-three:localhost",
            "blocked",
            content=_tag_record_content(data={"blocked_by": "$bad"}),
        ),
        {
            "type": THREAD_TAGS_EVENT_TYPE,
            "state_key": _thread_tag_state_key("$thread-four:localhost", "resolved"),
            "content": {},
        },
        _legacy_thread_tags_event(
            "$thread-six:localhost",
            content={
                "tags": {
                    "custom": {
                        "set_by": "@user:localhost",
                        "set_at": "2026-03-21T19:02:03+00:00",
                        "data": [],
                    },
                },
            },
        ),
        _legacy_thread_tags_event(
            "$thread-seven:localhost",
            content={
                "tags": {
                    "review": {
                        "set_by": "@user:localhost",
                        "set_at": "2026-03-21T19:02:03+00:00",
                        "note": 42,
                        "data": {},
                    },
                },
            },
        ),
        {
            "type": "com.mindroom.other",
            "state_key": "$thread-five:localhost",
            "content": _thread_tags_content(resolved=_tag_record_content()),
        },
    )

    all_threads = await list_tagged_threads(client, "!room:localhost")
    resolved_threads = await list_tagged_threads(client, "!room:localhost", tag="resolved")

    assert set(all_threads) == {"$thread-one:localhost", "$thread-two:localhost"}
    assert set(resolved_threads) == {"$thread-one:localhost"}


@pytest.mark.asyncio
async def test_list_tagged_threads_raises_for_room_state_fetch_errors() -> None:
    """Room-wide listing should surface Matrix failures instead of faking an empty result."""
    client = AsyncMock()
    client.room_get_state.return_value = object()

    with pytest.raises(ThreadTagsError, match="Failed to fetch room state for thread tags"):
        await list_tagged_threads(client, "!room:localhost")


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["", " ", "Bad Tag", "needs_underscore", "a" * 51])
async def test_set_thread_tag_rejects_invalid_tag_names(tag: str) -> None:
    """Tag names should follow the stable slug format before any Matrix I/O."""
    client = AsyncMock()

    with pytest.raises(ThreadTagsError, match="tag"):
        await set_thread_tag(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            tag,
            set_by="@alice:localhost",
        )

    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_thread_tag_normalizes_supported_predefined_payloads() -> None:
    """Predefined tag schemas should normalize accepted structured data."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    current_events: dict[str, dict[str, object]] = {}

    async def room_get_state_event(**kwargs: object) -> object:
        assert kwargs["event_type"] == "m.room.power_levels"
        return _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        )

    async def room_get_state(room_id: str) -> object:
        assert room_id == "!room:localhost"
        return _thread_tags_room_state_from_current(current_events)

    async def room_put_state(**kwargs: object) -> object:
        current_events[kwargs["state_key"]] = kwargs["content"]
        return nio.RoomPutStateResponse.from_dict(
            {"event_id": "$state"},
            room_id="!room:localhost",
        )

    client.room_get_state_event.side_effect = room_get_state_event
    client.room_get_state.side_effect = room_get_state
    client.room_put_state.side_effect = room_put_state

    state = await set_thread_tag(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "due",
        set_by="@alice:localhost",
        data={"deadline": "2026-03-26T22:00:00Z"},
    )

    assert state.tags["due"].data == {"deadline": "2026-03-26T22:00:00+00:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "data", "message"),
    [
        ("blocked", {"blocked_by": "$other:localhost"}, "blocked.data.blocked_by"),
        ("waiting", {"waiting_on": 42}, "waiting.data.waiting_on"),
        ("priority", {"level": "urgent"}, "priority.data.level"),
        ("due", {"deadline": "not-a-date"}, "due.data.deadline"),
    ],
)
async def test_set_thread_tag_rejects_invalid_predefined_payloads(
    tag: str,
    data: dict[str, object],
    message: str,
) -> None:
    """Predefined tag data should fail fast on invalid shapes."""
    client = AsyncMock()

    with pytest.raises(ThreadTagsError, match=message):
        await set_thread_tag(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            tag,
            set_by="@alice:localhost",
            data=data,
        )

    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_thread_tag_rejects_non_json_compatible_custom_data() -> None:
    """Custom tag data should fail fast when nested values are not JSON-compatible."""
    client = AsyncMock()

    with pytest.raises(ThreadTagsError, match="JSON-compatible"):
        await set_thread_tag(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "custom",
            set_by="@alice:localhost",
            data={"bad": object()},
        )

    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
async def test_set_thread_tag_rejects_non_finite_float_custom_data(value: float) -> None:
    """Custom tag data should reject NaN and infinity before any Matrix I/O."""
    client = AsyncMock()

    with pytest.raises(ThreadTagsError, match="finite JSON-compatible numbers"):
        await set_thread_tag(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "custom",
            set_by="@alice:localhost",
            data={"bad": value},
        )

    client.room_get_state_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "remove"])
async def test_thread_tags_write_rejects_insufficient_power_level(action: str) -> None:
    """State writes should fail before sending when the Matrix account lacks power."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 0,
            "@alice:localhost": 50,
        },
    )

    with pytest.raises(ThreadTagsError, match="Insufficient Matrix power level"):
        await _write_operation(client, action)

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "remove"])
async def test_thread_tags_write_rejects_low_power_requester(action: str) -> None:
    """State writes should fail when the requester lacks the required room power."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 0,
        },
    )
    client.joined_members.return_value = _joined_members_response(
        "@mindroom_general:localhost",
        "@alice:localhost",
    )

    with pytest.raises(ThreadTagsError, match="the requester"):
        await _write_operation(client, action)

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "remove"])
async def test_thread_tags_write_rejects_non_member_requester_even_with_pl0_event(action: str) -> None:
    """Cross-room writes should reject requesters who are not joined to the target room."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 100,
        },
        users_default=0,
        events={THREAD_TAGS_EVENT_TYPE: 0},
    )
    client.joined_members.return_value = nio.JoinedMembersResponse.from_dict(
        {
            "joined": {
                "@mindroom_general:localhost": {"display_name": "MindRoom"},
            },
        },
        room_id="!room:localhost",
    )

    with pytest.raises(ThreadTagsError, match="not joined to the target room"):
        await _write_operation(client, action)

    client.joined_members.assert_awaited_once_with("!room:localhost")
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "remove"])
async def test_thread_tags_write_rejects_missing_client_user_id(action: str) -> None:
    """State writes should fail closed when the Matrix client identity is unavailable."""
    client = AsyncMock()
    client.user_id = None

    with pytest.raises(ThreadTagsError, match=r"client\.user_id must be a non-empty string"):
        await _write_operation(client, action)

    client.room_get_state_event.assert_not_awaited()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "remove"])
async def test_thread_tags_write_rejects_when_power_levels_fetch_fails(action: str) -> None:
    """State writes should fail closed when room power levels cannot be loaded."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = object()

    with pytest.raises(ThreadTagsError, match="Failed to fetch Matrix power levels"):
        await _write_operation(client, action)

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_thread_tags_raises_for_non_missing_state_fetch_error() -> None:
    """State read failures should not be reported as missing tags."""
    client = AsyncMock()
    client.room_get_state.return_value = object()

    with pytest.raises(ThreadTagsError, match="Failed to fetch room state for thread tags"):
        await get_thread_tags(
            client,
            "!room:localhost",
            "$thread-root:localhost",
        )


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_root_for_root_event() -> None:
    """A root event should normalize to itself."""
    client = AsyncMock()
    client.room_get_event.return_value = _message_event_response(
        "$thread-root:localhost",
        content={"body": "Root", "msgtype": "m.text"},
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$thread-root:localhost",
    )

    assert normalized == "$thread-root:localhost"


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_thread_root_for_thread_reply() -> None:
    """Native thread replies should normalize to their thread root."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        side_effect=[
            _message_event_response(
                "$thread-reply:localhost",
                content={
                    "body": "Reply",
                    "msgtype": "m.text",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread-root:localhost",
                    },
                },
            ),
        ],
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$thread-reply:localhost",
    )

    assert normalized == "$thread-root:localhost"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$thread-reply:localhost")


@pytest.mark.asyncio
@pytest.mark.parametrize("event_id", ["", "   "])
async def test_normalize_thread_root_event_id_returns_none_for_blank_input(event_id: str) -> None:
    """Blank event IDs should be rejected before any event lookup."""
    client = AsyncMock()

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        event_id,
    )

    assert normalized is None
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_none_for_plain_reply() -> None:
    """Plain replies should no longer be promoted into synthetic thread roots."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        return_value=_message_event_response(
            "$reply-two:localhost",
            content={
                "body": "Reply two",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$reply-one:localhost"}},
            },
        ),
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$reply-two:localhost",
    )

    assert normalized is None
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$reply-two:localhost")


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_none_when_lookup_fails() -> None:
    """Missing or unreadable events should not guess a thread root."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(return_value=object())

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$reply-one:localhost",
    )
    assert normalized is None
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$reply-one:localhost")


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_resolves_thread_edit_via_original_event() -> None:
    """Thread edits should normalize directly from explicit thread metadata."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        return_value=_message_event_response(
            "$edit:localhost",
            content={
                "body": "* edited",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "edited",
                    "msgtype": "m.text",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread-root:localhost",
                    },
                },
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$thread-reply:localhost",
                },
            },
        ),
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$edit:localhost",
    )

    assert normalized == "$thread-root:localhost"
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$edit:localhost")


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_resolves_thread_reply_edit_via_original_event_without_nested_thread_metadata() -> (
    None
):
    """Edits of thread replies should fall back through the original event when nested thread metadata is absent."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        side_effect=[
            _message_event_response(
                "$edit:localhost",
                content={
                    "body": "* edited",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "edited",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": "$thread-reply:localhost",
                    },
                },
            ),
            _message_event_response(
                "$thread-reply:localhost",
                content={
                    "body": "Reply",
                    "msgtype": "m.text",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": "$thread-root:localhost",
                    },
                },
            ),
        ],
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$edit:localhost",
    )

    assert normalized == "$thread-root:localhost"
    assert client.room_get_event.await_args_list[0].args == ("!room:localhost", "$edit:localhost")
    assert client.room_get_event.await_args_list[1].args == ("!room:localhost", "$thread-reply:localhost")


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_resolves_thread_root_edit_via_original_event() -> None:
    """Edits of thread-root messages should normalize back to the original root event."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        side_effect=[
            _message_event_response(
                "$edit:localhost",
                content={
                    "body": "* edited root",
                    "msgtype": "m.text",
                    "m.new_content": {
                        "body": "edited root",
                        "msgtype": "m.text",
                    },
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": "$thread-root:localhost",
                    },
                },
            ),
            _message_event_response(
                "$thread-root:localhost",
                content={"body": "Root", "msgtype": "m.text"},
            ),
        ],
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$edit:localhost",
    )

    assert normalized == "$thread-root:localhost"
    assert client.room_get_event.await_args_list[0].args == ("!room:localhost", "$edit:localhost")
    assert client.room_get_event.await_args_list[1].args == ("!room:localhost", "$thread-root:localhost")
