"""One accepted owner around Agno's already-approved application FunctionCall."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
from collections.abc import AsyncIterator, Iterator
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from agno.exceptions import AgentRunException
from agno.run.agent import RUN_EVENT_TYPE_REGISTRY, CustomEvent, RunContentEvent
from agno.run.base import BaseRunOutputEvent
from agno.run.team import TEAM_RUN_EVENT_TYPE_REGISTRY
from agno.run.team import CustomEvent as TeamCustomEvent
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.workflow import WORKFLOW_RUN_EVENT_TYPE_REGISTRY
from agno.run.workflow import CustomEvent as WorkflowCustomEvent
from agno.tools import Toolkit
from agno.tools.function import FunctionCall, FunctionExecutionResult, ToolResult
from agno.utils.timer import Timer
from pydantic import BaseModel

from mindroom.background_tasks import (
    run_blocking_until_complete,
    run_coroutine_until_complete,
    wait_for_future_until_complete,
)
from mindroom.custom_tools.job import is_job_function
from mindroom.logging_config import get_logger
from mindroom.tool_jobs.agno_compat_functions import (
    function_actor,
    function_run_context,
    isolated_function_call,
    uses_sdk_async_dispatch,
)
from mindroom.tool_jobs.authorization import function_authority
from mindroom.tool_jobs.consumption import consume_tool_job, restore_control, session_state_delta
from mindroom.tool_jobs.control import job_checkpoint, job_owns_execution
from mindroom.tool_jobs.execution_authority import (
    authorized_tool_call,
    check_current_execution_authority,
    nested_tool_call,
)
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload, encode_tool_result
from mindroom.tool_jobs.runtime import (
    BackgroundOutcome,
    format_job_handle,
    get_background_runtime,
)
from mindroom.tool_jobs.settings import toolkit_is_background_excluded
from mindroom.tool_jobs.wait_timeout import ToolWaitMode, application_arguments, read_wait_timeout
from mindroom.tool_system.construction import get_toolkit_construction
from mindroom.tool_system.context_bound_streams import closing_async_stream
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, get_tool_runtime_context
from mindroom.tool_system.tool_hooks import SyncToolCompletionTracker, track_sync_tool_completion
from mindroom.tool_system.worker_routing import get_tool_execution_identity, tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from agno.tools.function import Function

    from mindroom.tool_jobs.resources import ExecutionResourceReference
    from mindroom.tool_jobs.results import ReplayItem
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)


type ToolCallResult = tuple[bool | AgentRunException, Timer, FunctionCall, FunctionExecutionResult]
type _Execute = Callable[[FunctionCall], Coroutine[object, object, ToolCallResult]]
_EVENT_TYPES: dict[str, type[BaseRunOutputEvent]] = {
    f"{event_type.__module__}.{event_type.__name__}": event_type
    for registry in (RUN_EVENT_TYPE_REGISTRY, TEAM_RUN_EVENT_TYPE_REGISTRY, WORKFLOW_RUN_EVENT_TYPE_REGISTRY)
    for event_type in registry.values()
}
_EVENT_IDS: dict[type, str] = {event_type: name for name, event_type in _EVENT_TYPES.items()}


class _SavedCustomEvent(BaseRunOutputEvent):
    """Replay saved fields/text, preserving SDK updates without importing plugin subclasses."""

    saved_fields: dict[str, Any]
    saved_text: str

    def __str__(self) -> str:
        return self.saved_text

    def to_dict(self) -> dict[str, Any]:
        """Keep custom fields from the snapshot alongside current SDK-owned fields."""
        return {**self.saved_fields, **super().to_dict()}


class _SavedAgentCustomEvent(_SavedCustomEvent, CustomEvent):
    """Retain the agent event's SDK text-accumulation behavior."""


class _SavedTeamCustomEvent(_SavedCustomEvent, TeamCustomEvent):
    """Retain a team custom event without adding it to tool result text."""


class _SavedWorkflowCustomEvent(_SavedCustomEvent, WorkflowCustomEvent):
    """Retain a workflow custom event without adding it to tool result text."""


_CUSTOM_EVENT_TYPES: dict[type, type[_SavedCustomEvent]] = {
    CustomEvent: _SavedAgentCustomEvent,
    TeamCustomEvent: _SavedTeamCustomEvent,
    WorkflowCustomEvent: _SavedWorkflowCustomEvent,
}


def _restore_event(saved: dict[str, Any], chunk: str) -> BaseRunOutputEvent:
    """Restore only registered SDK families; saved data never selects plugin code."""
    event_type = _EVENT_TYPES[saved["type"]]
    fields = deepcopy(saved["event"])
    if chunk and issubclass(event_type, (RunContentEvent, TeamRunContentEvent)):
        fields["content"] = chunk
    event = event_type.from_dict(fields)
    if custom_type := _CUSTOM_EVENT_TYPES.get(event_type):
        sdk_fields = event.to_dict()
        event = custom_type(**vars(event))
        event.saved_fields = {name: value for name, value in saved["event"].items() if name not in sdk_fields}
        event.saved_text = saved.get("text", chunk)
    return event


def _is_background_job_excluded(function: Function) -> bool:
    """Share exact tool exclusions between schema projection and execution."""
    toolkit = function.source_toolkit
    construction = get_toolkit_construction(toolkit) if isinstance(toolkit, Toolkit) else None
    context = get_tool_runtime_context()
    return (
        construction is not None
        and context is not None
        and toolkit_is_background_excluded(construction.name, context.config, context.runtime_paths)
    )


def _is_framework_function(function: Function) -> bool:
    """Only functions of toolkits MindRoom assembled for an actor become jobs; SDK-generated ones run inline."""
    return function.owning_toolkit is None or function_actor(function) is None


def _declares_wait_timeout(function: Function) -> bool:
    return "wait_timeout" in function.parameters.get("properties", {})


def _validate_wait_timeout_parameter(function: Function) -> None:
    """Reject application parameters that would be consumed as framework metadata."""
    if _declares_wait_timeout(function):
        msg = (
            f"Tool {function.name!r} declares its own wait_timeout parameter. "
            "Rename it or add its toolkit to background_tool_jobs.exclude_toolkits."
        )
        raise ValueError(msg)


def _holds_run_connection(function: Function) -> bool:
    """Toolkits the SDK connects for one run, such as Postgres or Agno MCP, must finish inside that run."""
    toolkit = function.source_toolkit
    # Agno recognizes its MCP toolkits by class name so the optional MCP SDK is never imported.
    return isinstance(toolkit, Toolkit) and (
        toolkit.requires_connect or any(base.__name__ == "MCPTools" for base in type(toolkit).__mro__)
    )


def _carries_own_grant(function: Function) -> bool:
    """Only a toolkit MindRoom assembled for an actor with an authority snapshot grants its function on its own."""
    return not _is_framework_function(function) and "scope" in function_authority(function)


def wait_mode(function: Function, *, depth: int) -> ToolWaitMode:
    """Classify one call from current policy: run it unchanged, run it here, or let it become a managed job."""
    if (
        is_job_function(function)
        or _is_framework_function(function)
        or _is_background_job_excluded(function)
        or _holds_run_connection(function)
    ):
        return "native"
    if job_owns_execution() or depth > 0 or function.stop_after_tool_call:
        return "inline"
    return "managed"


@dataclass
class _CollectedResult:
    chunks: list[str] = field(default_factory=list)
    replay: list[ReplayItem] = field(default_factory=list)
    rich: ToolResult = field(default_factory=lambda: ToolResult(content=""))
    has_rich: bool = False

    def _append(self, text: str, event: dict[str, Any] | None = None) -> None:
        self.chunks.append(text)
        self.replay.append((len(text), event))

    def collect(self, item: object) -> None:
        if isinstance(item, BaseRunOutputEvent):
            event = item.to_dict()
            event_type = next((_EVENT_IDS[base] for base in type(item).__mro__ if base in _EVENT_IDS), None)
            if event_type is None:
                msg = f"Unsupported durable tool event: {event['event']}"
                raise TypeError(msg)
            saved: dict[str, Any] = {"type": event_type, "event": event}
            text = ""
            if isinstance(item, (RunContentEvent, TeamRunContentEvent)):
                # Agno concatenates content during replay; model dictionaries must remain JSON text.
                content = item.content.model_dump_json() if isinstance(item.content, BaseModel) else item.content
                text = str(content or "")
                if text:
                    # The saved value holds this text; replay restores it into the event.
                    del event["content"]
            elif isinstance(item, CustomEvent):
                # Agno adds an agent custom event's text to the tool result; replay restores it from there.
                text = str(item)
            elif _EVENT_TYPES[event_type] in _CUSTOM_EVENT_TYPES:
                saved["text"] = str(item)
            self._append(text, saved)
        elif isinstance(item, ToolResult):
            self.has_rich = True
            self._append(item.content)
            self.rich.images = (self.rich.images or []) + (item.images or [])
            self.rich.audios = (self.rich.audios or []) + (item.audios or [])
            self.rich.videos = (self.rich.videos or []) + (item.videos or [])
            self.rich.files = (self.rich.files or []) + (item.files or [])
            self.rich.metadata = {**(self.rich.metadata or {}), **(item.metadata or {})}
        else:
            self._append(str(item))


async def _drain_result(value: object) -> tuple[object, tuple[ReplayItem, ...]]:
    if not isinstance(value, (Iterator, AsyncIterator)):
        return value, ()
    collected = _CollectedResult()

    if isinstance(value, AsyncIterator):
        async with closing_async_stream(value):
            async for item in value:
                collected.collect(item)
    else:

        def consume() -> None:
            try:
                for item in value:
                    collected.collect(item)
            finally:
                if inspect.isgenerator(value):
                    value.close()

        await run_blocking_until_complete(consume)
    text = "".join(collected.chunks)
    replay = tuple(collected.replay)
    if collected.has_rich:
        collected.rich.content = text
        return collected.rich, replay
    return text, replay


def _replayed(value: str | ToolResult, replay: tuple[ReplayItem, ...]) -> Iterator[object]:
    """Re-chunk the saved text around its SDK events; a consumption notice follows as a final chunk."""
    text = value.content if isinstance(value, ToolResult) else value
    offset = 0
    for length, saved in replay:
        chunk = text[offset : offset + length]
        offset += length
        yield chunk if saved is None else _restore_event(saved, chunk)
    if offset < len(text):
        yield text[offset:]


def _control_payload(error: AgentRunException) -> dict[str, Any]:
    return {
        "message": str(error),
        "user_message": error.user_message,
        "agent_message": error.agent_message,
        "messages": error.messages,
        "stop_execution": error.stop_execution,
    }


async def execute_owned_tool_call(original: _Execute, call: FunctionCall) -> ToolCallResult:
    """Drain one SDK dispatch without sharing its synchronous leaf, checking it as its own call."""
    if not job_owns_execution():
        return await original(call)
    tracker = SyncToolCompletionTracker()
    asynchronous = uses_sdk_async_dispatch(call.function)
    try:
        # A model embedded without MindRoom's executor still reaches this dispatch.
        with (
            track_sync_tool_completion(tracker if asynchronous else None),
            nested_tool_call(call, own_grant=_carries_own_grant(call.function)),
        ):
            invocation = original(call)
            if asynchronous:
                return await invocation
            # A synchronous hook bridge can create a worker-local loop. Retain
            # its whole dispatch instead of a leaf task bound to another loop.
            return await run_coroutine_until_complete(invocation)
    finally:
        started = tracker.started_task()
        if started is not None:
            await wait_for_future_until_complete(asyncio.gather(started, return_exceptions=True))


async def _run_operation(
    original: _Execute,
    owned_call: FunctionCall,
    owner: ToolExecutionIdentity,
    baseline: dict[str, Any],
    reference: ExecutionResourceReference,
) -> BackgroundOutcome:
    try:
        with (
            tool_execution_identity(owner),
            authorized_tool_call(owner, owned_call),
        ):
            job_checkpoint()
            check_current_execution_authority()
            success, timer, _, result = await original(owned_call)
            value, replay = await _drain_result(result.result)

            def encode_outcome() -> BackgroundOutcome:
                isolated = function_run_context(owned_call.function)
                delta = session_state_delta(baseline, isolated.session_state or {}) if isolated is not None else {}
                payload = ToolResultPayload(
                    value=value,
                    state_delta=delta,
                    error=owned_call.error or result.error,
                    elapsed=timer.elapsed,
                    replay=replay,
                    control=_control_payload(success) if isinstance(success, AgentRunException) else None,
                )
                return BackgroundOutcome(
                    "completed" if success is True else "failed",
                    result=value.content if isinstance(value, ToolResult) else str(value),
                    result_payload=encode_result_payload(payload),
                )

            encoding = asyncio.create_task(asyncio.to_thread(encode_outcome))
            try:
                return await wait_for_future_until_complete(encoding)
            except asyncio.CancelledError:
                # The tool already returned; stopping its serializer must not erase that outcome.
                return encoding.result()
    finally:
        try:
            await reference.release()
        except asyncio.CancelledError:
            # Resource release drains before propagating cancellation. Preserve
            # the tool's outcome (or its original exception) after that drain.
            pass
        except Exception:
            logger.exception("Tool execution resource cleanup failed", tool_name=owned_call.function.name)


async def _consume_result(
    runtime: ToolJobRuntime,
    job: BackgroundJob,
    claim: JobClaim,
    call: FunctionCall,
    timer: Timer,
) -> ToolCallResult:
    value, payload = await consume_tool_job(runtime, job, claim, function_call=call)
    timer.elapsed_time = payload.elapsed
    call.result = value
    call.error = payload.error or (job.result if job.status == "failed" else None)
    success = restore_control(payload.control) if payload.control is not None else job.status == "completed"
    result = FunctionExecutionResult(status="success" if success is True else "failure", result=value, error=call.error)
    if payload.replay:
        call.result = _replayed(value, payload.replay)
        if isinstance(value, ToolResult):
            result.images, result.audios, result.videos, result.files = (
                value.images,
                value.audios,
                value.videos,
                value.files,
            )
    return success, timer, call, result


async def _execute_inline(original: _Execute, call: FunctionCall, *, mode: ToolWaitMode) -> ToolCallResult:
    """Run a call here; a native call keeps its arguments, so a stray wait budget fails instead of running silently."""
    inline_call = (
        call if mode == "native" else call.model_copy(update={"arguments": application_arguments(call.arguments)})
    )
    success, timer, _, result = await original(inline_call)
    call.result, call.error = inline_call.result, inline_call.error
    return success, timer, call, result


def _failed_call(call: FunctionCall, error: ValueError) -> ToolCallResult:
    """Expose invalid framework arguments through Agno's ordinary tool failure contract."""
    with Timer() as timer:
        call.error = str(error)
        return False, timer, call, FunctionExecutionResult(status="failure", error=call.error)


def wrap_tool_execution(original: _Execute, *, depth: int) -> _Execute:  # noqa: C901, PLR0915 - Keep admission and cleanup together.
    """Wrap one approved SDK executor with admission and exact consumption."""

    async def execute(call: FunctionCall) -> ToolCallResult:  # noqa: C901, PLR0911 - Keep admission and cleanup together.
        context = get_tool_runtime_context()
        runtime = get_background_runtime(context.runtime_paths) if context is not None else None
        resources = current_execution_resources()
        if runtime is None or context is None or resources is None:
            return await original(call)
        if _is_framework_function(call.function):
            job_checkpoint()
            check_current_execution_authority()
            return await original(call)
        mode = wait_mode(call.function, depth=depth)
        try:
            if mode != "native":
                _validate_wait_timeout_parameter(call.function)
            wait_timeout = (
                None
                if mode == "native"
                else read_wait_timeout(call.arguments, owned_execution=job_owns_execution() or depth > 0)
            )
        except ValueError as error:
            return _failed_call(call, error)
        if call.function.stop_after_tool_call and wait_timeout is not None:
            return _failed_call(
                call,
                ValueError("wait_timeout is not supported for tools that stop the current model step"),
            )
        owner = get_tool_execution_identity() or build_execution_identity_from_runtime_context(context)
        actor = function_actor(call.function)
        if actor is not None and actor.id:
            owner = replace(owner, agent_name=actor.id)
        job_checkpoint()
        with authorized_tool_call(owner, call):
            check_current_execution_authority()
            if mode != "managed" or call.function.external_execution:
                return await _execute_inline(original, call, mode=mode)
        run_context = function_run_context(call.function)
        if run_context is None or not run_context.run_id or not call.call_id:
            msg = "Managed tool execution requires an exact run and tool-call identity"
            raise ValueError(msg)
        identity = {"owner": asdict(owner), "run_id": run_context.run_id, "tool_call_id": call.call_id}
        job_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        adapter = {
            "run_id": run_context.run_id,
            "tool_call_id": call.call_id,
            "arguments": encode_tool_result(call.arguments),
            "origin": function_provenance(call.function, call.arguments),
            "authority": function_authority(call.function),
        }
        owned_call = isolated_function_call(call)
        owned_call.arguments = application_arguments(owned_call.arguments)
        baseline = deepcopy(run_context.session_state or {})
        reference = resources.acquire()

        claim = None
        retained = False
        try:
            _, claim = await runtime.start(
                job_id,
                tool_name=call.function.name,
                depth=depth,
                toolkit_name=call.function.owning_toolkit,
                source_event_id=context.membership_turn_id,
                source_kind=context.source_kind,
                adapter=adapter,
                owner=owner,
                operation=lambda: _run_operation(original, owned_call, owner, baseline, reference),
                reattach=True,
            )
            with Timer() as timer:
                waited = await runtime.wait(job_id, owner=owner, depth=depth, timeout=wait_timeout, claim=claim)
            if waited.claim is None:
                call.result = format_job_handle(waited.job)
                return True, timer, call, FunctionExecutionResult(status="success", result=call.result)
            response = await _consume_result(runtime, waited.job, waited.claim, call, timer)
            retained = True
            return response
        finally:
            if not retained:
                await runtime.release_wait(job_id, claim)
            if not runtime.owns_execution(job_id, adapter):
                await reference.release()

    return execute
