"""What one thread's history says about its summary, read in one walk."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from mindroom.authorization import (
    ReplyMembershipPendingError,
    classify_responder_candidates_from_cached_room,
    is_sender_allowed_for_responder,
)
from mindroom.constants import THREAD_SUMMARY_CONTENT_KEY
from mindroom.logging_config import get_logger
from mindroom.matrix.conversation_reads import complete_thread_history
from mindroom.requester_identity import is_human_requester_id

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    import nio

    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.matrix.thread_history_result import ThreadHistoryResult

logger = get_logger(__name__)
THREAD_SUMMARY_MAX_LENGTH = 300


class TruncatedThreadHistoryError(RuntimeError):
    """One thread is longer than a single projected page.

    Summaries record the number of messages they were given as the size of the
    thread, and every later pass compares against that number. A page of a
    longer conversation is not that number, and no caller can tell by looking,
    so the loader refuses rather than returning something countable-looking.
    """


@dataclass(frozen=True)
class SummaryState:
    """One thread history classified once: trusted summary notices, authorized human pins, and ordinary messages.

    Only ordinary messages count toward the thread's size and reach the summarizer.
    Scheduling fields come from trusted notices alone, so a person's pin never moves a baseline.
    ``summary_event_ids`` names every message carrying summary metadata, accepted or not, because any of them can be edited into a pin.
    """

    ordinary_messages: tuple[ResolvedVisibleMessage, ...]
    last_summary_count: int
    initial_enrichment_complete: bool
    summary: str | None
    pinned: bool
    newest_generated_at: datetime | None
    summary_event_ids: tuple[str, ...]

    @property
    def message_count(self) -> int:
        """Return how many messages the thread holds, without its summaries."""
        return len(self.ordinary_messages)

    @classmethod
    def from_history(
        cls,
        history: Sequence[ResolvedVisibleMessage],
        *,
        thread_id: str,
        trusted_sender_ids: Collection[str],
        authorize_human: Callable[[str], bool],
    ) -> SummaryState:
        """Classify every message of one thread history in a single walk.

        The current summary and the pin decision are independent newest
        winners in the same order, so an edited summary is current from its
        edit onward. A later ``pinned: false`` summary releases a thread that
        an earlier summary pinned, and summaries that omit the key state no
        pin intent.

        The order follows Matrix event time, so a cold client's later pin wins
        even if it never loaded an earlier release with a skewed
        ``generated_at``. An edit reasserts the pin metadata in its replacement
        content at edit time. ``generated_at`` breaks equal event-time ties
        because history position can reflect a backward scan rather than send
        order. It remains the separate display clock new summaries are ordered
        after.

        Raises ``ReplyMembershipPendingError`` when the room membership that
        authorizes a person's pin is not known yet.
        """
        ordinary: list[ResolvedVisibleMessage] = []
        last_summary_count = 0
        initial_enrichment_complete = False
        newest_summary: tuple[int, datetime, int] | None = None
        summary: str | None = None
        newest_decision: tuple[int, datetime, int] | None = None
        pinned = False
        newest_generated_at: datetime | None = None
        for position, message in enumerate(history):
            metadata = trusted_summary_metadata(message, trusted_sender_ids=trusted_sender_ids)
            if metadata is not None:
                count = metadata.get("message_count")
                if isinstance(count, int) and not isinstance(count, bool):
                    last_summary_count = max(last_summary_count, count)
                if metadata.get("initial_enrichment_complete") is True:
                    initial_enrichment_complete = True
            else:
                metadata = _authorized_human_pin(message, thread_id=thread_id, authorize_human=authorize_human)
                if metadata is None:
                    ordinary.append(message)
                    continue
            generated_at = _parse_generated_at(metadata)
            if generated_at is None:
                continue
            if newest_generated_at is None or generated_at > newest_generated_at:
                newest_generated_at = generated_at
            event_timestamp = message.edited_timestamp if message.edited_timestamp is not None else message.timestamp
            order = (event_timestamp, generated_at, position)
            if newest_summary is None or order > newest_summary:
                newest_summary = order
                text = metadata.get("summary")
                summary = text if isinstance(text, str) and text else message.body
            recorded = metadata.get("pinned")
            if isinstance(recorded, bool) and (newest_decision is None or order > newest_decision):
                newest_decision = order
                pinned = recorded
        return cls(
            ordinary_messages=tuple(ordinary),
            last_summary_count=last_summary_count,
            initial_enrichment_complete=initial_enrichment_complete,
            summary=summary,
            pinned=pinned,
            newest_generated_at=newest_generated_at,
            summary_event_ids=tuple(
                message.event_id for message in history if THREAD_SUMMARY_CONTENT_KEY in message.content
            ),
        )


def summary_state(
    history: Sequence[ResolvedVisibleMessage],
    *,
    thread_id: str,
    trusted_sender_ids: Collection[str],
    authorize_human: Callable[[str], bool],
) -> SummaryState | None:
    """Classify one thread history, or return ``None`` while pin-authorizing membership is pending."""
    try:
        return SummaryState.from_history(
            history,
            thread_id=thread_id,
            trusted_sender_ids=trusted_sender_ids,
            authorize_human=authorize_human,
        )
    except ReplyMembershipPendingError:
        return None


def trusted_summary_metadata(
    message: ResolvedVisibleMessage,
    *,
    trusted_sender_ids: Collection[str],
) -> dict[str, object] | None:
    """Return the summary metadata of a notice a current runtime-owned sender wrote."""
    if message.sender not in trusted_sender_ids:
        return None
    metadata = message.content.get(THREAD_SUMMARY_CONTENT_KEY)
    return metadata if isinstance(metadata, dict) else None


def _parse_generated_at(metadata: dict[str, object]) -> datetime | None:
    """Return the ISO-8601 ``generated_at`` recorded on one summary, when usable."""
    raw = metadata.get("generated_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return None


def _authorized_human_pin(
    message: ResolvedVisibleMessage,
    *,
    thread_id: str,
    authorize_human: Callable[[str], bool],
) -> dict[str, object] | None:
    """Accept a person's manual pin exactly as MindRoom Chat writes it, without trusting its scheduling metadata."""
    metadata = message.content.get(THREAD_SUMMARY_CONTENT_KEY)
    if (
        message.thread_id != thread_id
        or message.content.get("msgtype") != "m.notice"
        or not isinstance(metadata, dict)
        or type(metadata.get("version")) is not int
        or metadata.get("version") != 1
        or metadata.get("model") != "manual"
        or metadata.get("pinned") is not True
    ):
        return None
    summary = metadata.get("summary")
    if (
        not isinstance(summary, str)
        or not summary.strip()
        or len(summary) > THREAD_SUMMARY_MAX_LENGTH
        or message.body != summary
        or _parse_generated_at(metadata) is None
        or not authorize_human(message.sender)
    ):
        return None
    return metadata


def human_summary_authorizer(
    client: nio.AsyncClient,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
) -> Callable[[str], bool]:
    """Reuse tool requester authority across responders sharing a room title."""

    def allowed(sender: str) -> bool:
        if not is_human_requester_id(sender, config, runtime_paths):
            return False
        pending = False
        try:
            if is_sender_allowed_for_responder(
                sender,
                entity_name,
                room_id,
                config,
                runtime_paths,
                membership_index,
                require_resolved_membership=True,
            ):
                return True
        except ReplyMembershipPendingError:
            pending = True
        room = client.rooms.get(room_id)
        if room is None:
            raise ReplyMembershipPendingError
        candidates = classify_responder_candidates_from_cached_room(
            room,
            sender,
            config,
            runtime_paths,
            membership_index,
            require_complete_discovery=True,
        )
        if candidates.allowed:
            return True
        pending = pending or bool(candidates.pending)
        if pending:
            raise ReplyMembershipPendingError
        return False

    return allowed


async def load_complete_thread_history(
    conversation_reader: ConversationReader,
    room_id: str,
    thread_id: str,
) -> ThreadHistoryResult:
    """Load complete thread history from the conversation projection.

    Raises ``TruncatedThreadHistoryError`` for a thread longer than one
    projected page, because summaries write the count they are given down as
    the size of the thread.
    """
    history = await complete_thread_history(conversation_reader, room_id, thread_id)
    if not history.is_full_history:
        msg = f"Thread {room_id}/{thread_id} is longer than one projected page"
        raise TruncatedThreadHistoryError(msg)
    return history


async def current_thread_summary(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_reader: ConversationReader,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
    trusted_sender_ids: Collection[str],
) -> SummaryState | None:
    """Return a thread's summary state under the trust and pin rules summary writers use.

    Returns ``None`` rather than guessing when the complete history cannot be
    read, which always happens for threads longer than one projected page, or
    when room membership needed to authorize a human pin is still pending.
    """
    try:
        thread_history = await load_complete_thread_history(conversation_reader, room_id, thread_id)
    except TruncatedThreadHistoryError:
        return None
    except Exception as exc:
        # A listing reads up to 50 threads, so one warning per failed read, not a traceback.
        logger.warning(
            "Thread history unavailable for current summary",
            room_id=room_id,
            thread_id=thread_id,
            error=str(exc),
        )
        return None
    return current_thread_summary_from_history(
        client,
        room_id,
        thread_id,
        thread_history,
        config=config,
        runtime_paths=runtime_paths,
        entity_name=entity_name,
        membership_index=membership_index,
        trusted_sender_ids=trusted_sender_ids,
    )


def current_thread_summary_from_history(
    client: nio.AsyncClient,
    room_id: str,
    thread_id: str,
    thread_history: Sequence[ResolvedVisibleMessage],
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    entity_name: str,
    membership_index: AgentReplyMembershipIndex,
    trusted_sender_ids: Collection[str],
) -> SummaryState | None:
    """Return the summary state of a thread history the caller already read.

    Applies the same rules as ``current_thread_summary``, which reads the
    history itself. Returns ``None`` when room membership needed to authorize a
    human pin is still pending.
    """
    return summary_state(
        thread_history,
        thread_id=thread_id,
        trusted_sender_ids=trusted_sender_ids,
        authorize_human=human_summary_authorizer(
            client,
            room_id,
            config,
            runtime_paths,
            entity_name,
            membership_index,
        ),
    )
