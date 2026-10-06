"""What main left in flight, made into reply records: its presentations, and what only Matrix shows.

``event_journal.legacy_reply_messages`` adopts main's replies from the database
at the first start with reply records. This module encodes what main stored
into presentations for it, and reads, after each room syncs, what only the
reply's Matrix event knows: what it showed, and for a reply its stream
created directly, which event that was (DESIGN.md §14.5). A read that keeps
failing gives up with the presentation unknown, which a replay answers with
main's "unknown attempt" account.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nio.exceptions import EncryptionError, RemoteProtocolError

from mindroom import reply_lifecycle as rl
from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_INTERRUPTED,
    VISIBLE_ROUTER_VOICE_ECHO_KEY,
)
from mindroom.event_journal.legacy_reply_messages import LegacyPresentations
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
from mindroom.tool_system.events import deserialize_tool_trace

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping
    from typing import Any

    import nio
    import structlog

    from mindroom.event_journal import ApprovalContinuation, MatrixDelivery, PrincipalStore
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.events import ToolTraceEntry

# Main's startup cleanup gave a restart note only to streams it found this
# recent (matrix/stale_stream_cleanup.py, removed with reply records).
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


def _paused(continuation: ApprovalContinuation, span_id: str) -> str:
    """What a waiting approval showed: the paused text and trace main kept in its continuation."""
    presentation = Presentation(
        segments=(
            Segment(
                kind="answer",
                text=continuation.response_text,
                span_id=span_id,
                tool_trace=tuple(deserialize_tool_trace(continuation.response_tool_trace)),
                team_state=continuation.response_presentation_state or None,
            ),
        )
        if continuation.response_text or continuation.response_tool_trace
        else (),
        placeholder=TEAM_PLACEHOLDER if continuation.entity_kind == "team" else AGENT_PLACEHOLDER,
        show_tool_calls=continuation.show_tool_calls,
    )
    return encode_presentation(presentation)


def _answered(delivery: MatrixDelivery, span_id: str) -> str:
    """What a frozen main-era answer row shows."""
    content: Mapping[str, object] = delivery.payload
    new_content = content.get("m.new_content")
    if isinstance(new_content, dict):
        content = new_content
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
    """Read what only Matrix knows about main's replies, after their rooms sync."""

    store: PrincipalStore
    client: Callable[[], nio.AsyncClient]
    response_sender: Callable[[], str]
    trusted_sender_ids: Callable[[], Collection[str]]
    logger: structlog.stdlib.BoundLogger
    # Wakes what waited for a reply: claims held for its read, and the notes it now owes.
    resolved: Callable[[str], None]
    _failures: dict[str, int] = field(default_factory=dict, init=False)

    async def run(self) -> None:
        """Read every main-era reply still waiting, giving up on one after a few failed passes."""
        for reply, last in await self.store.legacy_reply_reads():
            read = await self._read(reply, last)
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
        )
