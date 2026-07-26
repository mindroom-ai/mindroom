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

import nio

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.hooks import MessageEnvelope
from mindroom.interactive import InteractiveMetadata
from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import send_message_result
from mindroom.message_target import MessageTarget
from mindroom.response_identity import ResponseIdentity
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import structlog

    from mindroom.delivery_gateway import ResponseHookService
    from mindroom.matrix.conversation_cache import ConversationCacheProtocol
    from mindroom.post_response_effects import PostResponseEffectsSupport
    from mindroom.runtime_protocols import SupportsClientConfig

logger = get_logger(__name__)

TERMINAL_DELIVERY_SCHEMA_VERSION = 6
DEFAULT_POLL_INTERVAL_SECONDS = 15.0

TerminalDeliveryAttemptResult = Literal["delivered", "transient", "superseded"]
TerminalDeliveryLifecycleStep = Literal["interactive", "thread_summary"]


@dataclass(frozen=True, slots=True)
class TerminalDeliveryAttempt:
    """One classified transport attempt."""

    result: TerminalDeliveryAttemptResult
    reason: str

    @classmethod
    def delivered_now(cls, reason: str = "delivered") -> TerminalDeliveryAttempt:
        """Build a successful result."""
        return cls("delivered", reason)

    @classmethod
    def transient(cls, reason: str) -> TerminalDeliveryAttempt:
        """Build a retryable result."""
        return cls("transient", reason)

    @classmethod
    def superseded(cls, reason: str) -> TerminalDeliveryAttempt:
        """Build an obsolete result."""
        return cls("superseded", reason)


@dataclass(frozen=True, slots=True)
class TerminalDeliveryIntent:
    """One frozen terminal payload committed before its first transport attempt."""

    target_event_id: str
    identity: ResponseIdentity
    interactive_metadata: InteractiveMetadata | None
    thread_summary_entity_name: str
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
    """Persisted transport and lifecycle progress."""

    delivery_id: str
    target_event_id: str
    identity: ResponseIdentity
    interactive_metadata: InteractiveMetadata | None
    thread_summary_entity_name: str
    revision: int
    body: str
    wire_content: Mapping[str, Any]
    transaction_id: str
    attempts: int
    next_attempt_at: float
    transport_delivered: bool = False
    after_response_claimed: bool = False
    completed_lifecycle_steps: tuple[TerminalDeliveryLifecycleStep, ...] = ()

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


@dataclass
class _StoreState:
    items: dict[str, PendingTerminalDelivery] = field(default_factory=dict)
    redacted: frozenset[str] = frozenset()
    lock: threading.RLock = field(default_factory=threading.RLock)
    loaded: bool = False


_STORE_STATES: dict[str, _StoreState] = {}
_STORE_STATES_LOCK = threading.Lock()


def _reset_terminal_delivery_store_runtime() -> None:
    """Forget process-local store caches for tests and forked runtimes."""
    with _STORE_STATES_LOCK:
        _STORE_STATES.clear()


@dataclass
class TerminalDeliveryStore:
    """Copy-on-write JSON store for one entity's outstanding terminal work."""

    agent_name: str
    base_path: Path
    clock: Callable[[], float] = time.time
    _store_file: Path = field(init=False)
    _lock_file: Path = field(init=False)
    _state: _StoreState = field(init=False)

    def __post_init__(self) -> None:
        """Bind this instance to its process-shared durable state."""
        if not self.agent_name or any(part in self.agent_name for part in ("..", "/", "\\")):
            message = f"Invalid terminal delivery store agent name: {self.agent_name!r}"
            raise ValueError(message)
        self._store_file = self.base_path / f"{self.agent_name}_pending_terminal_deliveries.json"
        self._lock_file = self._store_file.with_suffix(".json.lock")
        with _STORE_STATES_LOCK:
            self._state = _STORE_STATES.setdefault(str(self._store_file.absolute()), _StoreState())

    def warm(self) -> tuple[PendingTerminalDelivery, ...]:
        """Reload outstanding work from disk."""
        with self._state.lock:
            self._load(force=True)
            return tuple(self._state.items.values())

    def record(self, intent: TerminalDeliveryIntent) -> PendingTerminalDelivery | None:
        """Atomically insert or supersede one frozen intent."""
        now = self.clock()
        with self._state.lock:
            self._load()
            source_event_ids = intent.identity.source_event_ids or (intent.identity.response_envelope.source_event_id,)
            if self._state.redacted.intersection((*source_event_ids, intent.target_event_id)):
                return None
            existing = self._state.items.get(intent.delivery_id)
            if existing is not None and (intent.identity.correlation_id == existing.identity.correlation_id):
                return existing
            revision = 0 if existing is None else existing.revision + 1
            item = PendingTerminalDelivery(
                delivery_id=intent.delivery_id,
                target_event_id=intent.target_event_id,
                identity=intent.identity,
                interactive_metadata=intent.interactive_metadata,
                thread_summary_entity_name=intent.thread_summary_entity_name,
                revision=revision,
                body=intent.body,
                wire_content=json.loads(json.dumps(dict(intent.wire_content))),
                transaction_id=f"mindroom-terminal-{intent.delivery_id}-{revision}",
                attempts=0,
                next_attempt_at=now,
            )
            items = dict(self._state.items)
            items[item.delivery_id] = item
            self._commit(items, self._state.redacted)
            return item

    def get(self, delivery_id: str) -> PendingTerminalDelivery | None:
        """Return one current row."""
        with self._state.lock:
            self._load()
            return self._state.items.get(delivery_id)

    def items(self) -> tuple[PendingTerminalDelivery, ...]:
        """Return all rows."""
        with self._state.lock:
            self._load()
            return tuple(self._state.items.values())

    def pending_target_event_ids(self, room_id: str | None = None) -> frozenset[str]:
        """Return visible targets still owned by this store."""
        return frozenset(
            item.target_event_id for item in self.items() if room_id is None or item.target.room_id == room_id
        )

    def due(self, *, limit: int) -> tuple[PendingTerminalDelivery, ...]:
        """Read due work round-robin across rooms without durable leases."""
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

    def mark_transport_delivered(self, delivery_id: str, *, revision: int) -> PendingTerminalDelivery | None:
        """Checkpoint transport success."""
        return self._replace_current(delivery_id, revision, transport_delivered=True)

    def claim_after_response(self, delivery_id: str, *, revision: int) -> bool:
        """Claim the at-most-once hook before invoking it."""
        with self._state.lock:
            self._load()
            item = self._current(delivery_id, revision)
            if item is None or item.after_response_claimed:
                return False
            self._replace(item, after_response_claimed=True)
            return True

    def complete_lifecycle_step(
        self,
        delivery_id: str,
        *,
        revision: int,
        step: TerminalDeliveryLifecycleStep,
    ) -> None:
        """Checkpoint one successful effect."""
        with self._state.lock:
            self._load()
            item = self._current(delivery_id, revision)
            if item is not None:
                self._replace(
                    item,
                    completed_lifecycle_steps=tuple(dict.fromkeys((*item.completed_lifecycle_steps, step))),
                )

    def lifecycle_is_settled(self, delivery_id: str, *, revision: int) -> bool:
        """Return whether every effect has a durable terminal decision."""
        item = self.get(delivery_id)
        if item is None or item.revision != revision:
            return False
        completed = set(item.completed_lifecycle_steps)
        return item.after_response_claimed and {"interactive", "thread_summary"}.issubset(completed)

    def defer(self, delivery_id: str, *, revision: int, next_attempt_at: float) -> None:
        """Move work's next attempt forward."""
        with self._state.lock:
            self._load()
            item = self._current(delivery_id, revision)
            if item is not None:
                self._replace(item, attempts=item.attempts + 1, next_attempt_at=next_attempt_at)

    def finish(self, delivery_id: str, *, revision: int) -> None:
        """Delete one exact settled revision."""
        with self._state.lock:
            self._load()
            if self._current(delivery_id, revision) is None:
                return
            items = dict(self._state.items)
            del items[delivery_id]
            self._commit(items, self._state.redacted)

    def redact(self, *, room_id: str, event_id: str) -> None:
        """Persist a tombstone and remove rows sourced from or targeting the event."""
        with self._state.lock:
            self._load()
            items = {
                key: item
                for key, item in self._state.items.items()
                if event_id not in item.source_event_ids
                and not (item.target.room_id == room_id and item.target_event_id == event_id)
            }
            self._commit(items, self._state.redacted.union((event_id,)))

    def is_current_and_not_redacted(self, item: PendingTerminalDelivery) -> bool:
        """Revalidate an exact revision immediately before transport."""
        current = self.get(item.delivery_id)
        return (
            current is not None
            and current.revision == item.revision
            and not self._state.redacted.intersection((*item.source_event_ids, item.target_event_id))
        )

    def _replace_current(self, delivery_id: str, revision: int, **changes: object) -> PendingTerminalDelivery | None:
        with self._state.lock:
            self._load()
            item = self._current(delivery_id, revision)
            return self._replace(item, **changes) if item is not None else None

    def _replace(self, item: PendingTerminalDelivery, **changes: object) -> PendingTerminalDelivery:
        updated = replace(item, **changes)
        items = dict(self._state.items)
        items[item.delivery_id] = updated
        self._commit(items, self._state.redacted)
        return updated

    def _current(self, delivery_id: str, revision: int) -> PendingTerminalDelivery | None:
        item = self._state.items.get(delivery_id)
        return item if item is not None and item.revision == revision else None

    def _load(self, *, force: bool = False) -> None:
        if self._state.loaded and not force:
            return
        items: dict[str, PendingTerminalDelivery] = {}
        redacted: frozenset[str] = frozenset()
        if self._store_file.exists():
            try:
                raw = json.loads(self._store_file.read_text())
                if raw.get("schema_version") != TERMINAL_DELIVERY_SCHEMA_VERSION:
                    message = "unsupported terminal delivery schema"
                    raise ValueError(message)  # noqa: TRY301
                raw_items = raw.get("items", [])
                if not isinstance(raw_items, list):
                    message = "terminal delivery items must be a list"
                    raise TypeError(message)  # noqa: TRY301
                for value in raw_items:
                    try:
                        item = _item_from_record(value)
                    except (ValueError, TypeError, KeyError, AttributeError):
                        logger.warning("terminal_delivery_row_dropped", agent=self.agent_name, exc_info=True)
                        continue
                    items[item.delivery_id] = item
                redacted = frozenset(value for value in raw.get("redacted_event_ids", []) if isinstance(value, str))
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                quarantined = self._store_file.with_suffix(f".corrupt-{time.time_ns()}.json")
                with contextlib.suppress(OSError):
                    self._store_file.replace(quarantined)
                logger.warning("terminal_delivery_store_quarantined", agent=self.agent_name, exc_info=True)
        self._state.items = items
        self._state.redacted = redacted
        self._state.loaded = True

    def _commit(self, items: dict[str, PendingTerminalDelivery], redacted: frozenset[str]) -> None:
        payload = {
            "schema_version": TERMINAL_DELIVERY_SCHEMA_VERSION,
            "items": [asdict(item) for item in items.values()],
            "redacted_event_ids": sorted(redacted),
        }
        with advisory_file_lock(self._lock_file):
            write_json_file_durable(self._store_file, payload)
        self._state.items = items
        self._state.redacted = redacted
        self._state.loaded = True


def _item_from_record(raw: object) -> PendingTerminalDelivery:
    record = cast("dict[str, Any]", raw)
    identity = cast("dict[str, Any]", record["identity"])
    envelope = cast("dict[str, Any]", identity["response_envelope"])
    origin = cast("dict[str, Any]", envelope["origin"])
    metadata = record["interactive_metadata"]
    envelope_target = MessageTarget(**cast("dict[str, Any]", envelope["target"]))
    response_envelope = MessageEnvelope(
        source_event_id=envelope["source_event_id"],
        target=envelope_target,
        body=envelope["body"],
        attachment_ids=tuple(envelope["attachment_ids"]),
        mentioned_agents=tuple(envelope["mentioned_agents"]),
        agent_name=envelope["agent_name"],
        origin=TurnOrigin(
            transport_sender_id=origin["transport_sender_id"],
            requester_id=origin["requester_id"],
            sender_entity_name=origin["sender_entity_name"],
            requester_entity_name=origin["requester_entity_name"],
            sender_kind=SenderKind(origin["sender_kind"]),
            requester_kind=SenderKind(origin["requester_kind"]),
            intent=TurnIntent(origin["intent"]),
            source_kind=origin["source_kind"],
            trust=TurnTrust(origin["trust"]),
        ),
        hook_source=envelope["hook_source"],
        message_received_depth=envelope["message_received_depth"],
        dispatch_policy_source_kind=envelope["dispatch_policy_source_kind"],
    )
    interactive_metadata = (
        None
        if metadata is None
        else InteractiveMetadata(
            question_text=metadata["question_text"],
            option_map=dict(metadata["option_map"]),
            option_labels=dict(metadata["option_labels"]),
            options_list=tuple(dict(option) for option in metadata["options_list"]),
        )
    )
    return PendingTerminalDelivery(
        delivery_id=record["delivery_id"],
        target_event_id=record["target_event_id"],
        identity=ResponseIdentity(
            response_kind=identity["response_kind"],
            response_envelope=response_envelope,
            correlation_id=identity["correlation_id"],
            source_event_ids=tuple(identity["source_event_ids"]),
            thread_summary_message_count_hint=identity["thread_summary_message_count_hint"],
        ),
        interactive_metadata=interactive_metadata,
        thread_summary_entity_name=record["thread_summary_entity_name"],
        revision=record["revision"],
        body=record["body"],
        wire_content=dict(record["wire_content"]),
        transaction_id=record["transaction_id"],
        attempts=record["attempts"],
        next_attempt_at=record["next_attempt_at"],
        transport_delivered=record["transport_delivered"],
        after_response_claimed=record["after_response_claimed"],
        completed_lifecycle_steps=tuple(record["completed_lifecycle_steps"]),
    )


@dataclass(frozen=True, slots=True)
class TerminalDeliveryCommit:
    """First-attempt result and durable ownership state."""

    item: PendingTerminalDelivery | None
    attempt: TerminalDeliveryAttempt
    settled: bool
    lifecycle_managed: bool = True

    @property
    def pending(self) -> PendingTerminalDelivery | None:
        """Return work still held by the durable authority."""
        return self.item if not self.settled else None


@dataclass(frozen=True)
class TerminalDeliveryCoordinatorDeps:
    """Runtime collaborators for terminal convergence."""

    runtime: SupportsClientConfig
    store: TerminalDeliveryStore
    conversation_cache: ConversationCacheProtocol
    response_hooks: ResponseHookService
    post_response_effects: PostResponseEffectsSupport
    is_ready: Callable[[], bool]
    logger: structlog.stdlib.BoundLogger = field(default_factory=lambda: logger)
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS


@dataclass
class TerminalDeliveryCoordinator:
    """Single authority for commit, transport, redaction, lifecycle, and retries."""

    deps: TerminalDeliveryCoordinatorDeps
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
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

    async def record(self, intent: TerminalDeliveryIntent) -> PendingTerminalDelivery | None:
        """Persist an intent under terminal ordering."""
        async with self._lock:
            return await asyncio.to_thread(self.store.record, intent)

    async def commit_and_attempt(self, intent: TerminalDeliveryIntent) -> TerminalDeliveryCommit:
        """Persist before first transport, then settle transport and lifecycle."""
        async with self._lock:
            item = await asyncio.to_thread(self.store.record, intent)
            if item is None:
                return TerminalDeliveryCommit(None, TerminalDeliveryAttempt.superseded("record_rejected"), True)
            attempt = (
                TerminalDeliveryAttempt.delivered_now("transport_already_delivered")
                if item.transport_delivered
                else None
            )
            try:
                if attempt is None:
                    attempt = await self._attempt_locked(item)
                settled = await self._settle_locked(item, attempt, self.store.clock())
            except asyncio.CancelledError:
                attempt = attempt or TerminalDeliveryAttempt.transient("attempt_cancelled")
            except Exception:
                self.deps.logger.exception("terminal_delivery_first_attempt_raised", **item.log_context)
                attempt = attempt or TerminalDeliveryAttempt.transient("attempt_exception")
            else:
                return TerminalDeliveryCommit(item, attempt, settled)
            current = await asyncio.to_thread(self.store.get, item.delivery_id)
            return TerminalDeliveryCommit(current, attempt, current is None)

    async def attempt(self, item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
        """Attempt one exact persisted revision."""
        async with self._lock:
            current = await asyncio.to_thread(self.store.get, item.delivery_id)
            if current is None or current.revision != item.revision:
                return TerminalDeliveryAttempt.superseded("stale_revision")
            if current.transport_delivered:
                return TerminalDeliveryAttempt.delivered_now("transport_already_delivered")
            return await self._attempt_locked(current)

    async def settle_attempt(
        self,
        item: PendingTerminalDelivery,
        attempt: TerminalDeliveryAttempt,
        next_attempt_at: float,
    ) -> None:
        """Persist one completed attempt decision."""
        async with self._lock:
            await self._settle_locked(item, attempt, next_attempt_at)

    async def redact(self, *, room_id: str, event_id: str) -> None:
        """Tombstone before waiting for an in-flight terminal attempt."""
        self._redacting.add(event_id)
        try:
            await asyncio.to_thread(self.store.redact, room_id=room_id, event_id=event_id)
            async with self._lock:
                pass
        finally:
            self._redacting.discard(event_id)

    def pending_target_event_ids(self, room_id: str | None = None) -> frozenset[str]:
        """Return targets still durably owned."""
        return self.store.pending_target_event_ids(room_id)

    async def drain_once(self) -> int:
        """Drain one room-diverse due batch sequentially."""
        due = await asyncio.to_thread(self.store.due, limit=8)
        for item in due:
            try:
                attempt = await self.attempt(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.deps.logger.exception("terminal_delivery_attempt_raised", **item.log_context)
                attempt = TerminalDeliveryAttempt.transient("attempt_exception")
            delay = min(300.0, 2 ** min(item.attempts, 8))
            self._settlement = asyncio.create_task(
                self.settle_attempt(item, attempt, self.store.clock() + delay),
                name=f"terminal_delivery_settle_{item.delivery_id}",
            )
            await asyncio.shield(self._settlement)
            self._settlement = None
        return len(due)

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

    async def _settle_locked(
        self,
        item: PendingTerminalDelivery,
        attempt: TerminalDeliveryAttempt,
        next_attempt_at: float,
    ) -> bool:
        current = await asyncio.to_thread(self.store.get, item.delivery_id)
        if current is None or current.revision != item.revision:
            return True
        if attempt.result == "superseded":
            await asyncio.to_thread(self.store.finish, item.delivery_id, revision=item.revision)
            return True
        if attempt.result == "transient":
            await asyncio.to_thread(
                self.store.defer,
                item.delivery_id,
                revision=item.revision,
                next_attempt_at=next_attempt_at,
            )
            return False
        current = await asyncio.to_thread(self.store.mark_transport_delivered, item.delivery_id, revision=item.revision)
        if current is None or not await self._complete_lifecycle(current):
            await asyncio.to_thread(
                self.store.defer,
                item.delivery_id,
                revision=item.revision,
                next_attempt_at=next_attempt_at,
            )
            return False
        await asyncio.to_thread(self.store.finish, item.delivery_id, revision=item.revision)
        return True

    async def _attempt_locked(self, item: PendingTerminalDelivery) -> TerminalDeliveryAttempt:
        if not await asyncio.to_thread(self.store.is_current_and_not_redacted, item):
            return TerminalDeliveryAttempt.superseded("stale_or_redacted")
        client = self.deps.runtime.client
        if client is None:
            return TerminalDeliveryAttempt.transient("matrix_client_unavailable")
        target_state = await self._inspect_target(item.target.room_id, item.target_event_id)
        if target_state in {"missing", "redacted"}:
            return TerminalDeliveryAttempt.superseded(f"target_event_{target_state}")
        if self._redacting.intersection((*item.source_event_ids, item.target_event_id)) or not await asyncio.to_thread(
            self.store.is_current_and_not_redacted,
            item,
        ):
            return TerminalDeliveryAttempt.superseded("stale_or_redacted")
        delivered = await send_message_result(
            client,
            item.target.room_id,
            dict(item.wire_content),
            operation="edit_message",
            transaction_id=item.transaction_id,
            content_is_prepared=True,
        )
        if delivered is None:
            return TerminalDeliveryAttempt.transient("edit_failed")
        self.deps.conversation_cache.notify_outbound_message(
            item.target.room_id,
            delivered.event_id,
            delivered.content_sent,
        )
        return TerminalDeliveryAttempt.delivered_now()

    async def _inspect_target(self, room_id: str, event_id: str) -> Literal["ok", "missing", "redacted", "unknown"]:
        client = self.deps.runtime.client
        if client is None:
            return "unknown"
        try:
            response = await client.room_get_event(room_id, event_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return "unknown"
        if isinstance(response, nio.RoomGetEventError):
            return "missing" if response.status_code == "M_NOT_FOUND" else "unknown"
        if not isinstance(response, nio.RoomGetEventResponse):
            return "unknown"
        if isinstance(response.event, nio.RedactedEvent):
            return "redacted"
        source = response.event.source if isinstance(response.event.source, dict) else {}
        unsigned = source.get("unsigned")
        return "redacted" if isinstance(unsigned, dict) and unsigned.get("redacted_because") is not None else "ok"

    async def _complete_lifecycle(self, item: PendingTerminalDelivery) -> bool:
        if await asyncio.to_thread(
            self.store.claim_after_response,
            item.delivery_id,
            revision=item.revision,
        ):
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
        for step in ("interactive", "thread_summary"):
            current = await asyncio.to_thread(self.store.get, item.delivery_id)
            if current is None or step in current.completed_lifecycle_steps:
                continue
            try:
                await self._run_lifecycle_step(item, step)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.deps.logger.exception("terminal_delivery_lifecycle_step_failed", step=step, **item.log_context)
                return False
            await asyncio.to_thread(
                self.store.complete_lifecycle_step,
                item.delivery_id,
                revision=item.revision,
                step=step,
            )
        return await asyncio.to_thread(self.store.lifecycle_is_settled, item.delivery_id, revision=item.revision)

    async def _run_lifecycle_step(
        self,
        item: PendingTerminalDelivery,
        step: TerminalDeliveryLifecycleStep,
    ) -> None:
        key = f"{item.delivery_id}:{item.revision}:{step}"
        if step == "interactive" and item.interactive_metadata is not None:
            await self.deps.post_response_effects.register_interactive_delivery(
                event_id=item.target_event_id,
                room_id=item.target.room_id,
                target=item.target,
                interactive_metadata=item.interactive_metadata,
                agent_name=item.thread_summary_entity_name,
                idempotency_key=key,
            )
        elif step == "thread_summary":
            thread_id = item.target.resolved_thread_id
            if thread_id is not None and self.deps.post_response_effects.should_queue_thread_summary(
                item.target.room_id,
                thread_id,
                item.identity.thread_summary_message_count_hint,
            ):
                await self.deps.post_response_effects.complete_thread_summary(
                    item.target.room_id,
                    thread_id,
                    item.thread_summary_entity_name,
                    idempotency_key=key,
                )


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "TERMINAL_DELIVERY_SCHEMA_VERSION",
    "PendingTerminalDelivery",
    "TerminalDeliveryAttempt",
    "TerminalDeliveryAttemptResult",
    "TerminalDeliveryCommit",
    "TerminalDeliveryCoordinator",
    "TerminalDeliveryCoordinatorDeps",
    "TerminalDeliveryIntent",
    "TerminalDeliveryLifecycleStep",
    "TerminalDeliveryStore",
]
