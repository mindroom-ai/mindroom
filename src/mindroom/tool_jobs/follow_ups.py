"""A follow-up turn: background jobs no reply collected start a new reply of their own, as a scheduled turn does."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.hooks.context import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.event_journal import JournalEvent
    from mindroom.tool_jobs.runtime import BackgroundJob

# How many follow-ups run in a row in a conversation without a human message; after that, results wait for one.
_FOLLOW_UP_LIMIT = 20


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


_FOLLOW_UP_ID_PREFIX = "job-follow-up:"


def _follow_up_event_id(key: ConversationKey, jobs: Sequence[BackgroundJob], streak: int) -> str:
    """Name the follow-up for one set of ended jobs, so admitting it again changes nothing.

    The name ends with ``streak``, how many follow-ups in a row it is, so a job its turn starts can count it from the
    job's own source after the follow-up settled.
    """
    conversation = "\n".join((key.recipient, key.room_id, key.thread_id or "", key.requester_id))
    jobs_digest = _digest(",".join(sorted(job.job_id for job in jobs)))
    return f"{_FOLLOW_UP_ID_PREFIX}{_digest(conversation)}:{jobs_digest}:{streak}"


def next_follow_up_id(key: ConversationKey, jobs: Sequence[BackgroundJob]) -> str:
    """Name the follow-up that reports ``jobs``, one more in a row than the longest chain that started them."""
    return _follow_up_event_id(key, jobs, 1 + max(_follow_up_streak(job.source_event_id) for job in jobs))


def follow_up_event(
    key: ConversationKey,
    follow_up_id: str,
    *,
    sender_id: str,
    now_ms: int,
) -> InboundEvent:
    """Return a follow-up's journal source, which is not a Matrix event."""
    return InboundEvent(
        event_id=follow_up_id,
        room_id=key.room_id,
        thread_id=key.thread_id,
        kind=EventKind.JOB_FOLLOW_UP,
        event_class=EventClass.ACTIONABLE,
        sender=sender_id,
        origin_server_ts=now_ms,
        source={"requester_id": key.requester_id},
    )


def follow_up_key(event: JournalEvent, recipient: str) -> ConversationKey | None:
    """Return the conversation a follow-up admitted for ``recipient`` answers in."""
    requester_id = event.source.get("requester_id")
    if not isinstance(requester_id, str):
        return None
    return ConversationKey(
        recipient=recipient,
        room_id=event.room_id,
        thread_id=event.thread_id,
        requester_id=requester_id,
        silent=False,
    )


def _follow_up_streak(source_event_id: str | None) -> int:
    """Return how many follow-ups in a row the turn that started a job ends, which is none for a human message."""
    if source_event_id is None or not source_event_id.startswith(_FOLLOW_UP_ID_PREFIX):
        return 0
    return int(source_event_id.rsplit(":", 1)[1])


def follow_up_envelope(key: ConversationKey, *, follow_up_id: str, sender_id: str, prompt: str) -> MessageEnvelope:
    """Address a follow-up to its conversation, as runtime work on behalf of the jobs' requester."""
    return MessageEnvelope(
        source_event_id=follow_up_id,
        # The follow-up is not a Matrix event, so its new reply replies to nothing.
        target=MessageTarget.resolve(key.room_id, key.thread_id, None),
        body=prompt,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=key.recipient,
        origin=TurnOrigin(
            transport_sender_id=sender_id,
            requester_id=key.requester_id,
            sender_entity_name=key.recipient,
            requester_entity_name=None,
            sender_kind=SenderKind.MANAGED_ENTITY,
            requester_kind=SenderKind.USER,
            intent=TurnIntent.JOB_FOLLOW_UP,
            source_kind=MESSAGE_SOURCE_KIND,
            trust=TurnTrust.TRUSTED_INTERNAL,
        ),
    )


def follow_up_jobs(jobs: Sequence[BackgroundJob]) -> tuple[BackgroundJob, ...]:
    """Return the ended jobs a follow-up reports.

    A cancellation needs no message of its own, and a job a chain of follow-ups at the limit started waits for a
    human message, without holding back the others.
    """
    return tuple(
        job for job in jobs if job.status != "cancelled" and _follow_up_streak(job.source_event_id) < _FOLLOW_UP_LIMIT
    )
