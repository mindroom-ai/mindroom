"""Tests for Matrix thread resolution state helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

import nio
import pytest

import mindroom.thread_resolution as thread_resolution_module
from mindroom.matrix.reply_chain import canonicalize_related_event_id
from mindroom.thread_resolution import (
    THREAD_RESOLUTION_EVENT_TYPE,
    ThreadResolutionError,
    clear_thread_resolution,
    get_thread_resolution,
    list_resolved_threads,
    normalize_thread_root_event_id,
    set_thread_resolved,
)


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


def _resolved_content(
    thread_root_id: str,
    *,
    status: str = "resolved",
) -> dict[str, str]:
    return {
        "thread_root_id": thread_root_id,
        "status": status,
        "resolved_by": "@user:localhost",
        "resolved_at": "2026-03-21T19:02:03+00:00",
        "updated_at": "2026-03-21T19:02:03+00:00",
    }


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


def _thread_resolution_state_response(
    thread_root_id: str,
    *,
    content: dict[str, object] | None = None,
) -> nio.RoomGetStateEventResponse:
    return nio.RoomGetStateEventResponse(
        content=content if content is not None else _resolved_content(thread_root_id),
        event_type=THREAD_RESOLUTION_EVENT_TYPE,
        state_key=thread_root_id,
        room_id="!room:localhost",
    )


def _thread_resolution_state_error(
    *,
    message: str,
    status_code: str,
) -> nio.RoomGetStateEventError:
    return nio.RoomGetStateEventError(
        message,
        status_code=status_code,
        room_id="!room:localhost",
    )


@pytest.mark.asyncio
async def test_set_thread_resolved_writes_state_and_returns_record() -> None:
    """Resolved state should be written to the expected room state key."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:localhost",
    )

    record = await set_thread_resolved(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        "@alice:localhost",
    )

    client.room_put_state.assert_awaited_once()
    _, kwargs = client.room_put_state.await_args
    assert kwargs["room_id"] == "!room:localhost"
    assert kwargs["event_type"] == THREAD_RESOLUTION_EVENT_TYPE
    assert kwargs["state_key"] == "$thread-root:localhost"
    assert kwargs["content"]["thread_root_id"] == "$thread-root:localhost"
    assert kwargs["content"]["status"] == "resolved"
    assert kwargs["content"]["resolved_by"] == "@alice:localhost"
    assert kwargs["content"]["resolved_at"] == record.resolved_at.isoformat()
    assert kwargs["content"]["updated_at"] == record.updated_at.isoformat()
    assert record.thread_root_id == "$thread-root:localhost"
    assert record.is_resolved is True


@pytest.mark.asyncio
async def test_set_thread_resolved_raises_on_write_error() -> None:
    """Write failures should surface as explicit helper errors."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 50,
        },
    )
    client.room_put_state.return_value = object()

    with pytest.raises(ThreadResolutionError, match="Failed to write thread resolution state"):
        await set_thread_resolved(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "@alice:localhost",
        )


@pytest.mark.asyncio
async def test_clear_thread_resolution_writes_empty_state() -> None:
    """Clearing a resolved marker should write an empty state payload."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.side_effect = [
        _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        ),
        _thread_resolution_state_response("$thread-root:localhost"),
    ]
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!room:localhost",
    )

    await clear_thread_resolution(
        client,
        "!room:localhost",
        "$thread-root:localhost",
        requester_user_id="@alice:localhost",
    )

    client.room_put_state.assert_awaited_once_with(
        room_id="!room:localhost",
        event_type=THREAD_RESOLUTION_EVENT_TYPE,
        content={},
        state_key="$thread-root:localhost",
    )
    assert client.room_get_state_event.await_count == 2


@pytest.mark.asyncio
async def test_get_thread_resolution_parses_valid_state() -> None:
    """A valid room-state payload should return a parsed record."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _thread_resolution_state_response("$thread-root:localhost")

    record = await get_thread_resolution(
        client,
        "!room:localhost",
        "$thread-root:localhost",
    )

    assert record is not None
    assert record.thread_root_id == "$thread-root:localhost"
    assert record.resolved_by == "@user:localhost"
    assert record.is_resolved is True


@pytest.mark.asyncio
async def test_get_thread_resolution_returns_none_for_non_resolved_status() -> None:
    """Only the persisted resolved status should be treated as a valid record."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _thread_resolution_state_response(
        "$thread-root:localhost",
        content=_resolved_content("$thread-root:localhost", status="in_progress"),
    )

    record = await get_thread_resolution(
        client,
        "!room:localhost",
        "$thread-root:localhost",
    )

    assert record is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "thread_root_id"),
    [
        (
            _thread_resolution_state_error(message="missing", status_code="M_NOT_FOUND"),
            "$thread-root:localhost",
        ),
        (
            _thread_resolution_state_response("$thread-root:localhost", content={}),
            "$thread-root:localhost",
        ),
        (
            _thread_resolution_state_response(
                "$thread-root:localhost",
                content={
                    "thread_root_id": "$other-root:localhost",
                    "status": "resolved",
                    "resolved_by": "@user:localhost",
                    "resolved_at": "2026-03-21T19:02:03+00:00",
                    "updated_at": "2026-03-21T19:02:03+00:00",
                },
            ),
            "$thread-root:localhost",
        ),
        (
            _thread_resolution_state_response(
                "$thread-root:localhost",
                content={
                    "thread_root_id": "$thread-root:localhost",
                    "status": "resolved",
                    "resolved_by": "@user:localhost",
                    "resolved_at": "not-a-timestamp",
                    "updated_at": "2026-03-21T19:02:03+00:00",
                },
            ),
            "$thread-root:localhost",
        ),
    ],
)
async def test_get_thread_resolution_returns_none_for_missing_empty_and_malformed(
    response: object,
    thread_root_id: str,
) -> None:
    """Missing, empty, or malformed state should be treated as unresolved."""
    client = AsyncMock()
    client.room_get_state_event.return_value = response

    record = await get_thread_resolution(
        client,
        "!room:localhost",
        thread_root_id,
    )

    assert record is None


@pytest.mark.asyncio
async def test_get_thread_resolution_raises_for_non_missing_state_fetch_error() -> None:
    """State read failures should not be reported as unresolved threads."""
    client = AsyncMock()
    client.room_get_state_event.return_value = _thread_resolution_state_error(
        message="forbidden",
        status_code="M_FORBIDDEN",
    )

    with pytest.raises(ThreadResolutionError, match="Failed to fetch thread resolution state"):
        await get_thread_resolution(
            client,
            "!room:localhost",
            "$thread-root:localhost",
        )


@pytest.mark.asyncio
async def test_list_resolved_threads_filters_non_matching_events_and_tombstones() -> None:
    """Room state listing should keep only valid non-empty resolution records."""
    client = AsyncMock()
    client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_RESOLUTION_EVENT_TYPE,
                "state_key": "$thread-one:localhost",
                "content": _resolved_content("$thread-one:localhost"),
            },
            {
                "type": THREAD_RESOLUTION_EVENT_TYPE,
                "state_key": "$thread-two:localhost",
                "content": {},
            },
            {
                "type": THREAD_RESOLUTION_EVENT_TYPE,
                "state_key": "$thread-three:localhost",
                "content": _resolved_content("$thread-three:localhost", status="in_progress"),
            },
            {
                "type": "com.mindroom.other",
                "state_key": "$thread-four:localhost",
                "content": _resolved_content("$thread-four:localhost"),
            },
        ],
        room_id="!room:localhost",
    )

    records = await list_resolved_threads(client, "!room:localhost")

    assert list(records) == ["$thread-one:localhost"]
    assert records["$thread-one:localhost"].thread_root_id == "$thread-one:localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resolve", "clear"])
async def test_thread_resolution_write_rejects_insufficient_power_level(action: str) -> None:
    """State writes should fail before sending when the Matrix account lacks power."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 0,
            "@alice:localhost": 50,
        },
    )
    operation = (
        set_thread_resolved(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "@alice:localhost",
        )
        if action == "resolve"
        else clear_thread_resolution(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            requester_user_id="@alice:localhost",
        )
    )

    with pytest.raises(ThreadResolutionError, match="Insufficient Matrix power level"):
        await operation

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resolve", "clear"])
async def test_thread_resolution_write_rejects_low_power_requester(action: str) -> None:
    """State writes should fail when the requester lacks the required room power."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = _power_levels_response(
        users={
            "@mindroom_general:localhost": 50,
            "@alice:localhost": 0,
        },
    )
    operation = (
        set_thread_resolved(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "@alice:localhost",
        )
        if action == "resolve"
        else clear_thread_resolution(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            requester_user_id="@alice:localhost",
        )
    )

    with pytest.raises(ThreadResolutionError, match="the requester"):
        await operation

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resolve", "clear"])
async def test_thread_resolution_write_rejects_missing_client_user_id(action: str) -> None:
    """State writes should fail closed when the Matrix client identity is unavailable."""
    client = AsyncMock()
    client.user_id = None
    operation = (
        set_thread_resolved(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "@alice:localhost",
        )
        if action == "resolve"
        else clear_thread_resolution(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            requester_user_id="@alice:localhost",
        )
    )

    with pytest.raises(ThreadResolutionError, match=r"client\.user_id must be a non-empty string"):
        await operation

    client.room_get_state_event.assert_not_awaited()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["resolve", "clear"])
async def test_thread_resolution_write_rejects_when_power_levels_fetch_fails(action: str) -> None:
    """State writes should fail closed when room power levels cannot be loaded."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.return_value = object()
    operation = (
        set_thread_resolved(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            "@alice:localhost",
        )
        if action == "resolve"
        else clear_thread_resolution(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            requester_user_id="@alice:localhost",
        )
    )

    with pytest.raises(ThreadResolutionError, match="Failed to fetch Matrix power levels"):
        await operation

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_thread_resolution_rejects_missing_existing_marker() -> None:
    """Clearing should fail instead of creating a tombstone for a missing marker."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.side_effect = [
        _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        ),
        _thread_resolution_state_error(message="missing", status_code="M_NOT_FOUND"),
    ]

    with pytest.raises(ThreadResolutionError, match="No thread resolution state exists"):
        await clear_thread_resolution(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            requester_user_id="@alice:localhost",
        )

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_clear_thread_resolution_raises_when_state_fetch_fails() -> None:
    """Clearing should surface state lookup failures that are not true misses."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.room_get_state_event.side_effect = [
        _power_levels_response(
            users={
                "@mindroom_general:localhost": 50,
                "@alice:localhost": 50,
            },
        ),
        _thread_resolution_state_error(message="forbidden", status_code="M_FORBIDDEN"),
    ]

    with pytest.raises(ThreadResolutionError, match="Failed to fetch thread resolution state"):
        await clear_thread_resolution(
            client,
            "!room:localhost",
            "$thread-root:localhost",
            requester_user_id="@alice:localhost",
        )

    client.room_put_state.assert_not_awaited()


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
            _message_event_response(
                "$thread-root:localhost",
                content={"body": "Root", "msgtype": "m.text"},
            ),
        ],
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$thread-reply:localhost",
    )

    assert normalized == "$thread-root:localhost"


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_none_for_missing_thread_root() -> None:
    """Thread replies with nonexistent thread roots should fail normalization."""
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
                        "event_id": "$missing-root:localhost",
                    },
                },
            ),
            object(),
        ],
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$thread-reply:localhost",
    )

    assert normalized is None
    assert client.room_get_event.await_count == 2


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
async def test_canonicalize_related_event_id_returns_none_for_blank_input() -> None:
    """The shared relation walker should reject blank event IDs directly."""
    client = AsyncMock()

    normalized = await canonicalize_related_event_id(
        client,
        "!room:localhost",
        "   ",
    )

    assert normalized is None
    client.room_get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_canonicalize_related_event_id_returns_none_for_related_events_without_targets() -> None:
    """Malformed related events should fail closed in the shared canonicalizer too."""
    client = AsyncMock()
    client.room_get_event.return_value = _message_event_response(
        "$annotation:localhost",
        content={
            "body": "Reaction",
            "msgtype": "m.text",
            "m.relates_to": {"rel_type": "m.annotation", "key": "👍"},
        },
    )

    normalized = await canonicalize_related_event_id(
        client,
        "!room:localhost",
        "$annotation:localhost",
    )

    assert normalized is None
    client.room_get_event.assert_awaited_once_with("!room:localhost", "$annotation:localhost")


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_walks_plain_reply_chain() -> None:
    """Plain replies should collapse to the conversation root."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        side_effect=[
            _message_event_response(
                "$reply-two:localhost",
                content={
                    "body": "Reply two",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$reply-one:localhost"}},
                },
            ),
            _message_event_response(
                "$reply-one:localhost",
                content={
                    "body": "Reply one",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-root:localhost"}},
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
        "$reply-two:localhost",
    )

    assert normalized == "$thread-root:localhost"


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_none_for_missing_target_event() -> None:
    """Missing events should fail normalization instead of guessing."""
    client = AsyncMock()
    client.room_get_event.return_value = object()

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$missing:localhost",
    )

    assert normalized is None


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_none_for_cycle() -> None:
    """Reply cycles should terminate without looping forever."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        side_effect=[
            _message_event_response(
                "$reply-one:localhost",
                content={
                    "body": "Reply one",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$reply-two:localhost"}},
                },
            ),
            _message_event_response(
                "$reply-two:localhost",
                content={
                    "body": "Reply two",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$reply-one:localhost"}},
                },
            ),
        ],
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$reply-one:localhost",
    )

    assert normalized is None
    assert client.room_get_event.await_count == 2


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_none_when_depth_limit_is_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Overly deep reply chains should stop at the configured traversal limit."""
    monkeypatch.setattr(thread_resolution_module, "MAX_THREAD_ROOT_NORMALIZATION_DEPTH", 2)
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        side_effect=[
            _message_event_response(
                "$reply-two:localhost",
                content={
                    "body": "Reply two",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$reply-one:localhost"}},
                },
            ),
            _message_event_response(
                "$reply-one:localhost",
                content={
                    "body": "Reply one",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-root:localhost"}},
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
        "$reply-two:localhost",
    )

    assert normalized is None
    assert client.room_get_event.await_count == 2


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_resolves_thread_edit_via_original_event() -> None:
    """Thread edits should normalize by fetching the edited event first."""
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
            _message_event_response(
                "$thread-reply:localhost",
                content={
                    "body": "Reply in thread",
                    "msgtype": "m.text",
                    "m.relates_to": {
                        "rel_type": "m.thread",
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
    assert client.room_get_event.await_args_list == [
        call("!room:localhost", "$edit:localhost"),
        call("!room:localhost", "$thread-reply:localhost"),
        call("!room:localhost", "$thread-root:localhost"),
    ]


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_returns_none_for_missing_edited_event() -> None:
    """Thread edits should fail closed when the edited event cannot be loaded."""
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
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$missing-root:localhost",
                        },
                    },
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": "$reply:localhost",
                    },
                },
            ),
            object(),
        ],
    )

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        "$edit:localhost",
    )

    assert normalized is None
    assert client.room_get_event.await_count == 2


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_ignores_forged_thread_root_from_edit() -> None:
    """Thread edits should ignore m.new_content thread roots until the edited event confirms them."""
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
                        "m.relates_to": {
                            "rel_type": "m.thread",
                            "event_id": "$forged-root:localhost",
                        },
                    },
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": "$reply:localhost",
                    },
                },
            ),
            _message_event_response(
                "$reply:localhost",
                content={
                    "body": "Reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-root:localhost"}},
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
    assert client.room_get_event.await_args_list == [
        call("!room:localhost", "$edit:localhost"),
        call("!room:localhost", "$reply:localhost"),
        call("!room:localhost", "$thread-root:localhost"),
    ]


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_resolves_plain_edit_via_original_event() -> None:
    """Plain edits should normalize by walking through the edited event."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        side_effect=[
            _message_event_response(
                "$edit:localhost",
                content={
                    "body": "* edited",
                    "msgtype": "m.text",
                    "m.new_content": {"body": "edited", "msgtype": "m.text"},
                    "m.relates_to": {
                        "rel_type": "m.replace",
                        "event_id": "$reply:localhost",
                    },
                },
            ),
            _message_event_response(
                "$reply:localhost",
                content={
                    "body": "Reply",
                    "msgtype": "m.text",
                    "m.relates_to": {"m.in_reply_to": {"event_id": "$thread-root:localhost"}},
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_id", "content"),
    [
        (
            "$edit:localhost",
            {
                "body": "* edited",
                "msgtype": "m.text",
                "m.new_content": {"body": "edited", "msgtype": "m.text"},
                "m.relates_to": {"rel_type": "m.replace"},
            },
        ),
        (
            "$annotation:localhost",
            {
                "body": "Reaction",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.annotation", "key": "👍"},
            },
        ),
        (
            "$reference:localhost",
            {
                "body": "Reference",
                "msgtype": "m.text",
                "m.relates_to": {"rel_type": "m.reference"},
            },
        ),
    ],
)
async def test_normalize_thread_root_event_id_returns_none_for_related_events_without_targets(
    event_id: str,
    content: dict[str, object],
) -> None:
    """Malformed related events should fail closed instead of using their own IDs."""
    client = AsyncMock()
    client.room_get_event.return_value = _message_event_response(event_id, content=content)

    normalized = await normalize_thread_root_event_id(
        client,
        "!room:localhost",
        event_id,
    )

    assert normalized is None
    client.room_get_event.assert_awaited_once_with("!room:localhost", event_id)


@pytest.mark.asyncio
async def test_normalize_thread_root_event_id_follows_reference_target() -> None:
    """Reference-style relations should normalize through their target event."""
    client = AsyncMock()
    client.room_get_event = AsyncMock(
        side_effect=[
            _message_event_response(
                "$reference:localhost",
                content={
                    "body": "Reference",
                    "msgtype": "m.text",
                    "m.relates_to": {
                        "rel_type": "m.reference",
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
        "$reference:localhost",
    )

    assert normalized == "$thread-root:localhost"
