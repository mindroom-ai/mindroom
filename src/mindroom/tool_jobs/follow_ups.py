"""A follow-up turn: background jobs no reply collected start a new reply of their own, as a scheduled turn does."""

from __future__ import annotations

import hashlib
import html
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


def follow_up_key(event: JournalEvent, recipient: str) -> ConversationKey:
    """Return the conversation a follow-up admitted for ``recipient`` answers in; only `follow_up_event` writes them."""
    return ConversationKey(
        recipient=recipient,
        room_id=event.room_id,
        thread_id=event.thread_id,
        requester_id=str(event.source["requester_id"]),
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


def held_jobs(jobs: Sequence[BackgroundJob]) -> tuple[BackgroundJob, ...]:
    """Return the ended jobs the limit holds for a human message, which `follow_up_jobs` leaves out."""
    return tuple(
        job for job in jobs if job.status != "cancelled" and _follow_up_streak(job.source_event_id) >= _FOLLOW_UP_LIMIT
    )


_MAX_HELD_NOTICE_JOBS = 5
_SHORT_JOB_ID_LENGTH = 8


def _held_job_parts(job: BackgroundJob) -> tuple[str, str]:
    child = job.adapter.get("child") if job.kind == "delegation" and isinstance(job.adapter, dict) else None
    title = child.get("title") if isinstance(child, dict) else None
    name = title.strip() if isinstance(title, str) and title.strip() else job.tool_name
    return name, job.job_id[:_SHORT_JOB_ID_LENGTH]


def held_notice(
    key: ConversationKey,
    jobs: Sequence[BackgroundJob],
    *,
    mention_plain: str = "",
    mention_html: str = "",
) -> tuple[str, str, str]:
    """Return the transaction ID, plain text, and HTML of the notice that the limit holds ``jobs``."""
    noun = "job" if len(jobs) == 1 else "jobs"
    header = (
        f"⏸️ {len(jobs)} background {noun} finished while I was paused after "
        f"{_FOLLOW_UP_LIMIT} automatic follow-ups in a row without a message from you"
    )
    lead_plain = f"{mention_plain} {header}" if mention_plain else header
    lead_html = f"{mention_html} {html.escape(header)}" if mention_html else html.escape(header)
    if len(jobs) == 1:
        name, short_id = _held_job_parts(jobs[0])
        body = f"{lead_plain}: {name} (`{short_id}`). Reply here to let me continue."
        formatted = f"<p>{lead_html}: {html.escape(name)} (<code>{html.escape(short_id)}</code>). Reply here to let me continue.</p>"
    else:
        shown = [_held_job_parts(job) for job in jobs[:_MAX_HELD_NOTICE_JOBS]]
        plain_lines = [f"- {name} (`{short_id}`)" for name, short_id in shown]
        html_lines = [
            f"<li>{html.escape(name)} (<code>{html.escape(short_id)}</code>)</li>" for name, short_id in shown
        ]
        if (remaining := len(jobs) - _MAX_HELD_NOTICE_JOBS) > 0:
            plain_lines.append(f"- and {remaining} more")
            html_lines.append(f"<li>and {remaining} more</li>")
        body = f"{lead_plain}:\n" + "\n".join(plain_lines) + "\n\nReply here to let me continue."
        formatted = (
            f"<p>{lead_html}:</p>\n<ul>\n" + "\n".join(html_lines) + "\n</ul>\n<p>Reply here to let me continue.</p>"
        )
    return _follow_up_event_id(key, jobs, 0), body, formatted
