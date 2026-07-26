"""Converge frozen terminal edits stored on canonical TurnRecords."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast
from weakref import WeakValueDictionary

from pydantic import TypeAdapter

from mindroom.handled_turns import TerminalEditCheckpoint, TurnRecord
from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.matrix.client_delivery import send_message_result

# Pydantic resolves these annotations from module globals when rebuilding the envelope adapter.
from mindroom.message_target import MessageTarget  # noqa: TC001
from mindroom.response_identity import ResponseIdentity

# Pydantic also needs the nested turn-origin annotation names in this module namespace.
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    import structlog

    from mindroom.delivery_gateway import ResponseHookService
    from mindroom.matrix.client_delivery import DeliveredMatrixEvent
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.post_response_effects import PostResponseEffectsSupport
    from mindroom.runtime_protocols import SupportsClientConfig
    from mindroom.turn_store import TurnStore

_TerminalDeliveryStatus = Literal["delivered", "deferred", "superseded"]
_MAX_RETRY_CONCURRENCY = 8
_ENVELOPE_ADAPTER: TypeAdapter[MessageEnvelope] | None = None
_INTERACTIVE_ADAPTER = TypeAdapter(InteractiveMetadata)


@dataclass(frozen=True, slots=True)
class TerminalDeliveryIntent:
    """One exact terminal edit ready for durable checkpoint commit."""

    target_event_id: str
    target_was_placeholder: bool
    identity: ResponseIdentity
    interactive_metadata: InteractiveMetadata | None
    body: str
    wire_content: Mapping[str, Any]

    @property
    def transaction_id(self) -> str:
        """Derive one stable transaction from the owner, target, and exact payload."""
        envelope = self.identity.response_envelope
        raw = json.dumps(
            [
                envelope.agent_name,
                envelope.room_id,
                envelope.source_event_id,
                self.identity.correlation_id,
                self.target_event_id,
                dict(self.wire_content),
            ],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return f"mindroom-terminal-{hashlib.sha256(raw.encode()).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class PendingTerminalDelivery:
    """Runtime view of one canonical TurnRecord checkpoint."""

    target_event_id: str
    target_was_placeholder: bool


@dataclass(frozen=True, slots=True)
class TerminalDeliveryCommit:
    """Result of checkpoint commit and its immediate convergence attempt."""

    status: _TerminalDeliveryStatus
    reason: str
    lifecycle_managed: bool = True


@dataclass(frozen=True, slots=True)
class TerminalDeliveryCoordinatorDeps:
    """Collaborators for checkpoint transport and visible-response effects."""

    runtime: SupportsClientConfig
    turn_store: TurnStore
    conversation_cache: ConversationCacheProtocol
    response_hooks: ResponseHookService
    post_response_effects: PostResponseEffectsSupport
    redact_message_event: Callable[..., Awaitable[bool]]
    is_ready: Callable[[], bool]
    logger: structlog.stdlib.BoundLogger
    poll_interval_seconds: float = 15.0


@dataclass
class TerminalDeliveryCoordinator:
    """Retry exact terminal edits from the canonical TurnStore authority."""

    deps: TerminalDeliveryCoordinatorDeps
    _locks: WeakValueDictionary[str, asyncio.Lock] = field(default_factory=WeakValueDictionary, init=False)
    _wake_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False)
    _stopping: bool = field(default=False, init=False)

    async def warm(self) -> int:
        """Return the number of checkpoints loaded by TurnStore startup."""
        return len(await asyncio.to_thread(self.deps.turn_store.terminal_checkpoint_records))

    def start(self) -> None:
        """Start the small periodic checkpoint retry loop."""
        if self._worker is not None and not self._worker.done():
            return
        self._stopping = False
        self._wake_event.set()
        self._worker = asyncio.create_task(self._run(), name="terminal_checkpoint_retry")

    async def stop(self) -> None:
        """Cancel network/effect work while allowing an active disk mutation to drain."""
        self._stopping = True
        worker, self._worker = self._worker, None
        if worker is None:
            return
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    def wake(self, *, reason: str) -> None:
        """Wake retry after Matrix readiness changes."""
        del reason
        self._wake_event.set()

    @asynccontextmanager
    async def stale_cleanup_guard(self, target_event_id: str) -> AsyncIterator[bool]:
        """Serialize stale cleanup with delivery and recheck durable ownership."""
        async with self._locked(None, target_event_id):
            record = await asyncio.to_thread(self.deps.turn_store.turn_for_event, target_event_id)
            is_owned = (
                record is not None
                and record.response_event_id == target_event_id
                and (
                    record.terminal_edit_checkpoint is not None
                    or record.settled_terminal_delivery_correlation_id is not None
                )
            )
            yield not is_owned

    async def owned_delivery(self, identity: ResponseIdentity) -> PendingTerminalDelivery | None:
        """Return a durable outcome only when all replay IDs belong to one episode."""
        source_event_ids = identity.source_event_ids or (identity.response_envelope.source_event_id,)
        record = await asyncio.to_thread(self._turn_for_sources, source_event_ids)
        if record is None or record.response_event_id is None:
            return None
        checkpoint = record.terminal_edit_checkpoint
        if checkpoint is not None:
            if (
                checkpoint.correlation_id != identity.correlation_id
                or checkpoint.response_kind != identity.response_kind
            ):
                return None
            return PendingTerminalDelivery(record.response_event_id, checkpoint.target_was_placeholder)
        if (
            not record.completed
            or record.settled_terminal_delivery_correlation_id != identity.correlation_id
            or record.response_owner != identity.response_envelope.agent_name
        ):
            return None
        # A cleared checkpoint means transport and lifecycle effects converged.
        # Retaining episode ownership prevents a sync-restart retry from racing
        # the final checkpoint clear and duplicating the already-delivered turn.
        return PendingTerminalDelivery(record.response_event_id, target_was_placeholder=False)

    async def commit_and_attempt(self, intent: TerminalDeliveryIntent) -> TerminalDeliveryCommit:
        """Commit exact content durably before the first Matrix edit."""
        source_event_ids = intent.identity.source_event_ids or (intent.identity.response_envelope.source_event_id,)
        checkpoint = TerminalEditCheckpoint(
            transaction_id=intent.transaction_id,
            wire_content=intent.wire_content,
            response_text=intent.body,
            response_kind=intent.identity.response_kind,
            target_was_placeholder=intent.target_was_placeholder,
            response_envelope=_dump_envelope(intent.identity.response_envelope),
            correlation_id=intent.identity.correlation_id,
            interactive_metadata=(
                _INTERACTIVE_ADAPTER.dump_python(intent.interactive_metadata, mode="json")
                if intent.interactive_metadata is not None
                else None
            ),
        )
        while True:
            authority = await asyncio.to_thread(self._turn_for_sources, source_event_ids)
            if authority is None:
                return TerminalDeliveryCommit("superseded", "turn_authority_missing")
            async with self._locked(authority, intent.target_event_id):
                try:
                    committed = await _durable_call(
                        self.deps.turn_store.commit_terminal_checkpoint,
                        authority,
                        response_event_id=intent.target_event_id,
                        checkpoint=checkpoint,
                        regeneration_turn_record=intent.identity.regeneration_turn_record,
                    )
                except OSError:
                    if intent.identity.regeneration_turn_record is None:
                        raise
                    self.deps.logger.exception(
                        "terminal_checkpoint_initial_persist_failed",
                        transaction_id=checkpoint.transaction_id,
                    )
                else:
                    if committed is None:
                        return TerminalDeliveryCommit("superseded", "checkpoint_rejected")
                    return await self._attempt_locked(committed)
            await asyncio.sleep(self.deps.poll_interval_seconds)

    async def retry_pending(self) -> None:
        """Attempt every unique checkpoint once."""
        records = await asyncio.to_thread(self.deps.turn_store.terminal_checkpoint_records)
        semaphore = asyncio.Semaphore(_MAX_RETRY_CONCURRENCY)
        records_by_room: dict[str, list[TurnRecord]] = {}
        for record in records:
            room_key = (
                record.conversation_target.room_id
                if record.conversation_target is not None
                else f"turn:{json.dumps(record.indexed_event_ids, separators=(',', ':'))}"
            )
            records_by_room.setdefault(room_key, []).append(record)

        async def retry_room(room_records: list[TurnRecord]) -> None:
            for record in room_records:
                target_event_id = record.response_event_id
                if target_event_id is None:
                    continue
                try:
                    async with semaphore, self._locked(record, target_event_id):
                        await self._attempt_locked(record)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.deps.logger.exception(
                        "terminal_checkpoint_attempt_failed",
                        source_event_ids=record.source_event_ids,
                        target_event_id=target_event_id,
                    )

        async with asyncio.TaskGroup() as retries:
            for room_records in records_by_room.values():
                retries.create_task(retry_room(room_records))

    async def redact(self, *, room_id: str, event_id: str) -> None:
        """Tombstone source or target and clear its checkpoint under shared locks."""
        owner = await asyncio.to_thread(self.deps.turn_store.turn_for_event, event_id)
        if owner is not None and owner.conversation_target is not None and owner.conversation_target.room_id != room_id:
            return
        checkpoint = owner.terminal_edit_checkpoint if owner is not None else None
        target_event_id = (
            owner.response_event_id
            if owner is not None and event_id in owner.indexed_event_ids and checkpoint is not None
            else None
        )
        async with self._locked(
            owner,
            event_id,
            additional_event_ids=((target_event_id,) if target_event_id is not None else ()),
        ):
            redacted = await _durable_call(
                self.deps.turn_store.mark_source_redacted,
                event_id,
                fallback_terminal_checkpoint=checkpoint,
                fallback_response_event_id=target_event_id,
            )
            if redacted is not None and redacted.terminal_edit_checkpoint is not None:
                try:
                    await self._attempt_locked(redacted)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.deps.logger.exception(
                        "terminal_checkpoint_redaction_cleanup_failed",
                        source_event_id=event_id,
                        target_event_id=redacted.response_event_id,
                    )

    def _turn_for_sources(self, source_event_ids: tuple[str, ...]) -> TurnRecord | None:
        records = tuple(self.deps.turn_store.get_turn_record(event_id) for event_id in source_event_ids)
        owners = {record.indexed_event_ids: record for record in records if record is not None}
        if len(owners) != 1:
            return None
        owner = next(iter(owners.values()))
        return owner if set(source_event_ids).issubset(owner.indexed_event_ids) else None

    async def _attempt_locked(self, record: TurnRecord) -> TerminalDeliveryCommit:
        current = await asyncio.to_thread(
            self.deps.turn_store.terminal_checkpoint_for_sources,
            record.indexed_event_ids,
        )
        checkpoint = record.terminal_edit_checkpoint
        if (
            current is None
            or checkpoint is None
            or current.terminal_edit_checkpoint is None
            or current.terminal_edit_checkpoint.transaction_id != checkpoint.transaction_id
        ):
            return TerminalDeliveryCommit("superseded", "checkpoint_replaced")
        client = self.deps.runtime.client
        if self._stopping or client is None or not self.deps.is_ready():
            return TerminalDeliveryCommit("deferred", "matrix_not_ready")
        target = _load_envelope(checkpoint.response_envelope).target
        if current.redacted_source_event_ids != checkpoint.accepted_redacted_source_event_ids:
            return await self._cleanup_redacted_checkpoint(current, checkpoint, target)
        delivered = await send_message_result(
            client,
            target.room_id,
            cast("dict[str, object]", _thaw_checkpoint_json(checkpoint.wire_content)),
            operation="edit_message",
            transaction_id=checkpoint.transaction_id,
            content_is_prepared=True,
        )
        if delivered is None:
            return TerminalDeliveryCommit("deferred", "edit_failed")
        if self._stopping:
            raise asyncio.CancelledError
        return await self._finalize_accepted_delivery(record, checkpoint, target, delivered)

    async def _cleanup_redacted_checkpoint(
        self,
        current: TurnRecord,
        checkpoint: TerminalEditCheckpoint,
        target: MessageTarget,
    ) -> TerminalDeliveryCommit:
        """Remove a visible target whose source was redacted."""
        cleaned = await self.deps.redact_message_event(
            room_id=target.room_id,
            event_id=cast("str", current.response_event_id),
            reason="Source event was redacted",
        )
        if not cleaned:
            return TerminalDeliveryCommit("deferred", "source_redaction_cleanup_failed")
        try:
            cleared = await _durable_call(
                self.deps.turn_store.clear_redacted_terminal_checkpoint,
                current,
                expected_transaction_id=checkpoint.transaction_id,
            )
        except OSError:
            self.deps.logger.exception(
                "terminal_checkpoint_redaction_cleanup_persist_failed",
                transaction_id=checkpoint.transaction_id,
            )
            return TerminalDeliveryCommit("deferred", "lifecycle_persist_failed")
        return TerminalDeliveryCommit(
            "superseded" if cleared is not None else "deferred",
            "source_redacted_cleanup" if cleared is not None else "checkpoint_clear_rejected",
        )

    async def _finalize_accepted_delivery(
        self,
        record: TurnRecord,
        checkpoint: TerminalEditCheckpoint,
        target: MessageTarget,
        delivered: DeliveredMatrixEvent,
    ) -> TerminalDeliveryCommit:
        """Converge lifecycle effects after Matrix accepted the terminal edit."""
        try:
            current = await asyncio.to_thread(
                self.deps.turn_store.terminal_checkpoint_for_sources,
                record.indexed_event_ids,
            )
            if current is None or current.terminal_edit_checkpoint is None:
                return TerminalDeliveryCommit("superseded", "checkpoint_redacted")
            self.deps.conversation_cache.notify_outbound_message(
                target.room_id,
                delivered.event_id,
                delivered.content_sent,
            )
            claimed = await self._claim_after_response(current)
            if claimed is None:
                return TerminalDeliveryCommit("superseded", "checkpoint_redacted")
            completed = await self._complete_interactive(claimed)
            if completed is None:
                latest = await asyncio.to_thread(
                    self.deps.turn_store.terminal_checkpoint_for_sources,
                    record.indexed_event_ids,
                )
                return TerminalDeliveryCommit(
                    "deferred" if latest is not None else "superseded",
                    "interactive_failed",
                )
            cleared = await _durable_call(
                self.deps.turn_store.clear_terminal_checkpoint,
                completed,
                expected_transaction_id=checkpoint.transaction_id,
            )
        except OSError:
            self.deps.logger.exception(
                "terminal_checkpoint_lifecycle_persist_failed",
                transaction_id=checkpoint.transaction_id,
            )
            return TerminalDeliveryCommit("deferred", "lifecycle_persist_failed")
        if cleared is None:
            return TerminalDeliveryCommit("superseded", "checkpoint_clear_rejected")
        return TerminalDeliveryCommit("delivered", "delivered")

    async def _claim_after_response(self, record: TurnRecord) -> TurnRecord | None:
        checkpoint = record.terminal_edit_checkpoint
        assert checkpoint is not None
        if checkpoint.after_response_claimed:
            return record
        claimed = await _durable_call(
            self.deps.turn_store.update_terminal_checkpoint,
            record,
            expected_transaction_id=checkpoint.transaction_id,
            update=lambda current: replace(current, after_response_claimed=True),
        )
        if claimed is None:
            return None
        try:
            await self.deps.response_hooks.emit_after_response(
                identity=_checkpoint_identity(claimed),
                response_text=checkpoint.response_text,
                response_event_id=cast("str", claimed.response_event_id),
                delivery_kind="edited",
                continue_on_cancelled=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.deps.logger.exception("terminal_checkpoint_after_response_failed")
        return claimed

    async def _complete_interactive(self, record: TurnRecord) -> TurnRecord | None:
        checkpoint = record.terminal_edit_checkpoint
        assert checkpoint is not None
        if checkpoint.interactive_metadata is None or checkpoint.interactive_completed:
            return record
        metadata = _INTERACTIVE_ADAPTER.validate_python(_thaw_checkpoint_json(checkpoint.interactive_metadata))
        identity = _checkpoint_identity(record)
        try:
            await self.deps.post_response_effects.register_interactive_delivery(
                event_id=cast("str", record.response_event_id),
                room_id=identity.response_envelope.room_id,
                target=identity.response_envelope.target,
                interactive_metadata=metadata,
                agent_name=record.response_owner or identity.response_envelope.agent_name,
                idempotency_key=f"terminal:{checkpoint.transaction_id}",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.deps.logger.exception("terminal_checkpoint_interactive_failed")
            return None
        return await _durable_call(
            self.deps.turn_store.update_terminal_checkpoint,
            record,
            expected_transaction_id=checkpoint.transaction_id,
            update=lambda current: replace(current, interactive_completed=True),
        )

    @asynccontextmanager
    async def _locked(
        self,
        record: TurnRecord | None,
        event_id: str,
        *,
        additional_event_ids: tuple[str, ...] = (),
    ) -> AsyncIterator[None]:
        keys = {f"event:{event_id}", *(f"event:{extra_event_id}" for extra_event_id in additional_event_ids)}
        if record is not None:
            keys.add(f"turn:{json.dumps(record.indexed_event_ids, separators=(',', ':'))}")
        locks = [self._locks.setdefault(key, asyncio.Lock()) for key in sorted(keys)]
        acquired: list[asyncio.Lock] = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    async def _run(self) -> None:
        while True:
            self._wake_event.clear()
            try:
                await self.retry_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.deps.logger.exception("terminal_checkpoint_retry_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.deps.poll_interval_seconds,
                )


async def _durable_call[ResultT](
    operation: Callable[..., ResultT],
    *args: object,
    **kwargs: object,
) -> ResultT:
    """Drain one disk writer before propagating caller cancellation."""
    writer = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(writer)
    except asyncio.CancelledError:
        while not writer.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(writer)
        with contextlib.suppress(Exception):
            writer.result()
        raise


def _dump_envelope(envelope: MessageEnvelope) -> Mapping[str, object]:
    return cast("Mapping[str, object]", _envelope_adapter().dump_python(envelope, mode="json"))


def _envelope_adapter() -> TypeAdapter[MessageEnvelope]:
    global _ENVELOPE_ADAPTER
    if _ENVELOPE_ADAPTER is None:
        _ENVELOPE_ADAPTER = TypeAdapter(MessageEnvelope)
        _ENVELOPE_ADAPTER.rebuild(force=True, _types_namespace=globals())
    return _ENVELOPE_ADAPTER


def _load_envelope(raw: Mapping[str, object]) -> MessageEnvelope:
    return _envelope_adapter().validate_python(_thaw_checkpoint_json(raw))


def _thaw_checkpoint_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_checkpoint_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_checkpoint_json(item) for item in value]
    return value


def _checkpoint_identity(record: TurnRecord) -> ResponseIdentity:
    checkpoint = record.terminal_edit_checkpoint
    assert checkpoint is not None
    return ResponseIdentity(
        response_kind=checkpoint.response_kind,
        response_envelope=_load_envelope(checkpoint.response_envelope),
        correlation_id=checkpoint.correlation_id,
        source_event_ids=record.source_event_ids,
    )


__all__ = [
    "PendingTerminalDelivery",
    "TerminalDeliveryCommit",
    "TerminalDeliveryCoordinator",
    "TerminalDeliveryCoordinatorDeps",
    "TerminalDeliveryIntent",
]
