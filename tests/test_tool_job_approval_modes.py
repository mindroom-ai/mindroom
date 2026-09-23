"""Accepted approval arguments survive toolkit exclusion changes and nested execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.agent import _tools as agent_tools
from agno.db.sqlite import SqliteDb
from agno.models.response import ModelResponse
from agno.run import RunContext
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from agno.team import Team
from agno.team import _tools as team_tools
from agno.tools import Toolkit

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.response_turn import paused_attempt_from_response
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_jobs.wait_timeout import record_tool_wait_mode, saved_tool_wait_mode
from mindroom.tool_system.construction import ToolConstruction, bind_toolkit_construction
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path


async def _output(operation: Awaitable[RunOutput | TeamRunOutput] | AsyncIterator[object]) -> RunOutput | TeamRunOutput:
    if not isinstance(operation, AsyncIterator):
        return await operation
    result = None
    async for event in operation:
        if isinstance(event, (RunOutput, TeamRunOutput)):
            result = event
    assert result is not None
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
@pytest.mark.parametrize("positional", [False, True])
async def test_sdk_preparation_preserves_wait_modes_with_either_call_style(
    tmp_path: Path,
    *,
    team: bool,
    positional: bool,
) -> None:
    """Supported SDK positional calls carry the same saved ownership as keyword calls."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(id="test")
    install_tool_job_execution(model)
    run = TeamRunOutput(run_id="saved", metadata={}) if team else RunOutput(run_id="saved", metadata={})
    run_context = RunContext(run_id="saved", session_id="session", session_state={}, metadata={})
    record_tool_wait_mode(run.metadata, "saved", "call", "native")
    if team:
        prepare = team_tools._determine_tools_for_model
        arguments = {
            "team": Team(id="leader", members=[], tools=[]),
            "model": model,
            "run_response": run,
            "run_context": run_context,
            "team_run_context": {},
            "session": TeamSession(session_id="session", team_id="leader"),
        }
    else:
        prepare = agent_tools.determine_tools_for_model
        arguments = {
            "agent": Agent(id="leader"),
            "model": model,
            "processed_tools": [],
            "run_response": run,
            "run_context": run_context,
            "session": AgentSession(session_id="session", agent_id="leader"),
        }
    try:
        with tool_runtime_context(context):
            if positional:
                prepare(*arguments.values())
            else:
                prepare(**arguments)
        assert saved_tool_wait_mode(run_context.metadata, "saved", "call") == "native"
    finally:
        await runtime.shutdown()
        register_background_runtime(paths, None)


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
@pytest.mark.parametrize("actor_kind", ["agent", "team", "member"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("initially_excluded", [False, True])
async def test_saved_approval_retains_timeout_semantics_after_exclusion_change(  # noqa: PLR0915 - Exercise persisted approval and later calls together.
    tmp_path: Path,
    actor_kind: str,
    stream: bool,
    initially_excluded: bool,
) -> None:
    """Restarted approvals retain accepted arguments; later calls use the new policy."""
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
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    observed: list[int] = []
    storage = SqliteDb(db_file=str(tmp_path / "approvals.db"))

    def actor(model: DelegationModel, *, confirm: bool = True, resuming: bool = False) -> Agent | Team:
        install_tool_job_execution(model)
        toolkit = _NativeTools(observed)
        toolkit.get_async_functions()["native_step"].requires_confirmation = confirm
        kwargs = {"id": "leader", "model": model, "tools": [toolkit], "db": storage, "telemetry": False}
        if actor_kind == "member":
            coordinator = DelegationModel(
                id="coordinator",
                responses=[
                    *(
                        []
                        if resuming
                        else [
                            ModelResponse(
                                tool_calls=[
                                    _call("delegate_task_to_member", "member", member_id="leader", task="Execute"),
                                ],
                            ),
                        ]
                    ),
                    ModelResponse(content="team done"),
                ],
            )
            install_tool_job_execution(coordinator)
            return Team(id="coordinator", model=coordinator, members=[Agent(**kwargs)], db=storage, telemetry=False)
        return Team(**kwargs, members=[]) if actor_kind == "team" else Agent(**kwargs)

    try:
        async with execution_resources():
            with tool_runtime_context(context):
                original = actor(
                    DelegationModel(
                        id="test",
                        responses=[
                            ModelResponse(
                                tool_calls=[
                                    _call("native_step", "approved", wait_timeout=3 if initially_excluded else 0),
                                ],
                            ),
                        ],
                    ),
                )
                paused = await _output(
                    original.arun(
                        "Execute",
                        session_id=context.session_id,
                        stream=stream,
                        stream_events=stream,
                        yield_run_output=True,
                    ),
                )
                assert paused.status is RunStatus.paused
                captured = paused_attempt_from_response(
                    paused,
                    fallback_session_id=context.session_id,
                    fallback_run_id=paused.run_id,
                    toolkit_owners={("leader", "native_step"): "native_plugin"},
                )
                assert captured is not None
                assert observed == []
                assert await runtime.list_jobs(owner=owner, depth=0) == []

                config.background_tool_jobs.exclude_toolkits = [] if initially_excluded else ["native_plugin"]
                restored = actor(
                    DelegationModel(
                        id="test",
                        responses=[
                            ModelResponse(
                                tool_calls=[
                                    _call("native_step", "new-call", wait_timeout=0 if initially_excluded else 5),
                                ],
                            ),
                            ModelResponse(content="approved result"),
                        ],
                    ),
                    resuming=True,
                )
                for requirement in paused.requirements or ():
                    requirement.confirm()
                options = {
                    "run_id": paused.run_id,
                    "session_id": context.session_id,
                    "requirements": paused.requirements,
                    "metadata": deepcopy(paused.metadata),
                }
                second_pause = await _output(
                    restored.acontinue_run(
                        **options,
                        stream=stream,
                        stream_events=stream,
                        yield_run_output=True,
                    ),
                )
                assert second_pause.status is RunStatus.paused
                config.background_tool_jobs.exclude_toolkits = ["native_plugin"] if initially_excluded else []
                for requirement in second_pause.requirements or ():
                    if requirement.needs_confirmation:
                        requirement.confirm()
                final_actor = actor(
                    DelegationModel(id="test", responses=[ModelResponse(content="done")]),
                    resuming=True,
                )
                completed = await _output(
                    final_actor.acontinue_run(
                        run_response=second_pause,
                        session_id=context.session_id,
                        requirements=second_pause.requirements,
                        metadata=deepcopy(second_pause.metadata),
                        stream=stream,
                        stream_events=stream,
                        yield_run_output=True,
                    ),
                )
                for job in await runtime.list_jobs(owner=owner, depth=0):
                    await runtime.wait(job.job_id, owner=owner, depth=0)
                assert completed.status is RunStatus.completed
                assert sorted(observed) == ([3, 7] if initially_excluded else [5, 7])
                assert len(await runtime.list_jobs(owner=owner, depth=0)) == 1
                assert captured.requires_background_tool_jobs is (not initially_excluded)

                # A new run can inherit metadata, but must never inherit the old
                # interpretation even if a provider reuses the same call ID.
                config.background_tool_jobs.exclude_toolkits = [] if initially_excluded else ["native_plugin"]
                fresh_model = DelegationModel(
                    id="test",
                    responses=[
                        ModelResponse(
                            tool_calls=[_call("native_step", "approved", wait_timeout=0 if initially_excluded else 5)],
                        ),
                        ModelResponse(content="new run result"),
                    ],
                )
                fresh = actor(fresh_model, confirm=False)
                await fresh.arun("New call", session_id=context.session_id, metadata=deepcopy(paused.metadata))
                for job in await runtime.list_jobs(owner=owner, depth=0):
                    await runtime.wait(job.job_id, owner=owner, depth=0)
                assert sorted(observed) == ([3, 7, 7] if initially_excluded else [5, 5, 7])
                assert len(await runtime.list_jobs(owner=owner, depth=0)) == (2 if initially_excluded else 1)
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()
        storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
async def test_earlier_managed_call_keeps_native_approval_owned(tmp_path: Path, team: bool) -> None:
    """A later excluded approval must not erase the run's earlier managed ownership."""

    async def ordinary_tool() -> str:
        return "completed"

    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True, exclude_toolkits=["native_plugin"]),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    context = _delegate_runtime_context(config, paths)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("ordinary_tool", "earlier")]),
            ModelResponse(tool_calls=[_call("native_step", "pending", wait_timeout=3)]),
        ],
    )
    install_tool_job_execution(model)
    kwargs = {"id": "leader", "model": model, "tools": [ordinary_tool, _NativeTools([])], "telemetry": False}
    actor = Team(**kwargs, members=[]) if team else Agent(**kwargs)
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                response = await actor.arun("Work then approve", session_id=context.session_id, metadata={})
                assert response.tools is not None
                assert response.tools[0].result == "completed"
                paused = paused_attempt_from_response(
                    response,
                    fallback_session_id=context.session_id,
                    fallback_run_id=response.run_id,
                    toolkit_owners={("leader", "native_step"): "native_plugin"},
                )
                assert paused is not None
                assert paused.requires_background_tool_jobs
                assert [tool.tool_call_id for tool in paused.tools] == ["pending"]
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


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
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow_child_tool", "child-call")]),
            ModelResponse(content="child done"),
        ],
    )
    install_tool_job_execution(model, depth=1)
    actor = Agent(id="leader", model=model, tools=[slow_child_tool], telemetry=False)
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
        register_background_runtime(paths, None)
        await runtime.shutdown()
