"""Tool calls that run as background jobs on the agent loop.

``with_job_admission`` admits each call one actor makes: it runs in its reply, as a managed job the reply waits for
within the call's ``background_after`` budget, or not at all when a job-gated call cannot become a job, which also
asks for that call's approval as the job's first phase. Inside a managed job every call admitted here, a nested
MindRoom agent's included, keeps the synchronous leaves it starts owned by the job and has its grant checked again.
``job_definitions_hook`` offers the budget on the definitions of the functions whose calls can become jobs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.model_loop import SendRequest
from mindroom.approval.tool_approval import JOB_APPROVAL_TYPE
from mindroom.background_tasks import wait_for_future_until_complete
from mindroom.custom_tools.job import JobTools
from mindroom.failure_text import failure_text
from mindroom.llm.runs import denial_note_for
from mindroom.logging_config import get_logger
from mindroom.session_storage.types import session_state_snapshot
from mindroom.tool_jobs.approvals import ask_tool_call_approval
from mindroom.tool_jobs.authorization import toolkit_authority
from mindroom.tool_jobs.background_after import (
    BACKGROUND_AFTER,
    BACKGROUND_AFTER_SCHEMA,
    ToolWaitMode,
    application_arguments,
    read_background_after,
)
from mindroom.tool_jobs.consumption import consume_tool_job, job_failure, session_state_delta
from mindroom.tool_jobs.control import job_checkpoint, job_owns_execution
from mindroom.tool_jobs.conversation import current_approval_wait_timeout
from mindroom.tool_jobs.execution_authority import (
    JobCall,
    JobTool,
    authorized_tool_call,
    check_current_execution_authority,
    enclosing_grant,
)
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.resources import current_execution_resources
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload, encode_tool_result
from mindroom.tool_jobs.runtime import BackgroundOutcome, format_job_handle, get_background_runtime
from mindroom.tool_jobs.settings import toolkit_is_background_excluded
from mindroom.tool_system.agent_tool_calls import cli_dispatch_active
from mindroom.tool_system.agno_toolkits import requires_connect
from mindroom.tool_system.call_record import record_call
from mindroom.tool_system.construction import get_toolkit_construction
from mindroom.tool_system.dedicated_threads import JOB_CALL_EXECUTOR
from mindroom.tool_system.invocation import run_tool_call
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, get_tool_runtime_context
from mindroom.tool_system.tool_function import ToolOutcome, ToolValue
from mindroom.tool_system.tool_hooks import owned_sync_tool_completion
from mindroom.tool_system.worker_routing import get_tool_execution_identity, tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.model_loop import BeforeRequestHook, LoopState
    from mindroom.llm.provider import ToolSpec
    from mindroom.tool_jobs.resources import ExecutionResourceReference, ExecutionResources
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.invocation import CallResult, ToolCall, ToolMiddleware, ToolNext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)

_UNASKABLE = "This call needs approval, which it cannot ask for here, so it did not run."


@dataclass(frozen=True, slots=True, kw_only=True)
class JobActor:
    """An agent or team whose calls may run as tool jobs.

    ``name`` is the agent the calls run as, or the team; ``team`` says a team's run saves them; ``depth`` is the
    actor's delegation depth. ``tools`` holds each function a MindRoom-assembled toolkit offers the actor, by name;
    the knowledge search, skill, and team delegation functions MindRoom adds beside those toolkits always run in its
    reply.
    """

    name: str
    team: bool
    depth: int
    tools: Mapping[str, JobTool]


class _JobCallError(Exception):
    """A managed call whose job did not complete; the text tells the model why."""


def assembled_job_tools(
    sources: Iterable[tuple[AnyToolkit, ToolFunction]],
    snapshot: Mapping[str, Any],
) -> dict[str, JobTool]:
    """Describe each function a MindRoom-assembled toolkit offers the actor, by name.

    ``sources`` holds, for each such function, the concrete toolkit built for the actor and the function itself,
    which names its authored toolkit; the knowledge search, skill, and team delegation functions MindRoom adds beside
    those toolkits are not among them. ``snapshot`` is the actor's execution scope and file access, empty for a team.
    """
    return {
        function.name: JobTool(function=function, toolkit=toolkit, authority=toolkit_authority(snapshot, toolkit))
        for toolkit, function in sources
    }


def _declares_background_after(parameters: Mapping[str, Any]) -> bool:
    """Whether an application schema itself declares a ``background_after`` parameter."""
    return BACKGROUND_AFTER in parameters.get("properties", {})


def approval_can_run_as_job(function: ToolFunction, toolkit: AnyToolkit) -> bool:
    """Whether a policy-gated function of ``toolkit`` can ask for its approval as the first phase of its call's job."""
    return not (
        isinstance(toolkit, JobTools)
        or function.external_execution
        or function.stop_after_call
        # A connection opened for one run must finish its calls inside that run.
        or requires_connect(toolkit)
    )


def _is_background_job_excluded(tool: JobTool, context: ToolRuntimeContext) -> bool:
    """Share exact tool exclusions between definition projection and execution."""
    construction = get_toolkit_construction(tool.toolkit)
    return construction is not None and toolkit_is_background_excluded(
        construction.name,
        context.config,
        context.runtime_paths,
    )


def wait_mode(tool: JobTool, *, depth: int, context: ToolRuntimeContext) -> ToolWaitMode:
    """Classify one call from current policy: run it unchanged, run it here, or let it become a managed job.

    ``context`` is the turn's tool context, whose configuration decides whether the call's toolkit is excluded.
    """
    if (
        isinstance(tool.toolkit, JobTools)
        or _is_background_job_excluded(tool, context)
        # A connection opened for one run must finish its calls inside that run.
        or requires_connect(tool.toolkit)
    ):
        return "native"
    # A call a CLI command makes returns to that command, whose own result is what the reply saves.
    if job_owns_execution() or depth > 0 or tool.function.stop_after_call or cli_dispatch_active():
        return "inline"
    return "managed"


@dataclass(frozen=True, slots=True)
class _JobScope:
    """Where calls made now become jobs: the turn's tool context, the job runtime beside it, and its resources."""

    context: ToolRuntimeContext
    runtime: ToolJobRuntime
    resources: ExecutionResources


def _job_scope() -> _JobScope | None:
    """Return where calls made now become jobs, or None when the turn runs without the job runtime or resources."""
    context = get_tool_runtime_context()
    runtime = get_background_runtime(context.runtime_paths) if context is not None else None
    resources = current_execution_resources()
    if context is None or runtime is None or resources is None:
        return None
    return _JobScope(context, runtime, resources)


def project_definitions(definitions: Sequence[ToolSpec], managed: Collection[str]) -> tuple[ToolSpec, ...]:
    """Add the optional ``background_after`` parameter to the definition of each function named in ``managed``.

    A function that declares its own ``background_after`` keeps its authored schema; the collision fails only that
    call's execution. A strict definition requires the parameter, since a strict schema requires every property.
    """
    return tuple(
        _with_background_after(definition)
        if definition.name in managed and not _declares_background_after(definition.parameters)
        else definition
        for definition in definitions
    )


def _with_background_after(definition: ToolSpec) -> ToolSpec:
    parameters = deepcopy(dict(definition.parameters))
    parameters["properties"] = {**parameters.get("properties", {}), BACKGROUND_AFTER: deepcopy(BACKGROUND_AFTER_SCHEMA)}
    if definition.strict:
        required = list(parameters.get("required", []))
        if BACKGROUND_AFTER not in required:
            required.append(BACKGROUND_AFTER)
        parameters["required"] = required
    return replace(definition, parameters=parameters)


def job_definitions_hook(actor: JobActor) -> BeforeRequestHook:
    """Return the pre-request hook that offers the wait budget of the actor's managed calls while they can be jobs."""

    async def project(state: LoopState) -> SendRequest:
        scope = _job_scope()
        if scope is not None:
            managed = [
                name
                for name, tool in actor.tools.items()
                if wait_mode(tool, depth=actor.depth, context=scope.context) == "managed"
            ]
            state.tools = project_definitions(state.tools, managed)
        return SendRequest()

    return project


def with_job_admission(actor: JobActor | None, inner: Sequence[ToolMiddleware]) -> tuple[ToolMiddleware, ...]:
    """Return the actor's tool-call policy ``inner`` with job admission outermost, the policy the loop runs.

    A managed job runs its call through ``inner`` again in the job's own task, on a copy of the call's session
    state, so the reply's state changes only when the reply reads the job's result. Without an ``actor``, whose
    calls never run as jobs, the loop runs ``inner`` as it is.
    """
    rest = tuple(inner)
    if actor is None:
        return rest

    async def admit(call: ToolCall, call_next: ToolNext) -> object:
        return await _admit(actor, rest, call, call_next)

    return (admit, *rest)


def agent_tool_middleware(build: AgentBuild) -> tuple[ToolMiddleware, ...]:
    """Return what the loop runs around each of the build's calls: ``tool_policy``, admitted as ``tool_jobs``."""
    return with_job_admission(build.tool_jobs, build.tool_policy)


async def _admit(actor: JobActor, inner: Sequence[ToolMiddleware], call: ToolCall, call_next: ToolNext) -> object:
    """Admit one call: run it here, as a managed job, or not at all when a job-gated call cannot become one."""
    scope = _job_scope()
    tool = actor.tools.get(call.function.name)
    job_call = (
        None if tool is None else JobCall(tool=tool, arguments=call.arguments, context=call.context, team=actor.team)
    )
    # A call that asks for its approval as its job never runs without becoming one.
    job_approval = call.function.approval_type == JOB_APPROVAL_TYPE
    if scope is None or job_call is None:
        if job_approval:
            raise ValueError(_UNASKABLE)
        job_checkpoint()
        check_current_execution_authority()
        return await _run_here(call_next, call.arguments, unassembled=job_call is None)
    mode = wait_mode(job_call.tool, depth=actor.depth, context=scope.context)
    if job_approval and mode != "managed":
        raise ValueError(_UNASKABLE)
    background_after = _wait_budget(call, mode=mode, depth=actor.depth)
    owner = replace(
        get_tool_execution_identity() or build_execution_identity_from_runtime_context(scope.context),
        agent_name=actor.name,
    )
    job_checkpoint()
    with authorized_tool_call(owner, job_call):
        # Access may change after the call was offered, as a reload that removed its toolkit does: the call fails.
        check_current_execution_authority()
        if mode != "managed" or call.function.external_execution:
            # A native call keeps its arguments, so a stray wait budget fails instead of running silently.
            arguments = call.arguments if mode == "native" else application_arguments(call.arguments)
            return await _run_here(call_next, arguments, unassembled=False)
    return await _run_managed_call(
        inner,
        call.function,
        job_call,
        scope,
        owner,
        depth=actor.depth,
        background_after=background_after,
        job_approval=job_approval,
    )


def _wait_budget(call: ToolCall, *, mode: ToolWaitMode, depth: int) -> float | None:
    """Return how long the caller waits for this call's job, or raise ValueError for an unusable budget."""
    background_after = None
    if mode != "native":
        if _declares_background_after(call.function.parameters):
            msg = (
                f"Tool {call.function.name!r} declares its own {BACKGROUND_AFTER} parameter. "
                "Rename it or add its toolkit to background_tool_jobs.exclude_toolkits."
            )
            raise ValueError(msg)
        background_after = read_background_after(call.arguments, owned_execution=job_owns_execution() or depth > 0)
    if call.function.stop_after_call and background_after is not None:
        msg = f"{BACKGROUND_AFTER} is not supported for tools that stop the current model step"
        raise ValueError(msg)
    return background_after


async def _run_here(call_next: ToolNext, arguments: dict[str, Any], *, unassembled: bool) -> object:
    """Run a call in this task; inside a managed job, the job owns the synchronous leaves it starts.

    ``unassembled`` says the function comes from no toolkit MindRoom assembles, as the knowledge search, skill, and
    team delegation functions MindRoom adds beside them do; inside a job, such a function runs under its enclosing
    call's grant.
    """
    if not job_owns_execution():
        return await call_next(arguments)
    # A managed job keeps its resources until every synchronous leaf the call started finishes.
    with enclosing_grant() if unassembled else nullcontext():
        async with owned_sync_tool_completion(JOB_CALL_EXECUTOR):
            return await call_next(arguments)


def _detached(call: JobCall) -> JobCall:
    """Return the call a job runs: its application arguments, on copies of the run's session state and metadata.

    The state is live: a sync tool of the same batch may be changing it from its executor thread.
    """
    context = replace(
        call.context,
        session_state=session_state_snapshot(call.context.session_state),
        metadata=deepcopy(call.context.metadata),
    )
    return replace(call, arguments=application_arguments(deepcopy(dict(call.arguments))), context=context)


# The approval a gated call asks for as the first phase of its job: whether it was approved, and why not.
type _AskApproval = Callable[[], Awaitable[tuple[bool, str | None]]]


async def _run_managed_call(
    inner: Sequence[ToolMiddleware],
    function: ToolFunction,
    job_call: JobCall,
    scope: _JobScope,
    owner: ToolExecutionIdentity,
    *,
    depth: int,
    background_after: float | None,
    job_approval: bool,
) -> object:
    """Accept one call as a managed job, wait for it within the caller's budget, and return what it returned.

    ``function`` is the function the loop called, which the job runs; ``job_call`` holds the call's grant.
    """
    context, runtime = scope.context, scope.runtime
    run_id, call_id = job_call.context.run_id, job_call.context.call_id
    if not run_id or not call_id:
        msg = "Managed tool execution requires an exact run and tool-call identity"
        raise ValueError(msg)
    identity = {"owner": asdict(owner), "run_id": run_id, "tool_call_id": call_id}
    job_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    adapter = {
        "run_id": run_id,
        "tool_call_id": call_id,
        "arguments": encode_tool_result(job_call.arguments),
        "origin": function_provenance(job_call.tool, job_call.arguments),
        "authority": dict(job_call.tool.authority),
    }
    detached = _detached(job_call)
    # The job's state changes are read against the copy it starts from, so a key a sibling adds to the live state
    # after that copy never reads as deleted by this job.
    baseline = deepcopy(detached.context.session_state)
    # The reply records the call before its job starts, as an inline call's hooks do; the job body runs unrecorded.
    record_return = await record_call(function.name, dict(detached.arguments))

    async def ask_approval() -> tuple[bool, str | None]:
        # The policy and approver current when the job asks decide, as for a paused reply.
        return await ask_tool_call_approval(
            runtime,
            job_id,
            detached,
            owner=owner,
            config=context.current_config,
            runtime_paths=context.runtime_paths,
        )

    approval = ask_approval if job_approval else None
    claim = None
    retained = False
    reference: ExecutionResourceReference | None = None
    try:
        reference = scope.resources.acquire()
        _, claim = await runtime.start(
            job_id,
            tool_name=function.name,
            depth=depth,
            toolkit_name=job_call.tool.toolkit_name,
            source_event_id=context.membership_turn_id,
            source_kind=context.source_kind,
            adapter=adapter,
            owner=owner,
            operation=lambda: _run_operation(inner, function, detached, owner, baseline, reference, approval),
            reattach=True,
        )
        waited = await runtime.wait(
            job_id,
            owner=owner,
            depth=depth,
            timeout=background_after,
            claim=claim,
            # The reply waits this long for a human decision, then goes on while the job waits for it.
            approval_timeout=current_approval_wait_timeout() if job_approval else None,
        )
        if waited.claim is None:
            value, error = format_job_handle(waited.job), None
        else:
            value, payload = await consume_tool_job(
                runtime,
                waited.job,
                waited.claim,
                job_call=job_call,
                returned_inline=True,
            )
            retained = True
            error = job_failure(waited.job, value, payload)
        # A released wait's call returned its job handle, which names the job a restart's account points to. A
        # failed call is recorded as failed, with the text the model reads.
        await record_return(value if error is None else ToolOutcome(content=error, is_error=True))
    except Exception as failure:
        # The call fails with what ended it, as the model reads it, such as the runtime refusing the job or this
        # caller's wait for it, which fails the call, not the reply's run. A cancelled call stays started, since its
        # job may still take effect.
        await record_return(ToolOutcome(content=failure_text(failure), is_error=True))
        raise
    finally:
        if not retained:
            await runtime.release_wait(job_id, claim)
        if reference is not None and not runtime.owns_execution(job_id, adapter):
            await reference.release()
    if error is not None:
        raise _JobCallError(error)
    return value


async def _run_operation(
    inner: Sequence[ToolMiddleware],
    function: ToolFunction,
    call: JobCall,
    owner: ToolExecutionIdentity,
    baseline: dict[str, Any],
    reference: ExecutionResourceReference,
    approval: _AskApproval | None,
) -> BackgroundOutcome:
    """Run a managed call as its job: its approval first when it asks for one, then the call, then its saved outcome."""
    try:
        if approval is not None:
            approved, reason = await approval()
            if not approved:
                return BackgroundOutcome("denied", denial_note_for(reason))
        with tool_execution_identity(owner), authorized_tool_call(owner, call):
            job_checkpoint()
            check_current_execution_authority()
            async with owned_sync_tool_completion(JOB_CALL_EXECUTOR):
                result = await run_tool_call(function, call.arguments, call.context, middleware=inner)
            encoding = asyncio.create_task(
                asyncio.to_thread(_saved_outcome, result, baseline, call.context.session_state),
            )
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
            logger.exception("Tool execution resource cleanup failed", tool_name=function.name)


def _saved_outcome(result: CallResult, baseline: dict[str, Any], state: dict[str, Any]) -> BackgroundOutcome:
    """Encode a finished call's outcome with the session-state changes it made, off the event loop.

    It fails when it raised, or when the policy answered it with a failed outcome, as for a call a plugin hook declined.
    """
    failure = None if result.succeeded else result.failure_text
    value = result.value if failure is None else None
    try:
        payload = ToolResultPayload(value=value, state_delta=session_state_delta(baseline, state), error=failure)
        encoded = encode_result_payload(payload)
    except Exception as error:
        # Whatever fails here fails after the call ran, so its lost result must not read as a call that did not
        # take effect.
        reason = failure_text(error)
        return BackgroundOutcome(
            "failed",
            f"The call ran, but its result could not be saved ({reason}). Its effects stand: do not call it again.",
        )
    if failure is not None:
        return BackgroundOutcome("failed", result=failure, result_payload=encoded)
    text = value.content if isinstance(value, ToolValue) else str(value)
    return BackgroundOutcome("completed", result=text, result_payload=encoded)


__all__ = [
    "JobActor",
    "agent_tool_middleware",
    "approval_can_run_as_job",
    "assembled_job_tools",
    "job_definitions_hook",
    "project_definitions",
    "wait_mode",
    "with_job_admission",
]
