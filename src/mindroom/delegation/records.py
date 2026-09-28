"""Durable workspace audit records for delegated agent runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4
from weakref import WeakValueDictionary

from mindroom.atomic_file import atomic_write_bytes_at, atomic_write_file_at
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.path_confinement import (
    open_directory_within_root,
    open_regular_file_at,
    read_regular_file_within_root,
    write_file_within_root,
)
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
type _DelegationStatus = _DelegationActiveStatus | DelegationTerminalStatus
# Device, inode, size, and change time of the event log as the primary last left it.
type _LogIdentity = tuple[int, int, int, int]

_SCHEMA_VERSION = 1
_DELEGATION_DIRECTORY = Path(".mindroom/delegations")
_RECEIPT_DIRECTORY = Path(".mindroom/delegation_receipts")
_MAX_INLINE_VALUE_BYTES = 64 * 1024
# The first use of a record in a process and each finish read the whole worker-writable record,
# so these caps bound what one planted record costs.
# Values above _MAX_INLINE_VALUE_BYTES already move to artifacts/, so real records stay far below them,
# and a real event line carries at most five such values.
_MAX_RUN_BYTES = 4 << 20
_MAX_EVENT_LINE_BYTES = 1 << 20
_MAX_EVENT_LOG_BYTES = 64 << 20
_MAX_EVENTS = 65536
# Other events stop this far short of the log cap, so the terminal event, four inline values at most, always fits.
_FINISH_EVENT_HEADROOM_BYTES = 1 << 20
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied"})
_STATUSES = frozenset({"running", "paused", *_TERMINAL_STATUSES})
# Records whose folded view stays in memory; a record pushed out folds its log again when next used.
_MAX_CACHED_RECORDS = 256
# Worker code can lock any file in the record directory, so writers of one record exclude each other in this process.
_RECORD_LOCKS: WeakValueDictionary[Path, threading.Lock] = WeakValueDictionary()
# Each record's view as the primary last wrote it, so a write never parses what worker code can plant in the log.
_RECORD_STATES: OrderedDict[Path, _RecordState] = OrderedDict()
_RECORDS_GUARD = threading.Lock()


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
class _DelegationRun:
    """The rebuildable run.json summary; a load keeps only these typed fields, so nothing else planted there survives."""

    delegation_id: str
    metadata: DelegationMetadata
    status: _DelegationStatus
    started_at: str
    updated_at: str
    finished_at: str | None
    output: _JsonValue
    error: _JsonValue
    usage: _JsonValue
    event_count: int
    record_reference: str

    @classmethod
    def from_json(cls, payload: object) -> _DelegationRun:
        """Parse one run summary, refusing any field the primary would not have written."""
        if not isinstance(payload, dict):
            msg = "Delegation run must be an object"
            raise ValueError(msg)  # noqa: TRY004 - callers handle one error type for every unusable record
        run = cast("dict[str, object]", payload)
        schema_version = run.get("schema_version")
        if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
            msg = "Delegation run has an unsupported schema version"
            raise ValueError(msg)
        status = _string_field(run, "status")
        event_count = run.get("event_count")
        if status not in _STATUSES or type(event_count) is not int:
            msg = "Delegation run has an invalid status or event count"
            raise ValueError(msg)
        return cls(
            delegation_id=_string_field(run, "delegation_id"),
            metadata=DelegationMetadata(
                caller_agent_name=_string_field(run, "caller_agent_name"),
                child_agent_name=_string_field(run, "child_agent_name"),
                requester_id=_optional_string_field(run, "requester_id"),
                parent_run_id=_optional_string_field(run, "parent_run_id"),
                parent_tool_call_id=_optional_string_field(run, "parent_tool_call_id"),
                source_room_id=_optional_string_field(run, "source_room_id"),
                source_thread_id=_optional_string_field(run, "source_thread_id"),
                model_name=_optional_string_field(run, "model_name"),
                task=_string_field(run, "task"),
                parent_delegation_id=_optional_string_field(run, "parent_delegation_id"),
                subagent_id=_optional_string_field(run, "subagent_id"),
                previous_delegation_id=_optional_string_field(run, "previous_delegation_id"),
            ),
            status=cast("_DelegationStatus", status),
            started_at=_string_field(run, "started_at"),
            updated_at=_string_field(run, "updated_at"),
            finished_at=_optional_string_field(run, "finished_at"),
            output=_inline_field(run, "output"),
            error=_inline_field(run, "error"),
            usage=_inline_field(run, "usage"),
            event_count=event_count,
            record_reference=_string_field(run, "record_reference"),
        )

    def to_json(self) -> dict[str, object]:
        """Return the run.json payload."""
        return {
            "schema_version": _SCHEMA_VERSION,
            "delegation_id": self.delegation_id,
            **asdict(self.metadata),
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "output": self.output,
            "error": self.error,
            "usage": self.usage,
            "event_count": self.event_count,
            "record_reference": self.record_reference,
        }


@dataclass
class _RecordState:
    """The primary's view of one record as of its last write to the event log."""

    run: _DelegationRun
    log_identity: _LogIdentity | None = None
    event_count: int = 0
    last_sequence: int | None = None
    event_ids: set[bytes] = field(default_factory=set)

    def has_event(self, event_id: str) -> bool:
        """Return whether a committed event carries this stable ID."""
        return _event_id_digest(event_id) in self.event_ids

    def observe(self, event: Mapping[str, object]) -> None:
        """Account for one committed event in the view."""
        _apply_event(self.run, event)
        sequence = event.get("sequence")
        event_id = event.get("event_id")
        self.event_count += 1
        self.last_sequence = sequence if type(sequence) is int else None
        if isinstance(event_id, str):
            self.event_ids.add(_event_id_digest(event_id))

    def next_sequence(self, delegation_id: str) -> int:
        """Return the sequence the next committed event takes."""
        if self.event_count == 0:
            return 1
        if self.last_sequence is None:
            msg = f"Delegation event sequence is malformed: {delegation_id}"
            raise ValueError(msg)
        return self.last_sequence + 1


@dataclass(frozen=True)
class DelegationRecordOwner:
    """Resolve scoped workspaces and durably maintain delegation audit exports."""

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
        """Append one full, redacted event and refresh readable record views."""
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
        started_date = timestamp[:10]
        locator = DelegationRecordLocator(
            delegation_id=resolved_id,
            started_date=started_date,
            caller_agent_name=metadata.caller_agent_name,
            child_agent_name=metadata.child_agent_name,
            caller_execution_identity=caller_execution_identity,
            child_execution_identity=child_execution_identity,
        )
        handle = self._resolve_handle(locator)
        with _record_lock(handle), _record_directory(handle, create=True) as record_fd:
            if _record_entry_exists(record_fd, "run.json"):
                msg = f"Delegation record already exists: {resolved_id}"
                raise FileExistsError(msg)
            run = _DelegationRun.from_json(
                redact_sensitive_data(
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
                ),
            )
            _write_run(record_fd, run)
            state = _RecordState(run)
            _append_jsonl(
                record_fd,
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
            _cache_record_state(_record_key(handle), state)
            _write_record_views(handle, record_fd, run)
        return handle

    def _reopen(self, locator: DelegationRecordLocator) -> DelegationRecordHandle:
        handle = self._resolve_handle(locator)
        with _record_lock(handle), _record_directory(handle) as record_fd:
            _record_state(handle, record_fd)
        return handle

    def _append_event(self, handle: DelegationRecordHandle, event: DelegationEvent) -> None:
        handle = self._validated_handle(handle)
        with _record_lock(handle), _record_directory(handle) as record_fd:
            state = _record_state(handle, record_fd)
            if event.event_id is not None and state.has_event(event.event_id):
                _write_record_views(handle, record_fd, state.run)
                return
            _ensure_active(state.run)
            sequence = state.next_sequence(handle.locator.delegation_id)
            timestamp = event.timestamp or _utc_timestamp()
            redacted_data = _redacted_event_data(
                record_fd,
                sequence=sequence,
                data=event.data,
            )
            payload = _event_payload(
                sequence=sequence,
                timestamp=timestamp,
                kind=event.kind,
                data=redacted_data,
                status=event.status,
                event_id=event.event_id,
            )
            _append_jsonl(record_fd, state, payload)
            _write_record_views(handle, record_fd, state.run)

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
        with _record_lock(handle), _record_directory(handle) as record_fd:
            state = _record_state(handle, record_fd)
            if state.run.status != status:
                _ensure_active(state.run)
                sequence = state.next_sequence(handle.locator.delegation_id)
                terminal_data = _redacted_event_data(
                    record_fd,
                    sequence=sequence,
                    data={
                        "status": status,
                        "output": output,
                        "error": error,
                        "usage": usage,
                    },
                )
                event = _event_payload(
                    sequence=sequence,
                    timestamp=_utc_timestamp(),
                    kind="delegation_finished",
                    data=terminal_data,
                    status=None,
                    event_id="delegation_finished",
                )
                _append_jsonl(record_fd, state, event, terminal=True)
            _write_record_views(handle, record_fd, state.run)
            # Rendering reads the whole log again, so only a finish, or a replayed one repairing it, renders it.
            _write_transcript(handle, record_fd, state.run)

    def _resolve_handle(self, locator: DelegationRecordLocator) -> DelegationRecordHandle:
        delegation_id = _validated_id(locator.delegation_id)
        started_date = _validated_date(locator.started_date)
        child_workspace = _resolve_workspace(
            locator.child_agent_name,
            config=self.config,
            runtime_paths=self.runtime_paths,
            execution_identity=locator.child_execution_identity,
        )
        caller_workspace = _resolve_workspace(
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
) -> Path:
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
        return agent_workspace_root_path(runtime_paths.storage_root, agent_name)
    return workspace.root


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _receipt_relative_path(locator: DelegationRecordLocator) -> Path:
    return _RECEIPT_DIRECTORY / _validated_date(locator.started_date) / f"{_validated_id(locator.delegation_id)}.json"


def _record_key(handle: DelegationRecordHandle) -> Path:
    return handle.child_workspace / handle.scoped_path


@contextmanager
def _record_lock(handle: DelegationRecordHandle) -> Iterator[None]:
    """Serialize the writers of one record without taking a lock worker code could hold."""
    record_path = _record_key(handle)
    with _RECORDS_GUARD:
        lock = _RECORD_LOCKS.get(record_path)
        if lock is None:
            lock = threading.Lock()
            _RECORD_LOCKS[record_path] = lock
    with lock:
        yield


def _cache_record_state(key: Path, state: _RecordState) -> None:
    with _RECORDS_GUARD:
        _RECORD_STATES[key] = state
        _RECORD_STATES.move_to_end(key)
        while len(_RECORD_STATES) > _MAX_CACHED_RECORDS:
            _RECORD_STATES.popitem(last=False)


def _cached_record_state(key: Path) -> _RecordState | None:
    with _RECORDS_GUARD:
        state = _RECORD_STATES.get(key)
        if state is not None:
            _RECORD_STATES.move_to_end(key)
        return state


def _record_state(handle: DelegationRecordHandle, record_fd: int) -> _RecordState:
    """Return the record's view under its lock, reading the log only when this process holds no view of it.

    Only the primary writes the log, so a log whose identity differs from the one the
    primary last left was changed by worker code and is refused without being read.
    """
    key = _record_key(handle)
    state = _cached_record_state(key)
    if state is None:
        run = _load_run(handle, record_fd)
        _validate_run_identity(run, handle.locator)
        state = _fold_record(handle, record_fd, run)
        _cache_record_state(key, state)
        return state
    msg = f"Delegation event log was changed outside the primary: {handle.locator.delegation_id}"
    try:
        identity = _log_identity(os.stat("events.jsonl", dir_fd=record_fd, follow_symlinks=False))
    except OSError as exc:
        raise ValueError(msg) from exc
    if identity != state.log_identity:
        raise ValueError(msg)
    _validate_run_identity(state.run, handle.locator)
    return state


def _log_identity(status: os.stat_result) -> _LogIdentity:
    return (status.st_dev, status.st_ino, status.st_size, status.st_ctime_ns)


def _event_id_digest(event_id: str) -> bytes:
    # Fixed-size digests keep a planted log's long IDs from growing the cached view.
    return hashlib.blake2b(event_id.encode("utf-8", "surrogatepass"), digest_size=16).digest()


@contextmanager
def _record_directory(handle: DelegationRecordHandle, *, create: bool = False) -> Iterator[int]:
    """Pin the record directory by a no-follow walk from the child workspace."""
    if create:
        handle.child_workspace.mkdir(parents=True, exist_ok=True)
    with open_directory_within_root(handle.child_workspace, handle.scoped_path, create=create, mode=0o700) as record_fd:
        yield record_fd


def _record_entry_exists(record_fd: int, filename: str) -> bool:
    try:
        os.stat(filename, dir_fd=record_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _json_bytes(payload: object) -> bytes:
    # Compact, because indenting deeply nested data a worker planted would multiply its size by its depth.
    return (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def _string_field(run: Mapping[str, object], name: str) -> str:
    value = run.get(name)
    if not isinstance(value, str):
        msg = f"Delegation run field {name} must be a string"
        raise ValueError(msg)  # noqa: TRY004 - callers handle one error type for every unusable record
    return value


def _optional_string_field(run: Mapping[str, object], name: str) -> str | None:
    if name not in run:
        msg = f"Delegation run field {name} is missing"
        raise ValueError(msg)
    value = run[name]
    if value is not None and not isinstance(value, str):
        msg = f"Delegation run field {name} must be a string or null"
        raise ValueError(msg)
    return value


def _inline_field(run: Mapping[str, object], name: str) -> _JsonValue:
    if name not in run:
        msg = f"Delegation run field {name} is missing"
        raise ValueError(msg)
    return _inline_value(run[name])


def _inline_value(value: object) -> _JsonValue:
    """Return a value no larger than one the primary keeps inline; it moves larger ones to artifacts."""
    if len(json.dumps(value, sort_keys=True).encode("utf-8")) > _MAX_INLINE_VALUE_BYTES:
        msg = "Delegation record value exceeds its size limit"
        raise ValueError(msg)
    return cast("_JsonValue", value)


def _write_run(record_fd: int, run: _DelegationRun) -> None:
    payload = _json_bytes(run.to_json())
    if len(payload) > _MAX_RUN_BYTES:
        # Refused before writing, so run.json never holds what its reader refuses.
        msg = "Delegation record exceeds its size limit"
        raise ValueError(msg)
    atomic_write_bytes_at(record_fd, "run.json", payload)


def _append_jsonl(
    record_fd: int,
    state: _RecordState,
    payload: Mapping[str, object],
    *,
    terminal: bool = False,
) -> None:
    """Commit one event and advance the record's view to the log this append left."""
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    log_limit = _MAX_EVENT_LOG_BYTES if terminal else _MAX_EVENT_LOG_BYTES - _FINISH_EVENT_HEADROOM_BYTES
    event_limit = _MAX_EVENTS if terminal else _MAX_EVENTS - 1
    descriptor = open_regular_file_at(record_fd, "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        committed = os.fstat(descriptor).st_size
        if len(line) > _MAX_EVENT_LINE_BYTES or committed + len(line) > log_limit or state.event_count >= event_limit:
            # Refused before writing, so the log never holds what its readers refuse.
            msg = "Delegation event exceeds its size limit"
            raise DelegationRecordLimitError(msg)
        try:
            remaining = memoryview(line)
            while remaining:
                remaining = remaining[os.write(descriptor, remaining) :]
            os.fsync(descriptor)
        except BaseException:
            # Like the rewrite this append replaces, a failed append leaves only committed events.
            os.ftruncate(descriptor, committed)
            raise
        finally:
            # Either way the view must match the log this descriptor left behind.
            state.log_identity = _log_identity(os.fstat(descriptor))
    finally:
        os.close(descriptor)
    state.observe(payload)


def _load_run(handle: DelegationRecordHandle, record_fd: int) -> _DelegationRun:
    try:
        return _DelegationRun.from_json(
            json.loads(read_regular_file_within_root(record_fd, "run.json", max_bytes=_MAX_RUN_BYTES)),
        )
    except FileNotFoundError:
        raise
    except (OSError, RecursionError, ValueError) as exc:
        msg = f"Delegation record is unreadable: {handle.locator.delegation_id}"
        raise ValueError(msg) from exc


def _validate_run_identity(run: _DelegationRun, locator: DelegationRecordLocator) -> None:
    expected = (locator.delegation_id, locator.caller_agent_name, locator.child_agent_name, locator.started_date)
    actual = (run.delegation_id, run.metadata.caller_agent_name, run.metadata.child_agent_name, run.started_at[:10])
    if actual != expected:
        msg = f"Delegation record identity mismatch: {locator.delegation_id}"
        raise ValueError(msg)


def _ensure_active(run: _DelegationRun) -> None:
    if run.status in _TERMINAL_STATUSES:
        msg = f"Delegation record is already terminal: {run.delegation_id}"
        raise ValueError(msg)


def _fold_record(handle: DelegationRecordHandle, record_fd: int, run: _DelegationRun) -> _RecordState:
    """Fold the committed log into the record's view, one event at a time."""
    state = _RecordState(run)
    descriptor = _open_event_log(handle, record_fd)
    try:
        state.log_identity = _log_identity(os.fstat(descriptor))
        for event in _iter_events(handle, descriptor):
            state.observe(event)
        if _log_identity(os.fstat(descriptor)) != state.log_identity:
            msg = f"Delegation event log changed while it was read: {handle.locator.delegation_id}"
            raise ValueError(msg)
    finally:
        os.close(descriptor)
    return state


def _read_event_values(descriptor: int) -> Iterator[object]:
    """Parse the log one bounded line at a time, so no reader ever holds the whole worker-writable log."""
    if os.fstat(descriptor).st_size > _MAX_EVENT_LOG_BYTES:
        msg = "Delegation event log exceeds its size limit"
        raise ValueError(msg)
    count = consumed = 0
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        while line := stream.readline(_MAX_EVENT_LINE_BYTES + 1):
            # Worker code can keep appending while this reads, so the caps also hold for what was read.
            count += 1
            consumed += len(line)
            if len(line) > _MAX_EVENT_LINE_BYTES or consumed > _MAX_EVENT_LOG_BYTES or count > _MAX_EVENTS:
                msg = "Delegation event log exceeds its size limit"
                raise ValueError(msg)
            yield json.loads(line)


def _open_event_log(handle: DelegationRecordHandle, record_fd: int) -> int:
    try:
        return open_regular_file_at(record_fd, "events.jsonl")
    except (OSError, ValueError) as exc:
        msg = f"Delegation event stream is unreadable: {handle.locator.delegation_id}"
        raise ValueError(msg) from exc


def _iter_event_values(handle: DelegationRecordHandle, descriptor: int) -> Iterator[object]:
    try:
        yield from _read_event_values(descriptor)
    except (OSError, RecursionError, ValueError) as exc:
        msg = f"Delegation event stream is unreadable: {handle.locator.delegation_id}"
        raise ValueError(msg) from exc


def _iter_events(handle: DelegationRecordHandle, descriptor: int) -> Iterator[dict[str, object]]:
    for event in _iter_event_values(handle, descriptor):
        if not isinstance(event, dict):
            msg = f"Delegation event stream is malformed: {handle.locator.delegation_id}"
            raise ValueError(msg)  # noqa: TRY004 - callers handle one error type for every unusable record
        yield cast("dict[str, object]", event)


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


def _apply_event(run: _DelegationRun, event: Mapping[str, object]) -> None:
    """Fold one committed event into the run view."""
    sequence = event.get("sequence")
    timestamp = event.get("timestamp")
    if type(sequence) is int:
        run.event_count = sequence
    if isinstance(timestamp, str):
        run.updated_at = timestamp
    event_status = event.get("status")
    if event_status in {"running", "paused"}:
        run.status = cast("_DelegationActiveStatus", event_status)
    if event.get("kind") != "delegation_finished":
        return
    data = event.get("data")
    if not isinstance(data, dict):
        return
    terminal_data = cast("dict[str, object]", data)
    terminal_status = terminal_data.get("status")
    if terminal_status not in _TERMINAL_STATUSES:
        return
    run.status = cast("DelegationTerminalStatus", terminal_status)
    run.finished_at = timestamp if isinstance(timestamp, str) else None
    run.output = _inline_value(terminal_data.get("output"))
    run.error = _inline_value(terminal_data.get("error"))
    run.usage = _inline_value(terminal_data.get("usage"))


def _write_record_views(
    handle: DelegationRecordHandle,
    record_fd: int,
    run: _DelegationRun,
) -> None:
    """Rewrite the run summary and the caller's receipt."""
    _write_run(record_fd, run)
    _write_receipt(handle, run)


def _redacted_event_data(
    record_fd: int,
    *,
    sequence: int,
    data: Mapping[str, object],
) -> dict[str, _JsonValue]:
    redacted = redact_sensitive_data(data)
    if not isinstance(redacted, dict):
        return {"value": cast("_JsonValue", redacted)}
    materialized: dict[str, _JsonValue] = {}
    for index, (field_name, value) in enumerate(redacted.items(), start=1):
        encoded = json.dumps(value, sort_keys=True).encode("utf-8")
        if len(encoded) <= _MAX_INLINE_VALUE_BYTES:
            materialized[field_name] = value
            continue
        artifact_name = f"{sequence:06d}-{index:02d}-{_safe_artifact_label(field_name)}.json"
        artifact_relative_path = Path("artifacts") / artifact_name
        with open_directory_within_root(record_fd, "artifacts", create=True, mode=0o700) as artifacts_fd:
            atomic_write_bytes_at(artifacts_fd, artifact_name, encoded)
        materialized[field_name] = {
            "artifact_path": artifact_relative_path.as_posix(),
            "byte_count": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "oversized": True,
            "redacted": True,
        }
    return materialized


def _safe_artifact_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return (label or "value")[:64]


def _write_receipt(
    handle: DelegationRecordHandle,
    run: _DelegationRun,
) -> None:
    receipt = {
        "schema_version": _SCHEMA_VERSION,
        "delegation_id": handle.locator.delegation_id,
        "caller_agent_name": handle.locator.caller_agent_name,
        "child_agent_name": handle.locator.child_agent_name,
        "status": run.status,
        "record_reference": handle._record_reference,
        "started_at": run.started_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
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
    run: _DelegationRun,
) -> None:
    header = [
        f"# Delegation {run.delegation_id}",
        "",
        f"- Caller: {run.metadata.caller_agent_name}",
        f"- Child: {run.metadata.child_agent_name}",
        f"- Model: {run.metadata.model_name}",
        f"- Status: {run.status}",
        f"- Started: {run.started_at}",
        f"- Updated: {run.updated_at}",
        f"- Record: {run.record_reference}",
        "",
        "## Task",
        "",
        run.metadata.task,
        "",
        "## Events",
        "",
        "",
    ]
    descriptor = _open_event_log(handle, record_fd)
    try:
        with atomic_write_file_at(record_fd, "transcript.md") as transcript:
            transcript.write("\n".join(header).encode("utf-8"))
            for index, event in enumerate(_iter_events(handle, descriptor)):
                section = [
                    f"### {event.get('sequence')}. {event.get('kind')}",
                    "",
                    f"Timestamp: {event.get('timestamp')}",
                    "",
                    "```json",
                    # Compact for the same reason as run.json.
                    json.dumps(event.get("data"), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    "```",
                    "",
                ]
                separator = "\n" if index else ""
                transcript.write((separator + "\n".join(section)).encode("utf-8"))
    finally:
        os.close(descriptor)
