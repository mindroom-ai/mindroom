"""Stop reaches active completion replies, descendant work, and deleted placeholders."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.event_journal import ApprovalCall, ApprovalContinuation, ApprovalDecisionMetadata
from mindroom.message_target import MessageTarget
from mindroom.response_sources import ResponseSources
from mindroom.tool_jobs.completion import completion_envelope, completion_event
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.turn_record import TurnRecord
from mindroom.user_stop_reconciliation import UserStopReconciler, UserStopReconcilerDeps
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request
from tests.test_event_journal_store import ROOM, admit
from tests.test_tool_job_completion import _persist_waiting_continuation
from tests.test_tool_job_stop import _bind_reply
from tests.test_user_stop_convergence import _CountingGateway
from tests.tool_job_helpers import JOB_TEST_TIMEOUT, job_owner, start_job, tool_job_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.response_runner import ResponseRequest, _EarlyPlaceholderState


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
async def test_stop_reaches_active_completion_and_its_descendant(tmp_path: Path, *, consumed: bool) -> None:  # noqa: PLR0915
    """Cancellation includes work already admitted from an older completion."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps.runtime.config.background_tool_jobs.enabled = True
    paths = runner.deps.runtime_paths
    store = runner.deps.approval_store
    await bot._turn_store.warm()
    await admit(store, "$first")
    await admit(store, "$follow-up")
    await _bind_reply(store, "$follow-up", "$reply", entity_name="general")
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = replace(
        job_owner(),
        agent_name="general",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )
    await bot._turn_store.record_turn(
        TurnRecord.create(
            ("$follow-up",),
            response_event_id="$reply",
            conversation_target=target,
            requester_id=owner.requester_id,
            response_owner="general",
            completed=False,
        ),
    )
    runtime = tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(runner.deps.runtime.config, paths)
    register_background_runtime(paths, runtime)
    started, release = asyncio.Event(), asyncio.Event()
    sent_after_stop = []
    response_task = None
    stop_task = None

    async def finished() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "prior result")

    async def child_operation() -> BackgroundOutcome:
        await asyncio.Event().wait()
        return BackgroundOutcome("completed", "unreachable")

    try:
        await start_job(
            runtime,
            "prior-job",
            tool_name="tool",
            depth=0,
            source_event_id="$first",
            adapter={},
            owner=owner,
            operation=finished,
        )
        ready = await runtime.wait("prior-job", owner=owner, depth=0)
        await runtime.release_wait("prior-job", ready.claim)
        event = completion_event(ready.job, sender_id=bot.matrix_id.full_id)
        await store.admit(event)
        request = replace(
            _plain_request(target, source_event_id=event.event_id),
            response_envelope=completion_envelope(ready.job, sender_id=bot.matrix_id.full_id),
        )

        async def active_completion(actual_target: MessageTarget, _early_placeholder: _EarlyPlaceholderState) -> str:
            runner.deps.stop_manager.set_current("$completion-reply", actual_target, asyncio.current_task())
            if consumed:
                consumed_wait = await runtime.wait("prior-job", owner=owner, depth=0)
                await runtime.acknowledge_wait("prior-job", consumed_wait.claim)
            await start_job(
                runtime,
                "completion-child",
                tool_name="tool",
                depth=0,
                source_event_id=event.event_id,
                adapter={},
                owner=owner,
                operation=child_operation,
            )
            started.set()
            await release.wait()
            sent_after_stop.append(await runtime.is_user_stopped("prior-job"))
            return "$completion-reply"

        response_task = runner.track_inbox_response(
            runner._run_locked_response_lifecycle(
                request,
                response_kind="test",
                locked_operation=active_completion,
                signal_queued_message=False,
            ),
            name="probe-completion",
            room_id=ROOM,
            recovery_proof_ready=lambda: True,
            source_event_ids=(event.event_id,),
        )
        await asyncio.wait_for(started.wait(), JOB_TEST_TIMEOUT)
        reconciler = UserStopReconciler(UserStopReconcilerDeps(bot._turn_store, runner, _CountingGateway()))
        stop_task = asyncio.create_task(reconciler.finalize("$reply", 100, AsyncMock()))
        assert await asyncio.wait_for(asyncio.shield(stop_task), 3)
        child = await runtime.lookup("completion-child", owner=owner, depth=0)
        completion_was_cancelled = response_task.cancelled()
        assert sent_after_stop == []
        assert completion_was_cancelled, "Stop did not cancel the already-admitted completion response"
        assert child.user_stop_receipt_order == 100, "Stop did not reach work started by the older job completion"
    finally:
        release.set()
        for task in (response_task, stop_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (response_task, stop_task) if task is not None), return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_stop_on_an_idle_completion_reply_finds_its_durable_turn(tmp_path: Path) -> None:
    """A completion's own reply is a turn like any other, so Stop on it settles durably and stops its jobs."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps.runtime.config.background_tool_jobs.enabled = True
    paths = runner.deps.runtime_paths
    store = runner.deps.approval_store
    await bot._turn_store.warm()
    # The human turn that started the job has finished, so its completion wakes an idle conversation.
    await admit(store, "$first")
    await store.settle("$first")
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = replace(
        job_owner(),
        agent_name="general",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )
    runtime = tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(runner.deps.runtime.config, paths)
    register_background_runtime(paths, runtime)
    replying = asyncio.Event()

    async def finished() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "prior result")

    async def wake_reply(request: ResponseRequest) -> str:
        try:
            # The visible reply is bound to its turn as soon as it exists, like any other response.
            if request.on_visible_response is not None:
                await request.on_visible_response("$wake-reply")
            await start_job(
                runtime,
                "wake-child",
                tool_name="tool",
                depth=0,
                source_event_id=event.event_id,
                adapter={},
                owner=owner,
                operation=asyncio.Event().wait,
            )
        finally:
            replying.set()
        await asyncio.Event().wait()
        raise AssertionError

    try:
        await start_job(
            runtime,
            "prior-job",
            tool_name="tool",
            depth=0,
            source_event_id="$first",
            adapter={},
            owner=owner,
            operation=finished,
        )
        ready = await runtime.wait("prior-job", owner=owner, depth=0)
        await runtime.release_wait("prior-job", ready.claim)
        event = completion_event(ready.job, sender_id=bot.matrix_id.full_id)
        await store.admit(event)
        with patch.object(runner, "generate_response", new=wake_reply):
            assert not await runner.handoff_tool_job_completion(await store.load_event(event.event_id))
            await asyncio.wait_for(replying.wait(), JOB_TEST_TIMEOUT)
            reconciler = UserStopReconciler(UserStopReconcilerDeps(bot._turn_store, runner, _CountingGateway()))
            assert await asyncio.wait_for(reconciler.finalize("$wake-reply", 100, AsyncMock()), JOB_TEST_TIMEOUT)
        stopped = bot._turn_store.turn_record_for_response_event_id("$wake-reply")
        assert stopped is not None
        assert (stopped.indexed_event_ids, stopped.user_stop_receipt_order) == ((event.event_id,), 100)
        child = await runtime.lookup("wake-child", owner=owner, depth=0)
        assert child.user_stop_receipt_order == 100
    finally:
        wakes = tuple(runner._inbox_response_tasks)
        for task in wakes:
            task.cancel()
        await asyncio.gather(*wakes, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_stop_after_placeholder_deletion_still_cancels_jobs(tmp_path: Path) -> None:
    """Retiring an empty placeholder cannot erase the Stop intent for its jobs."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps.runtime.config.background_tool_jobs.enabled = True
    paths = runner.deps.runtime_paths
    store = runner.deps.approval_store
    await bot._turn_store.warm()
    await admit(store, "$source")
    await _bind_reply(store, "$source", "$reply", entity_name="general")
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = replace(
        job_owner(),
        agent_name="general",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )
    await bot._turn_store.record_turn(
        TurnRecord.create(
            ("$source",),
            redacted_source_event_ids=("$source",),
            response_event_id=None,
            conversation_target=target,
            requester_id=owner.requester_id,
            response_owner="general",
            completed=False,
        ),
    )
    runtime = tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(runner.deps.runtime.config, paths)
    register_background_runtime(paths, runtime)

    class RetiredGateway(_CountingGateway):
        """Prove retirement of the original empty placeholder."""

        @asynccontextmanager
        async def user_stop_scope(self, response_event_id: str) -> AsyncIterator[str]:
            """Return the source whose initial delivery was retired."""
            del response_event_id
            yield "$source"

    async def active() -> BackgroundOutcome:
        await asyncio.Event().wait()
        return BackgroundOutcome("completed", "unreachable")

    try:
        await start_job(
            runtime,
            "active",
            tool_name="tool",
            depth=0,
            source_event_id="$source",
            adapter={},
            owner=owner,
            operation=active,
        )
        reconciler = UserStopReconciler(UserStopReconcilerDeps(bot._turn_store, runner, RetiredGateway()))
        assert await reconciler.finalize("$reply", 100, AsyncMock())
        job = await runtime.lookup("active", owner=owner, depth=0)
        turn = bot._turn_store.get_turn_record("$source")
        assert turn.user_stop_receipt_order == 100
        assert turn.response_event_id is None
        assert job.user_stop_receipt_order == 100
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_approval", [False, True])
async def test_stop_blocks_older_job_approval_owned_by_human_source(tmp_path: Path, *, owned_approval: bool) -> None:
    """Stopping a later reply also fences recovery and approvals of earlier managed work."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps.runtime.config.background_tool_jobs.enabled = True
    runner.deps.runtime.config.agents["general"].access.users = ["@user:localhost"]
    paths = runner.deps.runtime_paths
    store = runner.deps.approval_store
    await bot._turn_store.warm()
    await admit(store, "$first")
    await admit(store, "$follow-up")
    await _bind_reply(store, "$follow-up", "$reply", entity_name="general")
    target = MessageTarget.resolve(ROOM, "$thread", "$thread")
    owner = replace(
        job_owner(),
        agent_name="general",
        requester_id="@user:localhost",
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=target.session_id,
    )
    await bot._turn_store.record_turn(
        TurnRecord.create(
            ("$follow-up",),
            response_event_id="$reply",
            conversation_target=target,
            requester_id=owner.requester_id,
            response_owner="general",
            completed=False,
        ),
    )
    runtime = tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(runner.deps.runtime.config, paths)
    register_background_runtime(paths, runtime)

    async def awaiting() -> BackgroundOutcome:
        return BackgroundOutcome("awaiting_approval")

    try:
        await start_job(
            runtime,
            "older-approval-job",
            tool_name="delegate",
            depth=0,
            source_event_id="$first",
            adapter={},
            owner=owner,
            operation=awaiting,
        )
        waiting = await runtime.wait("older-approval-job", owner=owner, depth=0)
        await runtime.release_wait("older-approval-job", waiting.claim)
        original_request = _plain_request(target, source_event_id="$first")
        if owned_approval:
            continuation = ApprovalContinuation(
                approval_id="older-human-source-approval",
                run_id="run-paused",
                session_id=target.session_id,
                entity_kind="agent",
                entity_name="general",
                room_id=ROOM,
                thread_id=target.resolved_thread_id,
                requester_id=owner.requester_id,
                response_event_id="$older-waiting",
                sources=ResponseSources(("$first",), ("$first",)),
                calls=(
                    ApprovalCall(
                        tool_call_id="call-1",
                        tool_name="dangerous",
                        invoking_agent="general",
                        expires_at_ns=2**62,
                    ),
                ),
                state="waiting",
                runtime_generation=runner.deps.approval_runtime_generation,
                origin=original_request.response_envelope.origin,
                hook_source="message",
                requires_background_tool_jobs=True,
            )
            await _persist_waiting_continuation(
                store,
                principal_id=bot._journal_principal_id,
                continuation=continuation,
            )
            decision = await store.resolve_continuation_approval_card(
                card_event_id="$approval-card",
                requested_status="approved",
                reason=None,
                metadata=ApprovalDecisionMetadata(resolved_by=owner.requester_id),
            )
            assert decision.continuation_ready
        reconciler = UserStopReconciler(UserStopReconcilerDeps(bot._turn_store, runner, _CountingGateway()))
        assert await reconciler.finalize("$reply", 100, AsyncMock())
        assert await runtime.is_user_stopped("older-approval-job")
        execute = AsyncMock()
        resume = AsyncMock(return_value="$older-waiting")
        settled = AsyncMock(return_value=True)
        with (
            patch.object(runner, "_run_owned_approval_continuation", new=resume),
            patch.object(
                runner,
                "_settle_user_stopped_approval",
                new=settled,
            ),
        ):
            await runner._run_locked_response_lifecycle(
                original_request,
                response_kind="test",
                locked_operation=execute,
                signal_queued_message=False,
            )
        resume.assert_not_awaited()
        assert settled.await_count == int(owned_approval)
        execute.assert_not_awaited()
    finally:
        await runtime.shutdown()
