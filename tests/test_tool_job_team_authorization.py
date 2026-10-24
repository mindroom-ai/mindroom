"""Managed tools retain the requester grants of configured and ad hoc teams."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.agents import team_background_jobs
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig, ModelConfig
from mindroom.delegation.background import delegation_child
from mindroom.delegation.lifecycle import child_run_context, prepare_child_turn, start_child_turn
from mindroom.delegation.state import PersonaSourceKind, SubagentPersona
from mindroom.llm.messages import Message
from mindroom.loop_hooks import HookTurn, leader_loop_hooks
from mindroom.teams.members import _team_member, build_team_spec, materialize_exact_team_members, team_history_settings
from mindroom.teams.spec import TeamMode
from mindroom.tool_jobs.authorization import authority_snapshot
from mindroom.tool_jobs.conversation import current_conversation
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.notices import JobNotices, job_notice_context
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import (
    BackgroundOutcome,
    JobAccessError,
    register_background_runtime,
)
from mindroom.tool_system.agno_toolkits import Function, Toolkit, adapt_function
from mindroom.tool_system.call_record import recording_tool_calls
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    serialize_tool_execution_identity,
    tool_execution_identity,
)
from tests.conftest import bind_runtime_paths, install_model_providers
from tests.delegation_helpers import _delegate_runtime_context
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.history_helpers import _runtime_paths as history_runtime_paths
from tests.tool_approval_helpers import gated_functions
from tests.tool_job_helpers import (
    SpanRecorder,
    completed_delegation_job,
    job_actor,
    managed_team_config,
    pending_outcome,
    pending_outcomes,
    run_job_turn,
    saved_jobs,
    start_delegation_job,
    start_job,
    team_coordinator,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.llm.runs import RunRecord
    from mindroom.tool_jobs.execution import JobActor

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


def _calculator_member(config: Config, paths: RuntimePaths) -> JobActor:
    """The worker as a team runs it, granted by its own snapshot, whose one call adds 2 and 3."""
    toolkit = get_tool_by_name("calculator", paths, worker_target=None, disable_sandbox_proxy=True)
    tools = toolkit_job_tools(toolkit, authored_name="calculator", snapshot=authority_snapshot(config, "worker"))
    return job_actor(*tools.values(), name="worker")


async def _add(member: JobActor, session_id: str) -> RunRecord:
    model = ScriptedModel([calls(tool_call("add", "addition", a=2, b=3)), answer("done")])
    return await run_job_turn(member, model, "Calculate", session_id=session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_team_member_tool_uses_actual_actor_and_current_requester_grants(
    tmp_path: Path,
    configured: bool,
) -> None:
    """A member's tool becomes a job of that member until the grant is withdrawn.

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
    member = _calculator_member(config, paths)
    try:
        await coordinator.sync()
        async with execution_resources():
            with tool_runtime_context(context), tool_execution_identity(owner):
                await _add(member, owner.session_id or "session")
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
    member = _calculator_member(config, paths)
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )

    async def operation() -> BackgroundOutcome:
        async with child_run_context(child, config=config, runtime_paths=paths):
            with tool_execution_identity(child_owner):
                response = await _add(member, child.session_id)
                assert response.tools is not None
                assert not response.tools[0].is_error
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
        jobs = team_background_jobs(config, paths, team)
        assert jobs is not None
        [job] = jobs.controls.functions
        with tool_runtime_context(context):
            outcome = await execute_tool_call(
                job,
                {"action": "collect"},
                ToolCallContext(run_id="team-run", session_id=context.session_id, call_id="collect-call"),
                middleware=jobs.middleware,
            )
        assert "`member` (report) completed" in outcome.content
        assert "Member result" in outcome.content
    finally:
        await runtime.shutdown()


async def delegate_task_to_member(member_id: str, task: str) -> str:
    """Stand in for the leader's delegation tool."""
    return f"{member_id}: {task}"


@pytest.mark.asyncio
async def test_only_a_team_leaders_job_calls_go_through_its_tool_hooks(tmp_path: Path) -> None:
    """A team leader's job calls run through the tool-call policy; its delegations skip it, as with jobs off."""
    config = managed_team_config(tmp_path)
    paths = team_coordinator(tmp_path, config).runtime_paths
    team = replace(completed_delegation_job().owner, agent_name="team", transport_agent_name="team")
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=team),
        agent_name="team",
        transport_agent_name="team",
    )
    recorder = SpanRecorder(stopped=False)
    try:
        jobs = team_background_jobs(config, paths, team)
        assert jobs is not None
        [job] = jobs.controls.functions
        delegation = adapt_function(Function.from_callable(delegate_task_to_member))
        call_context = ToolCallContext(run_id="team-run", session_id=context.session_id)
        with tool_runtime_context(context), recording_tool_calls(recorder):
            await execute_tool_call(job, {"action": "list"}, call_context, middleware=jobs.middleware)
            delegated = await execute_tool_call(
                delegation,
                {"member_id": "worker", "task": "Add"},
                call_context,
                middleware=jobs.middleware,
            )
        assert delegated.content == "worker: Add"
        assert [(kind, name) for kind, name, _arguments, _result in recorder.records] == [
            ("started", "job"),
            ("finished", "job"),
        ]
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
        jobs = team_background_jobs(config, paths, team)
        assert jobs is not None
        [job] = jobs.controls.functions
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

    functions = gated_functions(Toolkit(name="job", tools=[run_job]), config, registered_tool_name="job")
    assert functions["run_job"].requires_confirmation is True


@pytest.mark.asyncio
async def test_a_team_built_with_jobs_on_gets_its_leaders_job_controls_and_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leader of a configured team can collect its members' jobs and hears when they end."""
    runtime_paths = history_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": AgentConfig(display_name="Alpha"), "zeta": AgentConfig(display_name="Zeta")},
            teams={"pair": TeamConfig(display_name="Pair", role="Test team", agents=["alpha", "zeta"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=2_000)},
            background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        ),
        runtime_paths,
    )
    runtime = await tool_job_runtime(tmp_path / "jobs")
    pin_background_tool_jobs(config, runtime_paths)
    register_background_runtime(runtime_paths, runtime)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="pair",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
        transport_agent_name="pair",
    )
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    context = replace(
        _delegate_runtime_context(config, runtime_paths, execution_identity=identity),
        agent_name="pair",
        transport_agent_name="pair",
    )

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Member result")

    try:
        members = materialize_exact_team_members(
            ["alpha", "zeta"],
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
        )
        built = build_team_spec(
            members=members,
            mode=TeamMode.COORDINATE,
            config=config,
            runtime_paths=runtime_paths,
            team_display_name="Pair",
            scope_context=None,
            execution_identity=identity,
            model_name="default",
            configured_team_name="pair",
        )
        assert any(function.name == "job" for toolkit in built.spec.toolkits for function in toolkit.functions)
        assert built.leader_jobs is not None
        assert built.leader_jobs.team
        hooks = leader_loop_hooks(
            HookTurn(
                config=config,
                runtime_paths=runtime_paths,
                model_name="default",
                storage=None,
                replay_plan=None,
                prompt_id=None,
            ),
            entity_name="pair",
            scope_id=built.spec.id,
            model=built.spec.model,
            history_settings=team_history_settings(config, "pair"),
            tool_middleware=built.leader_middleware,
            jobs=built.leader_jobs,
        )
        await start_job(runtime, "ended", tool_name="report", depth=0, adapter={}, owner=identity, operation=operation)
        await wait_for_status(runtime, "ended", "completed")
        messages = [Message(role="tool", tool_call_id="call", content="delegated")]
        with tool_runtime_context(context), job_notice_context(JobNotices()):
            for hook in hooks.after_tool_batch:
                await hook(messages, messages)
        assert "`ended` (report) completed" in str(messages[0].content)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_the_member_that_posts_an_ad_hoc_teams_replies_hears_when_its_jobs_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ad hoc team answers as the member whose account posts its replies, so only that member hears the notice."""
    runtime_paths = history_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"alpha": AgentConfig(display_name="Alpha"), "zeta": AgentConfig(display_name="Zeta")},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model", context_window=2_000)},
            background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        ),
        runtime_paths,
    )
    runtime = await tool_job_runtime(tmp_path / "jobs")
    pin_background_tool_jobs(config, runtime_paths)
    register_background_runtime(runtime_paths, runtime)
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="alpha",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-1",
        transport_agent_name="alpha",
    )
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    context = replace(
        _delegate_runtime_context(config, runtime_paths, execution_identity=identity),
        agent_name="zeta",
        transport_agent_name="alpha",
    )

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Member result")

    try:
        members = materialize_exact_team_members(
            ["alpha", "zeta"],
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=identity,
        )
        hooks = {
            build.spec.id: _team_member(
                build,
                "",
                config=config,
                runtime_paths=runtime_paths,
                model_name="default",
            ).hooks("member-run")
            for build in members.builds
        }
        await start_job(runtime, "ended", tool_name="report", depth=0, adapter={}, owner=identity, operation=operation)
        await wait_for_status(runtime, "ended", "completed")
        heard = {}
        with tool_runtime_context(context), job_notice_context(JobNotices()):
            for name in ("zeta", "alpha"):
                messages = [Message(role="tool", tool_call_id="call", content="result")]
                for hook in hooks[name].after_tool_batch:
                    await hook(messages, messages)
                heard[name] = str(messages[0].content)
        assert heard["zeta"] == "result"
        assert "`ended` (report) completed" in heard["alpha"]
    finally:
        await runtime.shutdown()
