"""Durable workspace audit records for delegated agent runs."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from uuid import uuid4

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.file_locks import advisory_file_lock_at
from mindroom.path_confinement import (
    open_directory_within_root,
    open_regular_file_at,
    open_regular_file_within_root,
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

_SCHEMA_VERSION = 1
_DELEGATION_DIRECTORY = Path(".mindroom/delegations")
_RECEIPT_DIRECTORY = Path(".mindroom/delegation_receipts")
_MAX_INLINE_VALUE_BYTES = 64 * 1024
_LOCK_FILENAME = ".record.lock"
# Values above _MAX_INLINE_VALUE_BYTES already move to artifacts/, so real events stay far below these.
_MAX_EVENT_LINE_BYTES = 4 << 20
_MAX_EVENT_LOG_BYTES = 256 << 20
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "denied"})


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
        with _record_directory(handle, create=True) as record_fd, advisory_file_lock_at(record_fd, _LOCK_FILENAME):
            if _record_entry_exists(record_fd, "run.json"):
                msg = f"Delegation record already exists: {resolved_id}"
                raise FileExistsError(msg)
            run = cast(
                "dict[str, _JsonValue]",
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
            event = _event_payload(
                sequence=1,
                timestamp=timestamp,
                kind="delegation_started",
                data={},
                status="running",
                event_id="delegation_started",
            )
            _append_jsonl(record_fd, event)
            run["event_count"] = 1
            _write_record_views(handle, record_fd, run)
        return handle

    def _reopen(self, locator: DelegationRecordLocator) -> DelegationRecordHandle:
        handle = self._resolve_handle(locator)
        with _record_directory(handle) as record_fd:
            run = _load_run(handle, record_fd)
        _validate_run_identity(run, locator)
        return handle

    def _append_event(self, handle: DelegationRecordHandle, event: DelegationEvent) -> None:
        handle = self._validated_handle(handle)
        with _record_directory(handle) as record_fd, advisory_file_lock_at(record_fd, _LOCK_FILENAME):
            run = _load_run(handle, record_fd)
            _validate_run_identity(run, handle.locator)
            events = _load_events(handle, record_fd)
            _apply_event_log(run, events)
            if event.event_id is not None and any(existing.get("event_id") == event.event_id for existing in events):
                _write_record_views(handle, record_fd, run)
                return
            _ensure_active(run)
            sequence = _next_sequence(handle, events)
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
            _append_jsonl(record_fd, payload)
            run["event_count"] = sequence
            run["updated_at"] = timestamp
            if event.status is not None:
                run["status"] = event.status
            _write_record_views(handle, record_fd, run)

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
        with _record_directory(handle) as record_fd, advisory_file_lock_at(record_fd, _LOCK_FILENAME):
            run = _load_run(handle, record_fd)
            _validate_run_identity(run, handle.locator)
            events = _load_events(handle, record_fd)
            _apply_event_log(run, events)
            if run.get("status") == status:
                _write_record_views(handle, record_fd, run)
                return
            _ensure_active(run)
            sequence = _next_sequence(handle, events)
            timestamp = _utc_timestamp()
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
                timestamp=timestamp,
                kind="delegation_finished",
                data=terminal_data,
                status=None,
                event_id="delegation_finished",
            )
            _append_jsonl(record_fd, event)
            run.update(
                {
                    "status": status,
                    "updated_at": timestamp,
                    "finished_at": timestamp,
                    "output": terminal_data["output"],
                    "error": terminal_data["error"],
                    "usage": terminal_data["usage"],
                    "event_count": sequence,
                },
            )
            _write_record_views(handle, record_fd, run)

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
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_run(record_fd: int, run: Mapping[str, object]) -> None:
    atomic_write_bytes_at(record_fd, "run.json", _json_bytes(run))


def _append_jsonl(record_fd: int, payload: Mapping[str, object]) -> None:
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    descriptor = open_regular_file_at(record_fd, "events.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        committed = os.fstat(descriptor).st_size
        if len(line) > _MAX_EVENT_LINE_BYTES or committed + len(line) > _MAX_EVENT_LOG_BYTES:
            # Refused before writing, so the log never holds what its readers refuse.
            msg = "Delegation event exceeds its size limit"
            raise ValueError(msg)
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
        os.close(descriptor)


def _load_run(handle: DelegationRecordHandle, record_fd: int) -> dict[str, _JsonValue]:
    try:
        payload = json.loads(read_regular_file_within_root(record_fd, "run.json"))
    except FileNotFoundError:
        raise
    except (OSError, ValueError) as exc:
        msg = f"Delegation record is unreadable: {handle.locator.delegation_id}"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"Delegation record is malformed: {handle.locator.delegation_id}"
        raise TypeError(msg)
    return cast("dict[str, _JsonValue]", payload)


def _validate_run_identity(
    run: Mapping[str, object],
    locator: DelegationRecordLocator,
) -> None:
    expected = (
        _SCHEMA_VERSION,
        locator.delegation_id,
        locator.caller_agent_name,
        locator.child_agent_name,
        locator.started_date,
    )
    started_at = run.get("started_at")
    actual = (
        run.get("schema_version"),
        run.get("delegation_id"),
        run.get("caller_agent_name"),
        run.get("child_agent_name"),
        started_at[:10] if isinstance(started_at, str) else None,
    )
    if actual != expected:
        msg = f"Delegation record identity mismatch: {locator.delegation_id}"
        raise ValueError(msg)


def _ensure_active(run: Mapping[str, object]) -> None:
    if run.get("status") in _TERMINAL_STATUSES:
        msg = f"Delegation record is already terminal: {run.get('delegation_id')}"
        raise ValueError(msg)


def _next_sequence(handle: DelegationRecordHandle, events: list[dict[str, object]]) -> int:
    if not events:
        return 1
    sequence = events[-1].get("sequence")
    if type(sequence) is not int:
        msg = f"Delegation event sequence is malformed: {handle.locator.delegation_id}"
        raise ValueError(msg)
    return sequence + 1


def _read_event_lines(descriptor: int) -> list[object]:
    if os.fstat(descriptor).st_size > _MAX_EVENT_LOG_BYTES:
        msg = "Delegation event log exceeds its size limit"
        raise ValueError(msg)
    events: list[object] = []
    with os.fdopen(os.dup(descriptor), "rb") as stream:
        while line := stream.readline(_MAX_EVENT_LINE_BYTES + 1):
            if len(line) > _MAX_EVENT_LINE_BYTES:
                msg = "Delegation event exceeds its size limit"
                raise ValueError(msg)
            events.append(json.loads(line))
    return events


def _load_events(handle: DelegationRecordHandle, record_fd: int) -> list[dict[str, object]]:
    try:
        with open_regular_file_within_root(record_fd, "events.jsonl") as descriptor:
            events = _read_event_lines(descriptor)
    except (OSError, ValueError) as exc:
        msg = f"Delegation event stream is unreadable: {handle.locator.delegation_id}"
        raise ValueError(msg) from exc
    if not all(isinstance(event, dict) for event in events):
        msg = f"Delegation event stream is malformed: {handle.locator.delegation_id}"
        raise ValueError(msg)
    return cast("list[dict[str, object]]", events)


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


def _apply_event_log(
    run: dict[str, _JsonValue],
    events: list[dict[str, object]],
) -> None:
    """Fold append-first durable events into the rebuildable run view."""
    for event in events:
        sequence = event.get("sequence")
        timestamp = event.get("timestamp")
        if type(sequence) is int:
            run["event_count"] = sequence
        if isinstance(timestamp, str):
            run["updated_at"] = timestamp
        event_status = event.get("status")
        if event_status in {"running", "paused"}:
            run["status"] = cast("_JsonValue", event_status)
        if event.get("kind") != "delegation_finished":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        terminal_data = cast("dict[str, object]", data)
        terminal_status = terminal_data.get("status")
        if terminal_status not in _TERMINAL_STATUSES:
            continue
        run.update(
            {
                "status": cast("_JsonValue", terminal_status),
                "finished_at": cast("_JsonValue", timestamp),
                "output": cast("_JsonValue", terminal_data.get("output")),
                "error": cast("_JsonValue", terminal_data.get("error")),
                "usage": cast("_JsonValue", terminal_data.get("usage")),
            },
        )


def _write_record_views(
    handle: DelegationRecordHandle,
    record_fd: int,
    run: Mapping[str, object],
) -> None:
    """Rewrite the run summary and both readable projections."""
    _write_run(record_fd, run)
    _write_transcript(handle, record_fd, run)
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
    run: Mapping[str, object],
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
    run: Mapping[str, object],
) -> None:
    events = _load_events(handle, record_fd)
    lines = [
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
        str(run["task"]),
        "",
        "## Events",
        "",
    ]
    for event in events:
        lines.extend(
            [
                f"### {event['sequence']}. {event['kind']}",
                "",
                f"Timestamp: {event['timestamp']}",
                "",
                "```json",
                json.dumps(event["data"], ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
            ],
        )
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_bytes_at(record_fd, "transcript.md", text.encode("utf-8"))
