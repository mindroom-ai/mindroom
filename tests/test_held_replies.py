"""A finished reply's message holds its conversation's outstanding background work until a turn continues it."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.event_journal import EventKind, JournalEvent
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner
from mindroom.stop import StopManager
from mindroom.streaming import StreamingPresentation
from mindroom.tool_jobs.completion import JOB_JOIN_LIMIT, ReplyBoundary
from mindroom.tool_jobs.held_replies import (
    _APPROVAL_NOTICE,
    _WAITING_NOTICE,
    HeldReply,
    HoldKey,
    _wake_event_id,
    decode_held_reply,
    encode_held_reply,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.tool_job_helpers import lookup, start_job, tool_job_runtime, wait_for_status

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.delivery_gateway import EditTextRequest
    from mindroom.response_runner import ResponseRunner
    from mindroom.tool_jobs.runtime import ToolJobRuntime

_THREAD = "$thread"
_KEY = HoldKey("general", "!room:localhost", _THREAD, "@user:localhost", False, ("general",))


def _owner(job_owner_session: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@user:localhost",
        room_id="!room:localhost",
        thread_id=_THREAD,
        resolved_thread_id=_THREAD,
        session_id=job_owner_session,
    )


def _completed(event_id: str, body: str) -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome(
        terminal_status="completed",
        event_id=event_id,
        is_visible_response=True,
        final_visible_body=body,
        extra_content={"io.mindroom.stream_status": "completed"},
    )


class _Held:
    """One runner over a real job runtime and journal, with Matrix edits and redactions observed."""

    def __init__(self, tmp_path: Path) -> None:
        self.bot = _bot(tmp_path)
        self.bot.config.background_tool_jobs.enabled = True
        self.runner: ResponseRunner = unwrap_extracted_collaborator(self.bot._response_runner)
        self.edits: list[EditTextRequest] = []
        self.request = _plain_request(_target(thread_id=_THREAD))
        self.owner = _owner(self.request.response_envelope.target.session_id)
        self.runtime: ToolJobRuntime

    async def open(self, tmp_path: Path) -> None:
        self.runtime = await tool_job_runtime(tmp_path)
        pin_background_tool_jobs(self.bot.config, self.bot.runtime_paths)
        register_background_runtime(self.bot.runtime_paths, self.runtime)

    async def edit(self, request: EditTextRequest) -> bool:
        self.edits.append(request)
        return True

    async def settle(
        self,
        event_id: str | None,
        body: str,
        notice: str | None,
        *,
        joins: int = 0,
        button: str | None = None,
        held: HeldReply | None = None,
    ) -> None:
        outcome = (
            _completed(event_id, body)
            if event_id is not None
            else FinalDeliveryOutcome(terminal_status="completed", event_id=None)
        )
        await self.runner._settle_held_reply(
            replace(self.request, held_reply=held),
            outcome,
            ReplyBoundary(_KEY, notice, joins),
            stop_button_event_id=button,
        )

    async def hold(self) -> HeldReply | None:
        saved = await self.runner.deps.held_replies.load(_KEY.hold_id)
        return decode_held_reply(saved) if saved is not None else None

    async def start(self, job_id: str, gate: asyncio.Event | None = None) -> None:
        async def operation() -> BackgroundOutcome:
            if gate is not None:
                await gate.wait()
            return BackgroundOutcome("completed", f"Result of {job_id}")

        await start_job(
            self.runtime,
            job_id,
            tool_name="tool",
            depth=0,
            source_event_id="$event",
            adapter={},
            owner=self.owner,
            operation=operation,
        )


@pytest_asyncio.fixture
async def held(tmp_path: Path) -> AsyncIterator[_Held]:
    """A runner whose Matrix edits are recorded, over a running job runtime."""
    state = _Held(tmp_path)
    await state.open(tmp_path)

    async def edit(_gateway: object, request: EditTextRequest) -> bool:
        return await state.edit(request)

    with patch.object(type(state.runner.deps.delivery_gateway), "edit_text", edit):
        try:
            yield state
        finally:
            await state.runtime.shutdown()


@pytest.mark.asyncio
async def test_a_reply_holding_work_keeps_its_message_waiting(held: _Held) -> None:
    """The finished reply's message shows the notice and is saved as the holder, and the coordinator is woken."""
    held.runtime.changed.clear()
    await held.settle("$reply", "Started the report.", _WAITING_NOTICE, button="$button")
    hold = await held.hold()
    assert hold is not None
    assert (hold.message_event_id, hold.presentation.response_text, hold.notice) == (
        "$reply",
        "Started the report.",
        _WAITING_NOTICE,
    )
    assert hold.stop_button_event_id == "$button"
    assert [edit.new_text for edit in held.edits] == [f"Started the report.\n\n{_WAITING_NOTICE}"]
    assert held.edits[0].extra_content["io.mindroom.stream_status"] == "streaming"
    assert held.runtime.changed.is_set()


@pytest.mark.asyncio
async def test_a_newer_reply_takes_the_work_over_from_the_older_message(held: _Held) -> None:
    """Only the latest reply's message holds the work; the older one shows its reply again and loses its button."""
    await held.settle("$first", "First answer.", _WAITING_NOTICE, button="$first-button")
    await held.settle("$second", "Second answer.", _WAITING_NOTICE)
    hold = await held.hold()
    assert hold is not None
    assert hold.message_event_id == "$second"
    assert [(edit.event_id, edit.new_text) for edit in held.edits] == [
        ("$first", f"First answer.\n\n{_WAITING_NOTICE}"),
        ("$first", "First answer."),
        ("$second", f"Second answer.\n\n{_WAITING_NOTICE}"),
    ]
    assert held.edits[1].extra_content["io.mindroom.stream_status"] == "completed"
    held.bot.client.room_redact.assert_awaited_once_with(
        "!room:localhost",
        "$first-button",
        reason="Response completed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("same_message", [False, True])
async def test_a_reply_leaving_nothing_outstanding_releases_the_hold(held: _Held, *, same_message: bool) -> None:
    """A reply that retrieved or saw the end of every job releases the message that held them."""
    await held.settle("$first", "First answer.", _WAITING_NOTICE)
    await held.settle("$first" if same_message else "$second", "Done.", None)
    assert await held.hold() is None
    # The reply's own final delivery already shows its message as finished.
    released = [edit for edit in held.edits[1:] if edit.event_id == "$first"]
    assert [edit.new_text for edit in released] == ([] if same_message else ["First answer."])


@pytest.mark.asyncio
async def test_a_continuation_ending_before_its_boundary_releases_its_hold(held: _Held) -> None:
    """A continuation that failed, was stopped, or paused leaves its message holding nothing."""
    await held.settle("$reply", "Started.", _WAITING_NOTICE)
    hold = await held.hold()
    assert hold is not None
    await held.runner._settle_held_reply(
        replace(held.request, held_reply=hold),
        FinalDeliveryOutcome(terminal_status="error", event_id="$reply", is_visible_response=True),
        None,
        stop_button_event_id=None,
    )
    assert await held.hold() is None
    assert len(held.edits) == 1


@pytest.mark.asyncio
async def test_a_silent_schedule_holds_its_work_without_a_message(held: _Held) -> None:
    """A silent schedule's hold has no message to edit, so only a turn continuing it reports the results."""
    silent_key = replace(_KEY, silent=True)
    await held.runner._settle_held_reply(
        held.request,
        FinalDeliveryOutcome(terminal_status="completed", event_id=None),
        ReplyBoundary(silent_key, _WAITING_NOTICE, 0),
        stop_button_event_id=None,
    )
    saved = await held.runner.deps.held_replies.load(silent_key.hold_id)
    assert saved is not None
    assert decode_held_reply(saved).message_event_id is None
    assert held.edits == []


@pytest.mark.asyncio
async def test_resuming_a_held_message_retrieves_ready_work(held: _Held) -> None:
    """Under the lock, ready work becomes the continuation's prompt and the held message its starting point."""
    await held.start("ready")
    await wait_for_status(held.runtime, "ready", "completed")
    await held.settle("$reply", "Started.", _WAITING_NOTICE, joins=2)
    hold = await held.hold()
    assert hold is not None
    resumed = await held.runner._resume_held_reply(replace(held.request, held_reply=hold))
    assert resumed is not None
    assert 'job_id="ready"' in resumed.prompt
    assert resumed.response_envelope.body == resumed.prompt
    assert resumed.held_continuation is not None
    assert resumed.held_continuation.ready_job_ids == frozenset({"ready"})
    assert resumed.held_continuation.joins == 2
    assert resumed.held_continuation.presentation.response_text == "Started."


@pytest.mark.asyncio
async def test_resuming_a_replaced_hold_does_nothing(held: _Held) -> None:
    """A wake for an earlier generation finds a newer reply holding the work and runs no turn."""
    await held.start("ready")
    await wait_for_status(held.runtime, "ready", "completed")
    await held.settle("$first", "First.", _WAITING_NOTICE)
    old = await held.hold()
    await held.settle("$second", "Second.", _WAITING_NOTICE)
    assert old is not None
    assert await held.runner._resume_held_reply(replace(held.request, held_reply=old)) is None
    current = await held.hold()
    assert current is not None
    assert current.message_event_id == "$second"


@pytest.mark.asyncio
async def test_resuming_shows_what_the_work_waits_for_now(held: _Held) -> None:
    """Work that changed without becoming ready, such as a job now awaiting approval, is held again with its notice."""
    decided = asyncio.Event()

    async def approval() -> BackgroundOutcome:
        await held.runtime.set_awaiting_approval("approval", awaiting=True)
        await decided.wait()
        return BackgroundOutcome("completed", "approved")

    try:
        await start_job(
            held.runtime,
            "approval",
            tool_name="delegate",
            depth=0,
            adapter={},
            owner=held.owner,
            operation=approval,
        )
        await wait_for_status(held.runtime, "approval", "awaiting_approval")
        await held.settle("$reply", "Started.", _WAITING_NOTICE)
        hold = await held.hold()
        assert hold is not None
        assert await held.runner._resume_held_reply(replace(held.request, held_reply=hold)) is None
        again = await held.hold()
        assert again is not None
        assert again.generation != hold.generation
        assert again.notice == _APPROVAL_NOTICE
        assert held.edits[-1].new_text == f"Started.\n\n{_APPROVAL_NOTICE}"
    finally:
        decided.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["nothing_outstanding", "join_limit"])
async def test_resuming_releases_a_message_with_nothing_left_to_continue(held: _Held, reason: str) -> None:
    """With no work left, or at the join limit, the wake releases the message and the next reply takes the rest."""
    if reason == "join_limit":
        await held.start("ready")
        await wait_for_status(held.runtime, "ready", "completed")
    await held.settle("$reply", "Started.", _WAITING_NOTICE, joins=JOB_JOIN_LIMIT if reason == "join_limit" else 0)
    hold = await held.hold()
    assert hold is not None
    assert await held.runner._resume_held_reply(replace(held.request, held_reply=hold)) is None
    assert await held.hold() is None
    assert held.edits[-1].new_text == "Started."
    assert held.edits[-1].extra_content["io.mindroom.stream_status"] == "completed"


@pytest.mark.asyncio
async def test_stop_on_a_held_message_ends_its_work(held: _Held) -> None:
    """Stop on a message no turn runs on cancels the work it holds and shows the message as stopped."""
    gate = asyncio.Event()
    await held.start("running", gate)
    await held.settle("$reply", "Started.", _WAITING_NOTICE, button="$button")
    assert await held.runner.held_reply_for_message("$reply", "!room:localhost")
    assert not await held.runner.held_reply_for_message("$reply", "!other:localhost")
    assert await held.runner.stop_held_reply("$reply", 7)
    stopped = await lookup(held.runtime, "running", owner=held.owner, depth=0)
    assert stopped.user_stop_receipt_order == 7
    await wait_for_status(held.runtime, "running", "cancelled")
    assert await held.hold() is None
    assert held.edits[-1].new_text == "Started.\n\n**[Response cancelled by user]**"
    assert held.edits[-1].extra_content["io.mindroom.stream_status"] == "cancelled"
    held.bot.client.room_redact.assert_awaited_once_with("!room:localhost", "$button", reason="Response completed")
    assert not await held.runner.stop_held_reply("$reply", 8)


def _wake(hold: HeldReply) -> JournalEvent:
    return JournalEvent(
        event_id=_wake_event_id(hold),
        room_id=hold.key.room_id,
        thread_id=hold.key.thread_id,
        kind=EventKind.HELD_REPLY_WAKE,
        sender="@mindroom_general:localhost",
        origin_server_ts=0,
        source={},
        receipt_order=1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_a_wake_whose_turn_never_begins_releases_its_hold(
    held: _Held,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stale: bool,
) -> None:
    """A wake settles; one whose continuation never began, such as an unauthorized one, releases the message."""
    await held.settle("$reply", "Started.", _WAITING_NOTICE)
    hold = await held.hold()
    assert hold is not None
    if stale:
        await held.settle("$reply", "Started again.", _WAITING_NOTICE)
    generate = AsyncMock()
    settle = AsyncMock()
    monkeypatch.setattr(held.runner, "_generate_held_continuation", generate)
    monkeypatch.setattr(type(held.runner.deps.approval_store), "settle", settle)
    await held.runner._continue_held_reply(_wake(hold))
    settle.assert_awaited_once_with(_wake_event_id(hold))
    if stale:
        generate.assert_not_awaited()
        assert await held.hold() is not None
    else:
        request = generate.await_args.args[0]
        assert request.held_reply == hold
        assert request.existing_event_id == "$reply"
        assert request.sources.pending_event_ids == (_wake_event_id(hold),)
        assert await held.hold() is None
        assert held.edits[-1].new_text == "Started."


def test_held_reply_round_trips_through_its_snapshot() -> None:
    """A saved hold restores exactly, and one saved under another key is refused."""
    hold = HeldReply(
        key=_KEY,
        target=_target(thread_id=_THREAD),
        source_kind=MESSAGE_SOURCE_KIND,
        message_event_id="$reply",
        presentation=StreamingPresentation("Started."),
        extra_content={"key": "value"},
        notice=_WAITING_NOTICE,
        stop_button_event_id="$button",
        joins=3,
        generation="a" * 32,
        woken_generation="a" * 32,
    )
    saved = MagicMock(
        hold_id=_KEY.hold_id,
        hold_json=encode_held_reply(hold),
        generation="a" * 32,
        woken_generation="a" * 32,
    )
    assert decode_held_reply(saved) == hold
    saved.hold_id = replace(_KEY, requester_id="@other:localhost").hold_id
    with pytest.raises(ValueError, match="Invalid held reply"):
        decode_held_reply(saved)


@pytest.mark.asyncio
async def test_coordinator_wakes_a_hold_once_its_work_changes(held: _Held) -> None:
    """A hold is woken once per generation: when work is ready, gone, or waits for something else."""
    gate = asyncio.Event()
    await held.start("running", gate)
    await held.settle("$reply", "Started.", _WAITING_NOTICE)
    woken: list[HeldReply] = []

    async def wake_held_reply(hold: HeldReply) -> None:
        woken.append(hold)

    bot = MagicMock(running=True, wake_held_reply=wake_held_reply)
    bot.client.rooms = {"!room:localhost": object()}
    coordinator = ToolJobRuntimeCoordinator(
        runtime_paths=held.bot.runtime_paths,
        config_provider=lambda: held.bot.config,
        bot_provider=lambda _name: bot,
        agent_reply_memberships=MagicMock(),
        journal_provider=lambda: held.bot._journal_store,
    )
    coordinator._runtime = held.runtime
    coordinator._journal = held.bot._journal_store
    assert coordinator._journal.held_replies() is not None
    await coordinator._wake_held_replies()
    assert woken == []
    gate.set()
    await wait_for_status(held.runtime, "running", "completed")
    await coordinator._wake_held_replies()
    await coordinator._wake_held_replies()
    assert [hold.message_event_id for hold in woken] == ["$reply"]
    saved = await held.runner.deps.held_replies.load(_KEY.hold_id)
    assert saved is not None
    assert saved.woken_generation == saved.generation


@pytest.mark.asyncio
@pytest.mark.parametrize("keep", [False, True])
async def test_attempt_reuses_and_keeps_a_held_message_stop_button(*, keep: bool) -> None:
    """A turn on a held message tracks its existing Stop button, and a message that goes on holding keeps it."""
    client = MagicMock()
    client.room_redact = AsyncMock()
    manager = StopManager()
    manager.add_stop_button = AsyncMock()  # type: ignore[method-assign]
    seen: list[str | None] = []

    def keep_stop_button(reaction_event_id: str | None) -> bool:
        seen.append(reaction_event_id)
        return keep

    async def response(_message_id: str | None) -> None:
        return None

    runner = ResponseAttemptRunner(
        ResponseAttemptDeps(
            client=client,
            stop_manager=manager,
            logger=MagicMock(),
            show_stop_button=lambda: True,
            config=MagicMock(),
        ),
    )
    await runner.run(
        ResponseAttemptRequest(
            target=_target(thread_id=_THREAD),
            response_function=response,
            existing_event_id="$reply",
            stop_button_event_id="$button",
            keep_stop_button=keep_stop_button,
        ),
    )
    # Cleanup redacts at once, then forgets the message after a delay this test does not wait out.
    for _ in range(10):
        await asyncio.sleep(0)
    for task in manager.cleanup_tasks:
        task.cancel()
    manager.add_stop_button.assert_not_awaited()
    assert seen == ["$button"]
    if keep:
        client.room_redact.assert_not_awaited()
    else:
        client.room_redact.assert_awaited_once_with(
            room_id="!room:localhost",
            event_id="$button",
            reason="Response completed",
        )
