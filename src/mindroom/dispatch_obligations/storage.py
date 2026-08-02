"""SQLite storage and durable state for exact callback obligations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

logger = get_logger(__name__)

_SCHEMA_VERSION = 1
_PENDING_STATE = "pending"
_DEFERRED_STATE = "deferred"
_UNSETTLED_STATES = (_PENDING_STATE, _DEFERRED_STATE)
_SQLITE_BUSY_TIMEOUT_MILLISECONDS = 5_000


class DispatchCallbackKind(StrEnum):
    """Exact correctness-critical callback purposes."""

    MESSAGE = "message"
    MEDIA = "media"
    REACTION = "reaction"
    APPROVAL = "approval"
    INVITE = "invite"
    ROOM_LIFECYCLE = "room_lifecycle"
    REDACTION = "redaction"
    DECRYPTION_FAILURE = "decryption_failure"


class DispatchSemanticConsumer(StrEnum):
    """Stable application consumer chosen for one multi-purpose callback."""

    APPROVAL_REPLY = "approval_reply"
    CONFIG_CONFIRMATION = "config_confirmation"
    TOOL_APPROVAL_REACTION = "tool_approval_reaction"
    STOP_REACTION = "stop_reaction"
    INTERACTIVE_REACTION = "interactive_reaction"
    REACTION_HOOKS = "reaction_hooks"

    @property
    def callback_kind(self) -> DispatchCallbackKind:
        """Return the only raw callback kind allowed to claim this consumer."""
        if self is DispatchSemanticConsumer.APPROVAL_REPLY:
            return DispatchCallbackKind.MESSAGE
        return DispatchCallbackKind.REACTION


class DispatchTerminalOutcome(StrEnum):
    """Explicit terminal outcomes for one exact callback obligation."""

    SUCCEEDED = "succeeded"
    INTENTIONALLY_IGNORED = "intentionally_ignored"


class DispatchCreateResult(StrEnum):
    """Result of durably creating one pending obligation."""

    CREATED = "created"
    ALREADY_PENDING = "already_pending"
    ALREADY_TERMINAL = "already_terminal"


class DispatchObligationCorruptionError(RuntimeError):
    """A pending row cannot be recovered without inventing source input."""


def _database_name(principal_id: str, entity_name: str) -> str:
    if ".." in entity_name or "/" in entity_name or "\\" in entity_name:
        msg = f"Invalid dispatch-obligation entity name: {entity_name!r}"
        raise ValueError(msg)
    principal_digest = hashlib.sha256(principal_id.encode()).hexdigest()[:12]
    return f"dispatch_obligations-{entity_name}-{principal_digest}.sqlite3"


@dataclass(frozen=True, slots=True)
class DispatchObligationKey:
    """Exact durable callback identity."""

    principal_id: str
    entity_name: str
    source_event_id: str
    callback_kind: DispatchCallbackKind


@dataclass(frozen=True, slots=True)
class DispatchObligation:
    """Replayable input for one exact Matrix callback."""

    principal_id: str
    entity_name: str
    source_event_id: str
    callback_kind: DispatchCallbackKind
    room_id: str
    event_source: Mapping[str, object]
    semantic_consumer: DispatchSemanticConsumer | None = None
    callback_completed: bool = False
    requires_pending_check: bool = field(default=False, compare=False, repr=False)

    @property
    def key(self) -> DispatchObligationKey:
        """Return the exact durable identity."""
        return DispatchObligationKey(
            principal_id=self.principal_id,
            entity_name=self.entity_name,
            source_event_id=self.source_event_id,
            callback_kind=self.callback_kind,
        )


@dataclass(frozen=True, slots=True)
class _StoredRow:
    room_id: str
    event_source_json: str
    state: str


@dataclass
class DispatchObligationStore:
    """Persist callbacks independently of Matrix sync transport positions."""

    tracking_path: Path
    principal_id: str
    entity_name: str

    def __post_init__(self) -> None:
        """Validate the bound identity and initialize the leaf database."""
        if not self.principal_id or not self.entity_name:
            msg = "Dispatch obligation store requires an exact principal and entity"
            raise ValueError(msg)
        self.tracking_path = Path(self.tracking_path)
        self.tracking_path.mkdir(parents=True, exist_ok=True)
        self._database_path = self.tracking_path / _database_name(
            self.principal_id,
            self.entity_name,
        )
        self._lock = threading.Lock()
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("BEGIN IMMEDIATE")
            self._initialize_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version not in {0, _SCHEMA_VERSION}:
            msg = f"Unsupported dispatch obligation schema version {current_version}"
            raise RuntimeError(msg)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dispatch_obligations (
                principal_id TEXT NOT NULL,
                entity_name TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                callback_kind TEXT NOT NULL,
                room_id TEXT NOT NULL,
                event_source_json TEXT NOT NULL,
                semantic_consumer TEXT,
                state TEXT NOT NULL CHECK (
                    state IN ('pending', 'deferred', 'succeeded', 'intentionally_ignored')
                ),
                created_at_ns INTEGER NOT NULL,
                settled_at_ns INTEGER,
                PRIMARY KEY (
                    principal_id,
                    entity_name,
                    source_event_id,
                    callback_kind
                )
            )
            """,
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS dispatch_obligations_pending_recovery
            ON dispatch_obligations (
                principal_id,
                entity_name,
                created_at_ns
            )
            WHERE state IN ('pending', 'deferred')
            """,
        )
        if current_version == 0:
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @staticmethod
    def _event_source_json(obligation: DispatchObligation) -> str:
        try:
            event_source_json = json.dumps(
                obligation.event_source,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            msg = "Dispatch obligation event source must be JSON-safe"
            raise ValueError(msg) from exc
        expected_source_event_id = (
            invite_source_event_id(obligation.room_id, event_source_json)
            if obligation.callback_kind is DispatchCallbackKind.INVITE
            else obligation.event_source.get("event_id")
        )
        if expected_source_event_id != obligation.source_event_id:
            msg = "Dispatch obligation source event ID does not match its event payload"
            raise ValueError(msg)
        return event_source_json

    def validate_bound_key(self, key: DispatchObligationKey) -> None:
        """Reject a key bound to another principal or entity."""
        if key.principal_id != self.principal_id or key.entity_name != self.entity_name:
            msg = "Dispatch obligation identity does not match the bound principal and entity"
            raise ValueError(msg)

    @staticmethod
    def _stored_row(row: sqlite3.Row | None) -> _StoredRow | None:
        if row is None:
            return None
        return _StoredRow(
            room_id=row["room_id"],
            event_source_json=row["event_source_json"],
            state=row["state"],
        )

    def _pending_obligation_from_row(self, row: sqlite3.Row) -> DispatchObligation:
        """Decode one exact pending row without inventing source input."""
        try:
            callback_kind = DispatchCallbackKind(row["callback_kind"])
            event_source = json.loads(row["event_source_json"])
            semantic_consumer = (
                DispatchSemanticConsumer(row["semantic_consumer"]) if row["semantic_consumer"] is not None else None
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            msg = f"corrupt dispatch obligation {row['source_event_id']!r}/{row['callback_kind']!r}"
            raise DispatchObligationCorruptionError(msg) from exc
        if semantic_consumer is not None and semantic_consumer.callback_kind is not callback_kind:
            msg = f"corrupt dispatch obligation {row['source_event_id']!r}/{row['callback_kind']!r}"
            raise DispatchObligationCorruptionError(msg)
        if not isinstance(event_source, dict):
            msg = f"corrupt dispatch obligation {row['source_event_id']!r}/{row['callback_kind']!r}"
            raise DispatchObligationCorruptionError(msg)
        return DispatchObligation(
            principal_id=self.principal_id,
            entity_name=self.entity_name,
            source_event_id=row["source_event_id"],
            callback_kind=callback_kind,
            room_id=row["room_id"],
            event_source=event_source,
            semantic_consumer=semantic_consumer,
            callback_completed=row["state"] == _DEFERRED_STATE,
            requires_pending_check=True,
        )

    def create_pending(self, obligation: DispatchObligation) -> DispatchCreateResult:
        """Durably create pending work before its callback can run."""
        self.validate_bound_key(obligation.key)
        if not obligation.source_event_id:
            msg = "Dispatch obligation requires a source event"
            raise ValueError(msg)
        key = obligation.key
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._stored_row(
                connection.execute(
                    """
                    SELECT room_id, event_source_json, state
                    FROM dispatch_obligations
                    WHERE principal_id = ?
                      AND entity_name = ?
                      AND source_event_id = ?
                      AND callback_kind = ?
                    """,
                    (
                        key.principal_id,
                        key.entity_name,
                        key.source_event_id,
                        key.callback_kind.value,
                    ),
                ).fetchone(),
            )
            if existing is not None and existing.state not in _UNSETTLED_STATES:
                return DispatchCreateResult.ALREADY_TERMINAL
            if not obligation.room_id:
                msg = "Dispatch obligation requires a room"
                raise ValueError(msg)
            event_source_json = self._event_source_json(obligation)
            if existing is not None:
                if existing.room_id != obligation.room_id or existing.event_source_json != event_source_json:
                    logger.warning(
                        "dispatch_obligation_replay_payload_differs",
                        source_event_id=key.source_event_id,
                        callback_kind=key.callback_kind.value,
                    )
                return DispatchCreateResult.ALREADY_PENDING
            connection.execute(
                """
                INSERT INTO dispatch_obligations (
                    principal_id,
                    entity_name,
                    source_event_id,
                    callback_kind,
                    room_id,
                    event_source_json,
                    state,
                    created_at_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    obligation.room_id,
                    event_source_json,
                    _PENDING_STATE,
                    time.time_ns(),
                ),
            )
        return DispatchCreateResult.CREATED

    def settle(
        self,
        key: DispatchObligationKey,
        outcome: DispatchTerminalOutcome,
    ) -> None:
        """Durably settle one exact pending callback."""
        self.validate_bound_key(key)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE dispatch_obligations
                SET room_id = '',
                    event_source_json = '',
                    semantic_consumer = NULL,
                    state = ?,
                    settled_at_ns = ?
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state IN (?, ?)
                """,
                (
                    outcome.value,
                    time.time_ns(),
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    _PENDING_STATE,
                    _DEFERRED_STATE,
                ),
            )

    def discard_pending(self, key: DispatchObligationKey) -> None:
        """Remove successful work whose source has no permanent Matrix event ID."""
        self.validate_bound_key(key)
        if key.callback_kind is not DispatchCallbackKind.INVITE:
            msg = "Only successful invite obligations may be deleted"
            raise ValueError(msg)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state IN (?, ?)
                """,
                (
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    _PENDING_STATE,
                    _DEFERRED_STATE,
                ),
            )

    def mark_callback_pending(self, key: DispatchObligationKey) -> bool:
        """Return deferred turn work to callback ownership before retrying it."""
        self.validate_bound_key(key)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE dispatch_obligations
                SET state = ?
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state = ?
                """,
                (
                    _PENDING_STATE,
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    _DEFERRED_STATE,
                ),
            )
        return cursor.rowcount == 1

    def claim_semantic_consumer(
        self,
        key: DispatchObligationKey,
        consumer: DispatchSemanticConsumer,
    ) -> DispatchSemanticConsumer:
        """Persist the sole application consumer before it performs side effects."""
        self.validate_bound_key(key)
        if consumer.callback_kind is not key.callback_kind:
            msg = f"{consumer.value!r} cannot consume a {key.callback_kind.value!r} callback"
            raise ValueError(msg)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                UPDATE dispatch_obligations
                SET semantic_consumer = COALESCE(semantic_consumer, ?)
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state IN (?, ?)
                RETURNING semantic_consumer
                """,
                (
                    consumer.value,
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    _PENDING_STATE,
                    _DEFERRED_STATE,
                ),
            ).fetchone()
        if row is None:
            msg = "Cannot claim a semantic consumer for terminal or missing work"
            raise RuntimeError(msg)
        return DispatchSemanticConsumer(row["semantic_consumer"])

    def receipt_order(self, key: DispatchObligationKey) -> int:
        """Return the stable SQLite admission order for one exact callback."""
        self.validate_bound_key(key)
        with self._lock, self._connection() as connection:
            # SQLite may reuse a deleted maximum rowid, so MESSAGE and REACTION
            # rows remain permanent and `discard_pending` enforces INVITE-only deletion.
            row = connection.execute(
                """
                SELECT rowid
                FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                """,
                (
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                ),
            ).fetchone()
        if row is None:
            msg = "Running dispatch callback lost its durable receipt order"
            raise RuntimeError(msg)
        return int(row["rowid"])

    def mark_callback_deferred(self, key: DispatchObligationKey) -> None:
        """Record that the callback completed and downstream turn work owns the source."""
        self.validate_bound_key(key)
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE dispatch_obligations
                SET state = ?
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state = ?
                """,
                (
                    _DEFERRED_STATE,
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    _PENDING_STATE,
                ),
            )

    def settle_from_turn_store(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> None:
        """Create a compact permanent tombstone from exact TurnStore truth."""
        if callback_kind not in {DispatchCallbackKind.MESSAGE, DispatchCallbackKind.MEDIA}:
            msg = "TurnStore can settle only a message or media dispatch obligation"
            raise ValueError(msg)
        if not source_event_id:
            msg = "TurnStore dispatch settlement requires a source event"
            raise ValueError(msg)
        settled_at_ns = time.time_ns()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO dispatch_obligations (
                    principal_id,
                    entity_name,
                    source_event_id,
                    callback_kind,
                    room_id,
                    event_source_json,
                    state,
                    created_at_ns,
                    settled_at_ns
                ) VALUES (?, ?, ?, ?, '', '', ?, ?, ?)
                ON CONFLICT (
                    principal_id,
                    entity_name,
                    source_event_id,
                    callback_kind
                ) DO UPDATE SET
                    room_id = '',
                    event_source_json = '',
                    semantic_consumer = NULL,
                    state = excluded.state,
                    settled_at_ns = excluded.settled_at_ns
                """,
                (
                    self.principal_id,
                    self.entity_name,
                    source_event_id,
                    callback_kind.value,
                    DispatchTerminalOutcome.SUCCEEDED.value,
                    settled_at_ns,
                    settled_at_ns,
                ),
            )

    def _settle_turn_sources(
        self,
        source_event_ids: tuple[str, ...],
        *,
        outcome: DispatchTerminalOutcome,
        eligible_states: tuple[str, str],
    ) -> None:
        """Compact matching turn-backed rows under one terminal-settlement invariant."""
        if not source_event_ids:
            return
        if any(not source_event_id for source_event_id in source_event_ids):
            msg = "Turn dispatch settlement requires source events"
            raise ValueError(msg)
        settled_at_ns = time.time_ns()
        with self._lock, self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                UPDATE dispatch_obligations
                SET room_id = '',
                    event_source_json = '',
                    semantic_consumer = NULL,
                    state = ?,
                    settled_at_ns = ?
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind IN (?, ?)
                  AND state IN (?, ?)
                """,
                (
                    (
                        outcome.value,
                        settled_at_ns,
                        self.principal_id,
                        self.entity_name,
                        source_event_id,
                        DispatchCallbackKind.MESSAGE.value,
                        DispatchCallbackKind.MEDIA.value,
                        *eligible_states,
                    )
                    for source_event_id in source_event_ids
                ),
            )

    def settle_pending_from_turn_store(self, source_event_ids: tuple[str, ...]) -> None:
        """Compact only transient turn-backed rows after TurnStore becomes durable."""
        self._settle_turn_sources(
            source_event_ids,
            outcome=DispatchTerminalOutcome.SUCCEEDED,
            eligible_states=(_DEFERRED_STATE, _DEFERRED_STATE),
        )

    def settle_intentionally_ignored_turn_sources(self, source_event_ids: tuple[str, ...]) -> None:
        """Compact message or media callbacks intentionally ignored downstream."""
        self._settle_turn_sources(
            source_event_ids,
            outcome=DispatchTerminalOutcome.INTENTIONALLY_IGNORED,
            eligible_states=(_PENDING_STATE, _DEFERRED_STATE),
        )

    def pending(self) -> tuple[DispatchObligation, ...]:
        """Return valid pending work oldest-first while retaining corrupt rows."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT source_event_id, callback_kind, room_id, event_source_json, semantic_consumer, state
                FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND state IN ('pending', 'deferred')
                ORDER BY created_at_ns, rowid
                """,
                (self.principal_id, self.entity_name),
            ).fetchall()
        obligations: list[DispatchObligation] = []
        for row in rows:
            try:
                obligations.append(self._pending_obligation_from_row(row))
            except DispatchObligationCorruptionError:
                logger.error(  # noqa: TRY400
                    "dispatch_obligation_pending_row_corrupt",
                    source_event_id=row["source_event_id"],
                    callback_kind=row["callback_kind"],
                )
        return tuple(obligations)

    def unsettled_source_event_ids(self) -> frozenset[str]:
        """Return raw source IDs whose callbacks are not terminal."""
        with self._lock, self._connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT source_event_id
                FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND state IN (?, ?)
                """,
                (
                    self.principal_id,
                    self.entity_name,
                    _PENDING_STATE,
                    _DEFERRED_STATE,
                ),
            ).fetchall()
        return frozenset(row["source_event_id"] for row in rows)

    def pending_for(self, key: DispatchObligationKey) -> DispatchObligation | None:
        """Reload the first durable payload for one still-pending exact key."""
        self.validate_bound_key(key)
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT source_event_id, callback_kind, room_id, event_source_json, semantic_consumer, state
                FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state IN (?, ?)
                """,
                (
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    _PENDING_STATE,
                    _DEFERRED_STATE,
                ),
            ).fetchone()
        return None if row is None else self._pending_obligation_from_row(row)

    def has_pending(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> bool:
        """Return whether one exact callback remains pending for one source."""
        with self._lock, self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND state IN (?, ?)
                  AND callback_kind = ?
                LIMIT 1
                """,
                (
                    self.principal_id,
                    self.entity_name,
                    source_event_id,
                    _PENDING_STATE,
                    _DEFERRED_STATE,
                    callback_kind.value,
                ),
            ).fetchone()
        return row is not None


def invite_source_event_id(room_id: str, event_source_json: str) -> str:
    """Return the stable synthetic source identity for one invite."""
    digest = hashlib.sha256(f"{room_id}\0{event_source_json}".encode()).hexdigest()
    return f"invite:{digest}"
