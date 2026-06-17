"""Durable registry for concrete workspace materializations."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from mindroom.tool_system.worker_routing import (
    parse_tool_execution_identity_payload,
    serialize_tool_execution_identity,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity, WorkerScope

_REGISTRY_DIR_NAME = "workspace_instances"
_REGISTRY_FILE_NAME = "instances.json"
_REGISTRY_VERSION = 1
_REGISTRY_LOCK = threading.Lock()


@dataclass(frozen=True)
class WorkspaceInstanceRecord:
    """One durable concrete workspace materialization."""

    agent_name: str
    workspace_root: Path
    state_root: Path
    execution_scope: WorkerScope | None
    execution_identity: ToolExecutionIdentity | None
    worker_key: str | None
    is_private: bool


def workspace_instance_registry_path(runtime_paths: RuntimePaths) -> Path:
    """Return the durable workspace instance registry path."""
    return runtime_paths.storage_root / _REGISTRY_DIR_NAME / _REGISTRY_FILE_NAME


def load_workspace_instance_records(runtime_paths: RuntimePaths) -> list[WorkspaceInstanceRecord]:
    """Load valid workspace instance records, skipping malformed entries."""
    registry_path = workspace_instance_registry_path(runtime_paths)
    with _REGISTRY_LOCK:
        return _load_workspace_instance_records_unlocked(registry_path)


def record_workspace_instance(runtime_paths: RuntimePaths, record: WorkspaceInstanceRecord) -> None:
    """Add or replace one workspace instance record in the durable registry."""
    _validate_record_for_write(record)
    registry_path = workspace_instance_registry_path(runtime_paths)
    with _REGISTRY_LOCK:
        records_by_key = {
            _workspace_instance_registry_key(existing): existing
            for existing in _load_workspace_instance_records_unlocked(registry_path)
        }
        records_by_key[_workspace_instance_registry_key(record)] = record
        _write_workspace_instance_records_unlocked(registry_path, records_by_key)


def _validate_record_for_write(record: WorkspaceInstanceRecord) -> None:
    if not record.agent_name.strip():
        msg = "workspace instance agent_name must not be empty"
        raise ValueError(msg)
    if record.is_private:
        if record.execution_identity is None:
            msg = "private workspace instance records require an execution identity"
            raise ValueError(msg)
        if record.worker_key is None:
            msg = "private workspace instance records require a worker key"
            raise ValueError(msg)


def _workspace_instance_registry_key(record: WorkspaceInstanceRecord) -> str:
    worker_key = record.worker_key or "shared"
    key_material = "\0".join(
        (
            record.agent_name,
            str(record.workspace_root.expanduser().resolve()),
            worker_key,
        ),
    )
    return hashlib.sha256(key_material.encode("utf-8")).hexdigest()


def _load_workspace_instance_records_unlocked(registry_path: Path) -> list[WorkspaceInstanceRecord]:
    payload = _read_registry_payload(registry_path)
    raw_instances = payload.get("instances")
    if not isinstance(raw_instances, dict):
        return []

    records: list[WorkspaceInstanceRecord] = []
    for raw_record in raw_instances.values():
        record = _parse_workspace_instance_record(raw_record)
        if record is not None:
            records.append(record)
    return records


def _read_registry_payload(registry_path: Path) -> dict[str, object]:
    if not registry_path.exists():
        return {"version": _REGISTRY_VERSION, "instances": {}}
    try:
        raw_payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"version": _REGISTRY_VERSION, "instances": {}}
    if not isinstance(raw_payload, dict):
        return {"version": _REGISTRY_VERSION, "instances": {}}
    return cast("dict[str, object]", raw_payload)


def _parse_workspace_instance_record(payload: object) -> WorkspaceInstanceRecord | None:
    if not isinstance(payload, dict):
        return None
    try:
        return _build_workspace_instance_record(cast("dict[str, object]", payload))
    except (TypeError, ValueError):
        return None


def _build_workspace_instance_record(raw_payload: dict[str, object]) -> WorkspaceInstanceRecord:
    agent_name = _parse_required_string(raw_payload.get("agent_name"), field_name="agent_name")
    workspace_root = _parse_path(raw_payload.get("workspace_root"), field_name="workspace_root")
    state_root = _parse_path(raw_payload.get("state_root"), field_name="state_root")
    execution_scope = _parse_execution_scope(raw_payload.get("execution_scope"))
    execution_identity = _parse_execution_identity(raw_payload.get("execution_identity"))
    worker_key = _parse_optional_string(raw_payload.get("worker_key"), field_name="worker_key")
    is_private = _parse_required_bool(raw_payload.get("is_private"), field_name="is_private")
    if is_private and (
        execution_scope not in {"user", "user_agent"} or execution_identity is None or worker_key is None
    ):
        msg = "private workspace instance records require user scope, execution identity, and worker key"
        raise ValueError(msg)

    return WorkspaceInstanceRecord(
        agent_name=agent_name,
        workspace_root=workspace_root,
        state_root=state_root,
        execution_scope=execution_scope,
        execution_identity=execution_identity,
        worker_key=worker_key,
        is_private=is_private,
    )


def _parse_required_string(value: object, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"workspace instance {field_name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _parse_optional_string(value: object, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _parse_required_string(value, field_name=field_name)


def _parse_required_bool(value: object, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        msg = f"workspace instance {field_name} must be a bool"
        raise TypeError(msg)
    return value


def _parse_path(value: object, *, field_name: str) -> Path:
    return Path(_parse_required_string(value, field_name=field_name))


def _parse_execution_scope(value: object) -> WorkerScope | None:
    if value is None:
        return None
    if value not in {"shared", "user", "user_agent"}:
        msg = "workspace instance execution_scope must be shared, user, user_agent, or null"
        raise ValueError(msg)
    return cast("WorkerScope", value)


def _parse_execution_identity(value: object) -> ToolExecutionIdentity | None:
    if value is None:
        return None
    execution_identity = parse_tool_execution_identity_payload(
        value,
        strict=False,
        error_prefix="Workspace instance execution_identity",
    )
    if execution_identity is None:
        msg = "workspace instance execution_identity is malformed"
        raise ValueError(msg)
    return execution_identity


def _write_workspace_instance_records_unlocked(
    registry_path: Path,
    records_by_key: dict[str, WorkspaceInstanceRecord],
) -> None:
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": _REGISTRY_VERSION,
        "instances": {
            key: _serialize_workspace_instance_record(record) for key, record in sorted(records_by_key.items())
        },
    }
    temp_path: Path | None = None
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=registry_path.parent,
        prefix=f".{registry_path.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
        json.dump(payload, temp_file, indent=2, sort_keys=True)
        temp_file.write("\n")
    temp_path.replace(registry_path)


def _serialize_workspace_instance_record(record: WorkspaceInstanceRecord) -> dict[str, object]:
    return {
        "agent_name": record.agent_name,
        "workspace_root": str(record.workspace_root),
        "state_root": str(record.state_root),
        "execution_scope": record.execution_scope,
        "execution_identity": (
            serialize_tool_execution_identity(record.execution_identity)
            if record.execution_identity is not None
            else None
        ),
        "worker_key": record.worker_key,
        "is_private": record.is_private,
    }


__all__ = [
    "WorkspaceInstanceRecord",
    "load_workspace_instance_records",
    "record_workspace_instance",
    "workspace_instance_registry_path",
]
