"""Adapt durable delegated child state and the owned loop's events into workspace records."""

from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from mindroom.delegation.records import (
    DelegationEvent,
    DelegationMetadata,
    DelegationRecordHandle,
    DelegationRecordLocator,
    DelegationRecordOwner,
)
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES
from mindroom.llm.events import ContentDelta, RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild, DelegationTerminalStatus
    from mindroom.llm.events import RunEvent
    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass
class _ChildAuditBinding:
    """One live child stream's record owner, buffered visible content, and its count of numbered events.

    The loop's events carry no index, so the binding numbers them as they arrive; the number identifies an event
    whose tool call has no id of its own. The count starts past the highest index the record already holds for the
    run, so a run resumed in a later context never repeats the ids of an earlier leg, which the record would drop.
    """

    child: DelegationChild
    owner: DelegationRecordOwner
    locator: DelegationRecordLocator
    observed: int
    pending_content: list[tuple[int, str]] = field(default_factory=list)


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
            persona=child.persona,
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
    owner = DelegationRecordOwner(config, runtime_paths)
    locator = _record_locator(child)
    handle = await owner.reopen(locator)
    binding = _ChildAuditBinding(
        child=child,
        owner=owner,
        locator=locator,
        observed=await owner.highest_event_index(handle, _stream_prefix(child.run_id)),
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


async def observe_child_event(event: RunEvent) -> None:
    """Record one child stream event without persisting token-sized deltas.

    The events are those of the child turn running in this context; the terminal record names the run it ended,
    and a record of another run is not this child's.
    """
    binding = _CHILD_AUDIT.get()
    if binding is None or not isinstance(event, ContentDelta | ToolStarted | ToolCompleted | RunFinished):
        return
    binding.observed += 1
    index = binding.observed
    if isinstance(event, ContentDelta):
        if event.text:
            binding.pending_content.append((index, event.text))
        return
    if isinstance(event, RunFinished) and (
        not isinstance(event.record, RunRecord)
        or event.record.run_id != binding.child.run_id
        or event.record.session_id != binding.child.session_id
    ):
        return

    handle = await binding.owner.reopen(binding.locator)
    await _flush_pending_content(binding, handle)
    run_id = binding.child.run_id
    if isinstance(event, RunFinished):
        assert isinstance(event.record, RunRecord)
        await _append_response_events(binding.owner, handle, event.record, run_id=run_id)
    elif isinstance(event, ToolStarted):
        await _append_tool_call(binding.owner, handle, event.tool, run_id=run_id, event_index=index)
    else:
        await _append_tool_result(binding.owner, handle, event.tool, run_id=run_id, event_index=index)


async def record_child_response(
    child: DelegationChild,
    response: RunRecord,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    decisions: Mapping[str, bool] | None = None,
    denial_reasons: Mapping[str, str | None] | None = None,
) -> dict[str, object] | None:
    """Record one snapshot of the child's run, which its lifecycle owner checked, and return its usage."""
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
    response: RunRecord,
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
        if tool.result is not None or tool.is_error:
            await _append_tool_result(owner, handle, tool, run_id=run_id)
        if response.status == RunStatus.paused and tool.requires_confirmation:
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

    # What the attempt hands over, as its parent reads it: a failed child's reason, not the text it wrote first.
    content = response.handover_content
    if content is not None:
        await owner.append_event(
            handle,
            DelegationEvent(
                kind="output",
                event_id=f"output:{run_id}:{_value_digest(content)}",
                data={"content": content},
            ),
        )
    usage = child_response_usage(response)
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


def child_response_usage(response: RunRecord) -> dict[str, object] | None:
    """Return the usage one child attempt reports."""
    return response.metrics.to_dict() if response.metrics is not None else None


async def finish_child_record(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    usage: dict[str, object] | None = None,
) -> str:
    """Idempotently project lifecycle-owned terminal state and usage."""
    owner, handle = await _open_record(child, config=config, runtime_paths=runtime_paths)
    if child.status not in TERMINAL_CHILD_STATUSES:
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


# TODO(simplify): This repeats lifecycle.child_execution_identity's check with its own error type and text. Take
# the identity from that one check. Nobody would notice: only the error for a mismatch no prepared child has changes.
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


def _tool_event_id(
    run_id: str,
    tool: ToolExecution,
    phase: str,
    event_index: int | None = None,
) -> str | None:
    if tool.tool_call_id:
        return f"run:{run_id}:tool:{tool.tool_call_id}:{phase}"
    if event_index is not None:
        return f"{_stream_prefix(run_id)}{event_index}"
    return None


def _stream_prefix(run_id: str) -> str:
    """Return the id prefix of this run's events that are numbered by the binding rather than by a tool call."""
    return f"run:{run_id}:stream:"


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
                "tool_call_error": tool.is_error,
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
    event_id = f"{_stream_prefix(binding.child.run_id)}{chunks[-1][0]}"
    content = "".join(text for _, text in chunks)
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
