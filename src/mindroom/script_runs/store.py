"""SQLite-backed durable state for background Python script runs."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from mindroom.script_runs.models import (
    ScriptCallClaim,
    ScriptCallRecord,
    ScriptCallState,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


_MAX_RECEIPT_BYTES = 64 * 1024
_MAX_RUN_ERROR_BYTES = 64 * 1024
_CONTROL_STATE_UNAVAILABLE = "Background script control state is unavailable."
_INVALID_CAPABILITY = "Background script capability is invalid."
_REVOKED_CAPABILITY = "Background script capability has been revoked."
_CALLS_NOT_ACCEPTED = "Background script run cannot accept new calls."
_GRANT_NOT_GRANTED = "Requested script tool grant was not granted at launch."
_RECEIPT_NOT_SERIALIZABLE = "Script call receipt must be JSON serializable."
_TERMINAL_RUN_STATES = frozenset(
    {
        ScriptRunState.EXITED,
        ScriptRunState.FAILED,
        ScriptRunState.CANCELLED,
        ScriptRunState.INTERRUPTED,
    },
)
_TERMINAL_CALL_STATES = frozenset(
    {
        ScriptCallState.COMPLETED,
        ScriptCallState.FAILED,
        ScriptCallState.INDETERMINATE,
    },
)
_RUN_TRANSITIONS = {
    ScriptRunState.STARTING: frozenset(
        {
            ScriptRunState.RUNNING,
            ScriptRunState.FAILED,
            ScriptRunState.CANCELLED,
            ScriptRunState.INTERRUPTED,
        },
    ),
    ScriptRunState.RUNNING: frozenset(
        {
            ScriptRunState.EXITED,
            ScriptRunState.FAILED,
            ScriptRunState.CANCELLED,
            ScriptRunState.INTERRUPTED,
        },
    ),
    ScriptRunState.EXITED: frozenset(),
    ScriptRunState.FAILED: frozenset(),
    ScriptRunState.CANCELLED: frozenset(),
    ScriptRunState.INTERRUPTED: frozenset(),
}


class ScriptRunStoreError(ValueError):
    """Raised when durable background-script state cannot be used safely."""


class ScriptRunNotFoundError(ScriptRunStoreError):
    """Raised when a requested script run is absent."""

    def __init__(self, run_id: str) -> None:
        super().__init__(f"Script run '{run_id}' was not found.")


class ScriptCallNotFoundError(ScriptRunStoreError):
    """Raised when a requested script call is absent."""

    def __init__(self, call_id: str) -> None:
        super().__init__(f"Script call '{call_id}' was not found.")


class ScriptCallConflictError(ScriptRunStoreError):
    """Raised when a stable call ID is reused with different immutable inputs."""


class ScriptCallRateLimitError(ScriptRunStoreError):
    """Raised when a new logical call would exceed its run's durable rate limit."""


class ScriptCapabilityError(ScriptRunStoreError):
    """Raised when a run capability is invalid or cannot accept calls."""


def mint_script_capability() -> tuple[str, str]:
    """Create a bearer capability and its durable SHA-256 digest."""
    token = secrets.token_urlsafe(32)
    return token, _capability_hash(token)


class ScriptRunStore:
    """Persist primary-only run and call state with atomic SQLite transitions."""

    def __init__(self, runtime_paths: RuntimePaths) -> None:
        control_root = runtime_paths.control_state_root
        if control_root is None:
            raise ScriptRunStoreError(_CONTROL_STATE_UNAVAILABLE)
        self.database_path = control_root / "script_runs" / "script_runs.sqlite3"
        self.storage_root = runtime_paths.storage_root.expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_database()

    def create_run(self, run: ScriptRunRecord) -> ScriptRunRecord:  # noqa: Vulture
        """Atomically store a new starting run before any worker action."""
        if run.state is not ScriptRunState.STARTING:
            msg = "A new script run must begin in the starting state."
            raise ScriptRunStoreError(msg)
        if run.max_tool_calls_per_minute <= 0 or run.max_runtime_seconds <= 0:
            msg = "Background script limits must be positive."
            raise ScriptRunStoreError(msg)
        with self._write_transaction() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO script_runs (
                        run_id, agent_name, owner_user_id, room_id, thread_root_event_id,
                        execution_identity_json, source_digest, grants_json, token_hash, preapprove_launch_grants,
                        worker_key, worker_id,
                        snapshot_locator, name, local_unsafe,
                        max_tool_calls_per_minute, max_runtime_seconds, state, created_at,
                        started_at, finished_at, exit_code, error, cancel_requested_at,
                        cancellation_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _run_values(run),
                )
            except sqlite3.IntegrityError as exc:
                msg = f"Script run '{run.run_id}' already exists."
                raise ScriptRunStoreError(msg) from exc
        return run

    def get_run(self, run_id: str) -> ScriptRunRecord:
        """Return one durable run record."""
        with self._read_connection() as connection:
            row = connection.execute("SELECT * FROM script_runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise ScriptRunNotFoundError(run_id)
        return _run_from_row(row)

    def list_runs(  # noqa: Vulture
        self,
        *,
        agent_name: str | None = None,
        owner_user_id: str | None = None,
        include_finished: bool = True,
    ) -> list[ScriptRunRecord]:
        """List durable runs, optionally narrowed to their owning agent and requester."""
        clauses: list[str] = []
        params: list[object] = []
        if agent_name is not None:
            clauses.append("agent_name = ?")
            params.append(agent_name)
        if owner_user_id is not None:
            clauses.append("owner_user_id = ?")
            params.append(owner_user_id)
        if not include_finished:
            clauses.append("state NOT IN (?, ?, ?, ?)")
            params.extend(sorted(state.value for state in _TERMINAL_RUN_STATES))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._read_connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM script_runs{where} ORDER BY created_at DESC, run_id DESC",  # noqa: S608
                tuple(params),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def require_active_capability(self, run_id: str, token: str) -> ScriptRunRecord:  # noqa: Vulture
        """Authenticate a token and reject runs that may no longer start calls."""
        run = self.get_run(run_id)
        if not hmac.compare_digest(run.token_hash, _capability_hash(token)):
            raise ScriptCapabilityError(_INVALID_CAPABILITY)
        if run.cancel_requested_at is not None or run.state not in {
            ScriptRunState.STARTING,
            ScriptRunState.RUNNING,
        }:
            raise ScriptCapabilityError(_REVOKED_CAPABILITY)
        return run

    def require_call_dispatch_allowed(self, run_id: str) -> ScriptRunRecord:
        """Recheck durable run authority immediately before an accepted call dispatches."""
        run = self.get_run(run_id)
        if run.cancel_requested_at is not None or run.state not in {
            ScriptRunState.STARTING,
            ScriptRunState.RUNNING,
        }:
            raise ScriptCapabilityError(_REVOKED_CAPABILITY)
        return run

    def claim_call(  # noqa: Vulture
        self,
        *,
        run_id: str,
        call_id: str,
        grant: ScriptToolGrant,
        arguments_digest: str,
    ) -> ScriptCallClaim:
        """Create one call claim or return its stable duplicate receipt."""
        with self._write_transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM script_calls WHERE run_id = ? AND call_id = ?",
                (run_id, call_id),
            ).fetchone()
            if existing is not None:
                existing_call = _call_from_row(existing)
                if existing_call.grant != grant or existing_call.arguments_digest != arguments_digest:
                    msg = f"Script call '{call_id}' does not match its original claim."
                    raise ScriptCallConflictError(msg)
                return ScriptCallClaim(call=existing_call, created=False)

            run_row = connection.execute(
                """
                SELECT state, cancel_requested_at, grants_json, max_tool_calls_per_minute
                FROM script_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise ScriptRunNotFoundError(run_id)
            if run_row["cancel_requested_at"] is not None or ScriptRunState(str(run_row["state"])) not in {
                ScriptRunState.STARTING,
                ScriptRunState.RUNNING,
            }:
                raise ScriptCapabilityError(_CALLS_NOT_ACCEPTED)
            if grant not in _grants_from_json(str(run_row["grants_json"])):
                raise ScriptCapabilityError(_GRANT_NOT_GRANTED)
            now = _utc_now()
            window_start = (datetime.now(UTC) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
            recent_call_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM script_calls WHERE run_id = ? AND created_at >= ?",
                    (run_id, window_start),
                ).fetchone()[0],
            )
            if recent_call_count >= int(run_row["max_tool_calls_per_minute"]):
                msg = "Background script tool-call rate limit exceeded."
                raise ScriptCallRateLimitError(msg)
            connection.execute(
                """
                INSERT INTO script_calls (
                    run_id, call_id, toolkit_name, function_name, arguments_digest,
                    state, receipt_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    run_id,
                    call_id,
                    grant.toolkit_name,
                    grant.function_name,
                    arguments_digest,
                    ScriptCallState.PENDING.value,
                    now,
                ),
            )
        return ScriptCallClaim(
            call=ScriptCallRecord(
                run_id=run_id,
                call_id=call_id,
                grant=grant,
                arguments_digest=arguments_digest,
                state=ScriptCallState.PENDING,
                created_at=now,
            ),
            created=True,
        )

    def get_call(self, run_id: str, call_id: str) -> ScriptCallRecord:  # noqa: Vulture
        """Return one durable logical call receipt."""
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT * FROM script_calls WHERE run_id = ? AND call_id = ?",
                (run_id, call_id),
            ).fetchone()
        if row is None:
            raise ScriptCallNotFoundError(call_id)
        return _call_from_row(row)

    def pending_calls(self, run_id: str) -> list[ScriptCallRecord]:
        """Return pending receipts that still need broker ownership settlement."""
        with self._read_connection() as connection:
            rows = connection.execute(
                "SELECT * FROM script_calls WHERE run_id = ? AND state = ? ORDER BY call_id",
                (run_id, ScriptCallState.PENDING.value),
            ).fetchall()
        return [_call_from_row(row) for row in rows]

    def settle_orphaned_call(
        self,
        *,
        run_id: str,
        call_id: str,
        error: object,
    ) -> ScriptCallRecord:
        """Publish indeterminate only if no terminal execution receipt already won."""
        receipt_json = _serialize_receipt(result=None, error=error)
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM script_calls WHERE run_id = ? AND call_id = ?",
                (run_id, call_id),
            ).fetchone()
            if row is None:
                raise ScriptCallNotFoundError(call_id)
            existing = _call_from_row(row)
            if existing.state in _TERMINAL_CALL_STATES:
                return existing
            connection.execute(
                """
                UPDATE script_calls
                SET state = ?, receipt_json = ?
                WHERE run_id = ? AND call_id = ? AND state = ?
                """,
                (
                    ScriptCallState.INDETERMINATE.value,
                    receipt_json,
                    run_id,
                    call_id,
                    ScriptCallState.PENDING.value,
                ),
            )
        _result, stored_error = _receipt_values(receipt_json)
        return replace(
            existing,
            state=ScriptCallState.INDETERMINATE,
            error=stored_error,
        )

    def publish_call_result(  # noqa: Vulture
        self,
        *,
        run_id: str,
        call_id: str,
        state: ScriptCallState,
        result: object | None = None,
        error: object | None = None,
    ) -> ScriptCallRecord:
        """Atomically publish one bounded terminal receipt for a claimed call."""
        if state not in _TERMINAL_CALL_STATES:
            msg = "A script call result must use a terminal state."
            raise ScriptRunStoreError(msg)
        receipt_json = _serialize_receipt(result=result, error=error)
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM script_calls WHERE run_id = ? AND call_id = ?",
                (run_id, call_id),
            ).fetchone()
            if row is None:
                raise ScriptCallNotFoundError(call_id)
            existing = _call_from_row(row)
            if existing.state in _TERMINAL_CALL_STATES:
                if existing.state is state and str(row["receipt_json"]) == receipt_json:
                    return existing
                msg = f"Script call '{call_id}' already has a terminal receipt."
                raise ScriptCallConflictError(msg)
            connection.execute(
                """
                UPDATE script_calls
                SET state = ?, receipt_json = ?
                WHERE run_id = ? AND call_id = ?
                """,
                (state.value, receipt_json, run_id, call_id),
            )
        stored_result, stored_error = _receipt_values(receipt_json)
        return replace(existing, state=state, result=stored_result, error=stored_error)

    def request_cancel(self, run_id: str, *, reason: str | None = None) -> ScriptRunRecord:  # noqa: Vulture
        """Durably revoke a run before any cancellation signal is sent."""
        with self._write_transaction() as connection:
            row = connection.execute("SELECT * FROM script_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise ScriptRunNotFoundError(run_id)
            run = _run_from_row(row)
            if run.cancel_requested_at is not None or run.state in _TERMINAL_RUN_STATES:
                return run
            now = _utc_now()
            connection.execute(
                """
                UPDATE script_runs
                SET cancel_requested_at = ?, cancellation_reason = ?
                WHERE run_id = ?
                """,
                (now, reason, run_id),
            )
        return replace(run, cancel_requested_at=now, cancellation_reason=reason)

    def record_snapshot_locator(self, run_id: str, locator: str) -> ScriptRunRecord:
        """Persist the containment-checked storage-relative directory for one launch snapshot."""
        normalized = _validated_snapshot_locator(run_id, locator)
        with self._write_transaction() as connection:
            row = connection.execute("SELECT * FROM script_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise ScriptRunNotFoundError(run_id)
            run = _run_from_row(row)
            if run.snapshot_locator is not None:
                if run.snapshot_locator == normalized:
                    return run
                msg = f"Script run '{run_id}' already owns a different snapshot locator."
                raise ScriptRunStoreError(msg)
            if run.state in _TERMINAL_RUN_STATES:
                msg = f"Terminal script run '{run_id}' cannot record a snapshot locator."
                raise ScriptRunStoreError(msg)
            connection.execute(
                "UPDATE script_runs SET snapshot_locator = ? WHERE run_id = ?",
                (normalized, run_id),
            )
        return replace(run, snapshot_locator=normalized)

    def transition_run(  # noqa: Vulture
        self,
        run_id: str,
        *,
        state: ScriptRunState,
        worker_id: str | None = None,
        exit_code: int | None = None,
        error: str | None = None,
    ) -> ScriptRunRecord:
        """Validate and atomically apply one durable run-state transition."""
        _validate_run_error(error)
        with self._write_transaction() as connection:
            row = connection.execute("SELECT * FROM script_runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise ScriptRunNotFoundError(run_id)
            run = _run_from_row(row)
            if state is ScriptRunState.RUNNING and run.cancel_requested_at is not None:
                msg = f"Script run '{run_id}' cannot enter running after cancellation was requested."
                raise ScriptRunStoreError(msg)
            if state is not run.state and state not in _RUN_TRANSITIONS[run.state]:
                msg = f"Script run '{run_id}' cannot transition from {run.state.value} to {state.value}."
                raise ScriptRunStoreError(msg)
            now = _utc_now()
            started_at = run.started_at
            finished_at = run.finished_at
            if state is ScriptRunState.RUNNING and started_at is None:
                started_at = now
            if state in _TERMINAL_RUN_STATES and finished_at is None:
                finished_at = now
            updated = replace(
                run,
                state=state,
                worker_id=worker_id if worker_id is not None else run.worker_id,
                started_at=started_at,
                finished_at=finished_at,
                exit_code=exit_code if exit_code is not None else run.exit_code,
                error=error if error is not None else run.error,
            )
            if run.state in _TERMINAL_RUN_STATES:
                if updated == run:
                    return run
                msg = f"Terminal script run '{run_id}' cannot be mutated."
                raise ScriptRunStoreError(msg)
            connection.execute(
                """
                UPDATE script_runs
                SET worker_id = ?, state = ?, started_at = ?,
                    finished_at = ?, exit_code = ?, error = ?
                WHERE run_id = ?
                """,
                (
                    updated.worker_id,
                    updated.state.value,
                    updated.started_at,
                    updated.finished_at,
                    updated.exit_code,
                    updated.error,
                    run_id,
                ),
            )
        return updated

    def prune_terminal_run(self, run_id: str, *, finished_before: str) -> bool:
        """Delete one terminal run and its receipts only after the retention cutoff."""
        with self._write_transaction() as connection:
            deleted = connection.execute(
                """
                DELETE FROM script_runs
                WHERE run_id = ? AND finished_at IS NOT NULL AND finished_at <= ?
                  AND state IN (?, ?, ?, ?)
                """,
                (
                    run_id,
                    finished_before,
                    *(state.value for state in sorted(_TERMINAL_RUN_STATES, key=lambda state: state.value)),
                ),
            )
        return deleted.rowcount == 1

    def _initialize_database(self) -> None:
        with self._write_transaction() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(script_runs)")}
            if "snapshot_locator" not in columns:
                connection.execute("ALTER TABLE script_runs ADD COLUMN snapshot_locator TEXT")
            if "preapprove_launch_grants" not in columns:
                connection.execute(
                    "ALTER TABLE script_runs ADD COLUMN preapprove_launch_grants INTEGER NOT NULL DEFAULT 0",
                )

    @contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        connection = _connect(self.database_path)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        connection = _connect(self.database_path)
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")
        finally:
            connection.close()


_SCHEMA_STATEMENTS = (
    """
                CREATE TABLE IF NOT EXISTS script_runs (
                    run_id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    owner_user_id TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    thread_root_event_id TEXT,
                    execution_identity_json TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    grants_json TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    preapprove_launch_grants INTEGER NOT NULL,
                    worker_key TEXT,
                    worker_id TEXT,
                    snapshot_locator TEXT,
                    name TEXT,
                    local_unsafe INTEGER NOT NULL,
                    max_tool_calls_per_minute INTEGER NOT NULL,
                    max_runtime_seconds INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    exit_code INTEGER,
                    error TEXT,
                    cancel_requested_at TEXT,
                    cancellation_reason TEXT
                )
                """,
    """
                CREATE TABLE IF NOT EXISTS script_calls (
                    run_id TEXT NOT NULL REFERENCES script_runs(run_id) ON DELETE CASCADE,
                    call_id TEXT NOT NULL,
                    toolkit_name TEXT NOT NULL,
                    function_name TEXT NOT NULL,
                    arguments_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    receipt_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, call_id)
                )
                """,
    """
                CREATE INDEX IF NOT EXISTS script_runs_owner_agent_created_idx
                    ON script_runs(owner_user_id, agent_name, created_at DESC)
                """,
    """
                CREATE INDEX IF NOT EXISTS script_calls_run_created_idx
                    ON script_calls(run_id, created_at)
                """,
)


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=10)
    connection.row_factory = sqlite3.Row  # noqa: Vulture
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _capability_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _run_values(run: ScriptRunRecord) -> tuple[object, ...]:
    return (
        run.run_id,
        run.agent_name,
        run.owner_user_id,
        run.room_id,
        run.thread_root_event_id,
        json.dumps(run.execution_identity, separators=(",", ":"), sort_keys=True),
        run.source_digest,
        json.dumps(
            [[grant.toolkit_name, grant.function_name] for grant in run.grants],
            separators=(",", ":"),
        ),
        run.token_hash,
        int(run.preapprove_launch_grants),
        run.worker_key,
        run.worker_id,
        run.snapshot_locator,
        run.name,
        int(run.local_unsafe),
        run.max_tool_calls_per_minute,
        run.max_runtime_seconds,
        run.state.value,
        run.created_at,
        run.started_at,
        run.finished_at,
        run.exit_code,
        run.error,
        run.cancel_requested_at,
        run.cancellation_reason,
    )


def _run_from_row(row: sqlite3.Row) -> ScriptRunRecord:
    execution_identity = json.loads(str(row["execution_identity_json"]))
    column_names = row.keys()
    return ScriptRunRecord(
        run_id=str(row["run_id"]),
        agent_name=str(row["agent_name"]),
        owner_user_id=str(row["owner_user_id"]),
        room_id=str(row["room_id"]),
        source_digest=str(row["source_digest"]),
        grants=_grants_from_json(str(row["grants_json"])),
        token_hash=str(row["token_hash"]),
        preapprove_launch_grants=(
            bool(row["preapprove_launch_grants"]) if "preapprove_launch_grants" in column_names else False
        ),
        thread_root_event_id=_nullable_string(row["thread_root_event_id"]),
        execution_identity=cast("dict[str, object]", execution_identity),
        worker_key=_nullable_string(row["worker_key"]),
        worker_id=_nullable_string(row["worker_id"]),
        snapshot_locator=_nullable_string(row["snapshot_locator"]),
        name=_nullable_string(row["name"]),
        local_unsafe=bool(row["local_unsafe"]),
        max_tool_calls_per_minute=int(row["max_tool_calls_per_minute"]),
        max_runtime_seconds=int(row["max_runtime_seconds"]),
        state=ScriptRunState(str(row["state"])),
        created_at=str(row["created_at"]),
        started_at=_nullable_string(row["started_at"]),
        finished_at=_nullable_string(row["finished_at"]),
        exit_code=cast("int | None", row["exit_code"]),
        error=_nullable_string(row["error"]),
        cancel_requested_at=_nullable_string(row["cancel_requested_at"]),
        cancellation_reason=_nullable_string(row["cancellation_reason"]),
    )


def _call_from_row(row: sqlite3.Row) -> ScriptCallRecord:
    result: object | None = None
    error: object | None = None
    if row["receipt_json"] is not None:
        result, error = _receipt_values(str(row["receipt_json"]))
    return ScriptCallRecord(
        run_id=str(row["run_id"]),
        call_id=str(row["call_id"]),
        grant=ScriptToolGrant(str(row["toolkit_name"]), str(row["function_name"])),
        arguments_digest=str(row["arguments_digest"]),
        state=ScriptCallState(str(row["state"])),
        created_at=str(row["created_at"]),
        result=result,
        error=error,
    )


def _serialize_receipt(*, result: object | None, error: object | None) -> str:
    try:
        wire_value = json.loads(
            json.dumps(
                {"result": result, "error": error},
                allow_nan=False,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        )
        serialized = json.dumps(
            wire_value,
            allow_nan=False,
            separators=(",", ":"),
            ensure_ascii=False,
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ScriptRunStoreError(_RECEIPT_NOT_SERIALIZABLE) from exc
    if len(serialized.encode("utf-8")) > _MAX_RECEIPT_BYTES:
        msg = f"Script call receipt exceeds the {_MAX_RECEIPT_BYTES}-byte limit."
        raise ScriptRunStoreError(msg)
    return serialized


def _receipt_values(receipt_json: str) -> tuple[object | None, object | None]:
    receipt = cast("dict[str, object]", json.loads(receipt_json))
    return receipt.get("result"), receipt.get("error")


def _grants_from_json(grants_json: str) -> tuple[ScriptToolGrant, ...]:
    pairs = json.loads(grants_json)
    return tuple(ScriptToolGrant(str(pair[0]), str(pair[1])) for pair in pairs)


def _validate_run_error(error: str | None) -> None:
    if error is not None and len(error.encode("utf-8")) > _MAX_RUN_ERROR_BYTES:
        msg = f"Script run error exceeds the {_MAX_RUN_ERROR_BYTES}-byte limit."
        raise ScriptRunStoreError(msg)


def _validated_snapshot_locator(run_id: str, locator: str) -> str:
    path = PurePosixPath(locator)
    if (
        not locator
        or "\\" in locator
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.parts[-3:] != (".mindroom", "script-runs", run_id)
    ):
        msg = "Background script snapshot locator must identify its storage-contained run directory."
        raise ScriptRunStoreError(msg)
    return path.as_posix()


def _nullable_string(value: object) -> str | None:
    return str(value) if value is not None else None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
