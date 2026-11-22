"""Seam tests for extracted conversation-ownership helpers used by the reply path."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.authorization import ReplyMembershipPendingError
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.matrix.state import MatrixState
from tests.access_schema_support import refresh_memberships
from tests.conftest import unwrap_extracted_collaborator
from tests.matrix_state_helpers import save_matrix_state
from tests.response_runner_helpers import _bot, _plain_request, _target

if TYPE_CHECKING:
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

    with pytest.raises(ReplyMembershipPendingError):
        runner.locked.requester_remains_authorized("@user:localhost", request.room_id)
    with pytest.raises(ReplyMembershipPendingError):
        await runner.locked.request_remains_authorized(request)

    membership_client = AsyncMock(spec=nio.AsyncClient)
    membership_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!grant:localhost"])
    membership_client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember("@user:localhost", None, None)],
        room_id="!grant:localhost",
    )
    await refresh_memberships(
        runner.deps.runtime.agent_reply_memberships,
        bot.config,
        bot.runtime_paths,
        membership_client,
    )

    assert runner.locked.requester_remains_authorized("@user:localhost", request.room_id) is True
    assert await runner.locked.request_remains_authorized(request) is True

    membership_client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[],
        room_id="!grant:localhost",
    )
    await refresh_memberships(
        runner.deps.runtime.agent_reply_memberships,
        bot.config,
        bot.runtime_paths,
        membership_client,
    )
    assert runner.locked.requester_remains_authorized("@user:localhost", request.room_id) is False
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
    runner.locked.deps = replace(runner.deps, agent_name="crew")

    assert (
        runner.locked.requester_remains_authorized(
            "@alice:localhost",
            "!room:localhost",
            reply_entity_names=("member",),
        )
        is True
    )
    assert (
        runner.locked.requester_remains_authorized(
            "@only-member:localhost",
            "!room:localhost",
            reply_entity_names=("member",),
        )
        is False
    )


@pytest.mark.asyncio
async def test_load_scrubbed_scope_session_drains_load_on_cancel_and_scrubs_redactions(tmp_path: Path) -> None:
    """Loading a scrubbed scope drains cancellation during blocking I/O and removes redacted runs."""
    import asyncio
    import threading
    from functools import partial

    from mindroom.history.session_context import load_scope_session_from_storage, load_scrubbed_scope_session
    from mindroom.history.types import HistoryScope
    from mindroom.session_storage.agent_storage import create_state_storage
    from mindroom.llm.runs import RunRecord
    from mindroom.session_storage.types import ConversationSession

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

    def blocking_load():
        entered.set()
        release.wait(timeout=5)
        try:
            return load_scope_session_from_storage(
                scope=scope,
                session_id="sess-1",
                storage_factory=lambda: storage,
            )
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
        partial(load_scope_session_from_storage, scope=scope, session_id="sess-1", storage_factory=lambda: storage),
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
    from mindroom import turn_store as turn_store_module
    from mindroom.event_journal import EventJournalStore
    from mindroom.history.session_context import load_scrubbed_scope_session
    from mindroom.history.types import HistoryScope
    from mindroom.message_target import MessageTarget
    from mindroom.session_storage.agent_storage import create_state_storage
    from mindroom.llm.runs import RunRecord
    from mindroom.session_storage.types import ConversationSession
    from mindroom.turn_record import TurnRecord
    from tests.test_turn_store import _store_with_storage

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

        async def spy(load_scope, redactions):
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
