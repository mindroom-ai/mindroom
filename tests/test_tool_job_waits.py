"""Replies that wait for background work: the boundary's hold, the wakes the job runtime admits, and job Stops."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from mindroom import reply_lifecycle as rl
from mindroom.event_journal import EventClass, EventKind, InboundEvent, PrincipalStore
from mindroom.reply_presentation import NoteKind, Presentation, Segment, encode_presentation, note_segment
from mindroom.response_sources import ResponseSources
from mindroom.tool_jobs.completion import ConversationKey
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, notify_job_stops, register_background_runtime
from tests.tool_job_helpers import (
    job_context,
    job_coordinator,
    job_owner,
    start_job,
    tool_job_runtime,
    user_stopped,
    wait_for_status,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
    from mindroom.tool_jobs.runtime import BackgroundJob
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

pytestmark = pytest.mark.asyncio

_PRINCIPAL = "@mindroom_parent:test"


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


async def _waiting_reply(principal: PrincipalStore, key: ConversationKey, *, source: str = "$turn") -> rl.Reply:
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


async def test_a_key_round_trips_through_its_recorded_json() -> None:
    """A waiting reply's hold key restores the key the boundary named."""
    key = _key(job_owner())
    assert ConversationKey.decode(key.encode()) == key


async def test_a_stop_cancels_the_work_its_reply_started_and_waits_for(tmp_path: Path) -> None:
    """The job runtime cancels the stopped reply's own and held work, leaves other work alone, then forgets the Stop."""
    owner = job_owner()
    runtime = await tool_job_runtime(tmp_path)
    coordinator = job_coordinator(tmp_path, runtime, MagicMock())
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
        [recorded] = await journal.reply_job_stops()
        assert recorded.stop_id.startswith(f"{reply.reply_id}:")

        await coordinator._apply_job_stops()

        assert await journal.reply_job_stops() == ()
        await wait_for_status(runtime, "held", "cancelled")
        assert user_stopped(runtime, "held")
        assert not user_stopped(runtime, "other")
    finally:
        await runtime.shutdown()


async def test_a_recorded_stop_reaches_its_work_before_the_job_runtime_applies_it(tmp_path: Path) -> None:
    """A call whose approval lands after its reply's Stop was recorded, but before it was applied, sees that Stop."""
    owner = job_owner()
    coordinators: list[ToolJobRuntimeCoordinator] = []

    async def stopped(job: BackgroundJob) -> bool:
        return await coordinators[0]._stop_recorded(job)

    runtime = await tool_job_runtime(tmp_path, stopped=stopped)
    coordinators.append(job_coordinator(tmp_path, runtime, MagicMock()))
    journal = coordinators[0]._journal
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
        assert not await runtime.stop_recorded("held")

        context = job_context(tmp_path, owner)
        pin_background_tool_jobs(context.config, context.runtime_paths)
        register_background_runtime(context.runtime_paths, runtime)
        runtime.changed.clear()
        stop = rl.stop(reply, None, rl.StopFacts(receipt_order=5, span_live=False), now_ns=30)
        await principal.replies.update(reply.reply_id, lambda _current: stop)
        # The bot tells the job runtime after the commit, so its next pass starts at once.
        notify_job_stops(context.runtime_paths)
        assert runtime.changed.is_set()

        assert await runtime.stop_recorded("held")
        assert not await runtime.stop_recorded("other")
        # A job that is gone never runs its call either.
        assert await runtime.stop_recorded("unknown")
    finally:
        await runtime.shutdown()


async def test_a_stop_of_an_older_waiting_reply_leaves_a_newer_turns_work_alone(tmp_path: Path) -> None:
    """Work a later message of the conversation started belongs to that message's reply, which stops it."""
    owner = job_owner()
    runtime = await tool_job_runtime(tmp_path)
    coordinator = job_coordinator(tmp_path, runtime, MagicMock())
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


async def test_a_stop_applied_after_a_regeneration_cancels_what_the_stopped_reply_owned(tmp_path: Path) -> None:
    """An edit stops a waiting reply and regenerates it before the runtime applies the Stop: the old work goes."""
    owner = job_owner()
    runtime = await tool_job_runtime(tmp_path)
    coordinator = job_coordinator(tmp_path, runtime, MagicMock())
    journal = coordinator._journal
    assert journal is not None
    principal = journal.principal(_PRINCIPAL)
    key = _key(owner)
    reply = await _waiting_reply(principal, key)

    async def forever() -> BackgroundOutcome:
        await asyncio.Event().wait()
        raise AssertionError

    try:
        await start_job(runtime, "held", tool_name="tool", depth=0, adapter={}, owner=owner, operation=forever)
        stop = rl.stop(reply, None, rl.StopFacts(receipt_order=5, span_live=False), now_ns=30)
        await principal.replies.update(reply.reply_id, lambda _current: stop)
        stopped = await principal.replies.load(reply.reply_id)
        assert stopped is not None
        # The regeneration's own work starts before the Stop is applied.
        await principal.admit(
            InboundEvent(
                "$edit",
                key.room_id,
                key.thread_id,
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
                key.requester_id,
                3,
                {},
            ),
        )
        edit = replace(
            _regeneration_request(stopped, key),
            sources=ResponseSources(("$edit",), ("$turn",)),
        )
        await principal.replies.update(reply.reply_id, lambda current: rl.claim(edit, _claim_context(current)))
        await start_job(
            runtime,
            "regenerated",
            tool_name="tool",
            depth=0,
            adapter={},
            owner=owner,
            source_event_id="$edit",
            operation=forever,
        )

        await coordinator._apply_job_stops()

        await wait_for_status(runtime, "held", "cancelled")
        assert not user_stopped(runtime, "regenerated")
    finally:
        await runtime.shutdown()


def _regeneration_request(reply: rl.Reply, key: ConversationKey) -> rl.ClaimRequest:
    return rl.ClaimRequest(
        span_id="span-edit",
        delivery_id="$edit",
        sources=ResponseSources(("$edit",), ("$turn",)),
        bot_generation="gen-1",
        now_ns=40,
        new_reply_id="unused",
        entity_name=key.recipient,
        room_id=reply.room_id,
        thread_id=reply.thread_id,
        membership_epoch=0,
        empty_presentation=encode_presentation(Presentation()),
        driving_edit_id="$edit",
    )


def _claim_context(reply: rl.Reply) -> rl.ClaimContext:
    return rl.ClaimContext(
        reply=replace(reply, owed_write=None),
        last_span=None,
        current_span=None,
        interactive_span=None,
        durable_write_debt=False,
        active_generation="gen-1",
    )
