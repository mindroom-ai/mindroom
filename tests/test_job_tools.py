"""Shared job controls preserve exact ownership and stored results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team
from agno.tools.function import ToolResult

from mindroom.agent_storage import create_session_storage
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.results import encode_tool_result
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import bind_runtime_paths
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput


@pytest.mark.asyncio
async def test_job_wait_waits_and_restores_rich_result(tmp_path: Path) -> None:
    """Job wait waits and restores rich result."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    gate = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await gate.wait()
        return BackgroundOutcome(
            "completed",
            "answer",
            result_payload={"value": encode_tool_result(ToolResult(content="answer", metadata={"proof": 1}))},
        )

    tools = JobTools(paths, owner)
    try:
        await runtime.start(JobSpec("ordinary", "slow", 0), owner=owner, operation=operation)
        with tool_runtime_context(context):
            pending = asyncio.create_task(tools.job("wait", "ordinary"))
            await asyncio.sleep(0)
            assert not pending.done()
            gate.set()
            result = await pending
            assert isinstance(result, ToolResult)
            assert result.metadata == {"proof": 1}
            assert result.content == "answer"
            assert json.loads(await tools.job("list"))[0]["job_id"] == "ordinary"
            assert set(tools.get_async_functions()) == {"job"}
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("delegate", [False, True])
async def test_managed_agent_has_one_job_schema(tmp_path: Path, delegate: bool) -> None:
    """Managed agent has one job schema."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"] if delegate else [])},
        models={"default": {"provider": "openai", "id": "gpt-6-astra"}},
        memory={"backend": "none"},
        defaults={"tools": []},
    )
    bind_runtime_paths(config, runtime_paths=paths)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    try:
        agent = create_agent("leader", config, paths, execution_identity=owner, persist_runtime_state=False)
        names = {name for toolkit in agent.tools for name in toolkit.get_async_functions()}
        assert "job" in names
        assert not {"inspect_subagent", "wait_subagent", "resume_subagent", "cancel_subagent"} & names
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_only_native_job_wait_projects_external_approval(tmp_path: Path) -> None:
    """Only native job wait projects external approval."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    try:
        await runtime.start(JobSpec("native", "delegate", 0, kind="delegation"), owner=owner, operation=operation)
        waited = await runtime.wait("native", owner=owner, depth=0)
        await runtime.release_wait("native", waited.token)
        model = DelegationModel(
            id="test",
            responses=[ModelResponse(tool_calls=[_call("job", "wait", action="wait", job_id="native")])],
        )
        install_tool_job_execution(model)
        agent = Agent(id="leader", model=model, tools=[JobTools(paths, owner)])
        with tool_runtime_context(context):
            response = await agent.arun("wait", session_id=context.session_id)
        assert response.requirements
        assert response.requirements[0].needs_external_execution
        assert response.requirements[0].tool_execution.approval_type == "mindroom_job_wait"
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_job_list_rediscovers_restart_outcomes_with_current_scope(tmp_path: Path, *, native: bool) -> None:
    """A new turn rediscovers saved results while foreign requesters cannot list them."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved " * 1000)

    spec = JobSpec("durable", "tool", 0)
    if native:
        spec = replace(spec, kind="delegation", adapter={"child": {"subagent_id": "reusable-child", "result": None}})
    await runtime.start(spec, owner=owner, operation=operation)
    waited = await runtime.wait("durable", owner=owner, depth=0)
    await runtime.acknowledge_wait("durable", waited.token)
    await runtime.shutdown()
    allowed = True
    runtime = ToolJobRuntime(tmp_path, authorize=lambda _: allowed)
    await runtime.recover()
    register_background_runtime(paths, runtime)
    tools = JobTools(paths, owner)
    try:
        with tool_runtime_context(context):
            summary = json.loads(await tools.job("list"))[0]
            assert summary["job_id"] == "durable"
            assert summary["summary_truncated"]
            assert summary.get("subagent_id") == ("reusable-child" if native else None)
            assert json.loads(await tools.job("inspect", "durable")) == summary
        for foreign in (
            replace(context, requester_id="@foreign:example.org"),
            replace(context, agent_name="other"),
            replace(context, target=replace(context.target, room_id="!other:example.org")),
        ):
            with tool_runtime_context(foreign):
                assert json.loads(await tools.job("list")) == []
        allowed = False
        with tool_runtime_context(context):
            assert json.loads(await tools.job("list")) == []
            assert "not available" in await tools.job("inspect", "durable")
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_team_routes_member_discovery_and_consumption_on_new_turn(tmp_path: Path) -> None:
    """A real Team driver routes list and wait back through the job's owning member."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = replace(_delegate_runtime_context(config, paths), agent_name="squad", transport_agent_name="squad")
    owner = replace(build_execution_identity_from_runtime_context(context), agent_name="leader")
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved member answer")

    storage = storage_factory()
    try:
        await runtime.start(JobSpec("member-job", "slow", 0), owner=owner, operation=operation)
        waited = await runtime.wait("member-job", owner=owner, depth=0)
        await runtime.release_wait("member-job", waited.token)
        member_model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("job", "list", action="list")]),
                ModelResponse(tool_calls=[_call("job", "wait", action="wait", job_id="member-job")]),
                ModelResponse(content="Member consumed result"),
            ],
        )
        team_model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(
                    tool_calls=[
                        _call(
                            "delegate_task_to_member",
                            "route",
                            member_id="leader",
                            task="Find jobs and retrieve result",
                        ),
                    ],
                ),
                ModelResponse(content="Team done"),
            ],
        )
        install_tool_job_execution(member_model)
        install_tool_job_execution(team_model)
        member = Agent(id="leader", model=member_model, tools=[JobTools(paths, owner)], db=storage)
        team = Team(id="squad", model=team_model, members=[member], db=storage)

        @owned_tool_execution
        async def run() -> TeamRunOutput:
            set_consumption_storage(storage_factory)
            return await team.arun("Follow up", session_id=context.session_id, user_id=context.requester_id)

        with tool_runtime_context(context):
            response = await run()
        results = response.member_responses[0].tools
        assert json.loads(results[0].result)[0]["job_id"] == "member-job"
        assert results[1].result == "saved member answer"
        assert (await runtime.lookup("member-job", owner=owner, depth=0)).wait_acknowledged
    finally:
        storage.close()
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "stored_error", "expected_error"),
    [
        ("failed", "controlled HTTP 500 failure", True),
        ("completed", None, False),
    ],
)
async def test_job_wait_replays_sdk_failure_and_acknowledges_saved_result(
    tmp_path: Path,
    status: Literal["completed", "failed"],
    stored_error: str | None,
    expected_error: bool,
) -> None:
    """The reserved Agno control retains failure state while consuming exact saved evidence."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome(
            status,
            "None",
            result_payload={
                "value": encode_tool_result(None),
                "state_delta": encode_tool_result({}),
                "error": stored_error,
                "elapsed": 0.1,
                "events": encode_tool_result([]),
                "replay": encode_tool_result([]),
                "control": None,
            },
        )

    storage = storage_factory()
    try:
        await runtime.start(JobSpec("ordinary", "slow", 0), owner=owner, operation=operation)
        waited = await runtime.wait("ordinary", owner=owner, depth=0)
        await runtime.release_wait("ordinary", waited.token)
        model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("job", "wait", action="wait", job_id="ordinary")]),
                ModelResponse(content="done"),
            ],
        )
        install_tool_job_execution(model)
        agent = Agent(id="leader", model=model, tools=[JobTools(paths, owner)], db=storage)

        @owned_tool_execution
        async def run() -> RunOutput:
            set_consumption_storage(storage_factory)
            return await agent.arun("wait", session_id=context.session_id, user_id=context.requester_id)

        with tool_runtime_context(context):
            response = await run()

        tool = response.tools[0]
        assert tool.tool_call_error is expected_error
        assert tool.result == (stored_error if expected_error else "None")
        assert (await runtime.lookup("ordinary", owner=owner, depth=0)).wait_acknowledged
    finally:
        storage.close()
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tool", "delegation"])
async def test_discovery_bounds_large_results_without_truncating_wait(
    tmp_path: Path,
    kind: Literal["tool", "delegation"],
) -> None:
    """Discovering large ordinary and native outcomes cannot flood context or discard their saved result."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    result = "large result " * 100_000

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome(
            "completed",
            result,
            result_payload={"value": encode_tool_result(result)} if kind == "tool" else None,
        )

    try:
        adapter = {"child": {"result": None}} if kind == "delegation" else {}
        await runtime.start(
            JobSpec("large", "large_tool", 0, kind=kind, adapter=adapter),
            owner=owner,
            operation=operation,
        )
        waited = await runtime.wait("large", owner=owner, depth=0)
        await runtime.release_wait("large", waited.token)
        with tool_runtime_context(context):
            tools = JobTools(paths, owner)
            discovery = await tools.job("list")
            assert len(discovery) < 2_000
            summary = json.loads(discovery)[0]
            assert summary["job_id"] == "large"
            assert summary["summary_truncated"] is True
            assert result.startswith(summary["summary"])
            assert await tools.job("wait", "large") == result
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_job_wait_can_return_immediately_without_cancelling(tmp_path: Path) -> None:
    """Management waits share the timeout contract, while work remains discoverable."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)
    gate = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await gate.wait()
        return BackgroundOutcome("completed", "answer")

    tools = JobTools(paths, owner)
    try:
        await runtime.start(JobSpec("ordinary", "slow", 0), owner=owner, operation=operation)
        with tool_runtime_context(context):
            result = await tools.job("wait", "ordinary", wait_timeout=0)
            assert json.loads(result)["status"] == "running"
            gate.set()
            assert await tools.job("wait", "ordinary", wait_timeout=None) == "answer"
    finally:
        gate.set()
        register_background_runtime(paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("save_fails", [False, True])
async def test_cancel_acknowledges_only_saved_management_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    save_fails: bool,
) -> None:
    """A saved cancellation receipt suppresses completion; failed parent saves remain discoverable."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(tmp_path)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        return BackgroundOutcome("completed", "unreachable")

    storage = storage_factory()
    if save_fails:

        def fail_save(*_args: object, **_kwargs: object) -> None:
            msg = "storage unavailable"
            raise RuntimeError(msg)

        monkeypatch.setattr(type(storage), "upsert_run", fail_save)
    try:
        await runtime.start(JobSpec("cancelled", "slow", 0), owner=owner, operation=operation)
        model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("job", "cancel", action="cancel", job_id="cancelled")]),
                ModelResponse(content="stopped"),
            ],
        )
        install_tool_job_execution(model)
        agent = Agent(id="leader", model=model, tools=[JobTools(paths, owner)], db=storage)

        @owned_tool_execution
        async def run() -> RunOutput:
            set_consumption_storage(storage_factory)
            return await agent.arun("cancel", session_id=context.session_id, user_id=context.requester_id)

        with tool_runtime_context(context):
            response = await run()
        assert json.loads(response.tools[0].result)["status"] == "cancelled"
        job = await runtime.lookup("cancelled", owner=owner, depth=0)
        assert job.wait_acknowledged is not save_fails
    finally:
        storage.close()
        register_background_runtime(paths, None)
        await runtime.shutdown()
