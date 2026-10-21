"""Approved tool calls that a scheduled task stores and its agent later runs by reference.

The scheduling agent names a function its run offers. When the task fires, the agent calls
``run_scheduled_call`` with the task ID, and the stored call runs through that same live function under the
run's tool-call policy, so the turn's worker routing, credentials, file access, output files, and tool hooks all
apply. The requester's approval is spent once, and the stored arguments run as stored unless any arguments were
approved. An agent build binds its run's functions to every call it runs (``bind_live_tools``); a scheduled call
outside an agent's own run finds nothing bound and runs nothing.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.approval.tool_approval import (
    POLICY_CONFIRMATION_APPROVAL_TYPE,
    claim_scheduled_call,
    record_scheduled_call_outcome,
    resolve_tool_approval_approver,
    scheduled_call,
)
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.json_utils import has_only_finite_numbers, object_with_unique_keys
from mindroom.tool_approval_grants import ANY_ARGUMENTS, canonical_arguments
from mindroom.tool_system.invocation import run_tool_call
from mindroom.tool_system.tool_access import validate_tool_arguments
from mindroom.tool_system.tool_hooks import SyncToolCompletionTracker, track_sync_tool_completion

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.event_journal import ScheduledCall, ScheduledCallRefusal
    from mindroom.tool_system.invocation import ToolCall, ToolCallContext, ToolMiddleware, ToolNext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

__all__ = [
    "LiveFunction",
    "bind_live_tools",
    "prepare_scheduled_call",
    "run_scheduled_call",
]

# The scheduler's own functions are never a scheduled call; its runner spends approvals itself.
_SCHEDULER_TOOLKIT = "scheduler"
# Parameters through which an entrypoint takes the run's media, which a scheduled call does not carry.
_MEDIA_PARAMETERS = frozenset({"images", "videos", "audios", "files"})

_REFUSALS: dict[ScheduledCallRefusal, str] = {
    "missing": "this task has no stored call",
    "elsewhere": "the call belongs to another agent, conversation, or requester",
    "withdrawn": "its approval was withdrawn when the task was cancelled",
    "not_approved": "the requester has not approved it",
    "not_armed": "its approval is not active; it activates when the task fires",
    "used": "its approval was already used",
    "late": "it is more than 15 minutes from its scheduled time",
    "arguments": "the approval covers only the stored arguments; call it without arguments_json",
    "left_room": "the room membership changed since the call was approved",
}


# Refusals after which the requester can still approve the same call the ordinary way.
_ASK_INSTEAD: frozenset[ScheduledCallRefusal] = frozenset({"not_approved", "not_armed", "late", "left_room"})


@dataclass(frozen=True, slots=True)
class LiveFunction:
    """One of a live agent's functions that a scheduled call can run."""

    toolkit_name: str
    function: ToolFunction
    # A tool that asks for its own confirmation is approved only for the exact call the requester saw.
    authored_confirmation: bool


@dataclass(frozen=True, slots=True)
class _LiveTools:
    """The functions one agent's run offers, their authored toolkits, and how the run calls them.

    ``middleware`` is the run's tool-call policy, and ``context`` is the run's context for the call that is
    running, so a stored call runs exactly as the model's own call of it would.
    """

    functions: Mapping[str, ToolFunction]
    owners: Mapping[str, str]
    middleware: tuple[ToolMiddleware, ...]
    context: ToolCallContext


_LIVE_TOOLS: ContextVar[_LiveTools | None] = ContextVar("mindroom_scheduled_call_live_tools", default=None)


def bind_live_tools(
    functions: Mapping[str, ToolFunction],
    owners: Mapping[str, str],
    middleware: Sequence[ToolMiddleware],
) -> tuple[ToolMiddleware, ...]:
    """Return ``middleware`` behind a first step that shows each call the run's functions, for scheduled calls.

    ``functions`` are the ones the run offers, by name, and ``owners`` maps each to its authored toolkit; a stored
    call runs under the returned policy, this step included.
    """
    policy: tuple[ToolMiddleware, ...]

    async def bind_live_functions(call: ToolCall, call_next: ToolNext) -> object:
        token = _LIVE_TOOLS.set(_LiveTools(functions=functions, owners=owners, middleware=policy, context=call.context))
        try:
            return await call_next(call.arguments)
        finally:
            _LIVE_TOOLS.reset(token)

    policy = (bind_live_functions, *middleware)
    return policy


def _parse_arguments(text: str) -> dict[str, object] | str:
    """Parse strict JSON object arguments, or return why they are not usable."""

    def reject_constant(name: str) -> object:
        msg = f"{name} is not JSON"
        raise ValueError(msg)

    try:
        arguments = json.loads(text, object_pairs_hook=object_with_unique_keys, parse_constant=reject_constant)
    except ValueError as exc:
        return f"arguments_json is not valid JSON ({exc})"
    if not isinstance(arguments, dict):
        return "arguments_json must be a JSON object of the tool's arguments"
    if not has_only_finite_numbers(arguments):
        return "arguments_json must not contain non-finite numbers"
    return arguments


def _resolve_live_function(
    live: _LiveTools | None,
    tool_name: str,
    toolkit_name: str | None = None,
) -> LiveFunction | str:
    """Find the function a scheduled call can run, the one the run offers by that name, or return why it cannot."""
    function = None if live is None else live.functions.get(tool_name)
    owner = None if live is None else live.owners.get(tool_name)
    if (
        function is None
        or owner is None
        or owner == _SCHEDULER_TOOLKIT
        or (toolkit_name is not None and owner != toolkit_name)
    ):
        return f"`{tool_name}` is not one of this agent's tools in this conversation"
    if function.external_execution or function.stop_after_tool_call or function.injected_params & _MEDIA_PARAMETERS:
        return f"`{tool_name}` needs live input or conversation control, so it cannot run as a scheduled call"
    return LiveFunction(
        toolkit_name=owner,
        function=function,
        authored_confirmation=function.requires_confirmation is True
        and function.approval_type != POLICY_CONFIRMATION_APPROVAL_TYPE,
    )


def prepare_scheduled_call(tool_name: str, arguments_json: str) -> tuple[LiveFunction, dict[str, object]] | str:
    """Resolve the function and arguments a new scheduled call stores, or return why it cannot."""
    resolved = _resolve_live_function(_LIVE_TOOLS.get(), tool_name)
    if isinstance(resolved, str):
        return _sentence(resolved)
    arguments = _parse_arguments(arguments_json)
    if isinstance(arguments, str):
        return _sentence(arguments)
    invalid = _invalid_arguments(resolved.function, arguments)
    if invalid is not None:
        return _sentence(invalid)
    return resolved, arguments


async def run_scheduled_call(  # noqa: PLR0911 - one refusal per broken condition
    context: ToolRuntimeContext,
    task_id: str,
    arguments_json: str | None,
) -> object:
    """Spend a scheduled task's approval once and run its stored call in this turn."""
    live = _LIVE_TOOLS.get()
    if live is None or context.agent_name not in context.config.agents:
        return _refused(task_id, "scheduled calls run only in their agent's own conversation turn")
    call = await scheduled_call(task_id)
    if call is None:
        return _refused(task_id, _REFUSALS["missing"])
    prepared = _prepare_run(context, call, live, arguments_json)
    if isinstance(prepared, str):
        return _refused(task_id, prepared)
    function, arguments_text, approver_id = prepared
    claimed = await claim_scheduled_call(call, arguments_json=arguments_text, approver_user_id=approver_id)
    if claimed is None:
        return _refused(task_id, "the approval runtime is not ready")
    if isinstance(claimed, str):
        if claimed in _ASK_INSTEAD and time.time_ns() >= call.execute_at_ns:
            # Once its time has come, the call can still go ahead the ordinary way, with an approval card now.
            return (
                f"{_refused(task_id, _REFUSALS[claimed])} To ask the requester to approve it now, call "
                f"`{call.tool_name}` with these arguments: {call.arguments_json}"
            )
        if claimed in _ASK_INSTEAD:
            return _refused(task_id, f"{_REFUSALS[claimed]}; it runs when its task fires")
        return _refused(task_id, _REFUSALS[claimed])
    # The approval is spent; a call interrupted from here leaves its outcome unknown and is never retried.
    try:
        result = await _execute(live, function, json.loads(arguments_text), call_id=f"scheduled-{task_id}")
    except Exception as error:
        await record_scheduled_call_outcome(task_id, "failed")
        return f"❌ Scheduled call `{task_id}` failed: {str(error) or 'the tool raised an error'}"
    await record_scheduled_call_outcome(task_id, "completed")
    return result


def _prepare_run(  # noqa: PLR0911 - one refusal per broken condition
    context: ToolRuntimeContext,
    call: ScheduledCall,
    live: _LiveTools,
    arguments_json: str | None,
) -> tuple[ToolFunction, str, str] | str:
    """Check a claim request before spending anything: who asks, the live function, and the arguments."""
    if (call.room_id, call.thread_id, call.requester_id, call.agent_name) != (
        context.room_id,
        context.resolved_thread_id,
        context.requester_id,
        context.agent_name,
    ):
        return _REFUSALS["elsewhere"]
    resolved = _resolve_live_function(live, call.tool_name, call.toolkit_name)
    if isinstance(resolved, str):
        return resolved
    arguments_text = call.arguments_json
    if arguments_json is not None:
        replacement = _parse_arguments(arguments_json)
        if isinstance(replacement, str):
            return replacement
        arguments_text = canonical_arguments(replacement)
        if arguments_text != call.arguments_json and (
            call.approved_scope != ANY_ARGUMENTS or resolved.authored_confirmation
        ):
            return _REFUSALS["arguments"]
    invalid = _invalid_arguments(resolved.function, json.loads(arguments_text))
    if invalid is not None:
        return invalid
    approver_id = resolve_tool_approval_approver(context.config, context.runtime_paths, call.requester_id)
    if approver_id is None:
        return "only a human requester's approval can run a scheduled call"
    return resolved.function, arguments_text, approver_id


def _invalid_arguments(function: ToolFunction, arguments: dict[str, object]) -> str | None:
    """Return why arguments do not fit the function's current schema, or None."""
    schema = dict(function.parameters)
    unknown = set(arguments) - set(schema.get("properties", {}))
    try:
        validate_tool_arguments(schema, arguments)
    except ValueError:
        return "the arguments do not match the tool's current parameters"
    # Tool entrypoints reject arguments they do not take, which would fail only after the approval is spent.
    if unknown and schema.get("additionalProperties") is not True:
        return "the arguments do not match the tool's current parameters"
    return None


async def _execute(live: _LiveTools, function: ToolFunction, arguments: dict[str, object], *, call_id: str) -> object:
    """Run one live function under the run's tool-call policy, owning a synchronous body until it finishes."""
    tracker = SyncToolCompletionTracker()

    async def settle() -> None:
        pending = tracker.started_task()
        if pending is not None:
            await pending

    try:
        with track_sync_tool_completion(tracker):
            return await run_tool_call(
                function,
                arguments,
                replace(live.context, call_id=call_id),
                middleware=live.middleware,
            )
    finally:
        await wait_for_future_until_complete(asyncio.create_task(settle(), name="scheduled-tool-call-cleanup"))


def _refused(task_id: str, reason: str) -> str:
    return f"❌ Scheduled call `{task_id}` did not run: {reason}."


def _sentence(reason: str) -> str:
    return f"❌ {reason}."
