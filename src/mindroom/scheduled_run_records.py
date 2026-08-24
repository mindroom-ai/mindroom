"""Machine-readable evidence for silent scheduled runs in agent workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import (
    agent_workspace_root_path,
    build_tool_execution_identity,
)
from mindroom.workspaces import resolve_workspace_relative_path

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import MessageEnvelope

_SCHEMA_VERSION = 1
_RUN_DIRECTORY = ".mindroom/scheduled_runs"


@dataclass(frozen=True)
class _ScheduledRunReceipt:
    """One agent-local view of a silent scheduled run."""

    schema_version: int
    source_event_id: str
    entity_name: str
    agent_name: str
    room_id: str
    thread_id: str | None
    prompt: str
    status: Literal["started", "completed"]
    result: Literal["reported", "no_report", "suppressed"] | None
    response_text: str | None
    started_at: str
    completed_at: str | None


@dataclass(frozen=True)
class _ExistingReceiptState:
    """Validated lifecycle state reused when rewriting one receipt."""

    status: Literal["started", "completed"]
    started_at: str


_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "source_event_id",
        "entity_name",
        "agent_name",
        "room_id",
        "thread_id",
        "prompt",
        "status",
        "result",
        "response_text",
        "started_at",
        "completed_at",
    },
)
_COMPLETED_RESULTS = frozenset({"reported", "no_report", "suppressed"})


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _receipt_path(workspace: Path, source_event_id: str) -> Path:
    source_digest = hashlib.sha256(source_event_id.encode("utf-8")).hexdigest()
    return resolve_workspace_relative_path(
        workspace,
        Path(_RUN_DIRECTORY) / f"{source_digest}.json",
        field_name="Silent schedule receipt",
    )


def _atomic_write_receipt(path: Path, receipt: _ScheduledRunReceipt) -> None:
    create_directory_durable(path.parent.parent, mode=0o700)
    create_directory_durable(path.parent, mode=0o700)
    write_json_file_durable(
        path,
        asdict(receipt),
        strict_atomic_replace=True,
        indent=2,
        sort_keys=True,
        trailing_newline=True,
    )


def _agent_names(config: Config, entity_name: str) -> tuple[str, ...]:
    team = config.teams.get(entity_name)
    if team is not None:
        return tuple(dict.fromkeys(team.agents))
    return (entity_name,)


def _workspace_for_agent(
    agent_name: str,
    *,
    entity_name: str,
    envelope: MessageEnvelope,
    config: Config,
    runtime_paths: RuntimePaths,
) -> Path:
    if agent_name not in config.agents:
        return agent_workspace_root_path(runtime_paths.storage_root, agent_name)
    target = envelope.target
    execution_identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=agent_name,
        transport_agent_name=entity_name,
        runtime_paths=runtime_paths,
        requester_id=envelope.requester_id,
        room_id=target.room_id,
        thread_id=target.resolved_thread_id,
        resolved_thread_id=target.resolved_thread_id,
        session_id=target.session_id,
    )
    resolved = resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        create=True,
    )
    if resolved.workspace is not None:
        return resolved.workspace.root
    return agent_workspace_root_path(runtime_paths.storage_root, agent_name)


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return False
    canonical = parsed.isoformat().replace("+00:00", "Z")
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed) and value == canonical


def _receipt_state_is_valid(payload: dict[object, object]) -> bool:
    if payload["status"] == "started":
        return all(payload[field] is None for field in ("result", "response_text", "completed_at"))
    return (
        payload["status"] == "completed"
        and isinstance(payload["result"], str)
        and payload["result"] in _COMPLETED_RESULTS
        and isinstance(payload["response_text"], str)
        and _is_utc_timestamp(payload["completed_at"])
    )


def _existing_receipt_state(path: Path, expected: _ScheduledRunReceipt) -> _ExistingReceiptState | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.keys() != _RECEIPT_FIELDS:
        return None
    identity = (
        payload["schema_version"],
        payload["source_event_id"],
        payload["entity_name"],
        payload["agent_name"],
        payload["room_id"],
        payload["thread_id"],
        payload["prompt"],
    )
    expected_identity = (
        expected.schema_version,
        expected.source_event_id,
        expected.entity_name,
        expected.agent_name,
        expected.room_id,
        expected.thread_id,
        expected.prompt,
    )
    if (
        type(payload["schema_version"]) is not int
        or identity != expected_identity
        or not _is_utc_timestamp(payload["started_at"])
        or not _receipt_state_is_valid(payload)
    ):
        return None
    return _ExistingReceiptState(status=payload["status"], started_at=payload["started_at"])


def _write_started_receipts(
    *,
    entity_name: str,
    envelope: MessageEnvelope,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    started_at = _utc_timestamp()
    for agent_name in _agent_names(config, entity_name):
        workspace = _workspace_for_agent(
            agent_name,
            entity_name=entity_name,
            envelope=envelope,
            config=config,
            runtime_paths=runtime_paths,
        )
        path = _receipt_path(workspace, envelope.source_event_id)
        receipt = _ScheduledRunReceipt(
            schema_version=_SCHEMA_VERSION,
            source_event_id=envelope.source_event_id,
            entity_name=entity_name,
            agent_name=agent_name,
            room_id=envelope.room_id,
            thread_id=envelope.target.resolved_thread_id,
            prompt=envelope.body,
            status="started",
            result=None,
            response_text=None,
            started_at=started_at,
            completed_at=None,
        )
        if existing := _existing_receipt_state(path, receipt):
            receipt = replace(receipt, started_at=existing.started_at)
            if existing.status == "started":
                continue
        _atomic_write_receipt(path, receipt)


def _write_completed_receipts(
    *,
    entity_name: str,
    envelope: MessageEnvelope,
    config: Config,
    runtime_paths: RuntimePaths,
    result: Literal["reported", "no_report", "suppressed"],
    response_text: str,
) -> None:
    completed_at = _utc_timestamp()
    for agent_name in _agent_names(config, entity_name):
        workspace = _workspace_for_agent(
            agent_name,
            entity_name=entity_name,
            envelope=envelope,
            config=config,
            runtime_paths=runtime_paths,
        )
        path = _receipt_path(workspace, envelope.source_event_id)
        receipt = _ScheduledRunReceipt(
            schema_version=_SCHEMA_VERSION,
            source_event_id=envelope.source_event_id,
            entity_name=entity_name,
            agent_name=agent_name,
            room_id=envelope.room_id,
            thread_id=envelope.target.resolved_thread_id,
            prompt=envelope.body,
            status="completed",
            result=result,
            response_text=response_text,
            started_at=completed_at,
            completed_at=completed_at,
        )
        if existing := _existing_receipt_state(path, receipt):
            receipt = replace(receipt, started_at=existing.started_at)
        _atomic_write_receipt(path, receipt)


async def record_silent_schedule_result_if_needed(
    *,
    entity_name: str,
    envelope: MessageEnvelope,
    config: Config,
    runtime_paths: RuntimePaths,
    suppression_reason: str | None,
    response_text: str,
) -> None:
    """Persist the final result only when the response is a silent schedule."""
    if envelope.source_kind != SILENT_SCHEDULE_SOURCE_KIND:
        return
    result: Literal["reported", "no_report", "suppressed"] = "reported"
    if suppression_reason == "silent_no_report":
        result = "no_report"
    elif suppression_reason is not None:
        result = "suppressed"
    await asyncio.to_thread(
        _write_completed_receipts,
        entity_name=entity_name,
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
        result=result,
        response_text=response_text,
    )


async def record_silent_schedule_started_if_needed(
    *,
    entity_name: str,
    envelope: MessageEnvelope,
    config: Config,
    runtime_paths: RuntimePaths,
) -> None:
    """Create an idempotent start receipt only for a silent schedule."""
    if envelope.source_kind != SILENT_SCHEDULE_SOURCE_KIND:
        return
    await asyncio.to_thread(
        _write_started_receipts,
        entity_name=entity_name,
        envelope=envelope,
        config=config,
        runtime_paths=runtime_paths,
    )
