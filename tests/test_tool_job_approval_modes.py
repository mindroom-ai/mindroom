"""Approved calls follow current waiting policy, and nested execution keeps its owner."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.team import Team
from agno.tools import Toolkit

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.response_turn import paused_attempt_from_response
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import assembled_function, tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path


class _NativeTools(Toolkit):
    def __init__(self, observed: list[int]) -> None:
        self.observed = observed
        super().__init__(name="native", tools=[self.native_step])
        bind_toolkit_construction(self, ToolConstruction("native_plugin", None))
        bind_toolkit_authority(self, authored_name="native_plugin")
        self.get_async_functions()["native_step"].requires_confirmation = True

    async def native_step(self, **options: int) -> str:
        """Observe native keywords without declaring a reserved managed parameter."""
        wait_timeout = options.get("wait_timeout", 7)
        self.observed.append(wait_timeout)
        return f"native:{wait_timeout}"


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("initially_excluded", [False, True])
async def test_approved_call_follows_exclusions_current_at_resume(
    tmp_path: Path,
    *,
    team: bool,
    initially_excluded: bool,
) -> None:
    """Waiting policy is decided when an approved call executes, so a restart with new exclusions applies to it."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(
            enabled=True,
            exclude_toolkits=["native_plugin"] if initially_excluded else [],
        ),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    observed: list[int] = []
    storage = SqliteDb(db_file=str(tmp_path / "approvals.db"))

    def actor(model: DelegationModel) -> Agent | Team:
        install_tool_job_execution(model)
        kwargs = {"id": "leader", "model": model, "tools": [_NativeTools(observed)], "db": storage, "telemetry": False}
        return Team(**kwargs, members=[]) if team else Agent(**kwargs)

    try:
        async with execution_resources():
            with tool_runtime_context(context):
                paused = await actor(
                    DelegationModel(
                        id="test",
                        responses=[ModelResponse(tool_calls=[_call("native_step", "approved", wait_timeout=3)])],
                    ),
                ).arun("Execute", session_id=context.session_id)
                assert paused.status is RunStatus.paused
                captured = paused_attempt_from_response(
                    paused,
                    fallback_session_id=context.session_id,
                    fallback_run_id=paused.run_id,
                    toolkit_owners={("leader", "native_step"): "native_plugin"},
                )
                assert captured is not None

                # A changed exclusion list takes effect when the instance starts again.
                config.background_tool_jobs.exclude_toolkits = [] if initially_excluded else ["native_plugin"]
                pin_background_tool_jobs(config, paths)
                register_background_runtime(paths, runtime)
                for requirement in paused.requirements or ():
                    requirement.confirm()
                completed = await actor(
                    DelegationModel(id="test", responses=[ModelResponse(content="done")]),
                ).acontinue_run(
                    run_id=paused.run_id,
                    session_id=context.session_id,
                    requirements=paused.requirements,
                )
                for job in await runtime.list_jobs(owner=owner, depth=0):
                    await runtime.wait(job.job_id, owner=owner, depth=0)
        assert completed.status is RunStatus.completed
        # Newly managed, the reserved budget never reaches the tool; newly excluded, it is the tool's own argument.
        assert observed == ([7] if initially_excluded else [3])
        assert len(await runtime.list_jobs(owner=owner, depth=0)) == (1 if initially_excluded else 0)
    finally:
        await runtime.shutdown()
        storage.close()


@pytest.mark.asyncio
async def test_nested_native_owner_keeps_slow_child_tool_after_human_signal(tmp_path: Path) -> None:
    """A delegated child without an outer managed job must never detach an orphan job."""
    started, release = asyncio.Event(), asyncio.Event()
    effects: list[str] = []

    async def slow_child_tool() -> str:
        started.set()
        await release.wait()
        effects.append("finished once")
        return effects[0]

    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True, exclude_toolkits=["delegate"]),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow_child_tool", "child-call")]),
            ModelResponse(content="child done"),
        ],
    )
    install_tool_job_execution(model, depth=1)
    actor = Agent(id="leader", model=model, tools=[assembled_function(slow_child_tool)], telemetry=False)
    signal = HumanMessageSignal()
    task = None
    try:
        async with execution_resources():
            with tool_runtime_context(context), human_message_signal_context(signal):
                task = asyncio.create_task(actor.arun("Work", session_id=context.session_id, metadata={}))
                await asyncio.wait_for(started.wait(), 2)
                signal.notify()
                done, _ = await asyncio.wait({task}, timeout=0.05)
                assert not done, "Child returned before its tool finished"
                assert await runtime.list_jobs(owner=owner, depth=1) == []
                release.set()
                result = await asyncio.wait_for(task, 2)
                assert result.tools is not None
                assert result.tools[0].result == "finished once"
                assert effects == ["finished once"]
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await runtime.shutdown()
