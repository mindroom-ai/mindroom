"""The reply message that holds its conversation's outstanding background work between turns.

A reply never waits inside its turn for background work. When work it can retrieve is still outstanding at its
response boundary, the turn ends and its message keeps a waiting notice and its Stop button. A wake admitted once
that work changes lets a later turn continue the same message, or release it when nothing is left to wait for.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.constants import (
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
    STREAM_WARMUP_SUFFIX_KEY,
)
from mindroom.delivery_gateway import EditTextRequest
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.hooks import MessageEnvelope
from mindroom.message_target import MessageTarget
from mindroom.streaming import PROGRESS_PLACEHOLDER, StreamingPresentation, build_cancelled_response_update
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES
from mindroom.tool_system.events import deserialize_tool_trace, serialize_tool_trace
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from mindroom.cancellation import CancelSource
    from mindroom.event_journal import SavedHeldReply
    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime

_WAITING_NOTICE = "⏳ Waiting for background work…"
_APPROVAL_NOTICE = "⏳ Waiting for approval…"
_WAKE_EVENT_ID = re.compile(r"held-reply:(?P<hold_id>[0-9a-f]{32}):(?P<generation>[0-9a-f]{32})")


@dataclass(frozen=True)
class HoldKey:
    """Whose outstanding work one reply holds: one recipient's work for one requester in one conversation."""

    recipient: str
    room_id: str
    thread_id: str | None
    requester_id: str
    # Work a silent schedule started is delivered silently, so visible replies never hold it, and the reverse.
    silent: bool
    # The entities whose work the reply retrieves: the agent, or a team's members.
    participants: tuple[str, ...]

    @property
    def hold_id(self) -> str:
        """Name the hold stably, so every reply holding this work replaces the same row."""
        identity = [
            self.recipient,
            self.room_id,
            self.thread_id,
            self.requester_id,
            self.silent,
            list(self.participants),
        ]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class HeldReply:
    """A finished reply whose message holds outstanding work; a silent schedule's hold has no message."""

    key: HoldKey
    target: MessageTarget
    source_kind: str
    message_event_id: str | None
    # The message as its reply finished, without the waiting notice.
    presentation: StreamingPresentation
    extra_content: dict[str, Any]
    # What the message shows it waits for.
    notice: str
    stop_button_event_id: str | None
    # Ready results this message already continued with, across its own turn and the turns continuing it.
    joins: int
    # Outcomes its turns already asked for; one the model left unread waits for the conversation's next reply.
    offered: frozenset[str] = frozenset()
    # The save that wrote this hold, set once it is saved.
    generation: str = ""


def holds_job(key: HoldKey, job: BackgroundJob) -> bool:
    """Whether a reply holding ``key`` holds this job, whatever its state."""
    owner = job.owner
    return (
        owner.recipient == key.recipient
        and owner.room_id == key.room_id
        and owner.resolved_thread_id == key.thread_id
        and owner.requester_id == key.requester_id
        and (job.source_kind == SILENT_SCHEDULE_SOURCE_KIND) == key.silent
        and owner.agent_name in key.participants
    )


@dataclass(frozen=True)
class _HeldWork:
    """The outstanding work a message holds, and the ready outcomes of it a turn may retrieve now."""

    jobs: tuple[BackgroundJob, ...]
    ready: tuple[BackgroundJob, ...]


async def conversation_work(
    runtime: ToolJobRuntime,
    key: HoldKey,
    *,
    attempted: Collection[str] = (),
) -> _HeldWork:
    """Return the outstanding work a reply holding ``key`` holds, apart from outcomes it already asked for."""
    held = [
        (job, readable)
        for job, readable in await runtime.held_jobs(
            transport_agent_name=key.recipient,
            room_id=key.room_id,
            thread_id=key.thread_id,
            requester_id=key.requester_id,
            source_kind=SILENT_SCHEDULE_SOURCE_KIND if key.silent else None,
        )
        if job.owner.agent_name in key.participants and job.job_id not in attempted
    ]
    return _HeldWork(
        jobs=tuple(job for job, _readable in held),
        ready=tuple(job for job, readable in held if readable and job.status in TERMINAL_STATUSES),
    )


def waiting_notice(jobs: Sequence[BackgroundJob]) -> str:
    """Name what a held message waits for."""
    return _APPROVAL_NOTICE if any(job.status == "awaiting_approval" for job in jobs) else _WAITING_NOTICE


def encode_held_reply(hold: HeldReply) -> str:
    """Serialize a hold's snapshot; the store owns its generation and wake marker."""
    key = hold.key
    return json.dumps(
        {
            "key": [key.recipient, key.room_id, key.thread_id, key.requester_id, key.silent, list(key.participants)],
            "target": hold.target.to_metadata(),
            "source_kind": hold.source_kind,
            "message_event_id": hold.message_event_id,
            "response_text": hold.presentation.response_text,
            "tool_trace": serialize_tool_trace(hold.presentation.tool_trace, include_internal=True),
            "extra_content": hold.extra_content,
            "notice": hold.notice,
            "stop_button_event_id": hold.stop_button_event_id,
            "joins": hold.joins,
            "offered": sorted(hold.offered),
        },
    )


def _restored(saved: SavedHeldReply) -> HeldReply | None:
    payload = json.loads(saved.hold_json)
    recipient, room_id, thread_id, requester_id, silent, participants = payload["key"]
    target = MessageTarget.from_metadata(payload["target"])
    if target is None:
        return None
    return HeldReply(
        key=HoldKey(recipient, room_id, thread_id, requester_id, silent, tuple(participants)),
        target=target,
        source_kind=payload["source_kind"],
        message_event_id=payload["message_event_id"],
        presentation=StreamingPresentation(
            response_text=payload["response_text"],
            tool_trace=tuple(deserialize_tool_trace(payload["tool_trace"])),
        ),
        extra_content=dict(payload["extra_content"]),
        notice=payload["notice"],
        stop_button_event_id=payload["stop_button_event_id"],
        joins=int(payload["joins"]),
        offered=frozenset(payload["offered"]),
        generation=saved.generation,
    )


def decode_held_reply(saved: SavedHeldReply) -> HeldReply:
    """Restore a saved hold, raising ``ValueError`` for a snapshot this runtime cannot use."""
    msg = f"Invalid held reply {saved.hold_id!r}."
    try:
        hold = _restored(saved)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(msg) from error
    if hold is None or hold.key.hold_id != saved.hold_id:
        raise ValueError(msg)
    return hold


def held_edit(hold: HeldReply) -> EditTextRequest:
    """Show that the message still holds work: its text, the waiting notice, and a streaming status."""
    assert hold.message_event_id is not None
    visible = hold.presentation.response_text.strip() or PROGRESS_PLACEHOLDER
    return EditTextRequest(
        target=hold.target,
        event_id=hold.message_event_id,
        new_text=f"{visible}\n\n{hold.notice}",
        tool_trace=list(hold.presentation.tool_trace),
        extra_content={
            **hold.extra_content,
            STREAM_STATUS_KEY: STREAM_STATUS_STREAMING,
            STREAM_WARMUP_SUFFIX_KEY: hold.notice,
        },
    )


def released_edit(hold: HeldReply) -> EditTextRequest:
    """Restore the message as its reply finished, now that it holds nothing."""
    assert hold.message_event_id is not None
    return EditTextRequest(
        target=hold.target,
        event_id=hold.message_event_id,
        new_text=hold.presentation.response_text.strip() or PROGRESS_PLACEHOLDER,
        tool_trace=list(hold.presentation.tool_trace),
        extra_content={**hold.extra_content, STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED},
    )


def ended_edit(hold: HeldReply, *, cancel_source: CancelSource) -> EditTextRequest:
    """Show that a Stop or an interruption ended what the message held, keeping what the reply already said."""
    assert hold.message_event_id is not None
    text, stream_status = build_cancelled_response_update(hold.presentation.response_text, cancel_source=cancel_source)
    return EditTextRequest(
        target=hold.target,
        event_id=hold.message_event_id,
        new_text=text,
        tool_trace=list(hold.presentation.tool_trace),
        extra_content={**hold.extra_content, STREAM_STATUS_KEY: stream_status},
    )


def _wake_event_id(hold: HeldReply) -> str:
    """Name the wake for one generation of a hold; any later save of the hold makes it a no-op."""
    return f"held-reply:{hold.key.hold_id}:{hold.generation}"


def parse_wake_event_id(event_id: str) -> tuple[str, str] | None:
    """Return the hold ID and generation a wake names, or ``None`` for any other event."""
    match = _WAKE_EVENT_ID.fullmatch(event_id)
    return None if match is None else (match["hold_id"], match["generation"])


def wake_event(hold: HeldReply, *, sender_id: str) -> InboundEvent:
    """Admit a turn for this hold without manufacturing a Matrix timeline event."""
    return InboundEvent(
        event_id=_wake_event_id(hold),
        room_id=hold.key.room_id,
        thread_id=hold.key.thread_id,
        kind=EventKind.HELD_REPLY_WAKE,
        event_class=EventClass.ACTIONABLE,
        sender=sender_id,
        origin_server_ts=int(time.time() * 1000),
        source={},
    )


def continuation_envelope(hold: HeldReply, *, source_event_id: str, sender_id: str, prompt: str) -> MessageEnvelope:
    """Address a held reply's continuation to its conversation, as runtime work on behalf of its requester."""
    return MessageEnvelope(
        source_event_id=source_event_id,
        # The wake is not a Matrix event, so the continuation replies to nothing; it edits the held message.
        target=replace(hold.target, reply_to_event_id=None),
        body=prompt,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=hold.key.recipient,
        origin=TurnOrigin(
            transport_sender_id=sender_id,
            requester_id=hold.key.requester_id,
            sender_entity_name=hold.key.recipient,
            requester_entity_name=None,
            sender_kind=SenderKind.MANAGED_ENTITY,
            requester_kind=SenderKind.USER,
            intent=TurnIntent.HELD_REPLY_CONTINUATION,
            source_kind=hold.source_kind,
            trust=TurnTrust.TRUSTED_INTERNAL,
        ),
    )
