"""Managed tools retain the requester grants of configured and ad hoc teams."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team
from agno.tools import Toolkit

from mindroom.agents import apply_tool_approval_capability, team_background_jobs
from mindroom.config.access import ResponderAccessConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.background import delegation_child
from mindroom.delegation.lifecycle import child_run_context, prepare_child_turn, start_child_turn
from mindroom.delegation.state import PersonaSourceKind, SubagentPersona
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import authority_snapshot, bind_actor_authority, bind_toolkit_authority
from mindroom.tool_jobs.conversation import current_conversation
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import (
    BackgroundOutcome,
    JobAccessError,
    register_background_runtime,
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
    saved_jobs,
    start_delegation_job,
    start_job,
    team_coordinator,
    tool_job_runtime,
    wait_for_status,
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
    """A member's tool becomes a job, while the leader's SDK delegation runs inline, until the grant is withdrawn.

    A configured team's access is the grant for its exact members; an ad hoc team's member needs its own.
    """
    config = managed_team_config(tmp_path)
    config.agents["worker"].tools = ["calculator"]
    config.teams["team"].agents = ["lead", "worker"]
    if configured:
        config.agents["worker"].access = ResponderAccessConfig(current_room_members=False)
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
                assert len(await saved_jobs(paths.storage_root)) == 1
                waited = await coordinator.runtime.wait(jobs[0].job_id, owner=member_owner, depth=0)
                assert waited.job.status == "completed", waited.job.result
                assert json.loads(waited.job.result)["result"] == 5
                # The entity that answers, the configured team or the ad hoc team's account, owns its member's job.
                answering = replace(owner, agent_name=owner.recipient)
                listed = await coordinator.runtime.list_jobs(owner=answering, depth=0)
                assert [job.job_id for job in listed] == [jobs[0].job_id]
                if configured:
                    config.teams["team"].access = ResponderAccessConfig(current_room_members=False)
                else:
                    config.agents["worker"].access = ResponderAccessConfig(current_room_members=False)
                assert await coordinator.runtime.list_jobs(owner=member_owner, depth=0) == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_the_entity_that_answers_owns_its_members_jobs(tmp_path: Path) -> None:
    """The entity whose account answers sees and collects a member's job; the member sees only its own."""
    config = managed_team_config(tmp_path)
    paths = team_coordinator(tmp_path, config).runtime_paths
    owner = replace(completed_delegation_job().owner, agent_name="worker", transport_agent_name="lead")
    runtime = await tool_job_runtime(paths.storage_root)
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
            current = current_conversation()
        assert current is not None
        assert [job.job_id for job in await current[0].ended_jobs(current[1])] == ["member"]
        answering = replace(owner, agent_name="lead")
        assert [job.job_id for job in await runtime.list_jobs(owner=answering, depth=0)] == ["member"]
        other_member = replace(owner, agent_name="other")
        assert await runtime.list_jobs(owner=other_member, depth=0) == []
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
    child = prepare_child_turn(
        "lead",
        "worker",
        "Work",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
    )
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
            current = current_conversation()
        assert current is not None
        assert await current[0].ended_jobs(current[1]) == ()
        for control in (runtime.wait, runtime.cancel):
            with pytest.raises(JobAccessError):
                await control(job.job_id, owner=owner, depth=0)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["inline", "profile"])
async def test_a_child_with_an_authored_persona_is_judged_by_its_grant(
    tmp_path: Path,
    source_kind: PersonaSourceKind,
) -> None:
    """A saved child's persona reads back as one, so admission, discovery, and revocation judge the child's grant."""
    config = managed_team_config(tmp_path)
    coordinator = team_coordinator(tmp_path, config)
    await coordinator.initialize()
    paths, owner = coordinator.runtime_paths, completed_delegation_job().owner
    child = prepare_child_turn(
        "lead",
        "worker",
        "Work",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
    )
    child.persona = SubagentPersona(source_kind=source_kind, source_name="analyst", system_prompt="Analyze.")

    async def operation() -> BackgroundOutcome:
        child.status = "completed"
        child.result = "saved answer"
        return BackgroundOutcome("completed", child.result)

    runtime = coordinator.runtime
    register_background_runtime(paths, runtime)
    try:
        job = await start_delegation_job(runtime, child, owner=owner, operation=operation)
        assert delegation_child(job).persona == child.persona
        await runtime.cancel_revoked(denied=lambda saved: coordinator._grant(saved) == "denied")
        assert [listed.job_id for listed in await runtime.list_jobs(owner=owner, depth=0)] == [job.job_id]
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


@pytest.mark.asyncio
async def test_a_team_leader_collects_the_jobs_its_members_started(tmp_path: Path) -> None:
    """The team's own job tool collects a member's ended job, as the team's reply that answers for it."""
    config = managed_team_config(tmp_path)
    paths = team_coordinator(tmp_path, config).runtime_paths
    member = replace(completed_delegation_job().owner, agent_name="lead", transport_agent_name="team")
    team = replace(member, agent_name="team")
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=team),
        agent_name="team",
        transport_agent_name="team",
    )

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Member result")

    try:
        await start_job(runtime, "member", tool_name="report", depth=0, adapter={}, owner=member, operation=operation)
        await wait_for_status(runtime, "member", "completed")
        controls = team_background_jobs(DelegationModel(id="leader"), config, paths, team)
        assert isinstance(controls, JobTools)
        with tool_runtime_context(context):
            collected = str(await controls.job("collect"))
        assert "`member` (report) completed" in collected
        assert "Member result" in collected
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_the_job_tool_never_asks_for_approval(tmp_path: Path) -> None:
    """A team leader's, like an agent's, job tool runs at once even when every call needs approval."""
    config = managed_team_config(tmp_path)
    config.tool_approval.default = "require_approval"
    paths = team_coordinator(tmp_path, config).runtime_paths
    team = replace(completed_delegation_job().owner, agent_name="team", transport_agent_name="team")
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    try:
        controls = team_background_jobs(DelegationModel(id="leader"), config, paths, team)
        assert isinstance(controls, JobTools)
        job = controls.async_functions["job"]
        assert job.requires_confirmation is not True
        assert job.approval_type is None
    finally:
        await runtime.shutdown()


def test_a_plugin_toolkit_named_job_still_asks_for_approval(tmp_path: Path) -> None:
    """Only the reserved job controls skip approval; a same-named plugin toolkit does not."""
    config = managed_team_config(tmp_path)
    config.tool_approval.default = "require_approval"

    def run_job() -> str:
        return "ran"

    toolkit = apply_tool_approval_capability(
        Toolkit(name="job", tools=[run_job]),
        config,
        supports_native_tool_approval=True,
        registered_tool_name="job",
    )
    assert toolkit is not None
    assert toolkit.get_functions()["run_job"].requires_confirmation is True
