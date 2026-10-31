"""Adapt durable delegated child state and the agent loop's events into workspace records."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from mindroom.ai_run_metadata import ReplyUsage, turn_usage
from mindroom.delegation.records import (
    DelegationEvent,
    DelegationMetadata,
    DelegationRecordHandle,
    DelegationRecordLocator,
    DelegationRecordOwner,
)
from mindroom.delegation.state import TERMINAL_CHILD_STATUSES, child_execution_identity
from mindroom.llm.events import RunFinished, TextDelta, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunRecord, RunStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild, DelegationTerminalStatus
    from mindroom.event_journal import ApprovalCall
    from mindroom.llm.events import ReportedCall
    from mindroom.llm.usage import RunMetrics
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@dataclass
class ChildAudit:
    """One live child stream's record owner, buffered visible content, and its count of numbered events.

    The loop's events carry no index, so the audit numbers them as they arrive; buffered content is recorded under
    the number of its last chunk. The count starts past the highest index the record already holds for the run, so a
    run resumed in a later context never repeats the ids of an earlier leg, which the record would drop.
    """

    child: DelegationChild
    owner: DelegationRecordOwner
    locator: DelegationRecordLocator
    observed: int
    pending_content: list[tuple[int, str]] = field(default_factory=list)


async def start_child_record(
    child: DelegationChild,
    *,
    parent_run_id: str | None,
    config: Config,
    runtime_paths: RuntimePaths,
    caller_execution_identity: ToolExecutionIdentity | None,
    parent_delegation_id: str | None,
) -> DelegationRecordLocator:
    """Start an audit projection and return its locator to the lifecycle owner."""
    child_identity = child_execution_identity(child)
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


async def open_child_audit(
    child: DelegationChild,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> ChildAudit:
    """Reopen the child's record to audit one live stream, numbering past the events it holds for the run."""
    owner, handle = await _open_record(child, config=config, runtime_paths=runtime_paths)
    return ChildAudit(
        child=child,
        owner=owner,
        locator=handle.locator,
        observed=await owner.highest_event_index(handle, _stream_prefix(child.run_id)),
    )


async def record_child_event(audit: ChildAudit, event: TextDelta | ToolStarted | ToolCompleted | RunFinished) -> None:
    """Record one child stream event without persisting token-sized deltas.

    A finished run's record is one the caller established is the child's current run's.
    """
    audit.observed += 1
    if isinstance(event, TextDelta):
        if event.text:
            audit.pending_content.append((audit.observed, event.text))
        return

    handle = await flush_child_audit(audit)
    run_id = audit.child.run_id
    if isinstance(event, RunFinished):
        assert isinstance(event.record, RunRecord)
        await _append_response_events(audit.owner, handle, event.record)
    elif isinstance(event, ToolStarted):
        await _append_tool_call(audit.owner, handle, event.call, run_id=run_id)
    else:
        await _append_tool_result(audit.owner, handle, event.call, run_id=run_id)


async def flush_child_audit(audit: ChildAudit) -> DelegationRecordHandle:
    """Record the visible content the stream buffered since its last recorded event; return the reopened record."""
    handle = await audit.owner.reopen(audit.locator)
    if audit.pending_content:
        chunks = audit.pending_content.copy()
        audit.pending_content.clear()
        await audit.owner.append_event(
            handle,
            DelegationEvent(
                kind="output",
                event_id=f"{_stream_prefix(audit.child.run_id)}{chunks[-1][0]}",
                data={"content": "".join(text for _, text in chunks)},
            ),
        )
    return handle


async def record_child_response(
    child: DelegationChild,
    response: RunRecord,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    approval_calls: Sequence[ApprovalCall],
) -> None:
    """Record one snapshot of the child's run, which its lifecycle owner checked."""
    owner, handle = await _open_record(child, config=config, runtime_paths=runtime_paths)
    pending_approval = await _append_response_events(
        owner,
        handle,
        response,
        approval_calls=approval_calls,
    )

    if response.status == RunStatus.paused and not pending_approval:
        await owner.append_event(
            handle,
            DelegationEvent(
                kind="status",
                event_id=f"run:{response.run_id}:paused:{len(response.tools or ())}",
                status="paused",
                data={"status": "paused"},
            ),
        )


async def _append_response_events(
    owner: DelegationRecordOwner,
    handle: DelegationRecordHandle,
    response: RunRecord,
    *,
    approval_calls: Sequence[ApprovalCall] = (),
) -> bool:
    """Persist one attempt snapshot, its usage that attempt's alone, and return whether it awaits an approval."""
    run_id = response.run_id
    for call in approval_calls:
        await owner.append_event(
            handle,
            DelegationEvent(
                kind="approval_decision",
                event_id=f"run:{run_id}:approval:{call.tool_call_id}:decision",
                status="running",
                data={
                    "tool_call_id": call.tool_call_id,
                    "approved": call.approved,
                    "reason": call.reason,
                },
            ),
        )

    pending_approval = False
    for tool in response.tools or ():
        await _append_tool_call(owner, handle, tool.call, run_id=run_id)
        if tool.result is not None or tool.is_error:
            await _append_tool_result(owner, handle, tool.call, run_id=run_id)
        if response.status == RunStatus.paused and tool.requires_confirmation:
            pending_approval = True
            await owner.append_event(
                handle,
                DelegationEvent(
                    kind="approval_requested",
                    event_id=_tool_event_id(run_id, tool.tool_call_id, "approval"),
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
    return pending_approval


def child_response_usage(response: RunRecord, prior_usage: Sequence[RunMetrics] = ()) -> dict[str, object] | None:
    """Return the usage of a child's run and ``prior_usage``, its turn's earlier runs', in the shape a reply publishes it."""
    metrics = turn_usage(prior_usage, response.metrics)
    if metrics is None:
        return None
    # The child's configured model is not at hand here; its run names the provider of each model it called.
    return ReplyUsage.of(
        metrics,
        provider=response.model_provider,
        configured_provider=None,
        model_id=response.model,
    ).to_wire()


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
    return f"{receipt}\nSubagent ID: {child.subagent_id}"


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


def _tool_event_id(run_id: str, tool_call_id: str, phase: str) -> str:
    return f"run:{run_id}:tool:{tool_call_id}:{phase}"


def _stream_prefix(run_id: str) -> str:
    """Return the id prefix of this run's events that the audit numbers: its buffered content."""
    return f"run:{run_id}:stream:"


async def _append_tool_call(
    owner: DelegationRecordOwner,
    handle: DelegationRecordHandle,
    call: ReportedCall,
    *,
    run_id: str,
) -> None:
    await owner.append_event(
        handle,
        DelegationEvent(
            kind="tool_call",
            event_id=_tool_event_id(run_id, call.tool_call_id, "call"),
            data={
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "arguments": call.tool_args or {},
            },
        ),
    )


async def _append_tool_result(
    owner: DelegationRecordOwner,
    handle: DelegationRecordHandle,
    call: ReportedCall,
    *,
    run_id: str,
) -> None:
    await owner.append_event(
        handle,
        DelegationEvent(
            kind="tool_result",
            event_id=_tool_event_id(run_id, call.tool_call_id, "result"),
            data={
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "result": call.result,
                "tool_call_error": call.is_error,
            },
        ),
    )


def _value_digest(value: object) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, default=repr).encode("utf-8")
    except BaseException:
        encoded = type(value).__name__.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]
