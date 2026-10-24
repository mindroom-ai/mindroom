"""Response turns whose background tool results outlive the reply that started them."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.control import pending_human_messages_context
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundJob, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    job_actor,
    job_tool,
    lookup,
    pending_outcomes,
    run_job_turn,
    tool_job_runtime,
    toolkit_job_tools,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.runs import RunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@pytest.mark.asyncio
async def test_consumed_results_leave_no_receipts_in_session_state(tmp_path: Path) -> None:
    """An agent's saved tool result is the consumption evidence, so session state never grows."""
    executions: list[str] = []

    async def report(topic: str) -> str:
        executions.append(topic)
        return f"report on {topic}"

    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", config, paths, owner)

    topics = ["alpha", "beta", "gamma"]
    storage = storage_factory()
    actor = job_actor(job_tool(report))

    @owned_tool_execution()
    async def run_turn(topic: str) -> RunRecord:
        set_consumption_storage(storage_factory)
        model = ScriptedModel(
            [
                calls(tool_call("report", f"{topic}-call", topic=topic, background_after=None)),
                answer(f"The {topic} report is ready."),
            ],
        )
        return await run_job_turn(
            actor,
            model,
            f"Report on {topic}",
            run_id=f"{topic}-run",
            session_id=context.session_id,
            store=storage,
        )

    try:
        with (
            tool_runtime_context(context),
            patch.object(runtime, "acknowledge_wait", wraps=runtime.acknowledge_wait) as acknowledge,
        ):
            runs = [await run_turn(topic) for topic in topics]
        session = storage.load_session(context.session_id, "agent")
        assert session is not None
        assert not session.session_state
        assert len(storage.list_runs(context.session_id)) == len(topics)
        assert executions == topics
        assert [run.tools[0].result for run in runs if run.tools] == [f"report on {topic}" for topic in topics]
        jobs = runtime.retained_jobs()
        assert len(jobs) == len(topics)
        assert all(job.consumed for job in jobs)
        # Each call returned its outcome as its own result, so none of them became a job to list.
        with tool_runtime_context(context):
            assert json.loads(await JobTools(paths, owner).job("list")) == []
        assert sorted(call.args[0] for call in acknowledge.await_args_list) == sorted(job.job_id for job in jobs)
        assert pending_outcomes(runtime) == []
    finally:
        await runtime.shutdown()


async def _wait_until_ready(
    runtime: ToolJobRuntime,
    job_id: str,
    *,
    owner: ToolExecutionIdentity,
) -> BackgroundJob:
    while True:
        job = await lookup(runtime, job_id, owner=owner, depth=0)
        if job.status not in {"running", "cancel_requested"}:
            return job
        runtime.changed.clear()
        job = await lookup(runtime, job_id, owner=owner, depth=0)
        if job.status not in {"running", "cancel_requested"}:
            return job
        await asyncio.wait_for(runtime.changed.wait(), JOB_TEST_TIMEOUT)


@pytest.mark.asyncio
async def test_human_released_job_is_rediscovered_and_consumed_in_newer_turn(  # noqa: PLR0915 - Real two-turn lifecycle.
    tmp_path: Path,
) -> None:
    """A newer turn lists and retrieves the original side effect exactly once."""
    started, release = asyncio.Event(), asyncio.Event()
    list_observed, allow_wait = asyncio.Event(), asyncio.Event()
    executions = 0

    async def slow_tool() -> str:
        nonlocal executions
        executions += 1
        started.set()
        await release.wait()
        return "durable report"

    async def hold_after_listing() -> None:
        list_observed.set()
        await allow_wait.wait()

    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(config, paths)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal = asyncio.Event()

    def storage_factory() -> ConversationStore:
        return create_session_storage("leader", config, paths, owner)

    model = ScriptedModel(
        [
            calls(tool_call("slow_tool", "original-call", background_after=None)),
            answer("The original turn was released."),
        ],
    )
    storage = storage_factory()
    controls = toolkit_job_tools(JobTools(paths, owner), authored_name="job").values()
    actor = job_actor(job_tool(slow_tool), *controls)

    @owned_tool_execution()
    async def run_turn(prompt: str, run_id: str) -> RunRecord:
        set_consumption_storage(storage_factory)
        return await run_job_turn(actor, model, prompt, run_id=run_id, session_id=context.session_id, store=storage)

    first_pending = None
    second_pending = None
    try:
        with tool_runtime_context(context), pending_human_messages_context(signal):
            first_pending = asyncio.create_task(run_turn("Start the report", "first"))
            await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
            signal.set()
            first = await asyncio.wait_for(first_pending, JOB_TEST_TIMEOUT)
        assert first.tools is not None
        job_id = json.loads(str(first.tools[0].result))["job_id"]
        assert "released" in str(first.content)
        assert (await lookup(runtime, job_id, owner=owner, depth=0)).status == "running"

        signal.clear()
        model.turns.extend(
            [
                calls(tool_call("job", "list-call", action="list")),
                [
                    Await(hold_after_listing),
                    *calls(tool_call("job", "wait-call", action="wait", job_id=job_id, wait_timeout=0)),
                ],
                answer("The retained report was retrieved."),
            ],
        )
        with tool_runtime_context(context), pending_human_messages_context(signal):
            second_pending = asyncio.create_task(run_turn("Check the earlier report", "second"))
            await asyncio.wait_for(list_observed.wait(), JOB_TEST_TIMEOUT)
            assert (await lookup(runtime, job_id, owner=owner, depth=0)).status == "running"
            assert job_id in str(model.requests[-1].messages[-1].content)
            release.set()
            ready = await _wait_until_ready(runtime, job_id, owner=owner)
            assert ready.status == "completed"
            allow_wait.set()
            second = await asyncio.wait_for(second_pending, JOB_TEST_TIMEOUT)

        assert second.tools is not None
        list_result = next(tool.result for tool in second.tools if tool.tool_call_id == "list-call")
        wait_result = next(tool.result for tool in second.tools if tool.tool_call_id == "wait-call")
        assert job_id in str(list_result)
        assert wait_result == "durable report"
        [read] = [
            message
            for message in model.requests[-1].messages
            if message.role == "tool" and message.tool_call_id == "wait-call"
        ]
        assert read.get_content_string() == "durable report"
        assert executions == 1
        assert (await lookup(runtime, job_id, owner=owner, depth=0)).consumed
        assert pending_outcomes(runtime) == []
    finally:
        release.set()
        allow_wait.set()
        for pending in (first_pending, second_pending):
            if pending is not None and not pending.done():
                pending.cancel()
            if pending is not None:
                await asyncio.gather(pending, return_exceptions=True)
        await runtime.shutdown()
