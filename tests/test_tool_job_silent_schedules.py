"""Background continuations retain silent scheduling's existing delivery policy."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse

from mindroom.delegation.background import delegation_child, start_delegation
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.response_runner import _is_silent_schedule_response, _with_silent_schedule_delivery
from mindroom.scheduled_run_records import record_silent_schedule_result_if_needed
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.completion import completion_envelope, join_conversation_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.turn_origin import TurnIntent
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_subagent_runtime import _job

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_recovered_silent_schedule_retains_guidance_and_receipt(tmp_path: Path) -> None:
    """Recovery remains runtime-owned without turning a quiet check into visible progress."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target(thread_id="$thread"))
    envelope = replace(
        request.response_envelope,
        origin=replace(
            request.response_envelope.origin,
            source_kind=SILENT_SCHEDULE_SOURCE_KIND,
            intent=TurnIntent.SCHEDULED_FIRE,
        ),
    )
    request = replace(request, response_envelope=envelope)
    context = runner.deps.tool_runtime.build_context(
        envelope.target,
        user_id=envelope.requester_id,
        source_envelope=envelope,
    )
    assert context is not None
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(bot.runtime_paths.storage_root)
    register_background_runtime(bot.runtime_paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("interrupted", "Interrupted; not replayed")

    try:
        await runtime.start(
            JobSpec("quiet", "tool", 0, adapter={"source_event_id": envelope.source_event_id}),
            owner=owner,
            operation=operation,
        )
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        await runtime.release_wait("quiet", waited.token)
        recovered = await runner._recover_tool_job_source(request)
        assert _is_silent_schedule_response(recovered)
        assert recovered.response_envelope.origin.intent is TurnIntent.TOOL_JOB_COMPLETION
        assert "NO_REPLY" in _with_silent_schedule_delivery((), recovered.response_envelope)[0].text
        await record_silent_schedule_result_if_needed(
            entity_name="general",
            agent_names=("general",),
            envelope=recovered.response_envelope,
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            suppression_reason="silent_no_report",
            response_text="NO_REPLY",
        )
        receipts = list(bot.runtime_paths.storage_root.glob("agents/general/workspace/.mindroom/scheduled_runs/*.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text())["result"] == "no_report"
    finally:
        register_background_runtime(bot.runtime_paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("silent", [False, True])
async def test_automatic_join_keeps_quiet_and_visible_results_separate(tmp_path: Path, *, silent: bool) -> None:
    """Automatic joining cannot publish a quiet result or silence an ordinary one."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    envelope = _plain_request(_target(thread_id="$thread")).response_envelope
    if silent:
        envelope = replace(envelope, origin=replace(envelope.origin, source_kind=SILENT_SCHEDULE_SOURCE_KIND))
    context = runner.deps.tool_runtime.build_context(
        envelope.target,
        user_id=envelope.requester_id,
        source_envelope=envelope,
    )
    assert context is not None
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(bot.runtime_paths.storage_root)
    register_background_runtime(bot.runtime_paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "result")

    try:
        for name, kind in (("quiet", SILENT_SCHEDULE_SOURCE_KIND), ("visible", "message")):
            await runtime.start(
                JobSpec(name, "tool", 0, adapter={"source_kind": kind}),
                owner=owner,
                operation=operation,
            )
            waited = await runtime.wait(name, owner=owner, depth=0)
            await runtime.release_wait(name, waited.token)
        with tool_runtime_context(context):
            joined = [item async for item in join_conversation_jobs(set())]
        assert len(joined) == 1
        assert not isinstance(joined[0], str)
        assert f'job_id="{"quiet" if silent else "visible"}"' in joined[0].prompt
        assert f'job_id="{"visible" if silent else "quiet"}"' not in joined[0].prompt
    finally:
        register_background_runtime(bot.runtime_paths, None)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_accepted_job_persists_silent_completion_policy_across_restart(tmp_path: Path, *, native: bool) -> None:
    """SDK and native delegated calls carry quiet delivery into their restored completion envelope."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    envelope = _plain_request(_target(thread_id="$thread")).response_envelope
    envelope = replace(envelope, origin=replace(envelope.origin, source_kind=SILENT_SCHEDULE_SOURCE_KIND))
    context = runner.deps.tool_runtime.build_context(
        envelope.target,
        user_id=envelope.requester_id,
        source_envelope=envelope,
    )
    assert context is not None
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(bot.runtime_paths.storage_root)
    register_background_runtime(bot.runtime_paths, runtime)
    release = asyncio.Event()

    async def slow() -> str:
        await release.wait()
        return "No findings"

    async def native_operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", await slow())

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("slow", "call", wait_timeout=0)]),
            ModelResponse(content="NO_REPLY"),
        ],
    )
    install_tool_job_execution(model)
    agent = Agent(id="general", model=model, tools=[slow])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                if native:
                    child = replace(delegation_child(_job()), caller_agent_name="general")
                    accepted = await start_delegation(runtime, child, owner=owner, operation=native_operation)
                    job_id = accepted.job_id
                else:
                    response = await agent.arun("Check quietly", session_id=owner.session_id)
                    job_id = json.loads(response.tools[0].result)["job_id"]
            release.set()
            waited = await runtime.wait(job_id, owner=owner, depth=0)
            await runtime.release_wait(job_id, waited.token)
        await runtime.shutdown()
        runtime = ToolJobRuntime(bot.runtime_paths.storage_root)
        await runtime.recover()
        restored = await runtime.outcome(job_id, 0)
        assert restored is not None
        completed = completion_envelope(restored, sender_id=bot.matrix_id.full_id)
        assert completed.source_kind == SILENT_SCHEDULE_SOURCE_KIND
        assert completed.origin.intent is TurnIntent.TOOL_JOB_COMPLETION
        assert "NO_REPLY" in _with_silent_schedule_delivery((), completed)[0].text
    finally:
        release.set()
        register_background_runtime(bot.runtime_paths, None)
        await runtime.shutdown()
