"""Durable pending terminal Matrix deliveries for one runtime entity.

MindRoom commits a terminal response outcome before Matrix necessarily accepts
the edit that makes it visible. When the transport is temporarily unavailable -
limited-sync timeline recovery, rate limiting, a network blip - the immediate
bounded retry in ``mindroom.matrix.client_delivery`` can still be exhausted.
This module records that committed-but-undelivered final body durably so a later
attempt can converge instead of leaving a permanently stuck partial stream.

State machine (see ``docs/architecture/durable-terminal-delivery.md``)::

    record ---> pending ---> attempting ---> delivered
                  ^             |  |  |
                  |             |  +------> superseded    (newer turn, redacted target)
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
from mindroom.terminal_delivery_lifecycle import TerminalDeliveryLifecycleFacts
from mindroom.tool_system.events import ToolTraceEntry

if typing.TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

TERMINAL_DELIVERY_SCHEMA_VERSION = 4
_SCHEMA_VERSION_KEY = "schema_version"
_ITEMS_KEY = "items"

TerminalDeliveryState = Literal["pending", "attempting", "retry_wait"]
TerminalDeliveryAttemptResult = Literal["delivered", "transient", "superseded"]

# One process owns semantic ordering for an entity, so a lease only has to
# outlive a single attempt; anything longer is a crash and must be recovered.
DEFAULT_ATTEMPT_LEASE_SECONDS = 120.0
# Identifies the current process for lease ownership across restarts.
RUNTIME_GENERATION = uuid4().hex


@dataclass(frozen=True, slots=True)
class TerminalDeliveryAttempt:
    """Outcome of one durable terminal delivery attempt."""

    result: TerminalDeliveryAttemptResult
    reason: str

    @classmethod
    def delivered_now(cls, reason: str = "delivered") -> TerminalDeliveryAttempt:
        """Return the successful attempt outcome."""
        return cls(result="delivered", reason=reason)

    @classmethod
    def transient(cls, reason: str) -> TerminalDeliveryAttempt:
        """Return a retryable attempt outcome."""
        return cls(result="transient", reason=reason)

    @classmethod
    def superseded(cls, reason: str) -> TerminalDeliveryAttempt:
        """Return an attempt outcome invalidated by newer valid state."""
        return cls(result="superseded", reason=reason)


@dataclass(frozen=True, slots=True)
class TerminalDeliveryIntent:
    """One committed final response body that still needs to become visible."""

    agent_name: str
    target: MessageTarget
    target_event_id: str
    anchor_event_id: str
    source_event_ids: tuple[str, ...]
    lifecycle: TerminalDeliveryLifecycleFacts
    body: str
    wire_content: Mapping[str, Any]
    correlation_id: str | None = None
    tool_trace: tuple[ToolTraceEntry, ...] = ()
    extra_content: Mapping[str, Any] | None = None

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
    target_event_id: str,
    anchor_event_id: str,
) -> str:
    """Return the deterministic identity for one terminal delivery target."""
    digest_source = f"{agent_name}\x1f{room_id}\x1f{target_event_id}\x1f{anchor_event_id}"
    return hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class PendingTerminalDelivery:
    """Durable, restart-safe record of one pending terminal delivery."""

    delivery_id: str
    agent_name: str
    target: MessageTarget
    target_event_id: str
    anchor_event_id: str
    source_event_ids: tuple[str, ...]
    lifecycle: TerminalDeliveryLifecycleFacts
    revision: int
    body: str
    wire_content: Mapping[str, Any]
    transaction_id: str
    correlation_id: str | None
    tool_trace: tuple[ToolTraceEntry, ...]
    extra_content: Mapping[str, Any] | None
    runtime_generation: str
    state: TerminalDeliveryState
    attempts: int
    created_at: float
    updated_at: float
    next_attempt_at: float
    last_error: str | None = None
    lease_expires_at: float | None = None

    @property
    def log_context(self) -> dict[str, object]:
        """Return identifier-free structured log fields for this record."""
        return {"revision": self.revision, "state": self.state, "attempts": self.attempts}

    def to_record(self) -> dict[str, Any]:
        """Return the JSON-safe persisted representation of this record."""
        return {
            "delivery_id": self.delivery_id,
            "agent_name": self.agent_name,
            "target": dict(self.target.to_metadata()),
            "target_event_id": self.target_event_id,
            "anchor_event_id": self.anchor_event_id,
            "source_event_ids": list(self.source_event_ids),
            "lifecycle": self.lifecycle.to_record(),
            "revision": self.revision,
            "body": self.body,
            "wire_content": dict(self.wire_content),
            "transaction_id": self.transaction_id,
            "correlation_id": self.correlation_id,
            "tool_trace": [
                {
                    "type": entry.type,
                    "tool_name": entry.tool_name,
                    "args_preview": entry.args_preview,
                    "result_preview": entry.result_preview,
                    "truncated": entry.truncated,
                }
                for entry in self.tool_trace
            ],
            "extra_content": dict(self.extra_content) if self.extra_content is not None else None,
            "runtime_generation": self.runtime_generation,
            "state": self.state,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_attempt_at": self.next_attempt_at,
            "last_error": self.last_error,
            "lease_expires_at": self.lease_expires_at,
        }

    @classmethod
    def from_record(cls, raw_record: object) -> PendingTerminalDelivery | None:
        """Parse one persisted record, rejecting anything not matching the current schema."""
        record = _string_keyed_dict(raw_record)
        if record is None:
            return None
        delivery_id = _optional_string(record.get("delivery_id"))
        agent_name = _optional_string(record.get("agent_name"))
        target_event_id = _optional_string(record.get("target_event_id"))
        anchor_event_id = _optional_string(record.get("anchor_event_id"))
        target = MessageTarget.from_metadata(record.get("target"))
        lifecycle = TerminalDeliveryLifecycleFacts.from_record(record.get("lifecycle"))
        state = record.get("state")
        revision = record.get("revision")
        body = record.get("body")
        wire_content = _string_keyed_dict(record.get("wire_content"))
        transaction_id = _optional_string(record.get("transaction_id"))
        attempts = record.get("attempts")
        if (
            delivery_id is None
            or agent_name is None
            or target_event_id is None
            or anchor_event_id is None
            or target is None
            or lifecycle is None
            or state not in typing.get_args(TerminalDeliveryState)
            or not _is_int(revision)
            or not isinstance(body, str)
            or wire_content is None
            or transaction_id is None
            or not _is_int(attempts)
        ):
            return None
        return cls(
            delivery_id=delivery_id,
            agent_name=agent_name,
            target=target,
            target_event_id=target_event_id,
            anchor_event_id=anchor_event_id,
            source_event_ids=_string_tuple(record.get("source_event_ids")),
            lifecycle=lifecycle,
            revision=typing.cast("int", revision),
            body=body,
            wire_content=wire_content,
            transaction_id=transaction_id,
            correlation_id=_optional_string(record.get("correlation_id")),
            tool_trace=_tool_trace_from_record(record.get("tool_trace")),
            extra_content=_string_keyed_dict(record.get("extra_content")),
            runtime_generation=_optional_string(record.get("runtime_generation")) or "",
            state=typing.cast("TerminalDeliveryState", state),
            attempts=max(0, typing.cast("int", attempts)),
            created_at=_float_or(record.get("created_at"), 0.0),
            updated_at=_float_or(record.get("updated_at"), 0.0),
            next_attempt_at=_float_or(record.get("next_attempt_at"), 0.0),
            last_error=_optional_string(record.get("last_error")),
            lease_expires_at=_optional_float(record.get("lease_expires_at")),
        )


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
    with _STORE_RUNTIME_LOCK:
        return _STORE_STATES.setdefault(str(store_file.absolute()), _StoreState())


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
            self._write_locked()
            return recovered

    def record(self, intent: TerminalDeliveryIntent) -> PendingTerminalDelivery | None:
        """Persist one committed terminal intent, applying precedence against existing state.

        Returns ``None`` when the intent is not durably representable, or when it
        is stale against what is already recorded for the same visible target.
        """
        extra_content = _json_safe_mapping(intent.extra_content)
        wire_content = _json_safe_mapping(intent.wire_content)
        if extra_content is _UNSERIALIZABLE or wire_content is _UNSERIALIZABLE or wire_content is None:
            logger.error("terminal_delivery_intent_not_serializable", agent=self.agent_name)
            return None
        now = self.clock()
        with self._state.lock:
            self._ensure_loaded_locked()
            delivery_id = intent.delivery_id
            existing = self._state.items.get(delivery_id)
            if existing is not None and not _supersedes(intent, existing):
                logger.info(
                    "terminal_delivery_intent_superseded_on_record",
                    agent=self.agent_name,
                    existing_state=existing.state,
                )
                return None
            revision = 0 if existing is None else existing.revision + 1
            candidate = PendingTerminalDelivery(
                delivery_id=delivery_id,
                agent_name=intent.agent_name,
                target=intent.target,
                target_event_id=intent.target_event_id,
                anchor_event_id=intent.anchor_event_id,
                source_event_ids=tuple(intent.source_event_ids),
                lifecycle=intent.lifecycle,
                revision=revision,
                body=intent.body,
                wire_content=typing.cast("Mapping[str, Any]", wire_content),
                transaction_id=f"mindroom-td-{delivery_id}-{revision}",
                correlation_id=intent.correlation_id,
                tool_trace=tuple(intent.tool_trace),
                extra_content=typing.cast("Mapping[str, Any] | None", extra_content),
                runtime_generation=RUNTIME_GENERATION,
                state="pending",
                attempts=0,
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
                next_attempt_at=now,
            )
            self._state.items[delivery_id] = candidate
            self._write_locked()
        logger.warning("terminal_delivery_pending_recorded", agent=self.agent_name, **candidate.log_context)
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
        return self.items()

    def pending_target_event_ids(self, room_id: str | None = None) -> frozenset[str]:
        """Return visible event IDs a durable terminal delivery still owns."""
        return frozenset(
            item.target_event_id for item in self.items() if room_id is None or item.target.room_id == room_id
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
            claimed = tuple(
                replace(
                    item,
                    state="attempting",
                    updated_at=now,
                    lease_expires_at=now + self.attempt_lease_seconds,
                    runtime_generation=RUNTIME_GENERATION,
                )
                for item in due
            )
            if not claimed:
                return ()
            for item in claimed:
                self._state.items[item.delivery_id] = item
            self._write_locked()
            return claimed

    def mark_delivered(self, delivery_id: str, *, revision: int, reason: str = "delivered") -> None:
        """Drop one record whose outcome is now visible in Matrix."""
        self._discard(delivery_id, revision=revision, reason=reason)

    def mark_superseded(self, delivery_id: str, *, revision: int, reason: str) -> None:
        """Drop one record that newer valid state replaced."""
        self._discard(delivery_id, revision=revision, reason=reason)

    def defer(self, delivery_id: str, *, revision: int, reason: str, next_attempt_at: float) -> None:
        """Return one record to the retry queue after a transient failure."""
        self._requeue(
            delivery_id,
            revision=revision,
            reason=reason,
            next_attempt_at=next_attempt_at,
            count_attempt=True,
        )

    def release(self, delivery_id: str, *, revision: int, reason: str) -> None:
        """Return one leased record to the queue without counting a failed attempt."""
        self._requeue(delivery_id, revision=revision, reason=reason, next_attempt_at=None, count_attempt=False)

    def supersede_sources(self, source_event_ids: Sequence[str], *, reason: str) -> tuple[str, ...]:
        """Settle unsettled records owned by any of the given source events."""
        return self._supersede_matching(
            lambda item: bool(set(item.source_event_ids).intersection(source_event_ids)),
            reason=reason,
        )

    def supersede_target_event(self, *, room_id: str, target_event_id: str, reason: str) -> tuple[str, ...]:
        """Settle unsettled records whose visible target event no longer exists."""
        return self._supersede_matching(
            lambda item: item.target.room_id == room_id and item.target_event_id == target_event_id,
            reason=reason,
        )

    def _supersede_matching(
        self,
        matches: typing.Callable[[PendingTerminalDelivery], bool],
        *,
        reason: str,
    ) -> tuple[str, ...]:
        """Settle every unsettled record matching one predicate."""
        with self._state.lock:
            self._ensure_loaded_locked()
            matched_ids = tuple(delivery_id for delivery_id, item in list(self._state.items.items()) if matches(item))
            if not matched_ids:
                return ()
            for delivery_id in matched_ids:
                del self._state.items[delivery_id]
            self._write_locked()
        logger.debug(
            "terminal_delivery_matches_superseded",
            agent=self.agent_name,
            delivery_reason=reason,
            superseded_count=len(matched_ids),
        )
        return matched_ids

    def _discard(self, delivery_id: str, *, revision: int, reason: str) -> None:
        """Remove one finished record so the outbox only holds outstanding work."""
        with self._state.lock:
            self._ensure_loaded_locked()
            existing = self._state.items.get(delivery_id)
            if not self._owns_outcome(existing, revision=revision, transition="discarded"):
                return
            del self._state.items[delivery_id]
            self._write_locked()
        logger.debug("terminal_delivery_discarded", agent=self.agent_name, delivery_reason=reason)

    def _requeue(
        self,
        delivery_id: str,
        *,
        revision: int,
        reason: str,
        next_attempt_at: float | None,
        count_attempt: bool,
    ) -> None:
        """Return one record to the retry queue, ignoring outcomes of stale revisions."""
        now = self.clock()
        with self._state.lock:
            self._ensure_loaded_locked()
            existing = self._state.items.get(delivery_id)
            if not self._owns_outcome(existing, revision=revision, transition="retry_wait"):
                return
            assert existing is not None
            self._state.items[delivery_id] = replace(
                existing,
                state="retry_wait",
                attempts=existing.attempts + 1 if count_attempt else existing.attempts,
                updated_at=now,
                next_attempt_at=(
                    next_attempt_at if next_attempt_at is not None else min(existing.next_attempt_at, now)
                ),
                last_error=reason,
                lease_expires_at=None,
            )
            self._write_locked()

    def _owns_outcome(
        self,
        existing: PendingTerminalDelivery | None,
        *,
        revision: int,
        transition: str,
    ) -> bool:
        """Return whether one attempt outcome still applies to the current record.

        A newer response turn replaces the row in place and bumps its revision
        while an older attempt may still be in flight. Applying that older
        outcome would settle content the regenerated turn never transmitted, so
        every transition is scoped to the revision it was leased against.
        """
        if existing is None:
            return False
        if existing.revision == revision:
            return True
        logger.info(
            "terminal_delivery_stale_revision_outcome_ignored",
            agent=self.agent_name,
            attempt_revision=revision,
            current_revision=existing.revision,
            transition=transition,
        )
        return False

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
            lease_live = (
                not force
                and item.lease_expires_at is not None
                and item.lease_expires_at > current_time
                and item.runtime_generation == RUNTIME_GENERATION
            )
            if item.state != "attempting" or lease_live:
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


def _supersedes(intent: TerminalDeliveryIntent, existing: PendingTerminalDelivery) -> bool:
    """Return whether a new intent may replace an existing record for the same target.

    Ordering never uses wall-clock time. A different response correlation is by
    construction a later response turn for the same visible target - a legitimate
    regeneration - and always wins. Re-recording the same turn is a no-op, which
    keeps repeated recording idempotent and preserves the existing retry budget.
    """
    return (
        intent.correlation_id is not None
        and existing.correlation_id is not None
        and intent.correlation_id != existing.correlation_id
    )


class _Unserializable:
    """Sentinel marking content that cannot be persisted."""


_UNSERIALIZABLE = _Unserializable()


def _json_safe_mapping(mapping: Mapping[str, Any] | None) -> dict[str, Any] | None | _Unserializable:
    """Return a JSON round-trippable copy, or the sentinel when content is not persistable."""
    if mapping is None:
        return None
    try:
        decoded = json.loads(json.dumps(dict(mapping)))
    except (TypeError, ValueError):
        return _UNSERIALIZABLE
    return decoded if isinstance(decoded, dict) else _UNSERIALIZABLE


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


def _string_keyed_dict(value: object) -> dict[str, Any] | None:
    """Return a string-keyed copy of one persisted mapping, or None."""
    if not isinstance(value, Mapping):
        return None
    mapping = typing.cast("Mapping[str, Any]", value)
    return {key: item for key, item in mapping.items() if isinstance(key, str)}


def _optional_string(value: object) -> str | None:
    """Return a non-empty string or None."""
    return value if isinstance(value, str) and value else None


def _string_tuple(value: object) -> tuple[str, ...]:
    """Return a deduplicated tuple of non-empty strings."""
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(item for item in value if isinstance(item, str) and item))


def _is_int(value: object) -> bool:
    """Return whether one persisted value is a real integer."""
    return isinstance(value, int) and not isinstance(value, bool)


def _float_or(value: object, default: float) -> float:
    """Return a float value or the provided default."""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else default


def _optional_float(value: object) -> float | None:
    """Return a float value or None."""
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


__all__ = [
    "DEFAULT_ATTEMPT_LEASE_SECONDS",
    "RUNTIME_GENERATION",
    "TERMINAL_DELIVERY_SCHEMA_VERSION",
    "PendingTerminalDelivery",
    "TerminalDeliveryAttempt",
    "TerminalDeliveryAttemptResult",
    "TerminalDeliveryIntent",
    "TerminalDeliveryState",
    "TerminalDeliveryStore",
    "terminal_delivery_id",
]
