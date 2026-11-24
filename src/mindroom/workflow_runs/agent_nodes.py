"""One agent turn of a workflow run as a delegation job: its launch, its wait, and what the job's end makes of the node."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING

from mindroom.ai import run_delegated_child_response
from mindroom.ai_run_metadata import prior_usage_by_run, turn_usage
from mindroom.budgets.pricing import model_prices, run_cost_usd
from mindroom.delegation.background import delegation_child
from mindroom.delegation.cost import SubagentCost, stored_receipts
from mindroom.delegation.execution import delegation_call_job
from mindroom.delegation.lifecycle import (
    DelegationPendingError,
    authorize_delegation,
    prepare_child_turn,
    reload_stopped_child_result,
)
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.state import (
    CONTINUE_SUBAGENT,
    RESTART_INTERRUPTION_REASON,
    RUN_SUBAGENT,
    SPENT_RUNS_KEY,
    SubagentPersona,
)
from mindroom.delegation.targets import resolve_follow_up_target, resolve_fresh_target
from mindroom.llm.messages import CUT_OFF_TEXT_KEY, is_own_model_message
from mindroom.llm.runs import RunStatus
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, JobAccessError
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload
from mindroom.workflow_runs.models import RECHECK_SECONDS, DispatchHandle, WorkflowNodeState
from mindroom.workflow_runs.replay import may_have_written
from mindroom.workflow_runs.structured import SchemaError, check_schema, schema_prompt

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.delegation.targets import DelegationTarget
    from mindroom.llm.runs import RunRecord, ToolExecution
    from mindroom.llm.usage import RunMetrics
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
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
_NOT_READABLE_YET = "The agent of this call cannot be read yet. Try again in a moment."
_NARROWS_WITH_SYSTEM = "tools, skills and knowledge narrow a copy only together with system=."
_SCHEMA_WITH_OUTPUT_PATH = "schema and output_path cannot be used together: return the value and write it."
_INTERRUPTED = "The agent was stopped before it started a tool call. A resume runs it again."
_NO_ANSWER = "The agent's turn left no record of how it ended."
_NODE_ENDED = "The call ended before its agent started."

# How one agent turn ended: `completed` and its answer, or the kind of its error and what to tell the script.
type ChildOutcome = tuple[str, str]
# What one agent turn adds to its run's ledger, by the turn each entry is saved under.
type TurnUsage = dict[int | str, dict[str, object]]
# How the ledger names what a run that a restart cut short spent, before that run's ID: the attempt that settles its
# node did not spend it.
EARLIER_TURN = "cut:"


class AgentNodeError(Exception):
    """An agent turn that did not start: the refusal the script reads."""


class AgentNotReadyError(Exception):
    """An agent turn that cannot be decided yet, such as one for an agent whose bot is still starting."""


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """What the engine runs agent turns on: the job runtime, and the live configuration to read their runs with."""

    jobs: ToolJobRuntime
    config: Config
    paths: RuntimePaths


def delegated_request(arguments: dict[str, object]) -> dict[str, object]:
    """Return the arguments of a `run_subagent` call as the request of the `agent()` call they stand for."""
    names = {theirs: ours for ours, theirs in _RENAMED.items()}
    return {names.get(name, name): value for name, value in arguments.items()}


def schema_failure(request: dict[str, object]) -> str | None:
    """Return why *request*'s schema cannot be used, or None when it is valid or absent."""
    schema = request.get("schema")
    if schema is None:
        return None
    if request.get("output_path") is not None:
        # The file would hold each turn's answer before it is checked, and a call that wrote one is not run again.
        return _SCHEMA_WITH_OUTPUT_PATH
    try:
        check_schema(schema)
    except SchemaError as exc:
        return str(exc)
    return None


def _first_prompt(node: WorkflowNode) -> str:
    """Return what the agent of *node* is sent first: its prompt, and with a schema the instruction to match it."""
    raw = node.request["prompt"] if "prompt" in node.request else node.request.get("message")
    prompt, schema = str(raw), node.request.get("schema")
    return prompt if schema is None else schema_prompt(prompt, schema)


def turn_job_id(run: WorkflowRun, key: str, generation: int, turn: int) -> str:
    """Return the ID of the job one turn of one generation of a node runs as: the same for the same turn, always."""
    identity = [run.execution_identity, run.run_id, key, generation, turn]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


async def load_follow_up_child(
    context: ToolRuntimeContext,
    run: WorkflowRun,
    subagent_id: str,
) -> DelegationChild | None:
    """Load the settled child for *subagent_id*, or return None when continuation is refused."""
    owner = parse_tool_execution_identity_payload(run.execution_identity)
    target = await resolve_follow_up_target(
        {"subagent_id": subagent_id, "message": ""},
        owner=owner,
        config=context.current_config,
        runtime_paths=context.runtime_paths,
        depth=run.depth,
    )
    return None if isinstance(target, str) else target.previous_child


async def _resolve_child_target(
    context: ToolRuntimeContext,
    run: WorkflowRun,
    node: WorkflowNode,
    owner: ToolExecutionIdentity,
    *,
    prompt: str,
    session: WorkflowNode | None,
    follow_up: str | None,
    correction: str | None,
    previous: DelegationChild | None,
) -> tuple[DelegationTarget, DelegationChild | None, str, dict[str, object]]:
    """Resolve a fresh or continued child target and its delegation tool name/arguments."""
    config, paths, caller = context.current_config, context.runtime_paths, run.agent_name
    source = session.request if session is not None else node.request
    arguments = {_RENAMED.get(name, name): value for name, value in source.items() if name in _OPTIONS}
    if session is not None:
        arguments["task"] = prompt
    if correction is None and follow_up is not None:
        call_args: dict[str, object] = {"subagent_id": follow_up, "message": prompt}
        if OUTPUT_PATH_ARGUMENT in arguments:
            call_args[OUTPUT_PATH_ARGUMENT] = arguments[OUTPUT_PATH_ARGUMENT]
        target = await resolve_follow_up_target(
            call_args,
            owner=owner,
            config=config,
            runtime_paths=paths,
            depth=run.depth,
        )
        if isinstance(target, str):
            raise AgentNodeError(target)
        return target, target.previous_child, CONTINUE_SUBAGENT, call_args
    cap = SubagentPersona.from_dict(run.persona) if run.persona else None
    if cap is not None and arguments.get("agent_name") in (None, caller) and not arguments.get("profile"):
        # A copy of a narrowed agent is that agent again: its prompt, and at most what it was left with.
        arguments["system_prompt"] = arguments.get("system_prompt") or cap.system_prompt
    if not arguments.get("system_prompt") and _NARROWING & arguments.keys():
        # `run_subagent` reads a list without a prompt as nothing, which would hand the copy every tool.
        raise AgentNodeError(_NARROWS_WITH_SYSTEM)
    target = resolve_fresh_target(
        arguments,
        caller_name=caller,
        config=config,
        runtime_paths=paths,
        depth=run.depth,
        workspace_root=lambda: resolve_agent_runtime(caller, config, paths, execution_identity=owner).workspace.root,
        cap=cap,
    )
    if isinstance(target, str):
        raise AgentNodeError(target)
    return target, previous, RUN_SUBAGENT, arguments


async def launch_child(
    context: ToolRuntimeContext,
    run: WorkflowRun,
    node: WorkflowNode,
    epoch: int,
    *,
    store: WorkflowRunStore,
    jobs: ToolJobRuntime,
    timeout: float | None = None,  # noqa: ASYNC109 - the seconds of work the turn gets, not a deadline of this call.
    queued: bool = False,
    turn: int = 1,
    correction: str | None = None,
    made: bool = False,
    session: WorkflowNode | None = None,
    follow_up: str | None = None,
) -> WorkflowNode:
    """Start the job of one agent turn of *run* in its owner's *context*, and return the node as saved with its handle.

    The one launch path: the target is resolved and authorized as a subagent's is, under the `workflow` grant, at
    the depth of the run's owner. The node is saved with the handle of its job before the job is started, once, and
    the handle says so afterwards. The child's task is the node's label; the job is sent the whole prompt. A turn
    that may not start raises `AgentNodeError` and saves nothing. A *queued* node is saved already, as waiting for a
    place among its run's working calls, and gets its handle here.

    A call with a `schema` is refused before anything starts when that is no JSON Schema, and its agent is told to
    answer with a value that matches. A further *turn* of the node continues the child of the turn before it with
    *correction*: it is authorized as the first was, and is a job and a ledger entry of its own. *made* says
    whether an earlier turn started a tool call, which the node keeps.
    """
    owner = parse_tool_execution_identity_payload(run.execution_identity)
    earlier = node.handle.ref if turn > 1 and node.handle is not None else None
    known = jobs.retained_job(earlier) if earlier is not None else None
    previous = None if known is None else delegation_child(known)
    # A further turn whose first child is no longer kept starts an agent that never saw the task.
    prompt = _first_prompt(node) if correction is None else correction
    prompt = prompt if correction is None or previous is not None else f"{_first_prompt(node)}\n\n{correction}"
    config, paths, caller = context.current_config, context.runtime_paths, run.agent_name
    with tool_runtime_context(context):
        target, previous, tool_name, call_args = await _resolve_child_target(
            context,
            run,
            node,
            owner,
            prompt=prompt,
            session=session,
            follow_up=follow_up,
            correction=correction,
            previous=previous,
        )
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
            previous=previous,
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
            tool_name=tool_name,
            arguments=call_args,
            run_child=run_delegated_child_response,
            prompt=prompt,
            config=authorized,
            runtime_paths=paths,
            refresh_scheduler=None if orchestrator is None else orchestrator.knowledge_refresh_scheduler,
        )
        if isinstance(spec, str):
            raise AgentNodeError(spec)
        work_until = None if timeout is None else time.time() + timeout
        handle = DispatchHandle("job", job_id, dispatched=False, work_until=work_until)
        links = {"job_id": job_id, "delegation_id": child.delegation_id, "subagent_id": child.subagent_id}
        # And what the call resolved to, which its arguments need not name.
        links |= {"agent": target.agent_name, "model": child.model_name}
        links |= {"turn": turn, "made": made} if turn > 1 else {}
        node = replace(node, state=WorkflowNodeState.RUNNING, handle=handle, links=links)
        stamp = partial(store.update_node, run.run_id, node.key, node.generation, epoch)
        if not queued:
            node = await asyncio.to_thread(store.put_node, node, epoch, dirty_after=True)
        elif not await asyncio.to_thread(stamp, state=node.state, handle=handle, links=links):
            # The node that waited for its place has an end by now: its run ended.
            raise AgentNodeError(_NODE_ENDED)
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
    node = replace(node, handle=replace(handle, dispatched=True))
    await asyncio.to_thread(stamp, handle=node.handle)
    if session is not None:
        links = {
            "subagent_id": child.subagent_id,
            "agent": target.agent_name,
            "model": child.model_name,
            "tip": f"{node.key}:{node.generation}",
        }
        await asyncio.to_thread(store.update_node, run.run_id, session.key, session.generation, epoch, links=links)
    return node


async def await_child(  # noqa: C901
    run: WorkflowRun,
    node: WorkflowNode,
    runtime: AgentRuntime,
    *,
    on_waiting: Callable[[str | None, float | None], Awaitable[object]] | None,
) -> tuple[ChildOutcome, TurnUsage, JobClaim | None, bool]:
    """Wait for the job of *node* to end; return its outcome, usage, claim, and whether a restart continued it.

    The wait is on the saved job ID and starts nothing. *on_waiting* is told when the job starts to wait for a
    person's approval, and with None when it works again; that time is not the turn's. After *timeout* seconds of
    work the job is cancelled, and the wait goes on until it has ended. Without *on_waiting* nothing is waited for:
    a job whose outcome is not this reader's to take now raises `AgentNotReadyError`.
    """
    assert node.handle is not None
    jobs, job_id = runtime.jobs, node.handle.ref
    owner = parse_tool_execution_identity_payload(run.execution_identity)
    wait = partial(jobs.wait, job_id, owner=owner, depth=run.depth)
    if jobs.retained_job(job_id) is None:
        # The one place where a handle without the mark proves something, since here the runtime can be read: a
        # job it never admitted did nothing. One it has since deleted may have done anything.
        kind = "indeterminate" if may_have_written(None if node.handle.dispatched else False) else "interrupted"
        return (kind, _INTERRUPTED), {}, None, False
    # What the turn has left of its working time, as the node's handle says: an earlier wait may have used some.
    handle, loop, timed_out, claim = node.handle, asyncio.get_running_loop(), False, None
    # Whether the node shows the wait for a person that an earlier wait saw begin, which this one must see end.
    waiting = node.state is WorkflowNodeState.WAITING
    left = handle.work_left if waiting or handle.work_until is None else handle.work_until - time.time()
    while True:
        since = loop.time()
        try:
            times = {"approval_timeout": None if waiting else 0, "work_timeout": 0 if waiting else left}
            waited = await wait(**times) if on_waiting is not None else await wait(timeout=0)
        except JobAccessError as exc:
            # A read that is denied says nothing of what the turn did, so its call is not one to run again: the
            # runtime holds the job, which may have started before its handle got the mark that it was handed over.
            return ("indeterminate", str(exc)), {}, None, False
        job, claim = waited.job, waited.claim
        if left is not None and not waiting:
            left -= loop.time() - since
        if job.status in TERMINAL_STATUSES and claim is not None:
            # The outcome is this wait's to read, by the access of now: that it was read before grants nothing, and
            # a job that ended while its requester's access is unresolved is nobody's yet.
            break
        if on_waiting is None:
            raise AgentNotReadyError(_NOT_READABLE_YET)
        if (job.status == "awaiting_approval") != waiting:
            waiting = not waiting
            reason = ("user" if job.adapter.get("wait") == "user" else "approval") if waiting else None
            await on_waiting(reason, left)
        elif left is not None and left <= 0 and job.status not in TERMINAL_STATUSES:
            # Asked once; the job's end then says what the turn had done.
            timed_out, left = True, None
            await jobs.request_cancel(job_id)
        else:
            # The job cannot be read while its requester's access resolves.
            await asyncio.sleep(RECHECK_SECONDS)
    try:
        outcome, usage, continued = await _job_outcome(job, runtime)
    except BaseException:
        # Nothing was read, so nothing is this wait's to save: the outcome is the next reader's.
        await jobs.release_wait(job_id, claim)
        raise
    if timed_out and outcome[0] == "cancelled":
        # Only a turn that had started no tool call: one that had is not run again because its time ran out.
        outcome = ("timeout", "The agent did not finish in time and was stopped.")
    return outcome, usage, claim, continued


async def _job_outcome(job: BackgroundJob, runtime: AgentRuntime) -> tuple[ChildOutcome, TurnUsage, bool]:
    """Return how an ended *job* ended, what it used, and whether a restart continued it.

    The answer is the run's, whole and as the child wrote it: not the job's text, which adds a receipt. The usage is
    that of every run of the job: the ones restarts cut short, and the one it ended with. What the subagents the
    turn itself ran cost is an entry of its own, and no part of the turn's, so no sum holds it twice. A turn that
    did not complete, whatever ended it, is no answer: it is safe to run again only while none of its runs started
    a tool call (`may_have_written`).
    """
    child = delegation_child(job)
    spent = [replace(child, run_id=run_id) for run_id in job.adapter.get(SPENT_RUNS_KEY, ())]
    continued = bool(spent)
    *cut_short, record = [await read_child_run(turn, runtime.config, runtime.paths) for turn in (*spent, child)]
    text = (
        job.result
        if job.status != "completed" and record is not None and record.status == RunStatus.completed and job.result
        else (record.handover_content if record is not None else None) or job.result or _NO_ANSWER
    )
    usage: TurnUsage = {}
    records = [run for run in (*cut_short, record) if run is not None]
    prices = model_prices(runtime.config)
    for is_final, run in (*((False, r) for r in cut_short), (True, record)):
        if run is None:
            continue
        used = turn_usage(tuple(prior_usage_by_run(run.metadata).values()), run.metrics)
        own = run_cost_usd(used, model_provider=run.model_provider, model_id=run.model, prices=prices)
        if (is_final and job.status == "completed") or own is not None or _did_model_work(run, used):
            usage[1 if is_final else f"{EARLIER_TURN}{run.run_id}"] = _spent(run, used, own)
        theirs = child.subagents_cost_usd if is_final else SubagentCost(stored_receipts(run.metadata)).usd
        if theirs is not None:
            # An entry of its own: a turn without a price keeps what its subagents are known to have cost.
            sub_key = f"sub:{child.delegation_id}" if is_final else f"{EARLIER_TURN}sub:{run.run_id}"
            usage[sub_key] = {"cost_usd": theirs}
        if is_final and child.subagents_cost_unknown:
            # A subagent turn it received whose cost nobody knows makes the sum a lower bound, as an unpriced turn
            # does. A delegation that was refused ran no turn, and changes nothing.
            usage[f"sub:{child.delegation_id}:unpriced"] = {"cost_usd": None}
    stopped = record is not None and reload_stopped_child_result(child, record) is not None
    if job.status == "completed" and not stopped:
        # A turn that completed without a record to read its answer from did what nobody can say.
        outcome = ("indeterminate", _NO_ANSWER) if record is None else ("completed", record.handover_content or "")
        return outcome, usage, continued
    # A run is saved before its first tool call starts, so a turn none of whose runs holds a call made none. A call
    # that waited for a person's decision when the restart came had not started, and its run says so. A run that
    # cannot be found may have made some. Only the last run of a turn whose process lived on is none: a run it
    # never saved never reached a call.
    made = [tool for run in records for tool in run.tools or () if tool.confirmed or not _never_started(tool)]
    unread = None in cut_short or (record is None and job.status == "interrupted")
    if may_have_written(None if unread else bool(made)):
        return ("indeterminate", text), usage, continued
    outcome = (job.status, text) if job.status in ("cancelled", "failed") else ("interrupted", _INTERRUPTED)
    return outcome, usage, continued


def _did_model_work(run: RunRecord, used: RunMetrics | None) -> bool:
    """Return whether *run* shows any model work: usage, tool activity, content, or an answered/cut-off model message."""
    if (used is not None and (used.reported or used.details)) or run.tools or run.content:
        return True
    return any(
        is_own_model_message(message) or CUT_OFF_TEXT_KEY in (message.provider_data or {}) for message in run.messages
    )


def _spent(run: RunRecord, used: RunMetrics | None, cost: float | None) -> dict[str, object]:
    """Return the ledger entry of one run of an agent's turn: its tokens, its calls, and *cost*."""
    return {
        "input_tokens": used.tokens.input_tokens if used is not None else 0,
        "output_tokens": used.tokens.output_tokens if used is not None else 0,
        "tool_calls": len(run.tools or ()),
        "cost_usd": cost,
    }


def _never_started(tool: ToolExecution) -> bool:
    """Return whether a call of a child's run still waited for a decision, or did when a restart ended its turn."""
    return tool.is_paused or tool.result == RESTART_INTERRUPTION_REASON
