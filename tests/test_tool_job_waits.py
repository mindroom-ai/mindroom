"""Replies that wait for background work: the boundary's hold, the wakes the job runtime admits, and job Stops."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import EventClass, EventKind, InboundEvent, PrincipalStore
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.reply_presentation import NoteKind, Presentation, Segment, encode_presentation, note_segment
from mindroom.reply_scope import SpanHandle, SpanSlot, _current_slot
from mindroom.response_sources import ResponseSources
from mindroom.tool_jobs.completion import HoldKey, join_conversation_jobs
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_jobs.wakes import wake_event, wake_event_id
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.conftest import test_runtime_paths, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot
from tests.test_tool_job_reply_join import _job_context
from tests.tool_job_helpers import (
    completed_delegation_job,
    job_owner,
    start_job,
    tool_job_journal,
    tool_job_runtime,
    user_stopped,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.event_journal import JournalEvent
    from mindroom.tool_jobs.runtime import ToolJobRuntime
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

pytestmark = pytest.mark.asyncio

_PRINCIPAL = "@mindroom_parent:test"


def _key(owner: ToolExecutionIdentity) -> HoldKey:
    assert owner.room_id is not None
    assert owner.requester_id is not None
    return HoldKey(
        recipient=owner.recipient,
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
        requester_id=owner.requester_id,
        silent=False,
        participants=(owner.agent_name,),
    )


async def _waiting_reply(principal: PrincipalStore, key: HoldKey, *, source: str = "$turn") -> rl.Reply:
    """Answer ``source`` with a reply that waits for ``key``'s work."""
    await principal.admit(
        InboundEvent(
            event_id=source,
            room_id=key.room_id,
            thread_id=key.thread_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=key.requester_id,
            origin_server_ts=1,
            source={},
        ),
    )
    await principal.replies.write_generation("gen-1")
    request = rl.ClaimRequest(
        span_id=f"span-{source}",
        delivery_id=source,
        sources=ResponseSources((source,), (source,)),
        bot_generation="gen-1",
        now_ns=10,
        new_reply_id=f"reply-{source}",
        entity_name=key.recipient,
        room_id=key.room_id,
        thread_id=key.thread_id,
        membership_epoch=0,
        empty_presentation=encode_presentation(Presentation()),
    )
    claimed = (await principal.replies.claim(request)).transition
    assert claimed.reply is not None
    assert claimed.claimed is not None

    def wait(reply: rl.Reply, span: rl.Span) -> rl.Transition:
        shown = Presentation(
            segments=(Segment(kind="answer", text="answer", span_id=span.span_id),),
            trailing_note=note_segment(NoteKind.JOB_WAIT),
        )
        write = rl.TerminalWrite(
            shown=encode_presentation(shown),
            prepared_revision=reply.revision,
            state=rl.ReplyState.WAITING,
        )
        return rl.wait(reply, span, write, hold_key=key.encode(), now_ns=20)

    waited = await principal.replies.decide(
        reply_id=claimed.reply.reply_id,
        span_id=claimed.claimed.span_id,
        decide=wait,
    )
    assert waited.transition.reply is not None
    return waited.transition.reply


def _coordinator(tmp_path: Path, runtime: ToolJobRuntime, bot: MagicMock) -> ToolJobRuntimeCoordinator:
    """A coordinator around a test runtime whose grants allow every job, delivering through one bot."""
    paths = test_runtime_paths(tmp_path)
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: None,
        bot_provider=lambda _name: bot,
        agent_reply_memberships=MagicMock(),
        journal_provider=lambda: tool_job_journal(paths.storage_root),
    )
    coordinator._runtime = runtime
    coordinator._journal = tool_job_journal(paths.storage_root)
    return coordinator


async def test_the_boundary_names_the_work_its_span_leaves(tmp_path: Path) -> None:
    """A span whose boundary leaves work outstanding with nothing ready records the work its answer waits for."""
    owner = completed_delegation_job().owner
    runtime = await tool_job_runtime(tmp_path)
    context = _job_context(tmp_path, owner)
    pin_background_tool_jobs(context.config, context.runtime_paths)
    register_background_runtime(context.runtime_paths, runtime)
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    handle = MagicMock(spec=SpanHandle)
    handle.leaves_work = None
    token = _current_slot.set(SpanSlot(handle=handle))
    try:
        await start_job(runtime, "work", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        with tool_runtime_context(context):
            await join_conversation_jobs(set(), joins=0)
        assert handle.leaves_work == _key(owner).encode()
        finish.set()
        await wait_for_status(runtime, "work", "completed")
        with tool_runtime_context(context):
            joined = await join_conversation_jobs(set(), joins=0)
        # Ready work continues the span instead; nothing is left for its answer to wait for.
        assert joined.prompt is not None
        assert handle.leaves_work is None
    finally:
        _current_slot.reset(token)
        finish.set()
        await runtime.shutdown()


async def test_a_key_round_trips_through_its_recorded_json() -> None:
    """A waiting reply's hold key restores the key the boundary named."""
    key = _key(job_owner())
    assert HoldKey.decode(key.encode()) == key


async def test_the_job_runtime_wakes_a_reply_once_its_work_is_ready(tmp_path: Path) -> None:
    """Outstanding work admits nothing, ready work admits one wake, and a wait with no work left admits its end."""
    owner = job_owner()
    runtime = await tool_job_runtime(tmp_path)
    bot = MagicMock()
    bot.admit_job_wake = AsyncMock()
    coordinator = _coordinator(tmp_path, runtime, bot)
    assert coordinator._journal is not None
    key = _key(owner)
    reply = await _waiting_reply(coordinator._journal.principal(_PRINCIPAL), key)
    finish = asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await finish.wait()
        return BackgroundOutcome("completed", "done")

    try:
        job = await start_job(runtime, "work", tool_name="tool", depth=0, adapter={}, owner=owner, operation=operation)
        await coordinator._admit_wakes(runtime)
        bot.admit_job_wake.assert_not_awaited()

        finish.set()
        await wait_for_status(runtime, "work", "completed")
        await coordinator._admit_wakes(runtime)
        bot.admit_job_wake.assert_awaited_once()
        woken, wake_id = bot.admit_job_wake.await_args.args
        assert woken.reply_id == reply.reply_id
        assert wake_id == wake_event_id(reply.reply_id, (job,))

        # The wake retrieves the outcome, so no work is left for the reply.
        retrieved = await runtime.wait("work", owner=owner, depth=0, timeout=0)
        await runtime.acknowledge_wait("work", retrieved.claim, source_event_id=wake_id)
        bot.admit_job_wake.reset_mock()
        await coordinator._admit_wakes(runtime)
        assert bot.admit_job_wake.await_args.args[1] == f"job-wake:{reply.reply_id}:release"
    finally:
        finish.set()
        await runtime.shutdown()


async def test_a_stop_cancels_the_work_its_reply_started_and_waits_for(tmp_path: Path) -> None:
    """The job runtime cancels the stopped reply's own and held work, leaves other work alone, then forgets the Stop."""
    owner = job_owner()
    runtime = await tool_job_runtime(tmp_path)
    coordinator = _coordinator(tmp_path, runtime, MagicMock())
    journal = coordinator._journal
    assert journal is not None
    principal = journal.principal(_PRINCIPAL)
    reply = await _waiting_reply(principal, _key(owner))

    async def forever() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    try:
        await start_job(runtime, "held", tool_name="tool", depth=0, adapter={}, owner=owner, operation=forever)
        elsewhere = replace(owner, room_id="!other:test")
        await start_job(runtime, "other", tool_name="tool", depth=0, adapter={}, owner=elsewhere, operation=forever)
        stop = rl.stop(reply, None, rl.StopFacts(receipt_order=5, span_live=False), now_ns=30)
        await principal.replies.update(reply.reply_id, lambda _current: stop)
        assert await journal.reply_job_stops() == ((_PRINCIPAL, reply.reply_id),)

        await coordinator._apply_job_stops()

        assert await journal.reply_job_stops() == ()
        await wait_for_status(runtime, "held", "cancelled")
        assert user_stopped(runtime, "held")
        assert not user_stopped(runtime, "other")
    finally:
        await runtime.shutdown()


def _runner_key(bot: AgentBot) -> HoldKey:
    return HoldKey(
        recipient=bot.agent_name,
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        silent=False,
        participants=(bot.agent_name,),
    )


def _runner_owner(key: HoldKey) -> ToolExecutionIdentity:
    return replace(
        job_owner(),
        agent_name=key.recipient,
        requester_id=key.requester_id,
        room_id=key.room_id,
        thread_id=key.thread_id,
        resolved_thread_id=key.thread_id,
    )


async def _wake(principal: PrincipalStore, reply: rl.Reply, wake_id: str) -> JournalEvent:
    await principal.admit(wake_event(reply, wake_id, sender_id="@mindroom_general:localhost", now_ms=1))
    event = await principal.load_event(wake_id)
    assert event is not None
    return event


async def test_a_wake_decides_under_the_lock_what_it_retrieves(tmp_path: Path) -> None:
    """A wake asks for the outcomes ready when it runs, and runs nothing once a newer reply retrieved them."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    key = _runner_key(bot)
    reply = await _waiting_reply(bot.journal_principal(), key)
    runtime = await tool_job_runtime(tmp_path)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)
    owner = _runner_owner(key)

    async def done() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "done")

    try:
        await start_job(runtime, "work", tool_name="tool", depth=0, adapter={}, owner=owner, operation=done)
        await wait_for_status(runtime, "work", "completed")
        event = await _wake(bot.journal_principal(), reply, "job-wake:1")
        request = await runner._job_wake_request(
            reply,
            key,
            event.event_id,
            claimed=asyncio.Event(),
            handoff=asyncio.Event(),
        )
        assert request.sources.logical_source_event_ids == ("$turn",)
        woken = await runner._wake_under_lock(request)
        assert woken is not None
        assert 'job_id="work"' in woken.prompt
        assert woken.response_envelope.body == woken.prompt

        retrieved = await runtime.wait("work", owner=owner, depth=0, timeout=0)
        await runtime.acknowledge_wait("work", retrieved.claim, source_event_id="$newer")
        assert await runner._wake_under_lock(request) is None
    finally:
        await runtime.shutdown()


async def test_a_wake_no_span_took_settles_and_ends_a_wait_no_work_is_left_for(tmp_path: Path) -> None:
    """With nothing outstanding the wake ends the wait, keeping the answer, and its source settles."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    principal = bot.journal_principal()
    reply = await _waiting_reply(principal, _runner_key(bot))
    event = await _wake(principal, reply, "job-wake:release")
    runner.generate_response = AsyncMock(return_value=None)

    await runner._run_job_wake(event)

    runner.generate_response.assert_awaited_once()
    assert not await principal.is_pending("job-wake:release")
    ended = await principal.replies.load(reply.reply_id)
    assert ended is not None
    assert ended.state is rl.ReplyState.COMPLETED


async def test_a_stop_of_an_older_waiting_reply_leaves_a_newer_turns_work_alone(tmp_path: Path) -> None:
    """Work a later message of the conversation started belongs to that message's reply, which stops it."""
    owner = job_owner()
    runtime = await tool_job_runtime(tmp_path)
    coordinator = _coordinator(tmp_path, runtime, MagicMock())
    journal = coordinator._journal
    assert journal is not None
    principal = journal.principal(_PRINCIPAL)
    key = _key(owner)

    def message(event_id: str, origin_server_ts: int) -> InboundEvent:
        return InboundEvent(
            event_id,
            key.room_id,
            key.thread_id,
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
            key.requester_id,
            origin_server_ts,
            {},
        )

    await principal.admit(message("$earlier", 1))
    reply = await _waiting_reply(principal, key)
    await principal.admit(message("$newer", 2))

    async def forever() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    try:
        for job_id, source in (("earlier", "$earlier"), ("newer", "$newer")):
            await start_job(
                runtime,
                job_id,
                tool_name="tool",
                depth=0,
                adapter={},
                owner=owner,
                source_event_id=source,
                operation=forever,
            )
        stop = rl.stop(reply, None, rl.StopFacts(receipt_order=5, span_live=False), now_ns=30)
        await principal.replies.update(reply.reply_id, lambda _current: stop)

        await coordinator._apply_job_stops()

        await wait_for_status(runtime, "earlier", "cancelled")
        assert user_stopped(runtime, "earlier")
        assert not user_stopped(runtime, "newer")
    finally:
        await runtime.shutdown()
