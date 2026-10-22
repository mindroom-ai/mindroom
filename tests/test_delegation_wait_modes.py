"""Delegation policy approvals retain their exact call's waiting owner across restart."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.delegation.state import DelegationState
from mindroom.llm.runs import RunStatus
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import (
    ScriptedAgents,
    _runtime_paths,
    _saved_approval_calls,
    approval_executor,
    respond,
    respond_team,
    resume,
    resume_team,
    stored_run,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.tool_job_helpers import JOB_TEST_TIMEOUT, saved_jobs, tool_job_runtime, wait_for_status

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("initially_excluded", [False, True])
@pytest.mark.parametrize("team", [False, True])
async def test_delegate_policy_approval_follows_exclusions_current_at_resume(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initially_excluded: bool,
    team: bool,
) -> None:
    """An approval before child admission follows the waiting policy of the instance that resumes it."""
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
            teams={"squad": TeamConfig(display_name="Squad", role="Delegates.", agents=["leader"], mode="coordinate")},
            defaults=DefaultsConfig(tools=[]),
            memory={"backend": "none"},
            tool_approval={"default": "require_approval"},
        ),
        "code",
        users=["@alice:example.org"],
    )
    entity_ids(config, paths)
    owner = ToolExecutionIdentity("matrix", "leader", "@alice:example.org", "!room:example.org", None, None, "parent")
    runtime = await tool_job_runtime(tmp_path)
    instance = pin_background_tool_jobs(config, paths)
    register_background_runtime(paths, runtime)
    arguments: dict[str, object] = {"agent_name": "code", "task": "Research"}
    if not initially_excluded:
        arguments["background_after"] = 0
    member = ScriptedModel([calls(tool_call("run_subagent", "approval", **arguments)), answer("Member done")])
    code = ScriptedModel([answer("Child done")])
    leader = ScriptedModel(
        [calls(tool_call("delegate_task_to_member", "member", member_id="leader", task="Delegate")), answer("Done")],
    )
    ScriptedAgents({"code": code}, by_session={"parent": member}, team=leader).install(monkeypatch)
    entity = "squad" if team else "leader"
    options = {"config": config, "runtime_paths": paths, "identity": owner}
    try:
        async with execution_resources():
            paused = (
                await respond_team("squad", "Delegate", **options, run_id="parent-run")
                if team
                else await respond("leader", "Delegate", **options, run_id="parent-run")
            )
            assert isinstance(paused, PausedAttempt)
            stored = stored_run(entity, "parent-run", **options)
            assert stored.status is RunStatus.paused
            state = DelegationState.from_metadata(stored.metadata)
            assert not state.children
            assert code.requests == []
            await runtime.shutdown()
            release_background_tool_jobs(paths, instance)
            config.background_tool_jobs.exclude_toolkits = [] if initially_excluded else ["delegate"]
            instance = pin_background_tool_jobs(config, paths)
            runtime = await tool_job_runtime(tmp_path)
            await runtime.recover()
            register_background_runtime(paths, runtime)
            approvals = _saved_approval_calls(state)
            decisions = {call.tool_call_id: True for call in approvals}
            result = (
                await resume_team("squad", paused, approvals, **options, decisions=decisions)
                if team
                else await resume(
                    approval_executor(config, paths),
                    paused,
                    approvals,
                    agent_name="leader",
                    **options,
                    decisions=decisions,
                )
            )
            assert isinstance(result, CompletedApprovalRun)
            assert stored_run(entity, "parent-run", **options).status is RunStatus.completed
            # The finished reply shows every call it made as finished.
            assert "⏳" not in result.response_text
            jobs = await saved_jobs(paths.storage_root)
            if initially_excluded:
                # Now managed: the approved child runs as a job.
                [job] = jobs.values()
                assert job.kind == "delegation"
                await asyncio.wait_for(wait_for_status(runtime, job.job_id, "completed"), JOB_TEST_TIMEOUT)
                assert len(code.requests) == 1
            else:
                # Now excluded: the approved call's wait budget is rejected, so the child never starts.
                assert code.requests == []
                assert jobs == {}
    finally:
        await runtime.shutdown()
        release_background_tool_jobs(paths, instance)
