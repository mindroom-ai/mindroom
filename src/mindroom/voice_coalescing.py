"""Receive-time coalescing for raw voice ingress."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .coalescing_batch import close_pending_event_metadata

if TYPE_CHECKING:
    import nio

    from mindroom.coalescing_batch import CoalescingKey, PendingEvent
    from mindroom.inbound_turn_normalizer import VoiceNormalizationResult
    from mindroom.matrix.media import AudioMessageEvent
    from mindroom.timing import DispatchPipelineTiming


@dataclass(frozen=True)
class VoiceIngressItem:
    """One raw audio event accepted before speech-to-text has completed."""

    room: nio.MatrixRoom
    event: AudioMessageEvent
    requester_user_id: str
    normalization_task: asyncio.Task[VoiceNormalizationResult | None]
    dispatch_timing: DispatchPipelineTiming | None
    received_at: float = field(default_factory=time.monotonic)
    received_wall_time: float = field(default_factory=time.time)


@dataclass(frozen=True)
class TextIngressItem:
    """One already-normalized text event joining a pending voice burst."""

    pending_event: PendingEvent
    received_at: float = field(default_factory=time.monotonic)


def _close_text_ingress_item_metadata(items: list[TextIngressItem] | tuple[TextIngressItem, ...]) -> None:
    close_pending_event_metadata([item.pending_event for item in items])


@dataclass
class _ClaimedTextIngressBuffer:
    """Text captured after one voice burst was claimed and before downstream handoff."""

    _items: list[TextIngressItem] = field(default_factory=list)
    _closed: bool = False
    _consumed: bool = False

    @property
    def is_open(self) -> bool:
        return not self._closed and not self._consumed

    def append(self, item: TextIngressItem) -> bool:
        if not self.is_open:
            return False
        item.pending_event.enqueue_time = time.time()
        self._items.append(item)
        return True

    def consume(self) -> tuple[TextIngressItem, ...]:
        if not self.is_open:
            return ()
        self._consumed = True
        items = tuple(self._items)
        self._items.clear()
        return items

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._consumed:
            _close_text_ingress_item_metadata(self._items)
        self._items.clear()


@dataclass(frozen=True)
class VoiceNormalizationOutcome:
    """Result of one voice normalization task in a claimed burst."""

    item: VoiceIngressItem
    result: VoiceNormalizationResult | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class VoiceIngressBatch:
    """One receive-time burst ready for downstream dispatch coalescing."""

    key: CoalescingKey
    voice_outcomes: tuple[VoiceNormalizationOutcome, ...]
    text_items: tuple[TextIngressItem, ...]
    claimed_text_buffer: _ClaimedTextIngressBuffer = field(
        default_factory=_ClaimedTextIngressBuffer,
        repr=False,
        compare=False,
    )

    def consume_claimed_text_items(self) -> tuple[TextIngressItem, ...]:
        """Return late text captured for this exact claimed burst."""
        return self.claimed_text_buffer.consume()


type _FlushVoiceIngressBatch = Callable[[VoiceIngressBatch], Awaitable[None]]


@dataclass
class _QueuedVoice:
    item: VoiceIngressItem
    done: asyncio.Future[None]


@dataclass
class _VoiceBurstEntry:
    voices: list[_QueuedVoice] = field(default_factory=list)
    text_items: list[TextIngressItem] = field(default_factory=list)
    drain_task: asyncio.Task[None] | None = None
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    wake_generation: int = 0
    deadline: float | None = None
    drain_all_requested: bool = False
    flush_batch: _FlushVoiceIngressBatch | None = None


class VoiceCoalescingGate:
    """Hold raw audio by receive time until every burst transcript is ready."""

    def __init__(
        self,
        *,
        debounce_seconds: Callable[[], float],
        is_shutting_down: Callable[[], bool],
    ) -> None:
        self._debounce_seconds = debounce_seconds
        self._is_shutting_down = is_shutting_down
        self._entries: dict[CoalescingKey, _VoiceBurstEntry] = {}
        self._claimed_text_buffers: dict[CoalescingKey, list[_ClaimedTextIngressBuffer]] = {}
        self._pending_voice_counts: dict[CoalescingKey, int] = {}
        self._drain_tasks: set[asyncio.Task[None]] = set()

    def has_pending_voice_burst(self, key: CoalescingKey) -> bool:
        """Return whether raw voice for this key is waiting on debounce, STT, flush, or handoff."""
        return self._pending_voice_counts.get(key, 0) > 0

    def enqueue_text_if_voice_pending(self, key: CoalescingKey, item: TextIngressItem) -> bool:
        """Append text to a live or claimed voice burst."""
        claimed_text_buffers = self._claimed_text_buffers.get(key)
        if claimed_text_buffers is not None:
            for claimed_text_buffer in claimed_text_buffers:
                if claimed_text_buffer.append(item):
                    return True
            self._prune_claimed_text_buffers(key)

        entry = self._entries.get(key)
        if entry is not None and entry.voices:
            item.pending_event.enqueue_time = time.time()
            entry.text_items.append(item)
            self._wake(entry)
            return True

        return False

    def _register_claimed_text_buffer(self, key: CoalescingKey, buffer: _ClaimedTextIngressBuffer) -> None:
        self._claimed_text_buffers.setdefault(key, []).append(buffer)

    def _prune_claimed_text_buffers(self, key: CoalescingKey) -> None:
        buffers = self._claimed_text_buffers.get(key)
        if buffers is None:
            return
        open_buffers = [buffer for buffer in buffers if buffer.is_open]
        if open_buffers:
            self._claimed_text_buffers[key] = open_buffers
            return
        self._claimed_text_buffers.pop(key, None)

    def _remove_claimed_text_buffer(self, key: CoalescingKey, buffer: _ClaimedTextIngressBuffer) -> None:
        buffers = self._claimed_text_buffers.get(key)
        if buffers is None:
            return
        remaining_buffers = [candidate for candidate in buffers if candidate is not buffer and candidate.is_open]
        if remaining_buffers:
            self._claimed_text_buffers[key] = remaining_buffers
            return
        self._claimed_text_buffers.pop(key, None)

    async def enqueue_voice(
        self,
        key: CoalescingKey,
        item: VoiceIngressItem,
        *,
        flush_batch: _FlushVoiceIngressBatch,
    ) -> None:
        """Accept one raw voice event and wait until its claimed burst is flushed."""
        entry = self._entries.get(key)
        if entry is None:
            entry = _VoiceBurstEntry(flush_batch=flush_batch)
            self._entries[key] = entry
        elif entry.flush_batch is None:
            entry.flush_batch = flush_batch

        done = asyncio.get_running_loop().create_future()
        self._pending_voice_counts[key] = self._pending_voice_counts.get(key, 0) + 1
        entry.voices.append(_QueuedVoice(item=item, done=done))
        self._ensure_drain_task(key, entry)
        self._wake(entry)
        await done

    def _release_pending_voice_count(self, key: CoalescingKey, count: int) -> None:
        pending_count = self._pending_voice_counts.get(key, 0)
        remaining_count = pending_count - count
        if remaining_count > 0:
            self._pending_voice_counts[key] = remaining_count
            return
        self._pending_voice_counts.pop(key, None)

    async def drain_all(self) -> None:
        """Force every pending voice burst to flush and await its drain task."""
        while True:
            entries = list(self._entries.items())
            tasks_to_await = [task for task in self._drain_tasks if not task.done()]
            if not entries and not tasks_to_await:
                return
            for key, entry in entries:
                entry.drain_all_requested = True
                entry.deadline = time.monotonic()
                self._ensure_drain_task(key, entry)
                self._wake(entry)
            tasks_to_await = [task for task in self._drain_tasks if not task.done()]
            if tasks_to_await:
                await asyncio.gather(*tasks_to_await, return_exceptions=True)

    def _ensure_drain_task(self, key: CoalescingKey, entry: _VoiceBurstEntry) -> None:
        if entry.drain_task is not None and not entry.drain_task.done():
            return
        entry.drain_task = asyncio.create_task(
            self._drain_entry(key, entry),
            name=f"voice_coalescing_drain:{key[0]}:{key[1] or 'room'}:{key[2]}",
        )
        self._drain_tasks.add(entry.drain_task)
        entry.drain_task.add_done_callback(self._drain_tasks.discard)

    @staticmethod
    def _wake(entry: _VoiceBurstEntry) -> None:
        entry.wake_generation += 1
        entry.wake_event.set()

    async def _wait_for_deadline(self, entry: _VoiceBurstEntry, deadline: float) -> bool:
        while True:
            delay = deadline - time.monotonic()
            if delay <= 0:
                return False
            wake_generation = entry.wake_generation
            entry.wake_event.clear()
            if entry.deadline != deadline or entry.wake_generation != wake_generation:
                return True
            try:
                await asyncio.wait_for(entry.wake_event.wait(), timeout=delay)
            except TimeoutError:
                return False
            else:
                return True

    async def _wait_for_debounce(self, entry: _VoiceBurstEntry) -> None:
        debounce_seconds = max(self._debounce_seconds(), 0.0)
        if debounce_seconds <= 0 or self._is_shutting_down() or entry.drain_all_requested:
            entry.deadline = time.monotonic()
            return
        entry.deadline = time.monotonic() + debounce_seconds
        while True:
            deadline = entry.deadline or time.monotonic()
            if not await self._wait_for_deadline(entry, deadline):
                return
            if self._is_shutting_down() or entry.drain_all_requested:
                return
            entry.deadline = time.monotonic() + debounce_seconds

    async def _drain_entry(self, key: CoalescingKey, entry: _VoiceBurstEntry) -> None:
        pending_count_released = False
        flush_started = False
        claimed_text_buffer: _ClaimedTextIngressBuffer | None = None
        try:
            await self._wait_for_debounce(entry)
            claimed_entry = self._entries.pop(key, None)
            if claimed_entry is not entry:
                return
            flush_batch = entry.flush_batch
            if flush_batch is None:
                _close_text_ingress_item_metadata(entry.text_items)
                self._release_pending_voice_count(key, len(entry.voices))
                pending_count_released = True
                return
            claimed_text_buffer = _ClaimedTextIngressBuffer()
            self._register_claimed_text_buffer(key, claimed_text_buffer)
            outcomes = await self._voice_outcomes(entry.voices)
            flush_started = True
            await flush_batch(
                VoiceIngressBatch(
                    key=key,
                    voice_outcomes=tuple(outcomes),
                    text_items=tuple(entry.text_items),
                    claimed_text_buffer=claimed_text_buffer,
                ),
            )
            self._release_pending_voice_count(key, len(entry.voices))
            pending_count_released = True
            self._resolve_voice_futures(entry.voices, outcomes)
        except BaseException as error:
            if not flush_started:
                _close_text_ingress_item_metadata(entry.text_items)
            if claimed_text_buffer is not None:
                claimed_text_buffer.close()
            if not pending_count_released:
                self._release_pending_voice_count(key, len(entry.voices))
            self._fail_voice_futures(entry.voices, error)
            raise
        finally:
            if claimed_text_buffer is not None:
                self._remove_claimed_text_buffer(key, claimed_text_buffer)
                claimed_text_buffer.close()
            if entry.drain_task is asyncio.current_task():
                entry.drain_task = None

    @staticmethod
    async def _voice_outcomes(voices: list[_QueuedVoice]) -> list[VoiceNormalizationOutcome]:
        results = await asyncio.gather(
            *(queued_voice.item.normalization_task for queued_voice in voices),
            return_exceptions=True,
        )
        outcomes: list[VoiceNormalizationOutcome] = []
        for queued_voice, result in zip(voices, results, strict=True):
            if isinstance(result, BaseException):
                outcomes.append(VoiceNormalizationOutcome(item=queued_voice.item, error=result))
                continue
            outcomes.append(VoiceNormalizationOutcome(item=queued_voice.item, result=result))
        return outcomes

    @staticmethod
    def _resolve_voice_futures(
        voices: list[_QueuedVoice],
        outcomes: list[VoiceNormalizationOutcome],
    ) -> None:
        for queued_voice, outcome in zip(voices, outcomes, strict=True):
            if queued_voice.done.done():
                continue
            if outcome.error is not None:
                queued_voice.done.set_exception(outcome.error)
                continue
            queued_voice.done.set_result(None)

    @staticmethod
    def _fail_voice_futures(voices: list[_QueuedVoice], error: BaseException) -> None:
        for queued_voice in voices:
            if not queued_voice.done.done():
                queued_voice.done.set_exception(error)
