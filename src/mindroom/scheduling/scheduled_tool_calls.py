"""Approved tool calls that a scheduled task stores and its agent later runs by reference.

The scheduling agent names a function its run offers. When the task fires, the agent calls
``run_scheduled_call`` with the task ID, and the stored call runs through that same live function under the
run's tool-call policy, so the turn's worker routing, credentials, file access, output files, and tool hooks all
apply. The requester's approval is spent once, and the stored arguments run as stored unless any arguments were
approved. An agent build that offers the scheduler binds its run's functions to every call it runs
(``bind_live_tools``); a scheduled call outside such a run finds nothing bound and runs nothing.
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, NoReturn

from mindroom.approval import manager as approval_manager
from mindroom.approval.tool_approval import resolve_tool_approval_approver
from mindroom.json_utils import has_only_finite_numbers, object_with_unique_keys
from mindroom.scheduling.errors import SchedulingError
from mindroom.tool_approval_grants import ANY_ARGUMENTS, canonical_arguments
from mindroom.tool_system.invocation import run_tool_call
from mindroom.tool_system.tool_access import validate_tool_arguments
from mindroom.tool_system.tool_function import ToolGate, ToolOutcome
from mindroom.tool_system.tool_hooks import owned_sync_tool_completion

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
# The scheduler's functions that read the run's live functions: one stores a call to one, one runs it.
_LIVE_FUNCTION_READERS = frozenset({"schedule_tool_call", "run_scheduled_call"})

_RUNTIME_NOT_READY = "the approval runtime is not ready"
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
    """One of a live agent's functions that a scheduled call can run, which names the toolkit it is offered from."""

    function: ToolFunction
    # A tool that asks for its own confirmation is approved only for the exact call the requester saw.
    authored_confirmation: bool

    @property
    def toolkit_name(self) -> str:
        """Return the authored toolkit the run offers the function from."""
        assert self.function.toolkit is not None, "a scheduled call runs only a function of a configured toolkit"
        return self.function.toolkit


@dataclass(frozen=True, slots=True)
class _LiveTools:
    """The functions one agent's run offers and how the run calls them.

    ``middleware`` is the run's tool-call policy, and ``context`` is the run's context for the call that is
    running, so a stored call runs exactly as the model's own call of it would.
    """

    functions: Mapping[str, ToolFunction]
    middleware: tuple[ToolMiddleware, ...]
    context: ToolCallContext


_LIVE_TOOLS: ContextVar[_LiveTools | None] = ContextVar("mindroom_scheduled_call_live_tools", default=None)


def bind_live_tools(
    functions: Mapping[str, ToolFunction],
    middleware: Sequence[ToolMiddleware],
) -> tuple[ToolMiddleware, ...]:
    """Return ``middleware`` behind a first step that shows each call the run's functions, for scheduled calls.

    ``functions`` are the ones the run offers, by name; a generated function names no toolkit, so no stored call can
    name it; a stored call runs under the returned policy, this step included. A run that offers no scheduler
    function reading them gets ``middleware`` as it is.
    """
    if _LIVE_FUNCTION_READERS.isdisjoint(functions):
        return tuple(middleware)
    policy: tuple[ToolMiddleware, ...]

    async def bind_live_functions(call: ToolCall, call_next: ToolNext) -> object:
        token = _LIVE_TOOLS.set(_LiveTools(functions=functions, middleware=policy, context=call.context))
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
    if function is None or function.toolkit in {None, _SCHEDULER_TOOLKIT}:
        return f"`{tool_name}` is not one of this agent's tools in this conversation"
    if toolkit_name is not None and function.toolkit != toolkit_name:
        return f"`{tool_name}` now comes from another toolkit than the one the call was stored for"
    if function.gate is ToolGate.DELEGATION or function.controls_run:
        return f"`{tool_name}` needs live input or conversation control, so it cannot run as a scheduled call"
    return LiveFunction(
        function=function,
        authored_confirmation=function.gate is ToolGate.CONFIRM,
    )


def prepare_scheduled_call(tool_name: str, arguments_json: str) -> tuple[LiveFunction, dict[str, object]]:
    """Resolve the function and arguments a new scheduled call stores, or raise why it cannot."""
    resolved = _resolve_live_function(_LIVE_TOOLS.get(), tool_name)
    if isinstance(resolved, str):
        _raise_unschedulable(resolved)
    arguments = _parse_arguments(arguments_json)
    if isinstance(arguments, str):
        _raise_unschedulable(arguments)
    invalid = _invalid_arguments(resolved.function, arguments)
    if invalid is not None:
        _raise_unschedulable(invalid)
    return resolved, arguments


async def run_scheduled_call(  # noqa: PLR0911 - one refusal per broken condition
    context: ToolRuntimeContext,
    task_id: str,
    arguments_json: str | None,
) -> object:
    """Spend a scheduled task's approval once and run its stored call in this turn.

    Returns the stored call's own result, or a failed outcome saying why it did not run or how it failed.
    """
    live = _LIVE_TOOLS.get()
    if live is None or context.agent_name not in context.config.agents:
        return _refused(task_id, "scheduled calls run only in their agent's own conversation turn")
    manager = approval_manager.get_approval_store()
    if manager is None:
        return _refused(task_id, _RUNTIME_NOT_READY)
    call = await manager.scheduled_call(task_id)
    if call is None:
        return _refused(task_id, _REFUSALS["missing"])
    prepared = _prepare_run(context, call, live, arguments_json)
    if isinstance(prepared, str):
        return _refused(task_id, prepared)
    function, arguments_text, approver_id = prepared
    claimed = await manager.claim_scheduled_call(call, arguments_json=arguments_text, approver_user_id=approver_id)
    if claimed is None:
        return _refused(task_id, _RUNTIME_NOT_READY)
    if isinstance(claimed, str):
        if claimed in _ASK_INSTEAD and time.time_ns() >= call.execute_at_ns:
            # Once its time has come, the call can still go ahead the ordinary way, with an approval card now.
            return _refused(
                task_id,
                _REFUSALS[claimed],
                advice=f" To ask the requester to approve it now, call `{call.tool_name}` with these arguments: "
                f"{call.arguments_json}",
            )
        if claimed in _ASK_INSTEAD:
            return _refused(task_id, f"{_REFUSALS[claimed]}; it runs when its task fires")
        return _refused(task_id, _REFUSALS[claimed])
    # The approval is spent; a call interrupted from here leaves its outcome unknown and is never retried.
    # The run's tool-call policy owns every synchronous body the call starts until it finishes.
    async with owned_sync_tool_completion():
        result = await run_tool_call(
            function,
            json.loads(arguments_text),
            replace(live.context, call_id=f"scheduled-{task_id}"),
            middleware=live.middleware,
        )
    await manager.record_scheduled_call_outcome(task_id, "completed" if result.succeeded else "failed")
    if result.error is not None:
        return ToolOutcome(content=f"❌ Scheduled call `{task_id}` failed: {result.failure_text}", is_error=True)
    # A call the policy refused, or one its tool answered as failed, fails this runner's own call with that outcome.
    return result.value


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


def _refused(task_id: str, reason: str, *, advice: str = "") -> ToolOutcome:
    """Answer a scheduled call that did not run as a failed call, which Claude and Gemini read as a failure."""
    return ToolOutcome(content=f"❌ Scheduled call `{task_id}` did not run: {reason}.{advice}", is_error=True)


def _raise_unschedulable(reason: str) -> NoReturn:
    msg = f"{reason}."
    raise SchedulingError(msg)
