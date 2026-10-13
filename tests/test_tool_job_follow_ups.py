"""Background jobs a reply did not collect get a follow-up turn of their own, which posts a new message."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.approval.response import continuation_target
from mindroom.config.agent import TeamConfig
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.reply_presentation import Presentation, encode_presentation
from mindroom.reply_scope import SpanHandle, SpanSlot, _current_slot
from mindroom.response_lifecycle import ResponseLifecycleCoordinator, _QueuedMessageState
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedAttempt, TurnSinks, run_blocking_response_turn
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import (
    _FOLLOW_UP_LIMIT,
    _follow_up_event_id,
    _follow_up_streak,
    follow_up_envelope,
    follow_up_event,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.approval_continuation_helpers import approval_continuation
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import (
    install_generate_response_mock,
    message_origin,
    patch_response_runner_module,
    unwrap_extracted_collaborator,
)
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot, _envelope, _noop_typing, _target
from tests.test_response_turn import _AdapterLog, _blocking_adapter
from tests.test_response_turn import _continuation as _turn_continuation
from tests.test_response_turn import _ctx as _turn_ctx
from tests.tool_job_helpers import (
    completed_delegation_job,
    job_context,
    job_coordinator,
    job_owner,
    start_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import JournalEvent, PrincipalStore
    from mindroom.response_turn import DynamicContinuationRunState, TurnRunState
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
    """Two jobs that ended in one conversation make one follow-up."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    try:
        await _ended(runtime, "job_a", BackgroundOutcome("completed", "a"))
        await _ended(runtime, "job_b", BackgroundOutcome("failed", "b"))
        await coordinator._admit_follow_ups(runtime)
        key, follow_up_id = bot.admit_job_follow_up.await_args.args
        assert key == _key(job_owner())
        jobs = [job for job in runtime.retained_jobs() if job.job_id in {"job_a", "job_b"}]
        assert follow_up_id == _follow_up_event_id(key, jobs, 1)
        assert _follow_up_streak(follow_up_id) == 1
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
        last = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT)
        await _ended(runtime, "from_the_last", BackgroundOutcome("completed", "a"), source_event_id=last)
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_not_awaited()

        before = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT - 1)
        other = replace(owner, requester_id="@bob:example.org")
        await _ended(
            runtime,
            "from_the_one_before",
            BackgroundOutcome("completed", "b"),
            owner=other,
            source_event_id=before,
        )
        await _ended(
            runtime,
            "from_a_human",
            BackgroundOutcome("completed", "c"),
            owner=replace(owner, requester_id="@carol:example.org"),
            source_event_id="$human",
        )
        await coordinator._admit_follow_ups(runtime)
        streaks = {
            call.args[0].requester_id: _follow_up_streak(call.args[1])
            for call in bot.admit_job_follow_up.await_args_list
        }
        assert streaks == {"@bob:example.org": _FOLLOW_UP_LIMIT, "@carol:example.org": 1}
    finally:
        await runtime.shutdown()


async def test_a_job_a_finished_follow_up_started_gets_its_own_follow_up(tmp_path: Path) -> None:
    """A follow-up's turn that started work and settled still counts once, so that work is followed up in turn."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    assert coordinator._journal is not None
    principal = coordinator._journal.principal("@mindroom_parent:test")
    bot.journal_principal = MagicMock(return_value=principal)
    try:
        await _ended(runtime, "first", BackgroundOutcome("completed", "a"), source_event_id="$human")
        await coordinator._admit_follow_ups(runtime)
        first_key, first_id, *_ = bot.admit_job_follow_up.await_args.args
        await principal.admit(follow_up_event(first_key, first_id, sender_id="@mindroom_parent:test", now_ms=1))
        collected = await runtime.wait("first", owner=job_owner(), depth=0, timeout=0)
        await runtime.acknowledge_wait("first", collected.claim)
        await principal.settle(first_id)
        await _ended(runtime, "second", BackgroundOutcome("completed", "b"), source_event_id=first_id)
        await coordinator._admit_follow_ups(runtime)
        assert bot.admit_job_follow_up.await_count == 2
        _key_arg, second_id = bot.admit_job_follow_up.await_args.args
        assert _follow_up_streak(second_id) == 2
    finally:
        await runtime.shutdown()


async def test_a_capped_job_does_not_hold_back_a_later_one(tmp_path: Path) -> None:
    """A job past the follow-up limit waits for a human message; a job a human message started is still followed up."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    assert coordinator._journal is not None
    principal = coordinator._journal.principal("@mindroom_parent:test")
    bot.journal_principal = MagicMock(return_value=principal)
    key = _key(job_owner())
    try:
        last = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT)
        await _ended(runtime, "capped", BackgroundOutcome("completed", "a"), source_event_id=last)
        await _ended(runtime, "fresh", BackgroundOutcome("completed", "b"), source_event_id="$human")
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_awaited_once()
        _key_arg, follow_up_id = bot.admit_job_follow_up.await_args.args
        fresh = [job for job in runtime.retained_jobs() if job.job_id == "fresh"]
        assert follow_up_id == _follow_up_event_id(key, fresh, 1)
    finally:
        await runtime.shutdown()


async def test_an_admitted_follow_up_is_not_admitted_again(tmp_path: Path) -> None:
    """Later passes over the same ended jobs leave the journal alone."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    try:
        await _ended(runtime, "job_a", BackgroundOutcome("completed", "a"))
        await coordinator._admit_follow_ups(runtime)
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_awaited_once()
    finally:
        await runtime.shutdown()


async def test_later_passes_do_not_recheck_access_for_jobs_already_followed_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ended jobs waiting on an admitted follow-up, or on nothing, cost later passes no access checks."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    try:
        await _ended(runtime, "job_a", BackgroundOutcome("completed", "a"))
        await _ended(
            runtime,
            "job_b",
            BackgroundOutcome("cancelled"),
            owner=replace(job_owner(), requester_id="@bob:test"),
        )
        await coordinator._admit_follow_ups(runtime)
        checks = AsyncMock(wraps=runtime.ended_jobs)
        monkeypatch.setattr(runtime, "ended_jobs", checks)
        await coordinator._admit_follow_ups(runtime)
        checks.assert_not_awaited()
        bot.admit_job_follow_up.assert_awaited_once()
    finally:
        await runtime.shutdown()


async def test_the_admission_memo_forgets_follow_ups_whose_jobs_were_collected(tmp_path: Path) -> None:
    """A long-running process keeps only the follow-ups of jobs still waiting to be reported."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    try:
        await _ended(runtime, "job_a", BackgroundOutcome("completed", "a"))
        await coordinator._admit_follow_ups(runtime)
        assert coordinator._admitted_follow_ups
        collected = await runtime.wait("job_a", owner=job_owner(), depth=0, timeout=0)
        await runtime.acknowledge_wait("job_a", collected.claim)
        await coordinator._admit_follow_ups(runtime)
        assert coordinator._admitted_follow_ups == set()
    finally:
        await runtime.shutdown()


def _runner_key(bot: AgentBot) -> ConversationKey:
    return ConversationKey(
        recipient=bot.agent_name,
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        silent=False,
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
        follow_up_event(key, follow_up_id, sender_id="@mindroom_general:localhost", now_ms=1),
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
        await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        request = runner._job_follow_up_request(
            key,
            "job-follow-up:1",
            claimed=asyncio.Event(),
            handoff=asyncio.Event(),
        )
        assert request.existing_event_id is None
        assert request.sources.pending_event_ids == ("job-follow-up:1",)
        assert await runner._follow_up_due(request)
        # The turn's start note lists the jobs; its prompt asks for one collect.
        assert request.prompt == bot.config.get_prompt("BACKGROUND_JOBS_FOLLOW_UP_PROMPT")
        assert 'job(action="collect")' in request.prompt
        assert request.response_envelope.body == request.prompt
    finally:
        await runtime.shutdown()


async def test_a_follow_up_runs_a_model_turn_that_posts_its_reply(tmp_path: Path) -> None:
    """The follow-up's turn reaches the model with its prompt and posts what the model answers."""
    bot = _bot(tmp_path)
    await bot._reply_runtime.start()
    unique_room_send_responses(bot.client)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    runtime = await _runner_runtime(bot, tmp_path)
    prompts: list[str] = []

    async def stream(*_args: object, **kwargs: object) -> AsyncIterator[str]:
        prompts.append(str(kwargs["prompt"]))
        yield "The job finished."

    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=_runner_owner(key))
        event = await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        with patch_response_runner_module(
            stream_agent_response=stream,
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await runner._run_job_follow_up(event)
        assert prompts == [bot.config.get_prompt("BACKGROUND_JOBS_FOLLOW_UP_PROMPT")]
        reply = await bot._reply_runtime.store.replies.for_sources(("job-follow-up:1",))
        assert reply is not None
        assert reply.state is rl.ReplyState.COMPLETED
    finally:
        await runtime.shutdown()


async def test_a_teams_follow_up_runs_as_the_team(tmp_path: Path) -> None:
    """A configured team answers its follow-up with its members and mode, as the entity that owns their jobs."""
    bot = _bot(tmp_path)
    bot.config.teams["crew"] = TeamConfig(display_name="Crew", role="Crew", agents=["general"], mode="collaborate")
    persist_entity_accounts(bot.config, bot.runtime_paths)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runner.deps = replace(runner.deps, agent_name="crew")
    team_response = AsyncMock(return_value=None)
    key = replace(_runner_key(bot), recipient="crew")
    request = runner._job_follow_up_request(key, "job-follow-up:1", claimed=asyncio.Event(), handoff=asyncio.Event())
    with patch.object(runner, "generate_team_response_helper", team_response):
        await runner._generate_job_follow_up(request)
    team_response.assert_awaited_once()
    assert team_response.await_args.args == (request,)
    members = team_response.await_args.kwargs["team_agents"]
    assert [member.full_id for member in members] == ["@mindroom_general:localhost"]
    assert team_response.await_args.kwargs["team_mode"] == "collaborate"


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
        collected = await runtime.wait("work", owner=owner, depth=0, timeout=0)
        await runtime.acknowledge_wait("work", collected.claim)
        request = runner._job_follow_up_request(
            key,
            "job-follow-up:1",
            claimed=asyncio.Event(),
            handoff=asyncio.Event(),
        )
        assert not await runner._follow_up_due(request)
        # Nothing claimed a reply for it, so its source settles.
        install_generate_response_mock(bot, AsyncMock(return_value=None))
        await runner._run_job_follow_up(event)
        assert not await bot.journal_principal().is_pending("job-follow-up:1")
    finally:
        await runtime.shutdown()


async def test_a_follow_up_is_not_due_for_jobs_past_the_limit(tmp_path: Path) -> None:
    """Under the lock, a follow-up whose own job was collected does not report jobs the limit holds back."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(
            runtime,
            "capped",
            BackgroundOutcome("completed", "done"),
            owner=_runner_owner(key),
            source_event_id=_follow_up_event_id(key, [], _FOLLOW_UP_LIMIT),
        )
        request = runner._job_follow_up_request(
            key,
            "job-follow-up:1",
            claimed=asyncio.Event(),
            handoff=asyncio.Event(),
        )
        assert not await runner._follow_up_due(request)
    finally:
        await runtime.shutdown()


async def test_a_follow_up_a_restart_cut_short_answers_again(tmp_path: Path) -> None:
    """A follow-up whose reply a restart cut short runs again, though that reply already collected its jobs."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    owner = _runner_owner(key)
    runtime = await _runner_runtime(bot, tmp_path)
    principal = bot.journal_principal()
    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=owner)
        await _follow_up(principal, key, "job-follow-up:1")
        await principal.replies.write_generation("gen-1")
        claimed = (
            await principal.replies.claim(
                rl.ClaimRequest(
                    span_id="span-1",
                    delivery_id="job-follow-up:1",
                    sources=ResponseSources(("job-follow-up:1",), ("job-follow-up:1",)),
                    bot_generation="gen-1",
                    now_ns=10,
                    new_reply_id="reply-1",
                    entity_name=key.recipient,
                    room_id=key.room_id,
                    thread_id=key.thread_id,
                    membership_epoch=0,
                    empty_presentation=encode_presentation(Presentation()),
                ),
            )
        ).transition
        assert claimed.reply is not None
        collected = await runtime.wait("work", owner=owner, depth=0, timeout=0)
        await runtime.acknowledge_wait("work", collected.claim)
        request = runner._job_follow_up_request(
            key,
            "job-follow-up:1",
            claimed=asyncio.Event(),
            handoff=asyncio.Event(),
        )
        assert await runner._follow_up_due(request)
    finally:
        await runtime.shutdown()


async def test_an_interrupted_job_gets_a_follow_up_saying_it_may_have_partly_run(tmp_path: Path) -> None:
    """A job a restart interrupted gets a follow-up like a finished one."""
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
        await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        request = runner._job_follow_up_request(
            key,
            "job-follow-up:1",
            claimed=asyncio.Event(),
            handoff=asyncio.Event(),
        )
        # The follow-up's start note says the interrupted job may have partly run.
        assert await runner._follow_up_due(request)
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("turn", ["follow_up", "scheduled"])
async def test_only_a_human_message_releases_a_running_wait(turn: str) -> None:
    """A follow-up or a scheduled turn queued behind a running reply leaves its tool waits alone."""
    coordinator = ResponseLifecycleCoordinator()
    queued = _QueuedMessageState()
    queued.begin_response_turn()
    key = ConversationKey("general", "!room:localhost", "$thread", "@user:localhost", silent=False)
    envelope = (
        follow_up_envelope(key, follow_up_id="job-follow-up:1", sender_id="@mindroom_general:localhost", prompt="")
        if turn == "follow_up"
        else _envelope(_target(thread_id="$thread"))
    )
    notice = coordinator._begin_response_turn_notice(
        lifecycle_lock=asyncio.Lock(),
        queued_signal=queued,
        response_envelope=envelope,
        # A scheduled turn is no human message for the running reply to hear about.
        signal_queued_message=turn == "follow_up",
    )
    assert notice is None
    assert not queued.has_pending_human_messages()


async def test_a_reply_never_waits(tmp_path: Path) -> None:
    """A reply leaving running work behind ends normally: its span records no work to wait for."""
    owner = completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = job_context(tmp_path, owner)
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

        async def attempt(_run: TurnRunState, _continuation: DynamicContinuationRunState) -> CompletedAttempt:
            return CompletedAttempt(response_text="answer", replayable_text="answer", has_visible_content=True)

        try:
            with tool_runtime_context(context):
                text = await run_blocking_response_turn(
                    _turn_ctx(background_tool_jobs=True),
                    _blocking_adapter(_AdapterLog(), attempt),
                    TurnSinks(),
                    continuation=_turn_continuation(),
                )
        finally:
            _current_slot.reset(token)
        assert text == "answer"
        assert handle.leaves_work is None
    finally:
        await runtime.shutdown()


async def test_a_ready_result_at_the_boundary_does_not_continue_the_turn(tmp_path: Path) -> None:
    """A job that ended before the reply finished leaves the reply as it is; its follow-up reports it."""
    owner = completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = job_context(tmp_path, owner)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    attempts = 0

    async def attempt(_run: TurnRunState, _continuation: DynamicContinuationRunState) -> CompletedAttempt:
        nonlocal attempts
        attempts += 1
        return CompletedAttempt(response_text="answer", replayable_text="answer", has_visible_content=True)

    try:
        await _ended(runtime, "ready", BackgroundOutcome("completed", "done"), owner=owner)
        with tool_runtime_context(context):
            text = await run_blocking_response_turn(
                _turn_ctx(background_tool_jobs=True),
                _blocking_adapter(_AdapterLog(), attempt),
                TurnSinks(),
                continuation=_turn_continuation(),
            )
        assert (text, attempts) == ("answer", 1)
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_awaited_once()
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("follow_up", [False, True])
async def test_a_follow_ups_resumed_approval_replies_to_no_event(*, follow_up: bool) -> None:
    """A follow-up is a runtime source, not a Matrix event, so its reply after an approval replies to nothing."""
    source = "job-follow-up:!room:localhost:abc:0" if follow_up else "$source"
    key = ConversationKey("general", "!room:localhost", "$thread", "@user:localhost", silent=False)
    origin = (
        follow_up_envelope(key, follow_up_id=source, sender_id="@mindroom_general:localhost", prompt="").origin
        if follow_up
        else message_origin(sender_id="@user:localhost")
    )
    continuation = approval_continuation(origin=origin, sources=ResponseSources((source,), (source,)))
    assert continuation_target(continuation, reply_to_event_id=source).reply_to_event_id == (
        None if follow_up else source
    )
