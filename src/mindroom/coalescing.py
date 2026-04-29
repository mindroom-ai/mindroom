"""Live message coalescing gate and batch helpers."""

from __future__ import annotations

import asyncio
import enum
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, TypeGuard, cast

import nio

from .attachments import merge_attachment_ids, parse_attachment_ids_from_event_source
from .commands.parsing import command_parser
from .constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY, VOICE_RAW_AUDIO_FALLBACK_KEY
from .hooks.ingress import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    is_voice_event,
)
from .logging_config import get_logger
from .matrix.media import extract_media_caption
from .timing import emit_elapsed_timing, event_timing_scope

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.response_lifecycle import QueuedHumanNoticeReservation

__all__ = [
    "COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP",
    "COALESCING_BYPASS_TRUSTED_INTERNAL_RELAY",
    "CoalescedBatch",
    "CoalescingGate",
    "CoalescingKey",
    "DispatchEvent",
    "GatePhase",
    "MediaDispatchEvent",
    "PendingEvent",
    "PreparedTextEvent",
    "TextDispatchEvent",
    "build_batch_dispatch_event",
    "build_coalesced_batch",
    "coalesced_prompt",
    "is_coalescing_exempt_source_kind",
]

_UPLOAD_GRACE_HARD_CAP_MULTIPLIER = 4.0
_UPLOAD_GRACE_MAX_HARD_CAP_SECONDS = 2.0
_COALESCING_FLUSH_WARNING_SECONDS = 5.0
COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP = ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
COALESCING_BYPASS_TRUSTED_INTERNAL_RELAY = TRUSTED_INTERNAL_RELAY_SOURCE_KIND
_COALESCING_EXEMPT_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        "hook",
        "hook_dispatch",
        COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
        COALESCING_BYPASS_TRUSTED_INTERNAL_RELAY,
    },
)
logger = get_logger(__name__)


class GatePhase(enum.Enum):
    """Lifecycle phases for one coalescing gate."""

    DEBOUNCE = "debounce"
    GRACE = "grace"
    IN_FLIGHT = "in_flight"


class _QueueKind(enum.Enum):
    """Dispatch behavior for one queued event."""

    NORMAL = "normal"
    COMMAND = "command"
    BYPASS = "bypass"


type MediaDispatchEvent = (
    # Voice messages are normalized into PreparedTextEvent before coalescing,
    # so this contract only includes routed image/file/video events.
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
)
type TextDispatchEvent = nio.RoomMessageText | PreparedTextEvent
type DispatchEvent = TextDispatchEvent | MediaDispatchEvent
type CoalescingKey = tuple[str, str | None, str]


@dataclass(frozen=True)
class PreparedTextEvent:
    """Canonical inbound text event for dispatch.

    Produced by voice normalization, coalesced-batch synthesis, and the
    ``_resolve_text_dispatch_event`` preparation step in ``bot.py``.
    Satisfies the ``CommandEvent`` protocol used by command handling.
    """

    sender: str
    event_id: str
    body: str
    source: dict[str, Any]
    server_timestamp: int | float | None = None
    is_synthetic: bool = False
    source_kind_override: str | None = None


@dataclass
class PendingEvent:
    """One queued inbound event waiting to be coalesced."""

    event: DispatchEvent
    room: nio.MatrixRoom
    source_kind: str
    enqueue_time: float = field(default_factory=time.time)
    queued_notice_reservation: QueuedHumanNoticeReservation | None = None


@dataclass
class _QueuedEvent:
    kind: _QueueKind
    pending_event: PendingEvent


@dataclass
class _GateEntry:
    phase: GatePhase = GatePhase.DEBOUNCE
    queue: deque[_QueuedEvent] = field(default_factory=deque)
    drain_task: asyncio.Task[None] | None = None
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake_generation: int = 0
    deadline: float | None = None
    grace_deadline: float | None = None
    drain_all_requested: bool = False


@dataclass(frozen=True)
class CoalescedBatch:
    """One flushed batch ready to dispatch through the text pipeline."""

    room: nio.MatrixRoom
    primary_event: DispatchEvent
    requester_user_id: str
    pending_events: tuple[PendingEvent, ...]
    prompt: str
    source_kind: str
    attachment_ids: list[str]
    source_event_ids: list[str]
    source_event_prompts: dict[str, str]
    media_events: list[MediaDispatchEvent]
    original_sender: str | None = None
    raw_audio_fallback: bool = False
    queued_notice_reservation: QueuedHumanNoticeReservation | None = None


@dataclass(frozen=True)
class _FlushDiagnostics:
    """Stable metadata for one flush attempt."""

    batch: CoalescedBatch
    pending_count: int
    timing_scope: str
    log_context: dict[str, object]


def _event_content_dict(event: DispatchEvent) -> dict[str, object] | None:
    if not isinstance(event.source, dict):
        return None
    content = event.source.get("content")
    if not isinstance(content, dict):
        return None
    return cast("dict[str, object]", content)


def _effective_source_kind(
    event: DispatchEvent,
    fallback_source_kind: str | None = None,
) -> str | None:
    if isinstance(event, PreparedTextEvent) and event.source_kind_override is not None:
        return event.source_kind_override
    return fallback_source_kind


def is_coalescing_exempt_source_kind(
    event: DispatchEvent,
    fallback_source_kind: str | None = None,
) -> bool:
    """Return True when coalescing should be skipped for this event."""
    return _effective_source_kind(event, fallback_source_kind) in _COALESCING_EXEMPT_SOURCE_KINDS


def is_command_event(
    event: DispatchEvent,
    *,
    fallback_source_kind: str | None = None,
) -> bool:
    """Return whether a dispatch event should bypass coalescing as a command."""
    if not isinstance(event, nio.RoomMessageText | PreparedTextEvent):
        return False
    if fallback_source_kind == "voice" or is_voice_event(event):
        return False
    if _effective_source_kind(event, fallback_source_kind) in {"image", "media"}:
        return False
    return command_parser.parse(event.body) is not None


def _is_media_event(event: DispatchEvent) -> TypeGuard[MediaDispatchEvent]:
    return isinstance(
        event,
        nio.RoomMessageImage
        | nio.RoomEncryptedImage
        | nio.RoomMessageFile
        | nio.RoomEncryptedFile
        | nio.RoomMessageVideo
        | nio.RoomEncryptedVideo,
    )


def _pending_has_only_text(pending_events: list[PendingEvent]) -> bool:
    return bool(pending_events) and all(
        isinstance(pending_event.event, nio.RoomMessageText | PreparedTextEvent) for pending_event in pending_events
    )


def _event_batch_sort_key(pending_event: PendingEvent, enqueue_order: int) -> tuple[float, int]:
    enqueue_time_ms = pending_event.enqueue_time * 1000.0
    server_timestamp = pending_event.event.server_timestamp
    if isinstance(server_timestamp, int | float):
        return (float(server_timestamp), enqueue_order)
    return (enqueue_time_ms, enqueue_order)


def coalesced_prompt(message_bodies: list[str]) -> str:
    """Return the single prompt text used to dispatch one coalesced turn."""
    if len(message_bodies) == 1:
        return message_bodies[0]
    combined_body = "\n".join(message_bodies)
    return (
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        f"{combined_body}"
    )


def _dispatch_prompt_for_event(event: DispatchEvent) -> str:
    if isinstance(event, nio.RoomMessageAudio | nio.RoomEncryptedAudio):
        msg = "Raw audio must be normalized into PreparedTextEvent before coalescing"
        raise TypeError(msg)
    if isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage):
        return extract_media_caption(event, default="[Attached image]")
    if isinstance(event, nio.RoomMessageVideo | nio.RoomEncryptedVideo):
        return extract_media_caption(event, default="[Attached video]")
    if isinstance(event, nio.RoomMessageFile | nio.RoomEncryptedFile):
        return extract_media_caption(event, default="[Attached file]")
    return event.body


def _batch_metadata(pending_events: list[PendingEvent]) -> tuple[str | None, bool]:
    original_sender: str | None = None
    raw_audio_fallback = False
    for pending_event in pending_events:
        content = _event_content_dict(pending_event.event)
        if content is None:
            continue
        if original_sender is None:
            content_original_sender = content.get(ORIGINAL_SENDER_KEY)
            if isinstance(content_original_sender, str):
                original_sender = content_original_sender
        if content.get(VOICE_RAW_AUDIO_FALLBACK_KEY) is True:
            raw_audio_fallback = True
        if original_sender is not None and raw_audio_fallback:
            break
    return original_sender, raw_audio_fallback


_SOURCE_KIND_PRIORITY: dict[str, int] = {"voice": 0, "image": 1, "media": 2}


def _batch_source_kind(ordered_pending_events: list[PendingEvent]) -> str:
    resolved_source_kinds = [
        _effective_source_kind(pending_event.event, pending_event.source_kind) or pending_event.source_kind
        for pending_event in ordered_pending_events
    ]
    return min(resolved_source_kinds, key=lambda sk: _SOURCE_KIND_PRIORITY.get(sk, 999))


def _batch_queued_notice_reservation(
    ordered_pending_events: list[PendingEvent],
) -> QueuedHumanNoticeReservation | None:
    reservations = [
        pending_event.queued_notice_reservation
        for pending_event in ordered_pending_events
        if pending_event.queued_notice_reservation is not None
    ]
    if not reservations:
        return None
    if len(ordered_pending_events) == 1:
        return reservations[0]
    for reservation in reservations:
        reservation.cancel()
    msg = "Queued-human notice reservations must dispatch as solo batches"
    raise ValueError(msg)


def _cancel_pending_event_reservations(pending_events: list[PendingEvent]) -> None:
    for pending_event in pending_events:
        if pending_event.queued_notice_reservation is not None:
            pending_event.queued_notice_reservation.cancel()


def _batch_source_event_prompts(ordered_pending_events: list[PendingEvent]) -> dict[str, str]:
    return {
        pending_event.event.event_id: _dispatch_prompt_for_event(pending_event.event)
        for pending_event in ordered_pending_events
    }


def build_coalesced_batch(key: CoalescingKey, pending_events: list[PendingEvent]) -> CoalescedBatch:
    """Build one normalized dispatch batch from queued pending events."""
    ordered_pending_events = [
        pending_event
        for _, pending_event in sorted(
            enumerate(pending_events),
            key=lambda item: _event_batch_sort_key(item[1], item[0]),
        )
    ]
    primary_pending_event = ordered_pending_events[-1]
    original_sender, raw_audio_fallback = _batch_metadata(ordered_pending_events)
    return CoalescedBatch(
        room=primary_pending_event.room,
        primary_event=primary_pending_event.event,
        requester_user_id=key[2],
        pending_events=tuple(ordered_pending_events),
        prompt=coalesced_prompt(
            [_dispatch_prompt_for_event(pending_event.event) for pending_event in ordered_pending_events],
        ),
        source_kind=_batch_source_kind(ordered_pending_events),
        attachment_ids=merge_attachment_ids(
            *(
                parse_attachment_ids_from_event_source(pending_event.event.source)
                for pending_event in ordered_pending_events
            ),
        ),
        source_event_ids=[pending_event.event.event_id for pending_event in ordered_pending_events],
        source_event_prompts=_batch_source_event_prompts(ordered_pending_events),
        media_events=[
            pending_event.event for pending_event in ordered_pending_events if _is_media_event(pending_event.event)
        ],
        original_sender=original_sender,
        raw_audio_fallback=raw_audio_fallback,
        queued_notice_reservation=_batch_queued_notice_reservation(ordered_pending_events),
    )


def _collect_batch_mentions_and_formatted_bodies(
    batch: CoalescedBatch,
) -> tuple[list[str], list[str]]:
    """Collect deduplicated user IDs and formatted_body parts from all batch events."""
    all_user_ids: list[str] = []
    seen_user_ids: set[str] = set()
    formatted_parts: list[str] = []
    for pe in batch.pending_events:
        content = _event_content_dict(pe.event)
        if content is None:
            continue
        raw_mentions = content.get("m.mentions")
        if isinstance(raw_mentions, dict):
            mentions = cast("dict[str, Any]", raw_mentions)
            for uid in mentions.get("user_ids", []):
                if isinstance(uid, str) and uid not in seen_user_ids:
                    all_user_ids.append(uid)
                    seen_user_ids.add(uid)
        fb = content.get("formatted_body")
        if isinstance(fb, str) and fb:
            formatted_parts.append(fb)
    return all_user_ids, formatted_parts


def _merge_batch_source(batch: CoalescedBatch) -> dict[str, Any]:
    """Build a merged ``source`` dict for a multi-event synthetic dispatch event.

    Combines ``m.mentions``, ``formatted_body``, relay/voice metadata, and
    attachment IDs from all events in the batch so downstream dispatch
    (mention detection, attachment handling) sees complete information.
    """
    primary_source: dict[str, Any] = batch.primary_event.source if isinstance(batch.primary_event.source, dict) else {}
    merged: dict[str, Any] = dict(primary_source)
    primary_content: dict[str, Any] = dict(merged.get("content", {})) if isinstance(merged.get("content"), dict) else {}

    all_user_ids, formatted_parts = _collect_batch_mentions_and_formatted_bodies(batch)
    if all_user_ids:
        primary_content["m.mentions"] = {"user_ids": all_user_ids}
    if formatted_parts:
        primary_content["formatted_body"] = "<br>".join(formatted_parts)
        primary_content["format"] = "org.matrix.custom.html"

    # Preserve original_sender and voice_raw_audio_fallback from any event
    if batch.original_sender is not None:
        primary_content[ORIGINAL_SENDER_KEY] = batch.original_sender
    if batch.raw_audio_fallback:
        primary_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True

    # Flow attachment IDs from the batch (must be a list for parse_attachment_ids_from_event_source)
    if batch.attachment_ids:
        primary_content[ATTACHMENT_IDS_KEY] = batch.attachment_ids

    merged["content"] = primary_content
    return merged


def build_batch_dispatch_event(batch: CoalescedBatch) -> TextDispatchEvent:
    """Return the dispatch event for one coalesced batch.

    Single-event batches reuse the primary event directly.
    Multi-event batches produce a ``PreparedTextEvent`` with the combined prompt.
    """
    if len(batch.pending_events) == 1 and isinstance(batch.primary_event, nio.RoomMessageText | PreparedTextEvent):
        if isinstance(batch.primary_event, PreparedTextEvent):
            if batch.source_kind == "message":
                return batch.primary_event
            if batch.primary_event.source_kind_override == batch.source_kind:
                return batch.primary_event
            return replace(batch.primary_event, source_kind_override=batch.source_kind)
        if isinstance(batch.primary_event, nio.RoomMessageText) and batch.source_kind != "message":
            return PreparedTextEvent(
                sender=batch.primary_event.sender,
                event_id=batch.primary_event.event_id,
                body=batch.primary_event.body,
                source=batch.primary_event.source,
                server_timestamp=batch.primary_event.server_timestamp,
                source_kind_override=batch.source_kind,
            )
        return batch.primary_event
    return PreparedTextEvent(
        sender=batch.primary_event.sender,
        event_id=batch.primary_event.event_id,
        body=batch.prompt,
        source=_merge_batch_source(batch),
        server_timestamp=batch.primary_event.server_timestamp,
        is_synthetic=True,
        source_kind_override=batch.source_kind,
    )


class CoalescingGate:
    """Debounce/grace state machine for live inbound message batching.

    State machine per (room, thread, sender) key:
    IDLE (absent) -> DEBOUNCE -> GRACE (optional, wait for images) ->
    flush -> IN_FLIGHT, while all undispatched work remains in one FIFO queue.
    """

    def __init__(
        self,
        *,
        dispatch_batch: Callable[[CoalescedBatch], Awaitable[None]],
        debounce_seconds: Callable[[], float],
        upload_grace_seconds: Callable[[], float],
        is_shutting_down: Callable[[], bool],
    ) -> None:
        self._dispatch_batch = dispatch_batch
        self._debounce_seconds = debounce_seconds
        self._upload_grace_seconds = upload_grace_seconds
        self._is_shutting_down = is_shutting_down
        self._gates: dict[CoalescingKey, _GateEntry] = {}
        self._retired_in_flight_drain_tasks: set[asyncio.Task[None]] = set()

    def is_idle(self) -> bool:
        """Return whether all coalescing gates are currently idle."""
        return not self._gates and not any(not task.done() for task in self._retired_in_flight_drain_tasks)

    def _track_retired_in_flight_drain(self, task: asyncio.Task[None]) -> None:
        self._retired_in_flight_drain_tasks.add(task)
        task.add_done_callback(self._retired_in_flight_drain_tasks.discard)

    def retarget(self, old_key: CoalescingKey, new_key: CoalescingKey) -> None:
        """Re-key one live gate after thread resolution changes the canonical scope."""
        if old_key == new_key:
            return
        gate = self._gates.get(old_key)
        if gate is None:
            return
        existing_gate = self._gates.get(new_key)
        if existing_gate is None:
            self._gates[new_key] = self._gates.pop(old_key)
            self._ensure_drain_task(new_key, gate)
            self._wake(gate)
            return
        if existing_gate is gate:
            self._gates.pop(old_key, None)
            self._gates[new_key] = gate
            self._ensure_drain_task(new_key, gate)
            self._wake(gate)
            return

        source_in_flight = gate.phase is GatePhase.IN_FLIGHT
        destination_in_flight = existing_gate.phase is GatePhase.IN_FLIGHT
        owner_gate = existing_gate if destination_in_flight or not source_in_flight else gate
        retired_gate = gate if owner_gate is existing_gate else existing_gate

        owner_gate.queue = deque(
            sorted(
                [*owner_gate.queue, *retired_gate.queue],
                key=lambda queued: queued.pending_event.enqueue_time,
            ),
        )
        owner_gate.drain_all_requested = owner_gate.drain_all_requested or retired_gate.drain_all_requested
        retired_gate.queue.clear()
        self._gates.pop(old_key, None)
        self._gates[new_key] = owner_gate
        retired_drain_task = retired_gate.drain_task
        if retired_drain_task is not None and not retired_drain_task.done():
            if retired_gate.phase is GatePhase.IN_FLIGHT:
                self._track_retired_in_flight_drain(retired_drain_task)
            elif retired_drain_task is not asyncio.current_task():
                retired_drain_task.cancel()
        self._ensure_drain_task(new_key, owner_gate)
        self._wake(owner_gate)

    def _resolve_gate_entry(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
    ) -> tuple[CoalescingKey | None, _GateEntry | None]:
        """Return the current key and entry for one gate, accounting for retargeting."""
        current_gate = self._gates.get(key)
        if current_gate is gate:
            return key, current_gate
        for current_key, current_gate in self._gates.items():
            if current_gate is gate:
                return current_key, current_gate
        return None, None

    def _get_or_create_gate(self, key: CoalescingKey) -> _GateEntry:
        gate = self._gates.get(key)
        if gate is None:
            gate = _GateEntry()
            self._gates[key] = gate
        return gate

    @staticmethod
    def _gate_work_count(gate: _GateEntry) -> int:
        return len(gate.queue)

    @staticmethod
    def _oldest_pending_age_ms(gate: _GateEntry) -> float | None:
        if not gate.queue:
            return None
        oldest_enqueue_time = min(queued.pending_event.enqueue_time for queued in gate.queue)
        return round((time.time() - oldest_enqueue_time) * 1000, 1)

    @staticmethod
    def _oldest_pending_events_age_ms(pending_events: list[PendingEvent]) -> float:
        oldest_enqueue_time = min(pending_event.enqueue_time for pending_event in pending_events)
        return round((time.time() - oldest_enqueue_time) * 1000, 1)

    @staticmethod
    def _source_event_ids(pending_events: list[PendingEvent]) -> list[str]:
        return [pending_event.event.event_id for pending_event in pending_events]

    @staticmethod
    def _queue_pending_events(gate: _GateEntry, count: int) -> list[PendingEvent]:
        return [gate.queue[index].pending_event for index in range(count)]

    @staticmethod
    def _claim_front_events(gate: _GateEntry, count: int) -> list[PendingEvent]:
        return [gate.queue.popleft().pending_event for _ in range(count)]

    @staticmethod
    def _front_normal_run_length(gate: _GateEntry) -> int:
        count = 0
        for queued in gate.queue:
            if queued.kind is not _QueueKind.NORMAL:
                break
            count += 1
        return count

    @staticmethod
    def _extend_candidate_with_grace_media(gate: _GateEntry, candidate_count: int) -> int:
        count = candidate_count
        while count < len(gate.queue):
            queued = gate.queue[count]
            if queued.kind is not _QueueKind.NORMAL or not _is_media_event(queued.pending_event.event):
                break
            count += 1
        return count

    @staticmethod
    def _has_barrier_after_front_normal_run(gate: _GateEntry) -> bool:
        normal_count = CoalescingGate._front_normal_run_length(gate)
        return normal_count < len(gate.queue)

    @staticmethod
    def _has_item_after_candidate(gate: _GateEntry, candidate_count: int) -> bool:
        return candidate_count < len(gate.queue)

    @staticmethod
    def _queue_kind(pending_event: PendingEvent) -> _QueueKind:
        if is_coalescing_exempt_source_kind(pending_event.event, pending_event.source_kind):
            return _QueueKind.BYPASS
        if is_command_event(pending_event.event, fallback_source_kind=pending_event.source_kind):
            return _QueueKind.COMMAND
        return _QueueKind.NORMAL

    def _enqueue_path(self, kind: _QueueKind) -> str:
        if kind is _QueueKind.BYPASS:
            return "bypass"
        if kind is _QueueKind.COMMAND:
            return "command_interrupt"
        if self._debounce_seconds() <= 0:
            return "zero_debounce"
        return "debounce_schedule"

    def _log_enqueue(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        *,
        enqueue_start: float,
        path: str,
        source_kind: str,
    ) -> None:
        logger.debug(
            "coalescing_gate_enqueue",
            room_id=key[0],
            thread_id=key[1],
            requester_user_id=key[2],
            path=path,
            source_kind=source_kind,
            pending_count=self._gate_work_count(gate),
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
            duration_ms=round((time.monotonic() - enqueue_start) * 1000, 1),
        )

    def _log_enqueued_event(
        self,
        key: CoalescingKey,
        pending_event: PendingEvent,
        *,
        pending_count: int,
    ) -> None:
        logger.info(
            "coalescing_gate_message_enqueued",
            room_id=key[0],
            thread_id=key[1],
            requester_user_id=key[2],
            event_id=pending_event.event.event_id,
            pending_count=pending_count,
            source_kind=pending_event.source_kind,
            timing_scope=event_timing_scope(pending_event.event.event_id),
        )

    def _flush_diagnostics(
        self,
        key: CoalescingKey,
        pending_events: list[PendingEvent],
        *,
        bypass_grace: bool,
    ) -> _FlushDiagnostics:
        batch = build_coalesced_batch(key, pending_events)
        pending_count = len(pending_events)
        timing_scope = event_timing_scope(batch.primary_event.event_id)
        return _FlushDiagnostics(
            batch=batch,
            pending_count=pending_count,
            timing_scope=timing_scope,
            log_context={
                "room_id": key[0],
                "thread_id": key[1],
                "requester_user_id": key[2],
                "pending_count": pending_count,
                "oldest_pending_age_ms": self._oldest_pending_events_age_ms(pending_events),
                "bypass_grace": bypass_grace,
                "source_event_ids": self._source_event_ids(pending_events),
                "timing_scope": timing_scope,
            },
        )

    @staticmethod
    def _log_flush_finished(
        flush_context: dict[str, object],
        *,
        flush_start: float,
        outcome: str,
    ) -> None:
        duration_ms = round((time.monotonic() - flush_start) * 1000, 1)
        log_context = {
            **flush_context,
            "duration_ms": duration_ms,
            "outcome": outcome,
        }
        if duration_ms >= _COALESCING_FLUSH_WARNING_SECONDS * 1000:
            logger.warning("coalescing_gate_flush_slow", **log_context)
            return
        logger.info("coalescing_gate_flush_finished", **log_context)

    def _ensure_drain_task(self, key: CoalescingKey, gate: _GateEntry) -> None:
        if gate.drain_task is not None and not gate.drain_task.done():
            return
        gate.drain_task = asyncio.create_task(
            self._drain_gate(key, gate),
            name=f"coalescing_drain:{key[0]}:{key[1] or 'room'}:{key[2]}",
        )

    def _schedule_drain(self, key: CoalescingKey, gate: _GateEntry) -> None:
        self._ensure_drain_task(key, gate)
        self._wake(gate)

    @staticmethod
    def _wake(gate: _GateEntry) -> None:
        gate.wake_generation += 1
        gate.wake_event.set()

    def _record_enqueue(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        pending_event: PendingEvent,
        enqueue_start: float,
        *,
        path: str,
        flush_outcome: str | None = None,
    ) -> None:
        self._log_enqueued_event(
            key,
            pending_event,
            pending_count=self._gate_work_count(gate),
        )
        self._log_enqueue(
            key,
            gate,
            enqueue_start=enqueue_start,
            path=path,
            source_kind=pending_event.source_kind,
        )
        emit_elapsed_timing(
            "coalescing_gate.enqueue",
            enqueue_start,
            path=path,
            source_kind=pending_event.source_kind,
            pending_count=self._gate_work_count(gate),
            flush_outcome=flush_outcome,
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
            timing_scope=event_timing_scope(pending_event.event.event_id),
        )

    async def enqueue(self, key: CoalescingKey, pending_event: PendingEvent) -> None:
        """Queue one pending event and schedule its eventual flush.

        This method is the Matrix ingress boundary for live coalescing.
        It mutates bounded in-memory state, wakes one owned drain task for the
        key, and returns without awaiting dispatch or model generation.
        """
        enqueue_start = time.monotonic()
        gate = self._get_or_create_gate(key)
        kind = self._queue_kind(pending_event)
        gate.queue.append(_QueuedEvent(kind, pending_event))
        self._schedule_drain(key, gate)
        path = self._enqueue_path(kind)
        self._record_enqueue(
            key,
            gate,
            pending_event,
            enqueue_start,
            path=path,
            flush_outcome="scheduled_drain" if path == "zero_debounce" else None,
        )

    async def drain_all(self) -> None:
        """Flush every active gate and await owned drain tasks."""
        for key, gate in list(self._gates.items()):
            gate.drain_all_requested = True
            gate.deadline = time.monotonic()
            gate.grace_deadline = None
            self._ensure_drain_task(key, gate)
            self._wake(gate)
        tasks_to_await = [
            gate.drain_task
            for gate in self._gates.values()
            if gate.drain_task is not None and not gate.drain_task.done()
        ]
        tasks_to_await.extend(task for task in self._retired_in_flight_drain_tasks if not task.done())
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        self._gates.clear()

    def _upload_grace_hard_cap_seconds(self) -> float:
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        return max(
            grace_seconds,
            min(grace_seconds * _UPLOAD_GRACE_HARD_CAP_MULTIPLIER, _UPLOAD_GRACE_MAX_HARD_CAP_SECONDS),
        )

    async def _wait_for_deadline(self, gate: _GateEntry, deadline: float) -> bool:
        """Return True when ingress woke the drain before the deadline."""
        while True:
            delay = deadline - time.monotonic()
            if delay <= 0:
                return False
            wake_generation = gate.wake_generation
            gate.wake_event.clear()
            if gate.deadline != deadline or gate.wake_generation != wake_generation:
                return True
            try:
                await asyncio.wait_for(gate.wake_event.wait(), timeout=delay)
            except TimeoutError:
                return False
            else:
                return True

    async def _wait_for_debounce(self, gate: _GateEntry) -> None:
        """Wait for the normal debounce window, returning early when a barrier appears."""
        gate.phase = GatePhase.DEBOUNCE
        gate.grace_deadline = None
        debounce_seconds = max(self._debounce_seconds(), 0.0)
        if debounce_seconds <= 0 or self._is_shutting_down() or gate.drain_all_requested:
            gate.deadline = time.monotonic()
            return
        if self._has_barrier_after_front_normal_run(gate):
            gate.deadline = time.monotonic()
            return
        gate.deadline = time.monotonic() + debounce_seconds
        while True:
            deadline = gate.deadline or time.monotonic()
            if not await self._wait_for_deadline(gate, deadline):
                return
            if self._is_shutting_down() or gate.drain_all_requested or self._has_barrier_after_front_normal_run(gate):
                return
            gate.deadline = time.monotonic() + debounce_seconds

    async def _wait_for_upload_grace(
        self,
        gate: _GateEntry,
        candidate_count: int,
        *,
        timing_scope: str,
    ) -> int:
        """Wait for late media without removing the candidate batch from the queue."""
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        if grace_seconds <= 0 or self._is_shutting_down() or gate.drain_all_requested:
            return candidate_count
        gate.phase = GatePhase.GRACE
        gate.grace_deadline = time.monotonic() + self._upload_grace_hard_cap_seconds()
        gate.deadline = time.monotonic() + min(grace_seconds, self._upload_grace_hard_cap_seconds())
        candidate_count = self._extend_candidate_with_grace_media(gate, candidate_count)
        if self._has_item_after_candidate(gate, candidate_count):
            return candidate_count
        grace_start = time.monotonic()
        emit_elapsed_timing(
            "coalescing_gate.flush",
            grace_start,
            outcome="scheduled_grace",
            pending_count=candidate_count,
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
            timing_scope=timing_scope,
        )
        while True:
            deadline = gate.deadline or time.monotonic()
            woke = await self._wait_for_deadline(gate, deadline)
            candidate_count = self._extend_candidate_with_grace_media(gate, candidate_count)
            if (
                self._is_shutting_down()
                or gate.drain_all_requested
                or self._has_item_after_candidate(gate, candidate_count)
                or not woke
            ):
                return candidate_count
            remaining_seconds = max((gate.grace_deadline or time.monotonic()) - time.monotonic(), 0.0)
            if remaining_seconds <= 0:
                return candidate_count
            gate.deadline = time.monotonic() + min(grace_seconds, remaining_seconds)

    def _log_dispatch_failure(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        error: Exception,
    ) -> None:
        logger.exception(
            "Coalescing drain failed",
            room_id=key[0],
            thread_id=key[1],
            requester_user_id=key[2],
            pending_count=self._gate_work_count(gate),
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
            exception_type=error.__class__.__name__,
            error_message="Coalesced dispatch failed.",
        )

    async def _dispatch_events(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        pending_events: list[PendingEvent],
        *,
        bypass_grace: bool,
    ) -> str:
        """Dispatch a claimed batch while buffering new ingress on the same gate."""
        flush_start = time.monotonic()
        gate.phase = GatePhase.IN_FLIGHT
        gate.deadline = None
        gate.grace_deadline = None
        pending_count = len(pending_events)
        timing_scope = event_timing_scope(pending_events[-1].event.event_id)
        log_context: dict[str, object] = {
            "room_id": key[0],
            "thread_id": key[1],
            "requester_user_id": key[2],
            "pending_count": pending_count,
            "oldest_pending_age_ms": self._oldest_pending_events_age_ms(pending_events),
            "bypass_grace": bypass_grace,
            "source_event_ids": self._source_event_ids(pending_events),
            "timing_scope": timing_scope,
        }
        dispatched = False
        try:
            diagnostics = self._flush_diagnostics(key, pending_events, bypass_grace=bypass_grace)
            pending_count = diagnostics.pending_count
            timing_scope = diagnostics.timing_scope
            log_context = diagnostics.log_context
            logger.info("coalescing_gate_flush_started", **log_context)
            dispatch_batch_start = time.monotonic()
            await self._dispatch_batch(diagnostics.batch)
            dispatched = True
            emit_elapsed_timing(
                "coalescing_gate.flush.dispatch_batch",
                dispatch_batch_start,
                pending_count=pending_count,
                bypass_grace=bypass_grace,
                timing_scope=timing_scope,
            )
            return "dispatched"
        finally:
            outcome = "dispatched" if dispatched else "failed"
            emit_elapsed_timing(
                "coalescing_gate.flush",
                flush_start,
                outcome=outcome,
                pending_count=pending_count,
                bypass_grace=bypass_grace,
                timing_scope=timing_scope,
            )
            self._log_flush_finished(
                log_context,
                flush_start=flush_start,
                outcome=outcome,
            )
            current_key, current_gate = self._resolve_gate_entry(key, gate)
            if current_key is not None and current_gate is not None:
                current_gate.phase = GatePhase.DEBOUNCE
                current_gate.grace_deadline = None
                current_gate.deadline = None

    async def _dispatch_claimed_events(
        self,
        key: CoalescingKey,
        gate: _GateEntry,
        pending_events: list[PendingEvent],
        *,
        bypass_grace: bool,
    ) -> None:
        try:
            await self._dispatch_events(key, gate, pending_events, bypass_grace=bypass_grace)
        except asyncio.CancelledError:
            _cancel_pending_event_reservations(pending_events)
            raise
        except Exception as error:
            _cancel_pending_event_reservations(pending_events)
            current_key, current_gate = self._resolve_gate_entry(key, gate)
            self._log_dispatch_failure(current_key or key, current_gate or gate, error)

    async def _drain_gate(self, key: CoalescingKey, gate: _GateEntry) -> None:  # noqa: C901, PLR0912, PLR0915
        """Own debounce, grace, and dispatch for one coalescing key."""
        drain_start = time.monotonic()
        current_key: CoalescingKey | None = key
        outcome = "finished"
        logger.debug(
            "coalescing_drain_start",
            room_id=key[0],
            thread_id=key[1],
            requester_user_id=key[2],
            pending_count=self._gate_work_count(gate),
            oldest_pending_age_ms=self._oldest_pending_age_ms(gate),
        )
        try:
            while True:
                current_key, current_gate = self._resolve_gate_entry(current_key or key, gate)
                if current_key is None or current_gate is None:
                    return
                gate = current_gate

                if not gate.queue:
                    self._gates.pop(current_key, None)
                    return

                front = gate.queue[0]
                if front.kind in {_QueueKind.BYPASS, _QueueKind.COMMAND}:
                    pending_events = self._claim_front_events(gate, 1)
                    await self._dispatch_claimed_events(
                        current_key,
                        gate,
                        pending_events,
                        bypass_grace=True,
                    )
                    continue

                await self._wait_for_debounce(gate)
                bypass_grace = self._is_shutting_down() or gate.drain_all_requested
                use_upload_grace = not bypass_grace and self._upload_grace_seconds() > 0
                candidate_count = self._front_normal_run_length(gate)
                if candidate_count == 0:
                    continue
                candidate_events = self._queue_pending_events(gate, candidate_count)
                if use_upload_grace and _pending_has_only_text(candidate_events):
                    timing_scope = event_timing_scope(
                        build_coalesced_batch(current_key, candidate_events).primary_event.event_id,
                    )
                    candidate_count = await self._wait_for_upload_grace(
                        gate,
                        candidate_count,
                        timing_scope=timing_scope,
                    )
                    bypass_grace = True
                pending_events = self._claim_front_events(gate, candidate_count)
                if not gate.queue:
                    gate.drain_all_requested = False
                await self._dispatch_claimed_events(
                    current_key,
                    gate,
                    pending_events,
                    bypass_grace=bypass_grace,
                )
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as error:
            outcome = "failed"
            log_key = current_key or key
            self._log_dispatch_failure(log_key, gate, error)
        finally:
            resolved_key, resolved_gate = self._resolve_gate_entry(current_key or key, gate)
            if resolved_key is not None and resolved_gate is not None:
                if resolved_gate.drain_task is asyncio.current_task():
                    resolved_gate.drain_task = None
                if self._gate_work_count(resolved_gate) == 0:
                    self._gates.pop(resolved_key, None)
                elif outcome in {"failed", "cancelled"} and not self._is_shutting_down():
                    self._ensure_drain_task(resolved_key, resolved_gate)
                    self._wake(resolved_gate)
            logger.debug(
                "coalescing_drain_finish",
                room_id=(resolved_key or current_key or key)[0],
                thread_id=(resolved_key or current_key or key)[1],
                requester_user_id=(resolved_key or current_key or key)[2],
                outcome=outcome,
                pending_count=self._gate_work_count(resolved_gate) if resolved_gate is not None else 0,
                oldest_pending_age_ms=(
                    self._oldest_pending_age_ms(resolved_gate) if resolved_gate is not None else None
                ),
                duration_ms=round((time.monotonic() - drain_start) * 1000, 1),
            )
