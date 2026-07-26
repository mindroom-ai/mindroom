"""Durably converge committed terminal Matrix edits and their success effects."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import threading
import time
from collections import deque
from collections.abc import Mapping  # noqa: TC003
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import TypeAdapter

from mindroom.durable_write import write_json_file_durable
from mindroom.interactive import InteractiveMetadata  # noqa: TC001
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.message_target import MessageTarget  # noqa: TC001
from mindroom.response_identity import FrozenThreadSummary, ResponseIdentity  # noqa: TC001
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import structlog

    from mindroom.delivery_gateway import ResponseHookService
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.post_response_effects import PostResponseEffectsSupport
    from mindroom.runtime_protocols import SupportsClientConfig
    from mindroom.turn_store import TurnStore

logger = get_logger(__name__)

TERMINAL_DELIVERY_SCHEMA_VERSION = 7
DEFAULT_POLL_INTERVAL_SECONDS = 15.0
_ITEM_ADAPTER: TypeAdapter[PendingTerminalDelivery] | None = None

TerminalDeliveryStatus = Literal["delivered", "deferred", "superseded"]
TerminalDeliveryLifecycleStep = Literal["interactive", "thread_summary"]


@dataclass(frozen=True, slots=True)
class TerminalDeliveryIntent:
    """One frozen terminal payload committed before its first transport attempt."""

    target_event_id: str
    identity: ResponseIdentity
    interactive_metadata: InteractiveMetadata | None
    body: str
    wire_content: Mapping[str, Any]

    @property
    def delivery_id(self) -> str:
        """Return stable identity for one visible target and source turn."""
        envelope = self.identity.response_envelope
        raw = f"{envelope.agent_name}\x1f{envelope.room_id}\x1f{self.target_event_id}\x1f{envelope.source_event_id}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PendingTerminalDelivery:
    """Persisted transport, lifecycle, and handled-ledger receipt progress."""

    delivery_id: str
    target_event_id: str
    identity: ResponseIdentity
    interactive_metadata: InteractiveMetadata | None
    revision: int
    body: str
    wire_content: Mapping[str, Any]
    attempts: int
    next_attempt_at: float
    transport_delivered: bool = False
    after_response_claimed: bool = False
    completed_lifecycle_steps: tuple[TerminalDeliveryLifecycleStep, ...] = ()
    thread_summary: FrozenThreadSummary | None = None
    settled: bool = False

    @property
    def log_context(self) -> dict[str, object]:
        """Return low-cardinality structured fields."""
        return {"revision": self.revision, "attempts": self.attempts}

    @property
    def target(self) -> MessageTarget:
        """Return the response target."""
        return self.identity.response_envelope.target

    @property
    def source_event_ids(self) -> tuple[str, ...]:
        """Return every source whose redaction supersedes this delivery."""
        return self.identity.source_event_ids or (self.identity.response_envelope.source_event_id,)

    @property
    def transaction_id(self) -> str:
        """Derive the stable Matrix transaction identity without persisting it twice."""
        return f"mindroom-terminal-{self.delivery_id}-{self.revision}"


@dataclass
class TerminalDeliveryStore:
    """One-file-per-row store for outstanding terminal work and settled receipts."""

    agent_name: str
    base_path: Path
    clock: Callable[[], float] = time.time
    _store_dir: Path = field(init=False)
    _items: dict[str, PendingTerminalDelivery] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _loaded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Validate the entity key and derive its private row directory."""
        if not self.agent_name or any(part in self.agent_name for part in ("..", "/", "\\")):
            message = f"Invalid terminal delivery store agent name: {self.agent_name!r}"
            raise ValueError(message)
        self._store_dir = self.base_path / f"{self.agent_name}_pending_terminal_deliveries"

    def warm(self) -> tuple[PendingTerminalDelivery, ...]:
        """Reload outstanding work from independent row files."""
        with self._lock:
            self._load(force=True)
            return tuple(self._items.values())

    def record(self, intent: TerminalDeliveryIntent) -> PendingTerminalDelivery:
        """Atomically insert or supersede one frozen intent."""
        with self._lock:
            self._load()
            existing = self._items.get(intent.delivery_id)
            if existing is not None and intent.identity.correlation_id == existing.identity.correlation_id:
                return existing
            revision = 0 if existing is None else existing.revision + 1
            item = PendingTerminalDelivery(
                delivery_id=intent.delivery_id,
                target_event_id=intent.target_event_id,
                identity=intent.identity,
                interactive_metadata=intent.interactive_metadata,
                revision=revision,
                body=intent.body,
                wire_content=_json_mapping(intent.wire_content),
                attempts=0,
                next_attempt_at=self.clock(),
            )
            self._publish(item)
            return item

    def get(self, delivery_id: str) -> PendingTerminalDelivery | None:
        """Return one current row."""
        with self._lock:
            self._load()
            return self._items.get(delivery_id)

    def items(self) -> tuple[PendingTerminalDelivery, ...]:
        """Return all rows."""
        with self._lock:
            self._load()
            return tuple(self._items.values())

    def matching(self, *, room_id: str, event_id: str) -> tuple[PendingTerminalDelivery, ...]:
        """Return rows sourced from or targeting one redacted event."""
        return tuple(
            item
            for item in self.items()
            if event_id in item.source_event_ids
            or (item.target.room_id == room_id and item.target_event_id == event_id)
        )

    def pending_target_event_ids(self, room_id: str | None = None) -> frozenset[str]:
        """Return visible targets still owned by pending work or an unsettled receipt."""
        return frozenset(
            item.target_event_id for item in self.items() if room_id is None or item.target.room_id == room_id
        )

    def due(self, *, limit: int) -> tuple[PendingTerminalDelivery, ...]:
        """Read due work round-robin across rooms."""
        due = sorted(
            (item for item in self.items() if item.next_attempt_at <= self.clock()),
            key=lambda item: (item.next_attempt_at, item.delivery_id),
        )
        by_room: dict[str, deque[PendingTerminalDelivery]] = {}
        for item in due:
            by_room.setdefault(item.target.room_id, deque()).append(item)
        selected: list[PendingTerminalDelivery] = []
        while len(selected) < limit and any(by_room.values()):
            for queue in by_room.values():
                if queue and len(selected) < limit:
                    selected.append(queue.popleft())
        return tuple(selected)

    def update(
        self,
        delivery_id: str,
        *,
        revision: int,
        **changes: object,
    ) -> PendingTerminalDelivery | None:
        """Replace one exact row and persist only that row."""
        with self._lock:
            self._load()
            current = self._items.get(delivery_id)
            if current is None or current.revision != revision:
                return None
            updated = replace(current, **changes)
            self._publish(updated)
            return updated

    def finish(self, delivery_id: str, *, revision: int) -> None:
        """Delete one exact obsolete row or handled receipt."""
        with self._lock:
            self._load()
            current = self._items.get(delivery_id)
            if current is None or current.revision != revision:
                return
            self._row_file(delivery_id).unlink(missing_ok=True)
            del self._items[delivery_id]

    def _publish(self, item: PendingTerminalDelivery) -> None:
        self._store_dir.mkdir(parents=True, exist_ok=True)
        write_json_file_durable(
            self._row_file(item.delivery_id),
            {
                "schema_version": TERMINAL_DELIVERY_SCHEMA_VERSION,
                "item": asdict(item),
            },
            temp_dir=self._store_dir,
        )
        self._items[item.delivery_id] = item
        self._loaded = True

    def _load(self, *, force: bool = False) -> None:
        if self._loaded and not force:
            return
        items: dict[str, PendingTerminalDelivery] = {}
        if self._store_dir.exists():
            for row_file in self._store_dir.glob("*.json"):
                try:
                    raw = json.loads(row_file.read_text())
                    if not isinstance(raw, dict) or raw.get("schema_version") != TERMINAL_DELIVERY_SCHEMA_VERSION:
                        message = "unsupported terminal delivery row schema"
                        raise ValueError(message)  # noqa: TRY301
                    item = _item_from_record(raw.get("item"))
                    if row_file != self._row_file(item.delivery_id):
                        message = "terminal delivery row filename mismatch"
                        raise ValueError(message)  # noqa: TRY301
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    logger.warning("terminal_delivery_row_dropped", agent=self.agent_name, exc_info=True)
                    continue
                items[item.delivery_id] = item
        self._items = items
        self._loaded = True

    def _row_file(self, delivery_id: str) -> Path:
        if len(delivery_id) != 32 or any(character not in "0123456789abcdef" for character in delivery_id):
            message = f"Invalid terminal delivery ID: {delivery_id!r}"
            raise ValueError(message)
        return self._store_dir / f"{delivery_id}.json"


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    copied = json.loads(json.dumps(dict(value)))
    if not isinstance(copied, dict):
        message = "terminal delivery content must be a mapping"
        raise TypeError(message)
    return copied


def _item_from_record(raw: object) -> PendingTerminalDelivery:
    global _ITEM_ADAPTER
    if _ITEM_ADAPTER is None:
        adapter = TypeAdapter(PendingTerminalDelivery)
        adapter.rebuild(force=True, _types_namespace=globals())
        _ITEM_ADAPTER = adapter
    item = _ITEM_ADAPTER.validate_python(raw)
    normalized = json.loads(json.dumps(asdict(item)))
    if not _same_json_types(raw, normalized):
        message = "terminal delivery row contains coerced field types"
        raise TypeError(message)
    return item


def _same_json_types(raw: object, normalized: object) -> bool:
    """Reject Pydantic coercion while allowing JSON integers for float fields."""
    if isinstance(normalized, dict):
        raw_mapping = cast("Mapping[object, object]", raw)
        normalized_mapping = cast("Mapping[object, object]", normalized)
        return (
            isinstance(raw, dict)
            and raw_mapping.keys() == normalized_mapping.keys()
            and all(_same_json_types(raw_mapping[key], value) for key, value in normalized_mapping.items())
        )
    if isinstance(normalized, list):
        return (
            isinstance(raw, list)
            and len(raw) == len(normalized)
            and all(_same_json_types(raw_item, item) for raw_item, item in zip(raw, normalized, strict=True))
        )
    if isinstance(normalized, bool):
        return isinstance(raw, bool)
    if isinstance(normalized, float):
        return isinstance(raw, int | float) and not isinstance(raw, bool)
    return raw is None if normalized is None else type(raw) is type(normalized)


@dataclass(frozen=True, slots=True)
class TerminalDeliveryCommit:
    """First-attempt result and durable ownership state."""

    item: PendingTerminalDelivery | None
    status: TerminalDeliveryStatus
    reason: str
    lifecycle_managed: bool = True

    @property
    def settled(self) -> bool:
        """Return whether retry work is complete."""
        return self.item is None or self.item.settled or self.status == "superseded"

    @property
    def pending(self) -> PendingTerminalDelivery | None:
        """Return work still awaiting transport or lifecycle convergence."""
        return self.item if not self.settled else None


@dataclass(frozen=True)
class TerminalDeliveryCoordinatorDeps:
    """Runtime collaborators for terminal convergence."""

    runtime: SupportsClientConfig
    store: TerminalDeliveryStore
    turn_store: TurnStore
    conversation_cache: ConversationCacheProtocol
    response_hooks: ResponseHookService
    post_response_effects: PostResponseEffectsSupport
    is_ready: Callable[[], bool]
    logger: structlog.stdlib.BoundLogger = field(default_factory=lambda: logger)
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS


@dataclass
class TerminalDeliveryCoordinator:
    """Single authority for durable terminal transport and retryable success effects."""

    deps: TerminalDeliveryCoordinatorDeps
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)
    _settlement: asyncio.Task[None] | None = field(default=None, init=False)
    _redacting: set[str] = field(default_factory=set, init=False)

    @property
    def store(self) -> TerminalDeliveryStore:
        """Return the durable store."""
        return self.deps.store

    @property
    def running(self) -> bool:
        """Return whether retry polling is active."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start retry polling."""
        if not self.running:
            self._wake.set()
            self._task = asyncio.create_task(self._run(), name="terminal_delivery")

    async def stop(self) -> None:
        """Stop polling after any shielded durable settlement completes."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._settlement is not None:
            await asyncio.gather(self._settlement, return_exceptions=True)

    def wake(self, *, reason: str = "recovery_ready") -> None:
        """Wake the retry loop."""
        del reason
        self._wake.set()

    async def commit_and_attempt(self, intent: TerminalDeliveryIntent) -> TerminalDeliveryCommit:
        """Persist before first transport, then converge this delivery revision."""
        async with self._lock_for(intent.delivery_id):
            item = await self._record_locked(intent)
            if item is None:
                return TerminalDeliveryCommit(None, "superseded", "redacted")
            if item.settled:
                return TerminalDeliveryCommit(item, "delivered", "settled_receipt")
            status, reason = (
                ("delivered", "transport_already_delivered")
                if item.transport_delivered
                else await self._attempt_locked(item)
            )
            try:
                await self._settle_locked(item, status, self.store.clock())
            except asyncio.CancelledError:
                return TerminalDeliveryCommit(item, status, reason)
            except Exception:
                self.deps.logger.exception("terminal_delivery_first_attempt_raised", **item.log_context)
                return TerminalDeliveryCommit(item, status, reason)
            current = await asyncio.to_thread(self.store.get, item.delivery_id)
            return TerminalDeliveryCommit(current or item, status, reason)

    async def redact(self, *, room_id: str, event_id: str) -> None:
        """Announce redaction before durable authority and keep failures fail-closed."""
        self._redacting.add(event_id)
        await asyncio.to_thread(self.deps.turn_store.mark_source_redacted, event_id)
        for item in await asyncio.to_thread(self.store.matching, room_id=room_id, event_id=event_id):
            async with self._lock_for(item.delivery_id):
                current = await asyncio.to_thread(self.store.get, item.delivery_id)
                if current is not None and current.revision == item.revision:
                    await asyncio.to_thread(self.store.finish, item.delivery_id, revision=item.revision)
        self._redacting.discard(event_id)

    def pending_target_event_ids(self, room_id: str | None = None) -> frozenset[str]:
        """Return targets still durably owned."""
        return self.store.pending_target_event_ids(room_id)

    async def drain_once(self) -> int:
        """Drain one room-diverse due batch sequentially."""
        due = await asyncio.to_thread(self.store.due, limit=8)
        for item in due:
            self._settlement = asyncio.create_task(
                self._drain_item(item),
                name=f"terminal_delivery_settle_{item.delivery_id}",
            )
            await asyncio.shield(self._settlement)
            self._settlement = None
        return len(due)

    async def _drain_item(self, item: PendingTerminalDelivery) -> None:
        async with self._lock_for(item.delivery_id):
            current = await asyncio.to_thread(self.store.get, item.delivery_id)
            if current is None or current.revision != item.revision:
                return
            if current.settled:
                await asyncio.to_thread(self.deps.turn_store.flush)
                if all(self.deps.turn_store.is_handled(event_id) for event_id in current.source_event_ids):
                    await asyncio.to_thread(self.store.finish, current.delivery_id, revision=current.revision)
                else:
                    await self._defer(current, self.store.clock() + self.deps.poll_interval_seconds)
                return
            try:
                status, _reason = (
                    ("delivered", "transport_already_delivered")
                    if current.transport_delivered
                    else await self._attempt_locked(current)
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self.deps.logger.exception("terminal_delivery_attempt_raised", **current.log_context)
                status = "deferred"
            delay = min(300.0, 2 ** min(current.attempts, 8))
            await self._settle_locked(current, status, self.store.clock() + delay)

    async def _run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.deps.poll_interval_seconds)
            self._wake.clear()
            if self.deps.is_ready():
                try:
                    await self.drain_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.deps.logger.exception("terminal_delivery_drain_failed")

    async def _record_locked(self, intent: TerminalDeliveryIntent) -> PendingTerminalDelivery | None:
        event_ids = (*intent.identity.source_event_ids, intent.target_event_id)
        if self._redacting.intersection(event_ids) or self.deps.turn_store.any_source_redacted(event_ids):
            return None
        return await asyncio.to_thread(self.store.record, intent)

    async def _attempt_locked(self, item: PendingTerminalDelivery) -> tuple[TerminalDeliveryStatus, str]:
        if not await self._is_current_and_live(item):
            return "superseded", "stale_or_redacted"
        client = self.deps.runtime.client
        if client is None:
            return "deferred", "matrix_client_unavailable"
        delivered = await send_message_result(
            client,
            item.target.room_id,
            dict(item.wire_content),
            operation="edit_message",
            transaction_id=item.transaction_id,
            content_is_prepared=True,
        )
        if delivered is None:
            return "deferred", "edit_failed"
        self.deps.conversation_cache.notify_outbound_message(
            item.target.room_id,
            delivered.event_id,
            delivered.content_sent,
        )
        return "delivered", "delivered"

    async def _settle_locked(
        self,
        item: PendingTerminalDelivery,
        status: TerminalDeliveryStatus,
        next_attempt_at: float,
    ) -> bool:
        current = await asyncio.to_thread(self.store.get, item.delivery_id)
        if current is None or current.revision != item.revision:
            return True
        if status == "superseded":
            await asyncio.to_thread(self.store.finish, item.delivery_id, revision=item.revision)
            return True
        if status == "deferred":
            await self._defer(current, next_attempt_at)
            return False
        current = await asyncio.to_thread(
            self.store.update,
            item.delivery_id,
            revision=item.revision,
            transport_delivered=True,
        )
        if current is None:
            return True
        if not await self._complete_lifecycle(current):
            if await self._is_current_and_live(current):
                await self._defer(current, next_attempt_at)
            else:
                await asyncio.to_thread(self.store.finish, item.delivery_id, revision=item.revision)
            return False
        settled = await asyncio.to_thread(
            self.store.update,
            item.delivery_id,
            revision=item.revision,
            settled=True,
            next_attempt_at=self.store.clock() + self.deps.poll_interval_seconds,
        )
        return settled is not None

    async def _claim_after_response(self, item: PendingTerminalDelivery) -> PendingTerminalDelivery | None:
        current = await asyncio.to_thread(self.store.get, item.delivery_id)
        if current is None or not await self._is_current_and_live(current):
            return None
        if current.after_response_claimed:
            return current
        current = await asyncio.to_thread(
            self.store.update,
            item.delivery_id,
            revision=item.revision,
            after_response_claimed=True,
        )
        if current is None:
            return None
        try:
            await self.deps.response_hooks.emit_after_response(
                identity=item.identity,
                response_text=item.body,
                response_event_id=item.target_event_id,
                delivery_kind="edited",
                continue_on_cancelled=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.deps.logger.exception("terminal_delivery_lifecycle_step_failed", step="after_response")
        return current

    async def _complete_lifecycle(self, item: PendingTerminalDelivery) -> bool:
        current = await self._claim_after_response(item)
        if current is None:
            return False
        for step in ("interactive", "thread_summary"):
            current = await asyncio.to_thread(self.store.get, item.delivery_id)
            if current is None or not await self._is_current_and_live(current):
                return False
            if step in current.completed_lifecycle_steps:
                continue
            try:
                if step == "interactive":
                    await self._complete_interactive(current)
                else:
                    current = await self._complete_thread_summary(current)
                    if current is None:
                        return False
            except asyncio.CancelledError:
                raise
            except Exception:
                self.deps.logger.exception("terminal_delivery_lifecycle_step_failed", step=step, **item.log_context)
                return False
            completed = tuple(dict.fromkeys((*current.completed_lifecycle_steps, step)))
            current = await asyncio.to_thread(
                self.store.update,
                item.delivery_id,
                revision=item.revision,
                completed_lifecycle_steps=completed,
            )
            if current is None:
                return False
        return True

    async def _complete_interactive(self, item: PendingTerminalDelivery) -> None:
        if item.interactive_metadata is None:
            return
        await self.deps.post_response_effects.register_interactive_delivery(
            event_id=item.target_event_id,
            room_id=item.target.room_id,
            target=item.target,
            interactive_metadata=item.interactive_metadata,
            agent_name=item.identity.response_envelope.agent_name,
            idempotency_key=f"{item.delivery_id}:{item.revision}:interactive",
        )

    async def _complete_thread_summary(
        self,
        item: PendingTerminalDelivery,
    ) -> PendingTerminalDelivery | None:
        thread_id = item.target.resolved_thread_id
        if thread_id is None or not self.deps.post_response_effects.should_queue_thread_summary(
            item.target.room_id,
            thread_id,
            item.identity.thread_summary_message_count_hint,
        ):
            return item
        frozen = item.thread_summary
        if frozen is None:
            frozen = await self.deps.post_response_effects.prepare_thread_summary(
                item.target.room_id,
                thread_id,
                item.identity.response_envelope.agent_name,
            )
            if frozen is None:
                return item
            if not await self._is_current_and_live(item):
                return None
            updated = await asyncio.to_thread(
                self.store.update,
                item.delivery_id,
                revision=item.revision,
                thread_summary=frozen,
            )
            if updated is None:
                return None
            item = updated
        await self.deps.post_response_effects.deliver_thread_summary(
            item.target.room_id,
            thread_id,
            frozen,
            transaction_id=f"mindroom-summary-{item.delivery_id}-{item.revision}",
        )
        return item

    async def _is_current_and_live(self, item: PendingTerminalDelivery) -> bool:
        current = await asyncio.to_thread(self.store.get, item.delivery_id)
        event_ids = (*item.source_event_ids, item.target_event_id)
        return (
            current is not None
            and current.revision == item.revision
            and not self._redacting.intersection(event_ids)
            and not self.deps.turn_store.any_source_redacted(event_ids)
        )

    async def _defer(self, item: PendingTerminalDelivery, next_attempt_at: float) -> None:
        await asyncio.to_thread(
            self.store.update,
            item.delivery_id,
            revision=item.revision,
            attempts=item.attempts + 1,
            next_attempt_at=next_attempt_at,
        )

    def _lock_for(self, delivery_id: str) -> asyncio.Lock:
        return self._locks.setdefault(delivery_id, asyncio.Lock())


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "TERMINAL_DELIVERY_SCHEMA_VERSION",
    "PendingTerminalDelivery",
    "TerminalDeliveryCommit",
    "TerminalDeliveryCoordinator",
    "TerminalDeliveryCoordinatorDeps",
    "TerminalDeliveryIntent",
    "TerminalDeliveryLifecycleStep",
    "TerminalDeliveryStatus",
    "TerminalDeliveryStore",
]
