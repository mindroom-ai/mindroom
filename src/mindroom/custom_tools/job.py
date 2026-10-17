"""One conversation-scoped management function for all managed tool jobs."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

from agno.tools import Toolkit
from agno.tools.function import ToolResult

from mindroom.tool_jobs.consumption import consume_tool_job, restore_control
from mindroom.tool_jobs.conversation import current_approval_wait_timeout, current_conversation
from mindroom.tool_jobs.runtime import (
    JobAccessError,
    JobUnavailableError,
    describe_job,
    format_job_handle,
    get_background_runtime,
    job_summary,
)
from mindroom.tool_system.declarations import tool_schema_source
from mindroom.tool_system.output_files import wrap_toolkit_for_output_files
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from agno.tools.function import Function

    from mindroom.constants import RuntimePaths
    from mindroom.tool_jobs.runtime import BackgroundJob, JobClaim, ToolJobRuntime
    from mindroom.tool_system.output_files import ToolOutputFilePolicy
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

# One answer for every reason, so a caller never learns whether a job of another conversation or caller exists.
_UNAVAILABLE = (
    "No job with this ID is available to you in this conversation. The ID may be wrong or incomplete, the job may "
    "have been deleted after its retention period, it may belong to another conversation or caller, or your access "
    'to it may have changed. Call job(action="list") to see the jobs you can access.'
)


def is_job_function(function: Function) -> bool:
    """Recognize the reserved implementation through SDK and MindRoom output wrappers, never a same-named plugin."""
    entrypoint = tool_schema_source(inspect.unwrap(function.entrypoint)) if function.entrypoint is not None else None
    return (
        inspect.ismethod(entrypoint)
        and isinstance(entrypoint.__self__, JobTools)
        and entrypoint.__func__ is JobTools.job
    )


async def _claimed_result(runtime: ToolJobRuntime, job: BackgroundJob, claim: JobClaim) -> Any:  # noqa: ANN401 - SDK tool value.
    """Return a claimed outcome as this call's own result, raising its control or failure for the SDK to record."""
    value, payload = await consume_tool_job(runtime, job, claim)
    if payload.control is not None:
        raise restore_control(payload.control)
    if job.status == "failed":
        text = value.content if isinstance(value, ToolResult) else value
        raise RuntimeError(str(payload.error or text or "Background tool job failed."))
    return value


async def _collect(runtime: ToolJobRuntime, owner: ToolExecutionIdentity) -> Any:  # noqa: ANN401 - SDK tool value.
    """Return every ended, unacknowledged job of this caller with its result, acknowledging each once this call saves."""
    sections: list[str] = []
    collected: list[ToolResult] = []
    current = current_conversation()
    for job in () if current is None else await current[0].ended_jobs(current[1]):
        try:
            waited = await runtime.wait(job.job_id, owner=owner, depth=0, timeout=0)
        except JobAccessError:
            # A team member collects only its own jobs; the team collects them all.
            continue
        if waited.claim is None:
            # Another caller is reading this outcome now.
            continue
        try:
            value, payload = await consume_tool_job(runtime, waited.job, waited.claim)
        except ValueError:
            # Unavailable or undecodable; its claim is released, so a later collect reads it, and the others arrive now.
            sections.append(f"{describe_job(job)} {job.status}: its result could not be read now")
            continue
        if isinstance(value, ToolResult):
            collected.append(value)
            value = value.content
        detail = payload.error or value or ""
        sections.append(f"{describe_job(job)} {job.status}:\n{detail}".rstrip())
    if not sections:
        return "No ended background jobs."
    content = "\n\n".join(sections)
    images = [item for result in collected for item in result.images or ()]
    audios = [item for result in collected for item in result.audios or ()]
    videos = [item for result in collected for item in result.videos or ()]
    files = [item for result in collected for item in result.files or ()]
    if images or audios or videos or files:
        return ToolResult(
            content=content,
            images=images or None,
            audios=audios or None,
            videos=videos or None,
            files=files or None,
        )
    return content


class JobTools(Toolkit):
    """Discover and manage jobs belonging to the exact caller and conversation."""

    def __init__(self, runtime_paths: RuntimePaths, owner: ToolExecutionIdentity) -> None:
        self._runtime_paths = runtime_paths
        self._owner = owner
        super().__init__(
            name="job",
            tools=[self.job],
            instructions=(
                "Managed tool calls accept background_after: omitted or null waits until completion or a newer message "
                "you answer, zero returns a job_id immediately, and a positive number is how many seconds to wait "
                "before the call continues in the background, without cancelling it. "
                "Only use background_after when the tool schema exposes it; excluded toolkits keep their native controls. "
                "Calls made inside an already running job stay with that job and accept no background_after. "
                "A newer message you answer releases waits while work continues. "
                'Use job(action="list") to rediscover jobs and their status after a new turn, '
                'job(action="wait", job_id=...) to retrieve the actual result, and job(action="cancel", job_id=...) to ask '
                "one to stop; the next notice says how it ended. "
                'When told background jobs ended, call job(action="collect") once to read and acknowledge all of them. '
                "You can access the jobs you started; when you answer as a team, also the jobs your members started for it."
            ),
        )
        self.async_functions["job"].owning_toolkit = "job"

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
        """Build the reserved toolkit for a top-level Matrix caller while the job runtime runs, else nothing."""
        if (
            not enabled
            or depth != 0
            or owner is None
            or owner.channel != "matrix"
            or get_background_runtime(runtime_paths) is None
        ):
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
        action: Literal["list", "wait", "cancel", "collect"],
        job_id: str | None = None,
        limit: int = 20,
        offset: int = 0,
        wait_timeout: float | None = None,
    ) -> Any:  # noqa: ANN401 - Preserve the original SDK tool result type.
        """Discover, wait for, or cancel this caller's managed work.

        Args:
            action: Operation; list reports status, saved summaries, and the latest progress of running work, wait
                retrieves the stored result or, when it times out, the job's progress, and collect returns and
                acknowledges every ended job.
            job_id: Exact job ID, required for wait and cancel.
            limit: Maximum number of jobs to list, from 1 to 100; active jobs first.
            offset: Number of accessible jobs to skip, at least zero.
            wait_timeout: Seconds to wait; null waits until completion or a newer message, zero returns immediately.

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
            if action == "collect":
                return await _collect(runtime, owner)
            if job_id is None:
                return "job_id is required for this action."
            if action == "wait":
                waited = await runtime.wait(
                    job_id,
                    owner=owner,
                    depth=0,
                    timeout=wait_timeout,
                    # A job awaiting a human decision holds the reply no longer than the gated call itself does.
                    approval_timeout=current_approval_wait_timeout(),
                )
                if waited.claim is not None:
                    return await _claimed_result(runtime, waited.job, waited.claim)
                return format_job_handle(waited.job)
            if action != "cancel":
                return "Unknown job action."
            # The job stops on its own time; the next notice reports how it ended.
            return format_job_handle(await runtime.cancel(job_id, owner=owner, depth=0))
        except JobUnavailableError:
            return _UNAVAILABLE
        except ValueError as error:
            # Invalid limits, offsets, or wait budgets are the model's to correct.
            return str(error)
