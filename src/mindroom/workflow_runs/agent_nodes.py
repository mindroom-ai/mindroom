"""One agent turn of a workflow run as a delegation job: its launch, its wait, and what the job's end makes of the node."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.ai import run_delegated_child_response
from mindroom.ai_run_metadata import prior_usage_of, turn_usage
from mindroom.delegation.background import delegation_child
from mindroom.delegation.execution import delegation_call_job
from mindroom.delegation.lifecycle import (
    DelegationPendingError,
    authorize_delegation,
    prepare_child_turn,
    reload_stopped_child_result,
)
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.state import RESTART_INTERRUPTION_REASON, RUN_SUBAGENT, SPENT_RUNS_KEY, SubagentPersona
from mindroom.delegation.targets import resolve_fresh_target
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, JobAccessError
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload
from mindroom.workflow_runs.models import DispatchHandle, WorkflowNodeState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.workflow_runs.models import WorkflowNode, WorkflowRun
    from mindroom.workflow_runs.store import WorkflowRunStore

# The options of the SDK's `agent()` as the arguments of `run_subagent`, whose rules resolve them.
_RENAMED = {"prompt": "task", "agent": "agent_name", "system": "system_prompt", "output_path": OUTPUT_PATH_ARGUMENT}
_OPTIONS = (
    frozenset(_RENAMED)
    | {"model", "tools", "skills", "knowledge", "profile", "minimal"}
    | {
        "dedicated_worker",
        "resource_profile",
    }
)
_NARROWING = frozenset({"tools", "skills", "knowledge"})
_NARROWS_WITH_SYSTEM = "tools, skills and knowledge narrow a copy only together with system=."
_NO_ANSWER = "The agent's turn left no record of how it ended."
# How long a wait for a job that cannot be read yet, while its requester's access resolves, sleeps before it asks again.
_RECHECK_SECONDS = 1.0

# How one agent turn ended: `completed` and its answer, or the kind of its error and what to tell the script.
type ChildOutcome = tuple[str, str]


class AgentNodeError(Exception):
    """An agent turn that did not start: the refusal the script reads, of kind `refused`."""


class AgentNotReadyError(Exception):
    """An agent turn that cannot be decided yet, such as one for an agent whose bot is still starting."""


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """What the engine runs agent turns on: the job runtime, and the live configuration to read their runs with."""

    jobs: ToolJobRuntime
    config: Config
    paths: RuntimePaths


def turn_job_id(run: WorkflowRun, key: str, generation: int, turn: int) -> str:
    """Return the ID of the job one turn of one generation of a node runs as: the same for the same turn, always."""
    identity = [run.execution_identity, run.run_id, key, generation, turn]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


async def launch_child(
    context: ToolRuntimeContext,
    run: WorkflowRun,
    node: WorkflowNode,
    epoch: int,
    *,
    store: WorkflowRunStore,
    jobs: ToolJobRuntime,
    turn: int = 1,
) -> WorkflowNode:
    """Start the job of one agent turn of *run* in its owner's *context*, and return the node as saved with its handle.

    The one launch path: the target is resolved and authorized as a subagent's is, under the `workflow` grant, at
    the depth of the run's owner. The node is saved with the handle of its job before the job is started, once, and
    the handle says so afterwards. The child's task is the node's label; the job is sent the whole prompt. A turn
    that may not start raises `AgentNodeError` and saves nothing.
    """
    owner = parse_tool_execution_identity_payload(run.execution_identity)
    config, paths, caller = context.current_config, context.runtime_paths, run.agent_name
    arguments = {_RENAMED.get(name, name): value for name, value in node.request.items() if name in _OPTIONS}
    cap = SubagentPersona.from_dict(run.persona) if run.persona else None
    if cap is not None and arguments.get("agent_name") in (None, caller) and not arguments.get("profile"):
        # A copy of a narrowed agent is that agent again: its prompt, and at most what it was left with.
        arguments["system_prompt"] = arguments.get("system_prompt") or cap.system_prompt
    if not arguments.get("system_prompt") and _NARROWING & arguments.keys():
        # `run_subagent` reads a list without a prompt as nothing, which would hand the copy every tool.
        raise AgentNodeError(_NARROWS_WITH_SYSTEM)
    with tool_runtime_context(context):
        target = resolve_fresh_target(
            arguments,
            caller_name=caller,
            config=config,
            runtime_paths=paths,
            depth=run.depth,
            workspace_root=lambda: (
                resolve_agent_runtime(caller, config, paths, execution_identity=owner).workspace.root
            ),
            cap=cap,
        )
        if isinstance(target, str):
            raise AgentNodeError(target)
        try:
            authorized = authorize_delegation(
                caller,
                target.agent_name,
                node.label,
                config=config,
                runtime_paths=paths,
                execution_identity=owner,
                depth=run.depth,
                model=target.model,
                grant="workflow",
            )
        except DelegationPendingError as exc:
            raise AgentNotReadyError(str(exc)) from exc
        if isinstance(authorized, str):
            raise AgentNodeError(authorized)
        child = prepare_child_turn(
            caller,
            target.agent_name,
            node.label,
            owner=owner,
            config=authorized,
            runtime_paths=paths,
            depth=run.depth,
            budget_monitor=context.budget_monitor,
            model=target.model,
            agent_mode=target.agent_mode,
            previous=None,
            persona=target.persona,
            dedicated_worker=target.dedicated_worker,
            worker_resource_profile=target.resource_profile,
        )
        child.workflow_run_id, child.workflow_node_key, child.workflow_generation = (
            run.run_id,
            node.key,
            node.generation,
        )
        job_id = turn_job_id(run, node.key, node.generation, turn)
        orchestrator = context.orchestrator
        spec = delegation_call_job(
            child,
            job_id=job_id,
            runtime=jobs,
            owner=owner,
            parent_run_id=None,
            tool_name=RUN_SUBAGENT,
            arguments=arguments,
            run_child=run_delegated_child_response,
            prompt=str(node.request.get("prompt")),
            config=authorized,
            runtime_paths=paths,
            refresh_scheduler=None if orchestrator is None else orchestrator.knowledge_refresh_scheduler,
        )
        if isinstance(spec, str):
            raise AgentNodeError(spec)
        # What tells the job of a run's call from a job of its own: the run reports it, and nothing else lists it.
        spec.adapter.update(workflow=run.run_id, node=node.key, generation=node.generation)
        handle = DispatchHandle("job", job_id, dispatched=False)
        links = {"job_id": job_id, "delegation_id": child.delegation_id, "subagent_id": child.subagent_id}
        node = replace(node, state=WorkflowNodeState.RUNNING, handle=handle, links=links)
        node = await asyncio.to_thread(partial(store.put_node, node, epoch, dirty_after=True))
        try:
            # In the owner's context, which the job's task keeps: the child runs in the run's conversation.
            _, claim = await jobs.start(
                job_id,
                tool_name=spec.tool_name,
                depth=run.depth,
                kind=spec.kind,
                adapter=spec.adapter,
                owner=owner,
                operation=spec.operation,
                cancel=spec.cancel,
            )
        except JobAccessError as exc:
            raise AgentNodeError(str(exc)) from exc
    await jobs.release_wait(job_id, claim)
    stamp = partial(store.update_node, run.run_id, node.key, node.generation, epoch)
    await asyncio.to_thread(partial(stamp, handle=replace(handle, dispatched=True)))
    return node


async def await_child(
    run: WorkflowRun,
    node: WorkflowNode,
    runtime: AgentRuntime,
    *,
    on_waiting: Callable[[str | None], Awaitable[object]],
    timeout: float | None = None,  # noqa: ASYNC109 - the seconds of work the turn gets, not a deadline of this call.
) -> tuple[ChildOutcome, dict[str, object] | None, JobClaim | None]:
    """Wait for the job of *node* to end; return how the turn ended, what it used, and the claim on the job's outcome.

    The wait is on the saved job ID and starts nothing. *on_waiting* is told when the job starts to wait for a
    person's approval, and with None when it works again; that time is not the turn's. After *timeout* seconds of
    work the job is cancelled, and the wait goes on until it has ended.
    """
    assert node.handle is not None
    jobs, job_id = runtime.jobs, node.handle.ref
    owner = parse_tool_execution_identity_payload(run.execution_identity)
    wait = partial(jobs.wait, job_id, owner=owner, depth=run.depth)
    if all(job.job_id != job_id for job in jobs.retained_jobs()):
        # A job the runtime never admitted did nothing. One it has since deleted may have done anything.
        kind = "indeterminate" if node.handle.dispatched else "interrupted"
        return (kind, "The agent was interrupted before it started."), None, None
    loop, waiting, timed_out, claim = asyncio.get_running_loop(), False, False, None
    while True:
        since = loop.time()
        try:
            waited = await wait(approval_timeout=None if waiting else 0, work_timeout=0 if waiting else timeout)
        except JobAccessError as exc:
            return ("denied", str(exc)), None, None
        job, claim = waited.job, waited.claim
        if job.status in TERMINAL_STATUSES and (claim is not None or job.consumed):
            # The outcome is this wait's to read: a job that ended while its requester's access is unresolved is not.
            break
        if (job.status == "awaiting_approval") != waiting:
            waiting = not waiting
            await on_waiting("approval" if waiting else None)
        elif job.status in TERMINAL_STATUSES:
            await asyncio.sleep(_RECHECK_SECONDS)
        elif timeout is not None and (timeout := timeout - (loop.time() - since)) <= 0:
            # Asked once; the job's end then says what the turn had done.
            timed_out, timeout = True, None
            await jobs.request_cancel(job_id)
        else:
            # The job cannot be read while its requester's access resolves.
            await asyncio.sleep(_RECHECK_SECONDS)
    outcome, usage = await _job_outcome(job, runtime)
    if timed_out and outcome[0] == "cancelled":
        outcome = ("timeout", "The agent did not finish in time and was stopped.")
    return outcome, usage, claim


async def _job_outcome(job: BackgroundJob, runtime: AgentRuntime) -> tuple[ChildOutcome, dict[str, object] | None]:
    """Return how the turn of an ended *job* ended and what it used, read from the child's own runs.

    The answer is the run's, whole and as the child wrote it: not the job's text, which adds a receipt. The usage is
    that of every run of the job: the ones restarts cut short, and the one it ended with. A turn that a restart or
    a configuration reload stopped is no answer: it is safe to run again while none of its runs called a tool.
    """
    child = delegation_child(job)
    spent = [replace(child, run_id=run_id) for run_id in job.adapter.get(SPENT_RUNS_KEY, ())]
    *cut_short, record = [await read_child_run(turn, runtime.config, runtime.paths) for turn in (*spent, child)]
    text = (record.handover_content if record is not None else None) or job.result or _NO_ANSWER
    usage: dict[str, object] | None = None
    if records := [run for run in (*cut_short, record) if run is not None]:
        used = [turn_usage(prior_usage_of(run), run.metrics) for run in records]
        usage = {
            "input_tokens": sum(metrics.tokens.input_tokens for metrics in used if metrics is not None),
            "output_tokens": sum(metrics.tokens.output_tokens for metrics in used if metrics is not None),
            "tool_calls": sum(len(run.tools or ()) for run in records),
            "cost_usd": child.cost_usd,
        }
    stopped = record is not None and reload_stopped_child_result(child, record) is not None
    if job.status == "interrupted" or stopped:
        # A run is saved before its first tool call starts, so a turn none of whose runs holds a call made none. A
        # call that waited for a person's decision when the restart came had not started, and its run says so. A
        # run that was cut short and cannot be read may have made some.
        made = [tool for run in records for tool in run.tools or () if tool.confirmed or not _never_started(tool)]
        called = len(records) < len(spent) + (record is not None) or bool(made)
        return ("indeterminate" if called else "interrupted", text), usage
    if job.status == "completed":
        return ("completed", (record.handover_content if record is not None else None) or ""), usage
    return (job.status, text), usage


def _never_started(tool: ToolExecution) -> bool:
    """Return whether a call of a child's run still waited for a decision, or did when a restart ended its turn."""
    return tool.is_paused or tool.result == RESTART_INTERRUPTION_REASON
