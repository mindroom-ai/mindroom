"""Tests for canonical turn ownership, precedence, and repair."""

from __future__ import annotations

import ast
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import ANY, MagicMock, patch

import pytest
from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom import constants
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.event_journal.store import TurnRecordStore
from mindroom.handled_turns import (
    SourceEventMetadata,
    TurnRecord,
    TurnRecordCodec,
    _reset_handled_turn_ledger_runtime,
)
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    read_scope_state,
    update_scope_seen_event_ids,
    write_scope_state,
)
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.text_ingress_dispatch import _run_claimed_response
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from mindroom.event_journal import EventJournalStore


async def _store(journal_store: EventJournalStore, *, agent_name: str = "agent") -> TurnStore:
    """Return a warmed store over this journal's turn records.

    Warming is not optional any more: the ledger's synchronous reads answer
    from a map that ``warm`` fills, and refuse until it has.
    """
    store = TurnStore(
        TurnStoreDeps(
            agent_name=agent_name,
            turn_records=journal_store.turn_records(agent_name),
            legacy_responses_file=None,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )
    await store.warm()
    return store


async def _load_with_recovery(
    store: TurnStore,
    *,
    original_event_id: str,
    recovery_record: TurnRecord | None,
) -> TurnRecord | None:
    room = MagicMock(room_id="!room:example.org")
    with patch.object(store, "_load_persisted_turn_record", return_value=recovery_record):
        return await store.load_turn(
            room=room,
            thread_id=None,
            original_event_id=original_event_id,
            requester_user_id="@user:example.org",
        )


@dataclass
class _FakeAgentStorage:
    session: AgentSession | TeamSession | None
    upserted_session: AgentSession | TeamSession | None = None

    def get_session(self, session_id: str, _session_type: object) -> AgentSession | TeamSession | None:
        if self.session is None or self.session.session_id != session_id:
            return None
        return self.session

    def upsert_session(self, session: AgentSession | TeamSession) -> None:
        self.upserted_session = session

    def close(self) -> None:
        return None


async def _store_with_storage(
    journal_store: EventJournalStore,
    storage: _FakeAgentStorage,
    *,
    agent_name: str = "agent",
) -> TurnStore:
    state_writer = MagicMock()
    state_writer.create_storage.return_value = storage
    state_writer.session_type_for_scope.return_value = SessionType.AGENT
    state_writer.history_scope.return_value = HistoryScope(kind="agent", scope_id="agent")
    store = TurnStore(
        TurnStoreDeps(
            agent_name=agent_name,
            turn_records=journal_store.turn_records(agent_name),
            legacy_responses_file=None,
            state_writer=state_writer,
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )
    await store.warm()
    return store


def _owned_turn_record(target: MessageTarget) -> TurnRecord:
    return TurnRecord.create(
        ["$user_msg"],
        response_event_id="$reply",
        response_owner="agent",
        requester_id="@user:example.org",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=target,
    )


async def _prepare_redaction(
    store: TurnStore,
    target: MessageTarget,
    *,
    redacted_event_id: str = "$user_msg",
) -> bool:
    """Tombstone one source and run the next response's locked cleanup gate."""
    await store.mark_source_redacted(redacted_event_id)
    return await store._prepare_response_for_redactions(
        target=target,
        source_event_ids=("$later",),
    )


@pytest.mark.asyncio
async def test_user_stop_durably_terminates_the_turn_that_owns_the_response(journal_store: EventJournalStore) -> None:
    """A user stop must become durable turn truth before dispatch settles it."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", None, "$source")
    pending = TurnRecord.create(
        ["$source"],
        response_event_id="$reply",
        completed=False,
        response_owner="agent",
        requester_id="@user:example.org",
        conversation_target=target,
    )
    await store.record_pending_turn(pending)

    stop_receipt_order = 2
    stopped = await store.record_user_stopped_response("$reply", stop_receipt_order)

    assert stopped is not None
    assert stopped.completed is True
    assert stopped.user_stop_receipt_order == stop_receipt_order
    assert stopped.user_stop_settled_receipt_order is None
    assert store.is_handled("$source") is True
    retried = await store.record_user_stopped_response("$reply", stop_receipt_order)
    assert retried is not None
    assert retried.completed is True
    assert retried.user_stop_receipt_order == stop_receipt_order
    assert retried.user_stop_settled_receipt_order is None

    finalized = await store.record_user_stopped_response(
        "$reply",
        stop_receipt_order,
        delivery_settled=True,
    )
    assert finalized is not None
    assert finalized.user_stop_settled_receipt_order == stop_receipt_order


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_receipt_order", [0, -1, True])
async def test_user_stop_rejects_invalid_receipt_order(
    journal_store: EventJournalStore,
    invalid_receipt_order: int,
) -> None:
    """STOP ordering requires a real positive durable admission sequence."""
    with pytest.raises(ValueError, match="receipt order must be positive"):
        await (await _store(journal_store)).record_user_stopped_response("$reply", invalid_receipt_order)


@pytest.mark.asyncio
async def test_locked_pending_response_preparation_suppresses_a_concurrent_user_stop(
    journal_store: EventJournalStore,
) -> None:
    """A response queued before STOP must recheck terminal truth after taking its lock."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", None, "$source")
    pending = TurnRecord.create(
        ["$source"],
        response_event_id="$reply",
        completed=False,
        response_owner="agent",
        requester_id="@user:example.org",
        conversation_target=target,
    )
    await store.record_pending_turn(pending)
    assert (
        await store.prepare_pending_response_source(
            target=target,
            source_event_ids=("$source",),
            terminal_source_event_ids=("$source",),
        )
        is False
    )

    await store.record_user_stopped_response("$reply", 2)

    assert (
        await store.prepare_pending_response_source(
            target=target,
            source_event_ids=("$source",),
            terminal_source_event_ids=("$source",),
        )
        is True
    )


@pytest.mark.asyncio
async def test_locked_edit_preparation_uses_stop_order_and_settles_superseded_delivery(
    journal_store: EventJournalStore,
) -> None:
    """Only later edits run, and they durably supersede an older STOP delivery."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", None, "$source")
    await store.record_turn(
        TurnRecord.create(
            ["$source"],
            response_event_id="$reply",
            response_owner="agent",
            requester_id="@user:example.org",
            conversation_target=target,
            user_stop_receipt_order=2,
        ),
    )

    assert await store.prepare_edit_response_source(
        target=target,
        source_event_ids=("$source",),
        response_event_id="$reply",
        edit_receipt_order=1,
    )
    stopped = store.get_turn_record("$source")
    assert stopped is not None
    assert stopped.latest_edit_receipt_order is None
    assert stopped.user_stop_settled_receipt_order is None

    assert not await store.prepare_edit_response_source(
        target=target,
        source_event_ids=("$source",),
        response_event_id="$reply",
        edit_receipt_order=3,
    )
    reopened = store.get_turn_record("$source")
    assert reopened is not None
    assert reopened.latest_edit_receipt_order == 3
    assert reopened.user_stop_settled_receipt_order == 2


@pytest.mark.asyncio
async def test_pending_delivery_intent_does_not_require_model_history_scope(journal_store: EventJournalStore) -> None:
    """Response ownership and target distinguish delivery intent from a raw visible echo."""
    store = await _store(journal_store)
    source_event_ids = ("$source",)
    visible_echo = TurnRecord.create(
        source_event_ids,
        response_event_id="$echo",
        completed=False,
    )
    await store.record_pending_turn(visible_echo)
    assert store.has_pending_response_intent(source_event_ids) is False

    target = MessageTarget.resolve("!room:example.org", None, "$source")
    delivery_intent = store.attach_response_context(
        visible_echo,
        history_scope=None,
        conversation_target=target,
    )
    await store.record_pending_turn(delivery_intent)

    assert store.has_pending_response_intent(source_event_ids) is True
    record = store.get_turn_record("$source")
    assert record is not None
    assert record.response_owner == "agent"
    assert record.conversation_target == target
    assert record.history_scope is None


@pytest.mark.asyncio
async def test_a_live_claim_is_observable_without_waiting_for_it(journal_store: EventJournalStore) -> None:
    """Whether a turn still owns a source, asked without blocking on the answer.

    The claim is taken before the turn is handed off and dropped however the
    turn ends, so its presence is what separates "a turn owns this" from "a
    turn owned this and is gone". A caller holding durable work owed to that
    turn has to be able to ask without joining the wait.
    """
    store = await _store(journal_store)
    claim = TurnRecord.create(("$source",), discovery_event_ids=("$alias",), completed=False)

    assert store.has_live_turn_claim("$source") is False

    assert store.try_claim_turn(claim) is True
    assert store.has_live_turn_claim("$source") is True
    assert store.has_live_turn_claim("$alias") is True, "an alias is indexed by the same claim"
    assert store.has_live_turn_claim("$unrelated") is False

    store.release_pending_turn_claim(claim)

    assert store.has_live_turn_claim("$source") is False
    assert store.has_live_turn_claim("$alias") is False


@pytest.mark.asyncio
async def test_pending_turn_claim_allows_only_one_concurrent_owner(journal_store: EventJournalStore) -> None:
    """Overlapping delivery of one source event must start one response."""
    store = await _store(journal_store)
    turn = TurnRecord.create(["$source"], completed=False)
    barrier = threading.Barrier(8)
    claims = [False] * barrier.parties

    def claim(index: int) -> None:
        barrier.wait()
        claims[index] = store.try_claim_turn(turn)

    threads = [threading.Thread(target=claim, args=(index,)) for index in range(barrier.parties)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(claims) == 1
    store.release_pending_turn_claim(turn)
    assert store.try_claim_turn(turn) is True


@pytest.mark.asyncio
async def test_discovery_alias_allows_original_but_excludes_second_relay(journal_store: EventJournalStore) -> None:
    """One human original may overlap its relay, but two relays for it may not."""
    store = await _store(journal_store)
    original = TurnRecord.create(["$human"], completed=False)
    first_relay = TurnRecord.create(["$relay-one"], discovery_event_ids=["$human"], completed=False)
    duplicate_relay = TurnRecord.create(["$relay-two"], discovery_event_ids=["$human"], completed=False)

    assert store.try_claim_turn(original) is True
    assert store.try_claim_turn(first_relay) is True
    assert store.try_claim_turn(duplicate_relay) is False

    store.release_pending_turn_claim(first_relay)
    assert store.try_claim_turn(duplicate_relay) is True
    store.release_pending_turn_claim(duplicate_relay)
    store.release_pending_turn_claim(original)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_claim", [False, True])
async def test_same_turn_can_reclaim_its_handled_alias(
    journal_store: EventJournalStore,
    *,
    completed_claim: bool,
) -> None:
    """A relay turn must remain claimable for its own edit or restart drain."""
    store = await _store(journal_store)
    completed = TurnRecord.create(["$relay"], discovery_event_ids=["$human"])
    await store.record_turn(completed)
    claim = replace(completed, completed=completed_claim)

    assert store.try_claim_turn(claim) is True

    store.release_pending_turn_claim(claim)


@pytest.mark.asyncio
async def test_pending_coalesced_turn_can_reclaim_tombstoned_alias(journal_store: EventJournalStore) -> None:
    """A sibling edit remains claimable after another alias is tombstoned."""
    store = await _store(journal_store)
    pending = TurnRecord.create(
        ["$relay-one", "$relay-two"],
        discovery_event_ids=["$human-one", "$human-two"],
        redacted_source_event_ids=["$human-two"],
        completed=False,
    )
    await store.record_pending_turn(pending)

    assert store.is_handled("$human-two") is True
    assert store.try_claim_turn(pending) is True

    store.release_pending_turn_claim(pending)


@pytest.mark.asyncio
async def test_turn_settlement_waits_for_pending_claim_release(journal_store: EventJournalStore) -> None:
    """A waiter should remain blocked until response ownership reaches its existing release seam."""
    store = await _store(journal_store)
    turn = TurnRecord.create(["$source"], completed=False)
    assert store.try_claim_turn(turn) is True
    wait_started = asyncio.Event()

    async def wait_for_settlement() -> None:
        wait_started.set()
        await store.wait_for_turn_settled(turn.indexed_event_ids)

    waiter = asyncio.create_task(wait_for_settlement())
    await wait_started.wait()
    assert not waiter.done()

    store.release_pending_turn_claim(turn)
    await waiter


@pytest.mark.asyncio
async def test_distinct_physical_claims_can_share_alias_until_both_settle(journal_store: EventJournalStore) -> None:
    """A discovery alias coordinates settlement without rejecting its physical relay."""
    store = await _store(journal_store)
    original = TurnRecord.create(["$human"], completed=False)
    relay = TurnRecord.create(["$relay"], discovery_event_ids=["$human"], completed=False)
    assert store.try_claim_turn(original) is True
    assert store.try_claim_turn(relay) is True
    first_wait_started = asyncio.Event()

    async def wait_for_alias(started: asyncio.Event) -> None:
        started.set()
        await store.wait_for_turn_settled(("$human",))

    waiter = asyncio.create_task(wait_for_alias(first_wait_started))
    await first_wait_started.wait()
    assert not waiter.done()

    store.release_pending_turn_claim(original)
    second_wait_started = asyncio.Event()
    second_waiter = asyncio.create_task(wait_for_alias(second_wait_started))
    await second_wait_started.wait()
    assert not second_waiter.done()
    store.release_pending_turn_claim(relay)
    await asyncio.gather(waiter, second_waiter)


@pytest.mark.asyncio
async def test_turn_settlement_wait_does_not_consume_default_executor(journal_store: EventJournalStore) -> None:
    """Claim settlement must progress while every default-executor worker is occupied."""
    store = await _store(journal_store)
    turn = TurnRecord.create(["$source"], completed=False)
    loop = asyncio.get_running_loop()
    # One worker, entirely occupied below: anything that reaches for the default
    # executor from here on cannot make progress, which is the point.
    loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
    worker_started = asyncio.Event()
    release_worker = threading.Event()

    def occupy_worker() -> None:
        loop.call_soon_threadsafe(worker_started.set)
        release_worker.wait()

    blocker = asyncio.create_task(asyncio.to_thread(occupy_worker))
    await worker_started.wait()
    try:
        assert store.try_claim_turn(turn) is True
        wait_started = asyncio.Event()

        async def wait_for_settlement() -> None:
            wait_started.set()
            await store.wait_for_turn_settled(turn.indexed_event_ids)

        waiter = asyncio.create_task(wait_for_settlement())
        await wait_started.wait()
        assert not waiter.done()
        store.release_pending_turn_claim(turn)
        async with asyncio.timeout(1):
            await waiter
    finally:
        release_worker.set()
        await blocker


@pytest.mark.asyncio
async def test_failed_claimed_response_releases_turn_for_replay(journal_store: EventJournalStore) -> None:
    """A failed owner must not permanently suppress a later delivery."""
    store = await _store(journal_store)
    turn = TurnRecord.create(["$source"], completed=False)
    assert store.try_claim_turn(turn) is True

    async def fail() -> None:
        msg = "response failed"
        raise RuntimeError(msg)

    controller = SimpleNamespace(deps=SimpleNamespace(turn_store=store))
    with pytest.raises(RuntimeError, match="response failed"):
        await _run_claimed_response(controller, turn, fail())

    assert store.try_claim_turn(turn) is True


@pytest.mark.asyncio
async def test_terminal_turn_keeps_claim_until_response_task_finishes(journal_store: EventJournalStore) -> None:
    """Terminal persistence must not reopen a source before task cleanup completes."""
    store = await _store(journal_store)
    turn = TurnRecord.create(["$source"], completed=False)
    other_turn = TurnRecord.create(["$discovery"], completed=False)
    expanded_turn = replace(turn, discovery_event_ids=other_turn.source_event_ids)
    terminal_recorded = asyncio.Event()
    release_response = asyncio.Event()
    assert store.try_claim_turn(turn) is True
    assert store.try_claim_turn(other_turn) is True

    async def finish_response() -> None:
        await store.record_turn(expanded_turn)
        terminal_recorded.set()
        await release_response.wait()

    controller = SimpleNamespace(deps=SimpleNamespace(turn_store=store))
    response_task = asyncio.create_task(_run_claimed_response(controller, turn, finish_response()))
    await terminal_recorded.wait()

    assert store.try_claim_turn(turn) is False
    release_response.set()
    await response_task
    assert store.try_claim_turn(turn) is True
    assert store.try_claim_turn(other_turn) is False


@pytest.mark.asyncio
async def test_prepare_redaction_removes_causal_run_suffix(journal_store: EventJournalStore) -> None:
    """Redacting a source must delete later output that may depend on that run."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[
            RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$user_msg"}),
            RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$other"}),
        ],
    )
    storage = _FakeAgentStorage(session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))

    should_suppress = await _prepare_redaction(store, target)

    assert should_suppress is False
    assert storage.upserted_session is session
    assert session.runs == []


@pytest.mark.asyncio
async def test_edit_then_redaction_never_replays_the_old_causal_suffix(journal_store: EventJournalStore) -> None:
    """Editing a source must remove later output before its replacement is persisted."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$source")
    scope = HistoryScope(kind="agent", scope_id="agent")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[
            RunOutput(
                session_id=target.session_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$source",
                    constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$source"],
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$source"],
                },
            ),
            RunOutput(
                session_id=target.session_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$dependent",
                    constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$dependent"],
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$dependent"],
                },
            ),
        ],
    )
    storage = _FakeAgentStorage(session)
    store = await _store_with_storage(journal_store, storage)
    source_record = TurnRecord.create(
        ["$source"],
        response_event_id="$reply",
        response_owner="agent",
        requester_id="@user:example.org",
        history_scope=scope,
        conversation_target=target,
    )
    await store.record_turn(source_record)

    store.remove_stale_runs_for_edit(
        turn_record=source_record,
        requester_user_id="@user:example.org",
    )
    assert session.runs == []

    session.runs = [
        RunOutput(
            session_id=target.session_id,
            metadata={
                constants.MATRIX_EVENT_ID_METADATA_KEY: "$source",
                constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$source"],
                constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$source"],
            },
        ),
    ]
    should_suppress = await _prepare_redaction(store, target, redacted_event_id="$source")

    assert should_suppress is False
    assert session.runs == []


@pytest.mark.asyncio
async def test_prepare_redaction_removes_runs_that_consumed_the_source(journal_store: EventJournalStore) -> None:
    """A later run that consumed redacted context must not remain eligible for replay."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[
            RunOutput(
                session_id=target.session_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$later",
                    constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$later"],
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$user_msg", "$later"],
                },
            ),
        ],
    )
    storage = _FakeAgentStorage(session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))

    should_suppress = await _prepare_redaction(store, target)

    assert should_suppress is False
    assert session.runs == []


@pytest.mark.asyncio
async def test_prepare_redaction_removes_source_from_every_recorded_history_scope(
    journal_store: EventJournalStore,
) -> None:
    """Later ad-hoc responses must not retain a source consumed outside its original scope."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    agent_scope = HistoryScope(kind="agent", scope_id="agent")
    team_scope = HistoryScope(kind="team", scope_id="team_private")
    agent_session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$user_msg"})],
    )
    team_session = TeamSession(
        session_id=target.session_id,
        team_id=team_scope.scope_id,
        runs=[
            TeamRunOutput(
                session_id=target.session_id,
                team_id=team_scope.scope_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$later",
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$user_msg", "$later"],
                },
            ),
        ],
    )
    storages = {
        agent_scope.key: _FakeAgentStorage(agent_session),
        team_scope.key: _FakeAgentStorage(team_session),
    }
    state_writer = MagicMock()
    state_writer.create_storage.side_effect = lambda _identity, *, scope: storages[scope.key]
    state_writer.session_type_for_scope.side_effect = lambda scope: (
        SessionType.TEAM if scope.kind == "team" else SessionType.AGENT
    )
    state_writer.history_scope.return_value = agent_scope
    store = TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            turn_records=journal_store.turn_records("agent"),
            legacy_responses_file=None,
            state_writer=state_writer,
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )
    await store.warm()
    await store.record_turn(_owned_turn_record(target))
    await store.record_turn(
        TurnRecord.create(
            ["$later"],
            response_event_id="$later-reply",
            response_owner="agent",
            requester_id="@user:example.org",
            history_scope=team_scope,
            conversation_target=target,
        ),
    )

    should_suppress = await _prepare_redaction(store, target)

    assert should_suppress is False
    assert agent_session.runs == []
    assert team_session.runs == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source_owner", [None, "other"])
async def test_prepare_redaction_cleans_later_owned_scopes_across_requesters(
    journal_store: EventJournalStore,
    *,
    source_owner: str | None,
) -> None:
    """An unowned source can still contaminate a later private response scope."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    default_scope = HistoryScope(kind="agent", scope_id="agent")
    team_scope = HistoryScope(kind="team", scope_id="team_private")
    team_session = TeamSession(
        session_id=target.session_id,
        team_id=team_scope.scope_id,
        runs=[
            TeamRunOutput(
                session_id=target.session_id,
                team_id=team_scope.scope_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$later",
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$user_msg", "$later"],
                },
            ),
        ],
    )
    storages = {
        (default_scope.key, "@source:example.org"): _FakeAgentStorage(None),
        (team_scope.key, "@later:example.org"): _FakeAgentStorage(team_session),
    }
    state_writer = MagicMock()
    state_writer.history_scope.return_value = default_scope
    state_writer.create_storage.side_effect = lambda identity, *, scope: storages[(scope.key, identity)]
    state_writer.session_type_for_scope.side_effect = lambda scope: (
        SessionType.TEAM if scope.kind == "team" else SessionType.AGENT
    )
    tool_runtime = MagicMock()
    tool_runtime.build_execution_identity.side_effect = lambda *, target, user_id: user_id  # noqa: ARG005
    store = TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            turn_records=journal_store.turn_records("agent"),
            legacy_responses_file=None,
            state_writer=state_writer,
            resolver=MagicMock(),
            tool_runtime=tool_runtime,
        ),
    )
    await store.warm()
    await store.record_turn(
        replace(
            _owned_turn_record(target),
            response_owner=source_owner,
            requester_id="@source:example.org",
        ),
    )
    await store.record_turn(
        TurnRecord.create(
            ["$later"],
            response_event_id="$later-reply",
            response_owner="agent",
            requester_id="@later:example.org",
            history_scope=team_scope,
            conversation_target=target,
        ),
    )

    should_suppress = await _prepare_redaction(store, target)

    assert should_suppress is False
    assert team_session.runs == []
    assert {call.kwargs["user_id"] for call in tool_runtime.build_execution_identity.call_args_list} == {
        "@source:example.org",
        "@later:example.org",
    }


@pytest.mark.asyncio
async def test_tombstone_gains_cleanup_context_when_the_source_turn_registers(journal_store: EventJournalStore) -> None:
    """A redaction race should become cleanup work only when its source turn registers."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$user_msg"})],
    )
    storage = _FakeAgentStorage(session)
    store = await _store_with_storage(journal_store, storage)
    marked = await store.mark_source_redacted("$user_msg")
    assert marked is not None
    assert marked.conversation_target is None
    assert marked.pending_redaction_cleanup_event_ids == ()
    pending = await store.record_pending_turn(
        TurnRecord.create(
            ["$user_msg"],
            completed=False,
            requester_id="@user:example.org",
            response_owner="agent",
            history_scope=HistoryScope(kind="agent", scope_id="agent"),
            conversation_target=target,
        ),
    )
    assert pending is not None
    assert pending.pending_redaction_cleanup_event_ids == ("$user_msg",)

    should_suppress = await store._prepare_response_for_redactions(
        target=target,
        source_event_ids=("$user_msg",),
    )

    assert should_suppress is True
    assert session.runs == []
    cleaned = store.get_turn_record("$user_msg")
    assert cleaned is not None
    assert cleaned.pending_redaction_cleanup_event_ids == ()


@pytest.mark.asyncio
async def test_prepare_redaction_invalidates_compacted_replay(journal_store: EventJournalStore) -> None:
    """Redaction must remove content already folded into the durable summary."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="agent", scope_id="agent")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[
            RunOutput(
                session_id=target.session_id,
                metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$post-compaction"},
            ),
        ],
        summary=SessionSummary(summary="The user disclosed REDACTED_SECRET."),
    )
    update_scope_seen_event_ids(session, scope, ["$user_msg", "$older"])
    write_scope_state(
        session,
        scope,
        HistoryScopeState(last_summary_model="summary-model", compacted_run_ids=("$compacted-run",)),
    )
    storage = _FakeAgentStorage(session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))

    should_suppress = await _prepare_redaction(store, target)

    assert should_suppress is False
    assert storage.upserted_session is session
    assert session.runs == []
    assert session.summary is None
    assert read_scope_seen_event_ids(session, scope) == set()
    assert read_scope_state(session, scope) == HistoryScopeState(compacted_run_ids=("$compacted-run",))


@pytest.mark.asyncio
async def test_redaction_before_response_registration_tombstones_pending_coalesced_turn(
    journal_store: EventJournalStore,
) -> None:
    """A source redacted before response startup must suppress its later pending batch."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$second")
    team_scope = HistoryScope(kind="team", scope_id="team_private")

    await store.mark_source_redacted("$first")
    pending = await store.record_pending_turn(
        TurnRecord.create(
            ["$first", "$second"],
            source_event_prompts={"$first": "REDACTED_SECRET", "$second": "keep"},
            requester_id="@user:example.org",
            response_owner="agent",
            history_scope=team_scope,
            conversation_target=target,
        ),
    )

    assert pending is not None
    assert pending.completed is False
    assert pending.redacted_source_event_ids == ("$first",)
    assert pending.source_event_prompts == {"$second": "keep"}
    assert pending.history_scope == team_scope
    assert store.is_handled("$first") is True
    assert store.is_handled("$second") is False


@pytest.mark.asyncio
async def test_redaction_detaches_from_a_pending_coalesced_turn_after_sibling_completion(
    journal_store: EventJournalStore,
) -> None:
    """A split sibling identity must not veto the remaining source tombstone."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$second")
    scope = HistoryScope(kind="agent", scope_id="agent")
    pending = TurnRecord.create(
        ["$first", "$second"],
        completed=False,
        requester_id="@user:example.org",
        response_owner="agent",
        history_scope=scope,
        conversation_target=target,
    )
    await store.record_pending_turn(pending)
    await store.record_turn(
        TurnRecord.create(
            ["$second"],
            response_event_id="$second-reply",
            requester_id="@user:example.org",
            response_owner="agent",
            history_scope=scope,
            conversation_target=target,
        ),
    )

    marked = await store.mark_source_redacted("$first")

    assert marked is not None
    assert marked.source_event_ids == ("$first",)
    assert marked.anchor_event_id == "$first"
    assert marked.redacted_source_event_ids == ("$first",)
    assert marked.pending_redaction_cleanup_event_ids == ("$first",)
    completed_sibling = store.get_turn_record("$second")
    assert completed_sibling is not None
    assert completed_sibling.source_event_ids == ("$second",)
    assert completed_sibling.response_event_id == "$second-reply"


@pytest.mark.asyncio
async def test_redaction_cleanup_clears_after_pending_coalesced_turn_splits(journal_store: EventJournalStore) -> None:
    """A completed sibling must not leave an old alias cleanup pending forever."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$second")
    scope = HistoryScope(kind="agent", scope_id="agent")
    storage = _FakeAgentStorage(None)
    store = await _store_with_storage(journal_store, storage)
    pending = TurnRecord.create(
        ["$first", "$second"],
        completed=False,
        requester_id="@user:example.org",
        response_owner="agent",
        history_scope=scope,
        conversation_target=target,
    )
    await store.record_pending_turn(pending)
    await store.mark_source_redacted("$first")
    await store.record_turn(
        TurnRecord.create(
            ["$second"],
            response_event_id="$second-reply",
            requester_id="@user:example.org",
            response_owner="agent",
            history_scope=scope,
            conversation_target=target,
        ),
    )

    should_suppress = await store._prepare_response_for_redactions(
        target=target,
        source_event_ids=("$second",),
    )

    assert should_suppress is False
    cleaned = store.get_turn_record("$first")
    assert cleaned is not None
    assert cleaned.source_event_ids == ("$first",)
    assert cleaned.redacted_source_event_ids == ("$first",)
    assert cleaned.pending_redaction_cleanup_event_ids == ()
    completed_sibling = store.get_turn_record("$second")
    assert completed_sibling is not None
    assert completed_sibling.source_event_ids == ("$second",)
    assert completed_sibling.response_event_id == "$second-reply"


@pytest.mark.asyncio
async def test_redaction_cleanup_keeps_context_after_colliding_alias_projection(
    journal_store: EventJournalStore,
) -> None:
    """Projecting a redacted physical source must retain the context needed to sanitize it."""
    relay_event_id = "$relay"
    human_event_id = "$human"
    requester_user_id = "@alice:example.org"
    target = MessageTarget.resolve("!room:example.org", "$thread", human_event_id)
    scope = HistoryScope(kind="agent", scope_id="agent")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[
            RunOutput(
                session_id=target.session_id,
                metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: human_event_id},
            ),
        ],
        summary=SessionSummary(summary="contains REDACTED_SECRET"),
    )
    update_scope_seen_event_ids(session, scope, [human_event_id])
    storage = _FakeAgentStorage(session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_pending_turn(
        TurnRecord.create(
            [relay_event_id, human_event_id],
            completed=False,
            source_event_metadata={
                relay_event_id: SourceEventMetadata(
                    sender="@bob:example.org",
                    discovery_event_id=human_event_id,
                ),
                human_event_id: SourceEventMetadata(sender=requester_user_id),
            },
            requester_id=requester_user_id,
            response_owner="agent",
            history_scope=scope,
            conversation_target=target,
        ),
    )
    await store.record_turn(
        TurnRecord.create(
            [relay_event_id],
            response_event_id="$relay-reply",
            requester_id="@bob:example.org",
        ),
    )

    projected = await store.mark_source_redacted(human_event_id)

    assert projected is not None
    assert projected.source_event_ids == (human_event_id,)
    assert projected.source_event_metadata == {}
    assert projected.requester_id == requester_user_id
    assert projected.requester_id_for_source(human_event_id) is None
    assert projected.pending_redaction_cleanup_event_ids == (human_event_id,)

    should_suppress = await store._prepare_response_for_redactions(
        target=target,
        source_event_ids=("$later",),
    )

    assert should_suppress is False
    assert storage.upserted_session is session
    assert session.summary is None
    assert read_scope_seen_event_ids(session, scope) == set()
    cleaned = store.get_turn_record(human_event_id)
    assert cleaned is not None
    assert cleaned.pending_redaction_cleanup_event_ids == ()


@pytest.mark.asyncio
async def test_active_ad_hoc_team_redaction_uses_pending_response_scope(journal_store: EventJournalStore) -> None:
    """Post-lock cleanup must retain the exact team scope recorded before generation."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="team", scope_id="team_private")
    session = TeamSession(
        session_id=target.session_id,
        team_id=scope.scope_id,
        runs=[
            TeamRunOutput(
                session_id=target.session_id,
                team_id=scope.scope_id,
                metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$user_msg"},
            ),
        ],
    )
    storage = MagicMock()
    storage.get_session.return_value = session
    state_writer = MagicMock()
    state_writer.create_storage.return_value = storage
    state_writer.session_type_for_scope.return_value = SessionType.TEAM
    state_writer.history_scope.return_value = HistoryScope(kind="agent", scope_id="agent")
    store = TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            turn_records=journal_store.turn_records("agent"),
            legacy_responses_file=None,
            state_writer=state_writer,
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )
    await store.warm()
    response_record = TurnRecord.create(
        ["$user_msg"],
        requester_id="@user:example.org",
        response_owner="agent",
        history_scope=scope,
        conversation_target=target,
    )
    await store.record_pending_turn(response_record)
    await store.mark_source_redacted("$user_msg")
    await store.record_turn(replace(response_record, response_event_id="$reply"))

    should_suppress = await store._prepare_response_for_redactions(
        target=target,
        source_event_ids=("$later",),
    )

    assert should_suppress is False
    assert session.runs == []
    state_writer.create_storage.assert_called_with(ANY, scope=scope)


@pytest.mark.asyncio
async def test_redaction_sanitizes_coalesced_ledger_prompt_and_metadata(journal_store: EventJournalStore) -> None:
    """Sibling edit regeneration must not recover a redacted coalesced prompt."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$second")
    await store.record_turn(
        TurnRecord.create(
            ["$first", "$second"],
            source_event_prompts={"$first": "REDACTED_SECRET", "$second": "keep"},
            source_event_metadata={
                "$first": SourceEventMetadata(sender="@first:example.org"),
                "$second": SourceEventMetadata(sender="@second:example.org"),
            },
            requester_id="@user:example.org",
            response_owner="agent",
            history_scope=HistoryScope(kind="agent", scope_id="agent"),
            conversation_target=target,
        ),
    )

    sanitized = await store.mark_source_redacted("$first")

    assert sanitized is not None
    assert sanitized.redacted_source_event_ids == ("$first",)
    assert sanitized.source_event_prompts == {"$second": "keep"}
    assert sanitized.source_event_metadata == {
        "$second": SourceEventMetadata(sender="@second:example.org"),
    }
    assert store.get_turn_record("$second") == sanitized


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_turn_merge_preserves_redacted_discovery_alias(
    journal_store: EventJournalStore,
    *,
    terminal: bool,
) -> None:
    """Backfilled aliases must retain their tombstone and cleanup intent across turn merges."""
    store = await _store(journal_store)
    existing = TurnRecord.create(
        ["$question"],
        discovery_event_ids=["$selection"],
        redacted_source_event_ids=["$selection"],
        pending_redaction_cleanup_event_ids=["$selection"],
        completed=False,
    )
    await store.record_pending_turn(existing)
    incoming = TurnRecord.create(
        ["$question"],
        response_event_id="$response" if terminal else None,
        completed=terminal,
    )

    if terminal:
        await store.record_turn(incoming)
    else:
        await store.record_pending_turn(incoming)

    merged = store.get_turn_record("$selection")
    assert merged is not None
    assert merged.discovery_event_ids == ("$selection",)
    assert merged.redacted_source_event_ids == ("$selection",)
    assert merged.pending_redaction_cleanup_event_ids == ("$selection",)


@pytest.mark.asyncio
async def test_multi_bot_redaction_only_queues_cleanup_for_the_bot_with_context(
    journal_store: EventJournalStore,
) -> None:
    """Bots that never handled a source must not accumulate or probe cleanup work."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="agent", scope_id="agent")
    owner_session = AgentSession(
        session_id=target.session_id,
        agent_id="owner",
        runs=[],
        summary=SessionSummary(summary="contains REDACTED_SECRET"),
    )
    update_scope_seen_event_ids(owner_session, scope, ["$user_msg"])
    owner_store = await _store_with_storage(
        journal_store,
        _FakeAgentStorage(owner_session),
        agent_name="owner",
    )
    await owner_store.record_turn(
        replace(
            _owned_turn_record(target),
            response_owner="owner",
        ),
    )
    unrelated_session = AgentSession(
        session_id=target.session_id,
        agent_id="unrelated",
        runs=[],
        summary=SessionSummary(summary="unrelated"),
    )
    unrelated_store = await _store_with_storage(
        journal_store,
        _FakeAgentStorage(unrelated_session),
        agent_name="unrelated",
    )

    owner_marked = await owner_store.mark_source_redacted("$user_msg")
    unrelated_marked = await unrelated_store.mark_source_redacted("$user_msg")

    assert owner_marked is not None
    assert owner_marked.pending_redaction_cleanup_event_ids == ("$user_msg",)
    assert unrelated_marked is not None
    assert unrelated_marked.redacted_source_event_ids == ("$user_msg",)
    assert unrelated_marked.pending_redaction_cleanup_event_ids == ()

    await owner_store._prepare_response_for_redactions(
        target=target,
        source_event_ids=("$later",),
    )
    should_suppress = await unrelated_store._prepare_response_for_redactions(
        target=target,
        source_event_ids=("$later",),
    )

    assert should_suppress is False
    assert owner_session.summary is None
    assert read_scope_seen_event_ids(owner_session, scope) == set()
    assert unrelated_session.summary is not None
    unrelated_store.deps.state_writer.create_storage.assert_not_called()


@pytest.mark.asyncio
async def test_turn_store_constructs_private_ledger_over_its_turn_records(
    journal_store: EventJournalStore,
) -> None:
    """TurnStore should own its private ledger and persist through the injected record store."""
    store = await _store(journal_store)

    await store.record_turn(TurnRecord.create(["$event"], response_event_id="$response"))

    _reset_handled_turn_ledger_runtime()
    reloaded_store = await _store(journal_store)

    assert reloaded_store.is_handled("$event")
    turn_record = reloaded_store.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"


@pytest.mark.asyncio
async def test_redaction_tombstone_persists_across_ledger_reload(journal_store: EventJournalStore) -> None:
    """A crash after cache mutation must not lose the source-redaction barrier."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$event")
    await store.record_turn(
        TurnRecord.create(
            ["$event", "$sibling"],
            source_event_prompts={"$event": "REDACTED_SECRET", "$sibling": "keep"},
            requester_id="@user:example.org",
            response_owner="agent",
            history_scope=HistoryScope(kind="agent", scope_id="agent"),
            conversation_target=target,
        ),
    )

    await store.mark_source_redacted("$event")
    _reset_handled_turn_ledger_runtime()
    reloaded_store = await _store(journal_store)

    reloaded_record = reloaded_store.get_turn_record("$event")
    assert reloaded_record is not None
    assert reloaded_record.redacted_source_event_ids == ("$event",)
    assert reloaded_record.source_event_prompts == {"$sibling": "keep"}
    assert reloaded_store.is_handled("$event") is True
    assert reloaded_store.is_handled("$sibling") is True


@pytest.mark.asyncio
async def test_warm_preserves_lazy_cleanup_until_next_response(journal_store: EventJournalStore) -> None:
    """A restart must retain replay cleanup for the conversation's next response."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$user_msg"})],
    )
    storage = _FakeAgentStorage(session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))
    marked = await store.mark_source_redacted("$user_msg")

    assert marked is not None
    assert marked.pending_redaction_cleanup_event_ids == ("$user_msg",)
    _reset_handled_turn_ledger_runtime()
    restarted_store = await _store_with_storage(journal_store, storage)

    await restarted_store.warm()

    assert len(session.runs or []) == 1
    restarted_record = restarted_store.get_turn_record("$user_msg")
    assert restarted_record is not None
    assert restarted_record.redacted_source_event_ids == ("$user_msg",)
    assert restarted_record.pending_redaction_cleanup_event_ids == ("$user_msg",)
    assert (
        await restarted_store._prepare_response_for_redactions(
            target=target,
            source_event_ids=("$later",),
        )
        is False
    )
    assert session.runs == []
    cleaned_record = restarted_store.get_turn_record("$user_msg")
    assert cleaned_record is not None
    assert cleaned_record.pending_redaction_cleanup_event_ids == ()


@pytest.mark.asyncio
async def test_locked_response_preparation_sanitizes_and_acknowledges_history_cleanup(
    journal_store: EventJournalStore,
) -> None:
    """The under-lock gate removes replay and acknowledges the completed work."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = AgentSession(
        session_id=target.session_id,
        agent_id="agent",
        runs=[RunOutput(session_id=target.session_id, metadata={"matrix_event_id": "$user_msg"})],
    )
    storage = _FakeAgentStorage(session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))
    await store.mark_source_redacted("$user_msg")

    should_suppress = await store._prepare_response_for_redactions(
        target=target,
        source_event_ids=("$later",),
    )

    assert should_suppress is False
    assert session.runs == []
    record = store.get_turn_record("$user_msg")
    assert record is not None
    assert record.pending_redaction_cleanup_event_ids == ()


def test_turn_record_codec_projects_and_parses_one_versioned_run_schema() -> None:
    """The same codec should own both run projection and recovery parsing."""
    history_scope = HistoryScope(kind="agent", scope_id="agent")
    target = MessageTarget.resolve("!room:example.org", "$thread", "$anchor")
    turn_record = TurnRecord.create(
        ["$first", "$anchor"],
        discovery_event_ids=["$selection"],
        redacted_source_event_ids=["$first"],
        response_event_id="$response",
        source_event_prompts={"$first": "first", "$anchor": "anchor"},
        source_event_metadata={
            "$first": SourceEventMetadata(sender="@alice:example.org", timestamp_ms=1_774_019_700_000),
        },
        response_owner="agent",
        requester_id="@user:example.org",
        correlation_id="corr-1",
        history_scope=history_scope,
        conversation_target=target,
    )

    metadata = TurnRecordCodec.to_run_metadata(turn_record)
    metadata.update(
        {
            constants.MATRIX_EVENT_ID_METADATA_KEY: "$anchor",
            constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: "$response",
            "requester_id": "@user:example.org",
            "correlation_id": "corr-1",
        },
    )
    parsed = TurnRecordCodec.from_run_metadata(metadata)

    assert metadata[constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY] == TurnRecordCodec.schema_version()
    assert metadata[constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY] == ["$selection"]
    assert metadata[constants.MATRIX_TURN_REDACTED_SOURCE_EVENT_IDS_METADATA_KEY] == ["$first"]
    assert parsed == turn_record


def test_turn_record_codec_preserves_physical_source_ownership_when_alias_id_collides() -> None:
    """A physical source must outrank another source's discovery alias with the same ID."""
    relay_event_id = "$relay"
    human_event_id = "$human"
    turn_record = TurnRecord.create(
        [relay_event_id, human_event_id],
        source_event_prompts={
            relay_event_id: "routed prompt",
            human_event_id: "physical prompt",
        },
        source_event_metadata={
            relay_event_id: SourceEventMetadata(
                sender="@bob:example.org",
                discovery_event_id=human_event_id,
            ),
            human_event_id: SourceEventMetadata(sender="@alice:example.org"),
        },
        requester_id="@bob:example.org",
    )

    run_metadata = TurnRecordCodec.to_run_metadata(turn_record)
    run_metadata[constants.MATRIX_EVENT_ID_METADATA_KEY] = human_event_id
    recovered = TurnRecordCodec.from_run_metadata(run_metadata)

    assert recovered is not None
    assert recovered.prompt_source_event_id(human_event_id) == human_event_id
    assert recovered.requester_id_for_source(human_event_id) == "@alice:example.org"
    assert recovered.requester_id_for_source(relay_event_id) == "@bob:example.org"


def test_physical_source_membership_outranks_alias_when_metadata_is_partial() -> None:
    """A missing physical metadata row must fail closed instead of resolving through a relay alias."""
    relay_event_id = "$relay"
    human_event_id = "$human"
    turn_record = TurnRecord.create(
        [relay_event_id, human_event_id],
        source_event_metadata={
            relay_event_id: SourceEventMetadata(
                sender="@bob:example.org",
                discovery_event_id=human_event_id,
            ),
        },
        requester_id="@bob:example.org",
    )

    assert turn_record.prompt_source_event_id(human_event_id) == human_event_id
    assert turn_record.requester_id_for_source(human_event_id) is None


def test_redacted_physical_source_does_not_tombstone_colliding_relay_alias() -> None:
    """Redacting a physical source must retain the sibling relay and its prompt."""
    relay_event_id = "$relay"
    human_event_id = "$human"
    turn_record = TurnRecord.create(
        [relay_event_id, human_event_id],
        redacted_source_event_ids=[human_event_id],
        source_event_prompts={
            relay_event_id: "routed prompt",
            human_event_id: "physical prompt",
        },
        source_event_metadata={
            relay_event_id: SourceEventMetadata(
                sender="@bob:example.org",
                discovery_event_id=human_event_id,
            ),
            human_event_id: SourceEventMetadata(sender="@alice:example.org"),
        },
        requester_id="@bob:example.org",
    )

    assert turn_record.prompt_source_event_id(human_event_id) == human_event_id
    assert turn_record.replay_source_event_ids == (relay_event_id,)
    assert turn_record.source_event_prompts == {relay_event_id: "routed prompt"}


def test_turn_record_codecs_preserve_explicit_unknown_source_ownership() -> None:
    """An explicit empty source map must survive persistence and disable singleton fallback."""
    event_id = "$source"
    turn_record = TurnRecord.create(
        [event_id],
        source_event_metadata={},
        requester_id="@stale:example.org",
    )

    ledger_recovered = TurnRecordCodec._from_ledger_record(
        event_id,
        TurnRecordCodec._to_ledger_record(turn_record),
    )
    run_metadata = TurnRecordCodec.to_run_metadata(turn_record)
    run_metadata[constants.MATRIX_EVENT_ID_METADATA_KEY] = event_id
    run_recovered = TurnRecordCodec.from_run_metadata(run_metadata)

    assert turn_record.source_event_metadata == {}
    assert ledger_recovered is not None
    assert ledger_recovered.source_event_metadata == {}
    assert run_recovered is not None
    assert run_recovered.source_event_metadata == {}
    assert run_recovered.requester_id_for_source(event_id) is None


@pytest.mark.asyncio
async def test_build_run_metadata_normalizes_discovery_aliases(journal_store: EventJournalStore) -> None:
    """Additional discovery IDs should share canonical source-ID normalization."""
    store = await _store(journal_store)
    turn_record = TurnRecord.create(["$first", "$anchor"])

    metadata = store.build_run_metadata(
        turn_record,
        additional_discovery_event_ids=("", "$first", "$selection", "$selection"),
    )

    assert metadata == {
        constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY: TurnRecordCodec.schema_version(),
        constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$anchor"],
        constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY: ["$selection"],
    }


@pytest.mark.asyncio
async def test_discovery_alias_recovery_repairs_anchor_and_alias_rows(journal_store: EventJournalStore) -> None:
    """Missing-ledger recovery should index one non-coalesced turn by its anchor and discovery alias."""
    metadata = TurnRecordCodec.to_run_metadata(
        TurnRecord.create(
            ["$question"],
            response_owner="agent",
        ),
    )
    metadata[constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY] = ["$selection"]
    metadata[constants.MATRIX_EVENT_ID_METADATA_KEY] = "$question"
    metadata[constants.MATRIX_RESPONSE_EVENT_ID_METADATA_KEY] = "$response"
    recovery_record = TurnRecordCodec.from_run_metadata(metadata)

    assert recovery_record is not None
    assert recovery_record.source_event_ids == ("$question",)
    assert recovery_record.discovery_event_ids == ("$selection",)
    assert not recovery_record.is_coalesced

    for lookup_event_id in ("$question", "$selection"):
        store = await _store(journal_store, agent_name=f"agent_{lookup_event_id.removeprefix('$')}")
        loaded = await _load_with_recovery(
            store,
            original_event_id=lookup_event_id,
            recovery_record=recovery_record,
        )

        assert loaded is not None
        assert loaded.source_event_ids == ("$question",)
        assert loaded.discovery_event_ids == ("$selection",)
        for indexed_event_id in ("$question", "$selection"):
            repaired = store.get_turn_record(indexed_event_id)
            assert repaired is not None
            assert repaired.source_event_ids == ("$question",)
            assert repaired.discovery_event_ids == ("$selection",)
            assert store.is_handled(indexed_event_id)


@pytest.mark.asyncio
async def test_recovery_does_not_replace_a_conflicting_completed_identity(journal_store: EventJournalStore) -> None:
    """Repair missing aliases without overwriting another completed source turn."""
    store = await _store(journal_store)
    await store.record_turn(TurnRecord.create(["$selection"], response_event_id="$selection-response"))
    recovery_record = TurnRecord.create(
        ["$question"],
        discovery_event_ids=["$selection"],
        response_event_id="$question-response",
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$question",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.source_event_ids == ("$question",)
    assert loaded.discovery_event_ids == ()
    assert loaded.indexed_event_ids == ("$question",)
    assert store.get_turn_record("$question") == loaded
    selection_record = store.get_turn_record("$selection")
    assert selection_record is not None
    assert selection_record.source_event_ids == ("$selection",)
    assert selection_record.response_event_id == "$selection-response"

    _reset_handled_turn_ledger_runtime()
    reloaded_store = await _store(journal_store)
    assert reloaded_store.get_turn_record("$question") == loaded
    assert reloaded_store.get_turn_record("$selection") == selection_record


@pytest.mark.asyncio
async def test_newer_delivered_run_recovers_mutable_facts_after_crash(journal_store: EventJournalStore) -> None:
    """A delivered run newer than the ledger should repair the edit crash window."""
    store = await _store(journal_store)
    ledger_record = TurnRecord.create(
        ["$first", "$anchor"],
        response_event_id="$old-response",
        source_event_prompts={"$first": "old first", "$anchor": "old anchor"},
        source_event_revisions={
            "$first": (10, "$old-edit"),
        },
        visible_echo_event_id="$echo",
        timestamp=10,
    )
    await store._ledger.record_handled_turn(ledger_record)
    recovery_record = TurnRecord.create(
        ["$first", "$anchor"],
        response_event_id="$new-response",
        source_event_prompts={"$first": "edited first", "$anchor": "old anchor"},
        source_event_revisions={
            "$first": (20, "$new-edit"),
        },
        response_owner="agent",
        timestamp=20,
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$first",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.source_event_ids == ledger_record.source_event_ids
    assert loaded.anchor_event_id == ledger_record.anchor_event_id
    assert loaded.response_event_id == "$new-response"
    assert loaded.source_event_prompts == {"$first": "edited first", "$anchor": "old anchor"}
    assert loaded.source_event_revisions == {
        "$first": (20, "$new-edit"),
    }
    assert loaded.visible_echo_event_id == "$echo"
    assert loaded.response_owner == "agent"
    assert loaded.timestamp == 20


@pytest.mark.asyncio
async def test_recovery_preserves_newer_ledger_only_sibling_edit(journal_store: EventJournalStore) -> None:
    """Recovery must merge edit facts per source instead of replacing the whole map."""
    store = await _store(journal_store)
    ledger_record = TurnRecord.create(
        ["$first", "$anchor"],
        response_event_id="$old-response",
        source_event_prompts={"$first": "old first", "$anchor": "suppressed anchor"},
        source_event_revisions={"$anchor": (30, "$anchor-edit")},
        timestamp=10,
    )
    await store._ledger.record_handled_turn(ledger_record)
    recovery_record = TurnRecord.create(
        ["$first", "$anchor"],
        response_event_id="$new-response",
        source_event_prompts={"$first": "edited first", "$anchor": "old anchor"},
        source_event_revisions={"$first": (20, "$first-edit")},
        timestamp=20,
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$first",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.source_event_prompts == {
        "$first": "edited first",
        "$anchor": "suppressed anchor",
    }
    assert loaded.source_event_revisions == {
        "$first": (20, "$first-edit"),
        "$anchor": (30, "$anchor-edit"),
    }
    assert loaded.response_event_id == "$new-response"


@pytest.mark.asyncio
async def test_recovery_preserves_newer_routed_alias_prompt(journal_store: EventJournalStore) -> None:
    """A newer human-alias revision must carry its owned relay prompt through recovery."""
    store = await _store(journal_store)
    source_metadata = {
        "$relay": SourceEventMetadata(sender="@user:example.org", discovery_event_id="$human"),
        "$anchor": SourceEventMetadata(sender="@user:example.org"),
    }
    await store._ledger.record_handled_turn(
        TurnRecord.create(
            ["$relay", "$anchor"],
            discovery_event_ids=["$human"],
            response_event_id="$old-response",
            source_event_prompts={"$relay": "new relay", "$anchor": "anchor"},
            source_event_revisions={"$human": (30, "$new-edit")},
            source_event_metadata=source_metadata,
            timestamp=10,
        ),
    )
    recovery_record = TurnRecord.create(
        ["$relay", "$anchor"],
        discovery_event_ids=["$human"],
        response_event_id="$new-response",
        source_event_prompts={"$relay": "stale relay", "$anchor": "anchor"},
        source_event_revisions={"$human": (20, "$stale-edit")},
        source_event_metadata=source_metadata,
        timestamp=20,
    )

    loaded = await _load_with_recovery(store, original_event_id="$human", recovery_record=recovery_record)

    assert loaded is not None
    assert loaded.source_event_prompts == {"$relay": "new relay", "$anchor": "anchor"}
    assert loaded.source_event_revisions == {"$human": (30, "$new-edit")}


@pytest.mark.asyncio
async def test_recovery_without_prompts_preserves_durable_prompt_map(journal_store: EventJournalStore) -> None:
    """A delivered recovery lacking prompt metadata cannot erase durable coalesced bodies."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(
            ["$first", "$anchor"],
            response_event_id="$old-response",
            source_event_prompts={"$first": "first", "$anchor": "anchor"},
            timestamp=10,
        ),
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$first",
        recovery_record=TurnRecord.create(
            ["$first", "$anchor"],
            response_event_id="$new-response",
            timestamp=20,
        ),
    )

    assert loaded is not None
    assert loaded.source_event_prompts == {"$first": "first", "$anchor": "anchor"}


@pytest.mark.asyncio
async def test_recovery_preserves_explicit_unknown_source_ownership(journal_store: EventJournalStore) -> None:
    """A newer explicit unknown-ownership marker must not inherit stale ledger attribution."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(
            ["$event"],
            response_event_id="$old-response",
            source_event_metadata={
                "$event": SourceEventMetadata(sender="@stale:example.org"),
            },
            timestamp=10,
        ),
    )
    recovery_record = TurnRecord.create(
        ["$event"],
        response_event_id="$new-response",
        source_event_metadata={},
        requester_id="@current:example.org",
        timestamp=20,
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.response_event_id == "$new-response"
    assert loaded.source_event_metadata == {}
    assert loaded.requester_id_for_source("$event") is None


@pytest.mark.asyncio
async def test_routed_alias_redaction_marks_owning_relay_under_lock(journal_store: EventJournalStore) -> None:
    """Under-lock redaction checks must recognize a physical relay tombstoned by its alias."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(
            ["$relay", "$anchor"],
            discovery_event_ids=["$human"],
            response_event_id="$response",
            source_event_prompts={"$relay": "secret", "$anchor": "keep"},
            source_event_metadata={
                "$relay": SourceEventMetadata(sender="@user:example.org", discovery_event_id="$human"),
                "$anchor": SourceEventMetadata(sender="@user:example.org"),
            },
        ),
    )

    marked = await store.mark_source_redacted("$human")

    assert marked is not None
    assert marked.source_event_prompts == {"$anchor": "keep"}
    assert store._any_source_redacted(("$relay",)) is True


@pytest.mark.asyncio
async def test_same_second_delivered_run_repairs_fractional_ledger_timestamp(journal_store: EventJournalStore) -> None:
    """Second-resolution run times should still repair a later run from the same second."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(["$event"], response_event_id="$old-response", timestamp=10.9),
    )
    recovery_record = TurnRecord.create(["$event"], response_event_id="$new-response", timestamp=10)

    loaded = await _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.response_event_id == "$new-response"
    assert loaded.timestamp > 10.9


@pytest.mark.asyncio
async def test_repeated_delivered_run_recovery_keeps_ledger_version_stable(journal_store: EventJournalStore) -> None:
    """Idempotent recovery should not rewrite the ledger with synthetic timestamp drift."""
    store = await _store(journal_store)
    ledger_record = TurnRecord.create(
        ["$event"],
        response_event_id="$response",
        response_owner="agent",
        timestamp=10,
    )
    await store._ledger.record_handled_turn(ledger_record)
    recovery_record = TurnRecord.create(
        ["$event"],
        response_event_id="$response",
        response_owner="agent",
        timestamp=20,
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded == ledger_record
    assert store.get_turn_record("$event") == ledger_record


@pytest.mark.asyncio
async def test_newer_interrupted_run_keeps_delivered_ledger_outcome(journal_store: EventJournalStore) -> None:
    """A newer run without Matrix delivery must not replace a visible response."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(["$event"], response_event_id="$response", timestamp=10),
    )
    recovery_record = TurnRecord.create(["$event"], completed=False, timestamp=20)

    loaded = await _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.response_event_id == "$response"
    assert loaded.completed
    assert loaded.timestamp == 10


@pytest.mark.asyncio
async def test_interrupted_recovery_does_not_mix_prompt_and_revision(journal_store: EventJournalStore) -> None:
    """An unfinished run cannot pair its edit revision with the delivered ledger prompt."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(
            ["$event"],
            response_event_id="$response",
            source_event_prompts={"$event": "base prompt"},
            timestamp=10,
        ),
    )
    recovery_record = TurnRecord.create(
        ["$event"],
        completed=False,
        source_event_prompts={"$event": "edited prompt"},
        source_event_revisions={"$event": (20, "$edit")},
        timestamp=20,
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.source_event_prompts == {"$event": "base prompt"}
    assert loaded.source_event_revisions is None
    assert loaded.response_event_id == "$response"


@pytest.mark.asyncio
async def test_recovery_does_not_adopt_revision_without_its_prompt(journal_store: EventJournalStore) -> None:
    """A ledger edit revision is unusable unless its matching durable prompt survived."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(
            ["$event"],
            response_event_id="$old-response",
            source_event_revisions={"$event": (20, "$new-edit")},
            timestamp=10,
        ),
    )
    recovery_record = TurnRecord.create(
        ["$event"],
        response_event_id="$new-response",
        source_event_prompts={"$event": "old prompt"},
        source_event_revisions={"$event": (10, "$old-edit")},
        timestamp=20,
    )

    loaded = await _load_with_recovery(store, original_event_id="$event", recovery_record=recovery_record)

    assert loaded is not None
    assert loaded.source_event_prompts == {"$event": "old prompt"}
    assert loaded.source_event_revisions == {"$event": (10, "$old-edit")}


@pytest.mark.asyncio
async def test_terminal_write_refreshes_ledger_precedence_timestamp(journal_store: EventJournalStore) -> None:
    """A successful terminal write should become newer than its recovered input."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(["$event"], response_event_id="$old-response", timestamp=1),
    )

    await store.record_turn(TurnRecord.create(["$event"], response_event_id="$new-response", timestamp=1))

    updated = store.get_turn_record("$event")
    assert updated is not None
    assert updated.response_event_id == "$new-response"
    assert updated.timestamp > 1


@pytest.mark.asyncio
async def test_terminal_turn_can_replace_a_provisional_source_identity(journal_store: EventJournalStore) -> None:
    """A partial visible echo may join the canonical coalesced turn that completes it."""
    store = await _store(journal_store)
    await store.record_visible_echo("$second", "$echo")

    await store.record_turn(TurnRecord.create(["$first", "$second"], response_event_id="$response"))

    first_record = store.get_turn_record("$first")
    second_record = store.get_turn_record("$second")
    assert first_record is not None
    assert first_record == second_record
    assert first_record.source_event_ids == ("$first", "$second")
    assert first_record.visible_echo_event_id == "$echo"


@pytest.mark.asyncio
async def test_visible_echo_is_finalized_only_after_replacement_acknowledgement(
    journal_store: EventJournalStore,
) -> None:
    """A posted placeholder should not become a terminal router outcome before its edit succeeds."""
    store = await _store(journal_store)
    await store.record_visible_echo("$event", "$echo")

    assert store.finalized_visible_echo_for_sources(("$event",)) is None

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=False)

    record = store.get_turn_record("$event")
    assert record is not None
    assert not record.completed
    assert record.response_event_id == "$echo"
    assert store.finalized_visible_echo_for_sources(("$event",)) == "$echo"
    finalized = store.finalized_visible_echo("$event")
    assert finalized is not None
    assert finalized.event_id == "$echo"
    assert finalized.is_fallback is False


@pytest.mark.asyncio
async def test_visible_echo_finalization_requires_tracked_placeholder(journal_store: EventJournalStore) -> None:
    """An acknowledgement alone must not create a finalized visible echo."""
    store = await _store(journal_store)

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=False)

    assert store.get_turn_record("$event") is None


@pytest.mark.asyncio
async def test_visible_echo_finalization_cannot_overwrite_terminal_outcome(journal_store: EventJournalStore) -> None:
    """A late edit acknowledgement should preserve a concurrently completed turn."""
    store = await _store(journal_store)
    await store.record_visible_echo("$event", "$echo")
    await store.record_turn(TurnRecord.create(["$event"], response_event_id="$response"))

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=False)

    record = store.get_turn_record("$event")
    assert record is not None
    assert record.completed
    assert record.response_event_id == "$response"
    finalized = store.finalized_visible_echo("$event")
    assert finalized is not None
    assert finalized.event_id == "$echo"
    assert finalized.is_fallback is False


@pytest.mark.asyncio
async def test_finalized_visible_echo_keeps_transcript_over_fallback(journal_store: EventJournalStore) -> None:
    """Transcript replacement should upgrade fallback and reject later downgrade."""
    store = await _store(journal_store)
    await store.record_visible_echo("$event", "$echo")

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=True)
    fallback = store.finalized_visible_echo("$event")
    assert fallback is not None
    assert fallback.is_fallback is True

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=False)
    transcript = store.finalized_visible_echo("$event")
    assert transcript is not None
    assert transcript.is_fallback is False

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=True)
    assert store.finalized_visible_echo("$event") == transcript

    _reset_handled_turn_ledger_runtime()
    assert (await _store(journal_store)).finalized_visible_echo("$event") == transcript


@pytest.mark.asyncio
async def test_terminal_turn_rejects_conflicting_completed_canonical_source(journal_store: EventJournalStore) -> None:
    """A completed source cannot be reassigned into a different canonical turn."""
    store = await _store(journal_store)
    await store.record_turn(TurnRecord.create(["$first"], response_event_id="$first-response"))

    await store.record_turn(TurnRecord.create(["$first", "$second"], response_event_id="$other-response"))

    first_record = store.get_turn_record("$first")
    assert first_record is not None
    assert first_record.source_event_ids == ("$first",)
    assert first_record.response_event_id == "$first-response"
    assert store.get_turn_record("$second") is None


def test_run_metadata_without_current_schema_version_is_not_recovery_data() -> None:
    """Stale pre-user run metadata should not create an implicit migration path."""
    assert (
        TurnRecordCodec.from_run_metadata(
            {
                constants.MATRIX_EVENT_ID_METADATA_KEY: "$event",
                constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$event"],
            },
        )
        is None
    )


def test_run_metadata_with_empty_normalized_sources_falls_back_to_anchor() -> None:
    """Current metadata should never decode into an eventless canonical record."""
    parsed = TurnRecordCodec.from_run_metadata(
        {
            constants.MATRIX_TURN_SCHEMA_VERSION_METADATA_KEY: TurnRecordCodec.schema_version(),
            constants.MATRIX_EVENT_ID_METADATA_KEY: "$anchor",
            constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["", None, 42],
        },
    )

    assert parsed is not None
    assert parsed.anchor_event_id == "$anchor"
    assert parsed.source_event_ids == ("$anchor",)


@pytest.mark.asyncio
async def test_undelivered_run_repairs_as_incomplete_and_remains_retryable(journal_store: EventJournalStore) -> None:
    """A persisted run without Matrix response linkage must not become a handled turn."""
    store = await _store(journal_store)
    metadata = TurnRecordCodec.to_run_metadata(
        TurnRecord.create(["$event"], response_owner="agent"),
    )
    metadata[constants.MATRIX_EVENT_ID_METADATA_KEY] = "$event"
    recovery_record = TurnRecordCodec.from_run_metadata(metadata)

    assert recovery_record is not None
    loaded = await _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert not loaded.completed
    repaired = store.get_turn_record("$event")
    assert repaired is not None
    assert not repaired.completed
    assert repaired.response_owner == "agent"
    assert not store.is_handled("$event")


@pytest.mark.asyncio
async def test_load_turn_uses_ledger_identity_and_outcome_then_backfills_missing_context(
    journal_store: EventJournalStore,
) -> None:
    """Ledger facts should win field-by-field while absent optional context comes from run metadata."""
    store = await _store(journal_store)
    ledger_record = TurnRecord.create(
        ["$first", "$anchor"],
        response_event_id="$ledger-response",
        source_event_prompts={"$first": "ledger first", "$anchor": "ledger anchor"},
        requester_id="@ledger-user:example.org",
    )
    await store.record_turn(ledger_record)
    persisted_ledger_record = store.get_turn_record("$first")
    assert persisted_ledger_record is not None
    recovery_target = MessageTarget.resolve("!room:example.org", None, "$anchor")
    recovery_record = TurnRecord.create(
        ["$run-only", "$anchor"],
        response_event_id="$run-response",
        source_event_prompts={"$run-only": "run", "$anchor": "run anchor"},
        response_owner="agent",
        requester_id="@run-user:example.org",
        history_scope=HistoryScope(kind="agent", scope_id="agent"),
        conversation_target=recovery_target,
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$first",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.source_event_ids == ("$first", "$anchor")
    assert loaded.anchor_event_id == "$anchor"
    assert loaded.response_event_id == "$ledger-response"
    assert loaded.source_event_prompts == {"$first": "ledger first", "$anchor": "ledger anchor"}
    assert loaded.requester_id == "@ledger-user:example.org"
    assert loaded.response_owner == "agent"
    assert loaded.history_scope == HistoryScope(kind="agent", scope_id="agent")
    assert loaded.conversation_target == recovery_target
    assert loaded.timestamp > persisted_ledger_record.timestamp
    repaired = store.get_turn_record("$first")
    assert repaired == loaded


@pytest.mark.asyncio
async def test_load_turn_repairs_missing_ledger_row_from_run_metadata(journal_store: EventJournalStore) -> None:
    """Run metadata should recover and immediately backfill an absent ledger row."""
    store = await _store(journal_store)
    recovery_record = TurnRecord.create(
        ["$event"],
        response_event_id="$response",
        response_owner="agent",
    )

    loaded = await _load_with_recovery(
        store,
        original_event_id="$event",
        recovery_record=recovery_record,
    )

    assert loaded is not None
    assert loaded.timestamp > 0
    assert replace(loaded, timestamp=0.0) == recovery_record
    repaired = store.get_turn_record("$event")
    assert repaired is not None
    assert repaired.response_event_id == "$response"
    assert repaired.response_owner == "agent"


@pytest.mark.asyncio
async def test_record_turn_preserves_existing_optional_facts_at_the_owner_boundary(
    journal_store: EventJournalStore,
) -> None:
    """TurnStore, rather than the physical ledger, should merge repeated writes."""
    store = await _store(journal_store)
    await store.record_turn(
        TurnRecord.create(
            ["$event"],
            response_event_id="$first-response",
            requester_id="@user:example.org",
            correlation_id="corr-1",
        ),
    )

    await store.record_turn(TurnRecord(source_event_ids=("$event",), response_event_id="$second-response"))

    record = store.get_turn_record("$event")
    assert record is not None
    assert record.response_event_id == "$second-response"
    assert record.requester_id == "@user:example.org"
    assert record.correlation_id == "corr-1"


@pytest.mark.asyncio
async def test_visible_echo_cannot_overwrite_concurrent_terminal_outcome(journal_store: EventJournalStore) -> None:
    """A visible-echo write suspended mid-flight must not undo a terminal write behind it.

    The database round trip is the one suspension point inside a ledger update,
    so without serialization a terminal write could overtake a stalled echo
    write in memory and then be durably overwritten by it -- memory saying the
    turn is finished while storage says it is not, which a restart resolves the
    wrong way. The terminal update therefore waits for the echo to finish
    owing its row, and storage ends up ordered the way memory is.
    """
    store = await _store(journal_store)
    terminal_record = TurnRecord.create(["$event"], response_event_id="$response")
    echo_write_reached_storage = asyncio.Event()
    release_echo_write = asyncio.Event()
    real_upsert = TurnRecordStore.upsert

    async def gate_first_write(records: TurnRecordStore, **kwargs: object) -> None:
        if not echo_write_reached_storage.is_set():
            echo_write_reached_storage.set()
            await release_echo_write.wait()
        await real_upsert(records, **kwargs)

    with patch.object(TurnRecordStore, "upsert", gate_first_write):
        echo_task = asyncio.create_task(store.record_visible_echo("$event", "$echo"))
        await echo_write_reached_storage.wait()

        terminal_task = asyncio.create_task(store.record_turn(terminal_record))
        await asyncio.sleep(0)
        assert not terminal_task.done(), "the terminal update must wait for the echo's row"

        release_echo_write.set()
        await asyncio.gather(echo_task, terminal_task)

    record = store.get_turn_record("$event")
    assert record is not None
    assert record.completed
    assert record.response_event_id == "$response"
    assert record.visible_echo_event_id == "$echo"

    _reset_handled_turn_ledger_runtime()
    restarted = await _store(journal_store)
    durable = restarted.get_turn_record("$event")
    assert durable is not None
    assert durable.completed, "the terminal outcome must survive the echo's late write"
    assert durable.response_event_id == "$response"
    assert durable.visible_echo_event_id == "$echo"


@pytest.mark.asyncio
async def test_the_record_a_final_acknowledgement_commits_binds_its_response(
    journal_store: EventJournalStore,
) -> None:
    """A delivered answer's event ID reaches the record in the acknowledgement's own commit.

    Until the record names the response event, an edit of that message has
    nothing to edit and is dropped. A startup pass used to rejoin the two
    afterwards; this closes the window instead of repairing it, so what the
    acknowledgement carries has to be the bound, completed record.
    """
    store = await _store(journal_store)
    await store.record_pending_turn(
        TurnRecord.create(["$source"], completed=False, response_owner="agent"),
    )

    bound = store.terminal_turn_record("$source", "$answer")

    assert bound is not None
    assert bound.response_event_id == "$answer"
    assert bound.completed is True
    assert bound.source_event_ids == ("$source",)


@pytest.mark.asyncio
async def test_a_record_that_already_names_an_answer_is_left_alone(
    journal_store: EventJournalStore,
) -> None:
    """The first answer ever sent must not overwrite a later, better one.

    Recovery can acknowledge a frozen row long after the turn moved on, and
    the event that row names may be older than the one the record already
    holds. Binding it anyway would replace the current answer with a stale one.
    """
    store = await _store(journal_store)
    await store.record_turn(TurnRecord.create(["$source"], response_event_id="$better"))

    assert store.terminal_turn_record("$source", "$first-ever-sent") is None


@pytest.mark.asyncio
async def test_an_unknown_turn_has_no_record_to_commit(journal_store: EventJournalStore) -> None:
    """An acknowledgement for a turn this ledger never recorded carries nothing."""
    store = await _store(journal_store)

    assert store.terminal_turn_record("$never-seen", "$answer") is None


@pytest.mark.asyncio
async def test_preparing_the_record_does_not_publish_it(journal_store: EventJournalStore) -> None:
    """Reading the record must not mark the turn answered before the commit lands.

    This runs *before* the transaction, and the transaction can lose the
    acknowledgement race -- first writer wins. Publishing here would leave the
    ledger calling a turn finished on the strength of a write that never
    happened, and the answer that did land named by nobody.
    """
    store = await _store(journal_store)
    await store.record_pending_turn(
        TurnRecord.create(["$source"], completed=False, response_owner="agent"),
    )

    assert store.terminal_turn_record("$source", "$answer") is not None

    still_pending = store.get_turn_record("$source")
    assert still_pending is not None
    assert still_pending.completed is False
    assert still_pending.response_event_id is None
    assert store.is_handled("$source") is False


@pytest.mark.asyncio
async def test_record_responded_turn_rejects_empty_response_event_id(journal_store: EventJournalStore) -> None:
    """The durable response boundary should reject a noncanonical empty event ID."""
    store = await _store(journal_store)
    noncanonical_record = TurnRecord(source_event_ids=("$source",), response_event_id="")

    with pytest.raises(RuntimeError, match="requires a visible Matrix response event ID"):
        await store.record_responded_turn(noncanonical_record)


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery_response_event_id", [None, "$stale-response"])
async def test_recovery_cannot_overwrite_concurrent_terminal_outcome(
    journal_store: EventJournalStore,
    recovery_response_event_id: str | None,
) -> None:
    """Slow incomplete or delivered recovery must preserve a concurrent terminal write."""
    store = await _store(journal_store)
    await store._ledger.record_handled_turn(
        TurnRecord.create(["$event"], response_event_id="$old-response", timestamp=9),
    )
    recovery_record = TurnRecord.create(
        ["$event"],
        response_event_id=recovery_response_event_id,
        completed=recovery_response_event_id is not None,
        response_owner="agent",
        timestamp=10,
    )
    real_update = store._ledger.update_handled_turn
    terminal_recorded = False

    async def record_terminal_before_repair(*args: object, **kwargs: object) -> TurnRecord | None:
        # The recovery read is over by the time this runs, so landing the
        # terminal write here reproduces exactly what the repair guards
        # against: the ledger moved on while the run metadata was loading.
        nonlocal terminal_recorded
        if not terminal_recorded:
            terminal_recorded = True
            with patch("mindroom.handled_turns.time.time", return_value=10.9):
                await store.record_turn(TurnRecord.create(["$event"], response_event_id="$response"))
        return await real_update(*args, **kwargs)

    with (
        patch.object(store, "_load_persisted_turn_record", return_value=recovery_record),
        patch.object(store._ledger, "update_handled_turn", side_effect=record_terminal_before_repair),
    ):
        loaded = await store.load_turn(
            room=MagicMock(room_id="!room:example.org"),
            thread_id=None,
            original_event_id="$event",
            requester_user_id="@user:example.org",
        )

    assert loaded is not None
    assert loaded.completed
    assert loaded.response_event_id == "$response"
    assert loaded.response_owner == "agent"
    assert loaded.timestamp > 10.9
    record = store.get_turn_record("$event")
    assert record == loaded


def test_only_turn_store_imports_handled_turn_ledger_in_production() -> None:
    """HandledTurnLedger imports should stay isolated to TurnStore in production code."""
    src_root = Path(__file__).resolve().parents[1] / "src" / "mindroom"
    offenders: list[str] = []

    for path in src_root.rglob("*.py"):
        if path.name in {"turn_store.py", "handled_turns.py"}:
            continue
        module = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(module):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "mindroom.handled_turns":
                continue
            if any(alias.name == "HandledTurnLedger" for alias in node.names):
                offenders.append(path.relative_to(src_root).as_posix())
                break

    assert offenders == []


def test_agent_bot_does_not_expose_removed_handled_turn_ledger_shim(tmp_path: Path) -> None:
    """AgentBot instances should route handled-turn state only through TurnStore."""
    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="agent",
            user_id="@mindroom_agent:localhost",
            display_name="Agent",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )

    removed_attr = "_handled" + "_turn_ledger"
    assert removed_attr not in AgentBot.__dict__
    assert not hasattr(bot, removed_attr)
    assert removed_attr not in vars(bot)


@pytest.mark.asyncio
async def test_router_turn_replay_uses_persisted_ledger_across_two_restarts(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """Router relay turns have durable ledger state but no Agno run storage."""

    async def router_store() -> TurnStore:
        config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
        store = TurnStore(
            TurnStoreDeps(
                agent_name="router",
                turn_records=journal_store.turn_records("router"),
                legacy_responses_file=None,
                state_writer=ConversationStateWriter(
                    ConversationStateWriterDeps(
                        runtime=SimpleNamespace(config=config),
                        logger=MagicMock(),
                        runtime_paths=runtime_paths_for(config),
                        agent_name="router",
                    ),
                ),
                resolver=MagicMock(),
                tool_runtime=MagicMock(),
            ),
        )
        await store.warm()
        return store

    target = MessageTarget.resolve("!room:localhost", "$thread", "$source")
    expected = TurnRecord.create(
        ["$source"],
        response_event_id="$relay",
        response_owner="router",
        requester_id="@user:localhost",
        conversation_target=target,
    )
    await (await router_store()).record_turn(expected)

    for _restart in range(2):
        _reset_handled_turn_ledger_runtime()
        restarted = await router_store()
        with patch.object(
            restarted,
            "_load_persisted_turn_record",
            side_effect=AssertionError("ledger-only entity attempted run recovery"),
        ):
            loaded = await restarted.load_turn(
                room=MagicMock(room_id="!room:localhost"),
                thread_id="$thread",
                original_event_id="$source",
                requester_user_id="@user:localhost",
            )

        assert loaded is not None
        assert replace(loaded, timestamp=0.0) == replace(expected, timestamp=0.0)
        assert loaded.timestamp > 0


def test_no_test_references_removed_bot_handled_turn_ledger_shim() -> None:
    """Tests should route all handled-turn access through TurnStore."""
    tests_root = Path(__file__).resolve().parent
    needle = "._handled" + "_turn_ledger"
    offenders = [
        path.relative_to(tests_root).as_posix() for path in tests_root.rglob("*.py") if needle in path.read_text()
    ]

    assert offenders == []
