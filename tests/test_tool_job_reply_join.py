"""A reply continues with its conversation's ready job results, or ends and lets its message hold the rest."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.visible_body import visible_body_from_content
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    DynamicContinuationRunState,
    TurnRunState,
    TurnSinks,
    run_blocking_response_turn,
    stream_response_turn,
)
from mindroom.streaming import StreamingPresentation
from mindroom.tool_jobs.completion import (
    JOB_JOIN_LIMIT,
    HeldContinuation,
    ReplyBoundaryReport,
    _JobJoin,
    completion_prompt,
    delegated_child_context,
    join_approval_jobs,
    join_conversation_jobs,
    reply_boundary_report,
)
from mindroom.tool_jobs.held_replies import (
    _APPROVAL_NOTICE,
    _WAITING_NOTICE,
    HeldReply,
    HoldKey,
    held_edit,
    released_edit,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.events import StructuredStreamChunk, ToolTraceEntry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import test_runtime_paths, unwrap_extracted_collaborator
from tests.delegation_helpers import _delegate_runtime_context
from tests.response_runner_helpers import _plain_request, _target
from tests.test_response_turn import _AdapterLog, _blocking_adapter, _continuation, _ctx, _streaming_adapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


import pytest

from tests.response_runner_helpers import _bot
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    completed_delegation_job,
    lookup,
    managed_team_config,
    pending_outcome,
    pending_outcomes,
    start_job,
    tool_job_runtime,
    wait_for_status,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize(
    ("first", "last", "expected"),
    [
        ("First finding", "NO_REPLY", "First finding"),
        ("NO_REPLY", "Second finding", "Second finding"),
        ("NO_REPLY", "NO_REPLY", "NO_REPLY"),
        ("First finding", "Second finding", "First finding\n\nSecond finding"),
    ],
)
async def test_quiet_join_preserves_findings_without_accumulating_no_reply(
    tmp_path: Path,
    first: str,
    last: str,
    expected: str,
    streaming: bool,
) -> None:
    """Quiet continuations retain substantive findings while treating NO_REPLY as control data."""
    paths, owner = test_runtime_paths(tmp_path), completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
        source_kind=SILENT_SCHEDULE_SOURCE_KIND,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    recorder = TurnRecorder(user_message="Quiet check")
    answers = iter((first, last))

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved result")

    async def attempt(_run: TurnRunState, _state: DynamicContinuationRunState) -> CompletedAttempt:
        text = next(answers)
        return CompletedAttempt(response_text=text, replayable_text=text, has_visible_content=True)

    async def stream_attempt(run: TurnRunState, state: DynamicContinuationRunState) -> AsyncIterator[AttemptResolved]:
        yield AttemptResolved(await attempt(run, state))

    try:
        await start_job(
            runtime,
            "quiet",
            tool_name="tool",
            depth=0,
            source_kind=SILENT_SCHEDULE_SOURCE_KIND,
            adapter={},
            owner=owner,
            operation=operation,
        )
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        await runtime.release_wait("quiet", waited.claim)
        with tool_runtime_context(context):
            if streaming:
                async for _ in stream_response_turn(
                    _ctx(allow_no_report_response=True, background_tool_jobs=True),
                    _streaming_adapter(_AdapterLog(), stream_attempt),
                    TurnSinks(turn_recorder=recorder),
                    continuation=_continuation(),
                ):
                    pass
            else:
                answer = await run_blocking_response_turn(
                    _ctx(allow_no_report_response=True, background_tool_jobs=True),
                    _blocking_adapter(_AdapterLog(), attempt),
                    TurnSinks(turn_recorder=recorder),
                    continuation=_continuation(),
                )
                assert answer == expected
        assert recorder.assistant_text == expected
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


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_response_boundary_joins_ready_results_without_repeating_ignored_prompt(
    tmp_path: Path,
    streaming: bool,
) -> None:
    """Both shared drivers continue once at the safe boundary even if the model ignores retrieval."""
    paths, owner = test_runtime_paths(tmp_path), completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    prompts = []

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "done")

    async def attempt(_run: TurnRunState, state: DynamicContinuationRunState) -> CompletedAttempt:
        prompts.append(state.active_prompt)
        return CompletedAttempt(response_text="answer", replayable_text="answer", has_visible_content=True)

    async def stream_attempt(run: TurnRunState, state: DynamicContinuationRunState) -> AsyncIterator[AttemptResolved]:
        yield AttemptResolved(await attempt(run, state))

    try:
        await start_job(runtime, "quiet", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        await runtime.release_wait("quiet", waited.claim)
        with tool_runtime_context(context):
            if streaming:
                _ = [
                    item
                    async for item in stream_response_turn(
                        _ctx(),
                        _streaming_adapter(_AdapterLog(), stream_attempt),
                        TurnSinks(),
                        continuation=_continuation(),
                    )
                ]
            else:
                await run_blocking_response_turn(
                    _ctx(),
                    _blocking_adapter(_AdapterLog(), attempt),
                    TurnSinks(),
                    continuation=_continuation(),
                )
        assert len(prompts) == 2
        assert 'job_id="quiet"' in prompts[1]
        assert pending_outcome(runtime, "quiet") is not None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("matching_source", [False, True])
@pytest.mark.parametrize("consumed", [False, True])
@pytest.mark.parametrize("authorized", [False, True])
async def test_replayed_human_source_uses_retained_job_without_rerunning_prompt(
    tmp_path: Path,
    matching_source: bool,
    consumed: bool,
    authorized: bool,
) -> None:
    """A crash during joining must recover the exact accepted work instead of repeating it."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target(thread_id="$thread"))
    owner = replace(
        completed_delegation_job().owner,
        agent_name="general",
        transport_agent_name=None,
        requester_id=request.response_envelope.requester_id,
        session_id=request.response_envelope.target.session_id,
    )
    allowed = True
    runtime = await tool_job_runtime(tmp_path, authorize=lambda _job: allowed)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("interrupted", "Execution stopped; side effects may have happened.")

    try:
        await start_job(
            runtime,
            "retained",
            tool_name="tool",
            depth=0,
            source_event_id=request.response_envelope.source_event_id if matching_source else "$unrelated",
            adapter={},
            owner=owner,
            operation=operation,
        )
        waited = await runtime.wait("retained", owner=owner, depth=0)
        if consumed:
            await runtime.acknowledge_wait("retained", waited.claim)
        else:
            await runtime.release_wait("retained", waited.claim)
        allowed = authorized
        recovered = await runner._recover_tool_job_source(request)
        if matching_source:
            note = recovered.system_enrichment_items[-1].text
            assert 'job_id="retained"' in note
            assert recovered.response_envelope.source_event_id == request.response_envelope.source_event_id
            assert recovered.sources == request.sources
            assert recovered.prompt == request.prompt
            assert "Execution stopped; side effects may have happened." not in note
            if not authorized:
                assert pending_outcome(runtime, "retained") is None
        else:
            assert recovered is request
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["continuation_cancel", "continuation_error"])
async def test_blocking_join_keeps_recorder_interruptible(tmp_path: Path, failure_boundary: str) -> None:
    """Retrieving ready work cannot publish top-level completion before the continuation finishes."""
    paths, owner = test_runtime_paths(tmp_path), completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    continuing = asyncio.Event()
    recorder = TurnRecorder(user_message="Original request", run_id="run-1")
    metadata: dict[str, object] = {}
    attempts = 0

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "retained result")

    async def attempt(_run: TurnRunState, _state: DynamicContinuationRunState) -> CompletedAttempt:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return CompletedAttempt(
                response_text="Independent work done",
                replayable_text="Independent work done",
                has_visible_content=True,
                metadata_content={"final": True},
            )
        continuing.set()
        if failure_boundary == "continuation_error":
            message = "result continuation failed"
            raise RuntimeError(message)
        await asyncio.Event().wait()
        message = "unreachable"
        raise AssertionError(message)

    task = None
    try:
        await start_job(runtime, "retained", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        await wait_for_status(runtime, "retained", "completed")
        with tool_runtime_context(context):
            task = asyncio.create_task(
                run_blocking_response_turn(
                    _ctx(),
                    _blocking_adapter(_AdapterLog(), attempt),
                    TurnSinks(turn_recorder=recorder, run_metadata_collector=metadata),
                    continuation=_continuation(),
                ),
            )
            await asyncio.wait_for(continuing.wait(), JOB_TEST_TIMEOUT)
            assert recorder.outcome == "pending"
            assert recorder.assistant_text == "Independent work done"
            assert metadata == {}
            if failure_boundary == "continuation_cancel":
                task.cancel()
            expected = RuntimeError if failure_boundary == "continuation_error" else asyncio.CancelledError
            with pytest.raises(expected):
                await task
        assert recorder.outcome == "interrupted"
        assert recorder.assistant_text == "Independent work done"
        assert recorder.claim_interrupted_persistence()
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_approval_join_stops_at_the_join_limit(tmp_path: Path) -> None:
    """A resumed approval joins ready results at most `JOB_JOIN_LIMIT` times, then leaves its message holding nothing."""
    paths = test_runtime_paths(tmp_path)
    owner = completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = _job_context(tmp_path, owner)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    continued: list[str] = []
    report = ReplyBoundaryReport()

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "done")

    async def leave_ready_result() -> None:
        job_id = f"ready-{len(continued)}"
        await start_job(runtime, job_id, tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait(job_id, owner=owner, depth=0)
        await runtime.release_wait(job_id, waited.claim)

    async def continue_response(response: str, prompt: str) -> str:
        continued.append(prompt)
        # Each continuation leaves another ready result, so only the budget ends the joins.
        await leave_ready_result()
        return response

    try:
        await leave_ready_result()
        with tool_runtime_context(context), reply_boundary_report(report):
            await join_approval_jobs(
                "completed run",
                is_complete=lambda _response: True,
                continue_response=continue_response,
            )
        assert len(continued) == JOB_JOIN_LIMIT
        # Past the limit the message holds nothing; the next reply in the conversation takes the work.
        assert report.boundary is not None
        assert report.boundary.notice is None
    finally:
        await runtime.shutdown()


def test_join_prompt_retrieves_each_finished_outcome_without_waiting() -> None:
    """Every joined job has finished, so each retrieval call reads its outcome without a wait budget."""
    done = completed_delegation_job()
    failed = replace(done, job_id="other", status="failed")
    prompt = completion_prompt([done, failed])
    assert f'job(action="wait", job_id="{done.job_id}", wait_timeout=0)' in prompt
    assert 'job(action="wait", job_id="other", wait_timeout=0)' in prompt


def _job_context(tmp_path: Path, owner: ToolExecutionIdentity) -> ToolRuntimeContext:
    paths = test_runtime_paths(tmp_path)
    return replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )


@pytest.mark.asyncio
async def test_boundary_records_outstanding_work_for_the_message_to_hold(tmp_path: Path) -> None:
    """With nothing ready, the reply ends and records the work its message holds, which keeps running."""
    owner = completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = _job_context(tmp_path, owner)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    finish = asyncio.Event()
    report = ReplyBoundaryReport()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    try:
        await start_job(runtime, "work", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        with tool_runtime_context(context), reply_boundary_report(report):
            assert await join_conversation_jobs(set(), joins=0) == _JobJoin(holds=True)
        assert report.boundary is not None
        assert report.boundary.notice == _WAITING_NOTICE
        assert report.boundary.joins == 0
        assert report.boundary.key == HoldKey(
            recipient=owner.recipient,
            room_id=owner.room_id,
            thread_id=owner.resolved_thread_id,
            requester_id=owner.requester_id,
            silent=False,
            participants=(owner.agent_name,),
        )
        assert (await lookup(runtime, "work", owner=owner, depth=0)).status == "running"
        finish.set()
        await wait_for_status(runtime, "work", "completed")
        with tool_runtime_context(context):
            joined = await join_conversation_jobs(set(), joins=0)
        assert joined.prompt is not None
        assert 'job_id="work"' in joined.prompt
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["agent", "team", "delegated_child"])
async def test_boundary_holds_only_its_participants_work(tmp_path: Path, reply: str) -> None:
    """A reply holds the work of the agents it speaks for; a delegated child inside its caller holds nothing."""
    owner = replace(completed_delegation_job().owner, agent_name="worker", transport_agent_name="lead")
    runtime = await tool_job_runtime(tmp_path)
    context = replace(_job_context(tmp_path, owner), agent_name="lead")
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    finish = asyncio.Event()
    report = ReplyBoundaryReport()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    try:
        await start_job(runtime, "member", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        with tool_runtime_context(context), reply_boundary_report(report):
            if reply == "delegated_child":
                with delegated_child_context():
                    assert await join_conversation_jobs(set(), joins=0) == _JobJoin()
                assert report.boundary is None
                return
            joined = await join_conversation_jobs(set(), joins=0, agent_names=("worker",) if reply == "team" else None)
        assert joined == _JobJoin(holds=reply == "team")
        assert report.boundary is not None
        assert report.boundary.key.participants == (("lead", "worker") if reply == "team" else ("lead",))
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_boundary_names_a_job_awaiting_approval(tmp_path: Path) -> None:
    """A held message says when its work waits for approval cards."""
    owner = completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = _job_context(tmp_path, owner)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    decided = asyncio.Event()
    report = ReplyBoundaryReport()

    async def approval() -> BackgroundOutcome:
        await runtime.set_awaiting_approval("approval", awaiting=True)
        await decided.wait()
        return BackgroundOutcome("completed", "approved and done")

    try:
        await start_job(runtime, "approval", tool_name="delegate", depth=0, adapter={}, owner=owner, operation=approval)
        await wait_for_status(runtime, "approval", "awaiting_approval")
        with tool_runtime_context(context), reply_boundary_report(report):
            assert await join_conversation_jobs(set(), joins=0) == _JobJoin(holds=True)
        assert report.boundary is not None
        assert report.boundary.notice == _APPROVAL_NOTICE
    finally:
        decided.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_boundary_does_not_hold_revoked_work(tmp_path: Path) -> None:
    """Work the requester may no longer access is gone for the reply, so its message holds nothing."""
    owner = completed_delegation_job().owner
    allowed = True
    runtime = await tool_job_runtime(tmp_path, authorize=lambda _job: allowed)
    context = _job_context(tmp_path, owner)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    report = ReplyBoundaryReport()

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    try:
        await start_job(runtime, "revoked", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        allowed = False
        await runtime.cancel_revoked(denied=lambda _job: True)
        with tool_runtime_context(context), reply_boundary_report(report):
            assert await join_conversation_jobs(set(), joins=0) == _JobJoin()
        assert report.boundary is not None
        assert report.boundary.notice is None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_turn_continuing_a_held_message_extends_it(tmp_path: Path, *, streaming: bool) -> None:
    """A continuation starts from the held message and asks once for the ready work it was woken for."""
    owner = completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = _job_context(tmp_path, owner)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    prompts: list[str] = []
    prior = StreamingPresentation(
        response_text="Started the report.",
        tool_trace=(ToolTraceEntry("tool_call_completed", "report"),),
    )

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "done")

    async def attempt(_run: TurnRunState, state: DynamicContinuationRunState) -> CompletedAttempt:
        prompts.append(state.active_prompt)
        return CompletedAttempt(response_text="The report is done.", replayable_text="The report is done.")

    async def stream_attempt(run: TurnRunState, state: DynamicContinuationRunState) -> AsyncIterator[AttemptResolved]:
        yield AttemptResolved(await attempt(run, state))

    ctx = replace(
        _ctx(),
        held_continuation=HeldContinuation(presentation=prior, attempted_job_ids=frozenset({"work"}), joins=3),
    )
    try:
        await start_job(runtime, "work", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        await wait_for_status(runtime, "work", "completed")
        with tool_runtime_context(context):
            if streaming:
                chunks = [
                    chunk
                    async for chunk in stream_response_turn(
                        ctx,
                        _streaming_adapter(_AdapterLog(), stream_attempt),
                        TurnSinks(),
                        continuation=_continuation("Retrieve the work"),
                    )
                ]
                assert chunks[0] == StructuredStreamChunk(
                    content="Started the report.",
                    tool_trace=list(prior.tool_trace),
                )
            else:
                answer = await run_blocking_response_turn(
                    ctx,
                    _blocking_adapter(_AdapterLog(), attempt),
                    TurnSinks(),
                    continuation=_continuation("Retrieve the work"),
                )
                assert answer == "Started the report.\n\nThe report is done."
        # The ready work is asked for once; its unread outcome is not asked for again within the turn.
        assert prompts == ["Retrieve the work"]
    finally:
        await runtime.shutdown()


def test_held_message_edits_show_and_drop_the_waiting_notice(tmp_path: Path) -> None:
    """A held message shows its reply with the notice while it waits, and without it once it holds nothing."""
    bot = _bot(tmp_path)
    hold = HeldReply(
        key=HoldKey("general", "!room:localhost", "$thread", "@user:localhost", False, ("general",)),
        target=_target(thread_id="$thread"),
        source_kind=MESSAGE_SOURCE_KIND,
        message_event_id="$response",
        presentation=StreamingPresentation("Ping @general"),
        extra_content={"io.mindroom.ai_run": {"run_id": "run"}},
        notice=_WAITING_NOTICE,
        stop_button_event_id=None,
        joins=0,
    )
    held = held_edit(hold)
    assert held.event_id == "$response"
    assert held.new_text == f"Ping @general\n\n{_WAITING_NOTICE}"
    assert held.extra_content == {
        "io.mindroom.ai_run": {"run_id": "run"},
        "io.mindroom.stream_status": "streaming",
        "io.mindroom.warmup_suffix": _WAITING_NOTICE,
    }
    content = format_message_with_mentions(
        bot.config,
        bot.runtime_paths,
        held.new_text,
        extra_content=held.extra_content,
    )
    recovered = visible_body_from_content(
        content,
        "",
        sender_id=bot.matrix_id.full_id,
        trusted_sender_ids={bot.matrix_id.full_id},
    )
    # Recovery reads the published body without the notice, with its resolved mention.
    assert recovered == content["body"].removesuffix(f"\n\n{_WAITING_NOTICE}")
    released = released_edit(hold)
    assert released.new_text == "Ping @general"
    assert released.extra_content == {"io.mindroom.ai_run": {"run_id": "run"}, "io.mindroom.stream_status": "completed"}
