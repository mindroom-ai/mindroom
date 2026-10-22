"""Shared job controls preserve exact ownership and stored results."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

import pytest

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.background import delegation_outcome
from mindroom.event_journal import ToolJobStore
from mindroom.llm.media import Image
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs import consumption as consumption_module
from mindroom.tool_jobs.consumption import ConsumptionOwner, consumption_context, set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.results import ToolResultPayload, encode_result_payload
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.agno_toolkits import Toolkit, ToolValue
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import bind_runtime_paths
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    call_job_tool,
    completed_delegation_job,
    job_actor,
    lookup,
    pending_outcome,
    pending_outcomes,
    run_job_turn,
    start_job,
    tool_job_runtime,
    toolkit_job_tools,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_jobs.execution import JobActor
    from mindroom.tool_jobs.runtime import BackgroundJob, EncodedResultPayload, ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _controls(paths: RuntimePaths, owner: ToolExecutionIdentity, *, team: bool = False) -> JobActor:
    """The actor whose only tool is its job controls, as an agent or a team leader offers them."""
    tools = toolkit_job_tools(JobTools(paths, owner), authored_name="job")
    return job_actor(*tools.values(), name=owner.recipient if team else owner.agent_name, team=team)


_NO_SUCH_JOB = (
    "No job with this ID is available to you in this conversation. The ID may be wrong or incomplete, the job may "
    "have been deleted after its retention period, it may belong to another conversation or caller, or your access "
    'to it may have changed. Call job(action="list") to see the jobs you can access.'
)


@pytest.mark.asyncio
async def test_enabling_jobs_reserves_the_function_name_job(tmp_path: Path) -> None:
    """A configured tool with its own `job` function would shadow the job controls, so the agent refuses it."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def job() -> str:
        return "a plugin's job"

    try:
        with pytest.raises(ValueError, match="reserved"):
            JobTools.build([Toolkit(name="ci", tools=[job])], paths, owner, depth=0, enabled=True)
    finally:
        await runtime.shutdown()


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
                ToolResultPayload(value=ToolValue(content="answer", images=(Image(content=b"proof"),))),
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
            assert isinstance(result, ToolValue)
            assert [image.content for image in result.images] == [b"proof"]
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
        build = create_agent("leader", config, paths, execution_identity=owner, persist_runtime_state=False)
        names = [function.name for toolkit in build.spec.toolkits for function in toolkit.functions]
        assert names.count("job") == 1
        assert build.tool_jobs is not None
        assert "job" in build.tool_jobs.tools
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_job_list_rediscovers_restart_outcomes_with_current_scope(tmp_path: Path, *, native: bool) -> None:
    """A new turn rediscovers saved results while foreign callers can neither list nor reach them.

    A foreign or revoked job reads exactly like an ID that never existed, so a caller never learns that it exists.
    """
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
                assert await tools.job("wait", "durable") == _NO_SUCH_JOB
                assert await tools.job("cancel", "durable") == _NO_SUCH_JOB
        allowed = False
        with tool_runtime_context(context):
            assert json.loads(await tools.job("list")) == []
            assert await tools.job("wait", "durable") == _NO_SUCH_JOB
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_kind", ["team", "agent"])
async def test_a_team_leaders_wait_is_read_once_its_team_run_saves_the_call(tmp_path: Path, saved_kind: str) -> None:
    """A team leader's job wait counts as read only when the team's own run saved the call, never an agent's."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = replace(_delegate_runtime_context(config, paths), agent_name="squad", transport_agent_name="squad")
    owner = replace(build_execution_identity_from_runtime_context(context), agent_name="leader")
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    store = create_session_storage("leader", config, paths, owner)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved member answer")

    try:
        await start_job(runtime, "member-job", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("member-job", owner=owner, depth=0)
        await runtime.release_wait("member-job", waited.claim)
        claims = ConsumptionOwner(storage_factory=lambda: store)
        async with execution_resources():
            with tool_runtime_context(context), consumption_context(claims):
                outcome = await call_job_tool(
                    _controls(paths, owner, team=True),
                    "job",
                    {"action": "wait", "job_id": "member-job"},
                    call_id="wait-call",
                    run_id="team-run",
                    session_id=context.session_id,
                )
        assert outcome.content == "saved member answer"
        saved_call = ToolExecution(tool_call_id="wait-call", tool_name="job", result=outcome.content)
        run = (
            TeamRunRecord(run_id="team-run", session_id=context.session_id, team_id="squad", tools=[saved_call])
            if saved_kind == "team"
            else RunRecord(run_id="team-run", session_id=context.session_id, agent_id="squad", tools=[saved_call])
        )
        session = ConversationSession(session_id=context.session_id, kind=saved_kind, owner_id="squad")
        store.save_run(session, replace(run, status=RunStatus.completed), record_usage=False)
        await claims.finalize()
        assert (await lookup(runtime, "member-job", owner=owner, depth=0)).consumed is (saved_kind == "team")
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "value", "stored_error", "expected_result"),
    [
        ("failed", None, "controlled HTTP 500 failure", "controlled HTTP 500 failure"),
        ("failed", ToolValue(content="rich failure"), None, "rich failure"),
        ("completed", None, None, "None"),
    ],
)
async def test_job_wait_reports_a_failure_and_acknowledges_the_saved_result(
    tmp_path: Path,
    status: Literal["completed", "failed"],
    value: ToolValue | None,
    stored_error: str | None,
    expected_result: str,
) -> None:
    """The job controls report a failed job as the call's failure while consuming exact saved evidence."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", config, paths, owner)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome(
            status,
            "None",
            result_payload=encode_result_payload(ToolResultPayload(value=value, error=stored_error)),
        )

    storage = storage_factory()
    try:
        await start_job(runtime, "ordinary", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("ordinary", owner=owner, depth=0)
        await runtime.release_wait("ordinary", waited.claim)
        model = ScriptedModel([calls(tool_call("job", "wait", action="wait", job_id="ordinary")), answer("done")])

        @owned_tool_execution()
        async def run() -> RunRecord:
            set_consumption_storage(storage_factory)
            return await run_job_turn(
                _controls(paths, owner),
                model,
                "wait",
                session_id=context.session_id,
                store=storage,
            )

        with tool_runtime_context(context):
            response = await run()

        tool = response.tools[0]
        assert tool.tool_call_error is (status == "failed")
        assert tool.result == expected_result
        assert (await lookup(runtime, "ordinary", owner=owner, depth=0)).consumed
    finally:
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
async def test_job_wait_on_a_cancelled_job_says_it_was_cancelled(tmp_path: Path) -> None:
    """Waiting on a job a person cancelled returns why it has no result, so the model does not run it again."""
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    tools = JobTools(paths, owner)
    try:
        await start_job(runtime, "ordinary", tool_name="slow", depth=0, adapter={}, owner=owner, operation=operation)
        await runtime.request_cancel("ordinary")
        with tool_runtime_context(context):
            result = await tools.job("wait", "ordinary", wait_timeout=None)
        assert "cancelled" in str(result)
        assert "do not call it again" in str(result)
    finally:
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
                assert await waiting == _NO_SUCH_JOB
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
        model = ScriptedModel(
            [calls(tool_call("job", "cancel", action="cancel", job_id="cancelled")), answer("stopped")],
        )
        with tool_runtime_context(context):
            response = await run_job_turn(_controls(paths, owner), model, "cancel", session_id=context.session_id)
        assert json.loads(str(response.tools[0].result))["status"] == "cancel_requested"
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
        ("wait", {"job_id": "missing"}, _NO_SUCH_JOB),
        ("cancel", {"job_id": "missing"}, _NO_SUCH_JOB),
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
) -> tuple[RunRecord, ToolJobRuntime, JobTools, ToolExecutionIdentity, object]:
    """Start jobs with the given outcomes (``None`` keeps one running), then collect them in one saved agent run."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> ConversationStore:
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
    model = ScriptedModel([calls(tool_call("job", "collect", action="collect")), answer("done")])
    tools = JobTools(paths, owner)
    actor = job_actor(*toolkit_job_tools(tools, authored_name="job").values())

    @owned_tool_execution()
    async def run() -> RunRecord:
        set_consumption_storage(storage_factory)
        return await run_job_turn(actor, model, "collect", session_id=context.session_id, store=storage)

    with tool_runtime_context(context):
        response = await run()
    return response, runtime, tools, owner, (storage, context)


def _payload(text: str, *, error: str | None = None) -> EncodedResultPayload:
    return encode_result_payload(ToolResultPayload(value=text, error=error))


@pytest.mark.asyncio
async def test_collect_returns_every_ended_job_once(tmp_path: Path) -> None:
    """One collect returns every ended job's result, and acknowledging them makes a second collect find none."""
    response, runtime, tools, owner, (_storage, context) = await _collect_in_a_run(
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
            msg = "Unsupported durable tool result payload."
            raise ValueError(msg)
        return await original(runtime, job)

    monkeypatch.setattr(consumption_module, "read_result_payload", read)
    response, runtime, _tools, owner, (_storage, _context) = await _collect_in_a_run(
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
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_collect_acknowledges_a_cancelled_job_without_a_result(tmp_path: Path) -> None:
    """A cancelled job is reported as cancelled and acknowledged, though it has no result."""
    response, runtime, _tools, owner, (_storage, _context) = await _collect_in_a_run(
        tmp_path,
        {"job_a": BackgroundOutcome("cancelled")},
    )
    try:
        result = str(response.tools[0].result)
        assert "`job_a`" in result
        assert "cancelled" in result
        assert (await lookup(runtime, "job_a", owner=owner, depth=0)).consumed
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_collect_leaves_running_jobs_alone(tmp_path: Path) -> None:
    """A job still running is neither returned nor claimed."""
    response, runtime, _tools, owner, (_storage, _context) = await _collect_in_a_run(tmp_path, {"running": None})
    try:
        assert response.tools[0].result == "No ended background jobs."
        job = await lookup(runtime, "running", owner=owner, depth=0)
        assert (job.status, job.consumed) == ("running", False)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_collect_returns_a_background_subagents_answer(tmp_path: Path) -> None:
    """A finished background subagent's answer is collected like any other job's result."""
    response, runtime, _tools, _owner, (_storage, _context) = await _collect_in_a_run(
        tmp_path,
        {"child": delegation_outcome("completed", "the child's answer")},
        kind="delegation",
    )
    try:
        assert "the child's answer" in str(response.tools[0].result)
    finally:
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
