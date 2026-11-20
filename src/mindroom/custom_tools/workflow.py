"""The `workflow` toolkit: start a workflow run as a background job, read it, and cancel it."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import Counter
from dataclasses import asdict, replace
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.delegation.lifecycle import running_child_id
from mindroom.script_runs.manager import workflow_placement
from mindroom.tool_jobs.control import job_owns_execution, job_stopped_by_shutdown
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec
from mindroom.tool_jobs.settings import background_tool_jobs_enabled, toolkit_is_background_excluded
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import get_tool_runtime_context
from mindroom.tool_system.tool_hooks import dispatch_external_tool_hooks
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from mindroom.workers.models import ScriptResourceProfileName  # noqa: TC001 - the tool schema reads it at runtime.
from mindroom.workflow_runs.engine import WorkflowLimits
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRun, WorkflowRunState, new_workflow_run_id
from mindroom.workflow_runs.store import WorkflowRunNotFoundError, WorkflowRunStoreError, owned_run

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction
    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode

_START = "start_workflow"
# The spec's teaching text, cut to what this release runs: each later release adds the lines for what it adds. The
# list of models follows it.
_START_DESCRIPTION = """\
Run a workflow: a Python script that runs your tools and shell commands in the background.

Give exactly one of `source` (inline Python) or `path` (a file in your workspace).
The script defines `def main(args):` and returns the final report, a string or a JSON value. MindRoom calls `main`; do not call it yourself.

    from mindroom.workflow import shell, tool, phase, log

- shell(command) runs a command with your shell tool and returns .ok, .exit_code and .output. tool(toolkit, function, **arguments) calls any other tool of yours.
  shell() returns at most the last 50 KiB of output; redirect to a file and read it for more.
- phase(title) groups the calls that follow; log(text) shows a progress line.

Every tool and shell call is saved with its result.
The call returns {run_id, job_id, status, title} at once, and you receive the run's report in full when it ends.
Where the call cannot be a background job, it waits and returns the report itself.
"""
_ONE_SOURCE = "Give exactly one of source or path."
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
        super().__init__(name="workflow", tools=[self.start_workflow, self.get_workflow, self.cancel_workflow])

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
        engine, context = _runtime()
        if running_child_id() is None and not _jobs_enabled(context):
            # A top-level call is a job or nothing; only an agent inside another's turn waits for its run here.
            raise WorkflowRunStoreError(_NEEDS_JOBS)
        run, _ = await self._reserve(engine, context, _inline_origin(context), arguments)
        try:
            await engine.launch(run.run_id, context, self.limits)
            ended = await engine.wait(run.run_id)
        except asyncio.CancelledError:
            # With nobody left to receive the report the run stops too, unless the primary itself is stopping.
            stopping = job_stopped_by_shutdown() if job_owns_execution() else not _running(context)
            if not stopping and not engine.waited(run.run_id):
                await run_coroutine_until_complete(
                    engine.cancel(run.run_id, reason="The call that waited was cancelled."),
                )
            raise
        return _outcome_text(ended)

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
        return json.dumps(status, indent=2)

    async def cancel_workflow(self, run_id: str) -> str:
        """Stop a run and its agents. Finished calls stay saved.

        Args:
            run_id: The run's ID, as start_workflow returned it.

        """
        engine, context = _runtime()
        run = _owned(engine, context, run_id)
        ended = await engine.cancel(run.run_id, reason="Cancelled by the agent.")
        return json.dumps({"run_id": ended.run_id, "status": ended.state.value})

    async def managed_job(
        self,
        function_name: str,
        arguments: dict[str, Any],
        *,
        job_id: str,
        runtime: ToolJobRuntime,
    ) -> JobSpec | str | None:
        """Write the intent of the run a managed `start_workflow` call starts and return the run's job, or why not.

        Nothing runs here: the job's operation, which runs only once the runtime admitted the job, launches the
        first attempt. The other functions run as any tool's call does (None).
        """
        del runtime
        if function_name != _START:
            return None
        try:
            engine, context = _runtime()
            # The plugins' hooks of a call, which the job's own task does not run for it: a decline reserves nothing.
            hooks = partial(
                dispatch_external_tool_hooks,
                execution_identity=context.execution_identity,
                config=context.config,
                runtime_paths=context.runtime_paths,
                tool_name=_START,
                arguments=arguments,
            )
            if (declined := await hooks(before=True, registered_toolkit="workflow")) is not None:
                await hooks(before=False, result=declined, blocked=True)
                return declined
            run, _ = await self._reserve(engine, context, job_id, arguments)
        except ValueError as exc:
            return str(exc)
        store, run_id, limits = engine.store, run.run_id, self.limits

        async def operation() -> BackgroundOutcome:
            await asyncio.to_thread(store.update_run, run_id, job_id=job_id)
            await engine.launch(run_id, context, limits)
            outcome = _workflow_job_outcome(await engine.wait(run_id))
            await hooks(before=False, result=outcome.result)
            return outcome

        async def cancel(_job: BackgroundJob) -> BackgroundOutcome | None:
            if job_stopped_by_shutdown():
                # A stopping primary cancels nothing: the run is the restart's to continue or to end.
                return None
            try:
                return _workflow_job_outcome(await engine.cancel(run_id, reason="The run's job was cancelled."))
            except WorkflowRunNotFoundError:
                return None

        def handle(job: BackgroundJob) -> str:
            return json.dumps({"job_id": job.job_id, "run_id": run_id, "status": job.status, "title": run.title})

        return JobSpec(
            tool_name="workflow",
            kind="workflow",
            toolkit_name="workflow",
            # Never changed afterwards, so a redelivered call names the same job and reattaches to it.
            adapter={"workflow_job": job_id, "workflow": run_id},
            operation=operation,
            cancel=cancel,
            refused=partial(asyncio.to_thread, store.delete_intent, run_id),
            handle=handle,
        )

    async def _reserve(
        self,
        engine: WorkflowEngine,
        context: ToolRuntimeContext,
        origin: str | None,
        arguments: Mapping[str, Any],
    ) -> tuple[WorkflowRun, bool]:
        """Check one start and write its intent, or return the run *origin* reserved before; a refusal raises."""
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
        )
        # On a thread, where it waits for the store's writer.
        return await asyncio.to_thread(engine.store.reserve_run, intent, max_active=self.limits.max_concurrent_runs)


def _workflow_job_outcome(run: WorkflowRun) -> BackgroundOutcome:
    """Return how the job of an ended run ends: with the whole report, or with why the run did not finish."""
    text = _outcome_text(run)
    return BackgroundOutcome(_JOB_STATUS[run.state], text, encode_result_payload(ToolResultPayload(value=text)))


def _outcome_text(run: WorkflowRun) -> str:
    """Return an ended run's report, a string as it is and any other value as indented JSON, or its error."""
    if run.state is not WorkflowRunState.COMPLETED:
        return run.error or f"Workflow run {run.run_id} ended {run.state.value}."
    return run.result if isinstance(run.result, str) else json.dumps(run.result, indent=2)


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


def _owned(engine: WorkflowEngine, context: ToolRuntimeContext, run_id: str) -> WorkflowRun:
    return owned_run(engine.store, run_id, agent_name=context.agent_name, requester_id=context.requester_id)


def _jobs_enabled(context: ToolRuntimeContext) -> bool:
    config, paths = context.config, context.runtime_paths
    return background_tool_jobs_enabled(config, paths) and not toolkit_is_background_excluded("workflow", config, paths)


def _running(context: ToolRuntimeContext) -> bool:
    return context.orchestrator is not None and context.orchestrator.running


def _inline_origin(context: ToolRuntimeContext) -> str | None:
    """Return the key an inline start is found again by: its owner, the model run that made the call, and the call.

    A provider's call ID alone is never a key, so a call without its run is never found again.
    """
    call = current_tool_call()
    if call is None or not call.context.run_id or not call.context.call_id:
        return None
    owner = asdict(context.execution_identity)
    identity = {"owner": owner, "run_id": call.context.run_id, "tool_call_id": call.context.call_id}
    return "inline:" + hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
