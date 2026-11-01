"""Tests for canonical turn ownership, precedence, and repair."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import constants
from mindroom import reply_lifecycle as rl
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_state_writer import ConversationStateWriter, ConversationStateWriterDeps
from mindroom.event_journal import journal, turn_rows
from mindroom.event_journal.identity import journal_principal_id
from mindroom.event_journal.models import ResponseRecoveryState
from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.storage import (
    read_scope_seen_event_ids,
    read_scope_state,
)
from mindroom.history.types import HistoryScope, HistoryScopeState, ScopeSummary, owner_scope
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.users import AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.message_target import MessageTarget
from mindroom.reply_turn.locked_turn import LockedTurns
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_sources import ResponseSources, SourceFate
from mindroom.session_storage.agent_storage import create_agent_state_storage, create_state_storage
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames
from mindroom.session_storage.legacy_scope_metadata import (
    LEGACY_COMPACTION_METADATA_KEY,
    LEGACY_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.session_storage.types import ConversationSession
from mindroom.teams.formation import TeamIntent, TeamMemberStatus, TeamResolution, TeamResolutionMember
from mindroom.teams.spec import TeamMode
from mindroom.turn_policy import ResponseAction
from mindroom.turn_record import (
    PreparedVoiceSource,
    RevisionReplay,
    SourceEventMetadata,
    SourceVisibleEcho,
    TurnRecord,
    canonicalize_turn_record,
)
from mindroom.turn_store import (
    ClaimContended,
    ClaimRefused,
    SourcePreparation,
    TurnClaim,
    TurnStore,
    TurnStoreDeps,
    _recovery_gap,
    _RecoveryGap,
)
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    build_unseen_context_messages_for_test,
    make_visible_message,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.history_helpers import compact_runs, stored_run_ids, summary_rows_of
from tests.journal_helpers import admit_room_event
from tests.redaction_helpers import remove_redacted_history_like_next_response
from tests.reply_turn_helpers import runner_for
from tests.session_storage.agno_layout import write_agno_database
from tests.test_edit_regenerator import _reply

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from mindroom.event_journal import EventJournalStore
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.session_storage.store import ConversationStore


ROOM = "!room:example.org"


def _in_room(record: TurnRecord, room_id: str = ROOM) -> TurnRecord:
    """Give a candidate the conversation every runtime write names."""
    target = MessageTarget.resolve(room_id, None, record.source_event_ids[-1])
    return canonicalize_turn_record(record, conversation_target=record.conversation_target or target)


async def _redact(
    journal_store: EventJournalStore,
    event_id: str,
    room_id: str = ROOM,
    *,
    agent_name: str = "agent",
) -> None:
    """Record a redaction as its admission does: once for the agent, applied to every Matrix login of it."""
    await journal_store.backend.write(
        lambda transaction: journal.redact_for_agent(
            transaction,
            agent_name,
            room_id=room_id,
            event_id=event_id,
            receipt_order=0,
        ),
    )


async def _store(journal_store: EventJournalStore, *, agent_name: str = "agent") -> TurnStore:
    """Return a store over this journal's turn rows, started as a runtime starts it."""
    principal = journal_store.principal(journal_principal_id(agent_name, "alice"))
    store = TurnStore(
        TurnStoreDeps(
            turn_records=journal_store.turn_records(agent_name),
            legacy_responses_file=None,
            state_writer=MagicMock(),
            tool_runtime=MagicMock(),
            response_recovery_state=principal.response_recovery_state,
            retry_dispatch_sources=MagicMock(),
        ),
    )
    await store.warm()
    return store


def test_a_team_reply_records_the_history_scope_of_the_members_it_formed_with(tmp_path: Path) -> None:
    """The pending record of an ad hoc team's reply names the history scope that all its formed members share."""
    config = bind_runtime_paths(
        Config(
            agents={name: AgentConfig(display_name=name.title()) for name in ("calculator", "general", "research")},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )
    members = [
        TeamResolutionMember(agent=MatrixID.parse(f"@mindroom_{name}:localhost"), name=name, status=status)
        for name, status in (
            ("research", TeamMemberStatus.ELIGIBLE),
            ("general", TeamMemberStatus.HIDDEN_FROM_SENDER),
            ("calculator", TeamMemberStatus.ELIGIBLE),
        )
    ]
    form_team = TeamResolution.team(
        intent=TeamIntent.IMPLICIT_THREAD_TEAM,
        requested_members=[member.agent for member in members],
        member_statuses=members,
        mode=TeamMode.COLLABORATE,
    )
    state_writer = ConversationStateWriter(
        ConversationStateWriterDeps(
            runtime=SimpleNamespace(config=config),
            logger=MagicMock(),
            runtime_paths=runtime_paths_for(config),
            agent_name="research",
        ),
    )
    store = TurnStore(
        TurnStoreDeps(
            turn_records=MagicMock(),
            legacy_responses_file=None,
            state_writer=state_writer,
            tool_runtime=MagicMock(),
            response_recovery_state=AsyncMock(),
            retry_dispatch_sources=MagicMock(),
        ),
    )

    scope = store.response_history_scope(
        ResponseAction(kind="team", form_team=form_team),
        requester_user_id="@alice:localhost",
    )

    assert scope == HistoryScope(kind="team", scope_id="team_calculator+research")


@pytest.mark.asyncio
async def test_an_edit_load_sees_a_write_already_queued_before_it(journal_store: EventJournalStore) -> None:
    """``get_turn_record_after_writes`` runs on the writer, after every write this process queued before it."""
    store = await _store(journal_store)
    recording = asyncio.create_task(
        store.record_pending_turn(_in_room(TurnRecord.create(["$event"], completed=False, response_owner="owner"))),
    )
    await asyncio.sleep(0)
    loaded = await store.get_turn_record_after_writes("$event")
    recorded = await recording

    assert loaded is not None
    assert recorded is not None
    assert loaded == recorded.record


def _seeded_storage(
    tmp_path: Path,
    session: ConversationSession | None = None,
    *,
    name: str = "agent",
) -> ConversationStore:
    """Return real conversation storage, seeded with ``session`` as an older release could have left it.

    Opening it converts that layout, as a restart onto this release would.
    """
    if session is not None:
        write_agno_database(AgnoTableNames(name).database_path(tmp_path / name), session, usage=False)
    owner = owner_scope(session) if session is not None else HistoryScope(kind="agent", scope_id=name)
    return create_state_storage(name, tmp_path / name, owner)


_EPOCH = datetime.fromtimestamp(0, UTC)


def _stored(storage: ConversationStore, session: ConversationSession) -> ConversationSession:
    """Reload ``session`` from storage."""
    stored = storage.load_session(session.session_id)
    assert stored is not None
    return stored


def _stored_run_ids(storage: ConversationStore, session: ConversationSession) -> list[str | None]:
    return [run.run_id for run in _stored(storage, session).runs or []]


def _seeded_storage_with_runs(
    tmp_path: Path,
    session: ConversationSession,
    *,
    name: str = "agent",
) -> ConversationStore:
    """Seed ``session`` and prove every run is readable, so later emptiness is meaningful."""
    storage = _seeded_storage(tmp_path, session, name=name)
    assert _stored_run_ids(storage, session) == [run.run_id for run in session.runs or []]
    return storage


def _with_legacy_seen_event_ids(session: ConversationSession, event_ids: Iterable[str]) -> dict[str, Any]:
    """Return ``session``'s metadata holding ``event_ids`` as its scope's preserved seen ids, as older releases stored them."""
    states = {owner_scope(session).key: {"seen_event_ids": sorted(event_ids)}}
    return {**(session.metadata or {}), LEGACY_MATRIX_HISTORY_METADATA_KEY: {"version": 1, "states": states}}


def _legacy_compacted_event_ids(run: RunRecord) -> list[str]:
    """Return the ids the destructive compactor preserved for one compacted run."""
    metadata = run.metadata or {}
    event_ids = list(metadata.get(constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY, []))
    revisions = metadata.get(constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY) or {}
    event_ids.extend(revision[1] for revision in revisions.values())
    return event_ids


async def _store_with_storage(
    journal_store: EventJournalStore,
    storage: ConversationStore,
    *,
    agent_name: str = "agent",
) -> TurnStore:
    state_writer = MagicMock()
    state_writer.create_storage.return_value = storage
    state_writer.history_scope.return_value = HistoryScope(kind="agent", scope_id="agent")
    store = TurnStore(
        TurnStoreDeps(
            turn_records=journal_store.turn_records(agent_name),
            legacy_responses_file=None,
            state_writer=state_writer,
            tool_runtime=MagicMock(),
            response_recovery_state=AsyncMock(),
            retry_dispatch_sources=MagicMock(),
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


async def _record_unrelated_turns(store: TurnStore) -> None:
    """Populate other threads and rooms with ordinary retained turn history."""
    for index in range(40):
        source_id = f"$unrelated-{index}"
        room_id = "!room:example.org" if index % 2 else "!other:example.org"
        await store.record_turn(
            TurnRecord.create(
                [source_id],
                response_event_id=f"$unrelated-reply-{index}",
                response_owner="agent",
                requester_id="@user:example.org",
                source_event_prompts={source_id: "Unrelated retained message"},
                source_event_revisions={source_id: (1, source_id)},
                conversation_target=MessageTarget.resolve(room_id, source_id, source_id),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [False, True])
async def test_response_preparation_leaves_its_own_and_unrelated_turns_alone(
    journal_store: EventJournalStore,
    editing: bool,
) -> None:
    """Preparing a response over retained history neither suppresses it nor changes what it stores."""
    store = await _store(journal_store)
    await _record_unrelated_turns(store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    record = replace(
        _owned_turn_record(target),
        completed=editing,
        response_event_id="$reply" if editing else None,
        source_event_prompts={"$user_msg": "Current message"},
        source_event_revisions={"$user_msg": (10, "$edit")},
    )
    if editing:
        await store.record_turn(record)
    else:
        await store.record_pending_turn(record)
    current = await store.get_turn_record("$user_msg")
    assert current is not None

    if editing:
        assert not await store.prepare_edit_snapshot(
            record=current,
            live_source_event_ids=("$user_msg",),
            driving_revision_id="$edit",
        )
    else:
        preparation = await store.prepare_pending_response_source(
            target=target,
            source_event_ids=("$user_msg",),
        )
        assert preparation.invalid == ()

    assert (await store.get_turn_record("$user_msg")).source_event_prompts == {"$user_msg": "Current message"}
    assert (await store.get_turn_record("$unrelated-0")).source_event_prompts == {
        "$unrelated-0": "Unrelated retained message",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("redacted_event_id", ["$user_msg", "$physical-edit"])
async def test_redaction_delivered_in_another_room_leaves_the_recorded_conversation_untouched(
    journal_store: EventJournalStore,
    redacted_event_id: str,
) -> None:
    """A redaction names its target by ID alone, so one delivered in another room must not erase this one."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    await store.record_turn(
        replace(
            _owned_turn_record(target),
            source_event_prompts={"$user_msg": "Edited message"},
            source_event_revisions={"$user_msg": (10, "$physical-edit")},
        ),
    )
    before = await store.get_turn_record("$user_msg")

    await _redact(journal_store, redacted_event_id, "!elsewhere:example.org")

    assert await store.get_turn_record("$user_msg") == before
    assert await store.redacted_history_events(target, (redacted_event_id,)) == {}
    await _redact(journal_store, redacted_event_id, target.room_id)
    assert await store.redacted_history_events(target, (redacted_event_id,)) == {redacted_event_id: None}
    assert (await store.get_turn_record("$user_msg")).source_event_prompts is None


@pytest.mark.asyncio
async def test_an_answer_hands_over_every_source_its_turn_indexes(journal_store: EventJournalStore) -> None:
    """A coalesced turn's FINAL binds it and settles all its sources; a delivery naming no stored turn settles itself."""
    store = await _store(journal_store)
    principal = journal_store.principal("agent@alice")
    for event_id in ("$first", "$second", "$unrecorded"):
        await admit_room_event(principal, ROOM, event_id)
    pending = await store.record_pending_turn(
        _in_room(TurnRecord.create(["$first", "$second"], discovery_event_ids=["$relay"], completed=False)),
    )
    assert pending is not None
    turn = pending.record

    bound = await principal.enqueue_final_answer(
        delivery_id="$second",
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
        turn_key=turn.turn_key,
    )
    unbound = await principal.enqueue_final_answer(
        delivery_id="$unrecorded",
        room_id=ROOM,
        thread_id=None,
        payload={"body": "answer"},
        turn_key=None,
    )

    assert bound is not None
    assert bound.settled_event_ids == turn.indexed_event_ids
    assert unbound is not None
    assert unbound.settled_event_ids == ("$unrecorded",)


@pytest.mark.parametrize(
    ("state", "gap"),
    [(rl.ReplyState.COMPLETED, None), (rl.ReplyState.ACTIVE, _RecoveryGap("reply_unfinished"))],
)
def test_a_reply_alone_decides_whether_the_turn_it_answers_is_recoverable(
    state: rl.ReplyState,
    gap: _RecoveryGap | None,
) -> None:
    """A finished reply proves its settled turn without an outbox row, and an unfinished one does not."""
    snapshot = ResponseRecoveryState(
        pending_sources=(False,),
        final_delivery=None,
        sources_settled_by_departure=False,
        redacted_sources=(False,),
        turn_records=(None,),
        reply=_reply(event_id="$answer", state=state),
    )

    assert _recovery_gap(TurnRecord.create(["$source"]), snapshot) == gap


@pytest.mark.asyncio
@pytest.mark.parametrize("known_before_redaction", ["unseen", "admitted", "pending"])
async def test_redaction_in_another_room_cannot_tombstone_an_event_without_a_recorded_room(
    journal_store: EventJournalStore,
    known_before_redaction: str,
) -> None:
    """A foreign redaction of an unanswered event must not drop it or block replies in its own room."""
    store = await _store(journal_store)
    principal = journal_store.principal("agent@alice")
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    if known_before_redaction != "unseen":
        await admit_room_event(principal, target.room_id, "$victim")
    if known_before_redaction == "pending":
        await store.record_pending_turn(_in_room(TurnRecord.create(["$victim"], completed=False)))
    before = await store.get_turn_record("$victim")

    await _redact(journal_store, "$victim", "!elsewhere:example.org")

    assert await store.get_turn_record("$victim") == before
    assert not await store.is_handled(target.room_id, "$victim")
    await admit_room_event(principal, target.room_id, "$victim")
    await store.record_pending_turn(replace(_owned_turn_record(target), response_event_id=None, completed=False))
    preparation = await store.prepare_pending_response_source(
        target=target,
        source_event_ids=("$user_msg",),
        thread_history=[make_visible_message(event_id="$victim", body="Earlier message", thread_id="$thread")],
    )
    assert preparation.invalid == ()


@pytest.mark.asyncio
async def test_source_preparation_writes_only_to_register_the_edits_its_history_consumes(
    journal_store: EventJournalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparing a reply's sources is a read; only an edit its history consumes waits for the single writer."""
    store = await _store(journal_store)
    target = MessageTarget.resolve(ROOM, "$thread", "$user_msg")
    await store.record_pending_turn(replace(_owned_turn_record(target), response_event_id=None, completed=False))
    backend = store.deps.turn_records.backend
    real_write = backend.write
    writes: list[object] = []

    async def counted_write(operation: Callable[..., object]) -> object:
        writes.append(operation)
        return await real_write(operation)

    monkeypatch.setattr(backend, "write", counted_write)
    context = make_visible_message(event_id="$context", body="Context", thread_id="$thread")
    edited = replace(
        make_visible_message(event_id="$edited", body="Edited", timestamp=1, thread_id="$thread"),
        latest_event_id="$edit",
        edited_timestamp=10,
    )

    async def prepare(history: list[ResolvedVisibleMessage]) -> SourcePreparation:
        return await store.prepare_pending_response_source(
            target=target,
            source_event_ids=("$user_msg",),
            thread_history=history,
        )

    assert await prepare([context]) == SourcePreparation(surviving=("$user_msg",))
    assert writes == []
    assert await prepare([context, edited]) == SourcePreparation(surviving=("$user_msg",))
    assert len(writes) == 1
    record = await store.get_turn_record("$user_msg")
    assert record is not None
    assert record.revision_replay == {"$edit": RevisionReplay("$edited", 10)}


@pytest.mark.asyncio
async def test_redaction_sanitizes_only_turns_referencing_that_revision(journal_store: EventJournalStore) -> None:
    """One physical edit loses its text where it was selected, keeps its watermark, and leaves other turns alone."""
    store = await _store(journal_store)
    await _record_unrelated_turns(store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    await store.record_turn(
        replace(
            _owned_turn_record(target),
            source_event_prompts={"$user_msg": "Deleted edit"},
            source_event_revisions={"$user_msg": (10, "$physical-edit")},
        ),
    )
    await store.record_turn(
        TurnRecord.create(
            ["$context-consumer"],
            response_event_id="$context-reply",
            response_owner="agent",
            requester_id="@user:example.org",
            source_event_prompts={"$context-consumer": "Surviving message"},
            revision_replay={"$physical-edit": RevisionReplay("$user_msg", 10)},
            conversation_target=MessageTarget.resolve("!room:example.org", "$other-thread", "$context-consumer"),
        ),
    )

    await _redact(journal_store, "$physical-edit")

    edited = await store.get_turn_record("$user_msg")
    consumer = await store.get_turn_record("$context-consumer")
    assert edited.source_event_prompts is None
    assert edited.source_event_revisions is None
    assert edited.revision_watermark("$user_msg") == (10, "$physical-edit")
    assert consumer.source_event_prompts == {"$context-consumer": "Surviving message"}
    assert "$physical-edit" in consumer.revision_replay
    assert (await store.get_turn_record("$unrelated-1")).source_event_prompts == {
        "$unrelated-1": "Unrelated retained message",
    }


async def _prepare_redaction(
    store: TurnStore,
    target: MessageTarget,
    *,
    redacted_event_id: str = "$user_msg",
) -> bool:
    """Redact one event and run the next response's locked gate and history cleanup."""
    await _redact_through(store, redacted_event_id)
    suppressed = await _suppressed(store, target, ("$later",))
    await remove_redacted_history_like_next_response(
        store,
        target,
        store.deps.state_writer.create_storage(None, scope=HistoryScope(kind="agent", scope_id="agent")),
    )
    return suppressed


async def _suppressed(store: TurnStore, target: MessageTarget, source_event_ids: tuple[str, ...]) -> bool:
    """Prepare the next response over these sources under its lock; return whether a deletion suppresses it."""
    await store.prepare_pending_response_source(
        target=target,
        source_event_ids=source_event_ids,
    )
    return bool(await store.deps.turn_records.read(turn_rows.deleted_events, target.room_id, source_event_ids))


async def _redact_through(store: TurnStore, event_id: str, room_id: str = ROOM) -> None:
    """Redact an event in the database the store's rows live in, for its agent."""
    await store.deps.turn_records.backend.write(
        lambda transaction: journal.redact_for_agent(
            transaction,
            store.deps.turn_records.agent_name,
            room_id=room_id,
            event_id=event_id,
            receipt_order=0,
        ),
    )


@pytest.mark.asyncio
async def test_pending_delivery_intent_does_not_require_model_history_scope(journal_store: EventJournalStore) -> None:
    """Response ownership and target distinguish delivery intent from a raw visible echo."""
    store = await _store(journal_store)
    source_event_ids = ("$source",)
    await store.record_visible_echo(ROOM, "$source", "$echo")

    target = MessageTarget.resolve(ROOM, None, "$source")
    delivery_intent = store.attach_response_context(
        TurnRecord.create(source_event_ids, completed=False),
        history_scope=None,
        conversation_target=target,
    )
    first = await store.record_pending_turn(delivery_intent)
    retry = await store.record_pending_turn(delivery_intent)

    assert first is not None
    assert first.resumes_response is False, "a visible echo alone owns no response"
    assert retry is not None
    assert retry.resumes_response is True
    record = await store.get_turn_record("$source")
    assert record is not None
    assert record.response_owner == "agent"
    assert record.conversation_target == target
    assert record.history_scope is None
    assert record.response_event_id is None
    assert record.visible_echoes["$source"].event_id == "$echo"


@pytest.mark.asyncio
async def test_edit_registration_after_retention_evicted_the_turn_finds_nothing(
    journal_store: EventJournalStore,
) -> None:
    """An edit that loaded a turn retention has since evicted registers nothing and takes the missing-turn path."""
    store = await _store(journal_store)
    with patch.object(turn_rows.time, "time", return_value=time.time() - 40 * 24 * 60 * 60):
        await store.record_turn(_in_room(TurnRecord.create(["$old"])))
    loaded = await store.get_turn_record_after_writes("$old")
    assert loaded is not None

    await store.cleanup()

    assert await store.register_edit_revision(loaded, "$old", (20, "$edit")) is None
    assert await store.get_turn_record("$old") is None


@pytest.mark.asyncio
async def test_a_live_claim_is_observable_without_waiting_for_it(journal_store: EventJournalStore) -> None:
    """Whether a turn still owns a source, asked without blocking on the answer.

    The claim is taken before the turn is handed off and dropped however the
    turn ends, so its presence is what separates "a turn owns this" from "a
    turn owned this and is gone". A caller holding durable work owed to that
    turn has to be able to ask without joining the wait.
    """
    store = await _store(journal_store)

    assert store.has_live_turn_claim("$source") is False

    claim = await store.claim(TurnRecord.create(("$source",), discovery_event_ids=("$alias",), completed=False))
    assert isinstance(claim, TurnClaim)
    assert store.has_live_turn_claim("$source") is True
    assert store.has_live_turn_claim("$alias") is True, "an alias is indexed by the same claim"
    assert store.has_live_turn_claim("$unrelated") is False

    claim.release()

    assert store.has_live_turn_claim("$source") is False
    assert store.has_live_turn_claim("$alias") is False


@pytest.mark.asyncio
async def test_pending_turn_claim_allows_only_one_concurrent_owner(journal_store: EventJournalStore) -> None:
    """Overlapping delivery of one source event must start one response."""
    store = await _store(journal_store)
    turn = TurnRecord.create(["$source"], completed=False)

    results = await asyncio.gather(*(store.claim(turn) for _ in range(8)))

    owners = [result for result in results if isinstance(result, TurnClaim)]
    assert len(owners) == 1
    assert all(result.blocking is owners[0] for result in results if isinstance(result, ClaimContended))
    owners[0].release()
    assert isinstance(await store.claim(turn), TurnClaim)


@pytest.mark.asyncio
async def test_discovery_alias_allows_original_but_excludes_second_relay(journal_store: EventJournalStore) -> None:
    """One human original may overlap its relay, but two relays for it may not."""
    store = await _store(journal_store)
    original = await store.claim(TurnRecord.create(["$human"], completed=False))
    first_relay = await store.claim(
        TurnRecord.create(["$relay-one"], discovery_event_ids=["$human"], completed=False),
    )
    duplicate_relay = TurnRecord.create(["$relay-two"], discovery_event_ids=["$human"], completed=False)
    assert isinstance(original, TurnClaim)
    assert isinstance(first_relay, TurnClaim)

    assert await store.claim(duplicate_relay) == ClaimContended(first_relay)

    first_relay.release()
    duplicate = await store.claim(duplicate_relay)
    assert isinstance(duplicate, TurnClaim)
    duplicate.release()
    original.release()


@pytest.mark.asyncio
async def test_a_stale_release_cannot_drop_another_owners_equal_claim(journal_store: EventJournalStore) -> None:
    """Release is by handle: a finished owner releasing again leaves the next owner of the same turn alone."""
    store = await _store(journal_store)
    turn = TurnRecord.create(["$source"], discovery_event_ids=["$alias"], completed=False)
    first = await store.claim(turn)
    assert isinstance(first, TurnClaim)
    first.release()
    second = await store.claim(turn)
    assert isinstance(second, TurnClaim)

    first.release()

    assert store.has_live_turn_claim("$source") is True
    assert await store.claim(turn) == ClaimContended(second)
    second.release()
    assert store.has_live_turn_claim("$source") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [asyncio.CancelledError, RuntimeError])
async def test_a_claim_whose_alias_lookup_fails_leaks_nothing(
    journal_store: EventJournalStore,
    failure: type[BaseException],
) -> None:
    """A claim published before its alias-owner lookup is released when the lookup is cancelled or fails."""
    store = await _store(journal_store)
    turn = TurnRecord.create(["$relay"], discovery_event_ids=["$human"], completed=False)

    with patch.object(store, "get_turn_record", side_effect=failure("lookup failed")), pytest.raises(failure):
        await store.claim(turn)

    assert store.has_live_turn_claim("$relay") is False
    assert store.has_live_turn_claim("$human") is False
    assert isinstance(await store.claim(turn), TurnClaim)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed_claim", [False, True])
async def test_same_turn_can_reclaim_its_handled_alias(
    journal_store: EventJournalStore,
    *,
    completed_claim: bool,
) -> None:
    """A relay turn must remain claimable for its own edit or restart drain."""
    store = await _store(journal_store)
    completed = _in_room(TurnRecord.create(["$relay"], discovery_event_ids=["$human"]))
    await store.record_turn(completed)

    claim = await store.claim(replace(completed, completed=completed_claim))

    assert isinstance(claim, TurnClaim)
    claim.release()


@pytest.mark.asyncio
async def test_a_duplicate_of_an_answered_relay_is_refused(journal_store: EventJournalStore) -> None:
    """A second relay of an original another relay answered may not claim, and holds nothing."""
    store = await _store(journal_store)
    await store.record_turn(_in_room(TurnRecord.create(["$relay-one"], discovery_event_ids=["$human"])))

    refused = await store.claim(TurnRecord.create(["$relay-two"], discovery_event_ids=["$human"], completed=False))

    assert refused == ClaimRefused()
    assert store.has_live_turn_claim("$relay-two") is False


@pytest.mark.asyncio
async def test_a_turn_without_sources_is_refused(journal_store: EventJournalStore) -> None:
    """Only physical sources make a turn claimable; a turn without any holds nothing, not even its aliases."""
    store = await _store(journal_store)

    assert await store.claim(TurnRecord.create([], discovery_event_ids=["$alias"], completed=False)) == ClaimRefused()
    assert store.has_live_turn_claim("$alias") is False


@pytest.mark.asyncio
async def test_pending_coalesced_turn_can_reclaim_a_redacted_alias(journal_store: EventJournalStore) -> None:
    """A coalesced relay turn stays claimable for its sibling after the message one of its relays answers was deleted."""
    store = await _store(journal_store)
    pending = _in_room(
        TurnRecord.create(
            ["$relay-one", "$relay-two"],
            discovery_event_ids=["$human-one", "$human-two"],
            source_event_metadata={
                "$relay-one": SourceEventMetadata("@user:example.org", discovery_event_id="$human-one"),
                "$relay-two": SourceEventMetadata("@user:example.org", discovery_event_id="$human-two"),
            },
            completed=False,
        ),
    )
    stored = await store.record_pending_turn(pending)
    await _redact(journal_store, "$human-two")

    assert stored is not None
    assert await store.is_handled(ROOM, "$human-two") is True
    assert await store.deleted_sources(stored.record) == {"$relay-two"}
    claim = await store.claim(pending)
    assert isinstance(claim, TurnClaim)
    claim.release()


@pytest.mark.asyncio
async def test_a_merge_keeps_every_replaced_event_and_retires_the_replaced_handles(
    journal_store: EventJournalStore,
) -> None:
    """The batch claim covers what each message claimed, and a late release of a message's claim does nothing."""
    store = await _store(journal_store)
    relay = await store.claim(TurnRecord.create(["$relay"], discovery_event_ids=["$human"], completed=False))
    text = await store.claim(TurnRecord.create(["$text"], completed=False))
    assert isinstance(relay, TurnClaim)
    assert isinstance(text, TurnClaim)

    merged = await store.merge_claims((relay, text), TurnRecord.create(["$relay"], completed=False))

    assert isinstance(merged, TurnClaim)
    assert merged.source_event_ids == {"$relay", "$text"}, "a source only a message claim held stays claimed"
    assert merged.discovery_event_ids == {"$human"}, "the alias only a message claim held stays claimed"
    relay.release()
    text.release()
    assert all(store.has_live_turn_claim(event_id) for event_id in ("$relay", "$text", "$human"))
    merged.release()
    assert not any(store.has_live_turn_claim(event_id) for event_id in ("$relay", "$text", "$human"))


@pytest.mark.asyncio
async def test_a_merge_overlapping_a_claim_outside_it_is_refused(journal_store: EventJournalStore) -> None:
    """A batch whose turn reaches an event another live owner holds takes nothing and gives its claims up."""
    store = await _store(journal_store)
    mine = await store.claim(TurnRecord.create(["$mine"], completed=False))
    outsider = await store.claim(TurnRecord.create(["$theirs"], completed=False))
    assert isinstance(mine, TurnClaim)
    assert isinstance(outsider, TurnClaim)

    assert await store.merge_claims((mine,), TurnRecord.create(["$mine", "$theirs"], completed=False)) is None

    assert store.has_live_turn_claim("$mine") is False
    assert store.has_live_turn_claim("$theirs") is True
    outsider.release()


@pytest.mark.asyncio
async def test_a_merge_whose_alias_another_turn_answered_is_refused(journal_store: EventJournalStore) -> None:
    """A batch carrying an alias an answered turn of another identity owns leaves every one of its events unowned."""
    store = await _store(journal_store)
    claim = await store.claim(TurnRecord.create(["$relay-two"], completed=False))
    assert isinstance(claim, TurnClaim)
    await store.record_turn(_in_room(TurnRecord.create(["$relay-one"], discovery_event_ids=["$human"])))

    merged = await store.merge_claims(
        (claim,),
        TurnRecord.create(["$relay-two"], discovery_event_ids=["$human"], completed=False),
    )

    assert merged is None
    assert store.has_live_turn_claim("$relay-two") is False
    assert store.has_live_turn_claim("$human") is False


@pytest.mark.asyncio
async def test_a_contended_source_counts_as_owned_until_its_blocker_releases(journal_store: EventJournalStore) -> None:
    """A source that lost its claim is left to the journal only once the claim it lost to is gone."""
    store = await _store(journal_store)
    retry = cast("MagicMock", store.deps.retry_dispatch_sources)
    winner = await store.claim(TurnRecord.create(["$relay-one"], discovery_event_ids=["$human"], completed=False))
    contended = await store.claim(TurnRecord.create(["$relay-two"], discovery_event_ids=["$human"], completed=False))
    assert isinstance(winner, TurnClaim)
    assert contended == ClaimContended(winner)

    store.retry_when_released(winner, "!room", "$relay-two")

    assert store.has_live_turn_claim("$relay-two") is True
    retry.assert_not_called()
    winner.release()
    retry.assert_called_once_with("!room", ("$relay-two",))
    assert store.has_live_turn_claim("$relay-two") is False


@pytest.mark.asyncio
async def test_a_contended_source_waits_for_the_claim_that_replaced_its_blocker(
    journal_store: EventJournalStore,
) -> None:
    """A batch merge carries the sources waiting on a message's claim, so they return when the batch ends."""
    store = await _store(journal_store)
    retry = cast("MagicMock", store.deps.retry_dispatch_sources)
    winner = await store.claim(TurnRecord.create(["$relay-one"], discovery_event_ids=["$human"], completed=False))
    assert isinstance(winner, TurnClaim)
    store.retry_when_released(winner, "!room", "$relay-two")

    merged = await store.merge_claims((winner,), TurnRecord.create(["$relay-one"], completed=False))
    assert isinstance(merged, TurnClaim)
    winner.release()

    retry.assert_not_called()
    assert store.has_live_turn_claim("$relay-two") is True
    merged.release()
    retry.assert_called_once_with("!room", ("$relay-two",))


@pytest.mark.asyncio
async def test_a_source_contending_with_a_released_claim_is_retried_at_once(journal_store: EventJournalStore) -> None:
    """A blocker gone before its loser registers leaves nothing to wait for."""
    store = await _store(journal_store)
    retry = cast("MagicMock", store.deps.retry_dispatch_sources)
    winner = await store.claim(TurnRecord.create(["$source"], completed=False))
    assert isinstance(winner, TurnClaim)
    winner.release()

    store.retry_when_released(winner, "!room", "$source")

    retry.assert_called_once_with("!room", ("$source",))
    assert store.has_live_turn_claim("$source") is False


@pytest.mark.asyncio
async def test_sibling_turn_stores_of_one_agent_share_their_claims(journal_store: EventJournalStore) -> None:
    """A replacement runtime's store meets the claims its predecessor still holds, and another agent's do not."""
    first = await _store(journal_store)
    replacement = await _store(journal_store)
    other_agent = await _store(journal_store, agent_name="other")
    claim = await first.claim(TurnRecord.create(["$source"], completed=False))
    assert isinstance(claim, TurnClaim)

    assert await replacement.claim(TurnRecord.create(["$source"], completed=False)) == ClaimContended(claim)
    assert replacement.has_live_turn_claim("$source") is True
    other = await other_agent.claim(TurnRecord.create(["$source"], completed=False))
    assert isinstance(other, TurnClaim)
    other.release()
    claim.release()
    assert replacement.has_live_turn_claim("$source") is False


@pytest.mark.asyncio
async def test_prepare_redaction_removes_causal_run_suffix(journal_store: EventJournalStore, tmp_path: Path) -> None:
    """Redacting a source must delete later output that may depend on that run."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="run-1",
                agent_id="agent",
                session_id=target.session_id,
                metadata={"matrix_event_id": "$user_msg"},
            ),
            RunRecord(
                run_id="run-2",
                agent_id="agent",
                session_id=target.session_id,
                metadata={"matrix_event_id": "$other"},
            ),
        ],
    )
    storage = _seeded_storage_with_runs(tmp_path, session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))

    should_suppress = await _prepare_redaction(store, target)

    assert should_suppress is False
    assert _stored_run_ids(storage, session) == []


@pytest.mark.asyncio
async def test_editing_a_folded_turn_beside_a_redacted_one_leaves_no_runs_or_summary(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Editing a folded turn while a later folded turn is redacted cuts both runs and the summary that folded them."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$a")
    scope = HistoryScope(kind="agent", scope_id="agent")

    def run(event_id: str) -> RunRecord:
        return RunRecord(
            run_id=f"{event_id}-run",
            agent_id="agent",
            session_id=target.session_id,
            metadata={
                constants.MATRIX_EVENT_ID_METADATA_KEY: event_id,
                constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: [event_id],
                constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [event_id],
            },
        )

    storage = _seeded_storage_with_runs(
        tmp_path,
        ConversationSession(kind="agent", session_id=target.session_id, owner_id="agent", runs=[run("$a"), run("$b")]),
    )
    stored = storage.load_session(target.session_id)
    assert stored is not None
    compact_runs(storage, stored, [run.run_id for run in stored.runs or [] if run.run_id], "summary of a and b")
    store = await _store_with_storage(journal_store, storage)
    records = {
        event_id: TurnRecord.create(
            [event_id],
            response_event_id=f"{event_id}-reply",
            response_owner="agent",
            requester_id="@user:example.org",
            history_scope=scope,
            conversation_target=target,
        )
        for event_id in ("$a", "$b")
    }
    for record in records.values():
        await store.record_turn(record)
    await _redact_through(store, "$b", target.room_id)

    await store.remove_stale_runs_for_edit(turn_record=records["$a"], requester_user_id="@user:example.org")
    await remove_redacted_history_like_next_response(store, target, storage, scope=scope)

    persisted = storage.load_session(target.session_id)
    assert persisted is not None
    assert [stored_run.run_id for stored_run in persisted.runs or []] == []
    assert persisted.summary is None


@pytest.mark.asyncio
async def test_editing_a_team_turn_removes_the_team_history_of_an_earlier_redacted_turn(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Editing a team turn removes, from the team's session, the history an earlier redacted turn left there."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$edited")
    scope = HistoryScope(kind="team", scope_id="team_general")

    def run(event_id: str) -> TeamRunRecord:
        return TeamRunRecord(
            run_id=f"{event_id}-run",
            team_id="team_general",
            session_id=target.session_id,
            metadata={
                constants.MATRIX_EVENT_ID_METADATA_KEY: event_id,
                constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: [event_id],
                constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [event_id],
            },
        )

    session = ConversationSession(
        kind="team",
        session_id=target.session_id,
        owner_id="team_general",
        runs=[run("$redacted"), run("$edited")],
    )
    storage = _seeded_storage_with_runs(tmp_path, session)
    store = await _store_with_storage(journal_store, storage)
    records = {
        event_id: TurnRecord.create(
            [event_id],
            response_event_id=f"{event_id}-reply",
            response_owner="team_general",
            requester_id="@user:example.org",
            history_scope=scope,
            conversation_target=target,
        )
        for event_id in ("$redacted", "$edited")
    }
    for record in records.values():
        await store.record_turn(record)
    await _redact_through(store, "$redacted", target.room_id)

    await store.remove_stale_runs_for_edit(turn_record=records["$edited"], requester_user_id="@user:example.org")

    assert _stored_run_ids(storage, session) == []


@pytest.mark.asyncio
async def test_edit_then_redaction_never_replays_the_old_causal_suffix(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Editing a source must remove later output before its replacement is persisted."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$source")
    scope = HistoryScope(kind="agent", scope_id="agent")
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="source-run",
                agent_id="agent",
                session_id=target.session_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$source",
                    constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$source"],
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$source"],
                },
            ),
            RunRecord(
                run_id="dependent-run",
                agent_id="agent",
                session_id=target.session_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$dependent",
                    constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$dependent"],
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$dependent"],
                },
            ),
        ],
    )
    storage = _seeded_storage_with_runs(tmp_path, session)
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

    await store.remove_stale_runs_for_edit(
        turn_record=source_record,
        requester_user_id="@user:example.org",
    )
    assert _stored_run_ids(storage, session) == []

    storage.save_run(
        session,
        RunRecord(
            run_id="replacement-run",
            agent_id="agent",
            session_id=target.session_id,
            metadata={
                constants.MATRIX_EVENT_ID_METADATA_KEY: "$source",
                constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$source"],
                constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$source"],
            },
        ),
        record_usage=False,
    )
    should_suppress = await _prepare_redaction(store, target, redacted_event_id="$source")

    assert should_suppress is False
    assert _stored_run_ids(storage, session) == []


@pytest.mark.asyncio
async def test_prepare_redaction_removes_runs_that_consumed_the_source(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A later run that consumed redacted context must not remain eligible for replay."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="later-run",
                agent_id="agent",
                session_id=target.session_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$later",
                    constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$later"],
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$user_msg", "$later"],
                },
            ),
        ],
    )
    storage = _seeded_storage_with_runs(tmp_path, session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))

    should_suppress = await _prepare_redaction(store, target)

    assert should_suppress is False
    assert _stored_run_ids(storage, session) == []


@pytest.mark.asyncio
async def test_each_history_scope_that_read_a_redacted_source_drops_it_when_opened(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Each history scope that read a redacted source drops it when it next opens, not only the source's own."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    agent_scope = HistoryScope(kind="agent", scope_id="agent")
    team_scope = HistoryScope(kind="team", scope_id="team_private")
    agent_session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="agent-run",
                agent_id="agent",
                session_id=target.session_id,
                metadata={"matrix_event_id": "$user_msg"},
            ),
        ],
    )
    team_session = ConversationSession(
        kind="team",
        session_id=target.session_id,
        owner_id=team_scope.scope_id,
        runs=[
            TeamRunRecord(
                run_id="team-run",
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
        agent_scope.key: _seeded_storage_with_runs(tmp_path, agent_session),
        team_scope.key: _seeded_storage_with_runs(tmp_path, team_session, name="team_private"),
    }
    state_writer = MagicMock()
    state_writer.create_storage.side_effect = lambda _identity, *, scope: storages[scope.key]
    state_writer.history_scope.return_value = agent_scope
    store = TurnStore(
        TurnStoreDeps(
            turn_records=journal_store.turn_records("agent"),
            legacy_responses_file=None,
            state_writer=state_writer,
            tool_runtime=MagicMock(),
            response_recovery_state=AsyncMock(),
            retry_dispatch_sources=MagicMock(),
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
    await remove_redacted_history_like_next_response(store, target, storages[team_scope.key], scope=team_scope)

    assert should_suppress is False
    assert _stored_run_ids(storages[agent_scope.key], agent_session) == []
    assert _stored_run_ids(storages[team_scope.key], team_session) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source_owner", [None, "other"])
async def test_another_requesters_private_scope_drops_a_redacted_source_when_opened(
    journal_store: EventJournalStore,
    tmp_path: Path,
    *,
    source_owner: str | None,
) -> None:
    """A private scope of another requester that read the source drops it when it next opens."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    default_scope = HistoryScope(kind="agent", scope_id="agent")
    team_scope = HistoryScope(kind="team", scope_id="team_private")
    team_session = ConversationSession(
        kind="team",
        session_id=target.session_id,
        owner_id=team_scope.scope_id,
        runs=[
            TeamRunRecord(
                run_id="team-run",
                session_id=target.session_id,
                team_id=team_scope.scope_id,
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: "$later",
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: ["$user_msg", "$later"],
                },
            ),
        ],
    )
    team_storage = _seeded_storage_with_runs(tmp_path, team_session, name="team_private")
    storages = {
        (default_scope.key, "@source:example.org"): _seeded_storage(tmp_path),
        (team_scope.key, "@later:example.org"): team_storage,
    }
    state_writer = MagicMock()
    state_writer.history_scope.return_value = default_scope
    state_writer.create_storage.side_effect = lambda identity, *, scope: storages[(scope.key, identity)]
    tool_runtime = MagicMock()
    tool_runtime.build_execution_identity.side_effect = lambda *, target, user_id: user_id  # noqa: ARG005
    store = TurnStore(
        TurnStoreDeps(
            turn_records=journal_store.turn_records("agent"),
            legacy_responses_file=None,
            state_writer=state_writer,
            tool_runtime=tool_runtime,
            response_recovery_state=AsyncMock(),
            retry_dispatch_sources=MagicMock(),
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

    await _redact_through(store, "$user_msg", "!room:example.org")
    should_suppress = await _suppressed(store, target, ("$later",))
    await remove_redacted_history_like_next_response(store, target, team_storage, scope=team_scope)

    assert should_suppress is False
    assert _stored_run_ids(team_storage, team_session) == []


@pytest.mark.asyncio
async def test_redaction_before_its_turn_registers_still_removes_the_replay(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A redaction that lands before its source turn registers is still found by the next response."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="run-1",
                agent_id="agent",
                session_id=target.session_id,
                metadata={"matrix_event_id": "$user_msg"},
            ),
        ],
    )
    storage = _seeded_storage_with_runs(tmp_path, session)
    store = await _store_with_storage(journal_store, storage)
    await admit_room_event(journal_store.principal("agent@alice"), "!room:example.org", "$user_msg")
    await _redact_through(store, "$user_msg", "!room:example.org")
    pending = await store.record_pending_turn(
        TurnRecord.create(
            ["$user_msg"],
            completed=False,
            requester_id="@user:example.org",
            response_owner="agent",
            history_scope=HistoryScope(kind="agent", scope_id="agent"),
            conversation_target=target,
            source_event_prompts={"$user_msg": "REDACTED_SECRET"},
        ),
    )
    assert pending is not None
    assert pending.deleted_sources == {"$user_msg"}
    assert pending.record.source_event_prompts is None

    should_suppress = await _suppressed(store, target, ("$user_msg",))
    await remove_redacted_history_like_next_response(store, target, storage)

    assert should_suppress is True
    assert _stored_run_ids(storage, session) == []


@pytest.mark.asyncio
async def test_prepare_redaction_invalidates_legacy_compacted_replay(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Redaction must remove content that a pre-archive compaction folded into the summary."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="agent", scope_id="agent")
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="post-compaction",
                agent_id="agent",
                session_id=target.session_id,
                metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$post-compaction"},
            ),
        ],
        summary=ScopeSummary(text="The user disclosed REDACTED_SECRET.", updated_at=_EPOCH),
    )
    session.metadata = _with_legacy_seen_event_ids(session, {"$user_msg", "$older"})
    session.metadata = {
        **(session.metadata or {}),
        LEGACY_COMPACTION_METADATA_KEY: {
            "version": 2,
            "states": {scope.key: {"last_summary_model": "summary-model", "compacted_run_ids": ["$compacted-run"]}},
        },
    }
    storage = _seeded_storage_with_runs(tmp_path, session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))

    should_suppress = await _prepare_redaction(store, target)

    assert should_suppress is False
    stored = _stored(storage, session)
    assert stored.runs == []
    assert stored.summary is None
    assert read_scope_seen_event_ids(storage, stored) == set()
    assert read_scope_state(stored) == HistoryScopeState()
    assert summary_rows_of(storage, scope.key, target.session_id) == []
    assert stored_run_ids(storage, target.session_id) == []


@pytest.mark.asyncio
async def test_redaction_before_response_registration_tombstones_pending_coalesced_turn(
    journal_store: EventJournalStore,
) -> None:
    """A source redacted before response startup is deleted in its later pending batch, its sibling is not."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$second")
    team_scope = HistoryScope(kind="team", scope_id="team_private")

    await admit_room_event(journal_store.principal("agent@alice"), "!room:example.org", "$first")
    await _redact_through(store, "$first", "!room:example.org")
    pending = await store.record_pending_turn(
        TurnRecord.create(
            ["$first", "$second"],
            completed=False,
            source_event_prompts={"$first": "REDACTED_SECRET", "$second": "keep"},
            requester_id="@user:example.org",
            response_owner="agent",
            history_scope=team_scope,
            conversation_target=target,
        ),
    )

    assert pending is not None
    assert pending.record.completed is False
    assert pending.deleted_sources == {"$first"}
    assert pending.record.source_event_prompts == {"$second": "keep"}
    assert pending.record.history_scope == team_scope
    assert await store.is_handled(target.room_id, "$first") is True
    assert await store.is_handled(target.room_id, "$second") is False


@pytest.mark.asyncio
async def test_a_source_a_completed_sibling_leaves_behind_loses_its_record_and_stays_redactable(
    journal_store: EventJournalStore,
) -> None:
    """A pending batch re-formed by its sibling's own answer drops the other source, which a redaction still reaches."""
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

    await _redact_through(store, "$first", "!room:example.org")

    assert await store.get_turn_record("$first") is None
    assert await store.is_handled(target.room_id, "$first") is True
    completed_sibling = await store.get_turn_record("$second")
    assert completed_sibling is not None
    assert completed_sibling.source_event_ids == ("$second",)
    assert completed_sibling.response_event_id == "$second-reply"


@pytest.mark.asyncio
async def test_completed_sibling_leaves_a_redacted_source_handled_across_reopen(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A source redacted inside a pending batch stays handled after its sibling answers alone, and after a restart."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$second")
    scope = HistoryScope(kind="agent", scope_id="agent")
    storage = _seeded_storage(tmp_path)
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
    await _redact_through(store, "$first", "!room:example.org")
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

    completed_sibling = await store.get_turn_record("$second")
    assert completed_sibling is not None
    assert completed_sibling.source_event_ids == ("$second",)
    assert completed_sibling.response_event_id == "$second-reply"
    reopened = await _store_with_storage(journal_store, storage)
    assert await reopened.is_handled(target.room_id, "$first") is True
    assert await reopened.redacted_history_events(target, ("$first", "$second")) == {"$first": None}


@pytest.mark.asyncio
async def test_redaction_cleanup_keeps_context_after_colliding_alias_projection(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A source a completed relay left behind is still found by its conversation's next history cleanup."""
    relay_event_id = "$relay"
    human_event_id = "$human"
    requester_user_id = "@alice:example.org"
    target = MessageTarget.resolve("!room:example.org", "$thread", human_event_id)
    scope = HistoryScope(kind="agent", scope_id="agent")
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="human-run",
                agent_id="agent",
                session_id=target.session_id,
                metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: human_event_id},
            ),
        ],
        summary=ScopeSummary(text="contains REDACTED_SECRET", updated_at=_EPOCH),
    )
    session.metadata = _with_legacy_seen_event_ids(session, {human_event_id})
    storage = _seeded_storage_with_runs(tmp_path, session)
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
        _in_room(
            TurnRecord.create([relay_event_id], response_event_id="$relay-reply", requester_id="@bob:example.org"),
        ),
    )

    await _redact_through(store, human_event_id, "!room:example.org")

    assert await store.get_turn_record(human_event_id) is None
    assert (await store.get_turn_record(relay_event_id)).response_event_id == "$relay-reply"
    should_suppress = await _suppressed(store, target, ("$later",))
    await remove_redacted_history_like_next_response(store, target, storage, scope=scope)

    assert should_suppress is False
    stored = _stored(storage, session)
    assert stored.runs == []
    assert stored.summary is None
    assert read_scope_seen_event_ids(storage, stored) == set()


@pytest.mark.asyncio
async def test_active_ad_hoc_team_redaction_uses_pending_response_scope(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A team scope recorded before generation drops the redacted source when it next opens."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="team", scope_id="team_private")
    session = ConversationSession(
        kind="team",
        session_id=target.session_id,
        owner_id=scope.scope_id,
        runs=[
            TeamRunRecord(
                run_id="team-run",
                session_id=target.session_id,
                team_id=scope.scope_id,
                metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$user_msg"},
            ),
        ],
    )
    storage = _seeded_storage_with_runs(tmp_path, session, name="team_private")
    store = await _store_with_storage(journal_store, storage)
    response_record = TurnRecord.create(
        ["$user_msg"],
        requester_id="@user:example.org",
        response_owner="agent",
        history_scope=scope,
        conversation_target=target,
    )
    await store.record_pending_turn(response_record)
    await _redact_through(store, "$user_msg", "!room:example.org")
    await store.record_turn(replace(response_record, response_event_id="$reply"))

    should_suppress = await _suppressed(store, target, ("$later",))
    await remove_redacted_history_like_next_response(store, target, storage, scope=scope)

    assert should_suppress is False
    assert _stored_run_ids(storage, session) == []


@pytest.mark.asyncio
async def test_redaction_sanitizes_coalesced_turn_prompt(journal_store: EventJournalStore) -> None:
    """Sibling edit regeneration cannot recover a redacted coalesced prompt; attribution stays to keep it deleted."""
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

    await _redact_through(store, "$first", "!room:example.org")

    sanitized = await store.get_turn_record("$second")
    assert sanitized is not None
    assert sanitized.source_event_prompts == {"$second": "keep"}
    assert await store.deleted_sources(sanitized) == {"$first"}
    assert await store.get_turn_record("$first") == sanitized


@pytest.mark.asyncio
async def test_multi_bot_redaction_only_changes_the_history_that_read_the_source(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """Each bot drops only the history that derives from the redacted source."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="agent", scope_id="agent")
    owner_session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[],
        summary=ScopeSummary(text="contains REDACTED_SECRET", updated_at=_EPOCH),
    )
    owner_session.metadata = _with_legacy_seen_event_ids(owner_session, {"$user_msg"})
    owner_storage = _seeded_storage(tmp_path, owner_session, name="owner")
    owner_store = await _store_with_storage(
        journal_store,
        owner_storage,
        agent_name="owner",
    )
    await owner_store.record_turn(
        replace(
            _owned_turn_record(target),
            response_owner="owner",
        ),
    )
    unrelated_session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="unrelated",
        runs=[],
        summary=ScopeSummary(text="unrelated", updated_at=_EPOCH),
    )
    unrelated_storage = _seeded_storage(tmp_path, unrelated_session, name="unrelated")
    unrelated_store = await _store_with_storage(
        journal_store,
        unrelated_storage,
        agent_name="unrelated",
    )

    await admit_room_event(journal_store.principal("agent@alice"), "!room:example.org", "$user_msg")
    await _redact_through(owner_store, "$user_msg", "!room:example.org")
    await _redact_through(unrelated_store, "$user_msg", "!room:example.org")

    await _suppressed(owner_store, target, ("$later",))
    await remove_redacted_history_like_next_response(owner_store, target, owner_storage, scope=scope)
    should_suppress = await _suppressed(unrelated_store, target, ("$later",))
    await remove_redacted_history_like_next_response(
        unrelated_store,
        target,
        unrelated_storage,
        scope=HistoryScope(kind="agent", scope_id="unrelated"),
    )

    assert should_suppress is False
    stored_owner = _stored(owner_storage, owner_session)
    assert stored_owner.summary is None
    assert read_scope_seen_event_ids(owner_storage, stored_owner) == set()
    assert _stored(unrelated_storage, unrelated_session).summary is not None


@pytest.mark.asyncio
async def test_a_recorded_turn_is_answered_for_a_store_opened_later(journal_store: EventJournalStore) -> None:
    """A turn answered through one store reads answered through any later store over the same database."""
    store = await _store(journal_store)

    await store.record_turn(_in_room(TurnRecord.create(["$event"], response_event_id="$response")))

    reloaded_store = await _store(journal_store)

    assert await reloaded_store.is_handled(ROOM, "$event")
    turn_record = await reloaded_store.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"


@pytest.mark.asyncio
async def test_redaction_persists_for_a_store_opened_later(journal_store: EventJournalStore) -> None:
    """A redaction recorded once stays in force for every later store, sibling sources included."""
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

    await _redact_through(store, "$event", "!room:example.org")
    reloaded_store = await _store(journal_store)

    reloaded_record = await reloaded_store.get_turn_record("$event")
    assert reloaded_record is not None
    assert await reloaded_store.deleted_sources(reloaded_record) == {"$event"}
    assert reloaded_record.source_event_prompts == {"$sibling": "keep"}
    assert await reloaded_store.is_handled(target.room_id, "$event") is True
    assert await reloaded_store.is_handled(target.room_id, "$sibling") is True


@pytest.mark.asyncio
async def test_redaction_cleanup_survives_restart_until_next_response(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """A ledger tombstone alone still lets the next response after a restart remove the replay."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="run-1",
                agent_id="agent",
                session_id=target.session_id,
                metadata={"matrix_event_id": "$user_msg"},
            ),
        ],
    )
    storage = _seeded_storage_with_runs(tmp_path, session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))
    await _redact_through(store, "$user_msg", "!room:example.org")
    restarted_store = await _store_with_storage(journal_store, storage)

    assert _stored_run_ids(storage, session) == ["run-1"]
    restarted_record = await restarted_store.get_turn_record("$user_msg")
    assert restarted_record is not None
    assert await restarted_store.deleted_sources(restarted_record) == {"$user_msg"}
    assert await _suppressed(restarted_store, target, ("$later",)) is False
    await remove_redacted_history_like_next_response(restarted_store, target, storage)
    assert _stored_run_ids(storage, session) == []


@pytest.mark.asyncio
async def test_next_response_removes_replay_of_a_redacted_source(
    journal_store: EventJournalStore,
    tmp_path: Path,
) -> None:
    """The next response's history scope drops the run that answered a redacted source."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            RunRecord(
                run_id="run-1",
                agent_id="agent",
                session_id=target.session_id,
                metadata={"matrix_event_id": "$user_msg"},
            ),
        ],
    )
    storage = _seeded_storage_with_runs(tmp_path, session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_turn(_owned_turn_record(target))
    await _redact_through(store, "$user_msg", "!room:example.org")

    should_suppress = await _suppressed(store, target, ("$later",))
    await remove_redacted_history_like_next_response(store, target, storage)

    assert should_suppress is False
    assert _stored_run_ids(storage, session) == []


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


@pytest.mark.asyncio
async def test_build_run_metadata_normalizes_discovery_aliases(journal_store: EventJournalStore) -> None:
    """Run metadata names a turn's sources and its discovery aliases, normalized like its source IDs."""
    store = await _store(journal_store)
    turn_record = TurnRecord.create(
        ["$first", "$anchor"],
        discovery_event_ids=("", "$first", "$selection", "$selection"),
    )

    metadata = store.build_run_metadata(turn_record)

    assert metadata == {
        constants.MATRIX_SOURCE_EVENT_IDS_METADATA_KEY: ["$first", "$anchor"],
        constants.MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY: ["$selection"],
    }


@pytest.mark.asyncio
async def test_routed_alias_redaction_deletes_the_owning_relay(journal_store: EventJournalStore) -> None:
    """Deleting the message a relay answers for deletes the relay source and its text, not its sibling."""
    store = await _store(journal_store)
    await store.record_turn(
        _in_room(
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
        ),
    )

    await admit_room_event(journal_store.principal("agent@alice"), ROOM, "$human")
    await _redact_through(store, "$human")

    record = await store.get_turn_record("$relay")
    assert record.source_event_prompts == {"$anchor": "keep"}
    assert await store.deleted_sources(record) == {"$relay"}


@pytest.mark.asyncio
async def test_a_later_answer_replaces_the_response_it_records(journal_store: EventJournalStore) -> None:
    """A responded write names its own response, even over one an earlier answer recorded."""
    store = await _store(journal_store)
    await store.record_responded_turn(_in_room(TurnRecord.create(["$event"], response_event_id="$old-response")))

    await store.record_responded_turn(_in_room(TurnRecord.create(["$event"], response_event_id="$new-response")))

    updated = await store.get_turn_record("$event")
    assert updated is not None
    assert updated.response_event_id == "$new-response"


@pytest.mark.asyncio
async def test_terminal_turn_can_absorb_a_source_that_only_carries_an_echo(journal_store: EventJournalStore) -> None:
    """A source tracked only for its visible echo joins the coalesced turn that completes it, keeping its echo."""
    store = await _store(journal_store)
    await store.record_visible_echo(ROOM, "$second", "$echo")

    await store.record_turn(_in_room(TurnRecord.create(["$first", "$second"], response_event_id="$response")))

    first_record = await store.get_turn_record("$first")
    second_record = await store.get_turn_record("$second")
    assert first_record is not None
    assert first_record == second_record
    assert first_record.source_event_ids == ("$first", "$second")
    assert await store.visible_echo("$second") == SourceVisibleEcho("$echo")


@pytest.mark.asyncio
async def test_visible_echo_is_finalized_only_after_replacement_acknowledgement(
    journal_store: EventJournalStore,
) -> None:
    """A posted placeholder is finalized only once its edit succeeds, and never becomes the turn's answer."""
    store = await _store(journal_store)
    await store.record_visible_echo(ROOM, "$event", "$echo")

    assert await store.finalized_visible_echoes(("$event",)) == ()

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=False)

    record = await store.get_turn_record("$event")
    assert record is not None
    assert not record.completed
    assert record.response_event_id is None
    assert await store.finalized_visible_echoes(("$event",)) == ("$echo",)
    assert await store.visible_echo("$event") == SourceVisibleEcho("$echo", is_fallback=False)


@pytest.mark.asyncio
async def test_visible_echo_finalization_requires_tracked_placeholder(journal_store: EventJournalStore) -> None:
    """An acknowledgement alone must not create a finalized visible echo."""
    store = await _store(journal_store)

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=False)

    assert await store.get_turn_record("$event") is None


@pytest.mark.asyncio
async def test_visible_echo_finalization_cannot_overwrite_terminal_outcome(journal_store: EventJournalStore) -> None:
    """A late edit acknowledgement preserves a concurrently completed turn."""
    store = await _store(journal_store)
    await store.record_visible_echo(ROOM, "$event", "$echo")
    await store.record_turn(_in_room(TurnRecord.create(["$event"], response_event_id="$response")))

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=False)

    record = await store.get_turn_record("$event")
    assert record is not None
    assert record.completed
    assert record.response_event_id == "$response"
    assert await store.visible_echo("$event") == SourceVisibleEcho("$echo", is_fallback=False)


@pytest.mark.asyncio
async def test_finalized_visible_echo_keeps_transcript_over_fallback(journal_store: EventJournalStore) -> None:
    """Transcript replacement upgrades a fallback and rejects a later downgrade, across a restart too."""
    store = await _store(journal_store)
    await store.record_visible_echo(ROOM, "$event", "$echo")

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=True)
    assert await store.visible_echo("$event") == SourceVisibleEcho("$echo", is_fallback=True)

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=False)
    transcript = SourceVisibleEcho("$echo", is_fallback=False)
    assert await store.visible_echo("$event") == transcript

    await store.record_finalized_visible_echo("$event", "$echo", is_fallback=True)
    assert await store.visible_echo("$event") == transcript
    assert await (await _store(journal_store)).visible_echo("$event") == transcript


@pytest.mark.asyncio
async def test_terminal_turn_rejects_conflicting_completed_canonical_source(journal_store: EventJournalStore) -> None:
    """A completed source cannot be reassigned into a different turn."""
    store = await _store(journal_store)
    await store.record_turn(_in_room(TurnRecord.create(["$first"], response_event_id="$first-response")))

    await store.record_turn(_in_room(TurnRecord.create(["$first", "$second"], response_event_id="$other-response")))

    first_record = await store.get_turn_record("$first")
    assert first_record is not None
    assert first_record.source_event_ids == ("$first",)
    assert first_record.response_event_id == "$first-response"
    assert await store.get_turn_record("$second") is None


@pytest.mark.asyncio
async def test_record_turn_keeps_facts_a_later_write_leaves_out(journal_store: EventJournalStore) -> None:
    """A repeated write of one turn keeps the context it does not name and takes the response it does."""
    store = await _store(journal_store)
    await store.record_responded_turn(
        _in_room(
            TurnRecord.create(
                ["$event"],
                response_event_id="$first-response",
                requester_id="@user:example.org",
            ),
        ),
    )

    await store.record_responded_turn(
        _in_room(TurnRecord(source_event_ids=("$event",), response_event_id="$second-response")),
    )

    record = await store.get_turn_record("$event")
    assert record is not None
    assert record.response_event_id == "$second-response"
    assert record.requester_id == "@user:example.org"


@pytest.mark.asyncio
async def test_visible_echo_cannot_overwrite_concurrent_terminal_outcome(journal_store: EventJournalStore) -> None:
    """An echo write racing a terminal write leaves the turn answered with its echo, before and after a restart."""
    store = await _store(journal_store)
    terminal_record = _in_room(TurnRecord.create(["$event"], response_event_id="$response"))

    await asyncio.gather(store.record_visible_echo(ROOM, "$event", "$echo"), store.record_turn(terminal_record))

    for current in (store, await _store(journal_store)):
        record = await current.get_turn_record("$event")
        assert record is not None
        assert record.completed
        assert record.response_event_id == "$response"
        assert await current.visible_echo("$event") == SourceVisibleEcho("$echo")


@pytest.mark.asyncio
async def test_record_responded_turn_rejects_empty_response_event_id(journal_store: EventJournalStore) -> None:
    """The durable response boundary rejects a noncanonical empty event ID."""
    store = await _store(journal_store)
    noncanonical_record = _in_room(TurnRecord(source_event_ids=("$source",), response_event_id=""))

    with pytest.raises(ValueError, match="requires a visible Matrix response event ID"):
        await store.record_responded_turn(noncanonical_record)


def test_agent_bot_does_not_expose_removed_handled_turn_ledger_shim(tmp_path: Path) -> None:
    """AgentBot instances should route handled-turn state only through TurnStore."""
    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    bot = make_test_agent_bot(
        agent_user=AgentMatrixUser(
            agent_name="agent",
            user_id="@mindroom_agent:localhost",
            display_name="Agent",
            password=TEST_PASSWORD,
        ),
        config=config,
        runtime_paths=runtime_paths_for(config),
    )

    removed_attr = "_handled" + "_turn_ledger"
    assert removed_attr not in AgentBot.__dict__
    assert not hasattr(bot, removed_attr)
    assert removed_attr not in vars(bot)


@pytest.mark.asyncio
async def test_router_turn_replay_uses_persisted_turns_across_two_restarts(
    tmp_path: Path,
    journal_store: EventJournalStore,
) -> None:
    """Router relay turns have durable turn state but no run storage."""

    async def router_store() -> TurnStore:
        config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
        store = TurnStore(
            TurnStoreDeps(
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
                tool_runtime=MagicMock(),
                response_recovery_state=AsyncMock(),
                retry_dispatch_sources=MagicMock(),
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
        source_event_metadata={"$source": SourceEventMetadata("@user:localhost")},
    )
    await (await router_store()).record_turn(expected)

    for _restart in range(2):
        restarted = await router_store()
        loaded = await restarted.get_turn_record_after_writes("$source")

        assert loaded is not None
        assert loaded.turn_key is not None
        assert replace(loaded, turn_key=None) == expected


def test_no_test_references_removed_bot_handled_turn_ledger_shim() -> None:
    """Tests should route all handled-turn access through TurnStore."""
    tests_root = Path(__file__).resolve().parent
    needle = "._handled" + "_turn_ledger"
    offenders = [
        path.relative_to(tests_root).as_posix() for path in tests_root.rglob("*.py") if needle in path.read_text()
    ]

    assert offenders == []


def _next_request(storage: ConversationStore, session_id: str, scope: HistoryScope, prompt: str) -> str:
    """Return the message contents the scope's next reply sends: its summary, the replayed history, then ``prompt``."""
    session = storage.load_session(session_id)
    assert session is not None
    history = scope_history_messages(
        session.runs,
        scope=scope,
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    messages = build_request(
        summary=session.summary,
        history=history,
        turn=TurnMessages(prompt=Message(role="user", content=prompt)),
        media=MediaInputs(),
    )
    return str([message.content for message in messages])


@pytest.mark.asyncio
@pytest.mark.parametrize("compacted", [False, True])
@pytest.mark.parametrize("context_only", [False, True])
async def test_deleted_edit_cannot_enter_reopened_model_history(  # noqa: PLR0915
    journal_store: EventJournalStore,
    tmp_path: Path,
    compacted: bool,
    context_only: bool,
) -> None:
    """Exact edit deletion removes durable causal replay without deleting its root."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="agent", scope_id="agent")
    store = await _store(journal_store)
    original = replace(
        _owned_turn_record(target),
        source_event_prompts={"$user_msg": "CURRENT_SOURCE" if context_only else "DELETED_EDIT_MARKER"},
        source_event_revisions=None if context_only else {"$user_msg": (10, "$physical-edit")},
    )
    if context_only:
        # The reply that consumes the edited context is still being prepared; it records its answer below.
        await store.record_pending_turn(replace(original, completed=False))
    else:
        await store.record_turn(original)

    def storage_factory(*_args: object, **_kwargs: object) -> ConversationStore:
        return create_agent_state_storage("agent", tmp_path)

    store.deps.state_writer.create_storage.side_effect = storage_factory
    store.deps.state_writer.history_scope.return_value = scope
    edited = RunRecord(
        run_id="edited",
        agent_id="agent",
        session_id=target.session_id,
        messages=[Message(role="user", content="DELETED_EDIT_MARKER")],
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: "$user_msg",
            constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY: {"$user_msg": [10, "$physical-edit"]},
        },
    )
    visible = replace(
        make_visible_message(event_id="$context-source", body="DELETED_EDIT_MARKER", timestamp=1),
        latest_event_id="$physical-edit",
        edited_timestamp=10,
    )
    if context_only:
        # The agent is no personal agent with contacts, so its turns are no contact turns.
        runner = runner_for(MagicMock(agent_name="agent", runtime=MagicMock(config=Config())))
        request = ResponseRequest(
            sources=ResponseSources(
                pending_event_ids=("$user_msg",),
                logical_source_event_ids=("$user_msg",),
            ),
            prompt="CURRENT_SOURCE",
            thread_history=[],
            user_id="@user:example.org",
            response_envelope=request_envelope(
                room_id=target.room_id,
                thread_id=target.resolved_thread_id,
                reply_to_event_id="$user_msg",
                user_id="@user:example.org",
                agent_name="agent",
            ),
            prepare_source_turn=lambda history: store.prepare_pending_response_source(
                target=target,
                source_event_ids=("$user_msg",),
                thread_history=history,
            ),
        )
        lifecycle_lock = runner.lifecycle_coordinator._response_lifecycle_lock(target)
        await lifecycle_lock.acquire()
        waiting = asyncio.Event()

        async def prepare_locked() -> ResponseRequest | SourceFate:
            waiting.set()
            async with lifecycle_lock:
                return await runner._begin_locked_turn(request, resolved_target=target, history_scope=scope)

        async def claim_without_reply(claimed: ResponseRequest, **_kwargs: object) -> ResponseRequest:
            # No reply record backs this preparation: the turn proceeds as one whose claim took.
            return claimed

        with (
            patch.object(LockedTurns, "request_remains_authorized", AsyncMock(return_value=True)),
            patch.object(runner, "_claim_reply_span", side_effect=claim_without_reply),
        ):
            task = asyncio.create_task(prepare_locked())
            await waiting.wait()
            runner.deps.resolver.fetch_thread_history = AsyncMock(
                return_value=ThreadHistoryResult([visible], is_full_history=True),
            )
            lifecycle_lock.release()
            prepared = await task
        assert isinstance(prepared, ResponseRequest)
        assert prepared.thread_history == [visible]
        assert await store.get_turn_record("$context-source") is None
        context_owner = await store.get_turn_record("$user_msg")
        assert context_owner is not None
        registered_before_consumption = "$physical-edit" in (context_owner.revision_replay or {})
        turn, consumed = build_unseen_context_messages_for_test(
            "current",
            prepared.thread_history,
            seen_event_ids=set(),
            current_event_id="$user_msg",
            active_event_ids=(),
            response_sender_id=None,
            config=Config(),
        )
        edited.messages = list(turn.in_request_order())
        edited.metadata = {constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: consumed}
        await store.record_turn(original)
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        runs=[
            edited,
            RunRecord(
                run_id="dependent",
                agent_id="agent",
                session_id=target.session_id,
                messages=[Message(role="assistant", content="dependent answer")],
            ),
        ],
    )
    if compacted:
        session.metadata = _with_legacy_seen_event_ids(session, set(_legacy_compacted_event_ids(edited)))
        session.summary = ScopeSummary(text="DELETED_EDIT_MARKER", updated_at=_EPOCH)
        session.runs = session.runs[1:]
    write_agno_database(AgnoTableNames("agent").database_path(tmp_path), session, usage=False)
    storage = storage_factory()
    initial_persisted = storage.load_session(target.session_id)
    assert initial_persisted is not None
    if compacted:
        assert initial_persisted.summary is not None
        assert initial_persisted.summary.text == "DELETED_EDIT_MARKER"
    else:
        assert "DELETED_EDIT_MARKER" in str([run.messages for run in initial_persisted.runs or []])
    assert "DELETED_EDIT_MARKER" in _next_request(storage, target.session_id, scope, "BASELINE_INPUT")
    await _redact_through(store, "$physical-edit", "!room:example.org")
    reopened = TurnStore(store.deps)
    await reopened.warm()
    assert not await _suppressed(reopened, target, ("$next",))
    storage = storage_factory()
    await remove_redacted_history_like_next_response(reopened, target, storage, scope=scope)
    persisted = storage.load_session(target.session_id)
    assert persisted is not None
    next_request = _next_request(storage, target.session_id, scope, "NEXT_SURVIVING_INPUT")
    assert "NEXT_SURVIVING_INPUT" in next_request
    assert "DELETED_EDIT_MARKER" not in next_request
    if context_only:
        assert registered_before_consumption
        assert await reopened.get_turn_record("$context-source") is None
    owner = await reopened.get_turn_record("$user_msg")
    assert owner is not None
    assert owner.completed
    assert owner.response_event_id == "$reply"
    assert await reopened.deleted_sources(owner) == frozenset()
    assert owner.source_event_prompts == ({"$user_msg": "CURRENT_SOURCE"} if context_only else None)


@pytest.mark.asyncio
async def test_completed_root_retains_unsettled_physical_edit(journal_store: EventJournalStore) -> None:
    """Retention keeps a turn whose recorded edit the journal still owes, without answering the edit."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    await store.record_turn(_owned_turn_record(target))
    owner = await store.get_turn_record_after_writes("$user_msg")
    assert owner is not None
    await store.register_edit_revision(owner, "$user_msg", (10, "$unsettled-edit"))
    with patch("mindroom.turn_store.time.time", return_value=time.time() + 40 * 86400):
        await store.cleanup(unsettled_source_event_ids=("$unsettled-edit",))
    assert await store.get_turn_record("$user_msg") is not None
    assert await store.get_turn_record("$unsettled-edit") is None


@pytest.mark.asyncio
async def test_late_completed_edit_keeps_consumption_proof_without_restoring_text(
    journal_store: EventJournalStore,
) -> None:
    """A request started before deletion may finish afterward with immutable attribution."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    await store.record_turn(_owned_turn_record(target))
    loaded = await store.get_turn_record_after_writes("$user_msg")
    await store.register_edit_revision(loaded, "$user_msg", (10, "$physical-edit"))
    snapshot = replace(
        await store.get_turn_record("$user_msg"),
        source_event_prompts={"$user_msg": "DELETED_EDIT_MARKER"},
        source_event_revisions={"$user_msg": (10, "$physical-edit")},
    )
    await _redact_through(store, "$physical-edit", "!room:example.org")
    await store.record_responded_turn(snapshot)
    owner = await store.get_turn_record("$user_msg")
    assert owner is not None
    assert owner.revision_replay["$physical-edit"].response_event_id == "$reply"
    assert await store.redacted_event_ids(target.room_id, ("$physical-edit",)) == {"$physical-edit"}
    assert not owner.source_event_prompts
    await _suppressed(store, target, ("$next",))
    assert await store.record_pending_turn(snapshot) is None
    await store.record_responded_turn(snapshot)
    owner = await store.get_turn_record("$user_msg")
    assert not owner.source_event_prompts


@pytest.mark.asyncio
@pytest.mark.parametrize("compacted", [False, True])
async def test_deleted_noncurrent_edit_preserves_independent_surviving_run(
    journal_store: EventJournalStore,
    tmp_path: Path,
    compacted: bool,
) -> None:
    """A superseded edit removes only runs that actually consumed that physical revision."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    newest = RunRecord(
        run_id="newest",
        agent_id="agent",
        session_id=target.session_id,
        metadata={
            constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY: {"$user_msg": [20, "$surviving-edit"]},
        },
    )
    session = ConversationSession(kind="agent", session_id=target.session_id, owner_id="agent", runs=[newest])
    if compacted:
        session.summary = ScopeSummary(text="Independent surviving summary", updated_at=_EPOCH)
        session.metadata = _with_legacy_seen_event_ids(session, {"$user_msg", "$surviving-edit"})
    storage = _seeded_storage_with_runs(tmp_path, session)
    store = await _store_with_storage(journal_store, storage)
    await store.record_responded_turn(
        replace(
            _owned_turn_record(target),
            source_event_prompts={"$user_msg": "deleted older"},
            source_event_revisions={"$user_msg": (10, "$physical-edit")},
        ),
    )
    await store.record_responded_turn(
        replace(
            _owned_turn_record(target),
            source_event_prompts={"$user_msg": "SURVIVING_EDIT"},
            source_event_revisions={"$user_msg": (20, "$surviving-edit")},
        ),
    )
    await _redact_through(store, "$physical-edit", "!room:example.org")
    await _suppressed(store, target, ("$next",))
    stored = _stored(storage, session)
    assert [run.run_id for run in stored.runs or []] == ["newest"]
    if compacted:
        assert stored.summary is not None
        assert stored.summary.text == "Independent surviving summary"
    owner = await store.get_turn_record("$user_msg")
    assert owner.source_event_prompts == {"$user_msg": "SURVIVING_EDIT"}
    assert owner.source_event_revisions == {"$user_msg": (20, "$surviving-edit")}
    assert owner.revision_replay["$physical-edit"].response_event_id == "$reply"


@pytest.mark.asyncio
async def test_newer_registration_during_refill_invalidates_older_selected_snapshot(
    journal_store: EventJournalStore,
) -> None:
    """A refreshed owner watermark does not certify an older captured edit body."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    await store.record_turn(_owned_turn_record(target))
    loaded = await store.get_turn_record_after_writes("$user_msg")
    await store.register_edit_revision(loaded, "$user_msg", (10, "$older"))
    newer = await store.register_edit_revision(loaded, "$user_msg", (20, "$newer"))
    assert newer is not None
    snapshot = replace(
        newer,
        source_event_prompts={"$user_msg": "STALE_BODY"},
        source_event_revisions={"$user_msg": (10, "$older")},
    )
    result = await store.prepare_edit_snapshot(
        record=snapshot,
        live_source_event_ids=("$user_msg",),
        driving_revision_id="$older",
    )
    assert result is True


@pytest.mark.asyncio
@pytest.mark.parametrize("registration_order", ["older_first", "newer_first"])
async def test_edits_of_two_messages_of_one_turn_let_the_newer_edit_regenerate(
    journal_store: EventJournalStore,
    registration_order: str,
) -> None:
    """Two messages of one coalesced turn edited before either regeneration claims: the newer edit still runs.

    Each edit selected only its own text, so the older one yields to the newer
    one; neither rejecting the other leaves the reply unregenerated.
    """
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    await store.record_responded_turn(
        replace(
            _owned_turn_record(target),
            source_event_ids=("$first", "$user_msg"),
            source_event_prompts={"$first": "FIRST", "$user_msg": "SECOND"},
        ),
    )
    loaded = await store.get_turn_record_after_writes("$user_msg")
    older_revision, newer_revision = (10, "$first-edit"), (20, "$second-edit")
    if registration_order == "older_first":
        older = await store.register_edit_revision(loaded, "$first", older_revision)
        newer = await store.register_edit_revision(loaded, "$user_msg", newer_revision)
    else:
        newer = await store.register_edit_revision(loaded, "$user_msg", newer_revision)
        older = await store.register_edit_revision(loaded, "$first", older_revision)
    assert older is not None
    assert newer is not None
    older_snapshot = replace(
        older,
        source_event_prompts={"$first": "FIRST_EDIT", "$user_msg": "SECOND"},
        source_event_revisions={"$first": older_revision},
    )
    newer_snapshot = replace(
        newer,
        source_event_prompts={"$first": "FIRST", "$user_msg": "SECOND_EDIT"},
        source_event_revisions={"$user_msg": newer_revision},
    )
    sources = ("$first", "$user_msg")

    assert (
        await store.prepare_edit_snapshot(
            record=older_snapshot,
            live_source_event_ids=sources,
            driving_revision_id="$first-edit",
        )
        is True
    )
    assert (
        await store.prepare_edit_snapshot(
            record=newer_snapshot,
            live_source_event_ids=sources,
            driving_revision_id="$second-edit",
        )
        is False
    )


@pytest.mark.asyncio
async def test_an_older_sibling_edit_registered_after_the_snapshot_does_not_stop_a_newer_edit(
    journal_store: EventJournalStore,
) -> None:
    """Edits that register out of timestamp order still let the newer one regenerate."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    await store.record_responded_turn(
        replace(
            _owned_turn_record(target),
            source_event_ids=("$first", "$user_msg"),
            source_event_prompts={"$first": "FIRST", "$user_msg": "SECOND"},
            source_event_revisions={"$first": (5, "$first-earlier-edit")},
        ),
    )
    loaded = await store.get_turn_record_after_writes("$user_msg")
    newer = await store.register_edit_revision(loaded, "$user_msg", (20, "$second-edit"))
    assert newer is not None
    newer_snapshot = replace(
        newer,
        source_event_prompts={"$first": "FIRST", "$user_msg": "SECOND_EDIT"},
        source_event_revisions={**(newer.source_event_revisions or {}), "$user_msg": (20, "$second-edit")},
    )
    # The older sibling edit registers only after the newer snapshot was built.
    assert await store.register_edit_revision(loaded, "$first", (10, "$first-edit")) is not None

    assert (
        await store.prepare_edit_snapshot(
            record=newer_snapshot,
            live_source_event_ids=("$first", "$user_msg"),
            driving_revision_id="$second-edit",
        )
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["driver", "sibling", "newer"])
async def test_edit_snapshot_rechecks_what_changed_since_it_was_built(
    journal_store: EventJournalStore,
    mutation: str,
) -> None:
    """A redaction of the edit or a sibling's selected text, or a newer edit, since the snapshot stops it."""
    store = await _store(journal_store)
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    record = replace(
        _owned_turn_record(target),
        source_event_ids=("$sibling", "$user_msg"),
        source_event_prompts={"$sibling": "SIBLING_EDIT", "$user_msg": "DRIVING_EDIT"},
        source_event_revisions={"$sibling": (10, "$sibling-edit"), "$user_msg": (20, "$driving-edit")},
    )
    await store.record_responded_turn(record)
    snapshot = await store.get_turn_record_after_writes("$user_msg")
    assert snapshot is not None
    if mutation == "newer":
        await store.register_edit_revision(snapshot, "$user_msg", (30, "$newer-edit"))
    else:
        await _redact_through(store, "$driving-edit" if mutation == "driver" else "$sibling-edit")

    result = await store.prepare_edit_snapshot(
        record=snapshot,
        live_source_event_ids=("$sibling", "$user_msg"),
        driving_revision_id="$driving-edit",
    )

    assert result is True
    owner = await store.get_turn_record("$user_msg")
    assert owner is not None
    assert owner.completed
    assert owner.response_event_id == "$reply"
    assert await store.deleted_sources(owner) == frozenset()
    if mutation == "newer":
        assert owner.revision_replay["$newer-edit"].response_event_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("historical_summary_owner", "later_run_names_revision"),
    [
        pytest.param(True, False, id="historical-summary-only"),
        pytest.param(True, True, id="historical-summary-and-later-run"),
        pytest.param(False, False, id="modern-per-revision"),
    ],
)
async def test_legacy_compacted_revision_uses_retained_owner_on_cold_reopen(
    journal_store: EventJournalStore,
    tmp_path: Path,
    historical_summary_owner: bool,
    later_run_names_revision: bool,
) -> None:
    """Only legacy summaries lacking physical IDs use the source the conversion recorded for their revision."""
    target = MessageTarget.resolve("!room:example.org", "$thread", "$user_msg")
    scope = HistoryScope(kind="agent", scope_id="agent")
    original = replace(
        _owned_turn_record(target),
        source_event_prompts={"$user_msg": "DELETED_EDIT_MARKER"},
        source_event_revisions={"$user_msg": (10, "$physical-edit")},
        revision_replay=(
            None
            if historical_summary_owner
            else {"$physical-edit": RevisionReplay("$user_msg", 10, response_event_id="$reply")}
        ),
    )
    store = await _store(journal_store)
    await store.record_turn(original)
    if historical_summary_owner:
        # What the conversion of a pre-v2026.9.43 record left beside its turn.
        await journal_store.backend.write(
            lambda transaction: transaction.execute(
                "INSERT INTO legacy_summary_revisions (agent_name, room_id, revision_id, source_event_id) "
                "VALUES (?, ?, ?, ?)",
                ("agent", target.room_id, "$physical-edit", "$user_msg"),
            ),
        )

    def storage_factory(*_args: object, **_kwargs: object) -> ConversationStore:
        return create_agent_state_storage("agent", tmp_path)

    later_run = RunRecord(
        run_id="later",
        agent_id="agent",
        session_id=target.session_id,
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: "$later",
            constants.MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY: {"$user_msg": [10, "$physical-edit"]},
        },
    )
    session = ConversationSession(
        kind="agent",
        session_id=target.session_id,
        owner_id="agent",
        summary=ScopeSummary(text="DELETED_EDIT_MARKER", updated_at=_EPOCH),
        runs=[later_run] if later_run_names_revision else [],
    )
    session.metadata = _with_legacy_seen_event_ids(session, {"$user_msg"})
    write_agno_database(AgnoTableNames("agent").database_path(tmp_path), session, usage=False)
    if later_run_names_revision:
        # A modern compaction folds the later run on top of the legacy summary.
        storage = storage_factory()
        reopened_session = storage.load_session(target.session_id)
        assert reopened_session is not None
        compact_runs(
            storage,
            reopened_session,
            [run.run_id for run in reopened_session.runs or [] if run.run_id],
            "DELETED_EDIT_MARKER and later",
        )
    store = await _store(journal_store)
    store.deps.state_writer.create_storage.side_effect = storage_factory
    store.deps.state_writer.history_scope.return_value = scope
    await _redact_through(store, "$physical-edit", "!room:example.org")
    await _suppressed(store, target, ("$next",))
    storage = storage_factory()
    await remove_redacted_history_like_next_response(store, target, storage, scope=scope)
    persisted = storage.load_session(target.session_id)
    assert persisted is not None
    if historical_summary_owner:
        assert persisted.summary is None
    else:
        assert persisted.summary is not None
        assert persisted.summary.text == "DELETED_EDIT_MARKER"
    owner = await store.get_turn_record("$user_msg")
    assert owner is not None
    assert await store.deleted_sources(owner) == frozenset()
    assert await store.redacted_history_events(target, ("$user_msg",)) == (
        {"$physical-edit": "$user_msg"} if historical_summary_owner else {}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("coalescing_thread_id", [None, "$coalescing-thread"])
async def test_prepared_voice_survives_cold_load_and_keeps_first_content(
    journal_store: EventJournalStore,
    coalescing_thread_id: str | None,
) -> None:
    """A replay checkpoint freezes nested content and survives both journal backends."""
    store = await _store(journal_store)
    content = {"body": "Transcript", "m.mentions": {"user_ids": ["@alice:example.org"]}, "attachments": [{"id": "one"}]}
    snapshot = PreparedVoiceSource.from_content(
        "Transcript",
        content,
        thread_id="$thread",
        coalescing_thread_id=coalescing_thread_id,
    )
    content["m.mentions"]["user_ids"].append("@other:example.org")
    content["attachments"][0]["id"] = "changed"
    assert await store.record_prepared_voice(ROOM, "$voice", snapshot) == snapshot
    later = PreparedVoiceSource.from_content("Wrong transcript", {})
    assert await store.record_prepared_voice(ROOM, "$voice", later) == snapshot
    restored_content = snapshot.to_content()
    restored_content["attachments"][0]["id"] = "also changed"
    restored = await _store(journal_store)
    saved = await restored.prepared_voice_for_source("$voice")
    assert saved == snapshot
    assert saved.to_content() == {
        "body": "Transcript",
        "m.mentions": {"user_ids": ["@alice:example.org"]},
        "attachments": [{"id": "one"}],
    }
    assert saved.thread_id == "$thread"
    assert saved.coalescing_thread_id == coalescing_thread_id
    assert await restored.prepared_voice_for_source("$unknown") is None
    assert "prepared_voice_sources" not in (restored.build_run_metadata(await restored.get_turn_record("$voice")) or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "redacted", "alias_redacted"])
async def test_prepared_voice_dropped_by_terminal_authority(journal_store: EventJournalStore, terminal: str) -> None:
    """Completion and physical or discovery redaction erase checkpoints permanently."""
    store = await _store(journal_store)
    await store.record_pending_turn(
        _in_room(
            TurnRecord.create(
                ["$voice"],
                completed=False,
                discovery_event_ids=["$alias"],
                source_event_metadata={
                    "$voice": SourceEventMetadata("@alice:example.org", discovery_event_id="$alias"),
                },
            ),
        ),
    )
    snapshot = PreparedVoiceSource.from_content("Transcript", {"body": "Transcript"})
    await store.record_prepared_voice(ROOM, "$voice", snapshot)
    if terminal == "completed":
        await store.record_turn(_in_room(TurnRecord.create(["$voice"])))
    else:
        redacted = "$alias" if terminal == "alias_redacted" else "$voice"
        await admit_room_event(journal_store.principal("agent@alice"), "!room:example.org", redacted)
        await _redact_through(store, redacted, "!room:example.org")
    assert await store.prepared_voice_for_source("$voice") is None
    assert await store.record_prepared_voice(ROOM, "$voice", snapshot) is None
    restored = await _store(journal_store)
    assert await restored.prepared_voice_for_source("$voice") is None


@pytest.mark.asyncio
async def test_pending_voice_checkpoint_survives_later_pending_writes(journal_store: EventJournalStore) -> None:
    """Later pending facts cannot replace a saved transcript or drop its sibling's."""
    store = await _store(journal_store)
    first = PreparedVoiceSource.from_content("First", {"body": "First"})
    second = PreparedVoiceSource.from_content("Second", {"body": "Second"})
    await store.record_prepared_voice(ROOM, "$one", first)
    await store.record_pending_turn(_in_room(TurnRecord.create(["$one", "$two"], completed=False)))
    await store.record_prepared_voice(ROOM, "$two", second)
    await store.record_pending_turn(
        _in_room(
            TurnRecord.create(
                ["$one", "$two"],
                completed=False,
                prepared_voice_sources={"$one": second},
            ),
        ),
    )
    restored = await _store(journal_store)
    assert await restored.prepared_voice_for_source("$one") == first
    assert await restored.prepared_voice_for_source("$two") == second


def test_prepared_voice_drops_only_malformed_or_unowned_entries() -> None:
    """One malformed checkpoint cannot poison another live source's, and a foreign one is dropped."""
    record = TurnRecord.create(
        ["$good", "$bad"],
        completed=False,
        prepared_voice_sources={
            "$good": {"body": "Kept", "content_json": '{"body": "Kept"}', "thread_id": "$root"},
            "$bad": {"body": "Bad", "content_json": "[]"},
            "$foreign": {"body": "Foreign", "content_json": "{}"},
        },
    )
    assert set(record.prepared_voice_sources) == {"$good"}
    assert record.prepared_voice_sources["$good"].to_content() == {"body": "Kept"}
    with pytest.raises(ValueError, match="Prepared voice requires"):
        PreparedVoiceSource("Bad", "[]")


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "settled", "command"])
async def test_prepared_voice_checkpoint_retention_follows_unsettled_sources(
    journal_store: EventJournalStore,
    state: str,
) -> None:
    """Only journal-owned checkpoints and genuine unfinished work escape ordinary retention."""
    store = await _store(journal_store)
    snapshot = PreparedVoiceSource.from_content("Transcript", {"body": "Transcript"})
    await store.record_prepared_voice(ROOM, "$voice", snapshot)
    if state == "command":
        await store.record_pending_turn(
            _in_room(TurnRecord.create(["$voice"], completed=False, command_execution_started=True)),
        )
    await store.deps.turn_records.write(
        turn_rows.retain,
        now=time.time() + 1,
        max_age_seconds=0,
        max_turns=0,
        unsettled_event_ids=("$voice",) if state == "pending" else (),
    )
    restored = await _store(journal_store)
    assert (await restored.prepared_voice_for_source("$voice") is not None) == (state == "pending")
    assert (await restored.get_turn_record("$voice") is not None) == (state != "settled")


async def _edited(
    store: TurnStore,
    revision: tuple[int, str],
    text: str,
) -> tuple[TurnRecord, tuple[int, str], str]:
    """Register an edit of ``$source`` and return what its regeneration commits: the turn, revision, and text."""
    loaded = await store.get_turn_record_after_writes("$source")
    assert loaded is not None
    assert await store.register_edit_revision(loaded, "$source", revision) is not None
    return loaded, revision, text


async def _commit(store: TurnStore, edited: tuple[TurnRecord, tuple[int, str], str]) -> None:
    loaded, revision, text = edited
    await store.record_edit(loaded, edited_event_id="$source", prompt=text, revision=revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True])
async def test_an_edit_reaches_its_turn_when_its_regeneration_starts(
    journal_store: EventJournalStore,
    answered: bool,
) -> None:
    """The turn takes the edit's text at once; whether it was answered stays as it was until the answer records it."""
    store = await _store(journal_store)
    original = _in_room(
        TurnRecord.create(["$source"], source_event_prompts={"$source": "original"}, completed=answered),
    )
    if answered:
        await store.record_responded_turn(replace(original, response_event_id="$answer"))
    else:
        await store.record_pending_turn(original)

    await _commit(store, await _edited(store, (20, "$edit"), "edited"))

    record = await (await _store(journal_store)).get_turn_record("$source")
    assert record is not None
    assert record.source_event_prompts == {"$source": "edited"}
    assert record.source_event_revisions == {"$source": (20, "$edit")}
    assert record.completed is answered


@pytest.mark.asyncio
async def test_an_older_edit_does_not_replace_a_newer_one(journal_store: EventJournalStore) -> None:
    """A regeneration of an older edit that starts late leaves the newer edit's text."""
    store = await _store(journal_store)
    await store.record_responded_turn(
        _in_room(
            TurnRecord.create(["$source"], response_event_id="$answer", source_event_prompts={"$source": "original"}),
        ),
    )
    older = await _edited(store, (20, "$older"), "older")
    await _commit(store, await _edited(store, (30, "$newer"), "newer"))

    await _commit(store, older)

    record = await store.get_turn_record("$source")
    assert record is not None
    assert record.source_event_prompts == {"$source": "newer"}
    assert record.source_event_revisions == {"$source": (30, "$newer")}


@pytest.mark.asyncio
@pytest.mark.parametrize("ended_by", ["deletion", "relayed_message_deletion", "relay_answer"])
async def test_a_batch_source_ended_before_any_reply_leaves_its_sibling_owing_an_answer(
    journal_store: EventJournalStore,
    ended_by: str,
) -> None:
    """Deleting S of a pending [S, F] or the message S relays, or a relay answering S, invalidates S alone."""
    store = await _store(journal_store)
    target = MessageTarget.resolve(ROOM, None, "$f")
    relayed = ended_by == "relayed_message_deletion"
    pending = await store.record_pending_turn(
        _in_room(
            TurnRecord.create(
                ["$s", "$f"],
                completed=False,
                requester_id="@user:example.org",
                discovery_event_ids=["$human"] if relayed else (),
                source_event_metadata={
                    "$s": SourceEventMetadata("@user:example.org", discovery_event_id="$human" if relayed else None),
                    "$f": SourceEventMetadata("@user:example.org"),
                },
            ),
        ),
    )
    assert pending is not None
    if ended_by == "deletion":
        await _redact_through(store, "$s")
    elif relayed:
        await _redact_through(store, "$human")
    else:
        await store.record_turn(
            TurnRecord.create(["$relay"], discovery_event_ids=["$s"], response_event_id="$relay-answer"),
            room_id=ROOM,
        )

    preparation = await store.prepare_pending_response_source(
        target=target,
        source_event_ids=pending.record.source_event_ids,
    )

    assert preparation.invalid == ("$s",)
    assert preparation.surviving == ("$f",)
    reopened = await _store(journal_store)
    assert not await reopened.is_handled(ROOM, "$f")


@pytest.mark.asyncio
async def test_another_login_of_the_agent_reads_its_turns_redactions_and_live_claims(
    journal_store: EventJournalStore,
) -> None:
    """Turn truth is the agent's: a re-login or a replacement store reads what the earlier login recorded and holds."""
    first = await _store(journal_store)
    await first.record_turn(_in_room(TurnRecord.create(["$answered"])))
    await _redact(journal_store, "$deleted")
    relogin = TurnStore(
        replace(
            first.deps,
            response_recovery_state=journal_store.principal("agent@bob").response_recovery_state,
        ),
    )
    await relogin.warm()
    claim = await first.claim(_in_room(TurnRecord.create(["$live"], completed=False)))
    assert isinstance(claim, TurnClaim)

    assert await relogin.is_handled(ROOM, "$answered")
    assert await relogin.redacted_event_ids(ROOM, ("$deleted",)) == {"$deleted"}
    assert relogin.has_live_turn_claim("$live")
    assert isinstance(await relogin.claim(_in_room(TurnRecord.create(["$live"], completed=False))), ClaimContended)
    claim.release()
    assert not relogin.has_live_turn_claim("$live")


@pytest.mark.asyncio
async def test_every_finalized_echo_of_a_coalesced_turn_is_known_after_a_restart(
    journal_store: EventJournalStore,
) -> None:
    """A routing turn of two voice messages excludes both echoes when a restart looks for its relay."""
    store = await _store(journal_store)
    for source, echo in (("$voice-1", "$echo-1"), ("$voice-2", "$echo-2")):
        await store.record_visible_echo(ROOM, source, echo)
        await store.record_finalized_visible_echo(source, echo, is_fallback=False)
    assert await store.record_pending_turn(_in_room(TurnRecord.create(["$voice-1", "$voice-2"], completed=False)))

    reopened = await _store(journal_store)

    assert await reopened.finalized_visible_echoes(("$voice-1", "$voice-2")) == ("$echo-1", "$echo-2")
    record = await reopened.get_turn_record("$voice-1")
    assert record is not None
    assert record.response_event_id is None, "an echo never becomes the turn's answer"


@pytest.mark.asyncio
async def test_a_refused_pending_write_hands_its_sources_back_once_its_claim_releases(
    journal_store: EventJournalStore,
) -> None:
    """A batch refused because another turn answered one of its sources retries each source after its claim ends."""
    store = await _store(journal_store)
    batch = _in_room(TurnRecord.create(["$answered", "$fresh"], completed=False))
    claim = await store.claim(batch)
    assert isinstance(claim, TurnClaim)
    await store.record_turn(_in_room(TurnRecord.create(["$answered"], response_event_id="$elsewhere")))

    assert await store.record_pending_turn(batch) is None
    retry = cast("MagicMock", store.deps.retry_dispatch_sources)
    retry.assert_not_called()
    claim.release()

    assert sorted(call.args for call in retry.call_args_list) == [(ROOM, ("$answered",)), (ROOM, ("$fresh",))]
    assert await store.get_turn_record("$fresh") is None
