"""Pure decision rules, single-key removal, and segmented continuation protection for thread auto-reopen."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.constants import (
    RESOLVED_IN_TURN_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.matrix.message_builder import build_matrix_edit_content, build_message_content
from mindroom.matrix.segmented_messages import segment_matrix_content
from mindroom.thread_auto_reopen import ResolvedThread, extract_auto_reopen_reply, resolved_from_state_event
from mindroom.thread_snooze import THREAD_SNOOZE_CONTENT_KEY
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    THREAD_TAGS_EVENT_TYPE,
    ThreadTagRecord,
    _thread_tag_state_key,
    reopen_resolved_thread_record,
)

ROOM = "!room:localhost"
ROOT = "$root:localhost"
HUMAN = "@alice:localhost"
BOT = "@mindroom_code:localhost"
SET_AT = datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
THREAD_REL = {"rel_type": "m.thread", "event_id": ROOT}


def _is_managed(sender: str) -> bool:
    return sender == BOT


def _reply(
    *,
    event_type: str = "m.room.message",
    sender: str = HUMAN,
    event_id: str = "$reply:localhost",
    origin_server_ts: object = 2_000,
    content: object | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload = {"msgtype": "m.text", "body": "follow up", "m.relates_to": THREAD_REL} if content is None else content
    source: dict[str, object] = {
        "type": event_type,
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": origin_server_ts,
        "content": payload,
    }
    if extra:
        source.update(extra)
    return source


def test_resolved_from_state_event_parses_live_fallback_tombstone_and_ignores_others() -> None:
    """Parse resolved tag records, timestamp fallback, tombstones, and ignore unrelated state events."""
    live = {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": _thread_tag_state_key(ROOT, RESOLVED_THREAD_TAG),
        "event_id": "$state1",
        "origin_server_ts": 1_700_000_000_123,
        "content": {"set_by": HUMAN, "set_at": SET_AT.isoformat()},
    }
    assert resolved_from_state_event(ROOM, live) == (
        ROOT,
        ResolvedThread(ROOM, ROOT, 1_700_000_000_123, ThreadTagRecord(set_by=HUMAN, set_at=SET_AT), "$state1"),
    )

    fallback = resolved_from_state_event(ROOM, {**live, "origin_server_ts": True, "event_id": ""})
    assert fallback is not None
    assert fallback[1] is not None
    assert fallback[1].resolved_ts == int(SET_AT.timestamp() * 1000)
    assert fallback[1].state_event_id is None

    assert resolved_from_state_event(ROOM, {**live, "content": {}}) == (ROOT, None)
    assert resolved_from_state_event(ROOM, {**live, "content": {"bad": 1}}) == (ROOT, None)
    assert resolved_from_state_event(ROOM, {**live, "state_key": _thread_tag_state_key(ROOT, "snoozed")}) is None
    assert resolved_from_state_event(ROOM, {**live, "type": "m.room.topic", "state_key": ""}) is None


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_reply(content={"msgtype": mt, "body": "x", "m.relates_to": THREAD_REL}), id=mt)
        for mt in ("m.text", "m.emote", "m.image", "m.audio", "m.video", "m.file", "m.location")
    ]
    + [
        pytest.param(_reply(event_type="m.sticker", content={"body": "cat", "m.relates_to": THREAD_REL}), id="sticker"),
        pytest.param(
            _reply(
                sender=BOT,
                content={
                    "msgtype": "m.notice",
                    "body": "...",
                    STREAM_STATUS_KEY: STREAM_STATUS_PENDING,
                    "m.relates_to": THREAD_REL,
                },
            ),
            id="managed-pending-notice",
        ),
        pytest.param(
            _reply(
                sender=BOT,
                content={
                    "msgtype": "m.notice",
                    "body": "part",
                    STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
                    "m.relates_to": THREAD_REL,
                },
            ),
            id="managed-streaming-notice",
        ),
        pytest.param(
            _reply(
                event_type="io.mindroom.tool_approval",
                sender=BOT,
                content={"approval_id": "a1", "m.relates_to": THREAD_REL},
            ),
            id="managed-tool-approval",
        ),
        pytest.param(
            _reply(
                sender=HUMAN,
                content={"msgtype": "m.text", "body": "spoof", RESOLVED_IN_TURN_KEY: True, "m.relates_to": THREAD_REL},
            ),
            id="human-spoofed-resolved-in-turn",
        ),
    ],
)
def test_extract_auto_reopen_reply_accepts_qualifying_replies(source: dict[str, object]) -> None:
    """Every qualifying new thread reply returns (thread_root_id, event_id, origin_server_ts)."""
    assert extract_auto_reopen_reply(source, sender_is_managed=_is_managed) == (ROOT, "$reply:localhost", 2_000)


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(_reply(extra={"state_key": ""}), id="state-event"),
        pytest.param(_reply(origin_server_ts=True), id="bool-ts"),
        pytest.param(_reply(content="not-a-dict"), id="non-dict-content"),
        pytest.param(_reply(content={"msgtype": "m.text", "body": "room msg"}), id="non-thread"),
        pytest.param(_reply(event_id=ROOT), id="self-root"),
        pytest.param(
            _reply(
                content={
                    "msgtype": "m.text",
                    "body": "* e",
                    "m.relates_to": {"rel_type": "m.replace", "event_id": "$old"},
                },
            ),
            id="replace-edit",
        ),
        pytest.param(
            _reply(
                event_type="io.mindroom.tool_approval",
                sender=BOT,
                content={"m.relates_to": {"rel_type": "m.replace", "event_id": "$c"}},
            ),
            id="approval-settlement-edit",
        ),
        pytest.param(
            _reply(
                content={
                    "msgtype": "m.text",
                    "body": "wake",
                    THREAD_SNOOZE_CONTENT_KEY: {"event": "ended"},
                    "m.relates_to": THREAD_REL,
                },
            ),
            id="snooze-wake",
        ),
        pytest.param(
            _reply(
                sender=BOT,
                content={"msgtype": "m.text", "body": "done", RESOLVED_IN_TURN_KEY: True, "m.relates_to": THREAD_REL},
            ),
            id="managed-resolved-in-turn",
        ),
        pytest.param(
            _reply(sender=BOT, content={"msgtype": "m.notice", "body": "summary", "m.relates_to": THREAD_REL}),
            id="managed-plain-notice",
        ),
        pytest.param(
            _reply(
                sender=BOT,
                content={
                    "msgtype": "m.notice",
                    "body": "done",
                    STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED,
                    "m.relates_to": THREAD_REL,
                },
            ),
            id="managed-completed-notice",
        ),
        pytest.param(
            _reply(
                sender=HUMAN,
                content={
                    "msgtype": "m.notice",
                    "body": "x",
                    STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
                    "m.relates_to": THREAD_REL,
                },
            ),
            id="human-streaming-notice",
        ),
        pytest.param(
            _reply(
                event_type="io.mindroom.tool_approval",
                sender=HUMAN,
                content={"approval_id": "a1", "m.relates_to": THREAD_REL},
            ),
            id="human-tool-approval",
        ),
        pytest.param(
            _reply(content={"msgtype": ["bad"], "body": "x", "m.relates_to": THREAD_REL}),
            id="unhashable-msgtype",
        ),
        *[
            pytest.param(_reply(event_type=et, sender=BOT, content={"m.relates_to": THREAD_REL}), id=et)
            for et in (
                "m.reaction",
                "m.room.redaction",
                "io.mindroom.route",
                "io.mindroom.scheduled.trigger",
                "io.mindroom.tool_approval_response",
                "io.mindroom.background_job_cancel",
                "io.mindroom.workflow.control",
                "m.room.encrypted",
            )
        ],
    ],
)
def test_extract_auto_reopen_reply_ignores_non_qualifying_events(source: dict[str, object]) -> None:
    """Edits, notices, bookkeeping events, and excluded metadata never trigger auto-reopen."""
    assert extract_auto_reopen_reply(source, sender_is_managed=_is_managed) is None


@pytest.mark.asyncio
async def test_reopen_resolved_thread_record_uses_single_key_gets_and_preserves_changes() -> None:
    """Single-key reopen clears matching resolved records without room_get_state and preserves newer writes."""
    resolved_key = _thread_tag_state_key(ROOT, RESOLVED_THREAD_TAG)
    other_key = _thread_tag_state_key(ROOT, "blocked")
    record = ThreadTagRecord(set_by=HUMAN, set_at=SET_AT)
    state: dict[str, dict[str, object]] = {
        resolved_key: {"set_by": HUMAN, "set_at": SET_AT.isoformat()},
        other_key: {"set_by": HUMAN, "set_at": SET_AT.isoformat()},
    }

    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = BOT
    client.room_get_state = AsyncMock()

    async def get_state_event(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == "m.room.power_levels":
            return nio.RoomGetStateEventResponse({"state_default": 50, "users": {BOT: 50}}, event_type, "", room_id)
        content = state.get(state_key)
        if content is None:
            return nio.RoomGetStateEventError("missing", status_code="M_NOT_FOUND")
        return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)

    async def put_state(room_id: str, event_type: str, content: dict[str, object], state_key: str = "") -> object:
        del event_type
        state[state_key] = dict(content)
        return nio.RoomPutStateResponse("$put", room_id)

    client.room_get_state_event = AsyncMock(side_effect=get_state_event)
    client.room_put_state = AsyncMock(side_effect=put_state)

    assert await reopen_resolved_thread_record(client, ROOM, ROOT, expected_record=record) is True
    assert state[resolved_key] == {}
    assert other_key in state
    client.room_get_state.assert_not_awaited()
    client.room_put_state.assert_awaited_once()

    client.room_put_state.reset_mock()
    assert await reopen_resolved_thread_record(client, ROOM, ROOT, expected_record=record) is True
    client.room_put_state.assert_not_awaited()

    newer = {"set_by": HUMAN, "set_at": datetime(2026, 11, 27, 11, 0, tzinfo=UTC).isoformat()}
    state[resolved_key] = newer
    assert await reopen_resolved_thread_record(client, ROOM, ROOT, expected_record=record) is False
    assert state[resolved_key] == newer
    client.room_put_state.assert_not_awaited()


def test_segmented_edit_and_resolved_in_turn_continuations_never_reopen() -> None:
    """Segmented continuations of edits or self-resolved replies carry RESOLVED_IN_TURN_KEY and are ignored."""
    large_body = "\n\n".join(f"Paragraph {i}: {'word ' * 15}" for i in range(600))

    edit = build_matrix_edit_content(event_id="$placeholder", new_content=build_message_content(large_body))
    seg_edit = segment_matrix_content(
        edit,
        room_encrypted=False,
        continuation_thread_id=ROOT,
        continuation_reply_to_event_id="$placeholder",
    )
    assert seg_edit is not None
    assert seg_edit.continuations
    for part in seg_edit.continuations:
        assert part.get(RESOLVED_IN_TURN_KEY) is True
        assert extract_auto_reopen_reply(_reply(sender=BOT, content=part), sender_is_managed=_is_managed) is None

    # Plain long no-placeholder reply: segment 1 still reopens, while continuation segments 2..N never reopen.
    plain_new = build_message_content(large_body, extra_content={"m.relates_to": THREAD_REL})
    seg_plain = segment_matrix_content(
        plain_new,
        room_encrypted=False,
        continuation_thread_id=ROOT,
        continuation_reply_to_event_id="$cause",
    )
    assert seg_plain is not None
    assert seg_plain.continuations
    assert RESOLVED_IN_TURN_KEY not in seg_plain.first
    assert extract_auto_reopen_reply(_reply(sender=BOT, content=seg_plain.first), sender_is_managed=_is_managed) == (
        ROOT,
        "$reply:localhost",
        2_000,
    )
    for part in seg_plain.continuations:
        assert part.get(RESOLVED_IN_TURN_KEY) is True
        assert extract_auto_reopen_reply(_reply(sender=BOT, content=part), sender_is_managed=_is_managed) is None

    new_msg = build_message_content(large_body, extra_content={RESOLVED_IN_TURN_KEY: True, "m.relates_to": THREAD_REL})
    seg_new = segment_matrix_content(
        new_msg,
        room_encrypted=False,
        continuation_thread_id=ROOT,
        continuation_reply_to_event_id="$cause",
    )
    assert seg_new is not None
    assert seg_new.continuations
    for part in (seg_new.first, *seg_new.continuations):
        assert part.get(RESOLVED_IN_TURN_KEY) is True
        assert extract_auto_reopen_reply(_reply(sender=BOT, content=part), sender_is_managed=_is_managed) is None
