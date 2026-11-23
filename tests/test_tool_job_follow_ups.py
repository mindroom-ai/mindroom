"""Background jobs a reply did not collect get a follow-up turn of their own, which posts a new message."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import CONVERSATION_CONTENT_KEY, EXACT_MENTIONS_KEY
from mindroom.custom_tools.job import JobTools
from mindroom.dispatch_source import SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal.models import EventClass, EventKind, InboundEvent
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.reply_presentation import Presentation, encode_presentation
from mindroom.reply_scope import SpanHandle, SpanSlot, _current_slot
from mindroom.response_lifecycle import ResponseLifecycleCoordinator, _Conversation
from mindroom.response_sources import Owned, ResponseSources, Unclaimed, UnclaimedReason
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    TurnAdapter,
    TurnContinuationRunState,
    TurnSinks,
    stream_response_turn,
)
from mindroom.session_storage.store import ConversationStore
from mindroom.thread_utils import history_mentioned_user_ids
from mindroom.tool_jobs.client_cancel import JobCancelRequest, cancel_jobs_command, cancel_room_jobs
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import (
    _FOLLOW_UP_LIMIT,
    _follow_up_event_id,
    _follow_up_streak,
    follow_up_envelope,
    follow_up_event,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.notices import JobNotices, _turn_start_job_note, job_notice_context
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.bot_helpers import unique_room_send_responses
from tests.conftest import (
    make_turn_context,
    message_origin,
    patch_response_runner_module,
    unwrap_extracted_collaborator,
)
from tests.delegation_helpers import _runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.response_runner_helpers import _bot, _envelope, _noop_typing, _target
from tests.tool_job_helpers import (
    awaiting_approval,
    completed_delegation_job,
    job_context,
    job_coordinator,
    job_owner,
    start_job,
    tool_job_runtime,
    wait_for_status,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import JournalEvent, PrincipalStore
    from mindroom.response_runner import ResponseRunner
    from mindroom.response_turn import ResponseTurnContext, TurnRunState
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

pytestmark = pytest.mark.asyncio


def _mock_scope() -> ScopeSessionContext:
    return ScopeSessionContext(
        scope=HistoryScope(kind="agent", scope_id="general"),
        storage=MagicMock(spec=ConversationStore),
        session=None,
        session_id="session-1",
        storage_factory=lambda: MagicMock(spec=ConversationStore),
    )


async def _drive_jobs_turn(
    attempt: Callable[[TurnRunState, TurnContinuationRunState], AsyncIterator[AttemptResolved]],
    *,
    prompt: str = "hello",
    load_scope: Callable[[], ScopeSessionContext] = _mock_scope,
    **ctx_overrides: object,
) -> str:
    """Run one background-jobs turn of ``attempt`` through the turn driver and return its text."""
    adapter = TurnAdapter[str](
        load_scope=load_scope,
        run_attempt=attempt,
        make_text_chunk=str,
        user_message=prompt,
        unexpected_error_text=str,
    )
    continuation = TurnContinuationRunState.initial(
        prompt=prompt,
        model_prompt=None,
        current_timestamp_ms=None,
        current_prompt_is_structured=False,
        current_event_id=None,
        run_id="run-1",
        continuation_model_prompt_tail="",
    )
    ctx = replace(make_turn_context(**ctx_overrides), background_tool_jobs=True)
    return "".join(
        [chunk async for chunk in stream_response_turn(ctx, adapter, TurnSinks(), continuation=continuation)],
    )


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
    reply_id: str | None = None,
    tool_name: str | None = None,
    kind: str = "tool",
    adapter: dict[str, object] | None = None,
) -> None:
    """Start a job that ends with ``outcome`` at once, as the turn ``source_event_id`` started it."""

    async def operation() -> BackgroundOutcome:
        return outcome

    await start_job(
        runtime,
        job_id,
        tool_name=tool_name or f"tool_{job_id}",
        depth=0,
        kind=kind,
        adapter={} if adapter is None else adapter,
        owner=owner or job_owner(),
        operation=operation,
        source_event_id=source_event_id,
        reply_id=reply_id,
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


@pytest.mark.parametrize("route", ["jobs_bar", "cancel_all", "stop", "agent_left"])
async def test_no_cancel_starts_a_follow_up(tmp_path: Path, route: str) -> None:
    """Whoever cancelled already knows, so no cancel starts a model turn; the next notice or turn tells the agent.

    The routes: the jobs bar's ✕, its Stop all or `!cancel_job all`, a Stop or regenerating edit of the reply that
    started the jobs, and the agent leaving the room.
    """
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    owner = job_owner()
    assert owner.room_id is not None
    try:
        for job_id in ("job_a", "job_b"):
            await start_job(
                runtime,
                job_id,
                tool_name=f"tool_{job_id}",
                depth=0,
                adapter={},
                owner=owner,
                operation=partial(awaiting_approval, runtime, job_id),
                reply_id="reply",
            )
            await wait_for_status(runtime, job_id, "awaiting_approval")
        if route == "jobs_bar":
            await runtime.request_cancel("job_a")
            await runtime.request_cancel("job_b")
        elif route == "cancel_all":
            await cancel_jobs_command(
                runtime,
                room_id=owner.room_id,
                request=JobCancelRequest(job_id=None, thread_id=None),
                may_cancel=lambda _job: True,
            )
        elif route == "stop":
            await runtime.cancel_stopped_reply("reply")
        else:
            await cancel_room_jobs(runtime, agent_name=owner.recipient, room_id=owner.room_id)
        for job_id in ("job_a", "job_b"):
            await wait_for_status(runtime, job_id, "cancelled")
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
    runtime = await tool_job_runtime(tmp_path, grant=lambda _job: "allowed" if allowed else "denied")
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


_SEND = "mindroom.orchestration.tool_job_runtime.send_message_result"


async def test_jobs_the_limit_holds_get_one_notice_of_the_agent(tmp_path: Path) -> None:
    """Jobs only the limit holds post one notice, which later passes of the process do not post again."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    key = _key(job_owner())
    last = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT)
    try:
        with patch(_SEND, AsyncMock()) as send:
            await _ended(runtime, "capped", BackgroundOutcome("completed", "a"), source_event_id=last)
            await _ended(runtime, "cancelled", BackgroundOutcome("cancelled"), source_event_id=last)
            await coordinator._admit_follow_ups(runtime)
            await coordinator._admit_follow_ups(runtime)
            bot.admit_job_follow_up.assert_not_awaited()
            send.assert_awaited_once()
            client, room_id, content = send.await_args.args
            assert (client, room_id) == (bot.client, key.room_id)
            assert send.await_args.kwargs["operation"] == "send_job_hold"
            assert send.await_args.kwargs["transaction_id"] == _follow_up_event_id(
                key,
                [job for job in runtime.retained_jobs() if job.job_id == "capped"],
                0,
            )
            assert content["msgtype"] == "m.notice"
            assert content[CONVERSATION_CONTENT_KEY] is True
            assert content[EXACT_MENTIONS_KEY] is True
            assert content["m.mentions"] == {"user_ids": []}
            assert content["m.relates_to"]["event_id"] == key.thread_id
            assert content["body"] == (
                "⏸️ 1 background job finished while I was paused after "
                "20 automatic follow-ups in a row without a message from you: `tool_capped` (capped). "
                "Reply here to let me continue."
            )
    finally:
        await runtime.shutdown()


async def test_a_room_level_hold_is_a_conversation_notice(tmp_path: Path) -> None:
    """Outside a thread the notice is marked as a conversation, so thread lists show it, and counts every held job."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    owner = replace(job_owner(), resolved_thread_id=None)
    last = _follow_up_event_id(_key(owner), [], _FOLLOW_UP_LIMIT)
    try:
        with patch(_SEND, AsyncMock()) as send:
            for job_id in ("capped_a", "capped_b"):
                await _ended(runtime, job_id, BackgroundOutcome("completed", "a"), owner=owner, source_event_id=last)
            await coordinator._admit_follow_ups(runtime)
            content = send.await_args.args[2]
            assert "m.relates_to" not in content
            assert content[CONVERSATION_CONTENT_KEY] is True
            assert content["body"].startswith("⏸️ 2 background jobs finished")
            assert "`tool_capped_a` (capped_a), `tool_capped_b` (capped_b)" in content["body"]
    finally:
        await runtime.shutdown()


async def test_a_job_the_requester_may_not_access_does_not_hide_the_notice(tmp_path: Path) -> None:
    """A below-limit job whose grant went after its follow-up was admitted leaves the held job's notice to be sent."""
    allowed = True
    runtime = await tool_job_runtime(
        tmp_path,
        grant=lambda job: "allowed" if allowed or job.job_id == "capped" else "denied",
    )
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    key = _key(job_owner())
    try:
        with patch(_SEND, AsyncMock()) as send:
            await _ended(runtime, "denied", BackgroundOutcome("completed", "b"), source_event_id="$human")
            await coordinator._admit_follow_ups(runtime)
            bot.admit_job_follow_up.assert_awaited_once()
            allowed = False
            last = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT)
            await _ended(runtime, "capped", BackgroundOutcome("completed", "a"), source_event_id=last)
            await coordinator._admit_follow_ups(runtime)
            await coordinator._admit_follow_ups(runtime)
            bot.admit_job_follow_up.assert_awaited_once()
            send.assert_awaited_once()
    finally:
        await runtime.shutdown()


async def test_a_notice_is_not_sent_again_after_its_jobs_were_out_of_reach_for_a_while(tmp_path: Path) -> None:
    """A held job that is briefly out of reach, as while a waiter claims it, keeps its sent notice from repeating."""
    denied: set[str] = set()
    runtime = await tool_job_runtime(tmp_path, grant=lambda job: "denied" if job.job_id in denied else "allowed")
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    last = _follow_up_event_id(_key(job_owner()), [], _FOLLOW_UP_LIMIT)
    try:
        with patch(_SEND, AsyncMock()) as send:
            await _ended(runtime, "capped", BackgroundOutcome("completed", "a"), source_event_id=last)
            await _ended(runtime, "unreachable", BackgroundOutcome("completed", "c"), source_event_id=last)
            denied.add("unreachable")
            await coordinator._admit_follow_ups(runtime)
            denied.add("capped")
            await coordinator._admit_follow_ups(runtime)
            denied.discard("capped")
            await coordinator._admit_follow_ups(runtime)
            send.assert_awaited_once()
    finally:
        await runtime.shutdown()


async def test_a_human_reply_collects_held_jobs_and_ends_their_notice(tmp_path: Path) -> None:
    """A reply that collects held jobs leaves no notice to repeat; a job a human message started gets a follow-up."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    key = _key(job_owner())
    try:
        with patch(_SEND, AsyncMock()) as send:
            await _ended(
                runtime,
                "capped",
                BackgroundOutcome("completed", "a"),
                source_event_id=_follow_up_event_id(key, [], _FOLLOW_UP_LIMIT),
            )
            await coordinator._admit_follow_ups(runtime)
            send.assert_awaited_once()
            collected = await runtime.wait("capped", owner=job_owner(), depth=0, timeout=0)
            await runtime.acknowledge_wait("capped", collected.claim)
            await _ended(runtime, "fresh", BackgroundOutcome("completed", "b"), source_event_id="$human")
            await coordinator._admit_follow_ups(runtime)
            send.assert_awaited_once()
            bot.admit_job_follow_up.assert_awaited_once()
            assert _follow_up_streak(bot.admit_job_follow_up.await_args.args[1]) == 1
    finally:
        await runtime.shutdown()


async def test_no_notice_while_a_follow_up_reports_the_jobs(tmp_path: Path) -> None:
    """Below the limit, and beside a job a follow-up reports, whose turn hears of the held one too, nothing is posted."""
    runtime = await tool_job_runtime(tmp_path)
    bot = _admitting_bot()
    coordinator = job_coordinator(tmp_path, runtime, bot)
    key = _key(job_owner())
    try:
        with patch(_SEND, AsyncMock()) as send:
            before = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT - 1)
            await _ended(runtime, "below", BackgroundOutcome("completed", "a"), source_event_id=before)
            last = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT)
            await _ended(runtime, "capped", BackgroundOutcome("completed", "b"), source_event_id=last)
            await coordinator._admit_follow_ups(runtime)
            bot.admit_job_follow_up.assert_awaited_once()
            send.assert_not_awaited()
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


async def _admit_message(principal: PrincipalStore, key: ConversationKey, source: str) -> None:
    await principal.admit(
        InboundEvent(
            event_id=source,
            room_id=key.room_id,
            thread_id=key.thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=key.requester_id,
            origin_server_ts=1,
            source={"content": {"msgtype": "m.text", "body": "hello"}},
        ),
    )


async def _claim_reply(
    principal: PrincipalStore,
    key: ConversationKey,
    source: str,
    *,
    generation: str = "gen-1",
) -> tuple[rl.Reply, rl.Span]:
    await principal.replies.write_generation(generation)
    claimed = (
        await principal.replies.claim(
            rl.ClaimRequest(
                span_id=f"{source}:{generation}",
                delivery_id=source,
                sources=ResponseSources((source,), (source,)),
                bot_generation=generation,
                now_ns=10,
                new_reply_id=f"reply:{source}",
                entity_name=key.recipient,
                room_id=key.room_id,
                thread_id=key.thread_id,
                membership_epoch=0,
                empty_presentation=encode_presentation(Presentation()),
            ),
        )
    ).transition
    assert claimed.reply is not None
    assert claimed.claimed is not None
    return claimed.reply, claimed.claimed


async def _end_span_for_replay(
    principal: PrincipalStore,
    span: rl.Span,
    outcome: Literal[rl.SpanOutcome.LOST, rl.SpanOutcome.RELEASED, rl.SpanOutcome.SUPERSEDED] = rl.SpanOutcome.LOST,
) -> None:
    if outcome is rl.SpanOutcome.LOST:
        await principal.replies.write_generation("gen-2")
        await principal.replies.owner_lost("gen-2", now_ns=20)
    else:
        await principal.replies.decide(
            reply_id=span.reply_id,
            span_id=span.span_id,
            decide=lambda reply, live: rl.release(reply, live, now_ns=20, outcome=outcome),
        )
    ended = await principal.replies.span(span.span_id)
    assert ended is not None
    assert ended.outcome is outcome
    reply = await principal.replies.load(span.reply_id)
    assert reply is not None
    assert reply.state is rl.ReplyState.ACTIVE
    assert reply.current_span_id is None
    assert await principal.is_pending(span.sources.pending_event_ids[0])


async def _runner_runtime(bot: AgentBot, tmp_path: Path) -> ToolJobRuntime:
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)
    return runtime


def _wait_for_next_turn(runner: ResponseRunner, key: ConversationKey, *, requester_id: str) -> None:
    """Leave a message of ``requester_id`` waiting in ``key``'s conversation, as one that arrived while a reply ran."""
    lifecycle = runner.lifecycle_coordinator
    target = _target(thread_id=key.thread_id, reply_to_event_id="$waiting")
    signal = lifecycle._conversation_for(target)
    signal.begin_response_turn()
    envelope = replace(
        _envelope(target, source_event_id="$waiting"),
        origin=message_origin(sender_id=requester_id, requester_id=requester_id),
    )
    assert lifecycle.reserve_waiting_human_message(target=target, response_envelope=envelope) is not None
    signal.finish_response_turn()


async def test_a_job_ending_with_no_reply_running_posts_one_follow_up(tmp_path: Path) -> None:
    """A follow-up runs an ordinary turn that posts a new reply, asking the model to collect what ended."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=_runner_owner(key))
        await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        request = runner._job_follow_up_request(key, "job-follow-up:1")
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
    await bot._take_over_replies()
    unique_room_send_responses(bot.client)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    runtime = await _runner_runtime(bot, tmp_path)
    prompts: list[str] = []

    async def stream(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        prompts.append(ctx.prompt)
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
    runner.locked.deps = replace(runner.deps, agent_name="crew")
    team_response = AsyncMock(return_value=Owned())
    key = replace(_runner_key(bot), recipient="crew")
    request = runner._job_follow_up_request(key, "job-follow-up:1")
    with patch.object(runner, "generate_team_response_helper", team_response):
        assert await runner._generate_job_follow_up(request) == Owned()
    team_response.assert_awaited_once()
    assert team_response.await_args.args == (request,)
    assert team_response.await_args.kwargs["member_names"] == ["general"]
    assert team_response.await_args.kwargs["team_mode"] == "collaborate"


async def test_a_follow_up_waits_for_the_running_reply_and_skips_what_it_collected(tmp_path: Path) -> None:
    """Under the conversation lock the follow-up runs nothing once the reply before it collected the jobs.

    A job cancelled meanwhile, which that reply did not collect, gets no model turn of its own either.
    """
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    owner = _runner_owner(key)
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=owner)
        event = await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        await _ended(runtime, "dropped", BackgroundOutcome("cancelled"), owner=owner)
        collected = await runtime.wait("work", owner=owner, depth=0, timeout=0)
        await runtime.acknowledge_wait("work", collected.claim)
        request = runner._job_follow_up_request(key, "job-follow-up:1")
        # Nothing claimed a reply for it, so its source settles.
        assert await runner._generate_job_follow_up(request) == Unclaimed(UnclaimedReason.NOT_DUE)
        await runner._run_job_follow_up(event)
        assert await bot._reply_runtime.store.replies.for_sources(("job-follow-up:1",)) is None
        assert not await bot.journal_principal().is_pending("job-follow-up:1")
    finally:
        await runtime.shutdown()


async def test_a_follow_up_gives_way_to_a_message_already_waiting(tmp_path: Path) -> None:
    """A follow-up posts nothing while a message waits for its conversation, whose reply reports the jobs instead."""
    bot = _bot(tmp_path)
    await bot._take_over_replies()
    unique_room_send_responses(bot.client)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    owner = _runner_owner(key)
    runtime = await _runner_runtime(bot, tmp_path)
    prompts: list[str] = []

    async def stream(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[str]:
        prompts.append(ctx.prompt)
        yield "The job finished."

    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=owner)
        event = await _follow_up(bot.journal_principal(), key, "job-follow-up:1")
        _wait_for_next_turn(runner, key, requester_id=key.requester_id)
        with patch_response_runner_module(
            stream_agent_response=stream,
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
        ):
            await runner._run_job_follow_up(event)
        assert prompts == []
        assert await bot._reply_runtime.store.replies.for_sources(("job-follow-up:1",)) is None
        assert not await bot.journal_principal().is_pending("job-follow-up:1")
        # The jobs wait for the waiting message's reply to collect them.
        assert [job.job_id for job in await runtime.ended_jobs(key)] == ["work"]
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("capped", [False, True])
@pytest.mark.parametrize("outcome", [rl.SpanOutcome.LOST, rl.SpanOutcome.RELEASED, rl.SpanOutcome.SUPERSEDED])
async def test_an_open_reply_holds_its_ended_jobs_until_it_ends(  # noqa: PLR0915 - Trace one reply through replay and settlement.
    tmp_path: Path,
    outcome: Literal[rl.SpanOutcome.LOST, rl.SpanOutcome.RELEASED, rl.SpanOutcome.SUPERSEDED],
    *,
    capped: bool,
) -> None:
    """A reply awaiting replay holds only its own jobs, even from the follow-up limit notice."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    principal = bot.journal_principal()
    admitting = _admitting_bot()
    admitting.journal_principal.return_value = principal
    source = _follow_up_event_id(key, [], _FOLLOW_UP_LIMIT) if capped else "$human"
    if capped:
        await _follow_up(principal, key, source)
    else:
        await _admit_message(principal, key, source)
    reply, span = await _claim_reply(principal, key, source)
    runtime = await _runner_runtime(bot, tmp_path)
    coordinator = job_coordinator(tmp_path, runtime, admitting)
    owner = _runner_owner(key)
    try:
        for job in ("a", "b"):
            await _ended(
                runtime,
                job,
                BackgroundOutcome("completed", job),
                owner=owner,
                source_event_id=source,
                reply_id=reply.reply_id,
            )
        await _end_span_for_replay(principal, span, outcome)
        parent_jobs = await runtime.ended_jobs(key)
        assert [job.job_id for job in parent_jobs] == ["a", "b"]
        with patch(_SEND, AsyncMock()) as send:
            await coordinator._admit_follow_ups(runtime)
            assert admitting.admit_job_follow_up.await_count == 0
            send.assert_not_awaited()
            if not capped:
                event = await _follow_up(principal, key, "job-follow-up:already-admitted")
                request = runner._job_follow_up_request(key, event.event_id)
                assert await runner._generate_job_follow_up(request) == Unclaimed(UnclaimedReason.NOT_DUE)

                await _ended(
                    runtime,
                    "orphan",
                    BackgroundOutcome("completed", "done"),
                    owner=owner,
                    reply_id="missing-reply",
                )
                assert await principal.replies.load("missing-reply") is None
                orphan = next(job for job in await runtime.ended_jobs(key) if job.job_id == "orphan")
                await coordinator._admit_follow_ups(runtime)
                admitting.admit_job_follow_up.assert_awaited_once_with(key, _follow_up_event_id(key, (orphan,), 1))
                collected = await runtime.wait("orphan", owner=owner, depth=0, timeout=0)
                await runtime.acknowledge_wait("orphan", collected.claim)

            replay, live = await _claim_reply(principal, key, source, generation="gen-2")
            assert replay.reply_id == reply.reply_id
            await coordinator._admit_follow_ups(runtime)
            assert admitting.admit_job_follow_up.await_count == (0 if capped else 1)
            send.assert_not_awaited()
            ended = await principal.replies.decide(
                reply_id=replay.reply_id,
                span_id=live.span_id,
                decide=lambda row, current: rl.suppress(row, current, reason="suppressed", now_ns=30),
            )
            assert ended.transition.reply is not None
            assert ended.transition.reply.terminal
            await coordinator._admit_follow_ups(runtime)
            if capped:
                send.assert_awaited_once()
                admitting.admit_job_follow_up.assert_not_awaited()
            else:
                assert admitting.admit_job_follow_up.await_count == 2
                assert admitting.admit_job_follow_up.await_args.args == (key, _follow_up_event_id(key, parent_jobs, 1))
                send.assert_not_awaited()
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("viewer", ["replay", "other", "no_span", "no_store"])
async def test_ended_jobs_of_a_lost_reply_are_visible_to_its_replay(tmp_path: Path, viewer: str) -> None:
    """Other turns with a reply store neither announce nor batch-collect a reply's held jobs."""
    bot = _bot(tmp_path)
    key = _runner_key(bot)
    principal = bot.journal_principal()
    owner = _runner_owner(key)
    await _admit_message(principal, key, "$human")
    reply, span = await _claim_reply(principal, key, "$human")
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(
            runtime,
            "work",
            BackgroundOutcome("completed", "the result"),
            owner=owner,
            reply_id=reply.reply_id,
        )
        await _end_span_for_replay(principal, span)
        context = replace(
            job_context(tmp_path, owner),
            runtime_paths=bot.runtime_paths,
            membership=None if viewer == "no_store" else principal,
        )
        async with bot._reply_runtime.span_scope() as slot:
            if viewer in {"replay", "other"}:
                source = "$human" if viewer == "replay" else "$other"
                if viewer == "other":
                    await _admit_message(principal, key, source)
                current, live = await _claim_reply(principal, key, source, generation="gen-2")
                assert (current.reply_id == reply.reply_id) == (viewer == "replay")
                slot.handle = SpanHandle(bot._reply_runtime, live, current, Presentation())
            with tool_runtime_context(context), job_notice_context(JobNotices()):
                note = await _turn_start_job_note()
                result = await JobTools(bot.runtime_paths).job(action="collect")
        if viewer in {"replay", "no_store"}:
            assert note is not None
            assert "work" in note
            assert 'job(action="collect")' in note
            assert "the result" in result
        else:
            assert note is None
            assert result == "No ended background jobs."
    finally:
        await runtime.shutdown()


async def test_a_paused_parent_reply_does_not_hold_its_ended_jobs(tmp_path: Path) -> None:
    """A reply waiting on approval still lets a follow-up report its jobs."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    principal = bot.journal_principal()
    admitting = _admitting_bot()
    admitting.journal_principal.return_value = principal
    await _admit_message(principal, key, "$human")
    reply, span = await _claim_reply(principal, key, "$human")
    paused = await principal.replies.decide(
        reply_id=reply.reply_id,
        span_id=span.span_id,
        decide=lambda row, live: rl.pause(
            row,
            live,
            rl.PauseWrite(row.presentation, row.revision, None),
            in_place=True,
            now_ns=20,
        ),
    )
    assert paused.transition.reply is not None
    assert paused.transition.reply.state is rl.ReplyState.PAUSED
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(
            runtime,
            "work",
            BackgroundOutcome("completed", "done"),
            owner=_runner_owner(key),
            reply_id=reply.reply_id,
        )
        await job_coordinator(tmp_path, runtime, admitting)._admit_follow_ups(runtime)
        admitting.admit_job_follow_up.assert_awaited_once()
        request = runner._job_follow_up_request(key, admitting.admit_job_follow_up.await_args.args[1])
        assert await runner._follow_up_due(request)
    finally:
        await runtime.shutdown()


async def test_a_follow_up_does_not_give_way_to_another_persons_message(tmp_path: Path) -> None:
    """Another person's waiting message gets a reply that lists only their jobs, so the follow-up still reports these."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    runtime = await _runner_runtime(bot, tmp_path)
    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=_runner_owner(key))
        _wait_for_next_turn(runner, key, requester_id="@other:localhost")
        request = runner._job_follow_up_request(key, "job-follow-up:1")
        assert await runner._follow_up_due(request)
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
        request = runner._job_follow_up_request(key, "job-follow-up:1")
        assert not await runner._follow_up_due(request)
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("waiting", [False, True])
async def test_a_follow_up_a_restart_cut_short_answers_again(tmp_path: Path, *, waiting: bool) -> None:
    """A follow-up can resume its own reply even when no jobs are available for a new reply."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    owner = _runner_owner(key)
    runtime = await _runner_runtime(bot, tmp_path)
    principal = bot.journal_principal()
    try:
        await _ended(runtime, "work", BackgroundOutcome("completed", "done"), owner=owner)
        await _follow_up(principal, key, "job-follow-up:1")
        _, span = await _claim_reply(principal, key, "job-follow-up:1")
        if not waiting:
            await _ended(
                runtime,
                "own",
                BackgroundOutcome("completed", "done"),
                owner=owner,
                source_event_id="job-follow-up:1",
                reply_id=span.reply_id,
            )
        await _end_span_for_replay(principal, span)
        collected = await runtime.wait("work", owner=owner, depth=0, timeout=0)
        await runtime.acknowledge_wait("work", collected.claim)
        if waiting:
            _wait_for_next_turn(runner, key, requester_id=key.requester_id)
            assert await runtime.ended_jobs(key) == ()
        else:
            assert [job.job_id for job in await runtime.ended_jobs(key)] == ["own"]
            another = runner._job_follow_up_request(key, "job-follow-up:2")
            assert not await runner._follow_up_due(another)
        request = runner._job_follow_up_request(key, "job-follow-up:1")
        assert (
            runner.lifecycle_coordinator.has_waiting_messages_from(
                request.response_envelope.target,
                key.requester_id,
            )
            is waiting
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
        request = runner._job_follow_up_request(key, "job-follow-up:1")
        # The follow-up's start note says the interrupted job may have partly run.
        assert await runner._follow_up_due(request)
    finally:
        await runtime.shutdown()


@pytest.mark.parametrize("turn", ["follow_up", "scheduled"])
async def test_only_a_human_message_releases_a_running_wait(turn: str) -> None:
    """A follow-up or a scheduled turn queued behind a running reply leaves its tool waits alone."""
    coordinator = ResponseLifecycleCoordinator()
    queued = _Conversation()
    queued.begin_response_turn()
    key = ConversationKey("general", "!room:localhost", "$thread", "@user:localhost", silent=False)
    envelope = (
        follow_up_envelope(key, follow_up_id="job-follow-up:1", sender_id="@mindroom_general:localhost", prompt="")
        if turn == "follow_up"
        else _envelope(_target(thread_id="$thread"))
    )
    notice = coordinator._begin_response_turn_notice(
        conversation=queued,
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

        async def attempt(
            _run: TurnRunState,
            _continuation: TurnContinuationRunState,
        ) -> AsyncIterator[AttemptResolved]:
            yield AttemptResolved(
                CompletedAttempt(response_text="answer", replayable_text="answer", has_visible_content=True),
            )

        try:
            with tool_runtime_context(context):
                text = await _drive_jobs_turn(attempt)
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

    async def attempt(_run: TurnRunState, _continuation: TurnContinuationRunState) -> AsyncIterator[AttemptResolved]:
        nonlocal attempts
        attempts += 1
        yield AttemptResolved(
            CompletedAttempt(response_text="answer", replayable_text="answer", has_visible_content=True),
        )

    try:
        await _ended(runtime, "ready", BackgroundOutcome("completed", "done"), owner=owner)
        with tool_runtime_context(context):
            text = await _drive_jobs_turn(attempt)
        assert (text, attempts) == ("answer", 1)
        await coordinator._admit_follow_ups(runtime)
        bot.admit_job_follow_up.assert_awaited_once()
    finally:
        await runtime.shutdown()


async def _post_held_jobs(tmp_path: Path, specs: list[tuple[str, str, str | None]]) -> dict[str, object]:
    runtime = await tool_job_runtime(tmp_path)
    coordinator = job_coordinator(tmp_path, runtime, _admitting_bot())
    last = _follow_up_event_id(_key(job_owner()), [], _FOLLOW_UP_LIMIT)
    try:
        with patch(_SEND, AsyncMock()) as send:
            for job_id, tool_name, title in specs:
                kind = "delegation" if title is not None else "tool"
                adapter = {"child": {"title": title}} if title is not None else None
                await _ended(
                    runtime,
                    job_id,
                    BackgroundOutcome("completed", "done"),
                    source_event_id=last,
                    tool_name=tool_name,
                    kind=kind,
                    adapter=adapter,
                )
            await coordinator._admit_follow_ups(runtime)
            send.assert_awaited_once()
            return send.await_args.args[2]
    finally:
        await runtime.shutdown()


async def test_held_notice_names_subagent_title_caps_list_and_keeps_mentions_exact(tmp_path: Path) -> None:
    """Held notices list titles or tool names + 8-char IDs, cap at 5, and add no history mentions from titles."""
    specs = [
        ("c6256cac" + "1" * 56, "research", "Review @general design"),
        ("d4e5f607" + "2" * 56, "research", ""),
        *((f"{idx:08x}" + "a" * 56, f"run_tool_{idx}", None) for idx in range(5)),
    ]
    content = await _post_held_jobs(tmp_path, specs)
    assert (content["msgtype"], content[EXACT_MENTIONS_KEY], content["m.mentions"]) == (
        "m.notice",
        True,
        {"user_ids": []},
    )
    assert "`Review @general design` (c6256cac), `research` (d4e5f607)" in str(content["body"])
    assert ", and 2 more." in str(content["body"])

    config, paths = Config(agents={"general": AgentConfig(display_name="General")}), _runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    assert history_mentioned_user_ids(content, config, paths) == []


async def test_held_notice_escapes_markdown_html_and_backticks_in_titles(tmp_path: Path) -> None:
    """Markdown, HTML, and backticks in job titles render as literal code spans without links or formatting."""
    tricky = "**x** [a](https://e.x) <b>y</b> [@general](https://matrix.to/#/@mindroom_general:example.org) ``tick``"
    formatted = str((await _post_held_jobs(tmp_path, [("a1b2c3d4" + "0" * 56, "research", tricky)]))["formatted_body"])
    assert not any(tag in formatted for tag in ("<a ", "<strong>", "<b>"))
    assert (
        "<code>**x** [a](https://e.x) &lt;b&gt;y&lt;/b&gt; "
        "[@general](https://matrix.to/#/@mindroom_general:example.org) ``tick``</code>"
    ) in formatted
