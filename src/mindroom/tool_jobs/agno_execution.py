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
from agno.run.agent import RUN_EVENT_TYPE_REGISTRY, RunContentEvent
from agno.run.base import BaseRunOutputEvent
from agno.run.team import TEAM_RUN_EVENT_TYPE_REGISTRY
from agno.run.team import RunContentEvent as TeamRunContentEvent
from agno.run.workflow import WORKFLOW_RUN_EVENT_TYPE_REGISTRY
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
    is_framework_function,
    isolated_function_call,
    uses_sdk_async_dispatch,
)
from mindroom.tool_jobs.authorization import function_authority
from mindroom.tool_jobs.consumption import (
    consume_tool_job,
    consuming_function_call,
    restore_control,
    session_state_delta,
)
from mindroom.tool_jobs.control import job_checkpoint, job_owns_execution
from mindroom.tool_jobs.execution_authority import authorized_tool_call, check_current_execution_authority
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload, encode_tool_result
from mindroom.tool_jobs.runtime import (
    BackgroundOutcome,
    format_job_handle,
    get_background_runtime,
)
from mindroom.tool_jobs.settings import toolkit_is_background_excluded
from mindroom.tool_jobs.wait_timeout import (
    ToolWaitMode,
    application_arguments,
    read_wait_timeout,
    record_tool_wait_mode,
    saved_tool_wait_mode,
)
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
_EVENT_TYPES = {**RUN_EVENT_TYPE_REGISTRY, **TEAM_RUN_EVENT_TYPE_REGISTRY, **WORKFLOW_RUN_EVENT_TYPE_REGISTRY}


def is_background_job_excluded(function: Function) -> bool:
    """Share exact tool exclusions between schema projection and execution."""
    toolkit = function.source_toolkit
    construction = get_toolkit_construction(toolkit) if isinstance(toolkit, Toolkit) else None
    context = get_tool_runtime_context()
    return (
        construction is not None
        and context is not None
        and toolkit_is_background_excluded(construction.name, context.config, context.runtime_paths)
    )


def _validate_wait_timeout_parameter(function: Function) -> None:
    """Reject application parameters that would be consumed as framework metadata."""
    if not is_job_function(function) and "wait_timeout" in function.parameters.get("properties", {}):
        msg = (
            f"Tool {function.name!r} declares its own wait_timeout parameter. "
            "Rename it or add its toolkit to background_tool_jobs.exclude_toolkits."
        )
        raise ValueError(msg)


def call_wait_mode(call: FunctionCall, *, depth: int) -> ToolWaitMode:
    """Freeze a call's argument/owner policy before the SDK can pause for approval."""
    run = function_run_context(call.function)
    if run is not None and call.call_id and (saved := saved_tool_wait_mode(run.metadata, run.run_id, call.call_id)):
        return saved
    mode: ToolWaitMode = "managed"
    if is_background_job_excluded(call.function) or is_framework_function(call.function):
        mode = "native"
    elif not is_job_function(call.function) and (
        job_owns_execution() or depth > 0 or call.function.stop_after_tool_call
    ):
        mode = "inline"
    if run is not None and run.metadata is not None and call.call_id:
        record_tool_wait_mode(run.metadata, run.run_id, call.call_id, mode)
    return mode


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
            if event["event"] not in _EVENT_TYPES:
                msg = f"Unsupported durable tool event: {event['event']}"
                raise TypeError(msg)
            text = ""
            if isinstance(item, (RunContentEvent, TeamRunContentEvent)):
                # Agno concatenates content during replay; model dictionaries must remain JSON text.
                content = item.content.model_dump_json() if isinstance(item.content, BaseModel) else item.content
                text = str(content or "")
                if text:
                    # The saved value holds this text; replay restores it into the event.
                    del event["content"]
            self._append(text, event)
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
        if saved is None:
            yield chunk
            continue
        event = dict(saved, content=chunk) if length else dict(saved)
        yield _EVENT_TYPES[event["event"]].from_dict(event)
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
    """Drain one SDK dispatch without sharing its synchronous leaf with nested calls."""
    if not job_owns_execution():
        return await original(call)
    tracker = SyncToolCompletionTracker()
    asynchronous = uses_sdk_async_dispatch(call.function)
    try:
        with track_sync_tool_completion(tracker if asynchronous else None):
            invocation = original(call)
            if asynchronous:
                return await invocation
            # A synchronous hook bridge can create a worker-local loop. Retain
            # its whole dispatch instead of a leaf task bound to another loop.
            return await run_coroutine_until_complete(invocation)
    finally:
        started = tracker.started_task()
        if started is not None:
            await run_coroutine_until_complete(_drain_sync(started))


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
            authorized_tool_call(owner, owned_call.function, arguments=owned_call.arguments),
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
    inline_call = (
        call
        if is_job_function(call.function) or mode == "native"
        else call.model_copy(update={"arguments": application_arguments(call.arguments)})
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

    async def execute(call: FunctionCall) -> ToolCallResult:  # noqa: C901, PLR0911, PLR0912, PLR0915 - Keep admission and cleanup together.
        context = get_tool_runtime_context()
        runtime = get_background_runtime(context.runtime_paths) if context is not None else None
        resources = current_execution_resources()
        if runtime is None or context is None or resources is None:
            return await original(call)
        if is_framework_function(call.function):
            job_checkpoint()
            check_current_execution_authority()
            return await original(call)
        mode = call_wait_mode(call, depth=depth)
        try:
            if mode != "native":
                _validate_wait_timeout_parameter(call.function)
            wait_timeout = (
                None
                if mode == "native"
                else read_wait_timeout(
                    call.arguments,
                    owned_execution=(job_owns_execution() or depth > 0) and not is_job_function(call.function),
                )
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
        with authorized_tool_call(owner, call.function, arguments=call.arguments), consuming_function_call(call):
            check_current_execution_authority()
            if (
                mode != "managed"
                or is_job_function(call.function)
                or call.function.external_execution
                or call.function.stop_after_tool_call
            ):
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
            if not runtime.owns_execution(job_id, adapter):
                await reference.release()
            waited = await runtime.wait(job_id, owner=owner, depth=depth, timeout=wait_timeout, claim=claim)
            timer = Timer()
            timer.start()
            timer.stop()
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


async def _drain_sync(task: asyncio.Task[Any]) -> None:
    await asyncio.gather(task, return_exceptions=True)
