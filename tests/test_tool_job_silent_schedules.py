"""Background jobs keep silent scheduling's delivery policy."""

from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from agno.agent import Agent
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.models.response import ModelResponse

from mindroom.ai import (
    _AgentRunContext,
    _collect_response_body_with_trace,
    _PreparedAgentRun,
    ai_response,
    stream_agent_response,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.background import delegation_child
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope, PreparedHistoryState
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.conversation import ConversationKey, current_conversation
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.conftest import make_turn_context, unwrap_extracted_collaborator
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.tool_job_helpers import (
    assembled_function,
    completed_delegation_job,
    pending_outcome,
    pending_outcomes,
    start_delegation_job,
    start_job,
    tool_job_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_system.events import ToolTraceEntry


@dataclass
class _SilentTurn:
    answer: str
    trace: list[ToolTraceEntry]
    recorder: TurnRecorder | None
    pending_outcomes: list[object]


async def _run_silent_turn(
    tmp_path: Path,
    responses: list[ModelResponse],
    *,
    enabled: bool,
    collect_stream: bool,
    record_turn: bool = True,
    direct_stream: bool = False,
    hold_probe: bool = False,
) -> _SilentTurn:
    """Run one silent scheduled agent turn, through `ai_response` or the streaming entry a Matrix reply calls.

    ``hold_probe`` keeps the probe tool running until the runtime shuts down, so a released call returns its handle.
    """
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=enabled),
        agents={"leader": AgentConfig(display_name="Leader")},
    )
    paths = _runtime_paths(tmp_path)
    context = replace(_delegate_runtime_context(config, paths), source_kind=SILENT_SCHEDULE_SOURCE_KIND)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    database = str(tmp_path / "silent.db")
    storage = SqliteDb(db_file=database)

    async def probe_tool() -> str:
        """Read evidence using the ordinary disabled execution path."""
        if hold_probe:
            await asyncio.Event().wait()
        return "Job evidence"

    model = DelegationModel(id="test", responses=responses)
    install_tool_job_execution(model)
    actor = Agent(
        id="leader",
        model=model,
        tools=[assembled_function(probe_tool), JobTools(paths, owner)] if enabled else [probe_tool],
        db=storage,
        telemetry=False,
    )
    scope = ScopeSessionContext(
        HistoryScope(kind="agent", scope_id="leader"),
        storage,
        None,
        session_id=context.session_id,
        storage_factory=lambda: SqliteDb(db_file=database),
    )
    ctx = replace(
        make_turn_context(
            entity_label="leader",
            session_id=context.session_id,
            room_id=owner.room_id,
            thread_id=owner.resolved_thread_id,
            requester_id=owner.requester_id,
        ),
        allow_no_report_response=True,
    )
    trace: list[ToolTraceEntry] = []
    recorder = TurnRecorder(user_message="Silent check") if record_turn else None

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "Job evidence")

    async def prepare(turn: ResponseTurnContext, **kwargs: object) -> _AgentRunContext:
        prompt = str(kwargs["prompt"])
        prepared = _PreparedAgentRun(
            agent=actor,
            messages=(Message(role="user", content=prompt),),
            unseen_event_ids=[],
            prepared_history=PreparedHistoryState(),
            runtime_model_name="default",
        )
        return _AgentRunContext(
            turn=turn,
            session_id=context.session_id,
            prompt=prompt,
            model_prompt=None,
            prepared_run=prepared,
            run_input=prepared.run_input,
            metadata=turn.matrix_run_metadata,
        )

    try:
        with (
            tool_runtime_context(context),
            patch("mindroom.ai.open_resolved_scope_session_context", return_value=nullcontext(scope)),
            patch("mindroom.ai._prepare_agent_run_context", new=prepare),
        ):
            if direct_stream:
                answer = await _collect_response_body_with_trace(
                    stream_agent_response(
                        ctx,
                        prompt="Silent check",
                        runtime_paths=paths,
                        config=config,
                        execution_identity=owner,
                        show_tool_calls=True,
                        turn_recorder=recorder,
                    ),
                    show_tool_calls=True,
                    tool_trace_collector=trace,
                )
            else:
                answer = await ai_response(
                    ctx,
                    prompt="Silent check",
                    runtime_paths=paths,
                    config=config,
                    execution_identity=owner,
                    collect_streamed_response=collect_stream,
                    show_tool_calls=True,
                    tool_trace_collector=trace,
                    turn_recorder=recorder,
                )
        return _SilentTurn(answer, trace, recorder, pending_outcomes(runtime))
    finally:
        await runtime.shutdown()
        storage.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("collect_stream", [False, True])
@pytest.mark.parametrize("final", ["NO_REPLY", "Finding", ""])
@pytest.mark.parametrize("use_tool", [False, True])
async def test_enabled_silent_turn_without_jobs_matches_a_disabled_turn(
    tmp_path: Path,
    *,
    collect_stream: bool,
    final: str,
    use_tool: bool,
) -> None:
    """Enabling background jobs changes nothing for a quiet turn that starts none."""
    tool_calls = [ModelResponse(tool_calls=[_call("probe_tool", "read")])] if use_tool else []
    observed = []
    for enabled in (False, True):
        turn = await _run_silent_turn(
            tmp_path / str(enabled),
            [*tool_calls, ModelResponse(content=final)],
            enabled=enabled,
            collect_stream=collect_stream,
        )
        assert turn.recorder is not None
        observed.append(
            (
                turn.answer,
                [(tool.tool_name, tool.result_preview) for tool in turn.trace],
                turn.recorder.outcome,
                turn.recorder.assistant_text,
                [(tool.tool_name, tool.result_preview) for tool in turn.recorder.completed_tools],
            ),
        )
    assert observed[1] == observed[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("collect_stream", "direct_stream"),
    [(False, False), (True, False), (True, True)],
    ids=["blocking", "collected-stream", "matrix-stream"],
)
async def test_a_turn_with_jobs_on_lets_a_managed_call_detach(
    tmp_path: Path,
    *,
    collect_stream: bool,
    direct_stream: bool,
) -> None:
    """Every reply entry point runs its managed calls as jobs once the instance runs them."""
    turn = await _run_silent_turn(
        tmp_path,
        [ModelResponse(tool_calls=[_call("probe_tool", "read", background_after=0)]), ModelResponse(content="done")],
        enabled=True,
        collect_stream=collect_stream,
        direct_stream=direct_stream,
        hold_probe=True,
    )
    [call] = [tool for tool in turn.trace if tool.tool_name == "probe_tool"]
    assert '"job_id"' in str(call.result_preview)


@pytest.mark.asyncio
@pytest.mark.parametrize("silent", [False, True])
async def test_quiet_and_visible_results_stay_with_their_own_replies(tmp_path: Path, *, silent: bool) -> None:
    """A silent schedule's reply hears only of quiet results, and an ordinary reply only of visible ones."""
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
    runtime = await tool_job_runtime(bot.runtime_paths.storage_root)
    pin_background_tool_jobs(context.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "result")

    try:
        for name, kind in (("quiet", SILENT_SCHEDULE_SOURCE_KIND), ("visible", "message")):
            await start_job(
                runtime,
                name,
                tool_name="tool",
                depth=0,
                source_kind=kind,
                adapter={},
                owner=owner,
                operation=operation,
            )
            waited = await runtime.wait(name, owner=owner, depth=0)
            await runtime.release_wait(name, waited.claim)
        with tool_runtime_context(context):
            current = current_conversation()
        assert current is not None
        assert [job.job_id for job in await current[0].ended_jobs(current[1])] == ["quiet" if silent else "visible"]
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_accepted_job_persists_silent_delivery_policy_across_restart(tmp_path: Path, *, native: bool) -> None:
    """SDK and native delegated calls stay with quiet replies after a restart, apart from visible ones."""
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
    runtime = await tool_job_runtime(bot.runtime_paths.storage_root)
    pin_background_tool_jobs(context.config, bot.runtime_paths)
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
            ModelResponse(tool_calls=[_call("slow", "call", background_after=0)]),
            ModelResponse(content="NO_REPLY"),
        ],
    )
    install_tool_job_execution(model)
    agent = Agent(id="general", model=model, tools=[assembled_function(slow)])
    try:
        async with execution_resources():
            with tool_runtime_context(context):
                if native:
                    child = replace(delegation_child(completed_delegation_job()), caller_agent_name="general")
                    accepted = await start_delegation_job(runtime, child, owner=owner, operation=native_operation)
                    job_id = accepted.job_id
                else:
                    response = await agent.arun("Check quietly", session_id=owner.session_id)
                    job_id = json.loads(response.tools[0].result)["job_id"]
            release.set()
            waited = await runtime.wait(job_id, owner=owner, depth=0)
            await runtime.release_wait(job_id, waited.claim)
        await runtime.shutdown()
        runtime = await tool_job_runtime(bot.runtime_paths.storage_root)
        await runtime.recover()
        restored = pending_outcome(runtime, job_id)
        assert restored is not None
        assert restored.source_kind == SILENT_SCHEDULE_SOURCE_KIND
        assert owner.room_id is not None
        assert owner.requester_id is not None
        conversation = {
            "recipient": owner.recipient,
            "room_id": owner.room_id,
            "thread_id": owner.resolved_thread_id,
            "requester_id": owner.requester_id,
        }
        quiet = await runtime.ended_jobs(ConversationKey(**conversation, silent=True))
        assert [job.job_id for job in quiet] == [job_id]
        assert await runtime.ended_jobs(ConversationKey(**conversation, silent=False)) == ()
    finally:
        release.set()
        await runtime.shutdown()
