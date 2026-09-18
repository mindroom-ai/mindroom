"""Managed tools retain the requester grants of configured and ad hoc teams."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team

from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.config.access import ResponderAccessConfig
from mindroom.delegation.background import delegation_child, start_delegation
from mindroom.delegation.lifecycle import child_run_context, start_child_turn
from mindroom.entity_resolution import entity_identity_registry
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import AUTHORITY_METADATA_KEY, authority_snapshot, bind_toolkit_authority
from mindroom.tool_jobs.completion import completion_event, join_conversation_jobs
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity, tool_execution_identity
from tests.conftest import unwrap_extracted_collaborator
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot
from tests.test_delegate_tools import _delegate_runtime_context
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_subagent_runtime import _config, _delivery_coordinator, _job

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID
    from mindroom.response_runner import ResponseRequest

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def _calculator_agent(config: Config, paths: RuntimePaths) -> Agent:
    toolkit = get_tool_by_name("calculator", paths, worker_target=None, disable_sandbox_proxy=True)
    bind_toolkit_authority(toolkit, authored_name="calculator")
    model = DelegationModel(
        id="test",
        responses=[ModelResponse(tool_calls=[_call("add", "addition", a=2, b=3)]), ModelResponse(content="done")],
    )
    install_tool_job_execution(model)
    return Agent(
        id="worker",
        name="Worker",
        model=model,
        tools=[toolkit],
        metadata={AUTHORITY_METADATA_KEY: authority_snapshot(config, "worker")},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_team_member_tool_uses_actual_actor_and_current_requester_grants(
    tmp_path: Path,
    configured: bool,
) -> None:
    """A non-transport team member can execute tools, until its requester grant is withdrawn."""
    config = _config(tmp_path)
    config.agents["worker"].tools = ["calculator"]
    config.teams["team"].agents = ["lead", "worker"]
    coordinator = _delivery_coordinator(tmp_path, config)
    paths = coordinator.runtime_paths
    owner = replace(_job().owner, transport_agent_name="team" if configured else "lead")
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
                waited = await coordinator.runtime.wait(jobs[0].job_id, owner=member_owner, depth=0)
                assert waited.job.status == "completed", waited.job.result
                assert json.loads(waited.job.result)["result"] == 5
                assert await coordinator.runtime.list_jobs(owner=owner, depth=0) == []
                config.agents["worker"].access = ResponderAccessConfig(current_room_members=False)
                assert await coordinator.runtime.list_jobs(owner=member_owner, depth=0) == []
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_idle_ad_hoc_completion_reconstructs_member_for_exact_result(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member result finishing after its parent turn is consumed by the resumed SDK member."""
    bot = _bot(tmp_path)
    config, paths = bot.config, bot.runtime_paths
    config.background_tool_jobs = True
    config.memory.backend = "none"
    config.defaults.tools = []
    config.agents["general"].learning = False
    config.agents["worker"] = config.agents["general"].model_copy(deep=True)
    persist_entity_accounts(config, paths)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    owner = replace(_job().owner, agent_name="worker", transport_agent_name="general")
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    release = asyncio.Event()
    results = []

    async def operation() -> BackgroundOutcome:
        await release.wait()
        return BackgroundOutcome("completed", "Exact member report")

    @owned_tool_execution
    async def respond(
        request: ResponseRequest,
        *,
        team_agents: list[MatrixID] | None = None,
        **_kwargs: object,
    ) -> None:
        registry = entity_identity_registry(config, paths)
        names = (
            [name for name, matrix_id in registry.current_ids.items() if matrix_id in team_agents]
            if team_agents
            else ["general"]
        )
        actors = []
        storage = create_session_storage("general", config, paths, owner)
        set_consumption_storage(lambda: create_session_storage("general", config, paths, owner))
        for name in names:
            model = DelegationModel(
                id="test",
                responses=[
                    ModelResponse(
                        tool_calls=[_call("job", "retrieve", action="wait", job_id="late-member", wait_timeout=0)],
                    ),
                    ModelResponse(content="Retrieved"),
                ],
            )
            monkeypatch.setattr(
                "mindroom.agents.model_loading.get_model_instance",
                lambda *_args, model=model, **_kwargs: model,
            )
            actors.append(
                create_agent(
                    name,
                    config,
                    paths,
                    execution_identity=replace(owner, agent_name="general"),
                    session_id=owner.session_id,
                    history_storage=storage,
                    include_interactive_questions=False,
                ),
            )
        context = replace(
            _delegate_runtime_context(config, paths, execution_identity=owner),
            agent_name="general",
            transport_agent_name="general",
        )
        try:
            with tool_runtime_context(context):
                for actor in actors:
                    result = await actor.arun(request.prompt, session_id=owner.session_id, user_id=owner.requester_id)
                    assert result.tools is not None
                    results.extend(tool.result for tool in result.tools)
            await runner.deps.approval_store.settle(event.event_id)
        finally:
            storage.close()

    monkeypatch.setattr(runner, "generate_response", respond)
    monkeypatch.setattr(runner, "generate_team_response_helper", respond)
    try:
        await runtime.start(JobSpec("late-member", "report", 0), owner=owner, operation=operation)
        # The launching response is gone before its accepted operation completes.
        release.set()
        waited = await runtime.wait("late-member", owner=owner, depth=0)
        await runtime.release_wait("late-member", waited.token)
        event = completion_event(waited.job, sender_id=bot.matrix_id.full_id)
        await runner.deps.approval_store.admit(event)
        admitted = await runner.deps.approval_store.load_event(event.event_id)
        assert admitted is not None
        await runner._resume_tool_job_completion(admitted, "late-member", 0)
        assert results == ["Exact member report"]
        assert await runtime.pending_outcomes() == []
    finally:
        release.set()
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_single_agent_turn_leaves_other_member_jobs_for_their_completion_owner(tmp_path: Path) -> None:
    """An ordinary later turn must not receive an unusable other-member retrieval directive."""
    config = _config(tmp_path)
    paths = _delivery_coordinator(tmp_path, config).runtime_paths
    owner = replace(_job().owner, agent_name="worker", transport_agent_name="lead")
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name="lead",
        transport_agent_name="lead",
    )

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Member result")

    try:
        await runtime.start(JobSpec("member", "report", 0), owner=owner, operation=operation)
        waited = await runtime.wait("member", owner=owner, depth=0)
        await runtime.release_wait("member", waited.token)
        with tool_runtime_context(context):
            assert [item async for item in join_conversation_jobs(set())] == []
            joined = [item async for item in join_conversation_jobs(set(), agent_names=("lead", "worker"))]
            assert len(joined) == 1
            assert not isinstance(joined[0], str)
            assert 'job_id="member"' in joined[0].prompt
        assert await runtime.outcome("member", 0) is not None
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_ad_hoc_member_native_delegation_retains_transport_and_ancestry(tmp_path: Path) -> None:
    """Native child tools and durable results keep an ordinary agent's ad hoc team transport."""
    config = _config(tmp_path)
    config.agents["carrier"] = config.agents["lead"].model_copy(deep=True)
    config.agents["worker"].tools = ["calculator"]
    coordinator = _delivery_coordinator(tmp_path, config)
    paths = coordinator.runtime_paths
    owner = replace(_job().owner, transport_agent_name="carrier")
    child = delegation_child(_job())
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
                job = await start_delegation(coordinator.runtime, child, owner=owner, operation=operation)
                waited = await coordinator.runtime.wait(job.job_id, owner=owner, depth=0)
                assert waited.job.status == "completed", waited.job.result
                assert json.loads(waited.job.result)["result"] == 5
                config.agents["lead"].delegate_to = []
                assert await coordinator.runtime.list_jobs(owner=owner, depth=0) == []
    finally:
        await coordinator.stop()
