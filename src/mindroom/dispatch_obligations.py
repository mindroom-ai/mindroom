"""Durable exact obligations for fallible Matrix callback dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

import nio
from typing_extensions import TypeIs

from mindroom.background_tasks import create_background_task, run_blocking_until_complete
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_scope
from mindroom.dispatch_source import IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.logging_config import get_logger
from mindroom.matrix.media import MATRIX_MEDIA_EVENT_TYPES, MatrixMediaEvent, parse_matrix_media_event_source

logger = get_logger(__name__)

_SCHEMA_VERSION = 1
_PENDING_STATE = "pending"
_DEFERRED_STATE = "deferred"
_UNSETTLED_STATES = (_PENDING_STATE, _DEFERRED_STATE)
_SQLITE_BUSY_TIMEOUT_MILLISECONDS = 5_000
_RETRY_INITIAL_DELAY_SECONDS = 1.0
_RETRY_MAX_DELAY_SECONDS = 30.0
_TOOL_APPROVAL_RESPONSE_EVENT_TYPE = "io.mindroom.tool_approval_response"
_RECOVERY_SECURITY_METADATA_KEY = "io.mindroom.dispatch_recovery_security"


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


class _DispatchTerminalOutcome(StrEnum):
    """Explicit terminal outcomes for one exact callback obligation."""

    SUCCEEDED = "succeeded"
    INTENTIONALLY_IGNORED = "intentionally_ignored"


class _DispatchCreateResult(StrEnum):
    """Result of durably creating one pending obligation."""

    CREATED = "created"
    ALREADY_PENDING = "already_pending"
    ALREADY_TERMINAL = "already_terminal"


class _DispatchCallbackResult(StrEnum):
    """One explicit callback outcome visible at the durable boundary."""

    SUCCEEDED = "succeeded"
    INTENTIONALLY_IGNORED = "intentionally_ignored"
    DEFERRED = "deferred"


class _DispatchObligationCorruptionError(RuntimeError):
    """A pending row cannot be recovered without inventing source input."""


def _database_name(principal_id: str, entity_name: str) -> str:
    if ".." in entity_name or "/" in entity_name or "\\" in entity_name:
        msg = f"Invalid dispatch-obligation entity name: {entity_name!r}"
        raise ValueError(msg)
    principal_digest = hashlib.sha256(principal_id.encode()).hexdigest()[:12]
    return f"dispatch_obligations-{entity_name}-{principal_digest}.sqlite3"


def callback_kind_for_source_kind(source_kind: str) -> DispatchCallbackKind:
    """Return the durable callback owner for one coalescing source kind."""
    if source_kind in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND}:
        return DispatchCallbackKind.MEDIA
    return DispatchCallbackKind.MESSAGE


@dataclass(frozen=True, slots=True)
class _DispatchObligationKey:
    """Exact durable callback identity."""

    principal_id: str
    entity_name: str
    source_event_id: str
    callback_kind: DispatchCallbackKind


@dataclass(frozen=True, slots=True)
class _DispatchObligation:
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
    def key(self) -> _DispatchObligationKey:
        """Return the exact durable identity."""
        return _DispatchObligationKey(
            principal_id=self.principal_id,
            entity_name=self.entity_name,
            source_event_id=self.source_event_id,
            callback_kind=self.callback_kind,
        )


_ADMITTED_OBLIGATION: ContextVar[_DispatchObligation | None] = ContextVar(
    "admitted_dispatch_obligation",
    default=None,
)
_RUNNING_OBLIGATION: ContextVar[_DispatchObligation | None] = ContextVar(
    "running_dispatch_obligation",
    default=None,
)


@dataclass(frozen=True, slots=True)
class _StoredRow:
    room_id: str
    event_source_json: str
    state: str


class _RoomIdEvent(Protocol):
    """Nio event carrying the room attached by its decryption pipeline."""

    room_id: str


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
    def _event_source_json(obligation: _DispatchObligation) -> str:
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
            _invite_source_event_id(obligation.room_id, event_source_json)
            if obligation.callback_kind is DispatchCallbackKind.INVITE
            else obligation.event_source.get("event_id")
        )
        if expected_source_event_id != obligation.source_event_id:
            msg = "Dispatch obligation source event ID does not match its event payload"
            raise ValueError(msg)
        return event_source_json

    def _validate_bound_key(self, key: _DispatchObligationKey) -> None:
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

    def _pending_obligation_from_row(self, row: sqlite3.Row) -> _DispatchObligation:
        """Decode one exact pending row without inventing source input."""
        try:
            callback_kind = DispatchCallbackKind(row["callback_kind"])
            event_source = json.loads(row["event_source_json"])
            semantic_consumer = (
                DispatchSemanticConsumer(row["semantic_consumer"]) if row["semantic_consumer"] is not None else None
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            msg = f"corrupt dispatch obligation {row['source_event_id']!r}/{row['callback_kind']!r}"
            raise _DispatchObligationCorruptionError(msg) from exc
        if semantic_consumer is not None and semantic_consumer.callback_kind is not callback_kind:
            msg = f"corrupt dispatch obligation {row['source_event_id']!r}/{row['callback_kind']!r}"
            raise _DispatchObligationCorruptionError(msg)
        if not isinstance(event_source, dict):
            msg = f"corrupt dispatch obligation {row['source_event_id']!r}/{row['callback_kind']!r}"
            raise _DispatchObligationCorruptionError(msg)
        return _DispatchObligation(
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

    def _create_pending(self, obligation: _DispatchObligation) -> _DispatchCreateResult:
        """Durably create pending work before its callback can run."""
        self._validate_bound_key(obligation.key)
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
                return _DispatchCreateResult.ALREADY_TERMINAL
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
                return _DispatchCreateResult.ALREADY_PENDING
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
        return _DispatchCreateResult.CREATED

    def settle(
        self,
        key: _DispatchObligationKey,
        outcome: _DispatchTerminalOutcome,
    ) -> None:
        """Durably settle one exact pending callback."""
        self._validate_bound_key(key)
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

    def _discard_pending(self, key: _DispatchObligationKey) -> None:
        """Remove successful work whose source has no permanent Matrix event ID."""
        self._validate_bound_key(key)
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

    def _mark_callback_pending(self, key: _DispatchObligationKey) -> bool:
        """Return deferred turn work to callback ownership before retrying it."""
        self._validate_bound_key(key)
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
        key: _DispatchObligationKey,
        consumer: DispatchSemanticConsumer,
    ) -> DispatchSemanticConsumer:
        """Persist the sole application consumer before it performs side effects."""
        self._validate_bound_key(key)
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

    def _receipt_order(self, key: _DispatchObligationKey) -> int:
        """Return the stable SQLite admission order for one exact callback."""
        self._validate_bound_key(key)
        with self._lock, self._connection() as connection:
            # SQLite may reuse a deleted maximum rowid, so MESSAGE and REACTION
            # rows remain permanent and `_discard_pending` enforces INVITE-only deletion.
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

    def _mark_callback_deferred(self, key: _DispatchObligationKey) -> None:
        """Record that the callback completed and downstream turn work owns the source."""
        self._validate_bound_key(key)
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

    def _settle_from_turn_store(
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
                    _DispatchTerminalOutcome.SUCCEEDED.value,
                    settled_at_ns,
                    settled_at_ns,
                ),
            )

    def _settle_turn_sources(
        self,
        source_event_ids: tuple[str, ...],
        *,
        outcome: _DispatchTerminalOutcome,
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

    def _settle_pending_from_turn_store(self, source_event_ids: tuple[str, ...]) -> None:
        """Compact only transient turn-backed rows after TurnStore becomes durable."""
        self._settle_turn_sources(
            source_event_ids,
            outcome=_DispatchTerminalOutcome.SUCCEEDED,
            eligible_states=(_DEFERRED_STATE, _DEFERRED_STATE),
        )

    def settle_intentionally_ignored_turn_sources(self, source_event_ids: tuple[str, ...]) -> None:
        """Compact message or media callbacks intentionally ignored downstream."""
        self._settle_turn_sources(
            source_event_ids,
            outcome=_DispatchTerminalOutcome.INTENTIONALLY_IGNORED,
            eligible_states=(_PENDING_STATE, _DEFERRED_STATE),
        )

    def pending(self) -> tuple[_DispatchObligation, ...]:
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
        obligations: list[_DispatchObligation] = []
        for row in rows:
            try:
                obligations.append(self._pending_obligation_from_row(row))
            except _DispatchObligationCorruptionError:
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

    def _pending_for(self, key: _DispatchObligationKey) -> _DispatchObligation | None:
        """Reload the first durable payload for one still-pending exact key."""
        self._validate_bound_key(key)
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

    def _has_pending(
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


_DispatchEvent = nio.Event | nio.InviteEvent
_DispatchCallback = Callable[[nio.MatrixRoom, _DispatchEvent], Awaitable[_DispatchCallbackResult]]
_MessageCallback = Callable[[nio.MatrixRoom, nio.RoomMessageText], Awaitable[TurnDispatchOutcome]]
_MediaCallback = Callable[[nio.MatrixRoom, MatrixMediaEvent], Awaitable[TurnDispatchOutcome]]
_ReactionCallback = Callable[[nio.MatrixRoom, nio.ReactionEvent], Awaitable[None]]
_ApprovalCallback = Callable[[nio.MatrixRoom, nio.UnknownEvent], Awaitable[None]]
_InviteCallback = Callable[[nio.MatrixRoom, nio.InviteEvent], Awaitable[None]]
_RoomLifecycleCallback = Callable[[nio.MatrixRoom, nio.RoomMemberEvent], Awaitable[None]]
_RedactionCallback = Callable[[nio.MatrixRoom, nio.RedactionEvent], Awaitable[None]]
_DecryptionFailureCallback = Callable[[nio.MatrixRoom, nio.MegolmEvent], Awaitable[None]]

_TURN_BACKED_KINDS = frozenset({DispatchCallbackKind.MESSAGE, DispatchCallbackKind.MEDIA})


def _invite_source_event_id(room_id: str, event_source_json: str) -> str:
    digest = hashlib.sha256(f"{room_id}\0{event_source_json}".encode()).hexdigest()
    return f"invite:{digest}"


def _dispatch_event_source(event: _DispatchEvent) -> dict[str, object]:
    source = dict(event.source)
    source.pop(_RECOVERY_SECURITY_METADATA_KEY, None)
    if isinstance(event, nio.Event) and event.decrypted:
        source[_RECOVERY_SECURITY_METADATA_KEY] = {
            "decrypted": True,
            "verified": event.verified,
            "sender_key": event.sender_key,
            "session_id": event.session_id,
        }
    if isinstance(event, nio.InviteMemberEvent):
        # nio pops content while parsing invites, so restore it for stable durable replay keys.
        source["content"] = dict(event.content)
    return source


def _apply_recovery_security_metadata(
    event: nio.Event,
    metadata: object,
    *,
    room_id: str,
    source_event_id: str,
) -> None:
    """Restore nio facts attached after a decrypted payload was parsed."""
    if metadata is None:
        return
    if not isinstance(metadata, dict):
        msg = f"corrupt dispatch obligation event {source_event_id!r}/security metadata"
        raise _DispatchObligationCorruptionError(msg)
    metadata_dict = cast("dict[str, object]", metadata)
    verified = metadata_dict.get("verified")
    sender_key = metadata_dict.get("sender_key")
    session_id = metadata_dict.get("session_id")
    if (
        metadata_dict.get("decrypted") is not True
        or not isinstance(verified, bool)
        or (sender_key is not None and not isinstance(sender_key, str))
        or (session_id is not None and not isinstance(session_id, str))
    ):
        msg = f"corrupt dispatch obligation event {source_event_id!r}/security metadata"
        raise _DispatchObligationCorruptionError(msg)
    event.decrypted = True
    event.verified = verified
    event.sender_key = sender_key
    event.session_id = session_id
    # Nio attaches this attribute to decrypted events even though its base Event
    # annotation omits it; the protocol keeps that runtime contract explicit here.
    cast(_RoomIdEvent, event).room_id = room_id  # noqa: TC006


def _dispatch_source_event_id(
    room_id: str,
    event: _DispatchEvent,
    callback_kind: DispatchCallbackKind,
    event_source_json: str,
) -> str:
    if callback_kind is DispatchCallbackKind.INVITE:
        if not isinstance(event, nio.InviteEvent):
            msg = "Invite dispatch requires an invite event"
            raise TypeError(msg)
        return _invite_source_event_id(room_id, event_source_json)
    if not isinstance(event, nio.Event):
        msg = f"{callback_kind.value} dispatch requires an event with an exact Matrix event ID"
        raise TypeError(msg)
    return event.event_id


def _parse_recovery_event(obligation: _DispatchObligation) -> _DispatchEvent:
    if obligation.callback_kind is DispatchCallbackKind.INVITE:
        event_source_json = json.dumps(
            obligation.event_source,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        if _invite_source_event_id(obligation.room_id, event_source_json) != obligation.source_event_id:
            msg = f"corrupt dispatch obligation event {obligation.source_event_id!r}/'invite'"
            raise _DispatchObligationCorruptionError(msg)
        event = nio.InviteEvent.parse_event(dict(obligation.event_source))
        if not isinstance(event, nio.InviteEvent):
            msg = f"corrupt dispatch obligation event {obligation.source_event_id!r}/'invite'"
            raise _DispatchObligationCorruptionError(msg)
        return event
    event_source = dict(obligation.event_source)
    security_metadata = event_source.pop(_RECOVERY_SECURITY_METADATA_KEY, None)
    event = (
        parse_matrix_media_event_source(event_source)
        if obligation.callback_kind is DispatchCallbackKind.MEDIA
        else nio.Event.parse_event(event_source)
    )
    if not isinstance(event, nio.Event) or event.event_id != obligation.source_event_id:
        msg = f"corrupt dispatch obligation event {obligation.source_event_id!r}/{obligation.callback_kind.value!r}"
        raise _DispatchObligationCorruptionError(msg)
    if isinstance(event, nio.MegolmEvent):
        event.room_id = obligation.room_id
    _apply_recovery_security_metadata(
        event,
        security_metadata,
        room_id=obligation.room_id,
        source_event_id=obligation.source_event_id,
    )
    return event


@dataclass(frozen=True, slots=True)
class _CallbackBindings:
    on_message: _MessageCallback
    on_media: _MediaCallback
    on_reaction: _ReactionCallback
    on_approval: _ApprovalCallback
    on_invite: _InviteCallback
    on_room_lifecycle: _RoomLifecycleCallback
    on_redaction: _RedactionCallback
    on_decryption_failure: _DecryptionFailureCallback
    source_has_live_owner: Callable[[str], bool]

    def as_mapping(self) -> Mapping[DispatchCallbackKind, _DispatchCallback]:
        return {
            DispatchCallbackKind.MESSAGE: self.dispatch_message,
            DispatchCallbackKind.MEDIA: self.dispatch_media,
            DispatchCallbackKind.REACTION: self.dispatch_reaction,
            DispatchCallbackKind.APPROVAL: self.dispatch_approval,
            DispatchCallbackKind.INVITE: self.dispatch_invite,
            DispatchCallbackKind.ROOM_LIFECYCLE: self.dispatch_room_lifecycle,
            DispatchCallbackKind.REDACTION: self.dispatch_redaction,
            DispatchCallbackKind.DECRYPTION_FAILURE: self.dispatch_decryption_failure,
        }

    @staticmethod
    def _turn_result(outcome: TurnDispatchOutcome) -> _DispatchCallbackResult:
        if outcome is TurnDispatchOutcome.DEFERRED:
            return _DispatchCallbackResult.DEFERRED
        if outcome is TurnDispatchOutcome.INTENTIONALLY_IGNORED:
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        msg = f"Turn dispatch callback returned invalid outcome {outcome!r}"
        raise TypeError(msg)

    async def dispatch_message(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, nio.RoomMessageText):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        return self._turn_result(await self.on_message(room, event))

    async def dispatch_media(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, MATRIX_MEDIA_EVENT_TYPES):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        if self.source_has_live_owner(event.event_id):
            return _DispatchCallbackResult.DEFERRED
        return self._turn_result(await self.on_media(room, event))

    async def dispatch_reaction(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, nio.ReactionEvent):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_reaction(room, event)
        return _DispatchCallbackResult.SUCCEEDED

    async def dispatch_approval(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, nio.Event) or not _is_tool_approval_response(event):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_approval(room, event)
        return _DispatchCallbackResult.SUCCEEDED

    async def dispatch_invite(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, nio.InviteEvent):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_invite(room, event)
        return _DispatchCallbackResult.SUCCEEDED

    async def dispatch_room_lifecycle(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, nio.RoomMemberEvent):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_room_lifecycle(room, event)
        return _DispatchCallbackResult.SUCCEEDED

    async def dispatch_redaction(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, nio.RedactionEvent):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_redaction(room, event)
        return _DispatchCallbackResult.SUCCEEDED

    async def dispatch_decryption_failure(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, nio.MegolmEvent):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_decryption_failure(room, event)
        return _DispatchCallbackResult.SUCCEEDED


@dataclass
class DispatchObligationRunner:
    """Persist, execute, and directly recover exact Matrix callbacks."""

    store: DispatchObligationStore
    callbacks: Mapping[DispatchCallbackKind, _DispatchCallback]
    room_for_id: Callable[[str], nio.MatrixRoom]
    turn_is_terminal: Callable[[str], bool]
    on_persist_failure: Callable[[], None] | None = None
    background_task_owner: object | None = None
    room_lifecycle_admission_enabled: Callable[[], bool] = lambda: False
    _retry_initial_delay_seconds: float = field(default=_RETRY_INITIAL_DELAY_SECONDS, repr=False)
    _retry_max_delay_seconds: float = field(default=_RETRY_MAX_DELAY_SECONDS, repr=False)
    _active: set[_DispatchObligationKey] = field(default_factory=set, init=False, repr=False)
    _active_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _retry_keys: dict[_DispatchObligationKey, int] = field(default_factory=dict, init=False, repr=False)
    _retry_corrupt: set[_DispatchObligationKey] = field(default_factory=set, init=False, repr=False)
    _retry_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _event_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _turn_settlement_retry_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _turn_settlement_retry_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @staticmethod
    def callbacks_for(
        *,
        on_message: _MessageCallback,
        on_media: _MediaCallback,
        on_reaction: _ReactionCallback,
        on_approval: _ApprovalCallback,
        on_invite: _InviteCallback,
        on_room_lifecycle: _RoomLifecycleCallback,
        on_redaction: _RedactionCallback,
        on_decryption_failure: _DecryptionFailureCallback,
        source_has_live_owner: Callable[[str], bool],
    ) -> Mapping[DispatchCallbackKind, _DispatchCallback]:
        """Bind typed Matrix callbacks to explicit durable outcomes."""
        return _CallbackBindings(
            on_message=on_message,
            on_media=on_media,
            on_reaction=on_reaction,
            on_approval=on_approval,
            on_invite=on_invite,
            on_room_lifecycle=on_room_lifecycle,
            on_redaction=on_redaction,
            on_decryption_failure=on_decryption_failure,
            source_has_live_owner=source_has_live_owner,
        ).as_mapping()

    def task_wrapper(
        self,
        callback_kind: DispatchCallbackKind,
        *,
        owner: object,
    ) -> _DispatchObligationTaskWrapper:
        """Return an ordinary callback that executes already-admitted work."""
        return _DispatchObligationTaskWrapper(
            runner=self,
            callback_kind=callback_kind,
            owner=owner,
        )

    def retry_pending_turn_source(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> None:
        """Return one deferred turn source to its exact stored callback owner."""
        if callback_kind not in _TURN_BACKED_KINDS:
            msg = "Deferred turn retry requires a message or media callback kind"
            raise ValueError(msg)
        self._schedule_retry(
            _DispatchObligationKey(
                principal_id=self.store.principal_id,
                entity_name=self.store.entity_name,
                source_event_id=source_event_id,
                callback_kind=callback_kind,
            ),
        )

    def retry_pending_turn_sources(self, source_event_ids: tuple[str, ...]) -> None:
        """Return turn sources of either ingress kind to their stored callback owners."""
        for source_event_id in source_event_ids:
            for callback_kind in _TURN_BACKED_KINDS:
                self.retry_pending_turn_source(source_event_id, callback_kind)

    def semantic_consumer(self) -> DispatchSemanticConsumer | None:
        """Return the durable application consumer for the running callback."""
        obligation = _RUNNING_OBLIGATION.get()
        if obligation is None:
            return None
        self.store._validate_bound_key(obligation.key)
        return obligation.semantic_consumer

    async def receipt_order(self) -> int:
        """Return the durable admission order of the running callback."""
        obligation = _RUNNING_OBLIGATION.get()
        if obligation is None:
            msg = "Dispatch receipt order is only available inside a durable callback"
            raise RuntimeError(msg)
        return await asyncio.to_thread(self.store._receipt_order, obligation.key)

    async def claim_semantic_consumer(self, consumer: DispatchSemanticConsumer) -> None:
        """Freeze the running callback's application consumer before side effects."""
        obligation = _RUNNING_OBLIGATION.get()
        if obligation is None:
            msg = "A semantic consumer can be claimed only inside a durable callback"
            raise RuntimeError(msg)
        claimed_consumer = await run_blocking_until_complete(
            self.store.claim_semantic_consumer,
            obligation.key,
            consumer,
        )
        if claimed_consumer is not consumer:
            msg = f"Dispatch callback is already owned by {claimed_consumer.value!r}"
            raise RuntimeError(msg)
        _RUNNING_OBLIGATION.set(replace(obligation, semantic_consumer=consumer))

    async def settle_intentionally_ignored_turn_sources(
        self,
        source_event_ids: tuple[str, ...],
    ) -> None:
        """Settle deferred turn sources that downstream dispatch intentionally ignored."""
        await run_blocking_until_complete(
            self.store.settle_intentionally_ignored_turn_sources,
            source_event_ids,
        )

    def bind_event_loop(self) -> None:
        """Bind the active runtime loop for callbacks arriving from persistence workers."""
        self._event_loop = asyncio.get_running_loop()

    def retry_turn_settlement(self, source_event_ids: tuple[str, ...]) -> None:
        """Retry failed TurnStore compaction on the active runtime loop."""
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is None:
            try:
                self.store._settle_pending_from_turn_store(source_event_ids)
            except Exception:
                logger.exception(
                    "turn_dispatch_obligation_initial_settlement_failed",
                    source_event_ids=source_event_ids,
                )
            else:
                return
            event_loop = self._event_loop
            if event_loop is None or event_loop.is_closed():
                logger.error(
                    "turn_dispatch_obligation_retry_loop_unavailable",
                    source_event_ids=source_event_ids,
                )
                return
            event_loop.call_soon_threadsafe(self._enqueue_turn_settlement_retry, source_event_ids)
            return
        self._event_loop = running_loop
        self._enqueue_turn_settlement_retry(source_event_ids)

    def _enqueue_turn_settlement_retry(self, source_event_ids: tuple[str, ...]) -> None:
        """Add exact terminal sources to the loop-owned settlement retry set."""
        self._turn_settlement_retry_ids.update(source_event_ids)
        retry_task = self._turn_settlement_retry_task
        if retry_task is not None and not retry_task.done():
            return
        self._turn_settlement_retry_task = create_background_task(
            self._retry_failed_turn_settlements(),
            name=f"retry_turn_dispatch_settlement_{self.store.entity_name}",
            owner=self.background_task_owner,
        )

    async def _retry_failed_turn_settlements(self) -> None:
        """Retry terminal TurnStore compaction with capped backoff until it lands."""
        retry_delay_seconds = self._retry_initial_delay_seconds
        try:
            while self._turn_settlement_retry_ids:
                source_event_ids = tuple(self._turn_settlement_retry_ids)
                try:
                    await run_blocking_until_complete(
                        self.store._settle_pending_from_turn_store,
                        source_event_ids,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "turn_dispatch_obligation_settlement_retry_failed",
                        source_event_ids=source_event_ids,
                    )
                    await asyncio.sleep(retry_delay_seconds)
                    retry_delay_seconds = min(
                        retry_delay_seconds * 2,
                        self._retry_max_delay_seconds,
                    )
                else:
                    self._turn_settlement_retry_ids.difference_update(source_event_ids)
                    retry_delay_seconds = self._retry_initial_delay_seconds
        finally:
            self._turn_settlement_retry_task = None

    def register_source_callbacks(self, client: nio.AsyncClient, *, owner: object) -> None:
        """Register every source-backed correctness callback except delayed room lifecycle."""
        client.add_event_admission_callback(self._admit_source_event)
        for policy in _SOURCE_CALLBACK_POLICIES:
            callback = self.task_wrapper(policy.callback_kind, owner=owner)
            if policy.predicate is None:
                for event_type in policy.event_types:
                    client.add_event_callback(callback, event_type)
                continue

            async def dispatch_matching(
                room: nio.MatrixRoom,
                event: nio.Event,
                *,
                callback: _DispatchObligationTaskWrapper = callback,
                policy: _SourceCallbackPolicy = policy,
            ) -> None:
                if policy.matches(event):
                    await callback(room, event)

            for event_type in policy.event_types:
                client.add_event_callback(dispatch_matching, event_type)

    async def _admit_source_event(self, room: nio.MatrixRoom, event: nio.Event) -> None:
        """Route every correctness-critical timeline event through one nio owner."""
        callback_kind = self._admission_kind(event)
        if callback_kind is None:
            return
        _ADMITTED_OBLIGATION.set(None)
        try:
            obligation = await self.persist(room, event, callback_kind)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise nio.CallbackNotAcceptedError(str(error)) from error
        _ADMITTED_OBLIGATION.set(obligation)

    def _admission_kind(self, event: nio.Event) -> DispatchCallbackKind | None:
        """Return the one durable callback kind owned by a timeline event."""
        for policy in _SOURCE_CALLBACK_POLICIES:
            if policy.matches(event):
                return policy.callback_kind
        if isinstance(event, nio.RoomMemberEvent) and self.room_lifecycle_admission_enabled():
            return DispatchCallbackKind.ROOM_LIFECYCLE
        return None

    async def dispatch(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        callback_kind: DispatchCallbackKind,
    ) -> None:
        """Persist exact work before invoking its fallible callback."""
        obligation = await self.persist(room, event, callback_kind)
        if obligation is None:
            return
        try:
            await self._run_persisted(obligation, room=room, event=event)
        except Exception:
            self._schedule_retry(obligation.key)
            raise

    async def dispatch_background(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        callback_kind: DispatchCallbackKind,
        *,
        owner: object,
    ) -> None:
        """Persist exact work before scheduling its fallible callback."""
        obligation = await self.persist(room, event, callback_kind)
        if obligation is None:
            return
        self._schedule_background_obligation(
            obligation,
            room=room,
            event=event,
            owner=owner,
        )

    async def persist(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        callback_kind: DispatchCallbackKind,
    ) -> _DispatchObligation | None:
        """Persist exact work before its background task may be created."""
        self._event_loop = asyncio.get_running_loop()
        try:
            obligation = self._obligation_for_event(room, event, callback_kind)
            create_result = await run_blocking_until_complete(self.store._create_pending, obligation)
            if create_result is _DispatchCreateResult.ALREADY_TERMINAL:
                persisted_obligation = None
            elif create_result is _DispatchCreateResult.ALREADY_PENDING:
                persisted_obligation = await asyncio.to_thread(self.store._pending_for, obligation.key)
            else:
                persisted_obligation = None if await self._settle_from_turn_store_if_owned(obligation) else obligation
        except (asyncio.CancelledError, Exception):
            if self.on_persist_failure is not None:
                self.on_persist_failure()
            raise
        return persisted_obligation

    def _obligation_for_event(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        callback_kind: DispatchCallbackKind,
    ) -> _DispatchObligation:
        event_source = _dispatch_event_source(event)
        event_source_json = json.dumps(
            event_source,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return _DispatchObligation(
            principal_id=self.store.principal_id,
            entity_name=self.store.entity_name,
            source_event_id=_dispatch_source_event_id(
                room.room_id,
                event,
                callback_kind,
                event_source_json,
            ),
            callback_kind=callback_kind,
            room_id=room.room_id,
            event_source=event_source,
        )

    async def _run_admitted(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        callback_kind: DispatchCallbackKind,
    ) -> None:
        """Execute one exact obligation previously accepted by nio admission."""
        key = self._obligation_for_event(room, event, callback_kind).key
        try:
            obligation = await asyncio.to_thread(self.store._pending_for, key)
            if obligation is None:
                return
            await self._run_persisted(obligation, room=room, event=event)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._schedule_retry(key)
            raise

    def _schedule_background_obligation(
        self,
        obligation: _DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        owner: object,
    ) -> None:
        """Schedule one exact accepted payload without reloading it from SQLite."""
        create_background_task(
            self._run_background_obligation(obligation, room=room, event=event),
            owner=owner,
        )

    async def _run_background_obligation(
        self,
        obligation: _DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> None:
        try:
            await self._run_persisted(obligation, room=room, event=event)
        except asyncio.CancelledError:
            return
        except Exception:
            self._schedule_retry(obligation.key)
            logger.exception(
                "dispatch_obligation_callback_failed",
                source_event_id=obligation.source_event_id,
                callback_kind=obligation.callback_kind.value,
                room_id=obligation.room_id,
            )

    async def _run_persisted(
        self,
        obligation: _DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> None:
        """Execute work whose exact durable obligation already exists."""
        if room.room_id != obligation.room_id or _dispatch_event_source(event) != obligation.event_source:
            room = self.room_for_id(obligation.room_id)
            event = _parse_recovery_event(obligation)
        if obligation.requires_pending_check:
            with turn_dispatch_recovery_scope(active=obligation.callback_kind in _TURN_BACKED_KINDS):
                await self._run_obligation(obligation, room=room, event=event)
            return
        await self._run_obligation(obligation, room=room, event=event)

    async def recover_pending(self, *, turn_backed: bool | None = None) -> None:
        """Retry every valid pending callback without waiting for another sync response."""
        self._event_loop = asyncio.get_running_loop()
        failed_keys: list[_DispatchObligationKey] = []
        for obligation in await asyncio.to_thread(self.store.pending):
            if turn_backed is not None and (obligation.callback_kind in _TURN_BACKED_KINDS) != turn_backed:
                continue
            try:
                event = _parse_recovery_event(obligation)
                room = self.room_for_id(obligation.room_id)
                with turn_dispatch_recovery_scope(active=obligation.callback_kind in _TURN_BACKED_KINDS):
                    await self._run_obligation(
                        obligation,
                        room=room,
                        event=event,
                    )
            except asyncio.CancelledError:
                raise
            except _DispatchObligationCorruptionError:
                logger.error(  # noqa: TRY400
                    "dispatch_obligation_recovery_corrupt",
                    source_event_id=obligation.source_event_id,
                    callback_kind=obligation.callback_kind.value,
                    room_id=obligation.room_id,
                )
            except Exception:
                logger.exception(
                    "dispatch_obligation_recovery_failed",
                    source_event_id=obligation.source_event_id,
                    callback_kind=obligation.callback_kind.value,
                    room_id=obligation.room_id,
                )
                failed_keys.append(obligation.key)
        for key in failed_keys:
            self._schedule_retry(key)

    def _schedule_retry(self, key: _DispatchObligationKey) -> None:
        """Ensure one failed exact callback remains autonomously retry-owned."""
        if key in self._retry_corrupt:
            return
        self._retry_keys.setdefault(key, 0)
        if self._retry_task is not None and not self._retry_task.done():
            return
        self._retry_task = create_background_task(
            self._retry_failed_obligations(),
            name=f"retry_dispatch_obligations_{self.store.entity_name}",
            owner=self.background_task_owner,
        )

    async def _drop_settled_retry_keys(self) -> None:
        """Remove retry keys whose durable obligation is no longer pending."""
        for key in tuple(self._retry_keys):
            try:
                is_pending = await asyncio.to_thread(
                    self.store._has_pending,
                    key.source_event_id,
                    key.callback_kind,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "dispatch_obligation_retry_discovery_failed",
                    source_event_id=key.source_event_id,
                    callback_kind=key.callback_kind.value,
                )
                continue
            if not is_pending:
                self._retry_keys.pop(key, None)

    async def _retry_failed_obligations(self) -> None:
        """Retry only callback failures, with one capped-backoff task per runner."""
        retry_delay_seconds = self._retry_initial_delay_seconds
        try:
            while self._retry_keys:
                await self._drop_settled_retry_keys()
                if not self._retry_keys:
                    break
                await asyncio.sleep(retry_delay_seconds)
                for key in tuple(self._retry_keys):
                    completed_attempts = self._retry_keys.pop(key)
                    try:
                        obligation = await asyncio.to_thread(self.store._pending_for, key)
                        if obligation is None:
                            continue
                        event = _parse_recovery_event(obligation)
                        with turn_dispatch_recovery_scope(active=obligation.callback_kind in _TURN_BACKED_KINDS):
                            claimed = await self._run_obligation(
                                obligation,
                                room=self.room_for_id(obligation.room_id),
                                event=event,
                            )
                            if not claimed:
                                self._retry_keys.setdefault(key, completed_attempts)
                    except asyncio.CancelledError:
                        raise
                    except _DispatchObligationCorruptionError:
                        self._retry_corrupt.add(key)
                        logger.exception(
                            "dispatch_obligation_retry_corrupt",
                            source_event_id=key.source_event_id,
                            callback_kind=key.callback_kind.value,
                        )
                    except Exception:
                        completed_attempts += 1
                        self._retry_keys.setdefault(key, completed_attempts)
                        logger.exception(
                            "dispatch_obligation_retry_failed",
                            source_event_id=key.source_event_id,
                            callback_kind=key.callback_kind.value,
                            retry_attempt=completed_attempts,
                        )
                retry_delay_seconds = min(
                    retry_delay_seconds * 2,
                    self._retry_max_delay_seconds,
                )
        finally:
            self._retry_task = None

    async def _run_obligation(
        self,
        obligation: _DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> bool:
        """Run one obligation, returning whether this caller acquired its live claim."""
        if not await self._claim(obligation.key):
            return False
        try:
            if obligation.requires_pending_check and not await asyncio.to_thread(
                self.store._has_pending,
                obligation.source_event_id,
                obligation.callback_kind,
            ):
                return True
            if obligation.callback_completed:
                if await self._settle_from_turn_store_if_owned(obligation):
                    return True
                callback_reclaimed = await run_blocking_until_complete(
                    self.store._mark_callback_pending,
                    obligation.key,
                )
                if not callback_reclaimed:
                    return True
                obligation = replace(obligation, callback_completed=False, requires_pending_check=False)
            callback = self.callbacks.get(obligation.callback_kind)
            if callback is None:
                msg = f"No callback registered for {obligation.callback_kind.value!r}"
                raise RuntimeError(msg)
            running_token = _RUNNING_OBLIGATION.set(obligation)
            try:
                callback_result = await callback(room, event)
            finally:
                _RUNNING_OBLIGATION.reset(running_token)
            await self._settle_callback_result(obligation, callback_result)
            return True
        finally:
            await self._release(obligation.key)

    async def _settle_from_turn_store_if_owned(self, obligation: _DispatchObligation) -> bool:
        if obligation.callback_kind not in _TURN_BACKED_KINDS:
            return False
        if not await asyncio.to_thread(self.turn_is_terminal, obligation.source_event_id):
            return False
        await run_blocking_until_complete(
            self.store._settle_from_turn_store,
            obligation.source_event_id,
            obligation.callback_kind,
        )
        return True

    async def _settle_callback_result(
        self,
        obligation: _DispatchObligation,
        result: _DispatchCallbackResult,
    ) -> None:
        if not isinstance(result, _DispatchCallbackResult):
            msg = f"Dispatch callback returned invalid result {result!r}"
            raise TypeError(msg)
        if await self._settle_from_turn_store_if_owned(obligation):
            return
        if result is _DispatchCallbackResult.DEFERRED:
            await run_blocking_until_complete(self.store._mark_callback_deferred, obligation.key)
            await self._settle_from_turn_store_if_owned(obligation)
            return
        if obligation.callback_kind is DispatchCallbackKind.INVITE:
            await run_blocking_until_complete(self.store._discard_pending, obligation.key)
            return
        outcome = (
            _DispatchTerminalOutcome.SUCCEEDED
            if result is _DispatchCallbackResult.SUCCEEDED
            else _DispatchTerminalOutcome.INTENTIONALLY_IGNORED
        )
        await run_blocking_until_complete(self.store.settle, obligation.key, outcome)

    async def _claim(self, key: _DispatchObligationKey) -> bool:
        async with self._active_lock:
            if key in self._active:
                return False
            self._active.add(key)
            return True

    async def _release(self, key: _DispatchObligationKey) -> None:
        async with self._active_lock:
            self._active.discard(key)


@dataclass(frozen=True, slots=True)
class _DispatchObligationTaskWrapper:
    """Schedule execution only after nio admits every matching callback."""

    runner: DispatchObligationRunner
    callback_kind: DispatchCallbackKind
    owner: object

    async def __call__(self, room: nio.MatrixRoom, event: _DispatchEvent) -> None:
        """Schedule already-persisted work without repeating durable admission."""
        key = self.runner._obligation_for_event(room, event, self.callback_kind).key
        obligation = _ADMITTED_OBLIGATION.get()
        _ADMITTED_OBLIGATION.set(None)
        if obligation is not None and obligation.key == key:
            self.runner._schedule_background_obligation(
                obligation,
                room=room,
                event=event,
                owner=self.owner,
            )
            return
        create_background_task(
            self._run(room=room, event=event),
            owner=self.owner,
        )

    async def _run(
        self,
        *,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> None:
        try:
            await self.runner._run_admitted(
                room,
                event,
                self.callback_kind,
            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "dispatch_obligation_callback_failed",
                source_event_id=self.runner._obligation_for_event(
                    room,
                    event,
                    self.callback_kind,
                ).source_event_id,
                callback_kind=self.callback_kind.value,
                room_id=room.room_id,
            )


def _is_tool_approval_response(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    return isinstance(event, nio.UnknownEvent) and event.type == _TOOL_APPROVAL_RESPONSE_EVENT_TYPE


@dataclass(frozen=True, slots=True)
class _SourceCallbackPolicy:
    """One shared timeline admission and callback-registration rule."""

    callback_kind: DispatchCallbackKind
    event_types: tuple[type[nio.Event], ...]
    predicate: Callable[[nio.Event], bool] | None = None

    def matches(self, event: nio.Event) -> bool:
        """Return whether this policy owns one exact event."""
        return isinstance(event, self.event_types) and (self.predicate is None or self.predicate(event))


_SOURCE_CALLBACK_POLICIES = (
    _SourceCallbackPolicy(DispatchCallbackKind.MESSAGE, (nio.RoomMessageText,)),
    _SourceCallbackPolicy(DispatchCallbackKind.REDACTION, (nio.RedactionEvent,)),
    _SourceCallbackPolicy(DispatchCallbackKind.REACTION, (nio.ReactionEvent,)),
    _SourceCallbackPolicy(DispatchCallbackKind.MEDIA, MATRIX_MEDIA_EVENT_TYPES),
    _SourceCallbackPolicy(
        DispatchCallbackKind.APPROVAL,
        (nio.UnknownEvent,),
        _is_tool_approval_response,
    ),
    _SourceCallbackPolicy(DispatchCallbackKind.DECRYPTION_FAILURE, (nio.MegolmEvent,)),
)
