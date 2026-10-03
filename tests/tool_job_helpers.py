"""Shared tool job runtime and assembled-function construction for tests."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict, replace
from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock
from uuid import uuid4

from agno.tools.function import Function

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.bot import AgentBot
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.delegation.background import delegation_child, start_delegation
from mindroom.delegation.state import DelegationChild
from mindroom.event_journal import EventJournalStore, ToolJobStore
from mindroom.event_journal.offloading import ThreadOffload
from mindroom.event_journal_open import event_journal_sqlite_path
from mindroom.matrix.identity import MatrixID
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.runtime import (
    TERMINAL_STATUSES,
    BackgroundJob,
    BackgroundOutcome,
    ToolJobRuntime,
    parse_saved_job,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from datetime import datetime
    from pathlib import Path

    import pytest


# One generous bound for waits that must eventually finish; a slow CI host must not turn them into failures.
JOB_TEST_TIMEOUT = 30


def _authorize_all(_job: BackgroundJob) -> bool:
    return True


def _allow_execution(_owner: ToolExecutionIdentity, _function: Function, _arguments: Mapping[str, Any]) -> None:
    return None


async def _no_cleanup(_job: BackgroundJob) -> None:
    return None


def tool_job_journal(storage_root: Path) -> EventJournalStore:
    """Open the event journal of one storage root, as one process does; another open is another process."""
    return EventJournalStore.open_sqlite(event_journal_sqlite_path(storage_root))


class ProcessRuntime(ToolJobRuntime):
    """One process's runtime over its own journal connection, which shutting down closes as the process exit would."""

    def __init__(
        self,
        journal: EventJournalStore,
        *,
        authorize: Callable[[BackgroundJob], bool],
        authorize_execution: Callable[[ToolExecutionIdentity, Function, Mapping[str, Any]], None],
        cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]],
    ) -> None:
        super().__init__(
            journal.tool_jobs(uuid4().hex),
            authorize=authorize,
            authorize_execution=authorize_execution,
            cancel=cancel,
        )
        self._journal = journal

    async def shutdown(self) -> None:
        """Shut down in order, then close this process's journal connection."""
        try:
            await super().shutdown()
        finally:
            await self.close_journal()

    async def close_journal(self) -> None:
        """Close this process's journal connection, as its exit does with or without an orderly shutdown."""
        await self._journal.close()


async def tool_job_runtime(
    storage_root: Path,
    *,
    authorize: Callable[[BackgroundJob], bool] = _authorize_all,
    authorize_execution: Callable[[ToolExecutionIdentity, Function, Mapping[str, Any]], None] = _allow_execution,
    cancel: Callable[[BackgroundJob], Awaitable[BackgroundOutcome | None]] = _no_cleanup,
) -> ProcessRuntime:
    """Start one process's runtime whose grants allow every job and call and whose adapters need no extra cleanup.

    It owns the saved jobs, fencing any earlier runtime, but leaves restoring them to `recover`.
    """
    runtime = ProcessRuntime(
        tool_job_journal(storage_root),
        authorize=authorize,
        authorize_execution=authorize_execution,
        cancel=cancel,
    )
    await runtime._store.take_ownership()
    return runtime


def _inline_statement[Result](_offload: ThreadOffload, call: Callable[[], Result]) -> asyncio.Future[Result]:
    work = asyncio.get_running_loop().create_future()
    try:
        work.set_result(call())
    except Exception as error:
        work.set_exception(error)
    return work


def run_journal_statements_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run journal statements on the event loop, so an idle loop means every save has landed or failed."""
    monkeypatch.setattr(ThreadOffload, "submit", _inline_statement)


# The journal's own saves, which every interception wraps however often a test intercepts them.
_JOURNAL_ACCEPT, _JOURNAL_SAVE = ToolJobStore.accept, ToolJobStore.save
# Runs around one job save with its decoded snapshot and payload; raising fails the save, before or after it lands.
type SaveHook = Callable[[dict[str, Any], object | None], Awaitable[None] | None]


def intercept_job_saves(
    monkeypatch: pytest.MonkeyPatch,
    *,
    before: SaveHook | None = None,
    after: SaveHook | None = None,
) -> None:
    """Run hooks around every journal save of a job, as a failing or slow database would; no hooks restores saving."""
    original_accept, original_save = _JOURNAL_ACCEPT, _JOURNAL_SAVE

    async def hook(callback: SaveHook | None, job_json: str, payload_json: str | None) -> None:
        if callback is not None:
            result = callback(json.loads(job_json), None if payload_json is None else json.loads(payload_json))
            if result is not None:
                await result

    async def accept(store: ToolJobStore, job_id: str, job_json: str) -> None:
        await hook(before, job_json, None)
        await original_accept(store, job_id, job_json)
        await hook(after, job_json, None)

    async def save(store: ToolJobStore, job_id: str, job_json: str, result_payload_json: str | None = None) -> None:
        await hook(before, job_json, result_payload_json)
        await original_save(store, job_id, job_json, result_payload_json)
        await hook(after, job_json, result_payload_json)

    monkeypatch.setattr(ToolJobStore, "accept", accept)
    monkeypatch.setattr(ToolJobStore, "save", save)


async def saved_jobs(storage_root: Path) -> dict[str, BackgroundJob]:
    """Read every job the journal of one storage root holds, as the next process would recover it."""
    journal = tool_job_journal(storage_root)
    try:
        return {saved.job_id: parse_saved_job(saved) for saved in await journal.saved_tool_jobs()}
    finally:
        await journal.close()


async def write_saved_job(storage_root: Path, job_id: str, job_json: str) -> None:
    """Save one snapshot as is, as an earlier process might have left it; the writer takes the jobs over."""
    journal = tool_job_journal(storage_root)
    try:
        store = journal.tool_jobs(uuid4().hex)
        await store.take_ownership()
        if job_id in {saved.job_id for saved in await journal.saved_tool_jobs()}:
            await store.save(job_id, job_json)
        else:
            await store.accept(job_id, job_json)
    finally:
        await journal.close()


async def saved_payload(storage_root: Path, job_id: str) -> object:
    """Read the outcome payload saved for one job, or None."""
    journal = tool_job_journal(storage_root)
    try:
        payload = await journal.tool_jobs("reader").load_payload(job_id)
    finally:
        await journal.close()
    return None if payload is None else json.loads(payload)


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
    paths = test_runtime_paths(tmp_path)
    return ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bot_provider=lambda name: bot if name == "team" else None,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        journal_provider=partial(tool_job_journal, paths.storage_root),
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
