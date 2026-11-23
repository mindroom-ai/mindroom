"""Primary-runtime SQLite store for reactive triggers, cursors, checkpoints, and firings."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from mindroom.file_locks import advisory_file_lock
from mindroom.logging_config import get_logger
from mindroom.reactive_triggers.models import (
    ExternalEventSourceSpec,
    FiringRecord,
    ReactiveTriggerRecord,
    RoomCursor,
    RoomMessageSourceSpec,
    SourceType,
    ThreadCheckpoint,
    ThreadTagSourceSpec,
    TriggerEvent,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_SCHEMA_VERSION = 1
_DB_FILENAME = "reactive_triggers.db"
_TRIGGER_ID_LOCK_FILENAME = ".trigger_id_creation.lock"
_FINISHED_FIRING_STATUSES = ("triaged_no", "delivered", "failed", "skipped")
_ONE_HOUR_SECONDS = 3600.0
_MAX_BACKOFF_SECONDS = 300.0
_BASE_BACKOFF_SECONDS = 5.0

ClaimResult = Literal["claimed", "disabled", "expired", "cooldown", "rate_limited", "missing", "rule_replaced"]
ClaimCheckResult = Literal["ok", "disabled", "expired", "cooldown", "rate_limited", "missing", "rule_replaced"]
RetryResult = Literal["retrying", "failed"]

_CREATION_THREAD_LOCKS_GUARD = threading.Lock()
_CREATION_THREAD_LOCKS: dict[Path, threading.Lock] = {}


class ReactiveTriggerStoreError(RuntimeError):
    """Base error raised when the reactive trigger store cannot read or persist state."""


class ReactiveTriggerDuplicateError(ReactiveTriggerStoreError):
    """Raised when creating a trigger whose trigger_id already exists."""


class ReactiveTriggerLimitError(ReactiveTriggerStoreError):
    """Raised when creating a trigger would exceed the owner's configured quota."""


@contextmanager
def trigger_id_creation_lock(runtime_paths: RuntimePaths) -> Iterator[None]:
    """Serialize cross-store trigger_id reservation across direct and reactive trigger stores."""
    control_root = runtime_paths.control_state_root
    if control_root is None:
        msg = "Trigger ID creation lock requires primary control state"
        raise ReactiveTriggerStoreError(msg)
    lock_path = (control_root / _TRIGGER_ID_LOCK_FILENAME).resolve()
    with _CREATION_THREAD_LOCKS_GUARD:
        thread_lock = _CREATION_THREAD_LOCKS.setdefault(lock_path, threading.Lock())
    with thread_lock, advisory_file_lock(lock_path, exclusive=True):
        yield


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reactive_triggers (
        trigger_id TEXT PRIMARY KEY,
        rule_uid TEXT NOT NULL,
        owner_user_id TEXT NOT NULL,
        source_type TEXT NOT NULL,
        source_room_id TEXT,
        source_id TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        disabled_reason TEXT,
        once INTEGER NOT NULL DEFAULT 0,
        expires_at REAL,
        max_fires_per_hour INTEGER NOT NULL,
        cooldown_seconds INTEGER NOT NULL DEFAULT 0,
        fire_count INTEGER NOT NULL DEFAULT 0,
        last_fired_at REAL,
        spec_json TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS room_cursors (
        trigger_id TEXT PRIMARY KEY REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
        created_ts INTEGER NOT NULL,
        logical_event_id TEXT NOT NULL,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS thread_checkpoints (
        trigger_id TEXT PRIMARY KEY REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
        tag_present INTEGER NOT NULL,
        state_event_id TEXT,
        updated_at REAL NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trigger_firings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trigger_id TEXT NOT NULL REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
        rule_uid TEXT NOT NULL,
        dedupe_key TEXT NOT NULL,
        status TEXT NOT NULL,
        event_json TEXT NOT NULL,
        triage_reason TEXT,
        matrix_event_id TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        next_retry_at REAL,
        error_message TEXT,
        claimed_at REAL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        UNIQUE(trigger_id, dedupe_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trigger_thread_keys (
        trigger_id TEXT NOT NULL REFERENCES reactive_triggers(trigger_id) ON DELETE CASCADE,
        thread_key TEXT NOT NULL,
        thread_root_id TEXT NOT NULL,
        updated_at REAL NOT NULL,
        PRIMARY KEY (trigger_id, thread_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reactive_triggers_owner ON reactive_triggers(owner_user_id)",
    "CREATE INDEX IF NOT EXISTS idx_reactive_triggers_source_type ON reactive_triggers(source_type, enabled)",
    "CREATE INDEX IF NOT EXISTS idx_reactive_triggers_source_room ON reactive_triggers(source_room_id)",
    "CREATE INDEX IF NOT EXISTS idx_reactive_triggers_source_id ON reactive_triggers(source_id)",
    "CREATE INDEX IF NOT EXISTS idx_trigger_firings_due ON trigger_firings(status, next_retry_at, id)",
    (
        "CREATE INDEX IF NOT EXISTS idx_trigger_firings_hourly "
        "ON trigger_firings(trigger_id, status, claimed_at, updated_at)"
    ),
    "CREATE INDEX IF NOT EXISTS idx_trigger_firings_updated ON trigger_firings(updated_at)",
)


class ReactiveTriggerStore:
    """SQLite-backed store for reactive triggers under primary control state."""

    def __init__(self, runtime_paths: RuntimePaths) -> None:
        """Bind the store to primary control state and initialize the SQLite schema."""
        control_root = runtime_paths.control_state_root
        if control_root is None:
            msg = "Reactive trigger store requires primary control state"
            raise ReactiveTriggerStoreError(msg)
        self._db_path = control_root / _DB_FILENAME
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    @property
    def db_path(self) -> Path:
        """Return the on-disk SQLite database path."""
        return self._db_path

    def create_trigger(
        self,
        record: ReactiveTriggerRecord,
        *,
        initial_cursor: RoomCursor | None = None,
        initial_checkpoint: ThreadCheckpoint | None = None,
        max_per_owner: int | None = None,
    ) -> ReactiveTriggerRecord:
        """Atomically enforce quota, insert a trigger, and optionally initialize cursor or checkpoint."""
        source_room_id, source_id = _source_index_fields(record)
        spec_json = record.model_dump_json()

        with self._write_transaction() as conn:
            existing = conn.execute(
                "SELECT 1 FROM reactive_triggers WHERE trigger_id = ?",
                (record.trigger_id,),
            ).fetchone()
            if existing is not None:
                msg = f"Reactive trigger already exists: {record.trigger_id}"
                raise ReactiveTriggerDuplicateError(msg)

            if max_per_owner is not None:
                owner_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM reactive_triggers WHERE owner_user_id = ?",
                        (record.owner_user_id,),
                    ).fetchone()[0],
                )
                if owner_count >= max_per_owner:
                    msg = f"Owner {record.owner_user_id} reached reactive trigger limit ({max_per_owner})"
                    raise ReactiveTriggerLimitError(msg)

            conn.execute(
                """
                INSERT INTO reactive_triggers (
                    trigger_id, rule_uid, owner_user_id, source_type, source_room_id, source_id,
                    enabled, disabled_reason, once, expires_at, max_fires_per_hour, cooldown_seconds,
                    fire_count, last_fired_at, spec_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trigger_id,
                    record.rule_uid,
                    record.owner_user_id,
                    record.source.source_type,
                    source_room_id,
                    source_id,
                    1 if record.enabled else 0,
                    record.disabled_reason,
                    1 if record.once else 0,
                    record.expires_at,
                    record.max_fires_per_hour,
                    record.cooldown_seconds,
                    record.fire_count,
                    record.last_fired_at,
                    spec_json,
                    record.created_at,
                    record.updated_at,
                ),
            )

            if initial_cursor is not None:
                _upsert_room_cursor(conn, record.trigger_id, initial_cursor, updated_at=record.updated_at)

            if initial_checkpoint is not None:
                _upsert_thread_checkpoint(
                    conn,
                    record.trigger_id,
                    tag_present=initial_checkpoint.tag_present,
                    state_event_id=initial_checkpoint.state_event_id,
                    updated_at=initial_checkpoint.updated_at,
                )

        return record

    def get_trigger(self, trigger_id: str) -> ReactiveTriggerRecord | None:
        """Return one reactive trigger by ID, or None if not found."""
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM reactive_triggers WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def list_triggers(
        self,
        *,
        owner_user_id: str | None = None,
        include_disabled: bool = True,
        source_type: SourceType | None = None,
        source_room_id: str | None = None,
        source_id: str | None = None,
    ) -> list[ReactiveTriggerRecord]:
        """List reactive triggers matching optional filters."""
        clauses: list[str] = []
        params: list[object] = []

        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
        if not include_disabled:
            clauses.append("enabled = 1")
        if source_type is not None:
            clauses.append("source_type = ?")
            params.append(source_type)
        if source_room_id is not None:
            clauses.append("source_room_id = ?")
            params.append(source_room_id)
        if source_id is not None:
            clauses.append("source_id = ?")
            params.append(source_id)

        where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM reactive_triggers{where_sql} ORDER BY created_at ASC, trigger_id ASC"  # noqa: S608

        with self._read_connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_record_from_row(row) for row in rows]

    def set_enabled(
        self,
        trigger_id: str,
        enabled: bool,
        *,
        expected_rule_uid: str | None = None,
    ) -> ReactiveTriggerRecord | None:
        """Enable or disable a reactive trigger and return the updated record, or None if missing/replaced."""
        now = time.time()
        disabled_reason = None if enabled else "manual"
        with self._write_transaction() as conn:
            if expected_rule_uid is not None:
                cursor = conn.execute(
                    """
                    UPDATE reactive_triggers
                    SET enabled = ?, disabled_reason = ?, updated_at = ?
                    WHERE trigger_id = ? AND rule_uid = ?
                    """,
                    (1 if enabled else 0, disabled_reason, now, trigger_id, expected_rule_uid),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE reactive_triggers
                    SET enabled = ?, disabled_reason = ?, updated_at = ?
                    WHERE trigger_id = ?
                    """,
                    (1 if enabled else 0, disabled_reason, now, trigger_id),
                )
            if cursor.rowcount == 0:
                return None
            row = conn.execute(
                "SELECT * FROM reactive_triggers WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
        return _record_from_row(row) if row is not None else None

    def delete_trigger(
        self,
        trigger_id: str,
        *,
        expected_rule_uid: str | None = None,
    ) -> bool:
        """Delete a reactive trigger and cascade-delete its cursors, checkpoints, firings, and thread keys."""
        with self._write_transaction() as conn:
            if expected_rule_uid is not None:
                cursor = conn.execute(
                    "DELETE FROM reactive_triggers WHERE trigger_id = ? AND rule_uid = ?",
                    (trigger_id, expected_rule_uid),
                )
            else:
                cursor = conn.execute(
                    "DELETE FROM reactive_triggers WHERE trigger_id = ?",
                    (trigger_id,),
                )
            return cursor.rowcount > 0

    def get_room_cursor(self, trigger_id: str) -> RoomCursor | None:
        """Return the persisted room journal cursor for a trigger, or None."""
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT created_ts, logical_event_id FROM room_cursors WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
        if row is None:
            return None
        return RoomCursor(created_ts=int(row["created_ts"]), logical_event_id=str(row["logical_event_id"]))

    def advance_room_cursor(
        self,
        trigger_id: str,
        cursor: RoomCursor,
        *,
        expected_rule_uid: str | None = None,
    ) -> bool:
        """Upsert the room journal cursor for an existing trigger; return False if expected_rule_uid mismatches."""
        now = time.time()
        with self._write_transaction() as conn:
            if expected_rule_uid is not None:
                live_uid = _live_rule_uid(conn, trigger_id)
                if live_uid != expected_rule_uid:
                    return False
            try:
                _upsert_room_cursor(conn, trigger_id, cursor, updated_at=now)
            except sqlite3.IntegrityError as exc:
                msg = f"Cannot advance cursor for unknown trigger: {trigger_id}"
                raise ReactiveTriggerStoreError(msg) from exc
            return True

    def get_thread_checkpoint(self, trigger_id: str) -> ThreadCheckpoint | None:
        """Return the persisted thread tag checkpoint for a trigger, or None."""
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT tag_present, state_event_id, updated_at FROM thread_checkpoints WHERE trigger_id = ?",
                (trigger_id,),
            ).fetchone()
        if row is None:
            return None
        return ThreadCheckpoint(
            tag_present=bool(row["tag_present"]),
            state_event_id=row["state_event_id"],
            updated_at=float(row["updated_at"]),
        )

    def update_thread_checkpoint_and_enqueue(
        self,
        trigger_id: str,
        *,
        tag_present: bool,
        state_event_id: str | None,
        firing_dedupe_key: str | None = None,
        event: TriggerEvent | None = None,
        expected_rule_uid: str | None = None,
    ) -> bool:
        """Atomically update a thread checkpoint and optionally enqueue a deduplicated firing."""
        if (firing_dedupe_key is None) != (event is None):
            msg = "firing_dedupe_key and event must either both be provided or both be None"
            raise ValueError(msg)

        now = time.time()
        with self._write_transaction() as conn:
            live_uid = _live_rule_uid(conn, trigger_id)
            if expected_rule_uid is not None:
                if live_uid != expected_rule_uid:
                    return False
            elif live_uid is None:
                msg = f"Cannot update checkpoint for unknown trigger: {trigger_id}"
                raise ReactiveTriggerStoreError(msg)
            assert live_uid is not None

            try:
                _upsert_thread_checkpoint(
                    conn,
                    trigger_id,
                    tag_present=tag_present,
                    state_event_id=state_event_id,
                    updated_at=now,
                )
                if firing_dedupe_key is None or event is None:
                    return False
                return _insert_pending_firing(
                    conn,
                    trigger_id=trigger_id,
                    rule_uid=live_uid,
                    dedupe_key=firing_dedupe_key,
                    event=event,
                    now=now,
                )
            except sqlite3.IntegrityError as exc:
                msg = f"Cannot update checkpoint for unknown trigger: {trigger_id}"
                raise ReactiveTriggerStoreError(msg) from exc

    def enqueue_firing(
        self,
        trigger_id: str,
        dedupe_key: str,
        event: TriggerEvent,
        *,
        advance_cursor: RoomCursor | None = None,
        expected_rule_uid: str | None = None,
    ) -> bool:
        """Enqueue a pending firing with idempotent (trigger_id, dedupe_key) deduplication."""
        now = time.time()
        with self._write_transaction() as conn:
            live_uid = _live_rule_uid(conn, trigger_id)
            if expected_rule_uid is not None:
                if live_uid != expected_rule_uid:
                    return False
            elif live_uid is None:
                msg = f"Cannot enqueue firing for unknown trigger: {trigger_id}"
                raise ReactiveTriggerStoreError(msg)
            assert live_uid is not None

            try:
                inserted = _insert_pending_firing(
                    conn,
                    trigger_id=trigger_id,
                    rule_uid=live_uid,
                    dedupe_key=dedupe_key,
                    event=event,
                    now=now,
                )
                if advance_cursor is not None:
                    _upsert_room_cursor(conn, trigger_id, advance_cursor, updated_at=now)
            except sqlite3.IntegrityError as exc:
                msg = f"Cannot enqueue firing for unknown trigger: {trigger_id}"
                raise ReactiveTriggerStoreError(msg) from exc
            else:
                return inserted

    def get_firing(self, firing_id: int) -> FiringRecord | None:
        """Return one firing record by ID, or None if not found."""
        with self._read_connection() as conn:
            row = conn.execute(
                "SELECT * FROM trigger_firings WHERE id = ?",
                (firing_id,),
            ).fetchone()
        return _firing_from_row(row) if row is not None else None

    def mark_triaged_no(self, firing_id: int, reason: str | None = None) -> bool:
        """Mark a pending firing as rejected by triage without consuming once=True or rate limits."""
        now = time.time()
        with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE trigger_firings
                SET status = 'triaged_no',
                    triage_reason = ?,
                    next_retry_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                (reason, now, firing_id),
            )
            return cursor.rowcount > 0

    def can_claim_now(
        self,
        firing_id: int,
        *,
        now: float | None = None,
        policy_max_fires_per_hour: int | None = None,
    ) -> ClaimCheckResult:
        """Read-only check of whether a pending firing currently passes claim guards."""
        current_time = time.time() if now is None else now
        with self._read_connection() as conn:
            firing = conn.execute(
                "SELECT id, trigger_id, rule_uid, status FROM trigger_firings WHERE id = ?",
                (firing_id,),
            ).fetchone()
            if firing is None or str(firing["status"]) != "pending":
                return "missing"

            trigger_id = str(firing["trigger_id"])
            trigger = conn.execute(
                """
                SELECT rule_uid, enabled, once, expires_at, max_fires_per_hour, cooldown_seconds, last_fired_at
                FROM reactive_triggers
                WHERE trigger_id = ?
                """,
                (trigger_id,),
            ).fetchone()
            if trigger is None:
                return "missing"
            if str(trigger["rule_uid"]) != str(firing["rule_uid"]):
                return "rule_replaced"

            blocked_reason = _evaluate_claim_guards(
                conn,
                trigger_id,
                trigger,
                current_time=current_time,
                policy_max_fires_per_hour=policy_max_fires_per_hour,
            )
            return blocked_reason if blocked_reason is not None else "ok"

    def claim_firing(
        self,
        firing_id: int,
        *,
        now: float | None = None,
        triage_reason: str | None = None,
        policy_max_fires_per_hour: int | None = None,
    ) -> ClaimResult:
        """Atomically verify trigger guards and claim a pending firing for delivery."""
        current_time = time.time() if now is None else now
        with self._write_transaction() as conn:
            firing = conn.execute(
                "SELECT id, trigger_id, rule_uid, status FROM trigger_firings WHERE id = ?",
                (firing_id,),
            ).fetchone()
            if firing is None or str(firing["status"]) != "pending":
                return "missing"

            trigger_id = str(firing["trigger_id"])
            trigger = conn.execute(
                """
                SELECT rule_uid, enabled, disabled_reason, once, expires_at,
                       max_fires_per_hour, cooldown_seconds, last_fired_at
                FROM reactive_triggers
                WHERE trigger_id = ?
                """,
                (trigger_id,),
            ).fetchone()
            if trigger is None:
                return "missing"

            if str(trigger["rule_uid"]) != str(firing["rule_uid"]):
                conn.execute(
                    """
                    UPDATE trigger_firings
                    SET status = 'skipped',
                        triage_reason = COALESCE(?, triage_reason),
                        error_message = 'rule_replaced',
                        next_retry_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (triage_reason, current_time, firing_id),
                )
                return "rule_replaced"

            blocked_reason = _evaluate_claim_guards(
                conn,
                trigger_id,
                trigger,
                current_time=current_time,
                policy_max_fires_per_hour=policy_max_fires_per_hour,
            )
            if blocked_reason is not None:
                conn.execute(
                    """
                    UPDATE trigger_firings
                    SET status = 'skipped',
                        triage_reason = COALESCE(?, triage_reason),
                        error_message = ?,
                        next_retry_at = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (triage_reason, blocked_reason, current_time, firing_id),
                )
                return blocked_reason

            conn.execute(
                """
                UPDATE trigger_firings
                SET status = 'claimed',
                    triage_reason = COALESCE(?, triage_reason),
                    claimed_at = ?,
                    attempt_count = 0,
                    next_retry_at = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (triage_reason, current_time, current_time, firing_id),
            )
            if bool(trigger["once"]):
                new_enabled = 0
                new_disabled_reason: str | None = "once_consumed"
            else:
                new_enabled = int(trigger["enabled"])
                new_disabled_reason = trigger["disabled_reason"]
            conn.execute(
                """
                UPDATE reactive_triggers
                SET fire_count = fire_count + 1,
                    last_fired_at = ?,
                    enabled = ?,
                    disabled_reason = ?,
                    updated_at = ?
                WHERE trigger_id = ?
                """,
                (current_time, new_enabled, new_disabled_reason, current_time, trigger_id),
            )
            return "claimed"

    def _retry_firing(
        self,
        firing_id: int,
        error: str,
        *,
        expected_status: Literal["pending", "claimed"],
        verb: str,
        now: float | None = None,
        max_attempts: int = 5,
    ) -> RetryResult:
        current_time = time.time() if now is None else now
        with self._write_transaction() as conn:
            row = conn.execute(
                "SELECT status, attempt_count FROM trigger_firings WHERE id = ?",
                (firing_id,),
            ).fetchone()
            if row is None:
                msg = f"Reactive trigger firing not found: {firing_id}"
                raise ReactiveTriggerStoreError(msg)
            status = str(row["status"])
            if status != expected_status:
                msg = f"Cannot {verb} firing {firing_id} in status {status!r}; expected {expected_status!r}"
                raise ReactiveTriggerStoreError(msg)

            attempt = int(row["attempt_count"]) + 1
            result: RetryResult
            if attempt >= max_attempts:
                next_status, next_retry_at, result = "failed", None, "failed"
            else:
                backoff_seconds = min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                next_status, next_retry_at, result = expected_status, current_time + backoff_seconds, "retrying"

            conn.execute(
                """
                UPDATE trigger_firings
                SET status = ?,
                    attempt_count = ?,
                    next_retry_at = ?,
                    error_message = ?,
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (next_status, attempt, next_retry_at, error, current_time, firing_id, expected_status),
            )
            return result

    def defer_pending_firing(
        self,
        firing_id: int,
        error: str,
        *,
        now: float | None = None,
        max_attempts: int = 5,
    ) -> RetryResult:
        """Back off a pending firing on transient pre-triage unavailability without consuming once=True."""
        return self._retry_firing(
            firing_id,
            error,
            expected_status="pending",
            verb="defer",
            now=now,
            max_attempts=max_attempts,
        )

    def mark_delivered(
        self,
        firing_id: int,
        matrix_event_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Mark a claimed firing as delivered with the posted Matrix event ID."""
        current_time = time.time() if now is None else now
        with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE trigger_firings
                SET status = 'delivered',
                    matrix_event_id = ?,
                    next_retry_at = NULL,
                    error_message = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'claimed'
                """,
                (matrix_event_id, current_time, firing_id),
            )
            return cursor.rowcount > 0

    def mark_delivery_retry(
        self,
        firing_id: int,
        error: str,
        *,
        now: float | None = None,
        max_attempts: int = 5,
    ) -> RetryResult:
        """Record a transient delivery error with exponential backoff, or mark failed once exhausted."""
        return self._retry_firing(
            firing_id,
            error,
            expected_status="claimed",
            verb="retry",
            now=now,
            max_attempts=max_attempts,
        )

    def mark_skipped(self, firing_id: int, reason: str, *, now: float | None = None) -> bool:
        """Mark a pending or claimed firing as skipped with an explanatory reason."""
        current_time = time.time() if now is None else now
        with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE trigger_firings
                SET status = 'skipped',
                    error_message = ?,
                    next_retry_at = NULL,
                    updated_at = ?
                WHERE id = ? AND status IN ('pending', 'claimed')
                """,
                (reason, current_time, firing_id),
            )
            return cursor.rowcount > 0

    def list_due_firings(self, *, now: float | None = None, limit: int = 50) -> list[FiringRecord]:
        """Return due pending and claimed firings ordered by id ASC, isolating corrupt rows."""
        current_time = time.time() if now is None else now
        with self._read_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM trigger_firings
                WHERE status IN ('pending', 'claimed')
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY id ASC
                LIMIT ?
                """,
                (current_time, limit),
            ).fetchall()

        firings: list[FiringRecord] = []
        corrupt_ids: list[int] = []
        for row in rows:
            try:
                firings.append(_firing_from_row(row))
            except (ValidationError, ValueError, KeyError, TypeError):
                firing_id = int(row["id"])
                logger.exception(
                    "Corrupt reactive trigger firing row; marking failed",
                    firing_id=firing_id,
                    trigger_id=row["trigger_id"],
                )
                corrupt_ids.append(firing_id)

        if corrupt_ids:
            with self._write_transaction() as conn:
                for firing_id in corrupt_ids:
                    conn.execute(
                        """
                        UPDATE trigger_firings
                        SET status = 'failed',
                            error_message = 'corrupt_event_json',
                            next_retry_at = NULL,
                            updated_at = ?
                        WHERE id = ? AND status IN ('pending', 'claimed')
                        """,
                        (current_time, firing_id),
                    )
        return firings

    def get_thread_key_binding(
        self,
        trigger_id: str,
        thread_key: str,
        *,
        ttl_seconds: float = 7 * 86400,
        now: float | None = None,
    ) -> str | None:
        """Return the bound Matrix thread root ID for a thread_key if present and unexpired."""
        current_time = time.time() if now is None else now
        with self._read_connection() as conn:
            row = conn.execute(
                """
                SELECT thread_root_id, updated_at
                FROM trigger_thread_keys
                WHERE trigger_id = ? AND thread_key = ?
                """,
                (trigger_id, thread_key),
            ).fetchone()
        if row is None:
            return None
        if current_time - float(row["updated_at"]) >= ttl_seconds:
            return None
        return str(row["thread_root_id"])

    def save_thread_key_binding(
        self,
        trigger_id: str,
        thread_key: str,
        thread_root_id: str,
        *,
        now: float | None = None,
        expected_rule_uid: str | None = None,
    ) -> bool:
        """Persist or refresh the Matrix thread root ID bound to (trigger_id, thread_key)."""
        current_time = time.time() if now is None else now
        with self._write_transaction() as conn:
            if expected_rule_uid is not None:
                live_uid = _live_rule_uid(conn, trigger_id)
                if live_uid != expected_rule_uid:
                    return False
            try:
                conn.execute(
                    """
                    INSERT INTO trigger_thread_keys (trigger_id, thread_key, thread_root_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(trigger_id, thread_key) DO UPDATE SET
                        thread_root_id = excluded.thread_root_id,
                        updated_at = excluded.updated_at
                    """,
                    (trigger_id, thread_key, thread_root_id, current_time),
                )
            except sqlite3.IntegrityError as exc:
                msg = f"Cannot save thread key binding for unknown trigger: {trigger_id}"
                raise ReactiveTriggerStoreError(msg) from exc
            return True

    def prune_old_firings(
        self,
        *,
        retention_seconds: float = 30 * 86400,
        now: float | None = None,
    ) -> int:
        """Delete finished firing rows older than retention_seconds and return the deleted count."""
        current_time = time.time() if now is None else now
        cutoff = current_time - retention_seconds
        with self._write_transaction() as conn:
            cursor = conn.execute(
                """
                DELETE FROM trigger_firings
                WHERE status IN (?, ?, ?, ?)
                  AND updated_at < ?
                """,
                (*_FINISHED_FIRING_STATUSES, cutoff),
            )
            return cursor.rowcount

    def _initialize_database(self) -> None:
        with self._write_transaction() as conn:
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_SCHEMA_VERSION,))
            elif int(row["version"]) != _SCHEMA_VERSION:
                msg = f"Unsupported reactive triggers schema version: {row['version']}"
                raise ReactiveTriggerStoreError(msg)

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        with closing(_connect(self._db_path)) as conn:
            yield conn

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(_connect(self._db_path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _live_rule_uid(conn: sqlite3.Connection, trigger_id: str) -> str | None:
    row = conn.execute(
        "SELECT rule_uid FROM reactive_triggers WHERE trigger_id = ?",
        (trigger_id,),
    ).fetchone()
    return str(row["rule_uid"]) if row is not None else None


def _source_index_fields(record: ReactiveTriggerRecord) -> tuple[str | None, str | None]:
    source = record.source
    if isinstance(source, (RoomMessageSourceSpec, ThreadTagSourceSpec)):
        return source.room_id, None
    if isinstance(source, ExternalEventSourceSpec):
        return None, source.source_id
    return None, None


def _upsert_room_cursor(
    conn: sqlite3.Connection,
    trigger_id: str,
    cursor: RoomCursor,
    *,
    updated_at: float,
) -> None:
    conn.execute(
        """
        INSERT INTO room_cursors (trigger_id, created_ts, logical_event_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(trigger_id) DO UPDATE SET
            created_ts = excluded.created_ts,
            logical_event_id = excluded.logical_event_id,
            updated_at = excluded.updated_at
        WHERE excluded.created_ts > room_cursors.created_ts
           OR (
               excluded.created_ts = room_cursors.created_ts
               AND excluded.logical_event_id > room_cursors.logical_event_id
           )
        """,
        (trigger_id, cursor.created_ts, cursor.logical_event_id, updated_at),
    )


def _upsert_thread_checkpoint(
    conn: sqlite3.Connection,
    trigger_id: str,
    *,
    tag_present: bool,
    state_event_id: str | None,
    updated_at: float,
) -> None:
    conn.execute(
        """
        INSERT INTO thread_checkpoints (trigger_id, tag_present, state_event_id, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(trigger_id) DO UPDATE SET
            tag_present = excluded.tag_present,
            state_event_id = excluded.state_event_id,
            updated_at = excluded.updated_at
        """,
        (trigger_id, 1 if tag_present else 0, state_event_id, updated_at),
    )


def _insert_pending_firing(
    conn: sqlite3.Connection,
    *,
    trigger_id: str,
    rule_uid: str,
    dedupe_key: str,
    event: TriggerEvent,
    now: float,
) -> bool:
    cursor = conn.execute(
        """
        INSERT INTO trigger_firings (
            trigger_id, rule_uid, dedupe_key, status, event_json, attempt_count, created_at, updated_at
        ) VALUES (?, ?, ?, 'pending', ?, 0, ?, ?)
        ON CONFLICT(trigger_id, dedupe_key) DO NOTHING
        """,
        (trigger_id, rule_uid, dedupe_key, event.model_dump_json(), now, now),
    )
    return cursor.rowcount > 0


def _evaluate_claim_guards(
    conn: sqlite3.Connection,
    trigger_id: str,
    trigger: sqlite3.Row,
    *,
    current_time: float,
    policy_max_fires_per_hour: int | None = None,
) -> Literal["disabled", "expired", "cooldown", "rate_limited"] | None:
    if not bool(trigger["enabled"]):
        return "disabled"

    expires_at = trigger["expires_at"]
    if expires_at is not None and current_time >= float(expires_at):
        return "expired"

    cooldown_seconds = int(trigger["cooldown_seconds"])
    last_fired_at = trigger["last_fired_at"]
    if cooldown_seconds > 0 and last_fired_at is not None and current_time - float(last_fired_at) < cooldown_seconds:
        return "cooldown"

    window_start = current_time - _ONE_HOUR_SECONDS
    hourly_count = int(
        conn.execute(
            """
            SELECT COUNT(*) FROM trigger_firings
            WHERE trigger_id = ?
              AND status IN ('claimed', 'delivered')
              AND COALESCE(claimed_at, updated_at) > ?
            """,
            (trigger_id, window_start),
        ).fetchone()[0],
    )
    effective_cap = int(trigger["max_fires_per_hour"])
    if policy_max_fires_per_hour is not None:
        effective_cap = min(effective_cap, policy_max_fires_per_hour)
    if hourly_count >= effective_cap:
        return "rate_limited"

    return None


def _record_from_row(row: sqlite3.Row) -> ReactiveTriggerRecord:
    record = ReactiveTriggerRecord.model_validate_json(str(row["spec_json"]))
    return record.model_copy(
        update={
            "rule_uid": str(row["rule_uid"]),
            "enabled": bool(row["enabled"]),
            "disabled_reason": row["disabled_reason"],
            "fire_count": int(row["fire_count"]),
            "last_fired_at": float(row["last_fired_at"]) if row["last_fired_at"] is not None else None,
            "updated_at": float(row["updated_at"]),
        },
    )


def _firing_from_row(row: sqlite3.Row) -> FiringRecord:
    return FiringRecord(
        id=int(row["id"]),
        trigger_id=str(row["trigger_id"]),
        rule_uid=str(row["rule_uid"]),
        dedupe_key=str(row["dedupe_key"]),
        status=row["status"],
        event=TriggerEvent.model_validate_json(str(row["event_json"])),
        triage_reason=row["triage_reason"],
        matrix_event_id=row["matrix_event_id"],
        attempt_count=int(row["attempt_count"]),
        next_retry_at=float(row["next_retry_at"]) if row["next_retry_at"] is not None else None,
        error_message=row["error_message"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )
