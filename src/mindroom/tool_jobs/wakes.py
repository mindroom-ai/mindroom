"""A waiting reply's wake: its journal source, and the turn that continues the reply with background work results."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.hooks.context import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.event_journal import JournalEvent
    from mindroom.reply_lifecycle import Reply
    from mindroom.tool_jobs.completion import HoldKey
    from mindroom.tool_jobs.runtime import BackgroundJob

_RELEASE = "release"
# How many times one waiting message continues with results; the requester's next answered message takes the rest.
WAKE_LIMIT = 20
# What a wake a restart cut short continues with, once what it retrieved is no longer ready to retrieve again.
WAKE_RETRY_PROMPT = (
    "Internal runtime update, not a new human request. A restart interrupted continuing this conversation with "
    "background work results; continue the conversation."
)


def wake_event_id(reply: Reply, ready: Sequence[BackgroundJob]) -> str:
    """Name the wake for one set of ready outcomes, or the one that ends this wait of the reply, as no work is left.

    Admitting the same wake again changes nothing, and outcomes that become ready later name a new one; each wait,
    told apart by the reply's revision, has its own wakes, so an outcome a wake left unretrieved can wake a later one.
    """
    if not ready:
        return f"job-wake:{reply.reply_id}:{_RELEASE}:{reply.revision}"
    digest = hashlib.sha256(",".join(sorted(job.job_id for job in ready)).encode()).hexdigest()[:16]
    return f"job-wake:{reply.reply_id}:{digest}:{reply.revision}"


def ends_wait(wake_id: str) -> bool:
    """Whether a wake was admitted to end a wait no work was left for."""
    return f":{_RELEASE}:" in wake_id


def wake_event(reply: Reply, wake_id: str, *, sender_id: str, now_ms: int) -> InboundEvent:
    """Return the journal source of a waiting reply's wake, which is not a Matrix event."""
    return InboundEvent(
        event_id=wake_id,
        room_id=reply.room_id,
        thread_id=reply.thread_id,
        kind=EventKind.JOB_WAKE,
        event_class=EventClass.ACTIONABLE,
        sender=sender_id,
        origin_server_ts=now_ms,
        source={"reply_id": reply.reply_id},
    )


def woken_reply_id(event: JournalEvent) -> str | None:
    """Return the reply a wake continues."""
    reply_id = event.source.get("reply_id")
    return reply_id if isinstance(reply_id, str) else None


def wake_envelope(key: HoldKey, *, wake_id: str, sender_id: str, prompt: str) -> MessageEnvelope:
    """Address a wake to its reply's conversation, as runtime work on behalf of the reply's requester."""
    return MessageEnvelope(
        source_event_id=wake_id,
        # The wake is not a Matrix event, so it replies to nothing; it continues the waiting message.
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
            intent=TurnIntent.JOB_WAKE,
            source_kind=MESSAGE_SOURCE_KIND,
            trust=TurnTrust.TRUSTED_INTERNAL,
        ),
    )
