"""Background jobs a reply did not collect get a follow-up turn of their own, which posts a new message."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.reply_scope import SpanHandle, SpanSlot, _current_slot
from mindroom.response_lifecycle import ResponseLifecycleCoordinator, _QueuedMessageState
from mindroom.tool_jobs.completion import ConversationKey, join_conversation_jobs
from mindroom.tool_jobs.follow_ups import (
    FOLLOW_UP_LIMIT,
    follow_up_envelope,
    follow_up_event,
    follow_up_event_id,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot
from tests.test_tool_job_reply_join import _job_context
from tests.tool_job_helpers import (
    completed_delegation_job,
    job_coordinator,
    job_owner,
    start_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import JournalEvent, PrincipalStore
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

pytestmark = pytest.mark.asyncio


def _key(owner: ToolExecutionIdentity) -> ConversationKey:
    assert owner.room_id is not None
    assert owner.requester_id is not None
    return ConversationKey(
        recipient=owner.recipient,
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
        requester_id=owner.requester_id,
        silent=False,
        participants=(owner.agent_name,),
    )


async def _ended(
    runtime: ToolJobRuntime,
    job_id: str,
    outcome: BackgroundOutcome,
    *,
    owner: ToolExecutionIdentity | None = None,
    source_event_id: str | None = None,
) -> None:
    """Start a job that ends with ``outcome`` at once, as the turn ``source_event_id`` started it."""

    async def operation() -> BackgroundOutcome:
        return outcome

    await start_job(
        runtime,
        job_id,
        tool_name=f"tool_{job_id}",
        depth=0,
        adapter={},
        owner=owner or job_owner(),
        operation=operation,
        source_event_id=source_event_id,
    )
    await wait_for_status(runtime, job_id, outcome.status)


def _admitting_bot() -> MagicMock:
    bot = MagicMock()
    bot.admit_job_follow_up = AsyncMock()
    return bot


async def test_jobs_ending_together_share_one_follow_up(tmp_path: Path) -> None:
    """Two jobs that ended in one conversation make one follow-up, and a later pass admits that same one."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    try:
        await _ended(runtime, "job_a", BackgroundOutcome("completed", "a"))
        await _ended(runtime, "job_b", BackgroundOutcome("failed", "b"))
        await coordinator._admit_follow_ups(runtime)
        await coordinator._admit_follow_ups(runtime)
        assert bot.admit_job_follow_up.await_count == 2
        first, second = (call.args for call in bot.admit_job_follow_up.await_args_list)
        assert first == second
        key, follow_up_id, streak = first
        assert key == _key(job_owner())
        jobs = [job for job in runtime.retained_jobs() if job.job_id in {"job_a", "job_b"}]
        assert follow_up_id == follow_up_event_id(key, jobs)
        assert streak == 1
    finally:
        await runtime.shutdown()


async def test_cancelled_jobs_get_no_follow_up(tmp_path: Path) -> None:
    """A cancelled job never posts a message of its own; the next notice or turn tells the agent."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    try:
        await _ended(runtime, "job_a", BackgroundOutcome("cancelled"))
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_not_awaited()
    finally:
        await runtime.shutdown()


async def test_a_silent_schedules_jobs_get_no_follow_up(tmp_path: Path) -> None:
    """Work a silent schedule started waits for its next silent run instead of posting a message."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)

    async def done() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "a")

    try:
        await start_job(
            runtime,
            "job_a",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=job_owner(),
            operation=done,
            source_kind=SILENT_SCHEDULE_SOURCE_KIND,
        )
        await wait_for_status(runtime, "job_a", "completed")
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_not_awaited()
    finally:
        await runtime.shutdown()


async def test_jobs_the_requester_may_no_longer_access_get_no_follow_up(tmp_path: Path) -> None:
    """A job whose requester lost access to the agent is reported to nobody."""
    allowed = True
    runtime = await tool_job_runtime(tmp_path, authorize=lambda _job: allowed)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    try:
        await _ended(runtime, "job_a", BackgroundOutcome("completed", "a"))
        allowed = False
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_not_awaited()
    finally:
        await runtime.shutdown()


async def test_a_job_ending_after_the_last_step_still_gets_its_follow_up(tmp_path: Path) -> None:
    """A job that ends while a reply runs, after its model's last step, is followed up; the reply never saw it."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    bot.has_active_response_for_target = MagicMock(return_value=True)
    coordinator = job_coordinator(tmp_path, runtime, bot)
    try:
        await _ended(runtime, "job_a", BackgroundOutcome("completed", "a"))
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_awaited_once()
    finally:
        await runtime.shutdown()


async def test_follow_ups_stop_after_twenty_in_a_row_without_a_human_message(tmp_path: Path) -> None:
    """Work a follow-up turn started counts one more in a row; work a human message started starts over."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    assert coordinator._journal is not None
    principal = coordinator._journal.principal("@mindroom_parent:test")
    bot.journal_principal = MagicMock(return_value=principal)
    owner = job_owner()
    key = _key(owner)
    try:
        last = follow_up_event(key, "job-follow-up:last", sender_id="@mindroom_parent:test", now_ms=1, streak=20)
        await principal.admit(last)
        await _ended(runtime, "from_the_last", BackgroundOutcome("completed", "a"), source_event_id=last.event_id)
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_not_awaited()

        before = follow_up_event(key, "job-follow-up:before", sender_id="@mindroom_parent:test", now_ms=1, streak=19)
        await principal.admit(before)
        other = replace(owner, requester_id="@bob:example.org")
        await _ended(
            runtime,
            "from_the_one_before",
            BackgroundOutcome("completed", "b"),
            owner=other,
            source_event_id=before.event_id,
        )
        await _ended(
            runtime,
            "from_a_human",
            BackgroundOutcome("completed", "c"),
            owner=replace(owner, requester_id="@carol:example.org"),
            source_event_id="$human",
        )
        await coordinator._admit_follow_ups(runtime)
        streaks = {call.args[0].requester_id: call.args[2] for call in bot.admit_job_follow_up.await_args_list}
        assert streaks == {"@bob:example.org": FOLLOW_UP_LIMIT, "@carol:example.org": 1}
    finally:
        await runtime.shutdown()


def _runner_key(bot: AgentBot) -> ConversationKey:
    return ConversationKey(
        recipient=bot.agent_name,
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        silent=False,
        participants=(bot.agent_name,),
    )


def _runner_owner(key: ConversationKey) -> ToolExecutionIdentity:
    return replace(
        job_owner(),
        agent_name=key.recipient,
        requester_id=key.requester_id,
        room_id=key.room_id,
        thread_id=key.thread_id,
        resolved_thread_id=key.thread_id,
    )


async def _follow_up(principal: PrincipalStore, key: ConversationKey, follow_up_id: str) -> JournalEvent:
    await principal.admit(
        follow_up_event(key, follow_up_id, sender_id="@mindroom_general:localhost", now_ms=1, streak=1),
    )
    event = await principal.load_event(follow_up_id)
    assert event is not None
    return event


async def _runner_runtime(bot: AgentBot, tmp_path: Path) -> ToolJobRuntime:
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)
    return runtime


async def test_a_job_ending_with_no_reply_running_posts_one_follow_up(tmp_path: Path) -> None:
    """A follow-up runs an ordinary turn that posts a new reply, asking the model to collect what ended."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=_runner_owner(key))
        event = await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        runner.generate_response = AsyncMock(return_value=None)
        await runner._run_job_follow_up(event)
        request = runner.generate_response.await_args.args[0]
        assert request.existing_event_id is None
        assert request.sources.pending_event_ids == ("job-follow-up:1",)
        prepared = await runner._follow_up_under_lock(request)
        assert prepared is not None
        assert "`work` (tool_work) completed" in prepared.prompt
        assert 'job(action="collect")' in prepared.prompt
        assert prepared.response_envelope.body == prepared.prompt
        assert prepared.asked_job_ids == ("work",)
    finally:
        await runtime.shutdown()


async def test_a_follow_up_waits_for_the_running_reply_and_skips_what_it_collected(tmp_path: Path) -> None:
    """Under the conversation lock the follow-up runs nothing once the reply before it collected the jobs."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    owner = _runner_owner(key)
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=owner)
        event = await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        runner.generate_response = AsyncMock(return_value=None)
        await runner._run_job_follow_up(event)
        request = runner.generate_response.await_args.args[0]
        collected = await runtime.wait("work", owner=owner, depth=0, timeout=0)
        await runtime.acknowledge_wait("work", collected.claim, source_event_id="$reply")
        assert await runner._follow_up_under_lock(request) is None
        # Nothing claimed a reply for it, so its source settles.
        assert not await bot.journal_principal().is_pending("job-follow-up:1")
    finally:
        await runtime.shutdown()


async def test_an_interrupted_job_gets_a_follow_up_saying_it_may_have_partly_run(tmp_path: Path) -> None:
    """A job a restart interrupted is followed up as interrupted, warning that it may have partly run."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(
            runtime,
            "work",
            BackgroundOutcome("interrupted", "Restart interrupted the job."),
            owner=_runner_owner(key),
        )
        event = await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        runner.generate_response = AsyncMock(return_value=None)
        await runner._run_job_follow_up(event)
        prepared = await runner._follow_up_under_lock(runner.generate_response.await_args.args[0])
        assert prepared is not None
        assert "`work` (tool_work) interrupted" in prepared.prompt
        assert "partly run" in prepared.prompt
    finally:
        await runtime.shutdown()


async def test_a_follow_up_never_releases_a_running_wait() -> None:
    """A follow-up queued behind a running reply leaves that reply's tool waits alone; only a human message ends them."""
    coordinator = ResponseLifecycleCoordinator()
    signal = _QueuedMessageState()
    released = asyncio.Event()
    signal.turn_signal.subscribe(released.set)
    signal.begin_response_turn()
    key = ConversationKey(
        "general",
        "!room:localhost",
        "$thread",
        "@user:localhost",
        silent=False,
        participants=("general",),
    )
    notice = coordinator._begin_response_turn_notice(
        lifecycle_lock=asyncio.Lock(),
        queued_signal=signal,
        response_envelope=follow_up_envelope(
            key,
            follow_up_id="job-follow-up:1",
            sender_id="@mindroom_general:localhost",
            prompt="ended",
        ),
        signal_queued_message=True,
    )
    assert notice is None
    assert not released.is_set()


async def test_a_reply_never_waits(tmp_path: Path) -> None:
    """A reply leaving running work behind ends normally: its span records no work to wait for."""
    owner = completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = _job_context(tmp_path, owner)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)

    async def forever() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    try:
        await start_job(runtime, "running", tool_name="tool", depth=0, adapter={}, owner=owner, operation=forever)
        handle = MagicMock(spec=SpanHandle)
        handle.leaves_work = None
        token = _current_slot.set(SpanSlot(handle=handle))
        try:
            with tool_runtime_context(context):
                join = await join_conversation_jobs(set(), joins=0)
        finally:
            _current_slot.reset(token)
        assert join.prompt is None
        assert handle.leaves_work is None
    finally:
        await runtime.shutdown()
