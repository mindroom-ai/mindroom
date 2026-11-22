"""Seam tests for extracted conversation-ownership helpers used by the reply path."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import turn_store as turn_store_module
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.conversation_resolver import DegradedThreadHistoryError, read_fresh_thread_history
from mindroom.event_journal import (
    RUNTIME_SOURCE_KINDS,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    approval_signals,
)
from mindroom.event_journal.approval_signals import PausedReply
from mindroom.history.session_context import (
    ScopeSessionContext,
    load_scope_session_from_storage,
    load_scrubbed_scope_session,
)
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord
from mindroom.matrix.state import MatrixState
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.reply_turn.request import requester_remains_authorized
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.response_sources import Owned
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import follow_up_event
from mindroom.turn_record import TurnRecord
from tests.access_schema_support import refresh_memberships
from tests.conftest import request_envelope, unwrap_extracted_collaborator
from tests.matrix_state_helpers import save_matrix_state
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_turn_store import _store_with_storage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from pathlib import Path


@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.asyncio
async def test_requester_remains_authorized_checks_resolved_membership_and_pending_raises(tmp_path: Path) -> None:
    """Post-lock re-authorization distinguishes allowed, revoked, and pending membership."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    bot.config.agents["general"].access = ResponderAccessConfig(members_of_rooms=["grant"])
    state = MatrixState.load(runtime_paths=bot.runtime_paths)
    state.add_room("grant", "!grant:localhost", "#grant:localhost", "Grant")
    save_matrix_state(state, bot.runtime_paths)
    request = _plain_request(_target())
    memberships = runner.deps.runtime.agent_reply_memberships

    with pytest.raises(ReplyMembershipPendingError):
        requester_remains_authorized(
            "@user:localhost",
            request.room_id,
            "general",
            bot.config,
            bot.runtime_paths,
            memberships,
        )
    with pytest.raises(ReplyMembershipPendingError):
        await runner.locked.request_remains_authorized(request)

    membership_client = AsyncMock(spec=nio.AsyncClient)
    membership_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!grant:localhost"])
    membership_client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember("@user:localhost", None, None)],
        room_id="!grant:localhost",
    )
    await refresh_memberships(memberships, bot.config, bot.runtime_paths, membership_client)

    assert (
        requester_remains_authorized(
            "@user:localhost",
            request.room_id,
            "general",
            bot.config,
            bot.runtime_paths,
            memberships,
        )
        is True
    )
    assert await runner.locked.request_remains_authorized(request) is True

    membership_client.joined_members.return_value = nio.JoinedMembersResponse(members=[], room_id="!grant:localhost")
    await refresh_memberships(memberships, bot.config, bot.runtime_paths, membership_client)
    assert (
        requester_remains_authorized(
            "@user:localhost",
            request.room_id,
            "general",
            bot.config,
            bot.runtime_paths,
            memberships,
        )
        is False
    )
    assert await runner.locked.request_remains_authorized(request) is False


@pytest.mark.usefixtures("enforce_turn_authorization")
@pytest.mark.asyncio
async def test_requester_remains_authorized_uses_configured_team_policy(tmp_path: Path) -> None:
    """A configured team bot evaluates the team policy rather than requiring each member grant."""
    bot = _bot(tmp_path)
    bot.config.agents["member"] = AgentConfig(
        display_name="Member",
        access=ResponderAccessConfig(users=["@only-member:localhost"]),
    )
    bot.config.teams["crew"] = TeamConfig(
        display_name="Crew",
        role="Coordinate",
        agents=["member"],
        access=ResponderAccessConfig(users=["@alice:localhost"]),
    )
    runner = unwrap_extracted_collaborator(bot._response_runner)
    memberships = runner.deps.runtime.agent_reply_memberships

    assert (
        requester_remains_authorized(
            "@alice:localhost",
            "!room:localhost",
            "crew",
            bot.config,
            bot.runtime_paths,
            memberships,
            reply_entity_names=("member",),
        )
        is True
    )
    assert (
        requester_remains_authorized(
            "@only-member:localhost",
            "!room:localhost",
            "crew",
            bot.config,
            bot.runtime_paths,
            memberships,
            reply_entity_names=("member",),
        )
        is False
    )


@pytest.mark.asyncio
async def test_load_scrubbed_scope_session_drains_load_on_cancel_and_scrubs_redactions(tmp_path: Path) -> None:
    """Loading a scrubbed scope drains cancellation during blocking I/O and removes redacted runs."""
    scope = HistoryScope(kind="agent", scope_id="general")
    storage = create_state_storage("general", tmp_path, scope)
    session = ConversationSession(kind="agent", session_id="sess-1", owner_id="general")
    storage.save_run(
        session,
        RunRecord(
            run_id="run-redacted",
            agent_id="general",
            session_id="sess-1",
            metadata={"matrix_event_id": "$gone"},
        ),
        record_usage=False,
    )

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_load() -> ScopeSessionContext:
        entered.set()
        release.wait(timeout=5)
        try:
            return load_scope_session_from_storage(scope, "sess-1", lambda: storage)
        finally:
            finished.set()

    cancelled_task = asyncio.create_task(load_scrubbed_scope_session(blocking_load, None))
    await asyncio.to_thread(entered.wait, 5)
    cancelled_task.cancel()
    await asyncio.sleep(0)
    assert not cancelled_task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_task
    assert finished.is_set()

    async def redactions(event_ids: tuple[str, ...]) -> dict[str, str | None]:
        return {"$gone": None} if "$gone" in event_ids else {}

    scope_context = await load_scrubbed_scope_session(
        partial(load_scope_session_from_storage, scope, "sess-1", lambda: storage),
        redactions,
    )
    assert scope_context.session is not None
    assert scope_context.session.runs == []


@pytest.mark.asyncio
async def test_edit_cut_scrubs_redacted_history_through_load_scrubbed_scope_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TurnStore.remove_stale_runs_for_edit delegates load+scrub to load_scrubbed_scope_session."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$edited")
    scope = HistoryScope(kind="agent", scope_id="agent")
    storage = create_state_storage("agent", tmp_path, scope)
    session = ConversationSession(kind="agent", session_id=target.session_id, owner_id="agent")
    storage.save_run(
        session,
        RunRecord(
            run_id="run-edited",
            agent_id="agent",
            session_id=target.session_id,
            metadata={"matrix_event_id": "$edited"},
        ),
        record_usage=False,
    )
    journal_store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    try:
        store = await _store_with_storage(journal_store, storage)
        record = TurnRecord.create(
            ["$edited"],
            response_event_id="$reply",
            response_owner="agent",
            requester_id="@user:example.org",
            history_scope=scope,
            conversation_target=target,
        )
        await store.record_turn(record)

        calls = 0

        async def spy(
            load_scope: Callable[[], ScopeSessionContext],
            redactions: Callable[[tuple[str, ...]], Awaitable[Mapping[str, str | None]]] | None,
        ) -> ScopeSessionContext:
            nonlocal calls
            calls += 1
            return await load_scrubbed_scope_session(load_scope, redactions)

        monkeypatch.setattr(turn_store_module, "load_scrubbed_scope_session", spy)
        await store.remove_stale_runs_for_edit(turn_record=record, requester_user_id="@user:example.org")

        assert calls == 1
        persisted = storage.load_session(target.session_id)
        assert persisted is not None
        assert persisted.runs == []
    finally:
        await journal_store.close()


@pytest.mark.asyncio
async def test_read_fresh_thread_history_handles_room_mode_normal_and_strict_degraded(tmp_path: Path) -> None:
    """Post-lock thread history read returns None in room mode and rejects degraded reads only when strict."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)

    room_target = MessageTarget.resolve("!room:localhost", None, "$src", room_mode=True)
    fetch = AsyncMock()
    runner.deps.resolver.fetch_thread_history = fetch
    assert await read_fresh_thread_history(runner.deps.resolver, room_target, strict=True) is None
    fetch.assert_not_awaited()

    thread_target = MessageTarget.resolve("!room:localhost", "$thread", "$src")
    healthy = thread_history_result([], is_full_history=True)
    degraded = thread_history_result([], is_full_history=False, diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True})

    runner.deps.resolver.fetch_thread_history = AsyncMock(return_value=healthy)
    assert await read_fresh_thread_history(runner.deps.resolver, thread_target, strict=True) is healthy

    runner.deps.resolver.fetch_thread_history = AsyncMock(return_value=degraded)
    assert await read_fresh_thread_history(runner.deps.resolver, thread_target, strict=False) is degraded
    with pytest.raises(DegradedThreadHistoryError) as exc_info:
        await read_fresh_thread_history(runner.deps.resolver, thread_target, strict=True)
    assert exc_info.value.target == thread_target
    assert exc_info.value.history is degraded

    # Reply fallback keeps degraded history and falls back on fetch failure.
    refreshed = await runner.locked.refresh_model_history(_plain_request(thread_target))
    assert refreshed.thread_history is degraded
    assert refreshed.requires_model_history_refresh is False

    runner.deps.resolver.fetch_thread_history = AsyncMock(side_effect=RuntimeError("boom"))
    fallback = await runner.locked.refresh_model_history(_plain_request(thread_target))
    assert fallback.requires_model_history_refresh is True


@pytest.mark.asyncio
async def test_exclusive_conversation_waits_for_approval_without_admission_or_reply_signal(tmp_path: Path) -> None:
    """Lock-only exclusive_conversation waits out approvals without admission and does not look like a reply."""
    bot = _bot(tmp_path)
    runtime = BotRuntimeState(orchestrator=bot.orchestrator)
    gate = runtime.response_admission_gate
    coordinator = ResponseLifecycleCoordinator()
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    target = MessageTarget.resolve("!room:localhost", "$thread", "$src")
    entered = asyncio.Event()
    release = asyncio.Event()
    reply_ran = asyncio.Event()
    order: list[str] = []

    try:
        coordinator.follow_approval_holds(store.approval_signals, "agent@general", lambda _room_id: None)
        await store.backend.write(
            lambda tx: approval_signals.paused(
                tx,
                PausedReply("agent@general", "appr-1", "!room:localhost", "$thread"),
            ),
        )

        async def run_exclusive() -> None:
            async with coordinator.exclusive_conversation(
                target,
                admit=runtime.admission_fence,
                wait_for_approval_release=runtime.wait_for_admission_or_shutdown,
            ):
                order.append("exclusive_enter")
                entered.set()
                await release.wait()
                order.append("exclusive_exit")

        task = asyncio.create_task(run_exclusive())
        await asyncio.sleep(0)
        assert not entered.is_set()
        assert gate.in_flight_response_count == 0
        assert gate.close_if_idle() is True
        gate.reopen()

        await store.backend.write(lambda tx: approval_signals.ended(tx, "agent@general", "appr-1", None))
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert gate.in_flight_response_count == 1
        assert coordinator.has_active_response_for_target(target) is False

        envelope = request_envelope(target=target, prompt="follow", user_id="@user:localhost")

        async def locked_reply(_target: MessageTarget) -> str:
            order.append("reply")
            reply_ran.set()
            return "ok"

        reply_task = asyncio.create_task(
            coordinator.run_locked_response(
                target=target,
                response_envelope=envelope,
                pipeline_timing=None,
                locked_operation=locked_reply,
            ),
        )
        await asyncio.sleep(0)
        assert not reply_ran.is_set()
        assert not coordinator._get_or_create_queued_signal(target).has_pending_human_messages()

        release.set()
        await asyncio.wait_for(task, timeout=2)
        assert await asyncio.wait_for(reply_task, timeout=2) == "ok"
        assert order == ["exclusive_enter", "exclusive_exit", "reply"]
        assert gate.in_flight_response_count == 0
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_exclusive_conversation_rewaits_when_hold_appears_while_queued_and_stops_on_drain(
    tmp_path: Path,
) -> None:
    """A hold appearing while queued releases both lock and admission; draining refuses the wait."""
    bot = _bot(tmp_path)
    runtime = BotRuntimeState(orchestrator=bot.orchestrator)
    gate = runtime.response_admission_gate
    coordinator = ResponseLifecycleCoordinator()
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    target = MessageTarget.resolve("!room:localhost", "$thread", "$src")
    reply_locked = asyncio.Event()
    reply_release = asyncio.Event()
    exclusive_entered = asyncio.Event()

    try:
        coordinator.follow_approval_holds(store.approval_signals, "agent@general", lambda _room_id: None)
        envelope = request_envelope(target=target, prompt="hi", user_id="@user:localhost")

        async def pausing_reply(_target: MessageTarget) -> None:
            reply_locked.set()
            await reply_release.wait()
            await store.backend.write(
                lambda tx: approval_signals.paused(
                    tx,
                    PausedReply("agent@general", "appr-2", "!room:localhost", "$thread"),
                ),
            )

        reply_task = asyncio.create_task(
            coordinator.run_locked_response(
                target=target,
                response_envelope=envelope,
                pipeline_timing=None,
                locked_operation=pausing_reply,
            ),
        )
        await asyncio.wait_for(reply_locked.wait(), timeout=2)

        async def run_exclusive() -> None:
            async with coordinator.exclusive_conversation(
                target,
                admit=runtime.admission_fence,
                wait_for_approval_release=runtime.wait_for_admission_or_shutdown,
            ):
                exclusive_entered.set()

        exclusive_task = asyncio.create_task(run_exclusive())
        await asyncio.sleep(0)
        reply_release.set()
        await asyncio.wait_for(reply_task, timeout=2)
        await asyncio.sleep(0)

        assert not exclusive_entered.is_set()
        assert gate.in_flight_response_count == 0

        runtime.begin_draining()
        with pytest.raises(ResponseAdmissionRefusedError):
            await asyncio.wait_for(exclusive_task, timeout=2)
        assert target.lifecycle_key not in coordinator._exclusive_users
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reply_admission_fence_logs_deferred_and_refused_events(tmp_path: Path) -> None:
    """Reply lifecycle admission logs deferral and refusal through the shared admission fence."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    logger_spy = MagicMock()
    runner.locked.deps = replace(runner.deps, logger=logger_spy)
    gate = runner.deps.runtime.response_admission_gate
    assert gate.close_if_idle() is True

    task = asyncio.create_task(
        runner._run_locked_response_lifecycle(
            _plain_request(_target()),
            response_kind="ai",
            locked_operation=AsyncMock(return_value=Owned()),
        ),
    )
    await asyncio.sleep(0)
    logger_spy.info.assert_called_once()
    assert logger_spy.info.call_args.args == ("response_deferred_during_replacement",)
    assert logger_spy.info.call_args.kwargs["response_kind"] == "ai"

    runner.deps.runtime.begin_draining()
    with pytest.raises(ResponseAdmissionRefusedError):
        await asyncio.wait_for(task, timeout=2)
    logger_spy.warning.assert_called_once()
    assert logger_spy.warning.call_args.args == ("response_refused_after_runtime_replacement",)
    assert logger_spy.warning.call_args.kwargs["response_kind"] == "ai"
    assert runner.in_flight_response_count == 0
    assert gate.in_flight_response_count == 0


@pytest.mark.asyncio
async def test_runtime_source_handoff_dispatches_by_kind_and_settles_unclaimed(tmp_path: Path) -> None:
    """Runtime sources dispatch via RUNTIME_SOURCE_KINDS and handoff_runtime_source's kind table."""
    assert EventKind.JOB_FOLLOW_UP in RUNTIME_SOURCE_KINDS
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = bot.journal_principal()
    key = ConversationKey(
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        recipient=bot.agent_name,
        silent=False,
    )
    await store.admit(follow_up_event(key, "job-follow-up:seam-1", sender_id=bot.matrix_id.full_id, now_ms=1_000))
    event = await store.load_event("job-follow-up:seam-1")
    assert event is not None
    await runner.handoff_runtime_source(event)
    await runner.inbox.drain_inbox_responses()
    assert not await store.is_pending("job-follow-up:seam-1")
    await runner.inbox.drain_inbox_responses()

    await store.admit(
        InboundEvent(
            event_id="unknown-runtime:1",
            room_id="!room:localhost",
            thread_id=None,
            kind=EventKind.OPAQUE_HISTORY,
            event_class=EventClass.ACTIONABLE,
            sender=bot.matrix_id.full_id,
            origin_server_ts=1_001,
            source={},
        ),
    )
    unknown = await store.load_event("unknown-runtime:1")
    assert unknown is not None
    await runner.handoff_runtime_source(unknown)
    assert not await store.is_pending("unknown-runtime:1")


@pytest.mark.asyncio
async def test_exclusive_conversation_rejects_reservation_and_holds_canonical_lock_across_retry(
    tmp_path: Path,
) -> None:
    """Public exclusive_conversation never accepts a reservation and always holds the canonical lock on retry."""
    bot = _bot(tmp_path)
    runtime = BotRuntimeState(orchestrator=bot.orchestrator)
    coordinator = ResponseLifecycleCoordinator()
    store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    target = MessageTarget.resolve("!room:localhost", "$thread", "$src")
    envelope = request_envelope(target=target, prompt="hi", user_id="@user:localhost")

    try:
        reservation = await coordinator.reserve_response_lifecycle(envelope)
        try:
            with pytest.raises(TypeError):
                async with coordinator.exclusive_conversation(target, reservation=reservation):  # type: ignore[call-arg]
                    pass
        finally:
            await reservation.release()

        # Verify retry after an approval hold still acquires and holds the canonical target lock exclusively.
        coordinator.follow_approval_holds(store.approval_signals, "agent@general", lambda _room_id: None)
        reply_locked = asyncio.Event()
        reply_release = asyncio.Event()
        first_entered = asyncio.Event()
        first_release = asyncio.Event()
        second_entered = asyncio.Event()
        lock_held_in_body: list[bool] = []

        async def pausing_reply(_target: MessageTarget) -> None:
            reply_locked.set()
            await reply_release.wait()
            await store.backend.write(
                lambda tx: approval_signals.paused(
                    tx,
                    PausedReply("agent@general", "appr-retry", "!room:localhost", "$thread"),
                ),
            )

        reply_task = asyncio.create_task(
            coordinator.run_locked_response(
                target=target,
                response_envelope=envelope,
                pipeline_timing=None,
                locked_operation=pausing_reply,
            ),
        )
        await asyncio.wait_for(reply_locked.wait(), timeout=2)

        async def first_exclusive() -> None:
            async with coordinator.exclusive_conversation(
                target,
                admit=runtime.admission_fence,
                wait_for_approval_release=runtime.wait_for_admission_or_shutdown,
            ):
                lock_held_in_body.append(coordinator._response_lifecycle_lock(target).locked())
                first_entered.set()
                await first_release.wait()

        async def second_exclusive() -> None:
            async with coordinator.exclusive_conversation(
                target,
                admit=runtime.admission_fence,
                wait_for_approval_release=runtime.wait_for_admission_or_shutdown,
            ):
                second_entered.set()

        first_task = asyncio.create_task(first_exclusive())
        await asyncio.sleep(0)
        reply_release.set()
        await asyncio.wait_for(reply_task, timeout=2)
        await asyncio.sleep(0)
        assert not first_entered.is_set()

        await store.backend.write(lambda tx: approval_signals.ended(tx, "agent@general", "appr-retry", None))
        await asyncio.wait_for(first_entered.wait(), timeout=2)
        assert lock_held_in_body == [True]

        second_task = asyncio.create_task(second_exclusive())
        await asyncio.sleep(0)
        assert not second_entered.is_set()

        first_release.set()
        await asyncio.wait_for(first_task, timeout=2)
        await asyncio.wait_for(second_task, timeout=2)
        assert second_entered.is_set()
    finally:
        await store.close()
