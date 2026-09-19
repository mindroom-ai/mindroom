"""Delegation policy approvals retain their exact call's waiting owner across restart."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.team import Team

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability, build_agent_toolkit
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.state import DelegationState
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import bind_toolkit_authority
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_jobs.settings import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.delegation.state import DelegationChild


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_excluded", [False, True])
@pytest.mark.parametrize("team", [False, True])
async def test_delegate_policy_approval_keeps_wait_owner_after_restart(  # noqa: PLR0915
    tmp_path: Path,
    initially_excluded: bool,
    team: bool,
) -> None:
    """An approval before child admission preserves its owning agent/member run's mode."""
    paths = _runtime_paths(tmp_path)
    config = with_responder_access(
        Config(
            background_tool_jobs=BackgroundToolJobsConfig(
                enabled=True,
                exclude_toolkits=["delegate"] if initially_excluded else [],
            ),
            agents={
                "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
                "code": AgentConfig(display_name="Code"),
            },
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
            tool_approval={"default": "require_approval"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    pin_background_tool_jobs(config, paths)
    storage = create_session_storage("leader", config, paths, owner)
    executed: list[str] = []
    child_done = asyncio.Event()

    async def run_child(child: DelegationChild, **_kwargs: object) -> str:
        executed.append(child.task)
        child_done.set()
        return "Child done"

    def build_parent(*, resumed: bool) -> Agent | Team:
        toolkit = build_agent_toolkit(
            "delegate",
            agent_name="leader",
            config=config,
            runtime_paths=paths,
            worker_tools=[],
            runtime_overrides=None,
            execution_identity=owner,
            session_id=owner.session_id,
        )
        assert toolkit is not None
        bind_toolkit_authority(toolkit, authored_name="delegate")
        apply_tool_approval_capability(
            toolkit,
            config,
            supports_native_tool_approval=True,
            registered_tool_name="delegate",
        )
        arguments = {"agent_name": "code", "task": "Research"}
        if not initially_excluded:
            arguments["wait_timeout"] = 0
        model = DelegationModel(
            id="test",
            responses=([] if resumed else [ModelResponse(tool_calls=[_call("run_subagent", "approval", **arguments)])])
            + [ModelResponse(content="Member done")],
        )
        install_tool_job_execution(model)
        member = Agent(id="leader", name="leader", model=model, db=storage, tools=[toolkit], telemetry=False)
        if not team:
            return member
        team_model = DelegationModel(
            id="test",
            responses=(
                []
                if resumed
                else [
                    ModelResponse(
                        tool_calls=[_call("delegate_task_to_member", "approval", member_id="leader", task="Delegate")],
                    ),
                ]
            )
            + [ModelResponse(content="Team done")],
        )
        install_tool_job_execution(team_model)
        return Team(id="squad", model=team_model, members=[member], db=storage, telemetry=False)

    try:
        async with execution_resources():
            with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)):
                parent = build_parent(resumed=False)
                paused = await parent.arun("Delegate", session_id=owner.session_id, user_id=owner.requester_id)
                options = {
                    "run_child": run_child,
                    "agent_name": "leader",
                    "config": config,
                    "runtime_paths": paths,
                    "execution_identity": owner,
                    "member_config_names": {"leader": "leader"},
                }
                paused = await drive_delegations(parent, paused, **options)
                assert paused.status == RunStatus.paused
                state = DelegationState.from_metadata(paused.metadata)
                assert not state.children
                assert not executed
                saved = (TeamRunOutput if team else RunOutput).from_dict(paused.to_dict())
                await runtime.shutdown()
                release_background_tool_jobs(paths)
                config.background_tool_jobs.exclude_toolkits = [] if initially_excluded else ["delegate"]
                pin_background_tool_jobs(config, paths)
                runtime = ToolJobRuntime(tmp_path)
                await runtime.recover()
                register_background_runtime(paths, runtime)
                call_id = state.pending_tools[0]["tool_call_id"]
                response = await drive_delegations(
                    build_parent(resumed=True),
                    saved,
                    **options,
                    decisions={call_id: True},
                    denial_reasons={call_id: None},
                )
                assert response.status == RunStatus.completed
                await asyncio.wait_for(child_done.wait(), 2)
                assert executed == ["Research"]
                jobs = await runtime.list_jobs(owner=owner, depth=0)
                assert len(jobs) == (0 if initially_excluded else 1)
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()
        release_background_tool_jobs(paths)
        storage.close()
