"""Durable pending terminal Matrix deliveries for one runtime entity.

MindRoom commits a terminal response outcome before Matrix necessarily accepts
the edit or send that makes it visible. When the transport is temporarily
unavailable - limited-sync timeline recovery, rate limiting, a network blip -
the immediate bounded retry in ``mindroom.matrix.client_delivery`` can still be
exhausted. This module records that committed-but-undelivered intent durably so
a later attempt can converge instead of leaving a permanently stuck placeholder.

State machine (see ``docs/architecture/durable-terminal-delivery.md``)::

    record ---> pending ---> attempting ---> delivered
                  ^             |  |  |
                  |             |  |  +---> dead_letter   (permanent failure)
                  |             |  +------> superseded    (newer terminal intent)
                  +-- retry_wait <-+        (transient failure, backoff)

Every transition is applied to shared in-memory state and durably persisted
under one advisory file lock before it is observable, so a crash always leaves
exactly one valid recoverable state. Loading the store converts any leaked
``attempting`` row back into a due ``retry_wait`` row.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal
from uuid import uuid4

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.tool_system.events import ToolTraceEntry

if typing.TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

TERMINAL_DELIVERY_SCHEMA_VERSION = 1
_SCHEMA_VERSION_KEY = "schema_version"
_ITEMS_KEY = "items"

TerminalDeliveryState = Literal[
    "pending",
    "attempting",
    "retry_wait",
    "delivered",
    "superseded",
    "dead_letter",
]
TerminalOutcomeKind = Literal["completed", "cancelled", "error"]
TerminalDeliveryKind = Literal["edit", "send"]

_SETTLED_STATES: frozenset[str] = frozenset({"delivered", "superseded", "dead_letter"})
_OUTCOME_PRECEDENCE: Mapping[str, int] = {"error": 0, "cancelled": 1, "completed": 2}

# One process owns semantic ordering for an entity, so a lease only has to
# outlive a single attempt; anything longer is a crash and must be recovered.
DEFAULT_ATTEMPT_LEASE_SECONDS = 120.0
# Settled rows are kept briefly so restart-time recovery and observability can
# still see what happened, then compacted. Unsettled rows never expire by age.
_DEFAULT_SETTLED_RETENTION_SECONDS = 24 * 60 * 60
_DEFAULT_MAX_SETTLED_ITEMS = 500
# A hard cap keeps a pathological outage from growing the file without bound.
_DEFAULT_MAX_UNSETTLED_ITEMS = 2000

# Identifies the current process for lease ownership and stale-generation checks.
RUNTIME_GENERATION = uuid4().hex


@dataclass(frozen=True, slots=True)
class TerminalDeliveryIntent:
    """One committed terminal outcome that still needs to become visible."""

    agent_name: str
    target: MessageTarget
    target_event_id: str | None
    anchor_event_id: str
    source_event_ids: tuple[str, ...]
    outcome_kind: TerminalOutcomeKind
    body: str
    correlation_id: str | None = None
    response_kind: str | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    extra_content: Mapping[str, Any] | None = None
    skip_mentions: bool = False
    runtime_generation: str = RUNTIME_GENERATION

    @property
    def delivery_kind(self) -> TerminalDeliveryKind:
        """Return whether this intent replaces an existing event or creates one."""
        return "edit" if self.target_event_id is not None else "send"

    @property
    def delivery_id(self) -> str:
        """Return the stable identity shared by every intent for one visible target."""
        return terminal_delivery_id(
            agent_name=self.agent_name,
            room_id=self.target.room_id,
            target_event_id=self.target_event_id,
            anchor_event_id=self.anchor_event_id,
        )


def terminal_delivery_id(
    *,
    agent_name: str,
    room_id: str,
    target_event_id: str | None,
    anchor_event_id: str,
) -> str:
    """Return the deterministic identity for one terminal delivery target."""
    digest_source = "\x1f".join((agent_name, room_id, target_event_id or "", anchor_event_id))
    return hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PendingTerminalDelivery:
    """Durable, restart-safe record of one pending terminal delivery."""

    delivery_id: str
    agent_name: str
    target: MessageTarget
    target_event_id: str | None
    anchor_event_id: str
    source_event_ids: tuple[str, ...]
    outcome_kind: TerminalOutcomeKind
    revision: int
    body: str
    correlation_id: str | None
    response_kind: str | None
    tool_trace: tuple[ToolTraceEntry, ...]
    extra_content: Mapping[str, Any] | None
    skip_mentions: bool
    runtime_generation: str
    state: TerminalDeliveryState
    attempts: int
    created_at: float
    updated_at: float
    next_attempt_at: float
    last_attempt_at: float | None = None
    last_error: str | None = None
    lease_expires_at: float | None = None
    settled_reason: str | None = None

    @property
    def delivery_kind(self) -> TerminalDeliveryKind:
        """Return whether this record replaces an existing event or creates one."""
        return "edit" if self.target_event_id is not None else "send"

    @property
    def is_settled(self) -> bool:
        """Return whether this record has reached a terminal state."""
        return self.state in _SETTLED_STATES

    @property
    def outcome_precedence(self) -> int:
        """Return the precedence rank used to stop stale outcomes overwriting newer ones."""
        return _OUTCOME_PRECEDENCE[self.outcome_kind]

    @property
    def transaction_id(self) -> str:
        """Return the deterministic Matrix transaction ID for this exact revision."""
        return f"mindroom-td-{self.delivery_id}-{self.revision}"

    @property
    def log_context(self) -> dict[str, object]:
        """Return identifier-free structured log fields for this record."""
        return {
            "delivery_kind": self.delivery_kind,
            "outcome_kind": self.outcome_kind,
            "revision": self.revision,
            "state": self.state,
            "attempts": self.attempts,
        }

    def to_record(self) -> dict[str, Any]:
        """Return the JSON-safe persisted representation of this record."""
        return {
            "delivery_id": self.delivery_id,
            "agent_name": self.agent_name,
            "target": dict(self.target.to_metadata()),
            "target_event_id": self.target_event_id,
            "anchor_event_id": self.anchor_event_id,
            "source_event_ids": list(self.source_event_ids),
            "outcome_kind": self.outcome_kind,
            "revision": self.revision,
            "body": self.body,
            "correlation_id": self.correlation_id,
            "response_kind": self.response_kind,
            "tool_trace": [_tool_trace_entry_to_record(entry) for entry in self.tool_trace],
            "extra_content": dict(self.extra_content) if self.extra_content is not None else None,
            "skip_mentions": self.skip_mentions,
            "runtime_generation": self.runtime_generation,
            "state": self.state,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_attempt_at": self.next_attempt_at,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "lease_expires_at": self.lease_expires_at,
            "settled_reason": self.settled_reason,
        }

    @classmethod
    def from_record(cls, raw_record: object) -> PendingTerminalDelivery | None:
        """Parse one persisted record, rejecting anything not matching the current schema."""
        if not isinstance(raw_record, Mapping):
            return None
        record = typing.cast("Mapping[str, object]", raw_record)
        delivery_id = _required_string(record.get("delivery_id"))
        agent_name = _required_string(record.get("agent_name"))
        anchor_event_id = _required_string(record.get("anchor_event_id"))
        target = MessageTarget.from_metadata(record.get("target"))
        outcome_kind = record.get("outcome_kind")
        state = record.get("state")
        revision = record.get("revision")
        body = record.get("body")
        attempts = record.get("attempts")
        if (
            delivery_id is None
            or agent_name is None
            or anchor_event_id is None
            or target is None
            or outcome_kind not in _OUTCOME_PRECEDENCE
            or state not in typing.get_args(TerminalDeliveryState)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or not isinstance(body, str)
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
        ):
            return None
        raw_extra_content = record.get("extra_content")
        return cls(
            delivery_id=delivery_id,
            agent_name=agent_name,
            target=target,
            target_event_id=_optional_string(record.get("target_event_id")),
            anchor_event_id=anchor_event_id,
            source_event_ids=_string_tuple(record.get("source_event_ids")),
            outcome_kind=typing.cast("TerminalOutcomeKind", outcome_kind),
            revision=revision,
            body=body,
            correlation_id=_optional_string(record.get("correlation_id")),
            response_kind=_optional_string(record.get("response_kind")),
            tool_trace=_tool_trace_from_record(record.get("tool_trace")),
            extra_content=_string_keyed_dict(raw_extra_content),
            skip_mentions=record.get("skip_mentions") is True,
            runtime_generation=_optional_string(record.get("runtime_generation")) or "",
            state=typing.cast("TerminalDeliveryState", state),
            attempts=max(0, attempts),
            created_at=_float_or(record.get("created_at"), 0.0),
            updated_at=_float_or(record.get("updated_at"), 0.0),
            next_attempt_at=_float_or(record.get("next_attempt_at"), 0.0),
            last_attempt_at=_optional_float(record.get("last_attempt_at")),
            last_error=_optional_string(record.get("last_error")),
            lease_expires_at=_optional_float(record.get("lease_expires_at")),
            settled_reason=_optional_string(record.get("settled_reason")),
        )


TerminalDeliveryAttemptResult = Literal["delivered", "transient", "permanent", "superseded"]


@dataclass(frozen=True, slots=True)
class TerminalDeliveryAttempt:
    """Outcome of one durable terminal delivery attempt."""

    result: TerminalDeliveryAttemptResult
    reason: str
    retry_after_seconds: float | None = None

    @classmethod
    def delivered_now(cls, reason: str = "delivered") -> TerminalDeliveryAttempt:
        """Return the successful attempt outcome."""
        return cls(result="delivered", reason=reason)

    @classmethod
    def transient(cls, reason: str, *, retry_after_seconds: float | None = None) -> TerminalDeliveryAttempt:
        """Return a retryable attempt outcome."""
        return cls(result="transient", reason=reason, retry_after_seconds=retry_after_seconds)

    @classmethod
    def permanent(cls, reason: str) -> TerminalDeliveryAttempt:
        """Return an attempt outcome that can never succeed."""
        return cls(result="permanent", reason=reason)

    @classmethod
    def superseded(cls, reason: str) -> TerminalDeliveryAttempt:
        """Return an attempt outcome invalidated by newer valid state."""
        return cls(result="superseded", reason=reason)


@dataclass(frozen=True, slots=True)
class TerminalDeliveryBacklog:
    """Bounded observability snapshot of the durable backlog."""

    unsettled_count: int
    dead_letter_count: int
    oldest_unsettled_age_seconds: float
    max_attempts: int
    unsettled_by_room: Mapping[str, int]
    unsettled_by_outcome: Mapping[str, int]


@dataclass
class _StoreState:
    """Shared in-memory records for one durable terminal-delivery file."""

    items: dict[str, PendingTerminalDelivery] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    loaded: bool = False


_STORE_STATES: dict[str, _StoreState] = {}
_STORE_RUNTIME_LOCK = threading.Lock()


def _shared_store_state(store_file: Path) -> _StoreState:
    """Return the process-wide shared state for one durable store file."""
    key = str(store_file.absolute())
    with _STORE_RUNTIME_LOCK:
        state = _STORE_STATES.get(key)
        if state is None:
            state = _StoreState()
            _STORE_STATES[key] = state
        return state


def _reset_terminal_delivery_store_runtime() -> None:
    """Drop shared store state (tests and forked runtimes)."""
    with _STORE_RUNTIME_LOCK:
        _STORE_STATES.clear()


@dataclass
class TerminalDeliveryStore:
    """Persist and transition pending terminal deliveries for one entity."""

    agent_name: str
    base_path: Path
    clock: typing.Callable[[], float] = time.time
    attempt_lease_seconds: float = DEFAULT_ATTEMPT_LEASE_SECONDS
    settled_retention_seconds: float = _DEFAULT_SETTLED_RETENTION_SECONDS
    max_settled_items: int = _DEFAULT_MAX_SETTLED_ITEMS
    max_unsettled_items: int = _DEFAULT_MAX_UNSETTLED_ITEMS
    _store_file: Path = field(init=False, repr=False)
    _lock_file: Path = field(init=False, repr=False)
    _state: _StoreState = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind shared state for this entity without touching the filesystem."""
        self._store_file = _store_file_path(self.base_path, self.agent_name)
        self._lock_file = self._store_file.with_suffix(f"{self._store_file.suffix}.lock")
        self._state = _shared_store_state(self._store_file)

    @property
    def store_file(self) -> Path:
        """Return the durable file backing this store."""
        return self._store_file

    def warm(self) -> tuple[PendingTerminalDelivery, ...]:
        """Load, repair, and compact durable state; call from a worker thread."""
        with self._state.lock:
            self._ensure_loaded_locked(force=True)
            # Startup owns no leases yet, so every attempting row is leaked by
            # definition regardless of the lease deadline it was written with.
            recovered = self._recover_leaked_attempts_locked(reason="process_restart", force=True)
            self._compact_locked()
            self._write_locked()
            return recovered

    def record(self, intent: TerminalDeliveryIntent) -> PendingTerminalDelivery | None:
        """Persist one committed terminal intent, applying precedence against existing state.

        Returns the stored record, or ``None`` when the intent is not durably
        representable. A stale intent is stored as ``superseded`` so the loss is
        observable rather than silent.
        """
        sanitized_extra_content = _json_safe_mapping(intent.extra_content)
        if sanitized_extra_content is _UNSERIALIZABLE:
            logger.error(
                "terminal_delivery_intent_not_serializable",
                agent=self.agent_name,
                outcome_kind=intent.outcome_kind,
                delivery_kind=intent.delivery_kind,
            )
            return None
        now = self.clock()
        with self._state.lock:
            self._ensure_loaded_locked()
            delivery_id = intent.delivery_id
            existing = self._state.items.get(delivery_id)
            candidate = PendingTerminalDelivery(
                delivery_id=delivery_id,
                agent_name=intent.agent_name,
                target=intent.target,
                target_event_id=intent.target_event_id,
                anchor_event_id=intent.anchor_event_id,
                source_event_ids=tuple(intent.source_event_ids),
                outcome_kind=intent.outcome_kind,
                revision=0 if existing is None else existing.revision + 1,
                body=intent.body,
                correlation_id=intent.correlation_id,
                response_kind=intent.response_kind,
                tool_trace=tuple(intent.tool_trace),
                extra_content=typing.cast("Mapping[str, Any] | None", sanitized_extra_content),
                skip_mentions=intent.skip_mentions,
                runtime_generation=intent.runtime_generation,
                state="pending",
                attempts=0,
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
                next_attempt_at=now,
            )
            if existing is not None and not _supersedes(candidate, existing):
                stale = replace(
                    candidate,
                    revision=existing.revision,
                    state="superseded",
                    settled_reason="stale_terminal_intent",
                    next_attempt_at=now,
                )
                logger.info(
                    "terminal_delivery_intent_superseded_on_record",
                    agent=self.agent_name,
                    existing_state=existing.state,
                    **stale.log_context,
                )
                return stale
            self._state.items[delivery_id] = candidate
            self._enforce_unsettled_bound_locked()
            self._write_locked()
        logger.warning(
            "terminal_delivery_pending_recorded",
            agent=self.agent_name,
            **candidate.log_context,
        )
        return candidate

    def get(self, delivery_id: str) -> PendingTerminalDelivery | None:
        """Return one durable record by identity."""
        with self._state.lock:
            self._ensure_loaded_locked()
            return self._state.items.get(delivery_id)

    def items(self) -> tuple[PendingTerminalDelivery, ...]:
        """Return every durable record currently known."""
        with self._state.lock:
            self._ensure_loaded_locked()
            return tuple(self._state.items.values())

    def unsettled_items(self) -> tuple[PendingTerminalDelivery, ...]:
        """Return every record that still owes a delivery attempt."""
        return tuple(item for item in self.items() if not item.is_settled)

    def pending_target_event_ids(self, room_id: str | None = None) -> frozenset[str]:
        """Return visible event IDs a durable terminal delivery still owns."""
        return frozenset(
            item.target_event_id
            for item in self.items()
            if not item.is_settled
            and item.target_event_id is not None
            and (room_id is None or item.target.room_id == room_id)
        )

    def claim_due(self, *, limit: int) -> tuple[PendingTerminalDelivery, ...]:
        """Lease due records for one attempt round, oldest scheduling first."""
        if limit <= 0:
            return ()
        now = self.clock()
        with self._state.lock:
            self._ensure_loaded_locked()
            self._recover_leaked_attempts_locked(reason="attempt_lease_expired", now=now)
            due = sorted(
                (
                    item
                    for item in self._state.items.values()
                    if item.state in {"pending", "retry_wait"} and item.next_attempt_at <= now
                ),
                key=lambda item: (item.next_attempt_at, item.created_at, item.delivery_id),
            )[:limit]
            if not due:
                return ()
            claimed = tuple(
                replace(
                    item,
                    state="attempting",
                    updated_at=now,
                    last_attempt_at=now,
                    lease_expires_at=now + self.attempt_lease_seconds,
                    runtime_generation=RUNTIME_GENERATION,
                )
                for item in due
            )
            for item in claimed:
                self._state.items[item.delivery_id] = item
            self._write_locked()
            return claimed

    def mark_delivered(self, delivery_id: str, *, reason: str = "delivered") -> None:
        """Settle one record as visibly delivered."""
        self._settle(delivery_id, state="delivered", reason=reason)

    def mark_superseded(self, delivery_id: str, *, reason: str) -> None:
        """Settle one record that a newer valid terminal outcome replaced."""
        self._settle(delivery_id, state="superseded", reason=reason)

    def mark_dead_letter(self, delivery_id: str, *, reason: str) -> None:
        """Settle one record that can never become visible."""
        self._settle(delivery_id, state="dead_letter", reason=reason)
        logger.error(
            "terminal_delivery_dead_letter",
            agent=self.agent_name,
            delivery_reason=reason,
        )

    def defer(self, delivery_id: str, *, reason: str, next_attempt_at: float) -> None:
        """Return one record to the retry queue after a transient failure."""
        now = self.clock()
        with self._state.lock:
            self._ensure_loaded_locked()
            existing = self._state.items.get(delivery_id)
            if existing is None or existing.is_settled:
                return
            self._state.items[delivery_id] = replace(
                existing,
                state="retry_wait",
                attempts=existing.attempts + 1,
                updated_at=now,
                next_attempt_at=next_attempt_at,
                last_error=reason,
                lease_expires_at=None,
            )
            self._write_locked()

    def release(self, delivery_id: str, *, reason: str) -> None:
        """Return one leased record to the queue without counting a failed attempt."""
        now = self.clock()
        with self._state.lock:
            self._ensure_loaded_locked()
            existing = self._state.items.get(delivery_id)
            if existing is None or existing.is_settled:
                return
            self._state.items[delivery_id] = replace(
                existing,
                state="retry_wait",
                updated_at=now,
                next_attempt_at=min(existing.next_attempt_at, now),
                last_error=reason,
                lease_expires_at=None,
            )
            self._write_locked()

    def supersede_sources(self, source_event_ids: Sequence[str], *, reason: str) -> tuple[str, ...]:
        """Settle unsettled records owned by any of the given source events."""
        cancelled_ids = tuple(
            item.delivery_id
            for item in self.items()
            if not item.is_settled and set(item.source_event_ids).intersection(source_event_ids)
        )
        for delivery_id in cancelled_ids:
            self.mark_superseded(delivery_id, reason=reason)
        return cancelled_ids

    def supersede_target_event(self, *, room_id: str, target_event_id: str, reason: str) -> tuple[str, ...]:
        """Settle unsettled records whose visible target event no longer exists."""
        cancelled_ids = tuple(
            item.delivery_id
            for item in self.items()
            if not item.is_settled and item.target.room_id == room_id and item.target_event_id == target_event_id
        )
        for delivery_id in cancelled_ids:
            self.mark_superseded(delivery_id, reason=reason)
        return cancelled_ids

    def backlog(self) -> TerminalDeliveryBacklog:
        """Return a bounded, identifier-light snapshot for logging and metrics."""
        now = self.clock()
        unsettled = self.unsettled_items()
        by_room: dict[str, int] = {}
        by_outcome: dict[str, int] = {}
        for item in unsettled:
            by_room[item.target.room_id] = by_room.get(item.target.room_id, 0) + 1
            by_outcome[item.outcome_kind] = by_outcome.get(item.outcome_kind, 0) + 1
        return TerminalDeliveryBacklog(
            unsettled_count=len(unsettled),
            dead_letter_count=sum(1 for item in self.items() if item.state == "dead_letter"),
            oldest_unsettled_age_seconds=max((now - item.created_at for item in unsettled), default=0.0),
            max_attempts=max((item.attempts for item in unsettled), default=0),
            unsettled_by_room=by_room,
            unsettled_by_outcome=by_outcome,
        )

    def _settle(self, delivery_id: str, *, state: TerminalDeliveryState, reason: str) -> None:
        """Move one record into a terminal state."""
        now = self.clock()
        with self._state.lock:
            self._ensure_loaded_locked()
            existing = self._state.items.get(delivery_id)
            if existing is None or existing.is_settled:
                return
            self._state.items[delivery_id] = replace(
                existing,
                state=state,
                updated_at=now,
                settled_reason=reason,
                lease_expires_at=None,
            )
            self._write_locked()

    def _recover_leaked_attempts_locked(
        self,
        *,
        reason: str,
        now: float | None = None,
        force: bool = False,
    ) -> tuple[PendingTerminalDelivery, ...]:
        """Return crashed or expired leases to the retry queue while the lock is held."""
        current_time = self.clock() if now is None else now
        recovered: list[PendingTerminalDelivery] = []
        for delivery_id, item in list(self._state.items.items()):
            if item.state != "attempting":
                continue
            lease_expires_at = item.lease_expires_at
            lease_live = (
                not force
                and lease_expires_at is not None
                and lease_expires_at > current_time
                and item.runtime_generation == RUNTIME_GENERATION
            )
            if lease_live:
                continue
            repaired = replace(
                item,
                state="retry_wait",
                updated_at=current_time,
                next_attempt_at=current_time,
                last_error=reason,
                lease_expires_at=None,
            )
            self._state.items[delivery_id] = repaired
            recovered.append(repaired)
        if recovered:
            logger.warning(
                "terminal_delivery_leases_recovered",
                agent=self.agent_name,
                recovered_count=len(recovered),
                recovery_reason=reason,
            )
        return tuple(recovered)

    def _enforce_unsettled_bound_locked(self) -> None:
        """Dead-letter the oldest unsettled records once the backlog cap is exceeded."""
        unsettled = sorted(
            (item for item in self._state.items.values() if not item.is_settled),
            key=lambda item: (item.created_at, item.delivery_id),
        )
        overflow = len(unsettled) - self.max_unsettled_items
        if overflow <= 0:
            return
        now = self.clock()
        for item in unsettled[:overflow]:
            self._state.items[item.delivery_id] = replace(
                item,
                state="dead_letter",
                updated_at=now,
                settled_reason="backlog_capacity_exceeded",
                lease_expires_at=None,
            )
        logger.error(
            "terminal_delivery_backlog_capacity_exceeded",
            agent=self.agent_name,
            dropped_count=overflow,
        )

    def _compact_locked(self) -> None:
        """Remove settled records past retention or beyond the settled cap."""
        now = self.clock()
        settled = sorted(
            (item for item in self._state.items.values() if item.is_settled),
            key=lambda item: (item.updated_at, item.delivery_id),
        )
        expired = {item.delivery_id for item in settled if now - item.updated_at >= self.settled_retention_seconds}
        retained = [item for item in settled if item.delivery_id not in expired]
        overflow = len(retained) - self.max_settled_items
        if overflow > 0:
            expired.update(item.delivery_id for item in retained[:overflow])
        for delivery_id in expired:
            self._state.items.pop(delivery_id, None)

    def _ensure_loaded_locked(self, *, force: bool = False) -> None:
        """Load durable records into shared memory while the state lock is held."""
        if self._state.loaded and not force:
            return
        self.base_path.mkdir(parents=True, exist_ok=True)
        with advisory_file_lock(self._lock_file, exclusive=True):
            self._state.items = self._read_locked()
        self._state.loaded = True

    def _read_locked(self) -> dict[str, PendingTerminalDelivery]:
        """Read current-version records while the file lock is held."""
        if not self._store_file.exists():
            return {}
        try:
            with self._store_file.open(encoding="utf-8") as store_file:
                data = json.load(store_file)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            self._quarantine_locked("malformed")
            return {}
        if not isinstance(data, dict) or data.get(_SCHEMA_VERSION_KEY) != TERMINAL_DELIVERY_SCHEMA_VERSION:
            self._quarantine_locked("unsupported-schema")
            return {}
        raw_items = data.get(_ITEMS_KEY)
        if not isinstance(raw_items, dict):
            self._quarantine_locked("structurally invalid items")
            return {}
        items: dict[str, PendingTerminalDelivery] = {}
        invalid_count = 0
        for delivery_id, raw_item in raw_items.items():
            item = PendingTerminalDelivery.from_record(raw_item) if isinstance(delivery_id, str) else None
            if item is None or item.delivery_id != delivery_id:
                invalid_count += 1
                continue
            items[delivery_id] = item
        if invalid_count:
            logger.warning(
                "terminal_delivery_records_quarantined",
                agent=self.agent_name,
                invalid_count=invalid_count,
                retained_count=len(items),
            )
        return items

    def _write_locked(self) -> None:
        """Durably replace the store file while the state lock is held."""
        payload = {
            _SCHEMA_VERSION_KEY: TERMINAL_DELIVERY_SCHEMA_VERSION,
            _ITEMS_KEY: {delivery_id: item.to_record() for delivery_id, item in self._state.items.items()},
        }
        with advisory_file_lock(self._lock_file, exclusive=True):
            write_json_file_durable(self._store_file, payload, temp_dir=self.base_path, indent=2)

    def _quarantine_locked(self, reason: str) -> None:
        """Move an unreadable store file aside so the worker keeps running."""
        quarantined_file = self.base_path / f"{self._store_file.name}.corrupt-{time.time_ns()}"
        try:
            self._store_file.replace(quarantined_file)
        except OSError:
            quarantined_file = self._store_file
        logger.warning(
            "terminal_delivery_store_quarantined",
            agent=self.agent_name,
            quarantine_reason=reason,
            quarantined_file=str(quarantined_file),
        )


def _supersedes(candidate: PendingTerminalDelivery, existing: PendingTerminalDelivery) -> bool:
    """Return whether a new intent may replace an existing record for the same target.

    Ordering never uses wall-clock time. A different response correlation is by
    construction a later response turn for the same visible target - a legitimate
    regeneration - and always wins. Within one response turn, only a strictly
    stronger outcome may replace what is pending or already delivered, so a
    delayed fallback or error can never overwrite a successful final response.
    """
    if (
        candidate.correlation_id is not None
        and existing.correlation_id is not None
        and candidate.correlation_id != existing.correlation_id
    ):
        return True
    return candidate.outcome_precedence > existing.outcome_precedence


class _Unserializable:
    """Sentinel marking content that cannot be persisted."""


_UNSERIALIZABLE = _Unserializable()


def _json_safe_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any] | None | _Unserializable:
    """Return a JSON round-trippable copy, or the sentinel when content is not persistable."""
    if mapping is None:
        return None
    try:
        encoded = json.dumps(dict(mapping))
    except (TypeError, ValueError):
        return _UNSERIALIZABLE
    decoded = json.loads(encoded)
    return decoded if isinstance(decoded, dict) else _UNSERIALIZABLE


def _tool_trace_entry_to_record(entry: ToolTraceEntry) -> dict[str, Any]:
    """Return the JSON-safe representation of one tool trace entry."""
    return {
        "type": entry.type,
        "tool_name": entry.tool_name,
        "args_preview": entry.args_preview,
        "result_preview": entry.result_preview,
        "truncated": entry.truncated,
    }


def _string_keyed_dict(value: object) -> dict[str, Any] | None:
    """Return a string-keyed copy of one persisted mapping, or None."""
    if not isinstance(value, Mapping):
        return None
    mapping = typing.cast("Mapping[str, Any]", value)
    return {key: item for key, item in mapping.items() if isinstance(key, str)}


def _tool_trace_from_record(raw_trace: object) -> tuple[ToolTraceEntry, ...]:
    """Parse persisted tool trace entries, dropping anything malformed."""
    if not isinstance(raw_trace, list):
        return ()
    entries: list[ToolTraceEntry] = []
    for raw_value in raw_trace:
        raw_entry = _string_keyed_dict(raw_value)
        if raw_entry is None:
            continue
        entry_type = raw_entry.get("type")
        tool_name = raw_entry.get("tool_name")
        if entry_type not in {"tool_call_started", "tool_call_completed"} or not isinstance(tool_name, str):
            continue
        entries.append(
            ToolTraceEntry(
                type=typing.cast("Literal['tool_call_started', 'tool_call_completed']", entry_type),
                tool_name=tool_name,
                args_preview=_optional_string(raw_entry.get("args_preview")),
                result_preview=_optional_string(raw_entry.get("result_preview")),
                truncated=raw_entry.get("truncated") is True,
            ),
        )
    return tuple(entries)


def _store_file_path(base_path: Path, agent_name: str) -> Path:
    """Return the lexically validated store path for one entity."""
    if not agent_name or ".." in agent_name or "/" in agent_name or "\\" in agent_name:
        message = f"Invalid terminal delivery store agent name: {agent_name!r}"
        raise ValueError(message)
    return base_path / f"{agent_name}_pending_terminal_deliveries.json"


def _required_string(value: object) -> str | None:
    """Return a non-empty string or None."""
    return value if isinstance(value, str) and value else None


def _optional_string(value: object) -> str | None:
    """Return a non-empty string or None."""
    return value if isinstance(value, str) and value else None


def _string_tuple(value: object) -> tuple[str, ...]:
    """Return a deduplicated tuple of non-empty strings."""
    if not isinstance(value, list):
        return ()
    seen: set[str] = set()
    ordered: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return tuple(ordered)


def _float_or(value: object, default: float) -> float:
    """Return a float value or the provided default."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return default


def _optional_float(value: object) -> float | None:
    """Return a float value or None."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


__all__ = [
    "DEFAULT_ATTEMPT_LEASE_SECONDS",
    "RUNTIME_GENERATION",
    "TERMINAL_DELIVERY_SCHEMA_VERSION",
    "PendingTerminalDelivery",
    "TerminalDeliveryAttempt",
    "TerminalDeliveryAttemptResult",
    "TerminalDeliveryBacklog",
    "TerminalDeliveryIntent",
    "TerminalDeliveryKind",
    "TerminalDeliveryState",
    "TerminalDeliveryStore",
    "TerminalOutcomeKind",
    "terminal_delivery_id",
]
