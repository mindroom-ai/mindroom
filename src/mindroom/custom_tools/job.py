"""One conversation-scoped management function for all managed tool jobs."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from agno.tools import Toolkit

from mindroom.tool_jobs.consumption import consume_tool_job
from mindroom.tool_jobs.runtime import JobAccessError, get_background_runtime
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from agno.tools.function import Function, FunctionCall

    from mindroom.constants import RuntimePaths
    from mindroom.tool_jobs.runtime import BackgroundJob
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def is_job_function(function: Function) -> bool:
    """Recognize the reserved implementation, never an unrelated same-named plugin."""
    entrypoint = inspect.unwrap(function.entrypoint) if function.entrypoint is not None else None
    return (
        inspect.ismethod(entrypoint)
        and isinstance(entrypoint.__self__, JobTools)
        and entrypoint.__func__ is JobTools.job
    )


_SUMMARY_MAX_CHARS = 500


def _summary(job: BackgroundJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "tool": job.tool_name,
        "status": job.status,
        "summary": job.result[:_SUMMARY_MAX_CHARS] if job.result is not None else None,
        "summary_truncated": job.result is not None and len(job.result) > _SUMMARY_MAX_CHARS,
    }


class JobTools(Toolkit):
    """Discover and manage jobs belonging to the exact caller and conversation."""

    def __init__(self, runtime_paths: RuntimePaths, owner: ToolExecutionIdentity, *, depth: int = 0) -> None:
        self._runtime_paths = runtime_paths
        self._owner = owner
        self._depth = depth
        super().__init__(
            name="job",
            tools=[self.job],
            instructions=(
                'Tools return a job_id when their wait ends before execution completes. Use job(action="list") to rediscover jobs after a new turn. '
                'Use job(action="wait", job_id=...) to retrieve the actual result. '
                "Human follow-ups release waits while work continues. "
                "Only the agent that started a job can access it; teams must ask that member to manage it."
            ),
        )
        self.async_functions["job"].owning_toolkit = "job"

    @staticmethod
    def install(
        tools: list[Toolkit],
        runtime_paths: RuntimePaths,
        owner: ToolExecutionIdentity | None,
        *,
        depth: int,
        enabled: bool,
    ) -> None:
        """Install the reserved function once after rejecting authored collisions."""
        if owner is None or owner.channel != "matrix" or get_background_runtime(runtime_paths) is None:
            return
        if any("job" in toolkit.get_async_functions() for toolkit in tools):
            msg = "Tool function name job is reserved for managed job controls"
            raise ValueError(msg)
        if enabled:
            tools.append(JobTools(runtime_paths, owner, depth=depth))

    def caller_identity(self) -> ToolExecutionIdentity:
        """Keep the original execution owner with the current conversation session."""
        context = get_tool_runtime_context()
        if context is None:
            return self._owner
        transport = context.transport_agent_name or context.agent_name
        if transport == (self._owner.transport_agent_name or self._owner.agent_name):
            transport = self._owner.transport_agent_name
        return replace(
            self._owner,
            agent_name=(
                self._owner.agent_name
                if context.agent_name in {self._owner.agent_name, self._owner.transport_agent_name}
                else context.agent_name
            ),
            requester_id=context.requester_id,
            room_id=context.room_id,
            thread_id=context.thread_id,
            resolved_thread_id=context.resolved_thread_id,
            session_id=context.session_id,
            transport_agent_name=transport,
        )

    async def job(  # noqa: PLR0911 - Each public action returns its own result.
        self,
        action: Literal["list", "inspect", "wait", "cancel"],
        job_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        wait_timeout: float | None = None,
    ) -> Any:  # noqa: ANN401 - Preserve the original SDK tool result type.
        """Discover, inspect, wait for or cancel this caller's managed work.

        Args:
            action: Operation; wait retrieves the stored result.
            job_id: Exact job ID, required except for list.
            limit: Maximum number of jobs to list, active jobs first.
            offset: Number of accessible jobs to skip.
            wait_timeout: Seconds to wait; null waits until completion or human input, zero returns immediately.

        Returns:
            Scoped job summaries, the original result, or an unavailable-job error.

        """
        runtime = get_background_runtime(self._runtime_paths)
        if runtime is None:
            return "Job controls require a managed conversation."
        owner = self.caller_identity()
        try:
            if action == "list":
                return json.dumps(
                    [
                        _summary(job)
                        for job in await runtime.list_jobs(owner=owner, depth=self._depth, limit=limit, offset=offset)
                    ],
                )
            if job_id is None:
                return "job_id is required for this action."
            if action == "wait":
                waited = await runtime.wait(job_id, owner=owner, depth=self._depth, timeout=wait_timeout)
                if waited.token is not None:
                    return await consume_tool_job(runtime, waited.job, waited.token)
                return json.dumps(_summary(waited.job))
            if action == "cancel":
                job = await runtime.cancel(job_id, owner=owner, depth=self._depth, await_completion=True)
                waited = await runtime.wait(job_id, owner=owner, depth=self._depth, timeout=0)
                if waited.token is not None:
                    await consume_tool_job(runtime, waited.job, waited.token)
            elif action == "inspect":
                job = await runtime.lookup(job_id, owner=owner, depth=self._depth)
            else:
                return "Unknown job action."
            return json.dumps(_summary(job))
        except JobAccessError as error:
            return str(error)


async def project_native_job_wait(call: FunctionCall, *, depth: int) -> None:
    """Project only the reserved native wait into the persisted approval driver."""
    if not is_job_function(call.function) or (call.arguments or {}).get("action") != "wait":
        return
    context = get_tool_runtime_context()
    runtime = get_background_runtime(context.runtime_paths) if context is not None else None
    job_id = (call.arguments or {}).get("job_id")
    if runtime is None or not isinstance(job_id, str):
        return
    assert call.function.entrypoint is not None
    entrypoint = inspect.unwrap(call.function.entrypoint)
    toolkit = entrypoint.__self__
    try:
        job = await runtime.lookup(job_id, owner=toolkit.caller_identity(), depth=depth)
    except JobAccessError:
        return
    if job.kind != "delegation":
        return
    call.function = call.function.model_copy()
    call.function.external_execution = True
    call.function.external_execution_silent = True
    call.function.requires_confirmation = False
    call.function.approval_type = "mindroom_job_wait"
