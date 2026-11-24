"""Seam tests for extracted conversation-ownership helpers used by the reply path."""

from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import response_runner as response_runner_module
from mindroom import response_turn as response_turn_module
from mindroom import turn_store as turn_store_module
from mindroom.authorization import ReplyMembershipPendingError
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.conversation_resolver import DegradedThreadHistoryError, read_fresh_thread_history
from mindroom.event_journal import (
    RUNTIME_SOURCE_KINDS,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
)
from mindroom.history import session_context as session_context_module
from mindroom.history.session_context import (
    ScopeSessionContext,
    load_scope_session_from_storage,
    load_scrubbed_scope_session,
)
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.state import MatrixState
from mindroom.matrix.thread_diagnostics import THREAD_HISTORY_DEGRADED_DIAGNOSTIC
from mindroom.matrix.thread_history_result import thread_history_result
from mindroom.message_target import MessageTarget
from mindroom.reply_turn import locked_turn as locked_turn_module
from mindroom.reply_turn.request import requester_remains_authorized
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_runner import ResponseRunner
from mindroom.response_sources import Owned
from mindroom.response_turn import ResponseTurnContext, TurnRunState
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_jobs.conversation import ConversationKey
from mindroom.tool_jobs.follow_ups import follow_up_event
from mindroom.turn_record import TurnRecord
from tests.access_schema_support import refresh_memberships
from tests.conftest import unwrap_extracted_collaborator
from tests.matrix_state_helpers import save_matrix_state
from tests.response_runner_helpers import _bot, _plain_request, _target
from tests.test_turn_store import _redact_through, _store_with_storage

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

    with pytest.raises(TypeError):
        cast("Any", requester_remains_authorized)(
            "@user:localhost",
            request.room_id,
            "general",
            bot.config,
            bot.runtime_paths,
            memberships,
        )
    with pytest.raises(ReplyMembershipPendingError):
        requester_remains_authorized(
            "@user:localhost",
            request.room_id,
            owner_entity_name="general",
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            membership_index=memberships,
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
            owner_entity_name="general",
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            membership_index=memberships,
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
            owner_entity_name="general",
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            membership_index=memberships,
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
            owner_entity_name="crew",
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            membership_index=memberships,
            reply_entity_names=("member",),
        )
        is True
    )
    assert (
        requester_remains_authorized(
            "@only-member:localhost",
            "!room:localhost",
            owner_entity_name="crew",
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            membership_index=memberships,
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

    scope_context, removed = await load_scrubbed_scope_session(
        partial(load_scope_session_from_storage, scope, "sess-1", lambda: storage),
        redactions,
    )
    assert removed == ["$gone"]
    assert scope_context.session is not None
    assert scope_context.session.runs == []


@pytest.mark.asyncio
async def test_edit_cut_scrubs_redacted_history_through_load_scrubbed_scope_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TurnStore.remove_stale_runs_for_edit scrubs separate redacted runs silently through the shared helper."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$edited")
    scope = HistoryScope(kind="agent", scope_id="agent")
    storage = create_state_storage("agent", tmp_path, scope)
    session = ConversationSession(kind="agent", session_id=target.session_id, owner_id="agent")
    storage.save_run(
        session,
        RunRecord(
            run_id="run-redacted",
            agent_id="agent",
            session_id=target.session_id,
            metadata={"matrix_event_id": "$earlier-redacted"},
        ),
        record_usage=False,
    )
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
        redacted_record = TurnRecord.create(
            ["$earlier-redacted"],
            response_event_id="$earlier-reply",
            response_owner="agent",
            requester_id="@user:example.org",
            history_scope=scope,
            conversation_target=target,
        )
        edited_record = TurnRecord.create(
            ["$edited"],
            response_event_id="$reply",
            response_owner="agent",
            requester_id="@user:example.org",
            history_scope=scope,
            conversation_target=target,
        )
        await store.record_turn(redacted_record)
        await store.record_turn(edited_record)
        await _redact_through(store, "$earlier-redacted", target.room_id)

        calls = 0

        async def spy(
            load_scope: Callable[[], ScopeSessionContext],
            redactions: Callable[[tuple[str, ...]], Awaitable[Mapping[str, str | None]]] | None,
        ) -> tuple[ScopeSessionContext, list[str]]:
            nonlocal calls
            calls += 1
            return await load_scrubbed_scope_session(load_scope, redactions)

        monkeypatch.setattr(turn_store_module, "load_scrubbed_scope_session", spy)
        assert not hasattr(session_context_module, "logger")
        await store.remove_stale_runs_for_edit(turn_record=edited_record, requester_user_id="@user:example.org")

        assert calls == 1
        persisted = storage.load_session(target.session_id)
        assert persisted is not None
        assert persisted.runs == []
    finally:
        await journal_store.close()


@pytest.mark.asyncio
async def test_reply_scope_load_emits_redaction_log_on_response_turn_logger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reply scope loading keeps logging redaction removals under mindroom.response_turn."""
    scope = HistoryScope(kind="agent", scope_id="general")
    storage = create_state_storage("general", tmp_path, scope)
    session = ConversationSession(kind="agent", session_id="sess-log", owner_id="general")
    storage.save_run(
        session,
        RunRecord(
            run_id="run-gone",
            agent_id="general",
            session_id="sess-log",
            metadata={"matrix_event_id": "$gone"},
        ),
        record_usage=False,
    )

    async def redactions(event_ids: tuple[str, ...]) -> dict[str, str | None]:
        return {"$gone": None} if "$gone" in event_ids else {}

    logger_spy = MagicMock()
    monkeypatch.setattr(response_turn_module, "logger", logger_spy)
    ctx = ResponseTurnContext(
        entity_label="general",
        session_id="sess-log",
        run_id=None,
        correlation_id="corr-1",
        reply_to_event_id="$src",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        matrix_run_metadata=None,
        redacted_history_events=redactions,
    )
    adapter = MagicMock()
    adapter.load_scope = partial(load_scope_session_from_storage, scope, "sess-log", lambda: storage)
    run = TurnRunState(record=None)

    loaded = await response_turn_module._load_scope_session(ctx, adapter, run)
    assert loaded.session is not None
    assert loaded.session.runs == []
    logger_spy.info.assert_called_once_with(
        "Removed history derived from redacted events",
        session_id="sess-log",
        history_scope=scope.key,
        redacted_event_ids=["$gone"],
    )


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
async def test_runtime_source_handoff_dispatches_by_kind_and_settles_unclaimed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime sources dispatch through the kind table via method lookup and fail loudly on unknown kinds."""
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

    # Instance monkeypatches on _run_job_follow_up are observed by table dispatch.
    patched = AsyncMock()
    monkeypatch.setattr(runner, "_run_job_follow_up", patched)
    await store.admit(follow_up_event(key, "job-follow-up:seam-2", sender_id=bot.matrix_id.full_id, now_ms=1_001))
    second = await store.load_event("job-follow-up:seam-2")
    assert second is not None
    await runner.handoff_runtime_source(second)
    await runner.inbox.drain_inbox_responses()
    patched.assert_awaited_once_with(second)

    # An unwired kind raises loudly rather than silently settling as ignored.
    await store.admit(
        InboundEvent(
            event_id="unknown-runtime:1",
            room_id="!room:localhost",
            thread_id=None,
            kind=EventKind.OPAQUE_HISTORY,
            event_class=EventClass.ACTIONABLE,
            sender=bot.matrix_id.full_id,
            origin_server_ts=1_002,
            source={},
        ),
    )
    unknown = await store.load_event("unknown-runtime:1")
    assert unknown is not None
    with pytest.raises(KeyError):
        await runner.handoff_runtime_source(unknown)
    assert await store.is_pending("unknown-runtime:1")


def test_runtime_source_kinds_matches_runner_handler_table() -> None:
    """The journal dispatcher's RUNTIME_SOURCE_KINDS stays in lockstep with ResponseRunner's handler table."""
    assert set(ResponseRunner._RUNTIME_SOURCE_HANDLERS) == set(RUNTIME_SOURCE_KINDS)


@pytest.mark.asyncio
async def test_read_fresh_thread_history_handles_room_mode_normal_and_strict_degraded(tmp_path: Path) -> None:
    """The post-lock thread read returns None in room mode and refuses a degraded read only when strict."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    resolver = runner.deps.resolver

    room_target = MessageTarget.resolve("!room:localhost", None, "$src", room_mode=True)
    fetch = AsyncMock()
    resolver.fetch_thread_history = fetch
    assert await read_fresh_thread_history(resolver, room_target, strict=True) is None
    fetch.assert_not_awaited()

    thread_target = MessageTarget.resolve("!room:localhost", "$thread", "$src")
    healthy = thread_history_result([], is_full_history=True)
    degraded = thread_history_result([], is_full_history=False, diagnostics={THREAD_HISTORY_DEGRADED_DIAGNOSTIC: True})

    resolver.fetch_thread_history = AsyncMock(return_value=healthy)
    assert await read_fresh_thread_history(resolver, thread_target, strict=True) is healthy
    resolver.fetch_thread_history.assert_awaited_once_with("!room:localhost", "$thread")

    resolver.fetch_thread_history = AsyncMock(return_value=degraded)
    assert await read_fresh_thread_history(resolver, thread_target) is degraded
    with pytest.raises(DegradedThreadHistoryError):
        await read_fresh_thread_history(resolver, thread_target, strict=True)

    # The reply's refresh is the non-strict read: it keeps a degraded history and falls back when the read fails.
    refreshed = await runner.locked.refresh_model_history(_plain_request(thread_target))
    assert refreshed.thread_history is degraded
    assert refreshed.requires_model_history_refresh is False
    resolver.fetch_thread_history.assert_awaited_with("!room:localhost", "$thread", requester_id="@user:localhost")

    resolver.fetch_thread_history = AsyncMock(side_effect=RuntimeError("boom"))
    fallback = await runner.locked.refresh_model_history(_plain_request(thread_target))
    assert fallback.requires_model_history_refresh is True

    # A request carrying an earlier expanded cross-room prefix never falls back to that snapshot when refresh fails.
    cross_room_request = replace(
        _plain_request(thread_target),
        thread_history=[
            ResolvedVisibleMessage.synthetic(
                sender="@user:localhost",
                body="earlier cross-room prefix",
                event_id="$parent",
                room_id="!source:localhost",
            ),
            ResolvedVisibleMessage.synthetic(sender="@user:localhost", body="fork root", event_id="$thread"),
        ],
        requires_model_history_refresh=False,
    )
    with pytest.raises(RuntimeError, match="boom"):
        await runner.locked.refresh_model_history(cross_room_request)


def test_reply_refresh_and_compaction_read_threads_only_through_the_fresh_read_helper() -> None:
    """Neither the reply's post-lock refresh nor compaction reads a thread except through the shared helper."""
    for module in (locked_turn_module, response_runner_module):
        source = inspect.getsource(module)
        assert "read_fresh_thread_history(" in source
        assert "fetch_thread_history(" not in source


def test_compaction_owns_its_conversation_only_through_the_reply_path_helpers() -> None:
    """The runner, where compaction's fold lives, has no requester check, scrub, lock or admission code of its own."""
    source = inspect.getsource(response_runner_module)
    for shared in (
        "requester_remains_authorized(",
        "load_scrubbed_scope_session(",
        "lifecycle_coordinator.run_locked(",
    ):
        assert shared in source
    for own in ("is_sender_allowed_for", "remove_history_of_redacted_events", "admission_fence", "_approval_holds"):
        assert own not in source


@pytest.mark.asyncio
async def test_edit_cut_opens_its_store_before_its_first_suspension(tmp_path: Path) -> None:
    """Edit regeneration opens its store synchronously, so a failed open surfaces before a pending cancellation runs."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$edited")
    journal_store = EventJournalStore.open_sqlite(tmp_path / "journal.db")
    order: list[str] = []

    def failing_open(*_args: object, **_kwargs: object) -> None:
        order.append("open_store")
        msg = "store unavailable"
        raise OSError(msg)

    try:
        store = await _store_with_storage(journal_store, MagicMock())
        cast("MagicMock", store.deps.state_writer).create_storage.side_effect = failing_open
        edited_record = TurnRecord.create(
            ["$edited"],
            response_event_id="$reply",
            response_owner="agent",
            requester_id="@user:example.org",
            history_scope=HistoryScope(kind="agent", scope_id="agent"),
            conversation_target=target,
        )
        task = asyncio.create_task(
            store.remove_stale_runs_for_edit(turn_record=edited_record, requester_user_id="@user:example.org"),
        )

        def stop() -> None:
            order.append("stop")
            task.cancel()

        asyncio.get_running_loop().call_soon(stop)
        with pytest.raises(OSError, match="store unavailable"):
            await task
        assert order == ["open_store", "stop"]
    finally:
        await journal_store.close()
