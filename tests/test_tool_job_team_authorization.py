"""Managed tools retain the requester grants of configured and ad hoc teams."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team

from mindroom.config.access import ResponderAccessConfig
from mindroom.delegation.background import delegation_child
from mindroom.delegation.lifecycle import child_run_context, prepare_child_turn, start_child_turn
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import authority_snapshot, bind_actor_authority, bind_toolkit_authority
from mindroom.tool_jobs.completion import join_conversation_jobs
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import (
    BackgroundOutcome,
    JobAccessError,
    register_background_runtime,
    saved_job_paths,
)
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity, tool_execution_identity
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context
from tests.tool_job_helpers import (
    completed_delegation_job,
    managed_team_config,
    pending_outcome,
    pending_outcomes,
    start_delegation_job,
    start_job,
    team_coordinator,
    tool_job_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def _calculator_agent(config: Config, paths: RuntimePaths) -> Agent:
    toolkit = get_tool_by_name("calculator", paths, worker_target=None, disable_sandbox_proxy=True)
    bind_toolkit_authority(toolkit, authored_name="calculator")
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("add", "addition", a=2, b=3)]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    return bind_actor_authority(
        Agent(
            id="worker",
            name="Worker",
            model=model,
            tools=[toolkit],
        ),
        authority_snapshot(config, "worker"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_team_member_tool_uses_actual_actor_and_current_requester_grants(
    tmp_path: Path,
    configured: bool,
) -> None:
    """A member's tool becomes a job, while the leader's SDK delegation runs inline, until the grant is withdrawn."""
    config = managed_team_config(tmp_path)
    config.agents["worker"].tools = ["calculator"]
    config.teams["team"].agents = ["lead", "worker"]
    coordinator = team_coordinator(tmp_path, config)
    paths = coordinator.runtime_paths
    owner = replace(completed_delegation_job().owner, transport_agent_name="team" if configured else "lead")
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(
                tool_calls=[_call("delegate_task_to_member", "member", member_id="worker", task="Add 2 and 3")],
            ),
            ModelResponse(content="team done"),
        ],
    )
    install_tool_job_execution(model)
    team = Team(id="ad-hoc", model=model, members=[_calculator_agent(config, paths)])
    try:
        await coordinator.sync()
        async with execution_resources():
            with tool_runtime_context(context), tool_execution_identity(owner):
                await team.arun("Calculate", session_id=owner.session_id)
                member_owner = replace(owner, agent_name="worker")
                jobs = await coordinator.runtime.list_jobs(owner=member_owner, depth=0)
                assert len(jobs) == 1
                assert len(saved_job_paths(paths.storage_root / "tool_jobs")) == 1
                waited = await coordinator.runtime.wait(jobs[0].job_id, owner=member_owner, depth=0)
                assert waited.job.status == "completed", waited.job.result
                assert json.loads(waited.job.result)["result"] == 5
                assert await coordinator.runtime.list_jobs(owner=owner, depth=0) == []
                config.agents["worker"].access = ResponderAccessConfig(current_room_members=False)
                assert await coordinator.runtime.list_jobs(owner=member_owner, depth=0) == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_single_agent_turn_leaves_other_member_jobs_for_a_team_reply(tmp_path: Path) -> None:
    """An ordinary later turn must not receive an unusable other-member retrieval directive."""
    config = managed_team_config(tmp_path)
    paths = team_coordinator(tmp_path, config).runtime_paths
    owner = replace(completed_delegation_job().owner, agent_name="worker", transport_agent_name="lead")
    runtime = tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name="lead",
        transport_agent_name="lead",
    )

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Member result")

    try:
        await start_job(runtime, "member", tool_name="report", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("member", owner=owner, depth=0)
        await runtime.release_wait("member", waited.claim)
        with tool_runtime_context(context):
            assert [item async for item in join_conversation_jobs(set())] == []
            joined = [item async for item in join_conversation_jobs(set(), agent_names=("lead", "worker"))]
            assert len(joined) == 1
            assert not isinstance(joined[0], str)
            assert 'job_id="member"' in joined[0].prompt
        assert pending_outcome(runtime, "member") is not None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_agent", ["lead", "worker"])
async def test_delegation_storage_change_revokes_discovery_and_controls(tmp_path: Path, changed_agent: str) -> None:
    """Native retrieval and generic discovery enforce the same frozen caller/child storage scope."""
    config = managed_team_config(tmp_path)
    coordinator = team_coordinator(tmp_path, config)
    await coordinator.initialize()
    paths, owner = coordinator.runtime_paths, completed_delegation_job().owner
    child = prepare_child_turn("lead", "worker", "Work", owner=owner, config=config, runtime_paths=paths, depth=0)
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )

    async def operation() -> BackgroundOutcome:
        child.status = "completed"
        child.result = "saved answer"
        return BackgroundOutcome("completed", child.result)

    runtime = coordinator.runtime
    register_background_runtime(paths, runtime)
    try:
        job = await start_delegation_job(runtime, child, owner=owner, operation=operation)
        waited = await runtime.wait(job.job_id, owner=owner, depth=0)
        await runtime.release_wait(job.job_id, waited.claim)
        assert len(await runtime.list_jobs(owner=owner, depth=0)) == 1
        assert len(pending_outcomes(runtime)) == 1
        config.agents[changed_agent].worker_scope = "user"
        assert await runtime.list_jobs(owner=owner, depth=0) == []
        assert pending_outcomes(runtime) == []
        assert pending_outcome(runtime, job.job_id) is None
        with tool_runtime_context(context):
            assert [item async for item in join_conversation_jobs(set())] == []
        for control in (runtime.wait, runtime.cancel):
            with pytest.raises(JobAccessError):
                await control(job.job_id, owner=owner, depth=0)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_ad_hoc_member_native_delegation_retains_transport_and_ancestry(tmp_path: Path) -> None:
    """Native child tools and durable results keep an ordinary agent's ad hoc team transport."""
    config = managed_team_config(tmp_path)
    config.agents["carrier"] = config.agents["lead"].model_copy(deep=True)
    config.agents["worker"].tools = ["calculator"]
    coordinator = team_coordinator(tmp_path, config)
    paths = coordinator.runtime_paths
    owner = replace(completed_delegation_job().owner, transport_agent_name="carrier")
    child = delegation_child(completed_delegation_job())
    child_owner = replace(owner, agent_name="worker", session_id=child.session_id)
    child.execution_identity = serialize_tool_execution_identity(child_owner)
    await start_child_turn(
        child,
        parent_run_id="parent",
        config=config,
        runtime_paths=paths,
        caller_execution_identity=owner,
    )
    agent = _calculator_agent(config, paths)
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )

    async def operation() -> BackgroundOutcome:
        async with child_run_context(child, config=config, runtime_paths=paths):
            with tool_execution_identity(child_owner):
                response = await agent.arun("Calculate", session_id=child.session_id)
                assert response.tools is not None
                assert not response.tools[0].tool_call_error
                return BackgroundOutcome("completed", response.tools[0].result)

    try:
        await coordinator.sync()
        async with execution_resources():
            with tool_runtime_context(context), tool_execution_identity(owner):
                job = await start_delegation_job(coordinator.runtime, child, owner=owner, operation=operation)
                waited = await coordinator.runtime.wait(job.job_id, owner=owner, depth=0)
                assert waited.job.status == "completed", waited.job.result
                assert json.loads(waited.job.result)["result"] == 5
                config.agents["lead"].delegate_to = []
                assert await coordinator.runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        await coordinator.stop()
