"""Agent/turn node execution: target resolution, launch, wait, usage extraction, schema retries, and prompts."""

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
    from mindroom.tool_jobs.runtime import BackgroundJob, BackgroundJobSpec, JobClaim, ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import NodeReceipt, WorkflowRun

logger = get_logger(__name__)

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

type ChildOutcome = tuple[str, str]
type TurnUsage = list[tuple[int | str, int, dict[str, object]]]


class AgentNodeError(Exception):
    """An agent turn that did not start: the refusal returned to the script."""


@dataclass(frozen=True, slots=True)
class AgentRuntime:
    """The job runtime and live configuration used to run and inspect agent turns."""

    jobs: ToolJobRuntime
    config: Config
    paths: RuntimePaths


def delegated_request(arguments: dict[str, object]) -> dict[str, object]:
    """Map `run_subagent` arguments to the equivalent `agent()` node request keys."""
    names = {theirs: ours for ours, theirs in _RENAMED.items()}
    return {names.get(name, name): value for name, value in arguments.items()}


def schema_failure(request: dict[str, object]) -> str | None:
    """Return why *request*'s schema cannot be used, or None when valid or absent."""
    schema = request.get("schema")
    if schema is None:
        return None
    if request.get("output_path") is not None:
        return _SCHEMA_WITH_OUTPUT_PATH
    try:
        check_schema(schema)
    except SchemaError as exc:
        return str(exc)
    return None


def first_prompt(node: WorkflowNode) -> str:
    """Return the initial prompt for *node*, appending JSON Schema instructions when present."""
    raw = node.request["prompt"] if "prompt" in node.request else node.request.get("message")
    prompt, schema = str(raw), node.request.get("schema")
    return prompt if schema is None else schema_prompt(prompt, schema)


def turn_job_id(run: WorkflowRun, key: str, generation: int, turn: int) -> str:
    """Return the deterministic job ID for one turn of one node generation."""
    identity = [run.execution_identity, run.run_id, key, generation, turn]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def works(runtime: AgentRuntime | None, job_id: str) -> bool:
    """Return whether *job_id* is still active in *runtime*."""
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


async def _resolve_child_target(
    context: ToolRuntimeContext,
    run: WorkflowRun,
    node: WorkflowNode,
    owner: ToolExecutionIdentity,
    *,
    prompt: str,
    session: WorkflowNode | None,
    follow_up: DelegationChild | None,
    correction: str | None,
    previous: DelegationChild | None,
) -> tuple[DelegationTarget, DelegationChild | None, str, dict[str, object]]:
    config, paths, caller = context.current_config, context.runtime_paths, run.agent_name
    source = session.request if session is not None else node.request
    arguments = {_RENAMED.get(name, name): value for name, value in source.items() if name in _OPTIONS}
    if session is not None:
        arguments["task"] = prompt
    if correction is None and follow_up is not None:
        call_args: dict[str, object] = {"subagent_id": follow_up.subagent_id, "message": prompt}
        if OUTPUT_PATH_ARGUMENT in arguments:
            call_args[OUTPUT_PATH_ARGUMENT] = arguments[OUTPUT_PATH_ARGUMENT]
        return (
            DelegationTarget(follow_up.child_agent_name, prompt, previous_child=follow_up),
            follow_up,
            CONTINUE_SUBAGENT,
            call_args,
        )
    cap = SubagentPersona.from_dict(run.persona) if run.persona else None
    if cap is not None and arguments.get("agent_name") in (None, caller) and not arguments.get("profile"):
        arguments["system_prompt"] = arguments.get("system_prompt") or cap.system_prompt
    if not arguments.get("system_prompt") and _NARROWING & arguments.keys():
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


def _prepare_turn_spec(
    context: ToolRuntimeContext,
    run: WorkflowRun,
    node: WorkflowNode,
    owner: ToolExecutionIdentity,
    job_runtime: ToolJobRuntime,
    *,
    target: DelegationTarget,
    previous: DelegationChild | None,
    tool_name: str,
    call_args: dict[str, object],
    prompt: str,
    job_id: str,
) -> tuple[DelegationChild, BackgroundJobSpec]:
    config, paths, caller = context.current_config, context.runtime_paths, run.agent_name
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
    return child, spec


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
    """Authorize and start one agent turn job for *node*, saving its handle before launching."""
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
    prompt = first_prompt(node) if correction is None else correction
    if correction is not None and previous is None:
        prompt = f"{first_prompt(node)}\n\n{correction}"

    job_id = turn_job_id(run, node.key, node.generation, turn)
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
        child, spec = _prepare_turn_spec(
            context,
            run,
            node,
            owner,
            job_runtime,
            target=target,
            previous=previous,
            tool_name=tool_name,
            call_args=call_args,
            prompt=prompt,
            job_id=job_id,
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
            "agent": target.agent_name,
            "model": child.model_name,
            **({"turn": turn, "made": made} if turn > 1 else {}),
        }
        node = replace(node, state=WorkflowNodeState.RUNNING, handle=handle, links=links)
        if (saved := await asyncio.to_thread(store.save_dispatched, node, epoch)) is None:
            raise AgentNodeError(_NODE_ENDED)
        node = saved
        try:
            _, claim = await job_runtime.start_spec(job_id, spec, owner=owner, depth=run.depth)
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
    """Wait on *node*'s saved job ID until terminal, pausing work-time during approval waits."""
    assert node.handle is not None
    jobs, job_id = runtime.jobs, node.handle.ref
    owner = parse_tool_execution_identity_payload(run.execution_identity)
    wait = partial(jobs.wait, job_id, owner=owner, depth=run.depth)
    if jobs.retained_job(job_id) is None:
        kind = "indeterminate" if may_have_written(None if node.handle.dispatched else False) else "interrupted"
        return (kind, _INTERRUPTED), [], None
    handle, loop, timed_out, claim = node.handle, asyncio.get_running_loop(), False, None
    waiting = node.state is WorkflowNodeState.WAITING
    left = handle.work_left if waiting or handle.work_until is None else handle.work_until - time.time()
    while True:
        since = loop.time()
        try:
            times = {"approval_timeout": None if waiting else 0, "work_timeout": 0 if waiting else left}
            waited = await wait(**times) if on_waiting is not None else await wait(timeout=0)
        except JobAccessError as exc:
            return ("indeterminate", str(exc)), [], None
        job, claim = waited.job, waited.claim
        if left is not None and not waiting:
            left -= loop.time() - since
        if job.status in TERMINAL_STATUSES and claim is not None:
            break
        if on_waiting is None:
            raise ScriptRuntimeUnavailableError(_NOT_READABLE_YET)
        if (job.status == "awaiting_approval") != waiting:
            waiting = not waiting
            reason = ("user" if job.adapter.get("wait") == "user" else "approval") if waiting else None
            await on_waiting(reason, left)
        elif left is not None and left <= 0 and job.status not in TERMINAL_STATUSES:
            timed_out, left = True, None
            await jobs.request_cancel(job_id)
        else:
            await asyncio.sleep(RECHECK_SECONDS)
    try:
        outcome, usage = await _job_outcome(job, runtime, run, node)
    except BaseException:
        await jobs.release_wait(job_id, claim)
        raise
    if timed_out and outcome[0] == "cancelled":
        outcome = ("timeout", "The agent did not finish in time and was stopped.")
    return outcome, usage, claim


async def _job_outcome(
    job: BackgroundJob,
    runtime: AgentRuntime,
    reading: WorkflowRun,
    node: WorkflowNode,
) -> tuple[ChildOutcome, TurnUsage]:
    """Read an ended job's child run records, extract own/subagent usage entries, and classify the outcome."""
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
        attempt = (reading.attempt if is_final else reading.attempt - 1) if continued else node.attempt
        used = turn_usage(tuple(prior_usage_by_run(rec.metadata).values()), rec.metrics)
        own = run_cost_usd(used, model_provider=rec.model_provider, model_id=rec.model, prices=prices)
        if (is_final and job.status == "completed") or own is not None or _did_model_work(rec, used):
            key: int | str = turn_num if is_final else f"{_EARLIER_TURN}{rec.run_id}"
            usage.append((key, attempt, _spent(rec, used, own)))
        theirs = child.subagents_cost_usd if is_final else SubagentCost(stored_receipts(rec.metadata)).usd
        if theirs is not None:
            sub_key = f"sub:{child.delegation_id}" if is_final else f"{_EARLIER_TURN}sub:{rec.run_id}"
            usage.append((sub_key, attempt, {"cost_usd": theirs}))
        if is_final and child.subagents_cost_unknown:
            usage.append((f"sub:{child.delegation_id}:unpriced", attempt, {"cost_usd": None}))
    stopped = record is not None and reload_stopped_child_result(child, record) is not None
    if job.status == "completed" and not stopped:
        outcome = ("indeterminate", _NO_ANSWER) if record is None else ("completed", record.handover_content or "")
        return outcome, usage
    made = [tool for rec in records for tool in rec.tools or () if tool.confirmed or not _never_started(tool)]
    unread = None in cut_short or (record is None and job.status == "interrupted")
    if may_have_written(None if unread else bool(made)):
        return ("indeterminate", text), usage
    return ((job.status, text) if job.status in ("cancelled", "failed") else ("interrupted", _INTERRUPTED)), usage


def _did_model_work(run: RunRecord, used: RunMetrics | None) -> bool:
    if (used is not None and (used.reported or used.details)) or run.tools or run.content:
        return True
    return any(
        is_own_model_message(message) or CUT_OFF_TEXT_KEY in (message.provider_data or {}) for message in run.messages
    )


def _spent(run: RunRecord, used: RunMetrics | None, cost: float | None) -> dict[str, object]:
    return {
        "input_tokens": used.tokens.input_tokens if used is not None else 0,
        "output_tokens": used.tokens.output_tokens if used is not None else 0,
        "tool_calls": len(run.tools or ()),
        "cost_usd": cost,
    }


def _never_started(tool: ToolExecution) -> bool:
    return tool.is_paused or tool.result == RESTART_INTERRUPTION_REASON


def _answer_outcome(node: WorkflowNode, text: str, *, last: bool, made: bool, stopping: bool) -> Outcome | str:
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
    """Start the job for *node* and spawn the engine wait task; a refusal settles the node failed."""
    try:
        node = await launch_child(engine, run, node, run.epoch, timeout=timeout, follow_up=follow_up)
    except AgentNodeError as exc:
        error: dict[str, object] = {"kind": "refused", "message": str(exc)}
        if not await engine.settle_node(node, run.epoch, state=WorkflowNodeState.FAILED, error=error) and not node.seq:
            refused = replace(node, state=WorkflowNodeState.FAILED, error=error, ended_at=utc_timestamp())
            await asyncio.to_thread(engine.store.put_node, refused, run.epoch, dirty_after=True)
        engine.drop_slot(run.run_id, node.key)
        return receipt(cast("WorkflowNode", engine.store.get_node(run.run_id, node.key)))
    except BaseException:
        saved = engine.store.get_node(run.run_id, node.key)
        if saved is not None and saved.generation == node.generation and saved.handle is not None:
            wait_for_agent(engine, run, saved, run.epoch)
        raise
    wait_for_agent(engine, run, node, run.epoch)
    await engine.restate(run.run_id)
    return receipt(node)


def wait_for_agent(engine: WorkflowEngine, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
    """Attach an engine task waiting on *node*'s job, replacing any earlier timed watcher."""
    assert node.handle is not None
    if engine.closed:
        return
    name = f"{TIMED_PREFIX}{node.handle.ref}"
    tasks = engine._runs[run.run_id].tasks if run.run_id in engine._runs else ()
    for stale in [task for task in tasks if task.get_name() == name]:
        stale.cancel()
    engine.places_for(run).working.add(node.key)
    task = engine.spawn(run.run_id, _run_agent_node(engine, run, node, epoch))
    if node.handle.work_until is not None or node.handle.work_left is not None:
        task.set_name(name)


async def _run_agent_node(engine: WorkflowEngine, run: WorkflowRun, node: WorkflowNode, epoch: int) -> None:
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
    """Settle *node* from its job, driving schema correction turns until matched or exhausted."""
    while (further := await _settle_turn(engine, run, node, epoch, block=block)) is not None:
        node = further
        if not block:
            raise ScriptRuntimeUnavailableError(_CORRECTING)
        task, timed = asyncio.current_task(), f"{TIMED_PREFIX}{cast('DispatchHandle', node.handle).ref}"
        if task is not None and task.get_name().startswith(TIMED_PREFIX):
            task.set_name(timed)


async def _settle_turn(
    engine: WorkflowEngine,
    run: WorkflowRun,
    node: WorkflowNode,
    epoch: int,
    *,
    block: bool,
) -> WorkflowNode | None:
    assert node.handle is not None
    if (runtime := engine.agents()) is None:
        raise ScriptRuntimeUnavailableError(NO_AGENTS)
    try:
        waits = partial(node_waits, engine, node, epoch) if block else None
        (kind, text), usage, claim = await await_child(run, node, runtime, on_waiting=waits)
    except ScriptRuntimeUnavailableError:
        raise
    except Exception as exc:
        logger.exception("workflow_agent_outcome_unreadable", run_id=node.run_id, key=node.key)
        (kind, text), usage, claim = ("indeterminate", str(exc)), [], None
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
    """Update *node*'s waiting state and persisted remaining work time, then restate and notify."""
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
    """Return the initial prompt for a live agent node generation, or None once ended/stopped."""
    node = engine.store.get_node(run_id, key) if key else None
    live = node is not None and node.generation == generation and node.state in LIVE_NODE_STATES
    if live and (runtime := engine.agents()) is not None:
        owner = engine.store.get_run(run_id).job_id
        job = runtime.jobs.retained_job(owner) if owner is not None else None
        live = job is None or job.status not in _STOPPED_JOB_STATUSES
    if node is None or not live or not isinstance(node.request.get("prompt"), str):
        return None
    return first_prompt(node)
