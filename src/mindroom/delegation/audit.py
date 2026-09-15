"""Adapt durable delegated child state and Agno events into workspace records."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from agno.run.agent import (
    RunCancelledEvent,
    RunContentEvent,
    RunErrorEvent,
    RunOutput,
    ToolCallCompletedEvent,
    ToolCallErrorEvent,
    ToolCallStartedEvent,
)
from agno.run.base import RunStatus

from mindroom.delegation.records import (
    DelegationEvent,
    DelegationMetadata,
    DelegationRecordHandle,
    DelegationRecordLocator,
    DelegationRecordOwner,
    DelegationTerminalStatus,
)
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from agno.models.response import ToolExecution

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass
class _ChildAuditBinding:
    """One live child stream's record owner and buffered visible content."""

    child: DelegationChild
    owner: DelegationRecordOwner
    locator: DelegationRecordLocator
    pending_content: list[tuple[int | None, object]] = field(default_factory=list)


_CHILD_AUDIT: ContextVar[_ChildAuditBinding | None] = ContextVar(
    "delegation_child_audit",
    default=None,
)


async def start_child_record(
    child: DelegationChild,
    *,
    parent_run_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    caller_execution_identity: ToolExecutionIdentity | None,
    parent_delegation_id: str | None = None,
) -> DelegationRecordLocator:
    """Start an audit projection and return its locator to the lifecycle owner."""
    child_identity = _child_identity(child)
    handle = await DelegationRecordOwner(config, runtime_paths).start(
        DelegationMetadata(
            caller_agent_name=child.caller_agent_name,
            child_agent_name=child.child_agent_name,
            requester_id=child_identity.requester_id,
            parent_run_id=parent_run_id,
            parent_tool_call_id=child.parent_tool_call_id or None,
            source_room_id=child_identity.room_id,
            source_thread_id=child_identity.resolved_thread_id,
            model_name=child.model_name,
            task=child.task,
            parent_delegation_id=parent_delegation_id,
            subagent_id=child.subagent_id,
            previous_delegation_id=child.previous_delegation_id,
        ),
        caller_execution_identity=caller_execution_identity,
        child_execution_identity=child_identity,
        delegation_id=child.delegation_id,
    )
    return handle.locator


@asynccontextmanager
async def child_audit_context(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> AsyncIterator[None]:
    """Bind one child stream so the shared event observer can audit exact events."""
    binding = _ChildAuditBinding(
        child=child,
        owner=DelegationRecordOwner(config, runtime_paths),
        locator=_record_locator(child),
    )
    token = _CHILD_AUDIT.set(binding)
    try:
        yield
    finally:
        try:
            handle = await binding.owner.reopen(binding.locator)
            await _flush_pending_content(binding, handle)
        finally:
            _CHILD_AUDIT.reset(token)


async def observe_child_event(event: object) -> None:  # noqa: C901, PLR0911
    """Record one matching child stream event without persisting token-sized deltas."""
    binding = _CHILD_AUDIT.get()
    if binding is None or not isinstance(
        event,
        RunOutput
        | RunContentEvent
        | RunCancelledEvent
        | RunErrorEvent
        | ToolCallStartedEvent
        | ToolCallCompletedEvent
        | ToolCallErrorEvent,
    ):
        return
    if event.run_id != binding.child.run_id or event.session_id != binding.child.session_id:
        return
    if isinstance(event, RunContentEvent):
        if event.content is not None:
            binding.pending_content.append((event.event_index, event.content))
        return

    handle = await binding.owner.reopen(binding.locator)
    await _flush_pending_content(binding, handle)
    if isinstance(event, RunOutput):
        await _append_response_events(
            binding.owner,
            handle,
            event,
            run_id=binding.child.run_id,
        )
        return
    if isinstance(event, (RunCancelledEvent, RunErrorEvent)):
        return
    if isinstance(event, ToolCallStartedEvent):
        if event.tool is not None:
            await _append_tool_call(
                binding.owner,
                handle,
                event.tool,
                run_id=binding.child.run_id,
                event_index=event.event_index,
            )
        return
    if isinstance(event, ToolCallCompletedEvent):
        if event.tool is not None:
            await _append_tool_result(
                binding.owner,
                handle,
                event.tool,
                run_id=binding.child.run_id,
                event_index=event.event_index,
            )
        return
    if event.tool is not None:
        await _append_tool_result(
            binding.owner,
            handle,
            event.tool,
            run_id=binding.child.run_id,
            event_index=event.event_index,
            error=event.error,
        )


async def record_child_response(
    child: DelegationChild,
    response: RunOutput,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    decisions: Mapping[str, bool] | None = None,
    denial_reasons: Mapping[str, str | None] | None = None,
) -> dict[str, object] | None:
    """Record one retained attempt snapshot and return its usage without finishing the child."""
    _validate_response_identity(child, response)
    run_id = child.run_id
    owner, handle = await _open_record(child, config=config, runtime_paths=runtime_paths)
    usage, pending_approval = await _append_response_events(
        owner,
        handle,
        response,
        run_id=run_id,
        decisions=decisions,
        denial_reasons=denial_reasons,
    )

    if response.status == RunStatus.paused and not pending_approval:
        await owner.append_event(
            handle,
            DelegationEvent(
                kind="status",
                event_id=f"run:{run_id}:paused:{len(response.tools or ())}",
                status="paused",
                data={"status": "paused"},
            ),
        )
    return usage


async def _append_response_events(
    owner: DelegationRecordOwner,
    handle: DelegationRecordHandle,
    response: RunOutput,
    *,
    run_id: str,
    decisions: Mapping[str, bool] | None = None,
    denial_reasons: Mapping[str, str | None] | None = None,
) -> tuple[dict[str, object] | None, bool]:
    """Persist one attempt snapshot without deciding whether the envelope is terminal."""
    for tool_call_id, approved in (decisions or {}).items():
        await owner.append_event(
            handle,
            DelegationEvent(
                kind="approval_decision",
                event_id=f"run:{run_id}:approval:{tool_call_id}:decision",
                status="running",
                data={
                    "tool_call_id": tool_call_id,
                    "approved": approved,
                    "reason": (denial_reasons or {}).get(tool_call_id),
                },
            ),
        )

    pending_approval = False
    for tool in response.tools or ():
        await _append_tool_call(owner, handle, tool, run_id=run_id)
        if _tool_has_result(tool):
            await _append_tool_result(owner, handle, tool, run_id=run_id)
        if response.status == RunStatus.paused and _tool_waits_for_approval(tool):
            pending_approval = True
            await owner.append_event(
                handle,
                DelegationEvent(
                    kind="approval_requested",
                    event_id=_tool_event_id(run_id, tool, "approval"),
                    status="paused",
                    data={
                        "tool_call_id": tool.tool_call_id,
                        "tool_name": tool.tool_name,
                        "arguments": tool.tool_args or {},
                        "approval_type": tool.approval_type,
                    },
                ),
            )

    if response.content is not None:
        await owner.append_event(
            handle,
            DelegationEvent(
                kind="output",
                event_id=f"output:{run_id}:{_value_digest(response.content)}",
                data={"content": response.content},
            ),
        )
    usage = response.metrics.to_dict() if response.metrics is not None else None
    if usage:
        await owner.append_event(
            handle,
            DelegationEvent(
                kind="usage",
                event_id=f"usage:{run_id}:{_value_digest(usage)}",
                data=usage,
            ),
        )
    return usage, pending_approval


async def finish_child_record(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    usage: dict[str, object] | None = None,
) -> str:
    """Idempotently project lifecycle-owned terminal state and usage."""
    owner, handle = await _open_record(child, config=config, runtime_paths=runtime_paths)
    if child.status not in {"completed", "failed", "cancelled", "denied"}:
        msg = f"Cannot finish active delegation record: {child.delegation_id}"
        raise ValueError(msg)
    status = cast("DelegationTerminalStatus", child.status)
    result = child.result
    await owner.finish(
        handle,
        status=status,
        output=result if status == "completed" else None,
        error=result if status != "completed" else None,
        usage=usage,
    )
    receipt = handle.to_receipt()
    return f"{receipt}\nSubagent ID: {child.subagent_id}" if child.subagent_id is not None else receipt


def _child_identity(child: DelegationChild) -> ToolExecutionIdentity:
    identity = parse_tool_execution_identity_payload(
        child.execution_identity,
        strict=True,
        error_prefix="Delegation child execution identity",
    )
    if identity is None or identity.agent_name != child.child_agent_name or identity.session_id != child.session_id:
        msg = "Delegation child execution identity does not match its retained run"
        raise ValueError(msg)
    return identity


def _record_locator(child: DelegationChild) -> DelegationRecordLocator:
    if not child.record_locator:
        msg = f"Delegation child has no audit locator: {child.delegation_id}"
        raise ValueError(msg)
    return DelegationRecordLocator.from_dict(child.record_locator)


async def _open_record(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[DelegationRecordOwner, DelegationRecordHandle]:
    owner = DelegationRecordOwner(config, runtime_paths)
    return owner, await owner.reopen(_record_locator(child))


def _validate_response_identity(child: DelegationChild, response: RunOutput) -> None:
    if response.run_id != child.run_id or response.session_id != child.session_id:
        msg = "Delegation child response does not match its retained run"
        raise ValueError(msg)


def _tool_event_id(
    run_id: str,
    tool: ToolExecution,
    phase: str,
    event_index: int | None = None,
) -> str | None:
    if tool.tool_call_id:
        return f"run:{run_id}:tool:{tool.tool_call_id}:{phase}"
    if event_index is not None:
        return f"run:{run_id}:stream:{event_index}"
    return None


def _tool_has_result(tool: ToolExecution) -> bool:
    return tool.result is not None or bool(tool.tool_call_error)


def _tool_waits_for_approval(tool: ToolExecution) -> bool:
    return bool(tool.requires_confirmation or tool.requires_user_input)


async def _append_tool_call(
    owner: DelegationRecordOwner,
    handle: DelegationRecordHandle,
    tool: ToolExecution,
    *,
    run_id: str,
    event_index: int | None = None,
) -> None:
    await owner.append_event(
        handle,
        DelegationEvent(
            kind="tool_call",
            event_id=_tool_event_id(run_id, tool, "call", event_index),
            data={
                "tool_call_id": tool.tool_call_id,
                "tool_name": tool.tool_name,
                "arguments": tool.tool_args or {},
            },
        ),
    )


async def _append_tool_result(
    owner: DelegationRecordOwner,
    handle: DelegationRecordHandle,
    tool: ToolExecution,
    *,
    run_id: str,
    event_index: int | None = None,
    error: str | None = None,
) -> None:
    await owner.append_event(
        handle,
        DelegationEvent(
            kind="tool_result",
            event_id=_tool_event_id(run_id, tool, "result", event_index),
            data={
                "tool_call_id": tool.tool_call_id,
                "tool_name": tool.tool_name,
                "result": tool.result,
                "error": error,
                "tool_call_error": tool.tool_call_error,
            },
        ),
    )


async def _flush_pending_content(
    binding: _ChildAuditBinding,
    handle: DelegationRecordHandle,
) -> None:
    if not binding.pending_content:
        return
    chunks = binding.pending_content.copy()
    binding.pending_content.clear()
    event_indexes = [event_index for event_index, _ in chunks if event_index is not None]
    run_id = binding.child.run_id
    event_id = (
        f"run:{run_id}:stream:{event_indexes[-1]}" if event_indexes else f"run:{run_id}:output:{_value_digest(chunks)}"
    )
    content = "".join(str(value) for _, value in chunks)
    await binding.owner.append_event(
        handle,
        DelegationEvent(
            kind="output",
            event_id=event_id,
            data={"content": content},
        ),
    )


def _value_digest(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, default=repr).encode("utf-8")
    except BaseException:
        encoded = type(value).__name__.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]
