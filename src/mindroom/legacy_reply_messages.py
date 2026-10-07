"""What an earlier release left in flight, made into reply records: its presentations, and what only Matrix shows.

``event_journal.legacy_reply_messages`` adopts an earlier release's replies from the database
at the first start with reply records. This module encodes what that release stored
into presentations for it, and reads, after each room syncs, what only the
reply's Matrix event knows: what it showed, and for a reply its stream
created directly, which event that was. A read that keeps
failing gives up with the presentation unknown, which a replay answers with
the "unknown attempt" account.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from nio.exceptions import EncryptionError, RemoteProtocolError

from mindroom import reply_lifecycle as rl
from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
)
from mindroom.event_journal.legacy_reply_messages import LegacyPausedAnswer, LegacyPresentations
from mindroom.matrix.client_visible_messages import fetch_latest_visible_message
from mindroom.matrix.room_history_reads import find_response_event_ids_via_room_messages
from mindroom.reply_presentation import (
    AGENT_PLACEHOLDER,
    TEAM_PLACEHOLDER,
    Presentation,
    Segment,
    decode_presentation,
    encode_presentation,
)
from mindroom.streaming import unfinished_streamed_reply
from mindroom.tool_system.events import deserialize_tool_trace, tool_trace_from_content

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from typing import Any

    import nio
    import structlog

    from mindroom.event_journal import ApprovalContinuation, MatrixDelivery, PrincipalStore
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.events import ToolTraceEntry

# LEGACY_COMPAT: Matrix events of replies an earlier release left in flight.
# Legacy format: a reply adopted with legacy_pending set, whose event an earlier release streamed into directly, so only
# the event's latest edit holds what it showed and its io.mindroom.stream_status says whether it ended.
# Last legacy release: v2026.10.199; replacement: the unreleased durable reply messages record every write ahead of
# sending it, so no reply needs its event read back.
# Handling: after the reply's room syncs, its event is read once per recovery pass, up to three passes, and the read
# becomes its presentation; a stream that ended without a terminal status within that release's six-hour stale-stream
# window gets the restart note its startup cleanup gave, and claims and notes wait for the read; only an event showing
# nothing but the placeholder may later be redacted as one. That release's own Stop reaction on such a stream, which
# its cleanup redacted, stays.
# Coverage: tests/test_legacy_reply_messages.py::test_reads_after_sync_record_what_the_event_showed,
# tests/test_legacy_reply_messages.py::test_a_superseded_replay_removes_an_adopted_event_only_when_it_showed_the_placeholder,
# tests/test_legacy_reply_messages.py::test_a_settled_stream_gets_the_restart_note_only_within_the_stale_stream_window.
_STALE_STREAM_LOOKBACK_MS = 6 * 60 * 60 * 1000
# Passes a read is retried in before its reply proceeds with what it showed unknown.
_READ_ATTEMPTS = 3

_ENDED_AS = {
    STREAM_STATUS_COMPLETED: rl.ReplyState.COMPLETED,
    STREAM_STATUS_CANCELLED: rl.ReplyState.CANCELLED,
    STREAM_STATUS_ERROR: rl.ReplyState.FAILED,
    STREAM_STATUS_INTERRUPTED: rl.ReplyState.FAILED,
}


def _answer(text: str, span_id: str, *, placeholder: str, tool_trace: tuple[ToolTraceEntry, ...] = ()) -> str:
    if not text and not tool_trace:
        return encode_presentation(Presentation(placeholder=placeholder))
    answer = Segment(kind="answer", text=text, span_id=span_id, tool_trace=tool_trace)
    return encode_presentation(Presentation(segments=(answer,), placeholder=placeholder))


def _paused(continuation: ApprovalContinuation, answer: LegacyPausedAnswer, span_id: str) -> str:
    """What a waiting approval showed: the paused text and trace the continuation kept."""
    presentation = Presentation(
        segments=(
            Segment(
                kind="answer",
                text=answer.text,
                span_id=span_id,
                tool_trace=tuple(deserialize_tool_trace(answer.tool_trace)),
                team_state=answer.team_state,
            ),
        )
        if answer.text or answer.tool_trace
        else (),
        placeholder=TEAM_PLACEHOLDER if continuation.entity_kind == "team" else AGENT_PLACEHOLDER,
        show_tool_calls=continuation.show_tool_calls,
    )
    return encode_presentation(presentation)


def _answered(delivery: MatrixDelivery, span_id: str) -> str:
    """What a frozen answer row of an earlier release shows."""
    content: Mapping[str, object] = delivery.payload
    new_content = content.get("m.new_content")
    if isinstance(new_content, dict):
        content = cast("Mapping[str, object]", new_content)
    body = content.get("body")
    return _answer(str(body) if isinstance(body, str) else "", span_id, placeholder=AGENT_PLACEHOLDER)


LEGACY_PRESENTATIONS = LegacyPresentations(
    empty=lambda team: encode_presentation(Presentation(placeholder=TEAM_PLACEHOLDER if team else AGENT_PLACEHOLDER)),
    paused=_paused,
    answered=_answered,
)


def _canonical(source: Mapping[str, Any]) -> bool:
    content = source.get("content")
    return not isinstance(content, dict) or content.get(VISIBLE_ROUTER_VOICE_ECHO_KEY) is not True


@dataclass
class LegacyReplyReads:
    """Read what only Matrix knows about an earlier release's replies, after their rooms sync."""

    store: PrincipalStore
    client: Callable[[], nio.AsyncClient]
    response_sender: Callable[[], str]
    trusted_sender_ids: Callable[[], Collection[str]]
    logger: structlog.stdlib.BoundLogger
    # Wakes what waited for a reply: claims held for its read, and the notes it now owes.
    resolved: Callable[[str], None]
    _failures: dict[str, int] = field(default_factory=dict, init=False)

    async def run(self) -> None:
        """Read every earlier-release reply still waiting, giving up on one after a few failed passes."""
        for reply, last in await self.store.legacy_reply_reads():
            try:
                read = await self._read(reply, last)
            except Exception:
                # A room history the bot cannot read must not hold up the rest of its recovery.
                self.logger.exception("legacy_reply_read_failed", reply_id=reply.reply_id)
                read = None
            if read is None:
                failures = self._failures.get(reply.reply_id, 0) + 1
                self._failures[reply.reply_id] = failures
                if failures < _READ_ATTEMPTS:
                    continue
                self.logger.warning("legacy_reply_read_gave_up", reply_id=reply.reply_id, event_id=reply.event_id)
                read = rl.LegacyRead()
            self._failures.pop(reply.reply_id, None)
            await self.store.finish_legacy_reply_read(reply.reply_id, read, now_ns=time.time_ns())
            self.resolved(reply.reply_id)

    async def _read(self, reply: rl.Reply, last: rl.Span) -> rl.LegacyRead | None:
        """Return what the reply's event showed, or ``None`` when this pass could not tell."""
        placeholder = decode_presentation(reply.presentation).placeholder
        event_id = reply.event_id
        if reply.legacy_pending is rl.LegacyPending.ADOPTION_SCAN:
            found = await find_response_event_ids_via_room_messages(
                self.client(),
                reply.room_id,
                response_sender=self.response_sender(),
                source_event_ids=last.sources.logical,
                response_source_filter=_canonical,
            )
            if len(found) != 1:
                # Nothing visible, or more than one candidate: the replay creates its own.
                return rl.LegacyRead()
            event_id = next(iter(found))
        assert event_id is not None, "a presentation read has the event it reads"
        try:
            message = await fetch_latest_visible_message(
                self.client(),
                room_id=reply.room_id,
                event_id=event_id,
                trusted_sender_ids=self.trusted_sender_ids(),
            )
        except (EncryptionError, RemoteProtocolError):
            # A key this device lacks, or edits the server will not list, may never change.
            message = None
        if message is None:
            return None
        return self._shown(message, event_id, last.span_id, placeholder=placeholder)

    @staticmethod
    def _shown(message: ResolvedVisibleMessage, event_id: str, span_id: str, *, placeholder: str) -> rl.LegacyRead:
        """What the event shows: the work a stopped stream left visible, or that the reply already ended."""
        unfinished = unfinished_streamed_reply(message.body, message.content)
        shown = (
            None
            if unfinished is None
            else _answer(unfinished.visible_text, span_id, placeholder=placeholder, tool_trace=unfinished.tool_trace)
        )
        latest_ms = message.edited_timestamp or message.timestamp
        return rl.LegacyRead(
            shown=shown,
            event_id=event_id,
            ended_as=_ENDED_AS.get(message.stream_status or ""),
            recent=latest_ms >= int(time.time() * 1000) - _STALE_STREAM_LOOKBACK_MS,
            # An ended stream's note or partial text is real content, not a placeholder.
            placeholder_only=message.body.strip() == placeholder and not tool_trace_from_content(message.content),
        )
