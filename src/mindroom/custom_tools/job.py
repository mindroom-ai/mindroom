"""One conversation-scoped management function for all managed tool jobs."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from agno.tools import Toolkit
from agno.tools.function import ToolResult

from mindroom.tool_jobs.consumption import consume_tool_job, restore_control, retain_claim
from mindroom.tool_jobs.runtime import JobAccessError, format_job_handle, get_background_runtime, job_summary
from mindroom.tool_system.declarations import tool_schema_source
from mindroom.tool_system.output_files import wrap_toolkit_for_output_files
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from agno.tools.function import Function, FunctionCall

    from mindroom.constants import RuntimePaths
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, ToolJobRuntime
    from mindroom.tool_system.output_files import ToolOutputFilePolicy
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _job_toolkit(function: Function) -> JobTools | None:
    """Resolve the reserved owner through SDK and MindRoom output wrappers."""
    entrypoint = tool_schema_source(inspect.unwrap(function.entrypoint)) if function.entrypoint is not None else None
    if (
        inspect.ismethod(entrypoint)
        and isinstance(entrypoint.__self__, JobTools)
        and entrypoint.__func__ is JobTools.job
    ):
        return entrypoint.__self__
    return None


def is_job_function(function: Function) -> bool:
    """Recognize the reserved implementation, never an unrelated same-named plugin."""
    return _job_toolkit(function) is not None


async def _claimed_result(runtime: ToolJobRuntime, job: BackgroundJob, claim: JobClaim) -> Any:  # noqa: ANN401 - SDK tool value.
    """Return a claimed outcome as this call's own result, raising its control or failure for the SDK to record."""
    value, payload = await consume_tool_job(runtime, job, claim)
    if payload.control is not None:
        raise restore_control(payload.control)
    if job.status == "failed":
        text = value.content if isinstance(value, ToolResult) else value
        raise RuntimeError(str(payload.error or text or "Background tool job failed."))
    return value


class JobTools(Toolkit):
    """Discover and manage jobs belonging to the exact caller and conversation."""

    def __init__(self, runtime_paths: RuntimePaths, owner: ToolExecutionIdentity) -> None:
        self._runtime_paths = runtime_paths
        self._owner = owner
        super().__init__(
            name="job",
            tools=[self.job],
            instructions=(
                "Managed tool calls accept wait_timeout: omitted or null waits until completion or human input, "
                "zero returns a job_id immediately, and a positive number bounds waiting without cancelling work. "
                "Only use wait_timeout when the tool schema exposes it; excluded toolkits keep their native controls, "
                "so a shell handle is checked or killed with check_shell_command or kill_shell_command. "
                "Calls made inside an already running job stay with that job and accept no separate wait budget. "
                "Human follow-ups release waits while work continues. "
                'Use job(action="list") to rediscover jobs and their status after a new turn, '
                'job(action="wait", job_id=...) to retrieve the actual result, and job(action="cancel", job_id=...) to stop one. '
                "Only the agent that started a job can access it; teams must ask that member to manage it."
            ),
        )
        self.async_functions["job"].owning_toolkit = "job"

    @staticmethod
    def available(
        runtime_paths: RuntimePaths,
        owner: ToolExecutionIdentity | None,
        *,
        depth: int,
        enabled: bool,
    ) -> bool:
        """Require the same live conversation capability at construction and approval recovery."""
        return (
            enabled
            and depth == 0
            and owner is not None
            and owner.channel == "matrix"
            and get_background_runtime(runtime_paths) is not None
        )

    @staticmethod
    def build(
        tools: list[Toolkit],
        runtime_paths: RuntimePaths,
        owner: ToolExecutionIdentity | None,
        *,
        depth: int,
        enabled: bool,
        output_file_policy: ToolOutputFilePolicy | None = None,
    ) -> Toolkit | None:
        """Build the reserved toolkit for the caller's ordinary policy/hook pipeline."""
        if owner is None or not JobTools.available(runtime_paths, owner, depth=depth, enabled=enabled):
            return None
        if any("job" in toolkit.get_async_functions() for toolkit in tools):
            msg = "Tool function name job is reserved for managed job controls"
            raise ValueError(msg)
        return wrap_toolkit_for_output_files(JobTools(runtime_paths, owner), output_file_policy)

    def caller_identity(self) -> ToolExecutionIdentity:
        """Keep the original execution owner with the current conversation session."""
        context = get_tool_runtime_context()
        if context is None:
            return self._owner
        transport = context.recipient
        if transport == self._owner.recipient:
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
        action: Literal["list", "wait", "cancel"],
        job_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        wait_timeout: float | None = None,
    ) -> Any:  # noqa: ANN401 - Preserve the original SDK tool result type.
        """Discover, wait for, or cancel this caller's managed work.

        Args:
            action: Operation; list reports status and saved summaries, wait retrieves the stored result.
            job_id: Exact job ID, required except for list.
            limit: Maximum number of jobs to list, from 1 to 100; active jobs first.
            offset: Number of accessible jobs to skip, at least zero.
            wait_timeout: Seconds to wait; null waits until completion or human input, zero returns immediately.

        Returns:
            Scoped job summaries, the original result, or an error message.

        """
        runtime = get_background_runtime(self._runtime_paths)
        if runtime is None:
            return "Job controls require a managed conversation."
        owner = self.caller_identity()
        # Job controls exist only for the top-level caller, so every job they reach has depth 0.
        try:
            if action == "list":
                return json.dumps(
                    [
                        job_summary(job)
                        for job in await runtime.list_jobs(owner=owner, depth=0, limit=limit, offset=offset)
                    ],
                )
            if job_id is None:
                return "job_id is required for this action."
            if action == "wait":
                waited = await runtime.wait(job_id, owner=owner, depth=0, timeout=wait_timeout)
                if waited.claim is not None and waited.job.status == "awaiting_approval":
                    # Only the native delegation projection can present a child's pending approval.
                    await runtime.release_wait(job_id, waited.claim)
                elif waited.claim is not None:
                    return await _claimed_result(runtime, waited.job, waited.claim)
                return format_job_handle(waited.job)
            if action != "cancel":
                return "Unknown job action."
            job = await runtime.cancel(job_id, owner=owner, depth=0)
            waited = await runtime.wait(job_id, owner=owner, depth=0, timeout=0)
            if waited.claim is not None:
                await retain_claim(runtime, job_id, waited.claim)
            return format_job_handle(job)
        except ValueError as error:
            # Unavailable jobs and invalid limits, offsets, or wait budgets are the model's to correct.
            return str(error)


async def project_native_job_wait(call: FunctionCall, *, depth: int) -> None:
    """Project only the reserved native wait into the persisted approval driver."""
    toolkit = _job_toolkit(call.function)
    if toolkit is None or (call.arguments or {}).get("action") != "wait":
        return
    context = get_tool_runtime_context()
    runtime = get_background_runtime(context.runtime_paths) if context is not None else None
    job_id = (call.arguments or {}).get("job_id")
    if runtime is None or not isinstance(job_id, str):
        return
    try:
        job = await runtime.lookup(job_id, owner=toolkit.caller_identity(), depth=depth, include_approval_state=False)
    except JobAccessError:
        return
    if job.kind != "delegation":
        return
    call.function = call.function.model_copy()
    call.function.external_execution = True
    call.function.external_execution_silent = True
    call.function.requires_confirmation = False
    call.function.approval_type = "mindroom_job_wait"
