"""Approved tool calls that a scheduled task stores and its agent later runs by reference.

The scheduling agent names a function on one of its own configured toolkits. When the
task fires, the agent calls ``run_scheduled_call`` with the task ID, and the stored
call runs through that same live function, so the turn's worker routing, credentials,
file access, output files, and tool hooks all apply. The requester's approval is spent
once, and the stored arguments run as stored unless any arguments were approved.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.tools.function import Function, entrypoint_accepts_media
from agno.tools.toolkit import Toolkit

from mindroom.agno_compat_prepared_tools import OwnedAgentFunctionCall, prepare_live_agent_function
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.tool_approval import (
    POLICY_CONFIRMATION_APPROVAL_TYPE,
    claim_scheduled_call,
    record_scheduled_call_outcome,
    resolve_tool_approval_approver,
    scheduled_call,
)
from mindroom.tool_approval_grants import ANY_ARGUMENTS, canonical_arguments
from mindroom.tool_system.tool_access import function_schema, validate_tool_arguments
from mindroom.tool_system.tool_hooks import SyncToolCompletionTracker, track_sync_tool_completion

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.run import RunContext
    from agno.tools.function import FunctionExecutionResult

    from mindroom.event_journal import ScheduledCall, ScheduledCallRefusal
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

__all__ = [
    "LiveFunction",
    "prepare_scheduled_call",
    "run_scheduled_call",
]

_REFUSALS: dict[ScheduledCallRefusal, str] = {
    "missing": "this task has no stored call",
    "elsewhere": "the call belongs to another agent, conversation, or requester",
    "withdrawn": "its approval was withdrawn when the task was cancelled or edited",
    "not_approved": "the requester has not approved it",
    "not_armed": "its approval is not active; it activates when the task fires",
    "used": "its approval was already used",
    "late": "it is more than 15 minutes from its scheduled time",
    "arguments": "the approval covers only the stored arguments; call it without arguments_json",
    "left_room": "the room membership changed since the call was approved",
}


# Refusals after which the requester can still approve the same call the ordinary way, such as after an edit.
_ASK_INSTEAD: frozenset[ScheduledCallRefusal] = frozenset(
    {"withdrawn", "not_approved", "not_armed", "late", "left_room"},
)


@dataclass(frozen=True, slots=True)
class LiveFunction:
    """One of a live agent's functions that a scheduled call can run."""

    toolkit_name: str
    function: Function
    # A tool that asks for its own confirmation is approved only for the exact call the requester saw.
    authored_confirmation: bool


def _parse_arguments(text: str) -> dict[str, object] | str:
    """Parse strict JSON object arguments, or return why they are not usable."""

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [key for key, _value in pairs]
        if len(keys) != len(set(keys)):
            msg = "duplicate key"
            raise ValueError(msg)
        return dict(pairs)

    def reject_constant(name: str) -> object:
        msg = f"{name} is not JSON"
        raise ValueError(msg)

    try:
        arguments = json.loads(text, object_pairs_hook=reject_duplicates, parse_constant=reject_constant)
    except ValueError as exc:
        return f"arguments_json is not valid JSON ({exc})"
    if not isinstance(arguments, dict):
        return "arguments_json must be a JSON object of the tool's arguments"
    if not _finite(arguments):
        return "arguments_json must not contain non-finite numbers"
    return arguments


def _resolve_live_function(agent: Agent, tool_name: str, toolkit_name: str | None = None) -> LiveFunction | str:
    """Find the one configured function a scheduled call can run, or return why it cannot."""
    candidates: dict[str, Function] = {}
    # MindRoom builds each turn's tools as a list of configured toolkits.
    for tool in agent.tools if isinstance(agent.tools, list) else ():
        function = tool.get_async_functions().get(tool_name) if isinstance(tool, Toolkit) else None
        if function is None or function.owning_toolkit is None:
            continue
        if toolkit_name is None or function.owning_toolkit == toolkit_name:
            candidates.setdefault(function.owning_toolkit, function)
    if not candidates:
        return f"`{tool_name}` is not one of this agent's tools in this conversation"
    if len(candidates) > 1:
        return f"more than one of this agent's tools is named `{tool_name}`"
    [(owner, function)] = candidates.items()
    if (
        function.requires_user_input
        or function.external_execution
        or function.stop_after_tool_call
        or (function.entrypoint is not None and entrypoint_accepts_media(function.entrypoint))
    ):
        return f"`{tool_name}` needs live input or conversation control, so it cannot run as a scheduled call"
    return LiveFunction(
        toolkit_name=owner,
        function=function,
        authored_confirmation=function.requires_confirmation is True
        and function.approval_type != POLICY_CONFIRMATION_APPROVAL_TYPE,
    )


def prepare_scheduled_call(
    agent: Agent,
    tool_name: str,
    arguments_json: str,
) -> tuple[LiveFunction, dict[str, object]] | str:
    """Resolve the function and arguments a new scheduled call stores, or return why it cannot."""
    live = _resolve_live_function(agent, tool_name)
    if isinstance(live, str):
        return _sentence(live)
    arguments = _parse_arguments(arguments_json)
    if isinstance(arguments, str):
        return _sentence(arguments)
    invalid = _invalid_arguments(live.function, arguments)
    if invalid is not None:
        return _sentence(invalid)
    return live, arguments


async def run_scheduled_call(  # noqa: PLR0911 - one refusal per broken condition
    context: ToolRuntimeContext,
    agent: Agent | None,
    run_context: RunContext | None,
    task_id: str,
    arguments_json: str | None,
) -> object:
    """Spend a scheduled task's approval once and run its stored call in this turn."""
    if agent is None or run_context is None or context.agent_name not in context.config.agents:
        return _refused(task_id, "scheduled calls run only in their agent's own conversation turn")
    call = await scheduled_call(task_id)
    if call is None:
        return _refused(task_id, _REFUSALS["missing"])
    prepared = _prepare_run(context, call, agent, arguments_json)
    if isinstance(prepared, str):
        return _refused(task_id, prepared)
    live, arguments_text, approver_id = prepared
    claimed = await claim_scheduled_call(call, arguments_json=arguments_text, approver_user_id=approver_id)
    if claimed is None:
        return _refused(task_id, "the approval runtime is not ready")
    if isinstance(claimed, str):
        if claimed in _ASK_INSTEAD:
            # The call can still go ahead the ordinary way, with an approval card now.
            return (
                f"{_refused(task_id, _REFUSALS[claimed])} To ask the requester to approve it now, call "
                f"`{call.tool_name}` with these arguments: {call.arguments_json}"
            )
        return _refused(task_id, _REFUSALS[claimed])
    # The approval is spent; a call interrupted from here leaves its outcome unknown and is never retried.
    execution = await _execute(
        agent,
        run_context,
        live.function,
        json.loads(arguments_text),
        call_id=f"scheduled-{task_id}",
    )
    if execution.status == "success" and not isinstance(execution.result, Iterator | AsyncIterator):
        await record_scheduled_call_outcome(task_id, "completed")
        return execution.result
    await record_scheduled_call_outcome(task_id, "failed")
    if execution.status == "success":
        return f"❌ Scheduled call `{task_id}` failed: `{call.tool_name}` streams its result, which a scheduled call cannot."
    return f"❌ Scheduled call `{task_id}` failed: {execution.error or 'the tool raised an error'}"


def _prepare_run(  # noqa: PLR0911 - one refusal per broken condition
    context: ToolRuntimeContext,
    call: ScheduledCall,
    agent: Agent,
    arguments_json: str | None,
) -> tuple[LiveFunction, str, str] | str:
    """Check a claim request before spending anything: who asks, the live function, and the arguments."""
    if (call.room_id, call.thread_id, call.requester_id, call.agent_name) != (
        context.room_id,
        context.resolved_thread_id,
        context.requester_id,
        context.agent_name,
    ):
        return _REFUSALS["elsewhere"]
    live = _resolve_live_function(agent, call.tool_name, call.toolkit_name)
    if isinstance(live, str):
        return live
    arguments_text = call.arguments_json
    if arguments_json is not None:
        replacement = _parse_arguments(arguments_json)
        if isinstance(replacement, str):
            return replacement
        arguments_text = canonical_arguments(replacement)
        if arguments_text != call.arguments_json and (
            call.approved_scope != ANY_ARGUMENTS or live.authored_confirmation
        ):
            return _REFUSALS["arguments"]
    invalid = _invalid_arguments(live.function, json.loads(arguments_text))
    if invalid is not None:
        return invalid
    approver_id = resolve_tool_approval_approver(context.config, context.runtime_paths, call.requester_id)
    if approver_id is None:
        return "only a human requester's approval can run a scheduled call"
    return live, arguments_text, approver_id


def _invalid_arguments(function: Function, arguments: dict[str, object]) -> str | None:
    """Return why arguments do not fit the function's current schema, or None."""
    schema = function_schema(function)
    unknown = set(arguments) - set(schema.get("properties", {}))
    try:
        validate_tool_arguments(schema, arguments)
    except ValueError:
        return "the arguments do not match the tool's current parameters"
    # Tool entrypoints reject arguments they do not take, which would fail only after the approval is spent.
    if unknown and schema.get("additionalProperties") is not True:
        return "the arguments do not match the tool's current parameters"
    return None


async def _execute(
    agent: Agent,
    run_context: RunContext,
    function: Function,
    arguments: dict[str, object],
    *,
    call_id: str,
) -> FunctionExecutionResult:
    """Run one prepared function with its hooks, owning a synchronous body until it finishes."""
    prepared = prepare_live_agent_function(agent, function, run_context)
    # A spent approval runs the tool body; a cached result would skip it and its hooks.
    prepared.cache_results = False
    call = OwnedAgentFunctionCall(function=prepared, call_id=call_id, arguments=arguments)
    tracker = SyncToolCompletionTracker()

    async def settle() -> None:
        await call.close_result()
        pending = tracker.started_task()
        if pending is not None:
            await pending

    try:
        with track_sync_tool_completion(tracker):
            return await call.aexecute()
    finally:
        await wait_for_future_until_complete(asyncio.create_task(settle(), name="scheduled-tool-call-cleanup"))


def _refused(task_id: str, reason: str) -> str:
    return f"❌ Scheduled call `{task_id}` did not run: {reason}."


def _sentence(reason: str) -> str:
    return f"❌ {reason}."


def _finite(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    return True
