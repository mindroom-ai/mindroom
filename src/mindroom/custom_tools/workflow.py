"""The `workflow` toolkit: start a workflow run as a background job, read it, cancel it, and resume it."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import asdict, fields, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import uuid4

from mindroom.background_tasks import run_blocking_or_compensate
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.config.resolution import authored_tool_entries
from mindroom.constants import RuntimePaths  # noqa: TC001  # resolved by tool contract introspection
from mindroom.delegation.lifecycle import running_child_id, running_child_run
from mindroom.matrix.state import resolve_room_aliases
from mindroom.script_runs.manager import workflow_placement
from mindroom.tool_jobs.control import job_stopped_by_shutdown
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, BackgroundOutcome, JobSpec, get_background_runtime
from mindroom.tool_jobs.settings import background_tool_jobs_enabled, toolkit_is_background_excluded
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_hooks import dispatch_external_tool_hooks
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from mindroom.workers.models import ScriptResourceProfileName  # noqa: TC001 - the tool schema reads it at runtime.
from mindroom.workflow_runs.engine import WorkflowLimits
from mindroom.workflow_runs.models import (
    TERMINAL_RUN_STATES,
    WorkflowNodeState,
    WorkflowRun,
    WorkflowRunState,
    new_workflow_run_id,
)
from mindroom.workflow_runs.store import WorkflowRunNotFoundError, WorkflowRunStoreError, owned_run

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode

_START = "start_workflow"
_RESUME = "resume_workflow"
# The spec's teaching text, cut to what this release runs: each later release adds the lines for what it adds. The
# list of models follows it.
_START_DESCRIPTION = """\
Run a workflow: a Python script that puts many agents to work in the background.

Use it when a task needs many agents, an independent check, or a loop with a hard stop.
Give exactly one of `source` (inline Python) or `path` (a file in your workspace).
The script defines `def main(args):` and returns the final report, a string or a JSON value. MindRoom calls `main`; do not call it yourself.

    from mindroom.workflow import agent, shell, tool, phase, log, now, random

    def main(args):
        phase("Write")
        draft = agent("Write a report on: " + args["topic"])
        for _ in range(3):                       # a bounded loop with an independent check
            problems = agent("List factual problems in this report, or reply NONE.\\n\\n" + draft)
            if problems.strip() == "NONE":
                break
            draft = agent(f"Fix these problems: {problems}\\n\\n{draft}")
        return draft

- agent(prompt, *, name=None, agent=None, model=None, system=None, tools=None, skills=None, knowledge=None,
        profile=None, minimal=False, timeout=None, output_path=None, dedicated_worker=False, resource_profile=None)
  runs one agent turn and returns its answer as text.
  The agent is a fresh copy of you with all your tools and no memory of this conversation: put everything it needs in the prompt.
  agent="other" runs that configured agent with its own tools. name is only a label; it does not pick an agent.
  model: one of the models below. system: replaces the system prompt. tools, skills, knowledge: lists that narrow a copy of you.
  profile: a subagents/<profile>.md file. minimal=True: a cheap copy with one shell tool. timeout: seconds, then the turn is cancelled.
  output_path: also write the answer to this workspace file. dedicated_worker=True: its own container. resource_profile: a worker size.
  A failed turn raises AgentError.
- shell(command) runs a command with your shell tool and returns .ok, .exit_code and .output. tool(toolkit, function, **arguments) calls any other tool of yours.
  shell() returns at most the last 50 KiB of output; redirect to a file and read it for more.
- phase(title) groups the calls that follow; log(text) shows a progress line.

Every agent, tool and shell call is saved with its result. The Python between the calls runs again on every resume,
so do writes (files, git, HTTP) through shell(), tool() or an agent, never with plain Python.
resume_workflow reuses saved calls: in each sequence of calls, those before the first changed call are reused; that call and the
calls after it run again.
Use now() and random() from the SDK instead of time and random when the value goes into a prompt.
shell(..., cached=False) and tool(..., cached=False) run again on every resume: use it for a check of something outside the run.
The call returns {run_id, job_id, status, title} at once, and you receive the run's report in full when it ends.
Where the call cannot be a background job, it waits and returns the report itself.
An agent that needs an approval waits for the user; it does not fail.
"""
_ONE_SOURCE = "Give exactly one of source or path."
_AT_MOST_ONE_SOURCE = "Give at most one of source or path."
_NEEDS_JOBS = "Workflows need background tool jobs. Set background_tool_jobs.enabled: true and do not exclude workflow."

_ACTIVE = frozenset({WorkflowNodeState.QUEUED, WorkflowNodeState.RUNNING, WorkflowNodeState.WAITING})
_FAILED = frozenset({WorkflowNodeState.FAILED, WorkflowNodeState.INDETERMINATE})
_JOB_STATUS: dict[WorkflowRunState, Literal["completed", "failed", "cancelled", "interrupted"]] = {
    WorkflowRunState.COMPLETED: "completed",
    WorkflowRunState.FAILED: "failed",
    WorkflowRunState.CANCELLED: "cancelled",
    WorkflowRunState.INTERRUPTED: "interrupted",
}


class WorkflowTools(Toolkit):
    """Start, read and cancel the workflow runs of one agent and requester."""

    def __init__(
        self,
        runtime_config: Config | None = None,
        agent_name: str | None = None,
        current_room_id: str | None = None,
        runtime_paths: RuntimePaths | None = None,
        max_concurrent_runs: int = 8,
        max_runtime_hours: float = 72,
        max_tool_calls_per_minute: int = 600,
        max_concurrent_agents: int | None = None,
        large_run_notice_usd: float = 10,
    ) -> None:
        self.limits = WorkflowLimits(
            max_concurrent_runs=max_concurrent_runs,
            max_runtime_hours=max_runtime_hours,
            max_tool_calls_per_minute=max_tool_calls_per_minute,
            max_concurrent_agents=max_concurrent_agents,
        )
        self.large_run_notice_usd = large_run_notice_usd
        # The models of this deployment, named as `run_subagent` names them, end the description.
        models = sorted((runtime_config.models if runtime_config is not None else {}).items())
        levels = [f"{name}@{model.reasoning.levels[-1]}" for name, model in models if model.reasoning]
        effort = f" (add an effort as in {levels[0]})" if levels else ""
        self._start_description = f"{_START_DESCRIPTION}\nModels: {', '.join(name for name, _ in models)}{effort}."
        # And the agents a run may name: those this agent may delegate to, and those configured for this room.
        agents = runtime_config.agents if runtime_config is not None else {}
        others = list(agents[agent_name].delegate_to) if agent_name in agents else []
        if runtime_paths is not None:
            rooms = {name: resolve_room_aliases(other.rooms, runtime_paths) for name, other in agents.items()}
            others += [name for name in rooms if name not in (agent_name, *others) and current_room_id in rooms[name]]
        if others:
            self._start_description += f"\nAgents you can run with agent=: {', '.join(others)}."
        tools = [self.start_workflow, self.get_workflow, self.cancel_workflow, self.resume_workflow]
        super().__init__(name="workflow", tools=tools)

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Describe `start_workflow` with the teaching text and this deployment's models."""
        return replace(function, description=self._start_description) if function.name == _START else function

    async def start_workflow(
        self,
        source: str | None = None,
        path: str | None = None,
        args: dict[str, Any] | None = None,
        title: str | None = None,
        max_concurrent: int = 16,
        resource_profile: ScriptResourceProfileName | None = None,
    ) -> str:
        """Run a workflow where the call cannot be a background job: wait for the run and return its report.

        A call that is refused fails with the sentence that says why, as every function of this toolkit does.

        Args:
            source: Inline Python source.
            path: Workspace-relative path of a Python file.
            args: JSON object passed to main(args).
            title: Three to six words shown on the run's card.
            max_concurrent: How many agents run at once. Raise it for large fan-outs.
            resource_profile: Worker size for the script itself.

        """
        arguments = {
            "source": source,
            "path": path,
            "args": args,
            "title": title,
            "max_concurrent": max_concurrent,
            "resource_profile": resource_profile,
        }
        engine, context = _waiting_runtime()
        origin = _inline_origin(context)
        reserve = partial(self._reserve, engine, context, origin, arguments)
        return (await engine.attend(origin, reserve, context, self.limits)).outcome_text()

    async def get_workflow(self, run_id: str, nodes: Literal["active", "failed", "all"] = "active") -> str:
        """Return a run's status, cost, open questions, failed calls and nodes. nodes is "active" (default), "failed" or "all".

        Args:
            run_id: The run's ID, as start_workflow returned it.
            nodes: Which calls to list: "active", "failed" or "all".

        """
        engine, context = _runtime()
        run = _owned(engine, context, run_id)
        listed = await asyncio.to_thread(engine.store.nodes, run.run_id)
        shown = {"active": _ACTIVE, "failed": _FAILED, "all": frozenset(WorkflowNodeState)}[nodes]
        status = {
            "run_id": run.run_id,
            "title": run.title,
            "status": run.state.value,
            "attempt": run.attempt,
            "counts": dict(Counter(node.state.value for node in listed)),
            "usage": await asyncio.to_thread(engine.store.usage_totals, run.run_id),
            "waiting": [
                {"id": f"n{node.seq}", "label": node.label, "waiting_on": node.waiting_on}
                for node in listed
                if node.state is WorkflowNodeState.WAITING
            ],
            "failed": [
                {"id": f"n{node.seq}", "label": node.label, "error": node.error}
                for node in listed
                if node.state in _FAILED
            ],
            "nodes": [_public_node(node) for node in listed if node.state in shown],
        }
        if run.state in TERMINAL_RUN_STATES:
            # The report, or why there is none, also for a run whose caller is gone.
            status["result"] = run.outcome_text()
        return json.dumps(status, indent=2)

    async def cancel_workflow(self, run_id: str) -> str:
        """Stop a run and its agents. Finished calls stay saved, so resume_workflow can continue it.

        Args:
            run_id: The run's ID, as start_workflow returned it.

        """
        engine, context = _runtime()
        run = _owned(engine, context, run_id)
        ended = await engine.cancel(run.run_id, reason="Cancelled by the agent.")
        return json.dumps({"run_id": ended.run_id, "status": ended.state.value})

    async def resume_workflow(
        self,
        run_id: str,
        source: str | None = None,
        path: str | None = None,
        rerun: list[str] | None = None,
    ) -> str:
        """Continue a stopped or failed run, or run a finished one again; saved calls are reused as start_workflow explains. Pass source or path to run an edited script, and rerun=[node ids] to make calls run again, such as one that ended indeterminate.

        Args:
            run_id: The run's ID, as start_workflow returned it.
            source: Inline Python source that replaces the run's script.
            path: Workspace-relative path of a Python file that replaces the run's script.
            rerun: IDs of saved calls to run again, such as ["n4"].

        """
        engine, context = _waiting_runtime()
        arguments = {"run_id": run_id, "source": source, "path": path, "rerun": rerun}
        origin = _inline_origin(context)
        run, request = await self._resumption(engine, context, arguments, owner=origin)
        if run.owner != origin and (report := run.claims.get(origin)) is not None:
            # This call took the run before and another has it now: it gets the report of its own attempt.
            return report
        # The call owns the run it takes, as an inline start owns the run it reserves: its leaving stops that run.
        reopen = partial(engine.reopen, run.run_id, context, limits=self.limits, owner=origin, **request)
        return (await engine.attend(origin, reopen, context, self.limits)).outcome_text()

    async def managed_job(
        self,
        function_name: str,
        arguments: dict[str, Any],
        *,
        job_id: str,
        runtime: ToolJobRuntime,
    ) -> JobSpec | str | None:
        """Return the job of the run a managed `start_workflow` or `resume_workflow` call starts, or why not.

        Nothing runs here: a start writes its intent, a resume is only checked, and the job's operation, which runs
        only once the runtime admitted the job, takes a resumed run and launches every attempt. The other functions
        run as any tool's call does (None).
        """
        if function_name not in (_START, _RESUME):
            return None
        try:
            engine, context = _runtime()
            # The plugins' hooks of a call, which the job's own task does not run for it: a decline reserves nothing.
            hooks = partial(
                dispatch_external_tool_hooks,
                execution_identity=context.execution_identity,
                config=context.config,
                runtime_paths=context.runtime_paths,
                tool_name=function_name,
                arguments=arguments,
            )
            if (declined := await hooks(before=True, registered_toolkit="workflow")) is not None:
                await hooks(before=False, result=declined, blocked=True)
                return declined
            refused = request = None
            if function_name == _START:
                run, _ = await self._reserve(engine, context, job_id, arguments, job_id=job_id)
                refused = partial(asyncio.to_thread, engine.store.delete_intent, run.run_id, job_id, run.epoch)
            else:
                # A call delivered again names the job that already holds what was accepted, and is checked no more.
                known = next((job.adapter for job in runtime.retained_jobs() if job.job_id == job_id), None)
                accepted = known and known.get("resume")
                run, request = await self._resumption(engine, context, arguments, accepted, owner=job_id)
        except ValueError as exc:
            return str(exc)
        after = partial(hooks, before=False)
        return _run_job(engine, run, job_id, context, self.limits, after=after, refused=refused, resume=request)

    async def _resumption(
        self,
        engine: WorkflowEngine,
        context: ToolRuntimeContext,
        arguments: Mapping[str, Any],
        accepted: dict[str, Any] | None = None,
        *,
        owner: str,
    ) -> tuple[WorkflowRun, dict[str, Any]]:
        """Check one resume and return its run and the request its job is admitted with; a refusal raises.

        The request names the epoch the run has now, so it takes the run only if nothing else did since.
        *accepted* is the request this same call was admitted with before, which is returned unchecked, and so is
        the call that finds the run its own already, or among those that took it: *owner*, the job or the waiting
        call, took it before, and the write that takes a run refuses it a second time.
        """
        source, path, rerun = arguments.get("source"), arguments.get("path"), arguments.get("rerun") or ()
        if source is not None and path is not None:
            raise WorkflowRunStoreError(_AT_MOST_ONE_SOURCE)
        run = _owned(engine, context, str(arguments.get("run_id")))
        if accepted is not None or owner in (run.owner, *run.claims):
            return run, accepted or {}
        engine.resumable(run.run_id, context, rerun, self.limits)
        delivered = ("absent", "stopped", "ended")
        if run.job_id is not None and workflow_job_standing(context.runtime_paths, run.job_id) not in delivered:
            msg = f"Run {run.run_id} is still delivering its report. Try again in a moment."
            raise WorkflowRunStoreError(msg)
        edited = source is not None or path is not None
        text = (await engine.scripts.resolve_source(context, source=source, path=path)).decode() if edited else None
        return run, {"source": text, "rerun": list(rerun), "epoch": run.epoch}

    async def _reserve(
        self,
        engine: WorkflowEngine,
        context: ToolRuntimeContext,
        origin: str,
        arguments: Mapping[str, Any],
        job_id: str | None = None,
    ) -> tuple[WorkflowRun, bool]:
        """Check one start and write its intent, or return the run *origin* reserved before; a refusal raises.

        *job_id* names the job the run is reserved for, which no call that waits for its run itself has.
        """
        source, path, title = arguments.get("source"), arguments.get("path"), arguments.get("title")
        max_concurrent = arguments.get("max_concurrent", 16)
        if (source is None) == (path is None):
            raise WorkflowRunStoreError(_ONE_SOURCE)
        if not isinstance(max_concurrent, int) or max_concurrent < 1:
            msg = "max_concurrent must be a whole number of at least 1."
            raise WorkflowRunStoreError(msg)
        placement = workflow_placement(context.config, context.runtime_paths)
        if placement not in ("worker", "local"):
            raise WorkflowRunStoreError(placement)
        text = (await engine.scripts.resolve_source(context, source=source, path=path)).decode()
        hours = self.limits.max_runtime_hours
        intent = WorkflowRun(
            run_id=new_workflow_run_id(),
            agent_name=context.agent_name,
            owner_user_id=context.requester_id,
            room_id=context.room_id,
            thread_root_event_id=context.resolved_thread_id,
            execution_identity=serialize_tool_execution_identity(context.execution_identity),
            # The tools a narrowed agent was left with are the run's, whatever its agent has.
            persona=asdict(context.persona) if context.persona is not None else None,
            # Kept whole: a frame that shows it shortens it.
            title=str(title) if title else None,
            source=text,
            source_digest=hashlib.sha256(text.encode()).hexdigest(),
            args=arguments.get("args"),
            max_concurrent=max_concurrent,
            max_cost_usd=None,
            resource_profile=arguments.get("resource_profile"),
            runtime_seconds_left=hours * 3600 if hours else None,
            origin_job_id=origin,
            owner=origin,
            job_id=job_id,
            # The agents the run runs are subagents of the one that starts it, one level below it.
            depth=child.child.depth if (child := running_child_run()) is not None else 0,
        )
        store = engine.store

        def take_back(reserved: tuple[WorkflowRun, bool]) -> None:
            if reserved[1]:
                store.delete_intent(reserved[0].run_id, origin, 0)

        # On a thread, where it waits for the store's writer. A call cancelled meanwhile takes back the intent it
        # wrote; the engine keeps an inline caller's cancellation away from here and ends the run itself.
        reserve = partial(store.reserve_run, intent, max_active=self.limits.max_concurrent_runs)
        return await run_blocking_or_compensate(reserve, take_back)


def _run_job(
    engine: WorkflowEngine,
    run: WorkflowRun,
    job_id: str,
    context: ToolRuntimeContext,
    limits: WorkflowLimits,
    *,
    after: Callable[..., Awaitable[object]] | None = None,
    refused: Callable[[], Awaitable[object]] | None = None,
    resume: dict[str, Any] | None = None,
) -> JobSpec:
    """Return the job of *run*: it starts each attempt the run waits for, and ends as the run ends.

    While the job is unfinished only its operation starts an attempt of the run, in *context* and under *limits* as
    `engine.admit` allows, so a Stop of the job cancels the one thing that could start one. *after* gets the report
    for the plugins' hooks, and *refused* undoes a start the runtime did not admit. *resume* is the accepted request
    of a resume, saved with the job so that a restart goes on with exactly it: the operation takes the run with it
    first, unless the run is this job's already.
    """
    run_id = run.run_id

    async def operation() -> BackgroundOutcome:
        launch = partial(engine.launch, run_id, context, limits, owner=job_id)
        try:
            if resume is not None:
                await engine.reopen(run_id, context, limits=limits, owner=job_id, job_id=job_id, **resume)
            outcome = _workflow_job_outcome(await engine.wait(run_id, launch, job_id))
        except WorkflowRunStoreError as exc:
            # Another call took the run: this job owns nothing of it, waits for none of its reports, and says so.
            return BackgroundOutcome("failed", str(exc), encode_result_payload(ToolResultPayload(value=str(exc))))
        if after is not None:
            await after(result=outcome.result)
        return outcome

    def handle(job: BackgroundJob) -> str:
        return json.dumps({"job_id": job.job_id, "run_id": run_id, "status": job.status, "title": run.title})

    return JobSpec(
        tool_name="workflow",
        kind="workflow",
        toolkit_name="workflow",
        # Never changed afterwards, so a redelivered call names the same job and reattaches to it.
        adapter={"workflow_job": job_id, "workflow": run_id} | ({"resume": resume} if resume is not None else {}),
        operation=operation,
        cancel=partial(stopped_workflow_job, engine),
        refused=refused,
        handle=handle,
    )


async def stopped_workflow_job(
    engine: WorkflowEngine,
    job: BackgroundJob,
    *,
    live: bool = True,
) -> BackgroundOutcome | None:
    """Stop the run of a stopped job and return how the job ends: as its run ended, while that run is still its own.

    A Stop that a restart found saved (not *live*) stops no run here, since the pass ends the run of a stopped job:
    it only gives the job the report of a run that had already ended.
    """
    if job_stopped_by_shutdown():
        # A stopping primary cancels nothing: the run is the restart's to continue or to end.
        return None
    run_id = str(job.adapter.get("workflow"))
    try:
        run = engine.store.get_run(run_id)
        if live:
            run = await engine.cancel(run_id, owner=job.job_id, **engine.job_stop(run, "The run's job was cancelled."))
    except WorkflowRunNotFoundError:
        return None
    # A run of another owner is left: a resume that never took it, or a job whose run was resumed since.
    return _workflow_job_outcome(run) if run.owner == job.job_id and run.state in TERMINAL_RUN_STATES else None


async def continued_workflow_job(job: BackgroundJob) -> JobSpec:
    """Return what the job of a workflow run goes on as after a restart stopped it.

    Called in the tool context of the job's conversation, once the job runtime granted the job again. The job waits
    for its run again and can cancel it, so every attempt the run waits for starts there: the first, the next once
    the engine's pass recorded the old one's end, and that of a resume that was admitted and stopped before it took
    the run. A run that has ended ends the job with its report, and one that is gone raises, which ends the job.
    """
    engine, context = _runtime()
    run = engine.store.get_run(str(job.adapter.get("workflow")))
    limits = workflow_limits(context.config, run.agent_name)
    return _run_job(engine, run, job.job_id, context, limits, resume=job.adapter.get("resume"))


def workflow_job_standing(runtime_paths: RuntimePaths, job_id: str) -> str:
    """Return what the job runtime knows of a run's job now: read when its run is judged, never ahead of that.

    `unknown` while there is no job runtime to ask, `absent` when it holds no such job, `waiting` for a job a restart
    stopped that is not granted again and cannot cancel its run yet, `stopping` once a cancel was requested, `live`
    otherwise while unfinished. An ended job was `stopped` by a Stop or a denial, or `ended` in any other way; one
    whose end is not saved yet is unfinished, since a restart would find it so and deliver its report again.
    """
    runtime = get_background_runtime(runtime_paths)
    if runtime is None:
        return "unknown"
    job = next((job for job in runtime.retained_jobs() if job.job_id == job_id), None)
    if job is None:
        return "absent"
    if job.status in TERMINAL_STATUSES and runtime.outcome_saved(job_id):
        return "stopped" if job.status in ("cancelled", "denied") else "ended"
    if job.status == "cancel_requested":
        return "stopping"
    return "waiting" if any(waiting.job_id == job_id for waiting in runtime.awaiting_continuation()) else "live"


def workflow_job_admission(runtime_paths: RuntimePaths, job_id: str) -> asyncio.Lock:
    """Return the lock a delivery of the call with *job_id* holds while its job is admitted."""
    return cast("ToolJobRuntime", get_background_runtime(runtime_paths)).admission(job_id)


def undelivered_workflow_jobs(runtime_paths: RuntimePaths) -> frozenset[str] | None:
    """Return the jobs whose end the journal does not hold yet, so their runs' reports are still theirs to deliver.

    None while there is no job runtime to ask.
    """
    runtime = get_background_runtime(runtime_paths)
    if runtime is None:
        return None
    held = runtime.retained_jobs()
    return frozenset(
        job.job_id for job in held if job.status not in TERMINAL_STATUSES or not runtime.outcome_saved(job.job_id)
    )


def workflow_limits(config: Config, agent_name: str) -> WorkflowLimits:
    """Return the limits the agent's `workflow` entry sets now, for a run that goes on with no call of the toolkit."""
    entries = authored_tool_entries(config, agent_name)
    settings = next((entry.tool_config_overrides for entry in entries if entry.name == "workflow"), {})
    names = {limit.name for limit in fields(WorkflowLimits)}
    limits: dict[str, Any] = {name: value for name, value in settings.items() if name in names}
    return WorkflowLimits(**limits)


def _workflow_job_outcome(run: WorkflowRun) -> BackgroundOutcome:
    """Return how the job of an ended run ends: with the whole report, or with why the run did not finish."""
    text = run.outcome_text()
    return BackgroundOutcome(_JOB_STATUS[run.state], text, encode_result_payload(ToolResultPayload(value=text)))


def _public_node(node: WorkflowNode) -> dict[str, object]:
    return {
        "id": f"n{node.seq}",
        "kind": node.kind,
        "label": node.label,
        "status": node.state.value,
        "error": node.error,
    }


def _runtime() -> tuple[WorkflowEngine, ToolRuntimeContext]:
    context = get_tool_runtime_context()
    if context is None or context.orchestrator is None:
        msg = "Workflows need an active room context."
        raise WorkflowRunStoreError(msg)
    return context.orchestrator.workflow_engine, context


def _waiting_runtime() -> tuple[WorkflowEngine, ToolRuntimeContext]:
    """Return the runtime of a call that waits for its run itself, which only a call inside another agent's turn may."""
    engine, context = _runtime()
    if running_child_id() is None and not _jobs_enabled(context):
        # A top-level call is a job or nothing.
        raise WorkflowRunStoreError(_NEEDS_JOBS)
    return engine, context


def _owned(engine: WorkflowEngine, context: ToolRuntimeContext, run_id: str) -> WorkflowRun:
    return owned_run(engine.store, run_id, agent_name=context.agent_name, requester_id=context.requester_id)


def _jobs_enabled(context: ToolRuntimeContext) -> bool:
    config, paths = context.config, context.runtime_paths
    return background_tool_jobs_enabled(config, paths) and not toolkit_is_background_excluded("workflow", config, paths)


def _inline_origin(context: ToolRuntimeContext) -> str:
    """Return the key a call that waits for its run owns that run by: its owner, its model run, and the call.

    A delivery of the same call finds its run again by it. A provider's call ID alone is never a key, so a call
    without its run gets a key of its own and is never found again.
    """
    call = current_tool_call()
    if call is None or not call.context.run_id or not call.context.call_id:
        return f"inline:{uuid4().hex}"
    owner = asdict(context.execution_identity)
    identity = {"owner": owner, "run_id": call.context.run_id, "tool_call_id": call.context.call_id}
    return "inline:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
