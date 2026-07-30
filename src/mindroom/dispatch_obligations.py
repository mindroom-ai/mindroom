"""Durable exact obligations for fallible Matrix callback dispatch."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

import nio
from typing_extensions import TypeIs

from mindroom.background_tasks import create_background_task
from mindroom.logging_config import get_logger
from mindroom.matrix.media import MATRIX_MEDIA_EVENT_TYPES, MatrixMediaEvent, parse_matrix_media_event_source

logger = get_logger(__name__)

_DATABASE_NAME = "dispatch_obligations.sqlite3"
_SCHEMA_VERSION = 2
_PENDING_STATE = "pending"
_TOOL_APPROVAL_RESPONSE_EVENT_TYPE = "io.mindroom.tool_approval_response"
_StoreResult = TypeVar("_StoreResult")


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

    @property
    def key(self) -> _DispatchObligationKey:
        """Return the exact durable identity."""
        return _DispatchObligationKey(
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
        self._database_path = self.tracking_path / _DATABASE_NAME
        self._lock = threading.Lock()
        with self._lock, self._connect() as connection:
            self._initialize_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        self.tracking_path.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _initialize_schema(connection: sqlite3.Connection) -> None:
        current_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version not in {0, 1, _SCHEMA_VERSION}:
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
                state TEXT NOT NULL CHECK (
                    state IN ('pending', 'succeeded', 'intentionally_ignored')
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
            WHERE state = 'pending'
            """,
        )
        if current_version == 1:
            connection.execute(
                """
                UPDATE dispatch_obligations
                SET room_id = '', event_source_json = ''
                WHERE state != ?
                """,
                (_PENDING_STATE,),
            )
        if current_version < _SCHEMA_VERSION:
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
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            msg = f"corrupt dispatch obligation {row['source_event_id']!r}/{row['callback_kind']!r}"
            raise _DispatchObligationCorruptionError(msg) from exc
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
        )

    def create_pending(self, obligation: _DispatchObligation) -> _DispatchCreateResult:
        """Durably create pending work before its callback can run."""
        self._validate_bound_key(obligation.key)
        if not obligation.source_event_id:
            msg = "Dispatch obligation requires a source event"
            raise ValueError(msg)
        key = obligation.key
        with self._lock, self._connect() as connection:
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
            if existing is not None and existing.state != _PENDING_STATE:
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
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE dispatch_obligations
                SET room_id = '',
                    event_source_json = '',
                    state = ?,
                    settled_at_ns = ?
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state = ?
                """,
                (
                    outcome.value,
                    time.time_ns(),
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    _PENDING_STATE,
                ),
            )
            if cursor.rowcount == 0:
                existing = connection.execute(
                    """
                    SELECT state
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
                if existing is None:
                    msg = f"Unknown dispatch obligation {key.source_event_id!r}"
                    raise KeyError(msg)

    def discard_pending(self, key: _DispatchObligationKey) -> None:
        """Remove successful work whose source has no permanent Matrix event ID."""
        self._validate_bound_key(key)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                DELETE FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state = ?
                """,
                (
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
        with self._lock, self._connect() as connection:
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

    def settle_pending_from_turn_store(self, source_event_ids: tuple[str, ...]) -> None:
        """Compact only transient turn-backed rows after TurnStore becomes durable."""
        if not source_event_ids:
            return
        if any(not source_event_id for source_event_id in source_event_ids):
            msg = "TurnStore dispatch settlement requires source events"
            raise ValueError(msg)
        settled_at_ns = time.time_ns()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                UPDATE dispatch_obligations
                SET room_id = '',
                    event_source_json = '',
                    state = ?,
                    settled_at_ns = ?
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind IN (?, ?)
                  AND state = ?
                """,
                (
                    (
                        _DispatchTerminalOutcome.SUCCEEDED.value,
                        settled_at_ns,
                        self.principal_id,
                        self.entity_name,
                        source_event_id,
                        DispatchCallbackKind.MESSAGE.value,
                        DispatchCallbackKind.MEDIA.value,
                        _PENDING_STATE,
                    )
                    for source_event_id in source_event_ids
                ),
            )

    def pending(self) -> tuple[_DispatchObligation, ...]:
        """Return pending work oldest-first, failing on unrecoverable durable input."""
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT source_event_id, callback_kind, room_id, event_source_json
                FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND state = ?
                ORDER BY created_at_ns, rowid
                """,
                (self.principal_id, self.entity_name, _PENDING_STATE),
            ).fetchall()
        return tuple(self._pending_obligation_from_row(row) for row in rows)

    def pending_for(self, key: _DispatchObligationKey) -> _DispatchObligation | None:
        """Reload the first durable payload for one still-pending exact key."""
        self._validate_bound_key(key)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_event_id, callback_kind, room_id, event_source_json
                FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND callback_kind = ?
                  AND state = ?
                """,
                (
                    key.principal_id,
                    key.entity_name,
                    key.source_event_id,
                    key.callback_kind.value,
                    _PENDING_STATE,
                ),
            ).fetchone()
        return None if row is None else self._pending_obligation_from_row(row)

    def has_pending(
        self,
        source_event_id: str,
        callback_kind: DispatchCallbackKind,
    ) -> bool:
        """Return whether one exact callback remains pending for one source."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM dispatch_obligations
                WHERE principal_id = ?
                  AND entity_name = ?
                  AND source_event_id = ?
                  AND state = ?
                  AND callback_kind = ?
                LIMIT 1
                """,
                (
                    self.principal_id,
                    self.entity_name,
                    source_event_id,
                    _PENDING_STATE,
                    callback_kind.value,
                ),
            ).fetchone()
        return row is not None


_DispatchEvent = nio.Event | nio.InviteEvent
_DispatchCallback = Callable[[nio.MatrixRoom, _DispatchEvent], Awaitable[_DispatchCallbackResult]]
_MessageCallback = Callable[[nio.MatrixRoom, nio.RoomMessageText], Awaitable[None]]
_MediaCallback = Callable[[nio.MatrixRoom, MatrixMediaEvent], Awaitable[None]]
_ReactionCallback = Callable[[nio.MatrixRoom, nio.ReactionEvent], Awaitable[None]]
_ApprovalCallback = Callable[[nio.MatrixRoom, nio.UnknownEvent], Awaitable[None]]
_InviteCallback = Callable[[nio.MatrixRoom, nio.InviteEvent], Awaitable[None]]
_RoomLifecycleCallback = Callable[[nio.MatrixRoom, nio.RoomMemberEvent], Awaitable[None]]
_RedactionCallback = Callable[[nio.MatrixRoom, nio.RedactionEvent], Awaitable[None]]
_DecryptionFailureCallback = Callable[[nio.MatrixRoom, nio.MegolmEvent], Awaitable[None]]

_TURN_BACKED_KINDS = frozenset({DispatchCallbackKind.MESSAGE, DispatchCallbackKind.MEDIA})


async def _run_owned_store_operation(
    operation: Callable[..., _StoreResult],
    *args: object,
) -> _StoreResult:
    """Finish one owned store operation before propagating cancellation."""
    worker_task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(worker_task)
    except asyncio.CancelledError:
        while not worker_task.done():
            try:
                await asyncio.shield(worker_task)
            except asyncio.CancelledError:
                continue
        worker_task.result()
        raise


async def _run_store_settlement(
    operation: Callable[..., None],
    *args: object,
) -> None:
    """Finish one owned store settlement before propagating cancellation."""
    await _run_owned_store_operation(operation, *args)


def _invite_source_event_id(room_id: str, event_source_json: str) -> str:
    digest = hashlib.sha256(f"{room_id}\0{event_source_json}".encode()).hexdigest()
    return f"invite:{digest}"


def _dispatch_event_source(event: _DispatchEvent) -> dict[str, object]:
    source = dict(event.source)
    if isinstance(event, nio.InviteMemberEvent):
        source["content"] = dict(event.content)
    return source


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
    event = (
        parse_matrix_media_event_source(obligation.event_source)
        if obligation.callback_kind is DispatchCallbackKind.MEDIA
        else nio.Event.parse_event(dict(obligation.event_source))
    )
    if event is None or isinstance(event, nio.BadEvent) or event.event_id != obligation.source_event_id:
        msg = f"corrupt dispatch obligation event {obligation.source_event_id!r}/{obligation.callback_kind.value!r}"
        raise _DispatchObligationCorruptionError(msg)
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
    turn_is_persisted: Callable[[str], bool]
    source_is_deferred: Callable[[str], bool]

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

    def _turn_result(self, source_event_id: str) -> _DispatchCallbackResult:
        if self.turn_is_persisted(source_event_id) or self.source_is_deferred(source_event_id):
            return _DispatchCallbackResult.DEFERRED
        return _DispatchCallbackResult.INTENTIONALLY_IGNORED

    async def dispatch_message(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, nio.RoomMessageText):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_message(room, event)
        return self._turn_result(event.event_id)

    async def dispatch_media(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> _DispatchCallbackResult:
        if not isinstance(event, MATRIX_MEDIA_EVENT_TYPES):
            return _DispatchCallbackResult.INTENTIONALLY_IGNORED
        await self.on_media(room, event)
        return self._turn_result(event.event_id)

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
    _active: set[_DispatchObligationKey] = field(default_factory=set, init=False, repr=False)
    _active_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

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
        turn_is_persisted: Callable[[str], bool],
        source_is_deferred: Callable[[str], bool],
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
            turn_is_persisted=turn_is_persisted,
            source_is_deferred=source_is_deferred,
        ).as_mapping()

    def task_wrapper(
        self,
        callback_kind: DispatchCallbackKind,
        *,
        owner: object,
    ) -> _DispatchObligationTaskWrapper:
        """Return one callback that durably accepts work before task creation."""
        return _DispatchObligationTaskWrapper(
            runner=self,
            callback_kind=callback_kind,
            owner=owner,
        )

    def register_source_callbacks(self, client: nio.AsyncClient, *, owner: object) -> None:
        """Register every source-backed correctness callback except delayed room lifecycle."""
        client.add_event_callback(
            self.task_wrapper(DispatchCallbackKind.MESSAGE, owner=owner),
            nio.RoomMessageText,
        )
        client.add_event_callback(
            self.task_wrapper(DispatchCallbackKind.REDACTION, owner=owner),
            nio.RedactionEvent,
        )
        client.add_event_callback(
            self.task_wrapper(DispatchCallbackKind.REACTION, owner=owner),
            nio.ReactionEvent,
        )
        media_callback = self.task_wrapper(DispatchCallbackKind.MEDIA, owner=owner)
        for event_type in MATRIX_MEDIA_EVENT_TYPES:
            client.add_event_callback(media_callback, event_type)
        approval_callback = self.task_wrapper(DispatchCallbackKind.APPROVAL, owner=owner)

        async def dispatch_approval(room: nio.MatrixRoom, event: nio.Event) -> None:
            if _is_tool_approval_response(event):
                await approval_callback(room, event)

        client.add_event_callback(dispatch_approval, nio.UnknownEvent)
        client.add_event_callback(
            self.task_wrapper(DispatchCallbackKind.DECRYPTION_FAILURE, owner=owner),
            nio.MegolmEvent,
        )

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
        await self.run_persisted(obligation, room=room, event=event)

    async def persist(
        self,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
        callback_kind: DispatchCallbackKind,
    ) -> _DispatchObligation | None:
        """Persist exact work before its background task may be created."""
        try:
            event_source = _dispatch_event_source(event)
            event_source_json = json.dumps(
                event_source,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            source_event_id = _dispatch_source_event_id(
                room.room_id,
                event,
                callback_kind,
                event_source_json,
            )
            obligation = _DispatchObligation(
                principal_id=self.store.principal_id,
                entity_name=self.store.entity_name,
                source_event_id=source_event_id,
                callback_kind=callback_kind,
                room_id=room.room_id,
                event_source=event_source,
            )
            if await self._settle_from_turn_store_if_owned(obligation):
                return None
            create_result = await _run_owned_store_operation(self.store.create_pending, obligation)
            if create_result is _DispatchCreateResult.ALREADY_TERMINAL:
                persisted_obligation = None
            elif create_result is _DispatchCreateResult.ALREADY_PENDING:
                persisted_obligation = await asyncio.to_thread(self.store.pending_for, obligation.key)
            else:
                persisted_obligation = obligation
        except asyncio.CancelledError:
            if self.on_persist_failure is not None:
                self.on_persist_failure()
            raise
        except Exception:
            if self.on_persist_failure is not None:
                self.on_persist_failure()
            raise
        return persisted_obligation

    async def run_persisted(
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
        await self._run_obligation(obligation, room=room, event=event)

    async def recover_pending(self, *, turn_backed: bool | None = None) -> None:
        """Retry every valid pending callback without waiting for another sync response."""
        for obligation in await asyncio.to_thread(self.store.pending):
            if turn_backed is not None and (obligation.callback_kind in _TURN_BACKED_KINDS) != turn_backed:
                continue
            try:
                event = _parse_recovery_event(obligation)
                await self._run_obligation(
                    obligation,
                    room=self.room_for_id(obligation.room_id),
                    event=event,
                )
            except asyncio.CancelledError:
                raise
            except _DispatchObligationCorruptionError:
                raise
            except Exception:
                logger.exception(
                    "dispatch_obligation_recovery_failed",
                    source_event_id=obligation.source_event_id,
                    callback_kind=obligation.callback_kind.value,
                    room_id=obligation.room_id,
                )

    async def _run_obligation(
        self,
        obligation: _DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> None:
        if not await self._claim(obligation.key):
            return
        try:
            if not await asyncio.to_thread(
                self.store.has_pending,
                obligation.source_event_id,
                obligation.callback_kind,
            ):
                return
            if await self._settle_from_turn_store_if_owned(obligation):
                return
            callback = self.callbacks.get(obligation.callback_kind)
            if callback is None:
                msg = f"No callback registered for {obligation.callback_kind.value!r}"
                raise RuntimeError(msg)
            callback_result = await callback(room, event)
            await self._settle_callback_result(obligation, callback_result)
        finally:
            await self._release(obligation.key)

    async def _settle_from_turn_store_if_owned(self, obligation: _DispatchObligation) -> bool:
        if obligation.callback_kind not in _TURN_BACKED_KINDS:
            return False
        if not await asyncio.to_thread(self.turn_is_terminal, obligation.source_event_id):
            return False
        await _run_store_settlement(
            self.store.settle_from_turn_store,
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
        if result is _DispatchCallbackResult.DEFERRED:
            return
        if await self._settle_from_turn_store_if_owned(obligation):
            return
        if obligation.callback_kind is DispatchCallbackKind.INVITE:
            await _run_store_settlement(self.store.discard_pending, obligation.key)
            return
        outcome = (
            _DispatchTerminalOutcome.SUCCEEDED
            if result is _DispatchCallbackResult.SUCCEEDED
            else _DispatchTerminalOutcome.INTENTIONALLY_IGNORED
        )
        await _run_store_settlement(self.store.settle, obligation.key, outcome)

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
    """Durably accept one nio callback before scheduling background execution."""

    runner: DispatchObligationRunner
    callback_kind: DispatchCallbackKind
    owner: object

    async def __call__(self, room: nio.MatrixRoom, event: _DispatchEvent) -> None:
        """Persist one callback obligation before scheduling its execution."""
        obligation = await self.runner.persist(room, event, self.callback_kind)
        if obligation is None:
            return
        create_background_task(
            self._run(obligation, room=room, event=event),
            owner=self.owner,
        )

    async def _run(
        self,
        obligation: _DispatchObligation,
        *,
        room: nio.MatrixRoom,
        event: _DispatchEvent,
    ) -> None:
        try:
            await self.runner.run_persisted(obligation, room=room, event=event)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(
                "dispatch_obligation_callback_failed",
                source_event_id=obligation.source_event_id,
                callback_kind=obligation.callback_kind.value,
                room_id=obligation.room_id,
            )


def _is_tool_approval_response(event: nio.Event) -> TypeIs[nio.UnknownEvent]:
    return isinstance(event, nio.UnknownEvent) and event.type == _TOOL_APPROVAL_RESPONSE_EVENT_TYPE
