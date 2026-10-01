"""Quiet runtime outcomes use internal journal sources and existing response ordering."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.dynamic_tool_continuation import DYNAMIC_TOOL_CONTINUATION_LIMIT
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

from mindroom.event_journal import EventKind
from mindroom.tool_jobs.completion import completion_envelope, completion_event
from tests.response_runner_helpers import _bot
from tests.tool_job_helpers import (
    JOB_TEST_TIMEOUT,
    completed_delegation_job,
    delivery_coordinator,
    finish_delegation_job,
    managed_team_config,
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
async def test_completion_source_is_internal_and_has_no_conversation_projection(tmp_path: Path) -> None:
    """An outcome gains durable response ownership without adding a Matrix message."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    job = replace(
        completed_delegation_job(),
        owner=replace(completed_delegation_job().owner, agent_name="general", transport_agent_name=None),
    )
    event = completion_event(job, sender_id=bot.matrix_id.full_id)
    envelope = completion_envelope(job, sender_id=bot.matrix_id.full_id)
    store = bot._journal_store.principal(bot._journal_principal_id)
    await store.admit(event)
    admitted = await store.load_event(event.event_id)
    assert admitted is not None
    assert admitted.kind is EventKind.TOOL_JOB_COMPLETION
    assert "content" not in admitted.source
    assert envelope.requester_id == job.owner.requester_id
    assert not envelope.origin.may_answer_interactive_prompt
    assert envelope.source_event_id == event.event_id
    assert envelope.target.resolved_thread_id == job.owner.resolved_thread_id
    assert envelope.target.reply_to_event_id != event.event_id
    assert await store.is_pending(event.event_id)


@pytest.mark.asyncio
async def test_internal_completion_dispatch_does_not_parse_matrix_event(tmp_path: Path) -> None:
    """Journal replay sends internal work straight to its completion owner."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    dispatcher = unwrap_extracted_collaborator(bot._journal_dispatcher)
    job = replace(
        completed_delegation_job(),
        owner=replace(completed_delegation_job().owner, agent_name="general", transport_agent_name=None),
    )
    event = completion_event(job, sender_id=bot.matrix_id.full_id)
    store = bot._journal_store.principal(bot._journal_principal_id)
    await store.admit(event)
    admitted = await store.load_event(event.event_id)
    assert admitted is not None
    callback = AsyncMock(return_value=False)
    dispatcher.callbacks = replace(dispatcher.callbacks, on_tool_job_completion=callback)
    dispatcher.release_turn_replay()
    with patch("mindroom.journal_dispatch.parse_journal_event", side_effect=AssertionError("Matrix parser called")):
        assert not await dispatcher._run_event(admitted)
    callback.assert_awaited_once_with(admitted)


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
        assert not await runtime.pending_outcomes()
        await runtime.release_wait("quiet", waited.claim)
        assert (await runtime.outcome("quiet", 0)).result == "tool failed"
        assert [job.job_id for job in await runtime.pending_outcomes()] == ["quiet"]
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        await runtime.acknowledge_wait("quiet", waited.claim)
        assert await runtime.outcome("quiet", 0) is None
        assert not await runtime.pending_outcomes()
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
async def test_completion_waits_for_active_and_newer_turns(tmp_path: Path, consumed: bool) -> None:
    """Completion never competes with a stream or an already queued human response."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    target = _target(thread_id="$thread")
    request = _plain_request(target)
    owner = replace(
        completed_delegation_job().owner,
        agent_name="general",
        transport_agent_name=None,
        requester_id=request.user_id or "@user:localhost",
    )
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)
    order = []
    started, release = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "answer")

    async def stream(_target: object) -> None:
        started.set()
        await release.wait()
        order.append("stream")

    async def newer(_target: object) -> None:
        order.append("human")

    async def completed(_request: object, **_kwargs: object) -> None:
        order.append("completion")
        await runner.deps.approval_store.settle(event.event_id)

    try:
        await start_job(runtime, "quiet", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        waited = await runtime.wait("quiet", owner=owner, depth=0)
        if consumed:
            await runtime.acknowledge_wait("quiet", waited.claim)
        else:
            await runtime.release_wait("quiet", waited.claim)
        event = completion_event(waited.job, sender_id=bot.matrix_id.full_id)
        await runner.deps.approval_store.admit(event)
        admitted = await runner.deps.approval_store.load_event(event.event_id)
        assert admitted is not None
        first = asyncio.create_task(
            runner._lifecycle_coordinator.run_locked_response(
                target=target,
                response_envelope=request.response_envelope,
                pipeline_timing=None,
                locked_operation=stream,
            ),
        )
        await started.wait()
        second = asyncio.create_task(
            runner._lifecycle_coordinator.run_locked_response(
                target=target,
                response_envelope=replace(request.response_envelope, source_event_id="$newer"),
                pipeline_timing=None,
                locked_operation=newer,
            ),
        )
        await asyncio.sleep(0)
        with patch.object(runner, "_generate_response_locked", completed):
            assert not await runner.handoff_tool_job_completion(admitted)
            await asyncio.sleep(0)
            assert not order
            release.set()
            await asyncio.gather(first, second)
            await asyncio.gather(*tuple(runner._inbox_response_tasks))
        assert order == (["stream", "human"] if consumed else ["stream", "human", "completion"])
        assert not await runner.deps.approval_store.is_pending(event.event_id)
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
            assert await runtime.outcome("quiet", 0) is not None
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
        assert await runtime.outcome("quiet", 0) is not None
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_coordinator_wakes_conversation_without_matrix_notice(tmp_path: Path) -> None:
    """Ready work is published only to the internal response source owner."""
    coordinator = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await coordinator.initialize()
    try:
        job = await finish_delegation_job(coordinator)
        bot = coordinator.bot_provider("team")
        assert bot is not None
        await coordinator.deliver_pending()
        bot.wake_tool_job_completion.assert_awaited_once_with(job)
        bot.client.room_send.assert_not_called()
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_successful_completion_admission_is_not_repeated_after_bot_replacement(tmp_path: Path) -> None:
    """The durable journal owns an admitted generation, including after a bot is replaced."""
    coordinator = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await coordinator.initialize()
    bot = _bot(tmp_path)
    bot.running = True
    coordinator.bot_provider = lambda _name: bot
    store = bot.journal_principal()
    try:
        job = await finish_delegation_job(coordinator)
        event = completion_event(job, sender_id=bot.matrix_id.full_id)
        with patch.object(type(store), "admit", autospec=True, side_effect=type(store).admit) as admit:
            await coordinator.deliver_pending()
            assert await store.is_pending(event.event_id)
            await coordinator.deliver_pending()
            admit.assert_awaited_once()
            replacement = _bot(tmp_path)
            replacement.running = True
            coordinator.bot_provider = lambda _name: replacement
            await coordinator.deliver_pending()
            admit.assert_awaited_once()
            assert await replacement.journal_principal().is_pending(event.event_id)
            await coordinator.stop()
            await coordinator.initialize()
            await coordinator.runtime.recover()
            # A restarted coordinator admits again, and the journal keeps its one pending event.
            await coordinator.deliver_pending()
            assert admit.await_count == 2
            assert await store.is_pending(event.event_id)
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_failed_completion_admission_retries_and_new_generation_is_admitted(tmp_path: Path) -> None:
    """Only successful durable admission suppresses retries; approval outcomes keep distinct generations."""
    coordinator = delivery_coordinator(tmp_path, managed_team_config(tmp_path))
    await coordinator.initialize()
    bot = _bot(tmp_path)
    bot.running = True
    coordinator.bot_provider = lambda _name: bot
    fixture = completed_delegation_job()

    async def approval() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    async def complete() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "approved result")

    try:
        await start_job(
            coordinator.runtime,
            fixture.job_id,
            tool_name=fixture.tool_name,
            depth=0,
            kind=fixture.kind,
            adapter=fixture.adapter,
            owner=fixture.owner,
            operation=approval,
        )
        waited = await coordinator.runtime.wait(fixture.job_id, owner=fixture.owner, depth=0)
        await coordinator.runtime.release_wait(fixture.job_id, waited.claim)
        store = bot.journal_principal()
        first = completion_event(waited.job, sender_id=bot.matrix_id.full_id)
        with patch.object(type(store), "admit", side_effect=OSError("journal unavailable")):
            await coordinator.deliver_pending()
        assert not await store.is_pending(first.event_id)
        with patch.object(type(store), "admit", autospec=True, side_effect=type(store).admit) as admit:
            await coordinator.deliver_pending()
            assert await store.is_pending(first.event_id)
            await coordinator.deliver_pending()
            admit.assert_awaited_once()
            await coordinator.runtime.continue_job(
                fixture.job_id,
                owner=fixture.owner,
                depth=0,
                expected_generation=0,
                operation=complete,
                adapter=fixture.adapter,
            )
            waited = await coordinator.runtime.wait(fixture.job_id, owner=fixture.owner, depth=0)
            await coordinator.runtime.release_wait(fixture.job_id, waited.claim)
            second = completion_event(waited.job, sender_id=bot.matrix_id.full_id)
            assert second.event_id != first.event_id
            await coordinator.deliver_pending()
            assert await store.is_pending(second.event_id)
            await coordinator.deliver_pending()
            assert admit.await_count == 2
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
async def test_internal_source_envelope_is_stable_after_runtime_recovery(tmp_path: Path) -> None:
    """Recovery cannot conflict with an outcome source admitted before the crash."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    owner = replace(completed_delegation_job().owner, agent_name="general", transport_agent_name=None)
    runtime = tool_job_runtime(tmp_path)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "retained")

    await start_job(runtime, "recover", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
    waited = await runtime.wait("recover", owner=owner, depth=0)
    await runtime.release_wait("recover", waited.claim)
    event = completion_event(waited.job, sender_id=bot.matrix_id.full_id)
    await bot._journal_store.principal(bot._journal_principal_id).admit(event)
    await runtime.shutdown()
    restored = tool_job_runtime(tmp_path)
    try:
        await restored.recover()
        job = await restored.outcome("recover", 0)
        assert job is not None
        replay = completion_event(job, sender_id=bot.matrix_id.full_id)
        assert replay == event
        await bot._journal_store.principal(bot._journal_principal_id).admit(replay)
    finally:
        await restored.shutdown()


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
            assert not recovered.response_envelope.origin.may_answer_interactive_prompt
            assert recovered.response_envelope.source_event_id == request.response_envelope.source_event_id
            assert recovered.sources == request.sources
            assert recovered.prompt == request.prompt
            assert "Execution stopped; side effects may have happened." not in note
            if not authorized:
                assert await runtime.outcome("retained", 0) is None
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
@pytest.mark.parametrize("thread_root", [False, True])
async def test_idle_completion_defers_to_still_pending_original_source(tmp_path: Path, thread_root: bool) -> None:
    """The original durable source keeps exclusive recovery ownership of accepted work."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = _plain_request(_target(thread_id="$event" if thread_root else "$thread"))
    owner = replace(
        completed_delegation_job().owner,
        agent_name="general",
        transport_agent_name=None,
        requester_id=request.response_envelope.requester_id,
        session_id=request.response_envelope.target.session_id,
        resolved_thread_id=request.thread_id,
    )
    runtime = tool_job_runtime(tmp_path)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("interrupted", "Interrupted without replay.")

    respond = AsyncMock()

    try:
        await start_job(
            runtime,
            "recovered",
            tool_name="tool",
            depth=0,
            source_event_id="$event",
            adapter={},
            owner=owner,
            operation=operation,
        )
        waited = await runtime.wait("recovered", owner=owner, depth=0)
        await runtime.release_wait("recovered", waited.claim)
        event = completion_event(waited.job, sender_id=bot.matrix_id.full_id)
        await runner.deps.approval_store.admit(
            replace(
                event,
                event_id="$event",
                kind=EventKind.MESSAGE,
                thread_id=None if thread_root else event.thread_id,
                source={"content": {"body": "Original instruction"}},
            ),
        )
        await runner.deps.approval_store.admit(event)
        admitted = await runner.deps.approval_store.load_event(event.event_id)
        assert admitted is not None
        with patch.object(runner, "generate_response", respond):
            await runner._resume_tool_job_completion(admitted, "recovered", 0)
        respond.assert_not_awaited()
        assert await runner.deps.approval_store.is_pending("$event")
        assert await runner.deps.approval_store.is_pending(event.event_id)
        await runner.deps.approval_store.settle("$event")
        with patch.object(runner, "generate_response", respond):
            await runner._resume_tool_job_completion(admitted, "recovered", 0)
        respond.assert_awaited_once()
        assert respond.call_args.args[0].response_envelope.source_event_id == event.event_id
    finally:
        await runtime.shutdown()


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
        assert await runtime.outcome("approval", 0) is not None
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
@pytest.mark.parametrize("used", [0, DYNAMIC_TOOL_CONTINUATION_LIMIT - 1, DYNAMIC_TOOL_CONTINUATION_LIMIT])
async def test_approval_join_spends_only_the_remaining_continuation_budget(tmp_path: Path, used: int) -> None:
    """A resumed approval joins ready results only within the continuations its turn has left."""
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
            _, joins = await join_approval_jobs(
                "completed run",
                is_complete=lambda _response: True,
                continue_response=continue_response,
                presentation=lambda: StreamingPresentation(response_text=""),
                continuation_count=used,
            )
        assert joins == len(continued) == DYNAMIC_TOOL_CONTINUATION_LIMIT - used
    finally:
        await runtime.shutdown()
