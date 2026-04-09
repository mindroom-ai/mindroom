"""Live message coalescing gate and batch helpers."""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeGuard, cast

import nio

from .attachments import merge_attachment_ids, parse_attachment_ids_from_event_source
from .commands.parsing import command_parser
from .constants import ATTACHMENT_IDS_KEY, ORIGINAL_SENDER_KEY, VOICE_RAW_AUDIO_FALLBACK_KEY
from .matrix.media import extract_media_caption

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
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
_COALESCING_EXEMPT_SOURCE_KINDS: frozenset[str] = frozenset({"hook", "hook_dispatch"})


class GatePhase(enum.Enum):
    """Lifecycle phases for one coalescing gate."""

    DEBOUNCE = "debounce"
    GRACE = "grace"
    IN_FLIGHT = "in_flight"


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


@dataclass
class _GateEntry:
    phase: GatePhase = GatePhase.DEBOUNCE
    pending: list[PendingEvent] = field(default_factory=list)
    timer_task: asyncio.Task[None] | None = None
    grace_deadline: float | None = None


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
    content = _event_content_dict(event)
    source_kind = content.get("com.mindroom.source_kind") if content is not None else None
    return source_kind if isinstance(source_kind, str) else fallback_source_kind


def is_coalescing_exempt_source_kind(
    event: DispatchEvent,
    fallback_source_kind: str | None = None,
) -> bool:
    """Return True when coalescing should be skipped for this event."""
    return _effective_source_kind(event, fallback_source_kind) in _COALESCING_EXEMPT_SOURCE_KINDS


def is_command_event(event: DispatchEvent) -> bool:
    """Return whether a dispatch event should bypass coalescing as a command."""
    if not isinstance(event, nio.RoomMessageText | PreparedTextEvent):
        return False
    if _effective_source_kind(event) in {"image", "media"}:
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
        media_events=cast(
            "list[MediaDispatchEvent]",
            [pending_event.event for pending_event in ordered_pending_events if _is_media_event(pending_event.event)],
        ),
        original_sender=original_sender,
        raw_audio_fallback=raw_audio_fallback,
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
    IDLE (absent) → DEBOUNCE → GRACE (optional, wait for images) →
    flush → IN_FLIGHT (buffer new messages) → DEBOUNCE or IDLE.
    """

    def __init__(
        self,
        *,
        dispatch_batch: Callable[[CoalescedBatch], Awaitable[None]],
        enabled: Callable[[], bool],
        debounce_seconds: Callable[[], float],
        upload_grace_seconds: Callable[[], float],
        is_shutting_down: Callable[[], bool],
    ) -> None:
        self._dispatch_batch = dispatch_batch
        self._enabled = enabled
        self._debounce_seconds = debounce_seconds
        self._upload_grace_seconds = upload_grace_seconds
        self._is_shutting_down = is_shutting_down
        self._gates: dict[CoalescingKey, _GateEntry] = {}

    def is_idle(self) -> bool:
        """Return whether all coalescing gates are currently idle."""
        return not self._gates

    def retarget(self, old_key: CoalescingKey, new_key: CoalescingKey) -> None:
        """Re-key one live gate after thread resolution changes the canonical scope."""
        if old_key == new_key:
            return
        gate = self._gates.get(old_key)
        if gate is None or new_key in self._gates:
            return
        self._gates[new_key] = self._gates.pop(old_key)

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

    async def enqueue(self, key: CoalescingKey, pending_event: PendingEvent) -> None:
        """Queue one pending event and schedule its eventual flush."""
        # Path 1: bypass — disabled or automation
        if not self._enabled() or is_coalescing_exempt_source_kind(pending_event.event, pending_event.source_kind):
            await self._dispatch_batch(build_coalesced_batch(key, [pending_event]))
            return

        # Path 2: command interrupt — flush pending, dispatch command solo
        if is_command_event(pending_event.event):
            gate = self._gates.get(key)
            if gate is not None:
                self._cancel_timer(gate)
            await self._flush(key, bypass_grace=True)
            await self._dispatch_batch(build_coalesced_batch(key, [pending_event]))
            return

        gate = self._gates.get(key)
        if gate is None:
            gate = _GateEntry()
            self._gates[key] = gate

        # Path 3: grace-phase handling
        if gate.phase is GatePhase.GRACE:
            if _is_media_event(pending_event.event):
                gate.pending.append(pending_event)
                self._schedule_grace(key)
                return
            # Text during grace → flush existing batch, start new turn
            self._cancel_timer(gate)
            await self._flush(key, bypass_grace=True)
            gate = self._gates.get(key)
            if gate is None:
                gate = _GateEntry()
                self._gates[key] = gate

        # Path 4: normal append + schedule
        gate.pending.append(pending_event)
        if gate.phase is GatePhase.IN_FLIGHT:
            return
        if self._debounce_seconds() <= 0:
            await self._flush(key)
            return
        self._reset_timer(key, delay=self._debounce_seconds(), phase=GatePhase.DEBOUNCE)

    async def drain_all(self) -> None:
        """Flush every active gate, canceling timers and awaiting dispatch.

        Invariant: ``prepare_for_sync_shutdown`` sets ``_sync_shutting_down``
        before calling this, so the ``_flush`` finally block always takes the
        recursive-flush path (not ``_reset_timer``).  If that invariant were
        violated, a newly scheduled timer task could be orphaned when we
        clear ``gate.timer_task`` below — but it would self-cancel harmlessly
        via the ``timer_task is asyncio.current_task()`` stale guard.

        There is a narrow race: if a timer callback has already returned from
        ``asyncio.sleep`` but not yet entered ``_flush`` when we cancel it,
        the cancellation hits during the flush's ``await _dispatch_batch``.
        The ``_flush`` finally block resets phase, but the batch events are
        lost.  This only occurs during shutdown, where the Matrix timeline
        retains messages for replay on restart, so the trade-off is accepted.
        """
        tasks_to_await: list[asyncio.Task[None]] = []
        for gate in self._gates.values():
            if gate.timer_task is not None and not gate.timer_task.done():
                # Only cancel tasks that are sleeping (not running a dispatch).
                if gate.phase is not GatePhase.IN_FLIGHT:
                    gate.timer_task.cancel()
                tasks_to_await.append(gate.timer_task)
        if tasks_to_await:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
        for gate in self._gates.values():
            gate.timer_task = None
        if self._gates:
            await asyncio.gather(
                *(self._flush(key, bypass_grace=True) for key in list(self._gates)),
                return_exceptions=True,
            )
        self._gates.clear()

    def _cancel_timer(self, gate: _GateEntry) -> None:
        """Cancel a pending timer. Preserves the task reference during IN_FLIGHT."""
        if gate.timer_task is not None and not gate.timer_task.done() and gate.phase is not GatePhase.IN_FLIGHT:
            gate.timer_task.cancel()
        if gate.phase is not GatePhase.IN_FLIGHT:
            gate.timer_task = None
        gate.grace_deadline = None

    def _reset_timer(self, key: CoalescingKey, *, delay: float, phase: GatePhase) -> None:
        """Cancel the current timer and schedule a new one."""
        gate = self._gates[key]
        if gate.timer_task is not None and not gate.timer_task.done():
            gate.timer_task.cancel()
        gate.phase = phase

        async def _timer_callback() -> None:
            try:
                await asyncio.sleep(max(delay, 0.0))
            except asyncio.CancelledError:
                return
            current_gate = self._gates.get(key)
            if current_gate is None or current_gate.timer_task is not asyncio.current_task():
                return
            # Keep timer_task alive through the flush so drain_all can await it.
            await self._flush(key, bypass_grace=phase is GatePhase.GRACE)
            current_gate = self._gates.get(key)
            if current_gate is not None and current_gate.timer_task is asyncio.current_task():
                current_gate.timer_task = None

        gate.timer_task = asyncio.create_task(_timer_callback())

    def _upload_grace_hard_cap_seconds(self) -> float:
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        return max(
            grace_seconds,
            min(grace_seconds * _UPLOAD_GRACE_HARD_CAP_MULTIPLIER, _UPLOAD_GRACE_MAX_HARD_CAP_SECONDS),
        )

    def _schedule_grace(self, key: CoalescingKey) -> None:
        gate = self._gates[key]
        if gate.grace_deadline is None:
            gate.grace_deadline = time.monotonic() + self._upload_grace_hard_cap_seconds()
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        remaining_seconds = max(gate.grace_deadline - time.monotonic(), 0.0)
        saved_deadline = gate.grace_deadline
        self._reset_timer(key, delay=min(grace_seconds, remaining_seconds), phase=GatePhase.GRACE)
        self._gates[key].grace_deadline = saved_deadline

    async def _flush(self, key: CoalescingKey, *, bypass_grace: bool = False) -> None:
        """Execute one gate flush cycle."""
        gate = self._gates.get(key)
        if gate is None or not gate.pending or gate.phase is GatePhase.IN_FLIGHT:
            return
        if (
            not bypass_grace
            and gate.phase is not GatePhase.GRACE
            and self._upload_grace_seconds() > 0
            and not self._is_shutting_down()
            and _pending_has_only_text(gate.pending)
        ):
            self._schedule_grace(key)
            return
        # Set IN_FLIGHT before _cancel_timer so the running timer task
        # (which may be the current task) is not self-cancelled.
        gate.phase = GatePhase.IN_FLIGHT
        self._cancel_timer(gate)
        pending_events = list(gate.pending)
        gate.pending.clear()
        try:
            await self._dispatch_batch(build_coalesced_batch(key, pending_events))
        finally:
            current_key, gate = self._resolve_gate_entry(key, gate)
            if gate is not None and current_key is not None:
                gate.phase = GatePhase.DEBOUNCE
                if not gate.pending:
                    self._gates.pop(current_key, None)
                elif self._is_shutting_down() or self._debounce_seconds() <= 0:
                    await self._flush(current_key, bypass_grace=self._is_shutting_down())
                else:
                    self._reset_timer(current_key, delay=self._debounce_seconds(), phase=GatePhase.DEBOUNCE)
