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

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.background import delegation_outcome
from mindroom.event_journal import ToolJobStore
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs import consumption as consumption_module
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobAccessError, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import bind_runtime_paths
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.tool_job_helpers import (
    completed_delegation_job,
    lookup,
    pending_outcome,
    pending_outcomes,
    start_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput

    from mindroom.tool_jobs.runtime import BackgroundJob, EncodedResultPayload, ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@pytest.mark.asyncio
async def test_job_wait_waits_and_restores_rich_result(tmp_path: Path) -> None:
    """Job wait waits and restores rich result."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    gate = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await gate.wait()
        return BackgroundOutcome(
            "completed",
            "answer",
            result_payload=encode_result_payload(
                ToolResultPayload(value=ToolResult(content="answer", metadata={"proof": 1})),
            ),
        )

    tools = JobTools(paths, owner)
    try:
        await start_job(runtime, "ordinary", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    try:
        agent = create_agent("leader", config, paths, execution_identity=owner, persist_runtime_state=False)
        names = [name for toolkit in agent.tools for name in toolkit.get_async_functions()]
        assert names.count("job") == 1
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_job_list_rediscovers_restart_outcomes_with_current_scope(tmp_path: Path, *, native: bool) -> None:
    """A new turn rediscovers saved results while foreign requesters cannot list them."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved " * 1000)

    await start_job(
        runtime,
        "durable",
        tool_name="tool",
        depth=0,
        kind="delegation" if native else "tool",
        adapter={"child": {"subagent_id": "reusable-child", "result": None}} if native else {},
        owner=owner,
        operation=operation,
    )
    waited = await runtime.wait("durable", owner=owner, depth=0)
    await runtime.acknowledge_wait("durable", waited.claim)
    await runtime.shutdown()
    allowed = True
    runtime = await tool_job_runtime(tmp_path, authorize=lambda _: allowed)
    await runtime.recover()
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    tools = JobTools(paths, owner)
    try:
        with tool_runtime_context(context):
            summary = json.loads(await tools.job("list"))[0]
            assert summary["job_id"] == "durable"
            assert summary["summary_truncated"]
            assert summary.get("subagent_id") == ("reusable-child" if native else None)
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
            assert "not available" in await tools.job("wait", "durable")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_team_routes_member_discovery_and_consumption_on_new_turn(tmp_path: Path) -> None:
    """A real Team driver routes list and wait back through the job's owning member."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = replace(_delegate_runtime_context(config, paths), agent_name="squad", transport_agent_name="squad")
    owner = replace(build_execution_identity_from_runtime_context(context), agent_name="leader")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved member answer")

    storage = storage_factory()
    try:
        await start_job(runtime, "member-job", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("member-job", owner=owner, depth=0)
        await runtime.release_wait("member-job", waited.claim)
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

        @owned_tool_execution()
        async def run() -> TeamRunOutput:
            set_consumption_storage(storage_factory)
            return await team.arun("Follow up", session_id=context.session_id, user_id=context.requester_id)

        with tool_runtime_context(context):
            response = await run()
        results = response.member_responses[0].tools
        assert json.loads(results[0].result)[0]["job_id"] == "member-job"
        assert results[1].result == "saved member answer"
        assert (await lookup(runtime, "member-job", owner=owner, depth=0)).consumed
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "value", "stored_error", "expected_result"),
    [
        ("failed", None, "controlled HTTP 500 failure", "controlled HTTP 500 failure"),
        ("failed", ToolResult(content="rich failure"), None, "rich failure"),
        ("completed", None, None, "None"),
    ],
)
async def test_job_wait_replays_sdk_failure_and_acknowledges_saved_result(
    tmp_path: Path,
    status: Literal["completed", "failed"],
    value: ToolResult | None,
    stored_error: str | None,
    expected_result: str,
) -> None:
    """The reserved Agno control retains failure state while consuming exact saved evidence."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome(
            status,
            "None",
            result_payload=encode_result_payload(ToolResultPayload(value=value, error=stored_error, elapsed=0.1)),
        )

    storage = storage_factory()
    try:
        await start_job(runtime, "ordinary", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("ordinary", owner=owner, depth=0)
        await runtime.release_wait("ordinary", waited.claim)
        model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("job", "wait", action="wait", job_id="ordinary")]),
                ModelResponse(content="done"),
            ],
        )
        install_tool_job_execution(model)
        agent = Agent(id="leader", model=model, tools=[JobTools(paths, owner)], db=storage)

        @owned_tool_execution()
        async def run() -> RunOutput:
            set_consumption_storage(storage_factory)
            return await agent.arun("wait", session_id=context.session_id, user_id=context.requester_id)

        with tool_runtime_context(context):
            response = await run()

        tool = response.tools[0]
        assert tool.tool_call_error is (status == "failed")
        assert tool.result == expected_result
        assert (await lookup(runtime, "ordinary", owner=owner, depth=0)).consumed
    finally:
        storage.close()
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
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    result = "large result " * 100_000

    async def operation() -> BackgroundOutcome:
        if kind == "delegation":
            return delegation_outcome("completed", result)
        return BackgroundOutcome("completed", result, result_payload=encode_result_payload(ToolResultPayload(result)))

    try:
        adapter = {"child": {"result": None}} if kind == "delegation" else {}
        await start_job(
            runtime,
            "large",
            tool_name="large_tool",
            depth=0,
            kind=kind,
            adapter=adapter,
            owner=owner,
            operation=operation,
        )
        waited = await runtime.wait("large", owner=owner, depth=0)
        await runtime.release_wait("large", waited.claim)
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
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_job_wait_can_return_immediately_without_cancelling(tmp_path: Path) -> None:
    """Management waits share the timeout contract, while work remains discoverable."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    gate = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await gate.wait()
        return BackgroundOutcome("completed", "answer")

    tools = JobTools(paths, owner)
    try:
        await start_job(runtime, "ordinary", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
        with tool_runtime_context(context):
            result = await tools.job("wait", "ordinary", wait_timeout=0)
            assert json.loads(result)["status"] == "running"
            gate.set()
            assert await tools.job("wait", "ordinary", wait_timeout=None) == "answer"
    finally:
        gate.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["cancelled", "missing"])
async def test_interrupted_payload_read_releases_the_wait_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    """A job wait whose payload read is cancelled or finds the payload gone leaves the result claimable."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    reading, release = asyncio.Event(), asyncio.Event()
    read_payload = runtime.read_payload

    async def gated_read(job: BackgroundJob) -> EncodedResultPayload:
        reading.set()
        await release.wait()
        return await read_payload(job)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved", result_payload=encode_result_payload(ToolResultPayload("saved")))

    tools = JobTools(paths, owner)
    try:
        await start_job(runtime, "read", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
        monkeypatch.setattr(runtime, "read_payload", gated_read)
        with tool_runtime_context(context):
            waiting = asyncio.create_task(tools.job("wait", "read"))
            await reading.wait()
            if interruption == "cancelled":
                waiting.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:

                async def missing(_store: ToolJobStore, _job_id: str) -> None:
                    return None

                monkeypatch.setattr(ToolJobStore, "load_payload", missing)
                release.set()
                assert await waiting == "Tool job is not available in this conversation."
        retried = await runtime.wait("read", owner=owner, depth=0, timeout=0)
        assert retried.claim is not None
        await runtime.release_wait("read", retried.claim)
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_cancel_returns_at_once_and_leaves_the_outcome_to_the_notice(tmp_path: Path) -> None:
    """The model's cancel returns the request; the job's cancelled outcome stays for the next notice to report."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        return BackgroundOutcome("completed", "unreachable")

    try:
        await start_job(runtime, "cancelled", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
        model = DelegationModel(
            id="test",
            responses=[
                ModelResponse(tool_calls=[_call("job", "cancel", action="cancel", job_id="cancelled")]),
                ModelResponse(content="stopped"),
            ],
        )
        install_tool_job_execution(model)
        agent = Agent(id="leader", model=model, tools=[JobTools(paths, owner)])
        with tool_runtime_context(context):
            response = await agent.arun("cancel", session_id=context.session_id, user_id=context.requester_id)
        assert json.loads(response.tools[0].result)["status"] == "cancel_requested"
        await wait_for_status(runtime, "cancelled", "cancelled")
        assert not (await lookup(runtime, "cancelled", owner=owner, depth=0)).consumed
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "arguments", "message"),
    [
        ("wait", {}, "job_id is required"),
        ("list", {"limit": 0}, "limit"),
        ("wait", {"job_id": "missing", "wait_timeout": -1}, "wait_timeout"),
        ("cancel", {"job_id": "missing"}, "not available"),
    ],
)
async def test_job_errors_return_as_tool_results(
    tmp_path: Path,
    action: str,
    arguments: dict[str, object],
    message: str,
) -> None:
    """Every expected mistake comes back as a message the model can act on, never a traceback."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    try:
        with tool_runtime_context(context):
            result = await JobTools(paths, build_execution_identity_from_runtime_context(context)).job(
                action,
                **arguments,
            )
        assert message in result
    finally:
        await runtime.shutdown()


async def _collect_in_a_run(
    tmp_path: Path,
    outcomes: dict[str, BackgroundOutcome | None],
    *,
    kind: str = "tool",
) -> tuple[RunOutput, ToolJobRuntime, JobTools, ToolExecutionIdentity, object]:
    """Start jobs with the given outcomes (``None`` keeps one running), then collect them in one saved agent run."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> BaseDb:
        return create_session_storage("leader", config, paths, owner)

    for job_id, outcome in outcomes.items():

        async def operation(outcome: BackgroundOutcome | None = outcome) -> BackgroundOutcome:
            if outcome is None:
                await asyncio.Event().wait()
                raise AssertionError
            return outcome

        await start_job(
            runtime,
            job_id,
            tool_name=f"tool_{job_id}",
            depth=0,
            kind=kind,
            adapter={},
            owner=owner,
            operation=operation,
        )
        if outcome is not None:
            await wait_for_status(runtime, job_id, outcome.status)
    storage = storage_factory()
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("job", "collect", action="collect")]),
            ModelResponse(content="done"),
        ],
    )
    install_tool_job_execution(model)
    tools = JobTools(paths, owner)
    agent = Agent(id="leader", model=model, tools=[tools], db=storage)

    @owned_tool_execution()
    async def run() -> RunOutput:
        set_consumption_storage(storage_factory)
        return await agent.arun("collect", session_id=context.session_id, user_id=context.requester_id)

    with tool_runtime_context(context):
        response = await run()
    return response, runtime, tools, owner, (storage, context)


def _payload(text: str, *, error: str | None = None) -> EncodedResultPayload:
    return encode_result_payload(ToolResultPayload(value=text, error=error))


@pytest.mark.asyncio
async def test_collect_returns_every_ended_job_once(tmp_path: Path) -> None:
    """One collect returns every ended job's result, and acknowledging them makes a second collect find none."""
    response, runtime, tools, owner, (storage, context) = await _collect_in_a_run(
        tmp_path,
        {
            "job_a": BackgroundOutcome("completed", "result a", result_payload=_payload("result a")),
            "job_b": BackgroundOutcome("failed", "broke", result_payload=_payload("", error="boom")),
        },
    )
    try:
        result = str(response.tools[0].result)
        assert "`job_a`" in result
        assert "result a" in result
        assert "`job_b`" in result
        assert "boom" in result
        for job_id in ("job_a", "job_b"):
            assert (await lookup(runtime, job_id, owner=owner, depth=0)).consumed
        with tool_runtime_context(context):
            assert await tools.job("collect") == "No ended background jobs."
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_collect_reports_the_others_when_one_result_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A result that cannot be read stays unacknowledged for a later collect, and the others still arrive."""
    original = consumption_module.read_result_payload

    async def read(runtime: ToolJobRuntime, job: BackgroundJob) -> ToolResultPayload:
        if job.job_id == "job_b":
            msg = "Tool job result is not available."
            raise JobAccessError(msg)
        return await original(runtime, job)

    monkeypatch.setattr(consumption_module, "read_result_payload", read)
    response, runtime, _tools, owner, (storage, _context) = await _collect_in_a_run(
        tmp_path,
        {
            "job_a": BackgroundOutcome("completed", "result a", result_payload=_payload("result a")),
            "job_b": BackgroundOutcome("completed", "result b", result_payload=_payload("result b")),
        },
    )
    try:
        result = str(response.tools[0].result)
        assert "result a" in result
        assert "`job_b` (tool_job_b) completed: its result could not be read now" in result
        assert (await lookup(runtime, "job_a", owner=owner, depth=0)).consumed
        assert not (await lookup(runtime, "job_b", owner=owner, depth=0)).consumed
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_collect_acknowledges_a_cancelled_job_without_a_result(tmp_path: Path) -> None:
    """A cancelled job is reported as cancelled and acknowledged, though it has no result."""
    response, runtime, _tools, owner, (storage, _context) = await _collect_in_a_run(
        tmp_path,
        {"job_a": BackgroundOutcome("cancelled")},
    )
    try:
        result = str(response.tools[0].result)
        assert "`job_a`" in result
        assert "cancelled" in result
        assert (await lookup(runtime, "job_a", owner=owner, depth=0)).consumed
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_collect_leaves_running_jobs_alone(tmp_path: Path) -> None:
    """A job still running is neither returned nor claimed."""
    response, runtime, _tools, owner, (storage, _context) = await _collect_in_a_run(tmp_path, {"running": None})
    try:
        assert response.tools[0].result == "No ended background jobs."
        job = await lookup(runtime, "running", owner=owner, depth=0)
        assert (job.status, job.consumed) == ("running", False)
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_collect_returns_a_background_subagents_answer(tmp_path: Path) -> None:
    """A finished background subagent's answer is collected like any other job's result."""
    response, runtime, _tools, _owner, (storage, _context) = await _collect_in_a_run(
        tmp_path,
        {"child": delegation_outcome("completed", "the child's answer")},
        kind="delegation",
    )
    try:
        assert "the child's answer" in str(response.tools[0].result)
    finally:
        storage.close()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_pending_outcomes_require_saved_consumption(tmp_path: Path) -> None:
    """Transient ready-result claims cannot hide an unsaved outcome after release."""
    runtime = await tool_job_runtime(tmp_path)
    owner = completed_delegation_job().owner

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("failed", "tool failed")

    try:
        await start_job(runtime, "quiet", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        assert not pending_outcomes(runtime)
        await runtime.release_wait("quiet", waited.claim)
        released = pending_outcome(runtime, "quiet")
        assert released is not None
        assert released.result == "tool failed"
        assert [job.job_id for job in pending_outcomes(runtime)] == ["quiet"]
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        await runtime.acknowledge_wait("quiet", waited.claim)
        assert pending_outcome(runtime, "quiet") is None
        assert not pending_outcomes(runtime)
    finally:
        await runtime.shutdown()
