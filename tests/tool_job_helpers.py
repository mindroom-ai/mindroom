"""Shared tool job runtime and assembled-function construction for tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from agno.tools.function import Function

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.bot import AgentBot
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.delegation.background import delegation_child, start_delegation
from mindroom.delegation.state import DelegationChild
from mindroom.matrix.identity import MatrixID
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.runtime import TERMINAL_STATUSES, BackgroundJob, BackgroundOutcome, ToolJobRuntime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime
    from pathlib import Path


# One generous bound for waits that must eventually finish; a slow CI host must not turn them into failures.
JOB_TEST_TIMEOUT = 30


def _authorize_all(_job: BackgroundJob) -> bool:
    return True


def _allow_execution(_owner: ToolExecutionIdentity, _function: Function, _arguments: Mapping[str, Any]) -> None:
    return None


async def _no_cleanup(_job: BackgroundJob) -> None:
    return None


def tool_job_runtime(
    storage_root: Path,
    *,
    authorize: Callable[[BackgroundJob], bool] = _authorize_all,
    authorize_execution: Callable[[ToolExecutionIdentity, Function, Mapping[str, Any]], None] = _allow_execution,
    cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] = _no_cleanup,
) -> ToolJobRuntime:
    """Build a runtime whose current grants allow every job and call and whose adapters need no extra cleanup."""
    return ToolJobRuntime(storage_root, authorize=authorize, authorize_execution=authorize_execution, cancel=cancel)


async def lookup(runtime: ToolJobRuntime, job_id: str, *, owner: ToolExecutionIdentity, depth: int) -> BackgroundJob:
    """Inspect an exact job as a caller would reach it, failing like any access when that caller cannot see it."""
    async with runtime._lock:
        return deepcopy(runtime._entry(job_id, owner, depth).job)


def pending_outcomes(runtime: ToolJobRuntime) -> list[BackgroundJob]:
    """Ready outcomes a holding reply would still deliver: unconsumed, not stopped, unclaimed, and authorized."""
    return [
        entry.job
        for entry in runtime._entries.values()
        if entry.job.status in TERMINAL_STATUSES and runtime._unconsumed(entry)
    ]


def pending_outcome(runtime: ToolJobRuntime, job_id: str) -> BackgroundJob | None:
    """The pending outcome of one job, if a holding reply would still deliver it."""
    return next((job for job in pending_outcomes(runtime) if job.job_id == job_id), None)


def user_stopped(runtime: ToolJobRuntime, job_id: str) -> bool:
    """Whether an explicit Stop marked this job; consuming its outcome does not count."""
    return runtime._entries[job_id].job.user_stop_receipt_order is not None


def assembled_function(entrypoint: Callable[..., object]) -> Function:
    """Build a function as MindRoom toolkit assembly binds it; only such functions may become jobs."""
    function = Function.from_callable(entrypoint)
    function.owning_toolkit = "application"
    return function


async def start_job(runtime: ToolJobRuntime, job_id: str, **options: Any) -> BackgroundJob:  # noqa: ANN401
    """Accept a job, then drop the claim its start minted, as a foreground caller whose wait ended early does."""
    job, claim = await runtime.start(job_id, **options)
    await runtime.release_wait(job_id, claim)
    return job


async def keep_child(_child: DelegationChild) -> None:
    """A cancellation cleanup that leaves the child as its operation settled it."""


async def start_delegation_job(runtime: ToolJobRuntime, child: DelegationChild, **options: Any) -> BackgroundJob:  # noqa: ANN401
    """Accept a native child, then drop the claim its start minted, as `start_job` does."""
    options.setdefault("cancel", keep_child)
    job, claim = await start_delegation(runtime, child, **options)
    await runtime.release_wait(job.job_id, claim)
    return job


async def wait_for_status(runtime: ToolJobRuntime, job_id: str, status: str) -> None:
    """Wait until the runtime publishes the expected status for an accepted job."""
    entry = runtime._entries[job_id]
    while entry.job.status != status:
        await entry.changed.wait()


def backdate_job(runtime: ToolJobRuntime, job_id: str, updated_at: datetime) -> None:
    """Backdate a saved job in memory so retention treats it as old."""
    entry = runtime._entries[job_id]
    entry.job = replace(entry.job, updated_at=updated_at.isoformat())


def job_owner() -> ToolExecutionIdentity:
    """The parent identity most runtime tests start jobs under."""
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="parent",
        requester_id="@alice:test",
        room_id="!room:test",
        thread_id=None,
        resolved_thread_id="$root",
        session_id="parent-session",
    )


def job_child(job_id: str = "a" * 32) -> DelegationChild:
    """A native child owned by `job_owner`, with a reusable subagent ID."""
    return DelegationChild(
        delegation_id=job_id,
        parent_tool_call_id="call",
        caller_agent_name="parent",
        child_agent_name="child",
        task="research",
        session_id="child-session",
        run_id="run",
        model_name="default",
        depth=1,
        execution_identity={},
        subagent_id="b" * 32,
    )


def managed_team_config(tmp_path: Path) -> Config:
    """An enabled instance whose `team` routes through `lead`, which may delegate to `worker`."""
    access = ResponderAccessConfig(users=["@human:localhost"], current_room_members=False)
    return bind_runtime_paths(
        Config(
            background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
            agents={
                "lead": AgentConfig(display_name="Lead", delegate_to=["worker"], access=access),
                "worker": AgentConfig(display_name="Worker", access=access),
            },
            teams={"team": TeamConfig(display_name="Team", role="Work", agents=["lead"], access=access)},
        ),
        runtime_paths=test_runtime_paths(tmp_path),
    )


def completed_delegation_job() -> BackgroundJob:
    """A finished native delegation from `lead` to `worker`, as `managed_team_config` routes it."""
    owner = ToolExecutionIdentity(
        channel="matrix",
        agent_name="lead",
        requester_id="@human:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="!room:localhost_$thread",
        transport_agent_name="team",
    )
    child = DelegationChild(
        delegation_id="job_123",
        parent_tool_call_id="call_123",
        caller_agent_name="lead",
        child_agent_name="worker",
        task="Inspect files",
        session_id="child_session",
        run_id="child_run",
        model_name="default",
        depth=1,
        execution_identity={},
    )
    return BackgroundJob(
        job_id="job_123",
        tool_name="delegate",
        depth=0,
        kind="delegation",
        adapter={"child": asdict(child)},
        owner=owner,
        status="completed",
        result="Finished @worker",
    )


def team_coordinator(tmp_path: Path, config: Config) -> ToolJobRuntimeCoordinator:
    """A coordinator whose only bot is the `team` transport."""
    bot = MagicMock(spec=AgentBot)
    bot.matrix_id = MatrixID.parse("@mindroom_team:localhost")
    return ToolJobRuntimeCoordinator(
        runtime_paths=test_runtime_paths(tmp_path),
        config_provider=lambda: config,
        bot_provider=lambda name: bot if name == "team" else None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
    )


async def finish_delegation_job(coordinator: ToolJobRuntimeCoordinator) -> BackgroundJob:
    """Run `completed_delegation_job` to a saved outcome whose claim is released."""
    fixture = completed_delegation_job()

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Saved answer")

    job = await start_delegation_job(
        coordinator.runtime,
        delegation_child(fixture),
        owner=fixture.owner,
        operation=operation,
    )
    result = await coordinator.runtime.wait(job.job_id, owner=job.owner, depth=0)
    await coordinator.runtime.release_wait(job.job_id, result.claim)
    return result.job


async def awaiting_approval(runtime: ToolJobRuntime, job_id: str) -> BackgroundOutcome:
    """Wait for approval cards forever, as a paused background child does until a decision or cancellation."""
    await runtime.set_awaiting_approval(job_id, awaiting=True)
    await asyncio.Event().wait()
    raise AssertionError
