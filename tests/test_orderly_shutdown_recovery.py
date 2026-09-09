"""Orderly shutdown preserves the journal's exact unfinished callback debt."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import nio
import pytest

from mindroom.agent_reply_membership_sync import AgentReplyMembershipSync
from mindroom.cancellation import current_task_is_process_shutdown
from mindroom.config.access import ResponderAccessConfig
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.event_journal_open import OpenEventJournal
from mindroom.handled_turns import _reset_handled_turn_ledger_runtime
from mindroom.journal_dispatch import JournalCallbacks, JournalDispatcher
from mindroom.matrix.client_session import MindRoomAsyncClient
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.state import MatrixState
from mindroom.orchestration.runtime import sync_forever_with_restart
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner
from mindroom.response_runner import ResponseShutdownTimeoutError
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN
from mindroom.stop import StopManager
from tests.conftest import unwrap_extracted_collaborator
from tests.journal_helpers import admit_dispatch_event
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_edit_regenerator import (
    AGENT_NAME,
    EDIT_EVENT_ID,
    NEW_RESPONSE_EVENT_ID,
    ORIGINAL_EVENT_ID,
    USER_ID,
    _acknowledge_test_edit,
    _edit_event,
    _harness,
    _turn_record,
)
from tests.test_response_delivery_gateway import _response_recovery_bot
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, PrincipalStore
    from mindroom.response_runner import ResponseRequest

pytestmark = [pytest.mark.ledger_loads_from_disk, pytest.mark.usefixtures("enforce_turn_authorization")]


def _dispatcher(
    principal: PrincipalStore,
    callback: Callable[[nio.MatrixRoom, nio.RoomMessageFormatted], Awaitable[TurnDispatchOutcome]],
) -> JournalDispatcher:
    return JournalDispatcher(
        store=principal,
        callbacks=JournalCallbacks(
            on_message=callback,
            on_media=AsyncMock(),
            on_reaction=AsyncMock(),
            on_approval=AsyncMock(),
            on_room_lifecycle=AsyncMock(),
            on_redaction=AsyncMock(),
            on_approval_continuation=AsyncMock(return_value=None),
            source_has_live_owner=lambda _event_id: False,
            turn_has_live_claim=lambda _event_id: False,
        ),
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, "@bot:localhost"),
    )


@pytest.mark.asyncio
async def test_orderly_shutdown_preserves_edit_callback_and_revision(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """Cancellation reaches the mailbox as interruption, then replay commits the exact edit."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store, agent_name=AGENT_NAME)
    await store.record_responded_turn(_turn_record(source_event_prompts={ORIGINAL_EVENT_ID: "original"}))
    harness = _harness(tmp_path, turn_record=None)
    started = asyncio.Event()
    attempt = ResponseAttemptRunner(
        ResponseAttemptDeps(
            client=harness.regenerator.deps.runtime.client,
            stop_manager=StopManager(),
            logger=MagicMock(),
            show_stop_button=lambda: False,
            config=harness.config,
        ),
    )

    async def generate(request: ResponseRequest) -> str | None:
        cancelled: list[str] = []

        async def model(_event_id: str | None) -> None:
            started.set()
            await asyncio.Event().wait()

        result = await attempt.run(
            ResponseAttemptRequest(
                target=request.response_envelope.target,
                existing_event_id=request.existing_event_id,
                response_function=model,
                on_cancelled=cancelled.append,
            ),
        )
        return None if cancelled else result

    harness.regenerator.deps = replace(harness.regenerator.deps, turn_store=store, generate_response=generate)

    async def callback(room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        await harness.regenerator.handle_message_edit(room, event, EventInfo.from_event(event.source), USER_ID)
        return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    dispatcher = _dispatcher(principal, callback)
    bot = _bot(tmp_path / "bot")
    bot.client = MindRoomAsyncClient("https://example.org", "@mindroom_general:localhost")
    bot._journal_dispatcher = dispatcher
    event, _event_info = _edit_event(new_body="revision survives shutdown")
    await admit_dispatch_event(dispatcher, harness.room, event, EventKind.MESSAGE, EventClass.ACTIONABLE)
    dispatcher.release_turn_replay()
    dispatcher.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await bot.stop(shutdown_intent=ORDERLY_SHUTDOWN)

    assert await principal.is_pending(EDIT_EVENT_ID)
    interrupted = store.get_turn_record(ORIGINAL_EVENT_ID)
    assert interrupted is not None
    assert not interrupted.source_event_revisions

    async def recover(request: ResponseRequest) -> str:
        await _acknowledge_test_edit(tmp_path, request, NEW_RESPONSE_EVENT_ID, store, journal_store=journal_store)
        return NEW_RESPONSE_EVENT_ID

    harness.regenerator.deps = replace(harness.regenerator.deps, generate_response=recover)
    recovered = _dispatcher(principal, callback)
    assert await recovered.drain_once() == 1
    _reset_handled_turn_ledger_runtime()
    reopened = await _store(journal_store, agent_name=AGENT_NAME)
    recovered_record = reopened.get_turn_record(ORIGINAL_EVENT_ID)
    assert recovered_record is not None
    assert recovered_record.completed
    assert recovered_record.response_event_id == NEW_RESPONSE_EVENT_ID
    assert recovered_record.source_event_revisions[ORIGINAL_EVENT_ID] == (1_000_001, EDIT_EVENT_ID)
    assert not await principal.is_pending(EDIT_EVENT_ID)
    assert await recovered.drain_once() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True], ids=["shutdown", "revocation"])
async def test_shutdown_closes_callbacks_before_membership_invalidation(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    revoked: bool,
) -> None:
    """An accepted request cannot become an ignored source through shutdown readiness loss."""
    principal = journal_store.principal("agent@alice")
    bot = _bot(tmp_path)
    bot.client = MindRoomAsyncClient("https://example.org", "@mindroom_general:localhost")
    runner = unwrap_extracted_collaborator(bot._response_runner)
    bot.config.agents["general"].access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", "!grant:localhost", "#grant:localhost", "Grant")
    state.save(runtime_paths=bot.runtime_paths)
    membership_client = AsyncMock(spec=nio.AsyncClient)
    membership_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!grant:localhost"])
    membership_client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember("@user:localhost", None, None)],
        room_id="!grant:localhost",
    )
    memberships = runner.deps.runtime.agent_reply_memberships
    await memberships.refresh(bot.config, bot.runtime_paths, membership_client)
    entered = asyncio.Event()
    exited = asyncio.Event()
    release = asyncio.Event()
    source_id = "$accepted-before-shutdown"
    request = replace(
        _plain_request(_target(), source_event_id=source_id),
        on_source_turn_suppressed=lambda: principal.settle(source_id),
    )
    assert await runner._request_remains_authorized(request)
    decisions: list[bool] = []

    async def callback(_room: nio.MatrixRoom, _event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        entered.set()
        try:
            await release.wait()
            decisions.append(await runner._request_remains_authorized(request))
        finally:
            exited.set()
        return TurnDispatchOutcome.DEFERRED

    dispatcher = _dispatcher(principal, callback)
    bot._journal_dispatcher = dispatcher
    event = nio.RoomMessageText.from_dict(
        {
            "event_id": source_id,
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "accepted request"},
        },
    )
    await admit_dispatch_event(
        dispatcher,
        nio.MatrixRoom("!room:localhost", "@bot:localhost"),
        event,
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
    )
    dispatcher.release_turn_replay()
    dispatcher.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = bot.config
    orchestrator.agent_reply_memberships = memberships
    orchestrator.agent_reply_membership_sync = AgentReplyMembershipSync(memberships)
    orchestrator.agent_bots = {"general": bot}

    async def release_callback() -> None:
        release.set()
        await asyncio.wait_for(exited.wait(), timeout=2)

    if revoked:
        leave = nio.RoomMemberEvent.from_dict(
            {
                "type": "m.room.member",
                "event_id": "$real-leave",
                "sender": "@user:localhost",
                "state_key": "@user:localhost",
                "origin_server_ts": 2000,
                "content": {"membership": "leave"},
                "unsigned": {"prev_content": {"membership": "join"}},
            },
        )
        assert isinstance(leave, nio.RoomMemberEvent)
        memberships.apply_member_event(
            bot.config,
            bot.runtime_paths,
            "!grant:localhost",
            leave,
            control_user_id="@mindroom_router:localhost",
        )
        await release_callback()
    with patch.object(type(orchestrator._script_runtime), "shutdown", side_effect=release_callback):
        await orchestrator.stop()
    assert await principal.is_pending(source_id) is not revoked, decisions
    if revoked:
        assert decisions == [False]


@pytest.mark.asyncio
async def test_recovery_proof_rejects_settlement_without_final_output(journal_store: EventJournalStore) -> None:
    """A completed source row alone cannot certify durable response recovery."""
    principal = journal_store.principal("agent@alice")
    store = await _store(journal_store)
    record = _turn_record(source_event_ids=("$missing-output",), response_event_id=None)
    await store.record_pending_turn(record)
    await principal.admit(
        InboundEvent(
            event_id="$missing-output",
            room_id="!room:example.org",
            thread_id=None,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender=USER_ID,
            origin_server_ts=1000,
            source={"event_id": "$missing-output", "content": {"msgtype": "m.text", "body": "request"}},
        ),
    )
    await principal.settle("$missing-output")
    proof_owner = _response_recovery_bot(journal_store, store)
    assert not await proof_owner._response_recovery_ready(record)


@pytest.mark.asyncio
async def test_shutdown_preparation_stops_dispatch_before_transport_fence(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """A later admission stays pending instead of making requests against fenced transport."""
    principal = journal_store.principal("agent@alice")
    bot = _bot(tmp_path)
    client = MindRoomAsyncClient("https://example.org", "@mindroom_general:localhost")
    bot.client = client
    attempted: list[str] = []

    async def callback(_room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        attempted.append(event.event_id)
        await client.send("GET", "/_matrix/client/v3/account/whoami")
        return TurnDispatchOutcome.DEFERRED

    dispatcher = _dispatcher(principal, callback)
    bot._journal_dispatcher = dispatcher
    dispatcher.start()
    try:
        await bot.prepare_for_sync_shutdown(shutdown_intent=ORDERLY_SHUTDOWN)
        bot.release_pending_turn_journal_replay()
        event, _event_info = _edit_event()
        await admit_dispatch_event(
            dispatcher,
            nio.MatrixRoom("!room:example.org", "@bot:localhost"),
            event,
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
        )
        await dispatcher.drain_once()
        assert await principal.is_pending(EDIT_EVENT_ID)
        assert attempted == []
    finally:
        await dispatcher.stop()
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("orchestrated", [False, True], ids=["bot", "orchestrator"])
async def test_shutdown_retains_slow_callback_cleanup_within_budgets(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
    orchestrated: bool,
) -> None:
    """A callback resisting cancellation retains its resources without extending either budget."""
    principal = journal_store.principal("agent@alice")
    bot = _bot(tmp_path)
    client = MindRoomAsyncClient("https://example.org", "@mindroom_general:localhost")
    session = aiohttp.ClientSession()
    client.client_session = session
    bot.client = client
    entered = asyncio.Event()
    unwinding = asyncio.Event()
    release = asyncio.Event()

    async def callback(_room: nio.MatrixRoom, _event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            unwinding.set()
            await release.wait()
            raise

    dispatcher = _dispatcher(principal, callback)
    bot._journal_dispatcher = dispatcher
    event, _event_info = _edit_event()
    await admit_dispatch_event(
        dispatcher,
        nio.MatrixRoom("!room:example.org", "@bot:localhost"),
        event,
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
    )
    dispatcher.release_turn_replay()
    dispatcher.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    monkeypatch.setattr("mindroom.bot.SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS", 0.02)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = bot.config
    orchestrator.agent_bots = {"general": bot}
    opened_journal = MagicMock(spec=OpenEventJournal, store=journal_store, close=AsyncMock())
    orchestrator._open_journal = opened_journal
    monkeypatch.setattr("mindroom.orchestrator.RESPONSE_FINALIZATION_TIMEOUT_SECONDS", 0.02)
    stopping = asyncio.create_task(orchestrator.stop() if orchestrated else bot.stop(shutdown_intent=ORDERLY_SHUTDOWN))
    try:
        await asyncio.wait_for(unwinding.wait(), timeout=1)
        done, _pending = await asyncio.wait({stopping}, timeout=0.5)
        assert done, "Callback cleanup escaped the bounded preparation phase"
        with pytest.raises(ResponseShutdownTimeoutError):
            stopping.result()
        assert bot.deferred_stop_required
        assert not session.closed
        if orchestrated:
            assert orchestrator._open_journal is opened_journal
            opened_journal.close.assert_not_awaited()
        assert await principal.is_pending(EDIT_EVENT_ID)
        with pytest.raises(ResponseShutdownTimeoutError):
            await asyncio.wait_for(
                bot.finish_deferred_stop(shutdown_intent=ORDERLY_SHUTDOWN, timeout_seconds=0.02),
                timeout=0.5,
            )
        assert bot.deferred_stop_required
        assert not release.is_set()
        assert not session.closed
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)
        await bot.finish_deferred_stop(shutdown_intent=ORDERLY_SHUTDOWN, timeout_seconds=1)
        await dispatcher.stop()
        await client.close()
    assert session.closed
    assert await principal.is_pending(EDIT_EVENT_ID)


@pytest.mark.asyncio
async def test_explicit_stop_keeps_edited_revision_terminal_after_restart(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """A durable user STOP remains final even when the exact edit callback is replayed."""
    store = await _store(journal_store, agent_name=AGENT_NAME)
    await store.record_responded_turn(_turn_record())
    harness = _harness(tmp_path, turn_record=None)
    generations = 0

    async def stop(request: ResponseRequest) -> str:
        nonlocal generations
        generations += 1
        assert request.on_user_stop_handled is not None
        await request.on_user_stop_handled(NEW_RESPONSE_EVENT_ID, 2)
        return NEW_RESPONSE_EVENT_ID

    harness.regenerator.deps = replace(harness.regenerator.deps, turn_store=store, generate_response=stop)
    event, event_info = _edit_event()
    await harness.regenerator.handle_message_edit(harness.room, event, event_info, USER_ID)
    _reset_handled_turn_ledger_runtime()
    reopened = await _store(journal_store, agent_name=AGENT_NAME)
    stopped_record = reopened.get_turn_record(ORIGINAL_EVENT_ID)
    assert stopped_record is not None
    assert stopped_record.completed
    assert stopped_record.user_stop_receipt_order == 2
    assert stopped_record.source_event_revisions[ORIGINAL_EVENT_ID] == (1_000_001, EDIT_EVENT_ID)
    harness.regenerator.deps = replace(harness.regenerator.deps, turn_store=reopened)
    await harness.regenerator.handle_message_edit(harness.room, event, event_info, USER_ID)
    assert generations == 1


@pytest.mark.asyncio
async def test_orderly_shutdown_upgrades_callback_already_stopping(  # noqa: PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An upgraded callback retains its actual response child and shared resources."""
    principal = journal_store.principal("agent@alice")
    bot = _bot(tmp_path)
    bot.client = MindRoomAsyncClient("https://example.org", "@mindroom_general:localhost")
    session = aiohttp.ClientSession()
    client = bot.client
    client.client_session = session
    entered = asyncio.Event()
    unwinding = asyncio.Event()
    release = asyncio.Event()
    shutdown_flags: list[bool] = []
    children: list[asyncio.Task[None]] = []
    attempt = ResponseAttemptRunner(
        ResponseAttemptDeps(
            client=bot.client,
            stop_manager=StopManager(),
            logger=MagicMock(),
            show_stop_button=lambda: False,
            config=bot.config,
        ),
    )

    async def model(_event_id: str | None) -> None:
        child = asyncio.current_task()
        assert child is not None
        children.append(child)
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            unwinding.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            shutdown_flags.append(current_task_is_process_shutdown())
            raise

    async def callback(_room: nio.MatrixRoom, _event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        await attempt.run(ResponseAttemptRequest(target=_target(), response_function=model))
        return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    dispatcher = _dispatcher(principal, callback)
    bot._journal_dispatcher = dispatcher
    event, _event_info = _edit_event()
    await admit_dispatch_event(
        dispatcher,
        nio.MatrixRoom("!room:example.org", "@bot:localhost"),
        event,
        EventKind.MESSAGE,
        EventClass.ACTIONABLE,
    )
    dispatcher.release_turn_replay()
    dispatcher.start()
    await asyncio.wait_for(entered.wait(), timeout=2)
    generic_stop = asyncio.create_task(dispatcher.stop())
    await asyncio.wait_for(unwinding.wait(), timeout=2)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = bot.config
    orchestrator.agent_bots = {"general": bot}
    opened_journal = MagicMock(spec=OpenEventJournal, store=journal_store, close=AsyncMock())
    orchestrator._open_journal = opened_journal
    monkeypatch.setattr("mindroom.bot.SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr("mindroom.orchestrator.RESPONSE_FINALIZATION_TIMEOUT_SECONDS", 0.02)
    try:
        with pytest.raises(ResponseShutdownTimeoutError):
            await asyncio.wait_for(orchestrator.stop(), timeout=3)
        assert dispatcher.pending_task_count > 0
        assert not children[0].done()
        assert not session.closed
        assert orchestrator._open_journal is opened_journal
        opened_journal.close.assert_not_awaited()
        assert await principal.is_pending(EDIT_EVENT_ID)
    finally:
        release.set()
        await asyncio.gather(generic_stop, *children, return_exceptions=True)
        await bot.finish_deferred_stop(shutdown_intent=ORDERLY_SHUTDOWN, timeout_seconds=1)
        await client.close()
    assert shutdown_flags == [True]
    assert session.closed
    assert await principal.is_pending(EDIT_EVENT_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("late_start", [False, True], ids=["supervisor-retry", "startup-completion"])
async def test_process_shutdown_fence_survives_late_sync_start(  # noqa: C901, PLR0915
    tmp_path: Path,
    journal_store: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
    late_start: bool,
) -> None:
    """The fleet's early teardown pause cannot reopen durable callback admission."""
    bot = _bot(tmp_path)
    bot.running = True
    principal = journal_store.principal("agent@alice")
    attempted: list[str] = []

    async def callback(_room: nio.MatrixRoom, event: nio.RoomMessageFormatted) -> TurnDispatchOutcome:
        attempted.append(event.event_id)
        return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    dispatcher = _dispatcher(principal, callback)
    bot._journal_dispatcher = dispatcher
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = bot.config
    orchestrator.agent_bots = {"general": bot}
    teardown_entered = asyncio.Event()
    release_teardown = asyncio.Event()
    retry_entered = asyncio.Event()
    release_retry = asyncio.Event()
    startup_entered = asyncio.Event()
    release_startup = asyncio.Event()
    receive_starts: list[int] = []
    invalidations: list[str] = []
    original_sleep = asyncio.sleep

    async def retry_sleep(delay: float) -> None:
        if delay == 17:
            retry_entered.set()
            await release_retry.wait()
        else:
            await original_sleep(delay)

    async def receive() -> None:
        receive_starts.append(len(receive_starts) + 1)
        if len(receive_starts) == 1:
            message = "transport disconnected"
            raise OSError(message)
        bot.release_pending_turn_journal_replay()

    async def pause_teardown() -> None:
        teardown_entered.set()
        await release_teardown.wait()

    async def pause_startup() -> None:
        startup_entered.set()
        await release_startup.wait()

    async def start_bot() -> None:
        with (
            patch.object(bot, "ensure_user_account", AsyncMock()),
            patch.object(bot, "_open_owned_matrix_client", AsyncMock(return_value=bot.client)),
            patch.object(bot, "_set_avatar_if_available", AsyncMock()),
            patch.object(bot, "_set_presence_with_model_info", side_effect=pause_startup),
        ):
            await bot.start()

    monkeypatch.setattr(bot, "sync_forever", receive)
    monkeypatch.setattr(bot, "invalidate_agent_reply_memberships", lambda *, reason: invalidations.append(reason))
    monkeypatch.setattr("mindroom.orchestration.runtime.retry_delay_seconds", lambda *_args, **_kwargs: 17)
    monkeypatch.setattr("mindroom.orchestration.runtime.asyncio.sleep", retry_sleep)
    supervisor = asyncio.create_task(sync_forever_with_restart(bot, max_retries=2))
    await asyncio.wait_for(retry_entered.wait(), timeout=2)
    assert receive_starts == [1]
    before_shutdown_invalidations = list(invalidations)
    startup = asyncio.create_task(start_bot()) if late_start else None
    if startup is not None:
        await asyncio.wait_for(startup_entered.wait(), timeout=2)
    with patch.object(type(orchestrator._script_runtime), "shutdown", side_effect=pause_teardown):
        stopping = asyncio.create_task(orchestrator.stop())
        try:
            await asyncio.wait_for(teardown_entered.wait(), timeout=2)
            event, _event_info = _edit_event()
            await admit_dispatch_event(
                dispatcher,
                nio.MatrixRoom("!room:example.org", "@bot:localhost"),
                event,
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
            )
            if startup is not None:
                release_startup.set()
                await asyncio.wait_for(startup, timeout=2)
                bot.mark_sync_loop_started()
                bot.release_pending_turn_journal_replay()
            release_retry.set()
            await asyncio.wait_for(supervisor, timeout=2)
            await dispatcher.drain_once()
            assert await principal.is_pending(EDIT_EVENT_ID)
            assert attempted == []
            assert receive_starts == [1]
            assert invalidations == before_shutdown_invalidations
            assert not await bot._response_runner.wait_for_admission_or_shutdown()
        finally:
            release_retry.set()
            release_startup.set()
            release_teardown.set()
            await asyncio.gather(supervisor, stopping, return_exceptions=True)
            if startup is not None:
                await asyncio.gather(startup, return_exceptions=True)
