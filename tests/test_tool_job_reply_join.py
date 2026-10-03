"""A reply joins its conversation's outstanding jobs before it finishes, waiting visibly and interruptibly."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
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
    _completion_prompt,
    _ReadyJobContinuation,
    background_wait_edit,
    background_wait_notice,
    join_approval_jobs,
    join_conversation_jobs,
    report_background_wait,
)
from mindroom.tool_jobs.control import HumanMessageSignal, human_message_signal_context
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import test_runtime_paths, unwrap_extracted_collaborator
from tests.delegation_helpers import _delegate_runtime_context
from tests.response_runner_helpers import _plain_request, _target
from tests.test_response_turn import _AdapterLog, _blocking_adapter, _continuation, _ctx, _streaming_adapter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.delivery_gateway import EditTextRequest
    from mindroom.tool_jobs.runtime import JobWait

import pytest

from tests.response_runner_helpers import _bot
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    completed_delegation_job,
    managed_team_config,
    pending_outcome,
    pending_outcomes,
    start_job,
    tool_job_runtime,
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
    runtime = tool_job_runtime(tmp_path)
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
    runtime = tool_job_runtime(tmp_path)
    owner = completed_delegation_job().owner

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("failed", "tool failed")

    try:
        await start_job(runtime, "quiet", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        assert not pending_outcomes(runtime)
        await runtime.release_wait("quiet", waited.claim)
        released = pending_outcome(runtime, "quiet", 0)
        assert released is not None
        assert released.result == "tool failed"
        assert [job.job_id for job in pending_outcomes(runtime)] == ["quiet"]
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        await runtime.acknowledge_wait("quiet", waited.claim)
        assert pending_outcome(runtime, "quiet", 0) is None
        assert not pending_outcomes(runtime)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_auto_join_waits_once_and_human_input_releases_only_wait(tmp_path: Path) -> None:
    """Turn-end waiting is visible, interruptible, and does not cancel the operation."""
    paths = test_runtime_paths(tmp_path)
    owner = completed_delegation_job().owner
    runtime = tool_job_runtime(tmp_path)
    context = replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    signal, finish = HumanMessageSignal(), asyncio.Event()
    attempted = set()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    try:
        with human_message_signal_context(signal):
            await start_job(runtime, "quiet", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        with tool_runtime_context(context), human_message_signal_context(signal):
            stream = join_conversation_jobs(attempted)
            assert "Waiting" in (await anext(stream)).content
            signal.notify()
            assert [item.content async for item in stream] == [None]
            assert (await runtime.lookup("quiet", owner=owner, depth=0)).status == "running"
            signal.clear()
            finish.set()
            waited = await runtime.wait("quiet", owner=owner, depth=0)
            await runtime.release_wait("quiet", waited.claim)
            items = [item async for item in join_conversation_jobs(attempted)]
            assert len(items) == 1
            assert 'job_id="quiet"' in items[0].prompt
            assert [item async for item in join_conversation_jobs(attempted)] == []
            assert pending_outcome(runtime, "quiet", 0) is not None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_job", [False, True])
async def test_revocation_during_the_reply_wait_finishes_the_reply(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    other_job: bool,
) -> None:
    """A job whose access is revoked while the reply waits is gone for that reply; its other jobs still join."""
    paths = test_runtime_paths(tmp_path)
    owner = completed_delegation_job().owner
    allowed = True
    runtime = tool_job_runtime(tmp_path, authorize=lambda job: allowed or job.job_id == "kept")
    context = replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)

    async def operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    finish = asyncio.Event()
    rejoined = asyncio.Event()
    kept_waits = 0
    original_wait = runtime.wait

    async def kept() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "Kept result")

    async def wait(job_id: str, **kwargs: Any) -> JobWait:  # noqa: ANN401
        nonlocal kept_waits
        if job_id == "kept":
            kept_waits += 1
            if kept_waits == 2:
                rejoined.set()
        return await original_wait(job_id, **kwargs)

    monkeypatch.setattr(runtime, "wait", wait)

    try:
        await start_job(runtime, "revoked", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        if other_job:
            await start_job(runtime, "kept", tool_name="tool", depth=0, adapter={}, owner=owner, operation=kept)
        with tool_runtime_context(context):
            stream = join_conversation_jobs(set())
            assert "Waiting" in (await anext(stream)).content

            async def rest() -> list[object]:
                return [item async for item in stream]

            joining = asyncio.create_task(rest())
            allowed = False
            await runtime.cancel_revoked(denied=lambda job: job.job_id == "revoked")
            if other_job:
                # The reply keeps waiting on the job it can still access.
                await asyncio.wait_for(rejoined.wait(), JOB_TEST_TIMEOUT)
                assert not joining.done()
            finish.set()
            joined = await joining
        assert [item.content for item in joined[:1]] == [None]
        if other_job:
            assert len(joined) == 2
            assert isinstance(joined[1], _ReadyJobContinuation)
            assert "kept" in joined[1].prompt
            assert "revoked" not in joined[1].prompt
        else:
            assert len(joined) == 1
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
    runtime = tool_job_runtime(tmp_path)
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
        assert pending_outcome(runtime, "quiet", 0) is not None
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
    runtime = tool_job_runtime(tmp_path, authorize=lambda _job: allowed)
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
                assert pending_outcome(runtime, "retained", 0) is None
        else:
            assert recovered is request
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_blocking_join_updates_existing_response_placeholder(tmp_path: Path) -> None:
    """Blocking work exposes the interruptible wait on its owned visible response."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = replace(_plain_request(_target()), existing_event_id="$placeholder", existing_event_is_placeholder=True)
    edits = []

    async def edit(_gateway: object, request: EditTextRequest) -> bool:
        edits.append(request)
        return True

    async def response(_target: object, _state: object) -> None:
        await report_background_wait(StreamingPresentation("Independent answer."), "Waiting for background work")

    with patch.object(type(runner.deps.delivery_gateway), "edit_text", edit):
        await runner._run_locked_response_lifecycle(request, response_kind="test", locked_operation=response)
    assert len(edits) == 1
    assert edits[0].event_id == "$placeholder"
    assert "Waiting" in edits[0].new_text
    assert edits[0].extra_content == {
        "msgtype": "m.notice",
        "io.mindroom.stream_status": "streaming",
        "io.mindroom.warmup_suffix": "Waiting for background work",
    }
    assert edits[0].delivery_turn_id is None


def test_blocking_wait_preserves_formatted_mention_on_recovery(tmp_path: Path) -> None:
    """Wait metadata must recover the body actually published after mention resolution."""
    bot = _bot(tmp_path)
    request = background_wait_edit(_target(), "$response", StreamingPresentation("Ping @general"), "Waiting")
    content = format_message_with_mentions(
        bot.config,
        bot.runtime_paths,
        request.new_text,
        extra_content=request.extra_content,
    )
    recovered = visible_body_from_content(
        content,
        "",
        sender_id=bot.matrix_id.full_id,
        trusted_sender_ids={bot.matrix_id.full_id},
    )
    expected = content["body"].removesuffix("\n\nWaiting")
    assert expected != "Ping @general"
    assert recovered == expected


@pytest.mark.asyncio
async def test_ready_approval_is_retrieved_before_waiting_on_other_running_jobs(tmp_path: Path) -> None:
    """A pending approval reaches the existing native wait path without a join deadlock."""
    paths, owner = test_runtime_paths(tmp_path), completed_delegation_job().owner
    runtime = tool_job_runtime(tmp_path)
    context = replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    release = asyncio.Event()

    async def running() -> BackgroundOutcome:
        await release.wait()
        return BackgroundOutcome("completed", "done")

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval", "Approval required")

    try:
        await start_job(runtime, "running", tool_name="tool", depth=0, adapter={}, owner=owner, operation=running)
        await start_job(
            runtime,
            "approval",
            tool_name="delegation",
            depth=0,
            adapter={},
            owner=owner,
            operation=approval,
        )
        waited = await runtime.wait("approval", owner=owner, depth=0)
        await runtime.release_wait("approval", waited.claim)
        with tool_runtime_context(context):
            async with asyncio.timeout(1):
                items = [item async for item in join_conversation_jobs(set())]
        assert len(items) == 1
        assert not isinstance(items[0], str)
        assert 'job_id="approval"' in items[0].prompt
        assert 'job_id="running"' not in items[0].prompt
        assert pending_outcome(runtime, "approval", 0) is not None
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_boundary", ["join_cancel", "continuation_cancel", "continuation_error"])
async def test_blocking_join_keeps_recorder_interruptible(tmp_path: Path, failure_boundary: str) -> None:  # noqa: PLR0915
    """Joining or retrieving retained work cannot publish top-level completion early."""
    paths, owner = test_runtime_paths(tmp_path), completed_delegation_job().owner
    runtime = tool_job_runtime(tmp_path)
    context = replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    finish, waiting, continuing = asyncio.Event(), asyncio.Event(), asyncio.Event()
    recorder = TurnRecorder(user_message="Original request", run_id="run-1")
    metadata: dict[str, object] = {}
    attempts = 0

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "retained result")

    async def progress(_presentation: StreamingPresentation, _notice: str | None) -> None:
        waiting.set()

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
        with tool_runtime_context(context), background_wait_notice(progress):
            task = asyncio.create_task(
                run_blocking_response_turn(
                    _ctx(),
                    _blocking_adapter(_AdapterLog(), attempt),
                    TurnSinks(turn_recorder=recorder, run_metadata_collector=metadata),
                    continuation=_continuation(),
                ),
            )
            await asyncio.wait_for(waiting.wait(), JOB_TEST_TIMEOUT)
            assert recorder.outcome == "pending"
            assert recorder.assistant_text == "Independent work done"
            assert metadata == {}
            if failure_boundary == "join_cancel":
                task.cancel()
            else:
                finish.set()
                await asyncio.wait_for(continuing.wait(), JOB_TEST_TIMEOUT)
                if failure_boundary == "continuation_cancel":
                    task.cancel()
            expected = RuntimeError if failure_boundary == "continuation_error" else asyncio.CancelledError
            with pytest.raises(expected):
                await task
        assert recorder.outcome == "interrupted"
        assert recorder.assistant_text == "Independent work done"
        assert recorder.claim_interrupted_persistence()
        if failure_boundary == "join_cancel":
            assert (await runtime.lookup("retained", owner=owner, depth=0)).status == "running"
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_approval_join_stops_at_the_join_limit(tmp_path: Path) -> None:
    """A resumed approval joins ready results at most `JOB_JOIN_LIMIT` times."""
    paths = test_runtime_paths(tmp_path)
    owner = completed_delegation_job().owner
    runtime = tool_job_runtime(tmp_path)
    context = replace(
        _delegate_runtime_context(managed_team_config(tmp_path), paths, execution_identity=owner),
        agent_name=owner.agent_name,
        transport_agent_name=owner.transport_agent_name,
    )
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    continued: list[str] = []

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
        with tool_runtime_context(context):
            await join_approval_jobs(
                "completed run",
                is_complete=lambda _response: True,
                continue_response=continue_response,
                presentation=lambda: StreamingPresentation(response_text=""),
            )
        assert len(continued) == JOB_JOIN_LIMIT
    finally:
        await runtime.shutdown()


def test_join_prompt_lets_an_approved_retrieval_wait_for_the_approved_work() -> None:
    """A ready result needs no wait; an approval pause's retrieval has no budget, so approving it waits for its work."""
    done = completed_delegation_job()
    paused = replace(done, status="awaiting_approval")
    assert f'job(action="wait", job_id="{done.job_id}", wait_timeout=0)' in _completion_prompt([done])
    assert f'job(action="wait", job_id="{paused.job_id}")' in _completion_prompt([paused])
