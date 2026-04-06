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
from .hooks.ingress import AUTOMATION_SOURCE_KINDS
from .matrix.media import extract_media_caption

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = [
    "COALESCED_SOURCE_EVENT_IDS_CONTENT_KEY",
    "COALESCED_SOURCE_EVENT_PROMPTS_CONTENT_KEY",
    "CoalescedBatch",
    "CoalescingGate",
    "CoalescingKey",
    "DispatchEvent",
    "GatePhase",
    "MediaDispatchEvent",
    "PendingEvent",
    "SyntheticTextEvent",
    "TextDispatchEvent",
    "build_batch_dispatch_event",
    "build_coalesced_batch",
    "coalesced_prompt",
    "is_coalescing_exempt_source_kind",
]

_UPLOAD_GRACE_HARD_CAP_MULTIPLIER = 4.0
_UPLOAD_GRACE_MAX_HARD_CAP_SECONDS = 2.0
COALESCED_SOURCE_EVENT_IDS_CONTENT_KEY = "com.mindroom.coalesced_source_event_ids"
COALESCED_SOURCE_EVENT_PROMPTS_CONTENT_KEY = "com.mindroom.coalesced_source_event_prompts"


class GatePhase(enum.Enum):
    """Lifecycle phases for one coalescing gate."""

    DEBOUNCE = "debounce"
    GRACE = "grace"
    IN_FLIGHT = "in_flight"


type MediaDispatchEvent = (
    # Voice messages are normalized into SyntheticTextEvent before coalescing,
    # so this contract only includes routed image/file/video events.
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
)
type TextDispatchEvent = nio.RoomMessageText | SyntheticTextEvent
type DispatchEvent = TextDispatchEvent | MediaDispatchEvent
type CoalescingKey = tuple[str, str | None, str]


@dataclass(frozen=True)
class SyntheticTextEvent:
    """Minimal text-event shape for internal normalized-media dispatch."""

    sender: str
    event_id: str
    body: str
    source: dict[str, Any]
    server_timestamp: int | float | None = None


@dataclass
class PendingEvent:
    """One queued inbound event waiting to be coalesced."""

    event: DispatchEvent
    room: nio.MatrixRoom
    source_kind: str
    enqueue_time: float = field(default_factory=time.time)


@dataclass
class DispatchGate:
    phase: GatePhase = GatePhase.DEBOUNCE
    pending: list[PendingEvent] = field(default_factory=list)
    wake_task: asyncio.Task[None] | None = None
    flush_task: asyncio.Task[None] | None = None
    wake_epoch: int = 0
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
    return _effective_source_kind(event, fallback_source_kind) in AUTOMATION_SOURCE_KINDS


def is_command_event(event: DispatchEvent) -> bool:
    """Return whether a dispatch event should bypass coalescing as a command."""
    if not isinstance(event, nio.RoomMessageText | SyntheticTextEvent):
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
        isinstance(pending_event.event, nio.RoomMessageText | SyntheticTextEvent) for pending_event in pending_events
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
        msg = "Raw audio must be normalized into SyntheticTextEvent before coalescing"
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


def _batch_source_kind(ordered_pending_events: list[PendingEvent]) -> str:
    resolved_source_kinds = [
        _effective_source_kind(pending_event.event, pending_event.source_kind) or pending_event.source_kind
        for pending_event in ordered_pending_events
    ]
    for source_kind in resolved_source_kinds:
        if source_kind == "voice":
            return source_kind
    for source_kind in resolved_source_kinds:
        if source_kind == "image":
            return source_kind
    for source_kind in resolved_source_kinds:
        if source_kind == "media":
            return source_kind
    return resolved_source_kinds[-1]


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


def _can_reuse_primary_batch_event(batch: CoalescedBatch) -> bool:
    return (
        len(batch.pending_events) == 1
        and not batch.media_events
        and not batch.attachment_ids
        and batch.original_sender is None
        and not batch.raw_audio_fallback
        and isinstance(batch.primary_event, nio.RoomMessageText | SyntheticTextEvent)
    )


def _collect_batch_mentions_and_formatted_bodies(batch: CoalescedBatch) -> tuple[list[str], list[str]]:
    merged_mentions: list[str] = []
    merged_formatted_bodies: list[str] = []
    for pending_event in batch.pending_events:
        pending_content = _event_content_dict(pending_event.event)
        if pending_content is None:
            continue
        pending_mentions = pending_content.get("m.mentions")
        if isinstance(pending_mentions, dict):
            pending_mentions_dict = cast("dict[str, object]", pending_mentions)
            pending_user_ids = pending_mentions_dict.get("user_ids")
            if isinstance(pending_user_ids, list):
                for user_id in pending_user_ids:
                    if isinstance(user_id, str) and user_id not in merged_mentions:
                        merged_mentions.append(user_id)
        formatted_body = pending_content.get("formatted_body")
        if isinstance(formatted_body, str) and formatted_body:
            merged_formatted_bodies.append(formatted_body)
    return merged_mentions, merged_formatted_bodies


def build_batch_dispatch_event(batch: CoalescedBatch) -> TextDispatchEvent:
    """Return the synthetic text event used to dispatch one coalesced batch."""
    if _can_reuse_primary_batch_event(batch):
        return cast("TextDispatchEvent", batch.primary_event)
    source = dict(batch.primary_event.source) if isinstance(batch.primary_event.source, dict) else {}
    source_content = source.get("content")
    content = dict(source_content) if isinstance(source_content, dict) else {}
    merged_mentions, merged_formatted_bodies = _collect_batch_mentions_and_formatted_bodies(batch)
    content["body"] = batch.prompt
    content["com.mindroom.source_kind"] = batch.source_kind
    if merged_mentions:
        content["m.mentions"] = {"user_ids": merged_mentions}
    if merged_formatted_bodies:
        content["formatted_body"] = "<br>".join(merged_formatted_bodies)
    if batch.attachment_ids:
        content[ATTACHMENT_IDS_KEY] = list(batch.attachment_ids)
    else:
        content.pop(ATTACHMENT_IDS_KEY, None)
    content[COALESCED_SOURCE_EVENT_IDS_CONTENT_KEY] = list(batch.source_event_ids)
    content[COALESCED_SOURCE_EVENT_PROMPTS_CONTENT_KEY] = dict(batch.source_event_prompts)
    if batch.original_sender is not None:
        content[ORIGINAL_SENDER_KEY] = batch.original_sender
    if batch.raw_audio_fallback:
        content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
    source["content"] = content
    return SyntheticTextEvent(
        sender=batch.primary_event.sender,
        event_id=batch.primary_event.event_id,
        body=batch.prompt,
        source=source,
        server_timestamp=batch.primary_event.server_timestamp,
    )


class CoalescingGate:
    """Own the debounce/grace state machine for live inbound message batching."""

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
        self._gates: dict[CoalescingKey, DispatchGate] = {}

    def debug_phases(self) -> tuple[GatePhase, ...]:
        """Return active gate phases for deterministic tests and debugging."""
        self._prune_all_idle_gates()
        return tuple(
            gate.phase
            for _, gate in sorted(self._gates.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2]))
        )

    def is_idle(self) -> bool:
        """Return whether all coalescing gates are currently idle."""
        self._prune_all_idle_gates()
        return not self._gates

    async def enqueue(self, key: CoalescingKey, pending_event: PendingEvent) -> None:
        """Queue one pending event and schedule its eventual flush."""
        if not self._enabled() or is_coalescing_exempt_source_kind(pending_event.event, pending_event.source_kind):
            await self._dispatch_batch(build_coalesced_batch(key, [pending_event]))
            return
        gate = self._gates.get(key)
        if is_command_event(pending_event.event):
            if gate is not None:
                self._cancel_gate_wake(gate)
            await self.flush(key, bypass_grace=True)
            await self._dispatch_batch(build_coalesced_batch(key, [pending_event]))
            return
        if gate is None:
            gate = DispatchGate()
            self._gates[key] = gate
        if gate.phase is GatePhase.GRACE:
            if _is_media_event(pending_event.event):
                gate.pending.append(pending_event)
                self._schedule_gate_grace(key)
                return
            self._cancel_gate_wake(gate)
            await self.flush(key, bypass_grace=True)
            gate = self._gates.get(key)
            if gate is None:
                gate = DispatchGate()
                self._gates[key] = gate
        gate.pending.append(pending_event)
        if gate.phase is GatePhase.IN_FLIGHT:
            self._cancel_gate_wake(gate)
            return
        if self._debounce_seconds() <= 0:
            self._cancel_gate_wake(gate)
            await self.flush(key)
            return
        self._schedule_gate_wake(key, delay=self._debounce_seconds(), phase=GatePhase.DEBOUNCE)

    async def drain_all(self) -> None:
        """Flush every active gate, canceling wake tasks and awaiting dispatch."""
        wake_tasks: list[asyncio.Task[None]] = []
        for gate in self._gates.values():
            wake_task = self._cancel_gate_wake(gate)
            if wake_task is not None:
                wake_tasks.append(wake_task)
        if wake_tasks:
            await asyncio.gather(*wake_tasks, return_exceptions=True)
        if self._gates:
            await asyncio.gather(
                *(self.flush(key, wait_for_existing=True, bypass_grace=True) for key in list(self._gates)),
                return_exceptions=True,
            )
        self._gates.clear()

    def _prune_all_idle_gates(self) -> None:
        for key in list(self._gates):
            self._prune_idle_gate(key)

    def _prune_idle_gate(self, key: CoalescingKey) -> None:
        gate = self._gates.get(key)
        if gate is None:
            return
        if gate.wake_task is not None and gate.wake_task.done():
            gate.wake_task = None
        if gate.flush_task is not None and gate.flush_task.done():
            gate.flush_task = None
        if gate.wake_task is None:
            gate.grace_deadline = None
        if gate.pending or gate.wake_task is not None or gate.flush_task is not None:
            return
        self._gates.pop(key, None)

    def _cancel_gate_wake(
        self,
        gate: DispatchGate,
        *,
        invalidate: bool = True,
        clear_deadline: bool = True,
    ) -> asyncio.Task[None] | None:
        if invalidate:
            gate.wake_epoch += 1
        wake_task = gate.wake_task
        gate.wake_task = None
        if clear_deadline:
            gate.grace_deadline = None
        if wake_task is not None and not wake_task.done():
            wake_task.cancel()
        return wake_task

    def _schedule_gate_wake(
        self,
        key: CoalescingKey,
        *,
        delay: float,
        phase: GatePhase,
        preserve_grace_deadline: bool = False,
    ) -> None:
        gate = self._gates[key]
        self._cancel_gate_wake(gate, invalidate=False, clear_deadline=not preserve_grace_deadline)
        gate.phase = phase
        gate.wake_epoch += 1
        wake_epoch = gate.wake_epoch

        async def _wake_then_flush() -> None:
            try:
                await asyncio.sleep(max(delay, 0.0))
                active_gate = self._gates.get(key)
                if active_gate is None or active_gate.wake_epoch != wake_epoch:
                    return
                if active_gate.wake_task is asyncio.current_task():
                    active_gate.wake_task = None
                await self.flush(key, wake_epoch=wake_epoch, bypass_grace=phase is GatePhase.GRACE)
            except asyncio.CancelledError:
                return

        gate.wake_task = asyncio.create_task(_wake_then_flush())

    def _upload_grace_hard_cap_seconds(self) -> float:
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        return max(
            grace_seconds,
            min(grace_seconds * _UPLOAD_GRACE_HARD_CAP_MULTIPLIER, _UPLOAD_GRACE_MAX_HARD_CAP_SECONDS),
        )

    def _schedule_gate_grace(self, key: CoalescingKey) -> None:
        gate = self._gates[key]
        if gate.grace_deadline is None:
            gate.grace_deadline = time.monotonic() + self._upload_grace_hard_cap_seconds()
        grace_seconds = max(self._upload_grace_seconds(), 0.0)
        remaining_seconds = max(gate.grace_deadline - time.monotonic(), 0.0)
        self._schedule_gate_wake(
            key,
            delay=min(grace_seconds, remaining_seconds),
            phase=GatePhase.GRACE,
            preserve_grace_deadline=True,
        )

    async def _run_gate_flush(
        self,
        key: CoalescingKey,
        *,
        wake_epoch: int | None = None,
        bypass_grace: bool = False,
    ) -> None:
        started_dispatch = False
        gate = self._gates.get(key)
        if gate is None:
            return
        try:
            if wake_epoch is not None and gate.wake_epoch != wake_epoch:
                return
            if gate.phase is GatePhase.IN_FLIGHT or not gate.pending:
                return
            if (
                not bypass_grace
                and gate.phase is not GatePhase.GRACE
                and self._upload_grace_seconds() > 0
                and not self._is_shutting_down()
                and _pending_has_only_text(gate.pending)
            ):
                self._schedule_gate_grace(key)
                return
            self._cancel_gate_wake(gate, invalidate=False)
            pending_events = list(gate.pending)
            gate.pending.clear()
            gate.phase = GatePhase.IN_FLIGHT
            started_dispatch = True
            await self._dispatch_batch(build_coalesced_batch(key, pending_events))
        finally:
            gate = self._gates.get(key)
            flush_immediately = False
            if gate is not None:
                if gate.flush_task is asyncio.current_task():
                    gate.flush_task = None
                if started_dispatch:
                    gate.phase = GatePhase.DEBOUNCE
                    flush_immediately = bool(gate.pending) and (
                        self._is_shutting_down() or self._debounce_seconds() <= 0
                    )
                    if gate.pending and not flush_immediately:
                        self._schedule_gate_wake(key, delay=self._debounce_seconds(), phase=GatePhase.DEBOUNCE)
                self._prune_idle_gate(key)
            if flush_immediately:
                await self.flush(key, bypass_grace=self._is_shutting_down())

    async def flush(
        self,
        key: CoalescingKey,
        *,
        wake_epoch: int | None = None,
        wait_for_existing: bool = False,
        bypass_grace: bool = False,
    ) -> None:
        """Force one gate to flush immediately, optionally skipping upload grace."""
        gate = self._gates.get(key)
        if gate is None:
            return
        if gate.flush_task is not None:
            if gate.flush_task.done():
                gate.flush_task = None
            elif wait_for_existing:
                await gate.flush_task
                return
            else:
                return
        gate.flush_task = asyncio.create_task(
            self._run_gate_flush(
                key,
                wake_epoch=wake_epoch,
                bypass_grace=bypass_grace,
            ),
        )
        await gate.flush_task
