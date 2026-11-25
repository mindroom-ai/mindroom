"""One agent turn of a workflow run as a delegation job: launch, wait, settlement, schema retries, and prompts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, cast

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
from mindroom.delegation.targets import DelegationTarget, resolve_follow_up_target, resolve_fresh_target
from mindroom.llm.messages import CUT_OFF_TEXT_KEY, is_own_model_message
from mindroom.llm.runs import RunStatus
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.script_runs.broker import ScriptRuntimeUnavailableError
from mindroom.script_runs.models import utc_timestamp
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, JobAccessError
from mindroom.tool_system.output_files import OUTPUT_PATH_ARGUMENT
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import parse_tool_execution_identity_payload
from mindroom.workflow_runs.limits import budget_exceeded
from mindroom.workflow_runs.models import (
    LIVE_NODE_STATES,
    RECHECK_SECONDS,
    DispatchHandle,
    WorkflowNode,
    WorkflowNodeState,
)
from mindroom.workflow_runs.replay import (
    NO_AGENTS,
    OWNER_NOT_READY,
    RUN_ENDED,
    TIMED_PREFIX,
    Outcome,
    indeterminate,
    may_have_written,
    receipt,
    unless_proven,
)
from mindroom.workflow_runs.store import SupersededAttemptError
from mindroom.workflow_runs.structured import (
    SCHEMA_TURNS,
    SchemaError,
    check_schema,
    extract_json,
    schema_prompt,
    validation_error,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.llm.runs import RunRecord, ToolExecution
    from mindroom.llm.usage import RunMetrics
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, JobSpec, ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import NodeReceipt, WorkflowRun
    from mindroom.workflow_runs.store import WorkflowRunStore

logger = get_logger(__name__)

# The options of the SDK's `agent()` as the arguments of `run_subagent`, whose rules resolve them.
_RENAMED = {"prompt": "task", "agent": "agent_name", "system": "system_prompt", "output_path": OUTPUT_PATH_ARGUMENT}
_OPTIONS = frozenset(_RENAMED) | {
    "model",
    "tools",
    "skills",
    "knowledge",
    "profile",
    "minimal",
    "dedicated_worker",
    "resource_profile",
}
_NARROWING = frozenset({"tools", "skills", "knowledge"})
_STOPPED_JOB_STATUSES = frozenset({"cancel_requested", "cancelled", "denied"})
_NOT_READABLE_YET = "The agent of this call cannot be read yet. Try again in a moment."
_NARROWS_WITH_SYSTEM = "tools, skills and knowledge narrow a copy only together with system=."
_SCHEMA_WITH_OUTPUT_PATH = "schema and output_path cannot be used together: return the value and write it."
_INTERRUPTED = "The agent was stopped before it started a tool call. A resume runs it again."
_NO_ANSWER = "The agent's turn left no record of how it ended."
_NODE_ENDED = "The call ended before its agent started."
_NAME_LABELS = 'name="{name}" only labels the node. Use agent="{name}" to run that agent.'
_CORRECTION = "Your answer did not match the schema: {error}. Reply with only the corrected fenced json block."
_NO_MATCH = "Agent '{name}' did not return JSON matching the schema after {turns} attempts. Last error: {error}."
_CORRECTING = "The agent of this call is correcting its answer. Try again in a moment."
_EARLIER_TURN = "cut:"

# How one agent turn ended: `completed` and its answer, or the kind of its error and what to tell the script.
type ChildOutcome = tuple[str, str]
# Ledger entries one agent turn adds: (ledger_key, attempt, usage_payload).
type TurnUsage = list[tuple[int | str, int, dict[str, object]]]


class AgentNodeError(Exception):
    """An agent turn that did not start: the refusal the script reads."""


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


def first_prompt(node: WorkflowNode) -> str:
    """Return what the agent of *node* is sent first: its prompt, and with a schema the instruction to match it."""
    raw = node.request["prompt"] if "prompt" in node.request else node.request.get("message")
    prompt, schema = str(raw), node.request.get("schema")
    return prompt if schema is None else schema_prompt(prompt, schema)


def turn_job_id(run: WorkflowRun, key: str, generation: int, turn: int) -> str:
    """Return the ID of the job one turn of one generation of a node runs as: the same for the same turn, always."""
    identity = [run.execution_identity, run.run_id, key, generation, turn]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def works(runtime: AgentRuntime | None, job_id: str) -> bool:
    """Return whether the job still works in *runtime*."""
    job = None if runtime is None else runtime.jobs.retained_job(job_id)
    return job is not None and job.status not in TERMINAL_STATUSES


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


def _prepare_turn_spec(
    context: ToolRuntimeContext,
    run: WorkflowRun,
    node: WorkflowNode,
    owner: ToolExecutionIdentity,
    job_runtime: ToolJobRuntime,
    *,
    prompt: str,
    job_id: str,
    session: WorkflowNode | None,
    follow_up: DelegationChild | None,
    correction: str | None,
    previous: DelegationChild | None,
) -> tuple[DelegationTarget, DelegationChild, JobSpec]:
    """Resolve a fresh or continued child target, authorize it under `workflow`, and build its job spec."""
    config, paths, caller = context.current_config, context.runtime_paths, run.agent_name
    source = session.request if session is not None else node.request
    arguments = {_RENAMED.get(name, name): value for name, value in source.items() if name in _OPTIONS}
    if session is not None:
        arguments["task"] = prompt
    if correction is None and follow_up is not None:
        call_args: dict[str, object] = {"subagent_id": follow_up.subagent_id, "message": prompt}
        if OUTPUT_PATH_ARGUMENT in arguments:
            call_args[OUTPUT_PATH_ARGUMENT] = arguments[OUTPUT_PATH_ARGUMENT]
        target = DelegationTarget(follow_up.child_agent_name, prompt, previous_child=follow_up)
        previous, tool_name = follow_up, CONTINUE_SUBAGENT
    else:
        cap = SubagentPersona.from_dict(run.persona) if run.persona else None
        if cap is not None and arguments.get("agent_name") in (None, caller) and not arguments.get("profile"):
            # A copy of a narrowed agent is that agent again: its prompt, and at most what it was left with.
            arguments["system_prompt"] = arguments.get("system_prompt") or cap.system_prompt
        if not arguments.get("system_prompt") and _NARROWING & arguments.keys():
            # `run_subagent` reads a list without a prompt as nothing, which would hand the copy every tool.
            raise AgentNodeError(_NARROWS_WITH_SYSTEM)
        resolved = resolve_fresh_target(
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
        if isinstance(resolved, str):
            raise AgentNodeError(resolved)
        target, tool_name, call_args = resolved, RUN_SUBAGENT, arguments
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
        raise ScriptRuntimeUnavailableError(str(exc)) from exc
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
    child.workflow_run_id, child.workflow_node_key, child.workflow_generation = run.run_id, node.key, node.generation
    orchestrator = context.orchestrator
    spec = delegation_call_job(
        child,
        job_id=job_id,
        runtime=job_runtime,
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
    return target, child, spec


def _workflow_source_kind(
    run: WorkflowRun,
    *,
    store: WorkflowRunStore,
    jobs: ToolJobRuntime,
    fallback: str | None,
) -> str | None:
    """Return the owning workflow job's ``source_kind``, walking nested runs when needed."""
    current: WorkflowRun | None = run
    seen: set[str] = set()
    while current is not None and current.run_id not in seen:
        seen.add(current.run_id)
        for job_id in (current.job_id, current.origin_job_id):
            if job_id is not None and (job := jobs.retained_job(job_id)) is not None:
                return job.source_kind
        if current.parent_run_id is None:
            break
        try:
            current = store.get_run(current.parent_run_id)
        except ValueError:
            break
    return fallback


async def launch_child(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    epoch: int,
    *,
    timeout: float | None = None,  # noqa: ASYNC109 - seconds of work the turn gets, not a call deadline.
    turn: int = 1,
    correction: str | None = None,
    made: bool = False,
    follow_up: DelegationChild | None = None,
    jobs: ToolJobRuntime | None = None,
) -> WorkflowNode:
    """Start the job of one agent turn of *run* in its owner's live context, and return the node with its handle.

    The one launch path: the target is resolved and authorized as a subagent's is, under the `workflow` grant, at
    the depth of the run's owner. Turn 1 logs the label-vs-agent notice when applicable. The node is saved with the
    handle of its job before the job is started, once, and the handle says so afterwards. The child's task is the
    node's label; the job is sent the whole prompt. A turn that may not start raises `AgentNodeError` and saves
    nothing.

    A call with a `schema` is refused before anything starts when that is no JSON Schema, and its agent is told to
    answer with a value that matches. A further *turn* of the node continues the child of the turn before it with
    *correction*: it is authorized as the first was, and is a job and a ledger entry of its own. *made* says
    whether an earlier turn started a tool call, which the node keeps.
    """
    context, runtime = engine.authorize(run), engine.agents()
    if context is None:
        raise ScriptRuntimeUnavailableError(OWNER_NOT_READY)
    if isinstance(context, str) or runtime is None:
        raise AgentNodeError(context if isinstance(context, str) else NO_AGENTS)
    store, job_runtime = engine.store, jobs or runtime.jobs
    session = store.get_node(run.run_id, node.session_key) if node.session_key else None
    source = session.request if session is not None else node.request
    first_turn = node.kind != "turn" or int(node.request.get("turn", 1)) == 1
    if turn == 1 and first_turn and not source.get("agent") and node.name in context.current_config.agents:
        await asyncio.to_thread(store.append_log, run.run_id, epoch, _NAME_LABELS.format(name=node.name))

    owner = parse_tool_execution_identity_payload(run.execution_identity)
    earlier = node.handle.ref if turn > 1 and node.handle is not None else None
    known = job_runtime.retained_job(earlier) if earlier is not None else None
    previous = None if known is None else delegation_child(known)
    # A further turn whose first child is no longer kept starts an agent that never saw the task.
    prompt = first_prompt(node) if correction is None else correction
    if correction is not None and previous is None:
        prompt = f"{first_prompt(node)}\n\n{correction}"
    source_kind = _workflow_source_kind(run, store=store, jobs=job_runtime, fallback=context.source_kind)
    if source_kind is not None and context.source_kind != source_kind:
        context = replace(context, source_kind=source_kind)

    job_id = turn_job_id(run, node.key, node.generation, turn)
    with tool_runtime_context(context):
        target, child, spec = _prepare_turn_spec(
            context,
            run,
            node,
            owner,
            job_runtime,
            prompt=prompt,
            job_id=job_id,
            session=session,
            follow_up=follow_up,
            correction=correction,
            previous=previous,
        )
        handle = DispatchHandle(
            "job",
            job_id,
            dispatched=False,
            work_until=None if timeout is None else time.time() + timeout,
        )
        links = {
            "job_id": job_id,
            "delegation_id": child.delegation_id,
            "subagent_id": child.subagent_id,
            # And what the call resolved to, which its arguments need not name.
            "agent": target.agent_name,
            "model": child.model_name,
            **({"turn": turn, "made": made} if turn > 1 else {}),
        }
        node = replace(node, state=WorkflowNodeState.RUNNING, handle=handle, links=links)
        if (saved := await asyncio.to_thread(store.save_dispatched, node, epoch)) is None:
            # The node that waited for its place has an end by now: its run ended.
            raise AgentNodeError(_NODE_ENDED)
        node = saved
        try:
            # In the owner's context, which the job's task keeps: the child runs in the run's conversation.
            _, claim = await job_runtime.start_spec(
                job_id,
                spec,
                owner=owner,
                depth=run.depth,
                source_kind=source_kind,
            )
        except JobAccessError as exc:
            raise AgentNodeError(str(exc)) from exc
    await job_runtime.release_wait(job_id, claim)
    node = replace(node, handle=replace(handle, dispatched=True))
    await asyncio.to_thread(store.update_node, run.run_id, node.key, node.generation, epoch, handle=node.handle)
    if session is not None:
        session_links = {"subagent_id": child.subagent_id, "agent": target.agent_name, "model": child.model_name}
        await asyncio.to_thread(
            store.update_node,
            run.run_id,
            session.key,
            session.generation,
            epoch,
            links=session_links,
        )
    return node


async def await_child(  # noqa: C901
    run: WorkflowRun,
    node: WorkflowNode,
    runtime: AgentRuntime,
    *,
    on_waiting: Callable[[str | None, float | None], Awaitable[object]] | None,
) -> tuple[ChildOutcome, TurnUsage, JobClaim | None]:
    """Wait for the job of *node* to end; return its outcome, usage, and claim.

    The wait is on the saved job ID and starts nothing. *on_waiting* is told when the job starts to wait for a
    person's approval, and with None when it works again; that time is not the turn's. After the handle's working
    time runs out the job is cancelled, and the wait goes on until it has ended. Without *on_waiting* nothing is
    waited for: a job whose outcome is not this reader's to take now raises `ScriptRuntimeUnavailableError`.
    """
    assert node.handle is not None
    jobs, job_id = runtime.jobs, node.handle.ref
    owner = parse_tool_execution_identity_payload(run.execution_identity)
    wait = partial(jobs.wait, job_id, owner=owner, depth=run.depth)
    if jobs.retained_job(job_id) is None:
        # The one place where a handle without the mark proves something, since here the runtime can be read: a
        # job it never admitted did nothing. One it has since deleted may have done anything.
        kind = "indeterminate" if may_have_written(None if node.handle.dispatched else False) else "interrupted"
        return (kind, _INTERRUPTED), [], None
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
            return ("indeterminate", str(exc)), [], None
        job, claim = waited.job, waited.claim
        if left is not None and not waiting:
            left -= loop.time() - since
        if job.status in TERMINAL_STATUSES and claim is not None:
            # The outcome is this wait's to read, by the access of now: that it was read before grants nothing, and
            # a job that ended while its requester's access is unresolved is nobody's yet.
            break
        if on_waiting is None:
            raise ScriptRuntimeUnavailableError(_NOT_READABLE_YET)
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
        outcome, usage = await _job_outcome(job, runtime, run, node)
    except BaseException:
        # Nothing was read, so nothing is this wait's to save: the outcome is the next reader's.
        await jobs.release_wait(job_id, claim)
        raise
    if timed_out and outcome[0] == "cancelled":
        # Only a turn that had started no tool call: one that had is not run again because its time ran out.
        outcome = ("timeout", "The agent did not finish in time and was stopped.")
    return outcome, usage, claim


async def _job_outcome(
    job: BackgroundJob,
    runtime: AgentRuntime,
    reading: WorkflowRun,
    node: WorkflowNode,
) -> tuple[ChildOutcome, TurnUsage]:
    """Return how an ended *job* ended and the ledger entries it adds for *node* as read by *reading*.

    The answer is the run's, whole and as the child wrote it: not the job's text, which adds a receipt. The usage is
    that of every run of the job: the ones restarts cut short, and the one it ended with. What the subagents the
    turn itself ran cost is an entry of its own, and no part of the turn's, so no sum holds it twice. A turn that
    did not complete, whatever ended it, is no answer: it is safe to run again only while none of its runs started
    a tool call (`may_have_written`).
    """
    child = delegation_child(job)
    spent = [replace(child, run_id=run_id) for run_id in job.adapter.get(SPENT_RUNS_KEY, ())]
    continued, turn_num = bool(spent), int(node.links.get("turn", 1))
    *cut_short, record = [await read_child_run(turn, runtime.config, runtime.paths) for turn in (*spent, child)]
    text = (
        job.result
        if job.status != "completed" and record is not None and record.status == RunStatus.completed and job.result
        else (record.handover_content if record is not None else None) or job.result or _NO_ANSWER
    )
    usage: TurnUsage = []
    records = [rec for rec in (*cut_short, record) if rec is not None]
    prices = model_prices(runtime.config)
    for is_final, rec in (*((False, r) for r in cut_short), (True, record)):
        if rec is None:
            continue
        # A turn that no restart continued worked in the attempt that started it, whichever attempt reads it. What
        # a restart cut short is of the attempt before the one that reads the turn's end, even if it left no row.
        attempt = (reading.attempt if is_final else reading.attempt - 1) if continued else node.attempt
        used = turn_usage(tuple(prior_usage_by_run(rec.metadata).values()), rec.metrics)
        own = run_cost_usd(used, model_provider=rec.model_provider, model_id=rec.model, prices=prices)
        if (is_final and job.status == "completed") or own is not None or _did_model_work(rec, used):
            key: int | str = turn_num if is_final else f"{_EARLIER_TURN}{rec.run_id}"
            usage.append((key, attempt, _spent(rec, used, own)))
        theirs = child.subagents_cost_usd if is_final else SubagentCost(stored_receipts(rec.metadata)).usd
        if theirs is not None:
            # An entry of its own: a turn without a price keeps what its subagents are known to have cost.
            sub_key = f"sub:{child.delegation_id}" if is_final else f"{_EARLIER_TURN}sub:{rec.run_id}"
            usage.append((sub_key, attempt, {"cost_usd": theirs}))
        if is_final and child.subagents_cost_unknown:
            # A subagent turn it received whose cost nobody knows makes the sum a lower bound, as an unpriced turn
            # does. A delegation that was refused ran no turn, and changes nothing.
            usage.append((f"sub:{child.delegation_id}:unpriced", attempt, {"cost_usd": None}))
    stopped = record is not None and reload_stopped_child_result(child, record) is not None
    if job.status == "completed" and not stopped:
        # A turn that completed without a record to read its answer from did what nobody can say.
        outcome = ("indeterminate", _NO_ANSWER) if record is None else ("completed", record.handover_content or "")
        return outcome, usage
    # A run is saved before its first tool call starts, so a turn none of whose runs holds a call made none. A call
    # that waited for a person's decision when the restart came had not started, and its run says so. A run that
    # cannot be found may have made some. Only the last run of a turn whose process lived on is none: a run it
    # never saved never reached a call.
    made = [tool for rec in records for tool in rec.tools or () if tool.confirmed or not _never_started(tool)]
    unread = None in cut_short or (record is None and job.status == "interrupted")
    if may_have_written(None if unread else bool(made)):
        return ("indeterminate", text), usage
    return ((job.status, text) if job.status in ("cancelled", "failed") else ("interrupted", _INTERRUPTED)), usage


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


def _answer_outcome(node: WorkflowNode, text: str, *, last: bool, made: bool, stopping: bool) -> Outcome | str:
    """Return how an agent's call ends on its answer, or what a further turn tells an agent that missed its schema.

    The answer is the call's result, or with a schema the value in it when that matches. A run that is ending
    starts no further turn, and after the *last* turn the call fails with kind `schema`: neither is run again
    unasked when a turn *made* a tool call.
    """
    if "schema" not in node.request:
        return WorkflowNodeState.COMPLETED, text, None
    value = None
    try:
        value = extract_json(text)
    except SchemaError as exc:
        wrong: str | None = str(exc)
    else:
        try:
            wrong = validation_error(value, node.request["schema"])
        except SchemaError as exc:
            # The schema itself cannot be applied, which no further turn changes.
            broken: Outcome = WorkflowNodeState.FAILED, None, {"kind": "schema", "message": str(exc)}
            return unless_proven(node, made, broken, str(exc))
    if wrong is None:
        return WorkflowNodeState.COMPLETED, value, None
    if stopping:
        ended: Outcome = WorkflowNodeState.CANCELLED, None, {"kind": "cancelled", "message": RUN_ENDED}
        return unless_proven(node, made, ended)
    if not last:
        return _CORRECTION.format(error=wrong)
    said = _NO_MATCH.format(name=node.name or node.label, turns=SCHEMA_TURNS, error=wrong)
    return unless_proven(node, made, (WorkflowNodeState.FAILED, None, {"kind": "schema", "message": said}), said)


def _turn_outcome(
    node: WorkflowNode,
    kind: str,
    text: str,
    *,
    last: bool,
    made: bool,
    stopping: bool,
) -> Outcome | str:
    """Return how an agent's call ends on the end of one of its turns, or what a further turn tells the agent.

    A turn that did not complete ends the call. Whatever ended it proves nothing of a tool call that an earlier
    turn of the call made, so such a call is not run again unasked.
    """
    if kind == "completed":
        return _answer_outcome(node, text, last=last, made=made, stopping=stopping)
    if kind == "indeterminate" or node.links.get("made"):
        return indeterminate(node, None if kind == "indeterminate" else text)
    state = WorkflowNodeState.CANCELLED if kind == "cancelled" else WorkflowNodeState.FAILED
    return state, None, {"kind": kind, "message": text}


async def dispatch_agent(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    *,
    timeout: float | None,  # noqa: ASYNC109 - seconds of work the turn gets, not a call deadline.
    follow_up: DelegationChild | None = None,
) -> NodeReceipt:
    """Start the job of an agent node and wait for it as the engine's own task; a refusal is the node's end.

    The turn starts in the live context of the run's owner, as an attempt does. While that cannot be answered
    the request fails as a transport error does, which the script's SDK repeats, and an already-saved queued
    node is tried again.
    """
    try:
        node = await launch_child(engine, run, node, run.epoch, timeout=timeout, follow_up=follow_up)
    except AgentNodeError as exc:
        # Nothing ran. The node is saved already when the job runtime refused the job it names.
        error: dict[str, object] = {"kind": "refused", "message": str(exc)}
        if not await engine.settle_node(node, run.epoch, state=WorkflowNodeState.FAILED, error=error) and not node.seq:
            refused = replace(node, state=WorkflowNodeState.FAILED, error=error, ended_at=utc_timestamp())
            await asyncio.to_thread(engine.store.put_node, refused, run.epoch, dirty_after=True)
        engine.drop_slot(run.run_id, node.key)
        return receipt(cast("WorkflowNode", engine.store.get_node(run.run_id, node.key)))
    except BaseException:
        # The start failed in some other way, or the request was cancelled. A node that names its job already
        # is settled from that job, which ran nothing if it never started; the request itself fails, and the
        # script's SDK sends it again.
        saved = engine.store.get_node(run.run_id, node.key)
        if saved is not None and saved.generation == node.generation and saved.handle is not None:
            wait_for_agent(engine, run, saved, run.epoch)
        raise
    wait_for_agent(engine, run, node, run.epoch)
    await engine.restate(run.run_id)
    return receipt(node)


def wait_for_agent(engine: WorkflowEngine, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
    """Start the engine's wait for the job of an agent node, in place of an earlier attempt's wait for it.

    The wait for a turn with a timeout is not stopped with its attempt (`end_run`), so somebody stops the turn
    on time between two attempts as well. What it reads then it cannot save, and leaves to the next wait.
    """
    assert node.handle is not None
    if engine.closed:
        # A closed engine starts nothing: the job stays as it is, for the next start to continue or to read.
        return
    name = f"{TIMED_PREFIX}{node.handle.ref}"
    tasks = engine._runs[run.run_id].tasks if run.run_id in engine._runs else ()
    for stale in [task for task in tasks if task.get_name() == name]:
        stale.cancel()
    # An agent that an earlier attempt started holds a place (including while waiting on approval), over the limit if it must.
    engine.places_for(run).working.add(node.key)
    task = engine.spawn(run.run_id, _run_agent_node(engine, run, node, epoch))
    if node.handle.work_until is not None or node.handle.work_left is not None:
        task.set_name(name)


async def _run_agent_node(engine: WorkflowEngine, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
    """Wait for the job of an agent node and settle the node as its turn ended, then wake the run's waiters."""
    try:
        await settle_agent(engine, run, node, epoch)
        await engine.release_slot(node.run_id, node.key)
    except BaseException:
        engine.drop_slot(node.run_id, node.key)
        raise


async def settle_agent(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    epoch: int,
    *,
    block: bool = True,
) -> None:
    """Settle an agent node from its job, once that has ended, for the attempt that began with *epoch*.

    The turn's usage and its answer are saved before the job's outcome is acknowledged, so a primary that dies
    in between reads the same job again. A run that moved on meanwhile reads the job itself. Without *block*,
    for a caller that holds the run's lock, a job that cannot be read now raises `ScriptRuntimeUnavailableError`.

    An answer that does not match the call's schema starts a further turn of the same agent, which is waited
    for here, unless the run is ending or the turn was the last the agent gets.
    """
    while (further := await _settle_turn(engine, run, node, epoch, block=block)) is not None:
        node = further
        if not block:
            # The further turn runs now: its end is the next reader's.
            raise ScriptRuntimeUnavailableError(_CORRECTING)
        task, timed = asyncio.current_task(), f"{TIMED_PREFIX}{cast('DispatchHandle', node.handle).ref}"
        if task is not None and task.get_name().startswith(TIMED_PREFIX):
            # The wait is found by the job it waits for, by the attempt that takes it over.
            task.set_name(timed)


async def _settle_turn(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    epoch: int,
    *,
    block: bool,
) -> WorkflowNode | None:
    """Read the job of one turn of an agent node and settle the node, or return it with its further turn started."""
    assert node.handle is not None
    if (runtime := engine.agents()) is None:
        # Without a job runtime nobody can read the job, now.
        raise ScriptRuntimeUnavailableError(NO_AGENTS)
    try:
        waits = partial(node_waits, engine, node, epoch) if block else None
        (kind, text), usage, claim = await await_child(run, node, runtime, on_waiting=waits)
    except ScriptRuntimeUnavailableError:
        raise
    except Exception as exc:
        logger.exception("workflow_agent_outcome_unreadable", run_id=node.run_id, key=node.key)
        # What the turn did is unknown, so its call is not one to run again without being asked to.
        (kind, text), usage, claim = ("indeterminate", str(exc)), [], None
    # A further turn of the node is an entry of its own, and the node keeps whether any turn started a tool call.
    turn = int(node.links.get("turn", 1))
    made = bool(node.links.get("made")) or any(used.get("tool_calls") for _, _, used in usage)
    further = None
    try:
        with suppress(SupersededAttemptError):
            add = partial(engine.store.add_usage, node.run_id, node.key, node.generation, epoch=epoch)
            for entry, attempt, used in usage:
                await asyncio.to_thread(add, entry, attempt, used)
            outcome: Outcome | str | WorkflowNode = _turn_outcome(
                node,
                kind,
                text,
                last=turn >= SCHEMA_TURNS,
                made=made,
                stopping=engine.is_stopping(node.run_id),
            )
            if isinstance(outcome, str):
                outcome = await _correct(engine, run, node, epoch, outcome, turn=turn + 1, made=made, block=block)
            if isinstance(outcome, WorkflowNode):
                further = outcome
            else:
                state, result, error = outcome
                await engine.settle_node(node, epoch, state=state, result=result, error=error)
            if claim is not None:
                await runtime.jobs.acknowledge_wait(node.handle.ref, claim)
    finally:
        # An outcome this wait did not save, because the run moved on or the wait was cancelled, is the next
        # reader's. After the acknowledgement there is no claim left to give back.
        await runtime.jobs.release_wait(node.handle.ref, claim)
    return further


async def _correct(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    epoch: int,
    correction: str,
    *,
    turn: int,
    made: bool,
    block: bool,
) -> WorkflowNode | Outcome:
    """Start the further *turn* of an agent node whose answer did not match its schema; return the node with it.

    The turn is authorized as every call of the run is, and gets what the call has left of its working time.
    One that may not start is the node's end, which is returned. While that cannot be decided the turn waits,
    or raises `ScriptRuntimeUnavailableError` without *block*.
    """
    saved = engine.store.get_node(node.run_id, node.key)
    current = saved.handle if saved is not None and saved.generation == node.generation else None
    handle = current or cast("DispatchHandle", node.handle)
    while True:
        left = handle.work_left if handle.work_until is None else max(0.0, handle.work_until - time.time())
        try:
            reason = await asyncio.to_thread(
                budget_exceeded,
                engine.store,
                engine.store.get_run(node.run_id),
            )
            if reason is not None:
                stopped: Outcome = WorkflowNodeState.CANCELLED, None, {"kind": "cancelled", "message": reason}
                return unless_proven(node, made, stopped)
            return await launch_child(
                engine,
                run,
                node,
                epoch,
                timeout=left,
                turn=turn,
                correction=correction,
                made=made,
            )
        except ScriptRuntimeUnavailableError:
            if not block:
                raise
            await asyncio.sleep(RECHECK_SECONDS)
        except AgentNodeError as exc:
            refused: Outcome = WorkflowNodeState.FAILED, None, {"kind": "refused", "message": str(exc)}
            return unless_proven(node, made, refused)


async def node_waits(
    engine: WorkflowEngine,
    node: WorkflowNode,
    epoch: int,
    on: str | None,
    left: float | None = None,
    *,
    gate_only: bool = False,
) -> None:
    """Show a node as waiting for a person, for what *on* says, or as running again with None; its run follows.

    A call waiting on a human approval keeps its concurrency place until it finishes; a *gate_only* call holds no
    place yet and returns to `queued` for admission. *left* is the working time an agent's turn has left, which its
    handle keeps for whoever waits for it next.
    """
    state = WorkflowNodeState.WAITING if on else (WorkflowNodeState.QUEUED if gate_only else WorkflowNodeState.RUNNING)
    changes: dict[str, object] = {"state": state, "waiting_on": on}
    if left is not None and node.handle is not None:
        changes["handle"] = replace(
            node.handle,
            work_left=left if on else None,
            work_until=None if on else time.time() + left,
        )
    await engine.stamp(node, epoch, ignore=True, **changes)
    await engine.notify(node.run_id)


def node_prompt(engine: WorkflowEngine, run_id: str, key: str | None, generation: int | None) -> str | None:
    """Return the prompt of the agent call that is *generation* of the node at *key*, while it waits for its agent.

    None once the call has an outcome, was replaced, or went with its run: nothing waits for its job any more.
    """
    node = engine.store.get_node(run_id, key) if key else None
    live = node is not None and node.generation == generation and node.state in LIVE_NODE_STATES
    if live and (runtime := engine.agents()) is not None:
        # A Stop of the run's own job ends the run at its next pass: until then nothing of the run goes on.
        owner = engine.store.get_run(run_id).job_id
        job = runtime.jobs.retained_job(owner) if owner is not None else None
        live = job is None or job.status not in _STOPPED_JOB_STATUSES
    if node is None or not live or not isinstance(node.request.get("prompt"), str):
        return None
    return first_prompt(node)
