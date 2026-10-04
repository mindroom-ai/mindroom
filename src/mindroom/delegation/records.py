"""Primary-owned delegation records and their write-only workspace audit exports."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4
from weakref import WeakValueDictionary

from mindroom.atomic_file import atomic_write_bytes_at, atomic_write_file_at
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import primary_records_dir
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, open_regular_file_at, write_file_within_root
from mindroom.redaction import redact_sensitive_data
from mindroom.runtime_resolution import resolve_agent_storage
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_workspace_root_path,
    parse_tool_execution_identity_payload,
    serialize_tool_execution_identity,
)
from mindroom.workspaces import resolve_agent_workspace_from_state_path

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

type _DelegationActiveStatus = Literal["running", "paused"]
type DelegationTerminalStatus = Literal["completed", "failed", "cancelled", "denied"]
type _DelegationEventKind = Literal[
    "delegation_started",
    "delegation_finished",
    "output",
    "tool_call",
    "tool_result",
    "approval_requested",
    "approval_decision",
    "error",
    "status",
    "usage",
]
type _JsonValue = None | bool | int | float | str | list["_JsonValue"] | dict[str, "_JsonValue"]

_SCHEMA_VERSION = 1
_DELEGATION_DIRECTORY = Path(".mindroom/delegations")
_RECEIPT_DIRECTORY = Path(".mindroom/delegation_receipts")
_EVENT_LOG = "events.jsonl"
_STATE_FILE = "state.json"
_MAX_INLINE_VALUE_BYTES = 64 * 1024
# Caps on what one record may hold; values above _MAX_INLINE_VALUE_BYTES move to artifacts/.
_MAX_RUN_BYTES = 4 << 20
_MAX_EVENT_LINE_BYTES = 1 << 20
_MAX_EVENT_LOG_BYTES = 64 << 20
_MAX_EVENTS = 65536
# Other events stop this far short of the log cap, so the terminal event, four inline values at most, always fits.
_FINISH_EVENT_HEADROOM_BYTES = 1 << 20
# A new run.json keeps room for the three inline values and timestamps finish adds.
_FINISH_RUN_HEADROOM_BYTES = 3 * _MAX_INLINE_VALUE_BYTES + 256
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied"})
# Writers of one record exclude each other in this process, without a lock file worker code could hold.
_RECORD_LOCKS: WeakValueDictionary[Path, threading.Lock] = WeakValueDictionary()
_RECORD_LOCKS_GUARD = threading.Lock()


class DelegationRecordLimitError(ValueError):
    """An event the record's size limits refuse before anything is written."""


@dataclass(frozen=True)
class DelegationMetadata:
    """Stable identities and source context for one delegated run."""

    caller_agent_name: str
    child_agent_name: str
    requester_id: str | None
    parent_run_id: str | None
    parent_tool_call_id: str | None
    source_room_id: str | None
    source_thread_id: str | None
    model_name: str | None
    task: str
    parent_delegation_id: str | None = None
    subagent_id: str | None = None
    previous_delegation_id: str | None = None


@dataclass(frozen=True)
class DelegationRecordLocator:
    """Persistable identity used to reopen one scoped record after restart."""

    delegation_id: str
    started_date: str
    caller_agent_name: str
    child_agent_name: str
    caller_execution_identity: ToolExecutionIdentity | None
    child_execution_identity: ToolExecutionIdentity | None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe locator without persisting trusted filesystem paths."""
        return {
            "delegation_id": self.delegation_id,
            "started_date": self.started_date,
            "caller_agent_name": self.caller_agent_name,
            "child_agent_name": self.child_agent_name,
            "caller_execution_identity": (
                serialize_tool_execution_identity(self.caller_execution_identity)
                if self.caller_execution_identity is not None
                else None
            ),
            "child_execution_identity": (
                serialize_tool_execution_identity(self.child_execution_identity)
                if self.child_execution_identity is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: object) -> DelegationRecordLocator:
        """Parse one persisted locator and reject path-like or malformed identities."""
        if not isinstance(payload, dict):
            msg = "Delegation record locator must be an object"
            raise TypeError(msg)
        raw = cast("dict[str, object]", payload)
        expected_fields = {
            "delegation_id",
            "started_date",
            "caller_agent_name",
            "child_agent_name",
            "caller_execution_identity",
            "child_execution_identity",
        }
        if raw.keys() != expected_fields:
            msg = "Delegation record locator has unexpected fields"
            raise ValueError(msg)
        delegation_id = _validated_id(raw["delegation_id"])
        started_date = _validated_date(raw["started_date"])
        caller_agent_name = _required_string(raw["caller_agent_name"], field_name="caller_agent_name")
        child_agent_name = _required_string(raw["child_agent_name"], field_name="child_agent_name")
        return cls(
            delegation_id=delegation_id,
            started_date=started_date,
            caller_agent_name=caller_agent_name,
            child_agent_name=child_agent_name,
            caller_execution_identity=_parse_optional_execution_identity(
                raw["caller_execution_identity"],
                field_name="caller_execution_identity",
            ),
            child_execution_identity=_parse_optional_execution_identity(
                raw["child_execution_identity"],
                field_name="child_execution_identity",
            ),
        )


@dataclass(frozen=True)
class DelegationRecordHandle:
    """Resolved paths plus the restart-safe identity for one delegation record."""

    locator: DelegationRecordLocator
    scoped_path: str
    child_workspace: Path
    caller_workspace: Path
    # The primary-only directory holding the record's working state, of which the workspace files are exports.
    state_dir: Path

    @property
    def _record_reference(self) -> str:
        """Return a stable agent-scoped reference suitable for caller output."""
        return f"{self.locator.child_agent_name}:{self.scoped_path}"

    def to_receipt(self) -> str:
        """Render a concise caller-visible record reference."""
        return f"Delegation {self.locator.delegation_id} record: {self._record_reference}"


@dataclass(frozen=True)
class DelegationEvent:
    """One ordered event appended while delegated work progresses."""

    kind: _DelegationEventKind
    data: Mapping[str, object] = field(default_factory=dict)
    timestamp: str | None = None
    status: _DelegationActiveStatus | None = None
    event_id: str | None = None


@dataclass
class _RecordState:
    """One record's committed state, kept beside its event log in primary-only storage."""

    # The run.json document the workspace receives.
    run: dict[str, Any]
    log_bytes: int = 0
    event_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DelegationRecordOwner:
    """Resolve scoped workspaces, own each record's state, and export it to the workspaces."""

    config: Config
    runtime_paths: RuntimePaths

    async def start(
        self,
        metadata: DelegationMetadata,
        *,
        caller_execution_identity: ToolExecutionIdentity | None,
        child_execution_identity: ToolExecutionIdentity | None,
        delegation_id: str | None = None,
    ) -> DelegationRecordHandle:
        """Create an initial record and caller-scoped receipt before child execution."""
        return await run_blocking_until_complete(
            partial(
                self._start,
                metadata,
                caller_execution_identity=caller_execution_identity,
                child_execution_identity=child_execution_identity,
                delegation_id=delegation_id,
            ),
        )

    async def reopen(self, locator: DelegationRecordLocator) -> DelegationRecordHandle:
        """Re-resolve and validate one record from its persisted identity."""
        return await run_blocking_until_complete(self._reopen, locator)

    async def append_event(
        self,
        handle: DelegationRecordHandle,
        event: DelegationEvent,
    ) -> None:
        """Append one full, redacted event and refresh the record's exports."""
        await run_blocking_until_complete(self._append_event, handle, event)

    async def finish(
        self,
        handle: DelegationRecordHandle,
        *,
        status: DelegationTerminalStatus,
        output: object | None = None,
        error: object | None = None,
        usage: object | None = None,
    ) -> None:
        """Settle one record with its exact terminal outcome."""
        await run_blocking_until_complete(
            partial(
                self._finish,
                handle,
                status=status,
                output=output,
                error=error,
                usage=usage,
            ),
        )

    def _start(
        self,
        metadata: DelegationMetadata,
        *,
        caller_execution_identity: ToolExecutionIdentity | None,
        child_execution_identity: ToolExecutionIdentity | None,
        delegation_id: str | None,
    ) -> DelegationRecordHandle:
        _validate_metadata(metadata)
        resolved_id = _validated_id(delegation_id or uuid4().hex)
        timestamp = _utc_timestamp()
        locator = DelegationRecordLocator(
            delegation_id=resolved_id,
            started_date=timestamp[:10],
            caller_agent_name=metadata.caller_agent_name,
            child_agent_name=metadata.child_agent_name,
            caller_execution_identity=caller_execution_identity,
            child_execution_identity=child_execution_identity,
        )
        handle = self._resolve_handle(locator)
        run = redact_sensitive_data(
            {
                "schema_version": _SCHEMA_VERSION,
                "delegation_id": resolved_id,
                **asdict(metadata),
                "status": "running",
                "started_at": timestamp,
                "updated_at": timestamp,
                "finished_at": None,
                "output": None,
                "error": None,
                "usage": None,
                "event_count": 0,
                "record_reference": handle._record_reference,
            },
        )
        state = _RecordState(cast("dict[str, Any]", run))
        if len(_json_bytes(state.run)) + _FINISH_RUN_HEADROOM_BYTES > _MAX_RUN_BYTES:
            msg = "Delegation record exceeds its size limit"
            raise DelegationRecordLimitError(msg)
        with _record_lock(handle):
            if (handle.state_dir / _STATE_FILE).exists():
                msg = f"Delegation record already exists: {resolved_id}"
                raise FileExistsError(msg)
            create_directory_durable(handle.state_dir, mode=0o700)
            line = _commit_event(
                handle,
                state,
                _event_payload(
                    sequence=1,
                    timestamp=timestamp,
                    kind="delegation_started",
                    data={},
                    status="running",
                    event_id="delegation_started",
                ),
            )
            _write_exports(handle, state, line)
        return handle

    def _reopen(self, locator: DelegationRecordLocator) -> DelegationRecordHandle:
        handle = self._resolve_handle(locator)
        with _record_lock(handle):
            _load_state(handle)
        return handle

    def _append_event(self, handle: DelegationRecordHandle, event: DelegationEvent) -> None:
        handle = self._validated_handle(handle)
        with _record_lock(handle):
            state = _load_state(handle)
            if state is None:
                return
            if event.event_id is not None and _event_id_digest(event.event_id) in state.event_ids:
                _write_exports(handle, state)
                return
            _ensure_active(state.run)
            sequence = state.run["event_count"] + 1
            data, artifacts = _redacted_event_data(sequence=sequence, data=event.data)
            payload = _event_payload(
                sequence=sequence,
                timestamp=event.timestamp or _utc_timestamp(),
                kind=event.kind,
                data=data,
                status=event.status,
                event_id=event.event_id,
            )
            _write_exports(handle, state, _commit_event(handle, state, payload), artifacts)

    def _finish(
        self,
        handle: DelegationRecordHandle,
        *,
        status: DelegationTerminalStatus,
        output: object | None,
        error: object | None,
        usage: object | None,
    ) -> None:
        if status not in _TERMINAL_STATUSES:
            msg = f"Invalid delegation terminal status: {status}"
            raise ValueError(msg)
        handle = self._validated_handle(handle)
        with _record_lock(handle):
            state = _load_state(handle)
            if state is None:
                return
            line = b""
            artifacts: dict[str, bytes] = {}
            # The first terminal outcome stands, so a later finish, even with another status, only refreshes exports.
            if state.run["status"] not in _TERMINAL_STATUSES:
                sequence = state.run["event_count"] + 1
                terminal_data, artifacts = _redacted_event_data(
                    sequence=sequence,
                    data={"status": status, "output": output, "error": error, "usage": usage},
                )
                payload = _event_payload(
                    sequence=sequence,
                    timestamp=_utc_timestamp(),
                    kind="delegation_finished",
                    data=terminal_data,
                    status=None,
                    event_id="delegation_finished",
                )
                line = _commit_event(handle, state, payload, terminal=True)
            # Every finish, a replayed one included, rewrites the exports and renders transcript.md.
            _write_exports(handle, state, line, artifacts, final=True)

    def _resolve_handle(self, locator: DelegationRecordLocator) -> DelegationRecordHandle:
        delegation_id = _validated_id(locator.delegation_id)
        started_date = _validated_date(locator.started_date)
        child_workspace, child_state_root = _resolve_workspace(
            locator.child_agent_name,
            config=self.config,
            runtime_paths=self.runtime_paths,
            execution_identity=locator.child_execution_identity,
        )
        caller_workspace, _caller_state_root = _resolve_workspace(
            locator.caller_agent_name,
            config=self.config,
            runtime_paths=self.runtime_paths,
            execution_identity=locator.caller_execution_identity,
        )
        return DelegationRecordHandle(
            locator=locator,
            scoped_path=(_DELEGATION_DIRECTORY / started_date / delegation_id).as_posix(),
            child_workspace=child_workspace,
            caller_workspace=caller_workspace,
            state_dir=primary_records_dir(child_state_root, self.runtime_paths)
            / "delegations"
            / started_date
            / delegation_id,
        )

    def _validated_handle(self, handle: DelegationRecordHandle) -> DelegationRecordHandle:
        resolved = self._resolve_handle(handle.locator)
        if resolved != handle:
            msg = f"Delegation record handle path mismatch: {handle.locator.delegation_id}"
            raise ValueError(msg)
        return resolved


def _required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"Delegation {field_name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _validated_id(value: object) -> str:
    delegation_id = _required_string(value, field_name="delegation_id")
    if _ID_PATTERN.fullmatch(delegation_id) is None:
        msg = "Delegation delegation_id must contain only letters, numbers, underscores, or hyphens"
        raise ValueError(msg)
    return delegation_id


def _validated_date(value: object) -> str:
    started_date = _required_string(value, field_name="started_date")
    try:
        parsed = date.fromisoformat(started_date)
    except ValueError as exc:
        msg = "Delegation started_date must be an ISO date"
        raise ValueError(msg) from exc
    if parsed.isoformat() != started_date:
        msg = "Delegation started_date must be a canonical ISO date"
        raise ValueError(msg)
    return started_date


def _parse_optional_execution_identity(
    payload: object,
    *,
    field_name: str,
) -> ToolExecutionIdentity | None:
    if payload is None:
        return None
    return parse_tool_execution_identity_payload(
        payload,
        strict=True,
        error_prefix=f"Delegation locator {field_name}",
    )


def _validate_metadata(metadata: DelegationMetadata) -> None:
    _required_string(metadata.caller_agent_name, field_name="caller_agent_name")
    _required_string(metadata.child_agent_name, field_name="child_agent_name")
    _required_string(metadata.task, field_name="task")


def _resolve_workspace(
    agent_name: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> tuple[Path, Path]:
    """Return the agent's workspace root and the state root it belongs to."""
    resolved = resolve_agent_storage(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    # Record lookup must not seed templates or reconcile knowledge with a
    # retained storage-only config during cancellation or restart recovery.
    workspace = resolve_agent_workspace_from_state_path(
        agent_name,
        config,
        runtime_paths=runtime_paths,
        state_storage_path=resolved.state_root,
        use_state_storage_path=resolved.execution.policy.private_workspace_enabled,
    )
    if workspace is None:
        return agent_workspace_root_path(runtime_paths.storage_root, agent_name), resolved.state_root
    return workspace.root, resolved.state_root


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _receipt_relative_path(locator: DelegationRecordLocator) -> Path:
    return _RECEIPT_DIRECTORY / _validated_date(locator.started_date) / f"{_validated_id(locator.delegation_id)}.json"


@contextmanager
def _record_lock(handle: DelegationRecordHandle) -> Iterator[None]:
    """Serialize the writers of one record without taking a lock worker code could hold."""
    with _RECORD_LOCKS_GUARD:
        lock = _RECORD_LOCKS.get(handle.state_dir)
        if lock is None:
            lock = threading.Lock()
            _RECORD_LOCKS[handle.state_dir] = lock
    with lock:
        yield


def _load_state(handle: DelegationRecordHandle) -> _RecordState | None:
    """Return the record's committed state, or None for a record that predates primary-owned state."""
    try:
        payload = (handle.state_dir / _STATE_FILE).read_bytes()
    except FileNotFoundError:
        # LEGACY_COMPAT: Delegation records whose working state lived only in their workspace files.
        # Legacy format: A record directory below the child workspace's .mindroom/delegations/ with no state.json below
        # tracking/; selected when the state file is missing while that workspace directory exists.
        # Last legacy release: v2026.10.101; replacement: the next release keeps each record's state below tracking/
        # and writes run.json, events.jsonl, and transcript.md as exports of it.
        # Handling: Such a record's operations return without reading or writing its workspace files, so a delegation
        # still in flight at the upgrade settles and its record stays as it was; a record found in neither place is missing.
        # Coverage: tests/test_delegation_records.py::test_a_record_started_before_primary_state_is_left_as_it_was.
        with _record_directory(handle):
            return None
    return _RecordState(**json.loads(payload))


def _event_id_digest(event_id: str) -> str:
    # Fixed-size digests keep long event IDs from growing the state file.
    return hashlib.blake2b(event_id.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


@contextmanager
def _record_directory(handle: DelegationRecordHandle, *, create: bool = False) -> Iterator[int]:
    """Pin the workspace record directory by a no-follow walk from the child workspace."""
    if create:
        handle.child_workspace.mkdir(parents=True, exist_ok=True)
    with open_directory_within_root(handle.child_workspace, handle.scoped_path, create=create, mode=0o700) as record_fd:
        yield record_fd


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        remaining = remaining[os.write(descriptor, remaining) :]


def _commit_event(
    handle: DelegationRecordHandle,
    state: _RecordState,
    payload: Mapping[str, Any],
    *,
    terminal: bool = False,
) -> bytes:
    """Append one event to the primary log, then commit the state that counts it, and return the event's line."""
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    log_limit = _MAX_EVENT_LOG_BYTES if terminal else _MAX_EVENT_LOG_BYTES - _FINISH_EVENT_HEADROOM_BYTES
    event_limit = _MAX_EVENTS if terminal else _MAX_EVENTS - 1
    if (
        len(line) > _MAX_EVENT_LINE_BYTES
        or state.log_bytes + len(line) > log_limit
        or state.run["event_count"] >= event_limit
    ):
        msg = "Delegation event exceeds its size limit"
        raise DelegationRecordLimitError(msg)
    descriptor = os.open(handle.state_dir / _EVENT_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        # The state file is the commit point, so an append whose state never committed is dropped first.
        os.ftruncate(descriptor, state.log_bytes)
        _write_all(descriptor, line)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _apply_event(state.run, payload)
    state.log_bytes += len(line)
    if payload["event_id"] is not None:
        state.event_ids.append(_event_id_digest(payload["event_id"]))
    write_json_file_durable(handle.state_dir / _STATE_FILE, asdict(state), strict_atomic_replace=True)
    return line


def _ensure_active(run: Mapping[str, Any]) -> None:
    if run["status"] in _TERMINAL_STATUSES:
        msg = f"Delegation record is already terminal: {run['delegation_id']}"
        raise ValueError(msg)


def _event_payload(
    *,
    sequence: int,
    timestamp: str,
    kind: _DelegationEventKind,
    data: Mapping[str, object],
    status: _DelegationActiveStatus | None,
    event_id: str | None,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "sequence": sequence,
        "timestamp": timestamp,
        "kind": kind,
        "data": data,
        "status": status,
        "event_id": event_id,
    }


def _apply_event(run: dict[str, Any], event: Mapping[str, Any]) -> None:
    """Fold one committed event into the run view."""
    run["event_count"] = event["sequence"]
    run["updated_at"] = event["timestamp"]
    if event["status"] is not None:
        run["status"] = event["status"]
    if event["kind"] == "delegation_finished":
        terminal_data = event["data"]
        run["status"] = terminal_data["status"]
        run["finished_at"] = event["timestamp"]
        run["output"] = terminal_data["output"]
        run["error"] = terminal_data["error"]
        run["usage"] = terminal_data["usage"]


def _write_exports(
    handle: DelegationRecordHandle,
    state: _RecordState,
    line: bytes = b"",
    artifacts: Mapping[str, bytes] | None = None,
    *,
    final: bool = False,
) -> None:
    """Rewrite the workspace exports and the caller's receipt from the committed state, never reading them back.

    Worker code can replace these files or their directories at will, so a failed write is only logged.
    """
    try:
        with _record_directory(handle, create=True) as record_fd:
            if artifacts:
                with open_directory_within_root(record_fd, "artifacts", create=True, mode=0o700) as artifacts_fd:
                    for artifact_name, encoded in artifacts.items():
                        atomic_write_bytes_at(artifacts_fd, artifact_name, encoded)
            atomic_write_bytes_at(record_fd, "run.json", _json_bytes(state.run))
            if final or not _append_export(record_fd, line, size_before=state.log_bytes - len(line)):
                with atomic_write_file_at(record_fd, _EVENT_LOG) as export:
                    export.writelines(_committed_lines(handle, state))
            if final:
                _write_transcript(handle, record_fd, state)
    except (OSError, ValueError) as exc:
        logger.warning("delegation_record_export_failed", delegation_id=handle.locator.delegation_id, error=str(exc))
    try:
        _write_receipt(handle, state.run)
    except (OSError, ValueError) as exc:
        logger.warning("delegation_receipt_export_failed", delegation_id=handle.locator.delegation_id, error=str(exc))


def _append_export(record_fd: int, line: bytes, *, size_before: int) -> bool:
    """Append the newest event to the workspace log while its size still matches the events committed before it."""
    try:
        descriptor = open_regular_file_at(record_fd, _EVENT_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    except (OSError, ValueError):
        # A link, FIFO, hard link, or unwritable file at the name is replaced by a rewrite instead.
        return False
    try:
        if os.fstat(descriptor).st_size != size_before:
            return False
        _write_all(descriptor, line)
    finally:
        os.close(descriptor)
    return True


def _committed_lines(handle: DelegationRecordHandle, state: _RecordState) -> Iterator[bytes]:
    """Yield the primary log's committed events in order, one line each."""
    with (handle.state_dir / _EVENT_LOG).open("rb") as log:
        yield from islice(log, state.run["event_count"])


def _redacted_event_data(
    *,
    sequence: int,
    data: Mapping[str, object],
) -> tuple[dict[str, _JsonValue], dict[str, bytes]]:
    """Return the event's redacted data and the oversized values it references, which export as artifacts."""
    redacted = redact_sensitive_data(data)
    if not isinstance(redacted, dict):
        return {"value": cast("_JsonValue", redacted)}, {}
    materialized: dict[str, _JsonValue] = {}
    artifacts: dict[str, bytes] = {}
    for index, (field_name, value) in enumerate(redacted.items(), start=1):
        encoded = json.dumps(value, sort_keys=True).encode("utf-8")
        if len(encoded) <= _MAX_INLINE_VALUE_BYTES:
            materialized[field_name] = value
            continue
        artifact_name = f"{sequence:06d}-{index:02d}-{_safe_artifact_label(field_name)}.json"
        artifact_relative_path = Path("artifacts") / artifact_name
        artifacts[artifact_name] = encoded
        materialized[field_name] = {
            "artifact_path": artifact_relative_path.as_posix(),
            "byte_count": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "oversized": True,
            "redacted": True,
        }
    return materialized, artifacts


def _safe_artifact_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return (label or "value")[:64]


def _write_receipt(
    handle: DelegationRecordHandle,
    run: Mapping[str, Any],
) -> None:
    receipt = {
        "schema_version": _SCHEMA_VERSION,
        "delegation_id": handle.locator.delegation_id,
        "caller_agent_name": handle.locator.caller_agent_name,
        "child_agent_name": handle.locator.child_agent_name,
        "status": run["status"],
        "record_reference": handle._record_reference,
        "started_at": run["started_at"],
        "updated_at": run["updated_at"],
        "finished_at": run["finished_at"],
    }
    write_file_within_root(
        handle.caller_workspace,
        _receipt_relative_path(handle.locator),
        _json_bytes(redact_sensitive_data(receipt)),
        dir_mode=0o700,
    )


def _write_transcript(
    handle: DelegationRecordHandle,
    record_fd: int,
    state: _RecordState,
) -> None:
    run = state.run
    header = [
        f"# Delegation {run['delegation_id']}",
        "",
        f"- Caller: {run['caller_agent_name']}",
        f"- Child: {run['child_agent_name']}",
        f"- Model: {run['model_name']}",
        f"- Status: {run['status']}",
        f"- Started: {run['started_at']}",
        f"- Updated: {run['updated_at']}",
        f"- Record: {run['record_reference']}",
        "",
        "## Task",
        "",
        run["task"],
        "",
        "## Events",
        "",
        "",
    ]
    with atomic_write_file_at(record_fd, "transcript.md") as transcript:
        transcript.write("\n".join(header).encode("utf-8"))
        for index, line in enumerate(_committed_lines(handle, state)):
            event = json.loads(line)
            section = [
                f"### {event['sequence']}. {event['kind']}",
                "",
                f"Timestamp: {event['timestamp']}",
                "",
                "```json",
                json.dumps(event["data"], ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                "```",
                "",
            ]
            separator = "\n" if index else ""
            transcript.write((separator + "\n".join(section)).encode("utf-8"))
