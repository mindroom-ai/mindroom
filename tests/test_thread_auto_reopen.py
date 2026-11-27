"""Pure decision rules and segmented continuation protection for automatic thread reopen."""

from __future__ import annotations

import pytest

from mindroom.constants import (
    RESOLVED_IN_TURN_KEY,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
)
from mindroom.matrix.message_builder import build_matrix_edit_content, build_message_content
from mindroom.matrix.segmented_messages import segment_matrix_content
from mindroom.thread_auto_reopen import event_origin_server_ts, extract_auto_reopen_reply
from mindroom.thread_snooze import THREAD_SNOOZE_CONTENT_KEY

ROOT, HUMAN, BOT = "$root:localhost", "@alice:localhost", "@mindroom_code:localhost"
THREAD_REL = {"rel_type": "m.thread", "event_id": ROOT}
EDIT_REL = {"rel_type": "m.replace", "event_id": "$old"}


def _is_managed(sender: str) -> bool:
    return sender == BOT


def _msg(msgtype: str = "m.text", **extra: object) -> dict[str, object]:
    return {"msgtype": msgtype, "body": "x", "m.relates_to": THREAD_REL, **extra}


def _reply(
    *,
    event_type: str = "m.room.message",
    sender: str = HUMAN,
    event_id: str = "$reply:localhost",
    origin_server_ts: object = 2_000,
    content: object | None = None,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "type": event_type,
        "event_id": event_id,
        "sender": sender,
        "origin_server_ts": origin_server_ts,
        "content": _msg() if content is None else content,
        **(extra or {}),
    }


def test_event_origin_server_ts_rejects_booleans_and_non_ints() -> None:
    """Only genuine integer timestamps are accepted."""
    assert event_origin_server_ts({"origin_server_ts": 1234}) == 1234
    assert event_origin_server_ts({"origin_server_ts": True}) is None
    assert event_origin_server_ts({"origin_server_ts": "1234"}) is None
    assert event_origin_server_ts({}) is None


_QUALIFYING = [
    *(
        (mt, _reply(content=_msg(mt)))
        for mt in ("m.text", "m.emote", "m.image", "m.audio", "m.video", "m.file", "m.location")
    ),
    ("sticker", _reply(event_type="m.sticker", content={"body": "cat", "m.relates_to": THREAD_REL})),
    ("pending", _reply(sender=BOT, content=_msg("m.notice", **{STREAM_STATUS_KEY: STREAM_STATUS_PENDING}))),
    ("streaming", _reply(sender=BOT, content=_msg("m.notice", **{STREAM_STATUS_KEY: STREAM_STATUS_STREAMING}))),
    ("approval", _reply(event_type="io.mindroom.tool_approval", sender=BOT, content={"m.relates_to": THREAD_REL})),
    ("human-spoof-resolved", _reply(sender=HUMAN, content=_msg(**{RESOLVED_IN_TURN_KEY: True}))),
    ("human-spoof-snooze", _reply(sender=HUMAN, content=_msg(**{THREAD_SNOOZE_CONTENT_KEY: {"event": "ended"}}))),
    ("human-spoof-voice-echo", _reply(sender=HUMAN, content=_msg(**{VISIBLE_ROUTER_VOICE_ECHO_KEY: True}))),
]

_IGNORED = [
    ("state-event", _reply(extra={"state_key": ""})),
    ("bool-ts", _reply(origin_server_ts=True)),
    ("non-dict-content", _reply(content="not-a-dict")),
    ("non-thread", _reply(content={"msgtype": "m.text", "body": "room msg"})),
    ("self-root", _reply(event_id=ROOT)),
    ("edit", _reply(content={"msgtype": "m.text", "body": "* e", "m.relates_to": EDIT_REL})),
    ("approval-edit", _reply(event_type="io.mindroom.tool_approval", sender=BOT, content={"m.relates_to": EDIT_REL})),
    ("managed-snooze", _reply(sender=BOT, content=_msg(**{THREAD_SNOOZE_CONTENT_KEY: {"event": "ended"}}))),
    ("managed-resolved-in-turn", _reply(sender=BOT, content=_msg(**{RESOLVED_IN_TURN_KEY: True}))),
    ("managed-voice-echo", _reply(sender=BOT, content=_msg(**{VISIBLE_ROUTER_VOICE_ECHO_KEY: True}))),
    ("managed-plain-notice", _reply(sender=BOT, content=_msg("m.notice"))),
    ("completed-notice", _reply(sender=BOT, content=_msg("m.notice", **{STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED}))),
    ("human-streaming", _reply(sender=HUMAN, content=_msg("m.notice", **{STREAM_STATUS_KEY: STREAM_STATUS_STREAMING}))),
    (
        "human-approval",
        _reply(event_type="io.mindroom.tool_approval", sender=HUMAN, content={"m.relates_to": THREAD_REL}),
    ),
    ("unhashable-msgtype", _reply(content={"msgtype": ["bad"], "body": "x", "m.relates_to": THREAD_REL})),
    *(
        (et, _reply(event_type=et, sender=BOT, content={"m.relates_to": THREAD_REL}))
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
    ),
]


@pytest.mark.parametrize(("case_id", "source"), _QUALIFYING, ids=[c[0] for c in _QUALIFYING])
def test_extract_auto_reopen_reply_accepts_qualifying_replies(case_id: str, source: dict[str, object]) -> None:
    """Every qualifying new thread reply returns (thread_root_id, event_id, origin_server_ts)."""
    del case_id
    assert extract_auto_reopen_reply(source, sender_is_managed=_is_managed) == (ROOT, "$reply:localhost", 2_000)


@pytest.mark.parametrize(("case_id", "source"), _IGNORED, ids=[c[0] for c in _IGNORED])
def test_extract_auto_reopen_reply_ignores_non_qualifying_events(case_id: str, source: dict[str, object]) -> None:
    """Edits, notices, bookkeeping events, and managed markers never trigger auto-reopen."""
    del case_id
    assert extract_auto_reopen_reply(source, sender_is_managed=_is_managed) is None


def test_segmented_continuations_never_reopen() -> None:
    """Segmented continuations carry RESOLVED_IN_TURN_KEY while plain first segments still reopen."""
    body = "\n\n".join(f"Paragraph {i}: {'word ' * 15}" for i in range(600))

    def seg(content: dict[str, object]) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
        res = segment_matrix_content(
            content,
            room_encrypted=False,
            continuation_thread_id=ROOT,
            continuation_reply_to_event_id="$c",
        )
        assert res is not None
        assert res.continuations
        return res.first, res.continuations

    _, edit_parts = seg(build_matrix_edit_content("$p", build_message_content(body)))
    assert all(
        extract_auto_reopen_reply(_reply(sender=BOT, content=p), sender_is_managed=_is_managed) is None
        for p in edit_parts
    )

    first, plain_parts = seg(build_message_content(body, extra_content={"m.relates_to": THREAD_REL}))
    assert extract_auto_reopen_reply(_reply(sender=BOT, content=first), sender_is_managed=_is_managed) == (
        ROOT,
        "$reply:localhost",
        2_000,
    )
    assert all(
        extract_auto_reopen_reply(_reply(sender=BOT, content=p), sender_is_managed=_is_managed) is None
        for p in plain_parts
    )

    res_first, res_parts = seg(
        build_message_content(body, extra_content={RESOLVED_IN_TURN_KEY: True, "m.relates_to": THREAD_REL}),
    )
    assert all(
        extract_auto_reopen_reply(_reply(sender=BOT, content=p), sender_is_managed=_is_managed) is None
        for p in (res_first, *res_parts)
    )
