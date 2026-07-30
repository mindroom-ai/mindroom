"""Retry frozen terminal Matrix edits from canonical TurnRecords."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from mindroom.handled_turns import TerminalEditCheckpoint, TurnRecord
from mindroom.matrix.client_delivery import send_message_result

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import nio
    import structlog

    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.turn_store import TurnStore

_TerminalDeliveryStatus = Literal["delivered", "deferred", "superseded"]


class _RuntimeClient(Protocol):
    """Runtime surface needed by terminal transport."""

    @property
    def client(self) -> nio.AsyncClient | None: ...


@dataclass(frozen=True, slots=True)
class TerminalDeliveryIntent:
    """One exact terminal edit ready for durable checkpoint commit."""

    source_event_ids: tuple[str, ...]
    target_event_id: str
    correlation_id: str
    wire_content: Mapping[str, Any]

    @property
    def transaction_id(self) -> str:
        """Derive one stable transaction from owner, target, and payload."""
        raw = json.dumps(
            [
                self.source_event_ids,
                self.correlation_id,
                self.target_event_id,
                dict(self.wire_content),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"mindroom-terminal-{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class TerminalDeliveryCommit:
    """Result of checkpoint commit and its immediate transport attempt."""

    status: _TerminalDeliveryStatus
    reason: str


@dataclass(frozen=True, slots=True)
class TerminalDeliveryCoordinatorDeps:
    """Collaborators needed for durable terminal transport."""

    runtime: _RuntimeClient
    turn_store: TurnStore
    conversation_cache: ConversationCacheProtocol
    redact_message_event: Callable[..., Awaitable[bool]]
    is_ready: Callable[[], bool]
    logger: structlog.stdlib.BoundLogger
    poll_interval_seconds: float = 15.0


@dataclass
class TerminalDeliveryCoordinator:
    """Persist and retry exceptional terminal-edit transport debt."""

    deps: TerminalDeliveryCoordinatorDeps
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False)
    _stopping: bool = field(default=False, init=False)

    async def warm(self) -> int:
        """Return the number of checkpoints loaded by TurnStore startup."""
        records = await asyncio.to_thread(self.deps.turn_store.terminal_checkpoint_records)
        return len(records)

    def start(self) -> None:
        """Start the periodic checkpoint retry loop."""
        if self._worker is not None and not self._worker.done():
            return
        self._stopping = False
        self._worker = asyncio.create_task(self._run(), name="terminal_checkpoint_retry")

    async def stop(self) -> None:
        """Stop retry transport without discarding durable checkpoint debt."""
        self._stopping = True
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    def wake(self, *, reason: str) -> None:
        """Wake retry after Matrix readiness or new checkpoint work."""
        del reason
        self._wake_event.set()

    async def can_checkpoint(self, source_event_ids: tuple[str, ...]) -> bool:
        """Return whether one canonical turn currently owns every source."""
        return await asyncio.to_thread(self._turn_for_sources, source_event_ids) is not None

    @asynccontextmanager
    async def stale_cleanup_guard(self, target_event_id: str) -> AsyncIterator[bool]:
        """Hold delivery serialization while stale cleanup rechecks ownership."""
        async with self._lock:
            record = await asyncio.to_thread(self.deps.turn_store.turn_for_event, target_event_id)
            owns_target = record is not None and record.completed and record.response_event_id == target_event_id
            yield not owns_target

    async def commit_and_attempt(self, intent: TerminalDeliveryIntent) -> TerminalDeliveryCommit:
        """Persist exact content before the first Matrix attempt."""
        checkpoint = TerminalEditCheckpoint(
            transaction_id=intent.transaction_id,
            wire_content=intent.wire_content,
            correlation_id=intent.correlation_id,
        )
        async with self._lock:
            authority = await asyncio.to_thread(self._turn_for_sources, intent.source_event_ids)
            if authority is None:
                return TerminalDeliveryCommit("superseded", "turn_authority_missing")
            committed = await asyncio.to_thread(
                self.deps.turn_store.commit_terminal_checkpoint,
                authority,
                response_event_id=intent.target_event_id,
                checkpoint=checkpoint,
            )
            if committed is None:
                return TerminalDeliveryCommit("superseded", "checkpoint_rejected")
            self.wake(reason="checkpoint_committed")
            try:
                return await self._attempt_locked(committed, allow_unready=True)
            except asyncio.CancelledError:
                return TerminalDeliveryCommit("deferred", "attempt_cancelled")

    async def retry_pending(self) -> None:
        """Attempt each pending checkpoint once, sequentially."""
        records = await asyncio.to_thread(self.deps.turn_store.terminal_checkpoint_records)
        for record in records:
            try:
                async with self._lock:
                    await self._attempt_locked(record)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.deps.logger.exception(
                    "terminal_checkpoint_attempt_failed",
                    source_event_ids=record.source_event_ids,
                    target_event_id=record.response_event_id,
                )

    async def redact(self, *, room_id: str, event_id: str) -> None:
        """Tombstone source or target and settle matching terminal debt."""
        async with self._lock:
            owner = await asyncio.to_thread(self.deps.turn_store.turn_for_event, event_id)
            if (
                owner is not None
                and owner.conversation_target is not None
                and owner.conversation_target.room_id != room_id
            ):
                return
            redacted = await asyncio.to_thread(self.deps.turn_store.mark_source_redacted, event_id)
            if redacted is not None and redacted.terminal_edit_checkpoint is not None:
                await self._attempt_locked(redacted)
            elif (
                redacted is not None
                and event_id in redacted.indexed_event_ids
                and redacted.response_event_id is not None
                and redacted.conversation_target is not None
            ):
                target_event_id = redacted.response_event_id
                cleaned = await self.deps.redact_message_event(
                    room_id=redacted.conversation_target.room_id,
                    event_id=target_event_id,
                    reason="Source event was redacted",
                )
                if cleaned:
                    await asyncio.to_thread(
                        self.deps.turn_store.clear_redacted_response,
                        redacted,
                        expected_response_event_id=target_event_id,
                    )

    def _turn_for_sources(self, source_event_ids: tuple[str, ...]) -> TurnRecord | None:
        if not source_event_ids:
            return None
        owner = self.deps.turn_store.get_turn_record(source_event_ids[0])
        if (
            owner is None
            or any(self.deps.turn_store.get_turn_record(event_id) != owner for event_id in source_event_ids)
            or not set(source_event_ids).issubset(owner.indexed_event_ids)
        ):
            return None
        return owner

    async def _attempt_locked(
        self,
        record: TurnRecord,
        *,
        allow_unready: bool = False,
    ) -> TerminalDeliveryCommit:
        current = await asyncio.to_thread(
            self.deps.turn_store.terminal_checkpoint_for_sources,
            record.indexed_event_ids,
        )
        if current is None or current.terminal_edit_checkpoint is None:
            return TerminalDeliveryCommit("superseded", "checkpoint_replaced")
        checkpoint = current.terminal_edit_checkpoint
        if current.redacted_source_event_ids != checkpoint.accepted_redacted_source_event_ids:
            return await self._cleanup_redacted_checkpoint(current, checkpoint)
        target = current.conversation_target
        target_event_id = current.response_event_id
        client = self.deps.runtime.client
        if (
            self._stopping
            or client is None
            or (not allow_unready and not self.deps.is_ready())
            or target is None
            or target_event_id is None
        ):
            return TerminalDeliveryCommit("deferred", "matrix_not_ready")
        delivered = await send_message_result(
            client,
            target.room_id,
            cast("dict[str, object]", _thaw_json(checkpoint.wire_content)),
            operation="edit_message",
            transaction_id=checkpoint.transaction_id,
            content_is_prepared=True,
        )
        if delivered is None:
            return TerminalDeliveryCommit("deferred", "edit_failed")
        self.deps.conversation_cache.notify_outbound_message(
            target.room_id,
            delivered.event_id,
            delivered.content_sent,
        )
        try:
            await asyncio.to_thread(
                self.deps.turn_store.clear_terminal_checkpoint,
                current,
                expected_transaction_id=checkpoint.transaction_id,
            )
        except OSError:
            self.deps.logger.exception(
                "terminal_checkpoint_clear_failed",
                transaction_id=checkpoint.transaction_id,
            )
        return TerminalDeliveryCommit("delivered", "delivered")

    async def _cleanup_redacted_checkpoint(
        self,
        current: TurnRecord,
        checkpoint: TerminalEditCheckpoint,
    ) -> TerminalDeliveryCommit:
        """Remove any visible target whose frozen source set became invalid."""
        target = current.conversation_target
        target_event_id = current.response_event_id
        if target is None or target_event_id is None:
            return TerminalDeliveryCommit("deferred", "redaction_target_missing")
        cleaned = await self.deps.redact_message_event(
            room_id=target.room_id,
            event_id=target_event_id,
            reason="Source event was redacted",
        )
        if not cleaned:
            return TerminalDeliveryCommit("deferred", "source_redaction_cleanup_failed")
        cleared = await asyncio.to_thread(
            self.deps.turn_store.clear_redacted_terminal_checkpoint,
            current,
            expected_transaction_id=checkpoint.transaction_id,
        )
        return TerminalDeliveryCommit(
            "superseded" if cleared is not None else "deferred",
            "source_redacted" if cleared is not None else "checkpoint_clear_rejected",
        )

    async def _run(self) -> None:
        """Retry immediately, after wakeups, and at the polling interval."""
        while not self._stopping:
            self._wake_event.clear()
            await self.retry_pending()
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.deps.poll_interval_seconds,
                )


def _thaw_json(value: object) -> object:
    """Copy immutable checkpoint JSON into ordinary transport containers."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "TerminalDeliveryCommit",
    "TerminalDeliveryCoordinator",
    "TerminalDeliveryCoordinatorDeps",
    "TerminalDeliveryIntent",
]
