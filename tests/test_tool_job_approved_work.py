"""A natively approved call keeps running as a background job after its approval resumes the reply.

The gate is the toolkit's own (``requires_confirmation`` on the function), as a tool that insists on a human
decision declares it: a policy-gated call that can run as a job takes its approval as the job's first phase instead.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, ClassVar

import pytest
from agno.tools import Toolkit

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.declarations import ToolFileAccess
from mindroom.tool_system.metadata import ToolCategory
from mindroom.tool_system.registration import register_tool_with_metadata
from mindroom.tool_system.registry_state import capture_tool_registry_snapshot, restore_tool_registry_snapshot
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from tests.delegation_helpers import (
    ScriptedAgents,
    _delegate_runtime_context,
    _runtime_paths,
    approval_calls_for,
    approval_executor,
    respond,
    resume,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.tool_job_helpers import lookup, pending_outcomes, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


class SlowTools(Toolkit):
    """One natively gated function whose run the test holds open until it is released."""

    started: ClassVar[asyncio.Event]
    release: ClassVar[asyncio.Event]
    executions: ClassVar[int] = 0

    def __init__(self) -> None:
        super().__init__(name="slow", tools=[self.work])
        for functions in (self.functions, self.async_functions):
            if "work" in functions:
                functions["work"].requires_confirmation = True

    async def work(self) -> str:
        """Do the approved work."""
        type(self).executions += 1
        type(self).started.set()
        await type(self).release.wait()
        return "retained actual result"


@contextmanager
def _registered_slow_tool() -> Iterator[type[SlowTools]]:
    """Register the ``slow`` toolkit for one test and restore the registries afterwards."""
    snapshot = capture_tool_registry_snapshot()

    @register_tool_with_metadata(
        name="slow",
        file_access=ToolFileAccess.NONE,
        display_name="Slow",
        description="Holds its work open until released.",
        category=ToolCategory.DEVELOPMENT,
    )
    def slow_factory() -> type[SlowTools]:
        return SlowTools

    SlowTools.started, SlowTools.release, SlowTools.executions = asyncio.Event(), asyncio.Event(), 0
    try:
        yield SlowTools
    finally:
        restore_tool_registry_snapshot(snapshot)


async def _wait_for_progress(pending: asyncio.Task[object], progress: asyncio.Event) -> None:
    """Wait for real progress, surfacing a premature response failure immediately."""
    waiter = asyncio.create_task(progress.wait())
    try:
        await asyncio.wait({pending, waiter}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if pending.done():
            pending.result()
        assert progress.is_set(), "response did not reach expected progress"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("background_after", [0, 0.001])
async def test_native_approval_leaves_running_work_to_its_follow_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    background_after: float,
) -> None:
    """A resumed approval finishes while its approved work runs on, leaving that work to its follow-up."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader", tools=["slow"])},
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    model = ScriptedModel(
        [
            calls(tool_call("work", "approved-call", background_after=background_after)),
            answer("Independent work done."),
        ],
    )
    ScriptedAgents({"leader": model}).install(monkeypatch)
    options = {"config": config, "runtime_paths": paths, "identity": owner}
    pending = None
    with _registered_slow_tool() as slow:
        try:
            paused = await respond("leader", "Start the approved work", **options)
            assert isinstance(paused, PausedAttempt)
            assert slow.executions == 0
            approval_calls = approval_calls_for(paused, default_agent="leader", toolkit_names={"work": "slow"})
            assert [call.tool_call_id for call in approval_calls] == ["approved-call"]

            pending = asyncio.create_task(
                resume(
                    approval_executor(config, paths),
                    paused,
                    approval_calls,
                    agent_name="leader",
                    decisions={call.tool_call_id: True for call in approval_calls},
                    **options,
                ),
            )
            await _wait_for_progress(pending, slow.started)
            result = await asyncio.wait_for(pending, 30)
            assert isinstance(result, CompletedApprovalRun)
            assert "Independent work done." in result.response_text
            jobs = await runtime.list_jobs(owner=owner, depth=0)
            assert len(jobs) == 1
            assert (await lookup(runtime, jobs[0].job_id, owner=owner, depth=0)).status == "running"
            slow.release.set()
            ready = await runtime.wait(jobs[0].job_id, owner=owner, depth=0)
            await runtime.release_wait(jobs[0].job_id, ready.claim)
            # Nothing retrieved the outcome; its follow-up reports it.
            assert [job.job_id for job in pending_outcomes(runtime)] == [jobs[0].job_id]
            assert slow.executions == 1
        finally:
            slow.release.set()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
            await runtime.shutdown()
