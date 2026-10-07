"""Released continuations keep the reply identity they answer across the upgrade that names their span."""

import json
from collections.abc import Sequence
from dataclasses import replace
from typing import Any
from unittest.mock import patch

import pytest

from mindroom.event_journal import (
    DeliveryStage,
    EventJournalStore,
    legacy_response_attempts,
    postgres_backend,
    sqlite_backend,
)
from mindroom.event_journal.approval_continuations import SUPERSEDED_FAILURE_REASON
from mindroom.event_journal.approvals import StoredApprovalCard
from mindroom.handled_turns import TurnRecordCodec
from mindroom.history.types import HistoryScope
from mindroom.legacy_reply_messages import LEGACY_PRESENTATIONS
from mindroom.message_target import MessageTarget
from mindroom.reply_lifecycle import ReplyState, SpanKind, SpanOutcome, WakeApproval
from mindroom.turn_record import TurnRecord
from tests.legacy_reply_helpers import store_main_continuation
from tests.test_event_journal_store import TestApprovalContinuations as _ApprovalContinuations
from tests.test_journal_upgrade_boundary import _LegacyDatabase
from tests.test_journal_upgrade_boundary import legacy_database as _legacy_database

legacy_database = _legacy_database

# What v2026.9.137 and earlier wrote: the continuation's room and event in its context.
_CONTEXT_OWNER = """
CREATE TABLE matrix_sync_consumers (
    principal_id TEXT PRIMARY KEY, consumer_generation TEXT NOT NULL,
    stream_id TEXT UNIQUE, next_sequence BIGINT NOT NULL DEFAULT 1
);
INSERT INTO room_membership VALUES ('@bot:example.org', '!room:example.org', 7, 0, 2);
INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender,
    origin_server_ts, source_json, membership_epoch, state) VALUES
    ('@bot:example.org', '$edit', '!room:example.org', '', 'message', '@user:example.org', 1, '{}', 7, 'pending');
INSERT INTO approval_continuations (principal_id, approval_id, entity_name, state, context_json, created_at_ns)
VALUES ('@bot:example.org', 'approval', 'bot', 'waiting',
    '{"run_id":"run","session_id":"session","entity_kind":"agent","room_id":"!room:example.org","thread_id":"$thread","requester_id":"@user:example.org","response_event_id":"$answer","prepared_edit_record":{"anchor_event_id":"$source","source_event_ids":["$source","$second"],"discovery_event_ids":["$alias"],"completed":false,"timestamp":1,"latest_edit_receipt_order":1,"source_event_revisions":{"$source":[1,"$edit"]},"response_event_id":"$answer","response_owner":"bot","conversation_target":{"room_id":"!room:example.org","session_id":"session","source_thread_id":null,"resolved_thread_id":null,"reply_to_event_id":"$source"}}}', 1);
INSERT INTO approval_continuation_sources VALUES ('@bot:example.org', 'approval', '$edit', 0);
"""

# What v2026.10.201 wrote: the continuation's identity in its response attempt tables.
_ATTEMPT_OWNER = """
CREATE TABLE matrix_sync_consumers (
    principal_id TEXT PRIMARY KEY, consumer_generation TEXT NOT NULL,
    stream_id TEXT UNIQUE, next_sequence BIGINT NOT NULL DEFAULT 1
);
CREATE TABLE response_attempts (
    principal_id TEXT NOT NULL, driving_event_id TEXT NOT NULL, entity_name TEXT NOT NULL,
    room_id TEXT NOT NULL, membership_epoch BIGINT NOT NULL, response_event_id TEXT,
    logical_source_key TEXT NOT NULL, selected_receipt_order BIGINT NOT NULL, edit_receipt_order BIGINT,
    PRIMARY KEY (principal_id, driving_event_id)
);
CREATE TABLE response_attempt_sources (
    principal_id TEXT NOT NULL, driving_event_id TEXT NOT NULL, event_id TEXT NOT NULL,
    source_ordinal BIGINT NOT NULL, source_kind TEXT NOT NULL,
    PRIMARY KEY (principal_id, driving_event_id, source_kind, source_ordinal)
);
INSERT INTO room_membership VALUES ('@bot:example.org', '!room:example.org', 7, 0, 2);
INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender,
    origin_server_ts, source_json, membership_epoch, state) VALUES
    ('@bot:example.org', '$first', '!room:example.org', '', 'message', '@user:example.org', 1, '{}', 7, 'pending');
INSERT INTO approval_continuations (principal_id, approval_id, entity_name, state, context_json, created_at_ns)
VALUES ('@bot:example.org', 'approval', 'bot', 'waiting',
    '{"run_id":"run","session_id":"session","entity_kind":"agent","thread_id":null,"requester_id":"@user:example.org"}', 1);
INSERT INTO approval_continuation_sources VALUES ('@bot:example.org', 'approval', '$first', 0);
INSERT INTO response_attempts VALUES
    ('@bot:example.org', '$first', 'bot', '!room:example.org', 7, '$answer', '["$first","$second"]', 1, NULL);
INSERT INTO response_attempt_sources VALUES
    ('@bot:example.org', '$first', '$first', 0, 'logical'),
    ('@bot:example.org', '$first', '$second', 1, 'logical'),
    ('@bot:example.org', '$first', '$alias', 0, 'discovery');
"""


def _table_query(postgres: bool, table: str) -> str:
    return (
        f"SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = '{table}'"  # noqa: S608
        if postgres
        else f"SELECT name FROM sqlite_master WHERE name = '{table}'"  # noqa: S608
    )


@pytest.mark.asyncio
async def test_a_continuation_keeps_the_identity_its_context_held(legacy_database: _LegacyDatabase) -> None:
    """A continuation from before response attempts answers the reply its context named, across reopens."""
    legacy_database.execute(_CONTEXT_OWNER)
    for _ in range(2):
        store = legacy_database.open()
        try:
            approval = await store.principal("@bot:example.org").approval_continuation("approval")
        finally:
            await store.close()
        assert approval is not None
        assert approval.span_id is None
        assert (approval.entity_name, approval.room_id, approval.thread_id, approval.response_event_id) == (
            "bot",
            "!room:example.org",
            "$thread",
            "$answer",
        )
        assert approval.source_event_ids == ("$edit",)
        assert approval.sources.logical_source_event_ids == ("$source", "$second")
        assert approval.sources.discovery_event_ids == ("$alias",)
        assert approval.sources.edit_receipt_order == 1


@pytest.mark.asyncio
async def test_a_continuation_keeps_the_identity_its_response_attempt_held(legacy_database: _LegacyDatabase) -> None:
    """The identity v2026.10.201 kept in response attempts moves onto the continuation, and the tables go."""
    legacy_database.execute(_ATTEMPT_OWNER)
    store = legacy_database.open()
    try:
        approval = await store.principal("@bot:example.org").approval_continuation("approval")
    finally:
        await store.close()
    assert approval is not None
    assert (approval.entity_name, approval.room_id, approval.thread_id, approval.response_event_id) == (
        "bot",
        "!room:example.org",
        None,
        "$answer",
    )
    assert approval.sources.logical_source_event_ids == ("$first", "$second")
    assert approval.sources.discovery_event_ids == ("$alias",)
    assert legacy_database.query(_table_query(legacy_database.postgres, "response_attempts")) == []
    assert legacy_database.query(_table_query(legacy_database.postgres, "response_attempt_sources")) == []
    # Its held sources moved with its identity; reply classification moves them onto its paused span.
    assert approval.source_event_ids == ("$first",)
    assert legacy_database.query(_table_query(legacy_database.postgres, "approval_continuation_sources")) == []
    # Its entity moved too: the paused reply names it once classified.
    assert (
        legacy_database.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'approval_continuations' AND column_name = 'entity_name'"
            if legacy_database.postgres
            else "SELECT name FROM pragma_table_info('approval_continuations') WHERE name = 'entity_name'",
        )
        == []
    )


@pytest.mark.asyncio
async def test_a_claimed_continuation_keeps_its_claim_across_the_upgrade(legacy_database: _LegacyDatabase) -> None:
    """A claim v2026.10.201 stored on the continuation reads as claimed by no running instance until classification."""
    legacy_database.execute(_ATTEMPT_OWNER)
    legacy_database.execute(
        "UPDATE approval_continuations SET state = 'claimed', runtime_generation = 'old-runtime' "
        "WHERE approval_id = 'approval'",
    )
    for _ in range(2):
        store = legacy_database.open()
        try:
            approval = await store.principal("@bot:example.org").approval_continuation("approval")
            pending = await store.principal("@bot:example.org").pending(runtime_generation="new-runtime")
        finally:
            await store.close()
        assert approval is not None
        assert (approval.state, approval.runtime_generation, approval.claim_span_id) == ("claimed", None, None)
        # Recovery owns a claim the stopped instance left.
        assert [event.event_id for event in pending] == ["$first"]
    assert legacy_database.query(
        "SELECT state, runtime_generation FROM approval_continuations WHERE approval_id = 'approval'",
    ) == [("ready", None)]


def test_the_adoption_pages_through_every_continuation(
    legacy_database: _LegacyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each continuation is adopted in bounded pages."""
    legacy_database.execute(_CONTEXT_OWNER)
    legacy_database.execute("""
        INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender,
            origin_server_ts, source_json, membership_epoch, state) VALUES
            ('@bot:example.org', '$A', '!room:example.org', '', 'message', '@user:example.org', 3, '{}', 7, 'pending'),
            ('@bot:example.org', '$z', '!room:example.org', '', 'message', '@user:example.org', 4, '{}', 7, 'pending');
        INSERT INTO approval_continuations (principal_id, approval_id, entity_name, state, context_json, created_at_ns)
        VALUES
            ('@bot:example.org', 'approval-A', 'agent-A', 'waiting',
                '{"room_id":"!room:example.org","response_event_id":"$answer-A"}', 2),
            ('@bot:example.org', 'approval-z', 'agent-z', 'waiting',
                '{"room_id":"!room:example.org","response_event_id":"$answer-z"}', 3);
        INSERT INTO approval_continuation_sources VALUES
            ('@bot:example.org', 'approval-A', '$A', 0),
            ('@bot:example.org', 'approval-z', '$z', 0);
    """)
    module = postgres_backend if legacy_database.postgres else sqlite_backend
    transaction_type = module._PostgresTransaction if legacy_database.postgres else module._SqliteTransaction
    original_fetchall = transaction_type.fetchall
    page_sizes: list[int] = []

    def bounded_fetchall(
        transaction: Any,  # noqa: ANN401
        sql: str,
        params: Sequence[Any] = (),
    ) -> tuple[dict[str, Any], ...]:
        rows = original_fetchall(transaction, sql, params)
        if "FROM approval_continuations" in " ".join(sql.split()) and "LIMIT" in sql:
            page_sizes.append(len(rows))
        return rows

    monkeypatch.setattr(legacy_response_attempts, "_PAGE_SIZE", 2)
    with patch.object(transaction_type, "fetchall", bounded_fetchall):
        legacy_database.open()
    assert page_sizes == [2, 1]
    identities = legacy_database.query(
        "SELECT approval_id, context_json FROM approval_continuations ORDER BY approval_id",
    )
    assert [approval_id for approval_id, context in identities if '"legacy_identity"' in str(context)] == [
        "approval",
        "approval-A",
        "approval-z",
    ]


def test_an_unprovable_identity_rolls_back_the_upgrade(legacy_database: _LegacyDatabase) -> None:
    """A live continuation whose reply cannot be named aborts the whole schema transaction."""
    legacy_database.execute(_CONTEXT_OWNER)
    legacy_database.execute("UPDATE approval_continuations SET context_json = '{}' WHERE approval_id = 'approval'")
    with pytest.raises(ValueError, match="identity"):
        legacy_database.open()
    columns = (
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = 'approval_continuations' AND column_name = 'span_id'"
        if legacy_database.postgres
        else "SELECT name FROM pragma_table_info('approval_continuations') WHERE name = 'span_id'"
    )
    assert legacy_database.query(columns) == []
    assert legacy_database.query("SELECT state FROM journal_events WHERE event_id = '$edit'") == [("pending",)]


@pytest.mark.asyncio
async def test_an_unclassified_continuation_settles_its_adopted_sources(legacy_database: _LegacyDatabase) -> None:
    """A continuation whose entity never classified its reply settles the sources its adopted identity names."""
    legacy_database.execute(_ATTEMPT_OWNER)
    legacy_database.execute(
        f"UPDATE approval_continuations SET state = 'failing', failure_reason = '{SUPERSEDED_FAILURE_REASON}' "  # noqa: S608
        "WHERE approval_id = 'approval'",
    )
    store = legacy_database.open()
    try:
        principal = store.principal("@bot:example.org")
        assert await principal.is_pending("$first")
        assert await principal.finish_approval_continuation("approval") is not None
        assert await principal.approval_continuation("approval") is None
        assert not await principal.is_pending("$first")
    finally:
        await store.close()


# A newer edit's regeneration answered the reply the approval paused, in v2026.10.201.
_NEWER_ANSWER = """
INSERT INTO response_attempts VALUES
    ('@bot:example.org', '$edit', 'bot', '!room:example.org', 7, '$answer', '["$first","$second"]', 2, 5);
INSERT INTO matrix_delivery_outbox (
    principal_id, delivery_id, stage, event_type, room_id, membership_epoch, thread_id, transaction_id,
    payload_json, result_json, edits_event_id, attempted, retired, acknowledged_event_id, created_at_ns
) VALUES ('@bot:example.org', '$edit', 'final', 'm.room.message', '!room:example.org', 7, '', 'edit-transaction',
    '{"body":"* regenerated"}', '{"body":"regenerated"}', '$answer', 1, 0, '$answer-edit', 2);
"""


def _paused_turn_rows() -> str:
    """Return the placeholder row and turn record v2026.10.201 kept for the turn the approval paused."""
    record = TurnRecord.create(
        ["$first", "$second"],
        requester_id="@user:example.org",
        response_event_id="$answer",
        response_owner="bot",
        conversation_target=MessageTarget.resolve("!room:example.org", None, "$first", room_mode=True),
        history_scope=HistoryScope(kind="agent", scope_id="bot"),
    )
    stored = json.dumps(TurnRecordCodec._to_ledger_record(record)).replace("'", "''")
    turns = ",\n".join(
        f"('bot', '{event_id}', '{record.anchor_event_id}', '{stored}')" for event_id in record.indexed_event_ids
    )
    return f"""
INSERT INTO turn_records VALUES {turns};
INSERT INTO matrix_delivery_outbox (
    principal_id, delivery_id, stage, event_type, room_id, membership_epoch, thread_id, transaction_id,
    payload_json, attempted, acknowledged_event_id, created_at_ns
) VALUES ('@bot:example.org', '$first', 'initial', 'm.room.message', '!room:example.org', 7, '', 'first-transaction',
    '{{"body":"Thinking...","io.mindroom.stream_status":"pending"}}', 1, '$answer', 1);
"""  # noqa: S608


@pytest.mark.parametrize("delivered", [True, False])
@pytest.mark.parametrize("with_turn_rows", [False, True])
@pytest.mark.parametrize("failing", [False, True])
@pytest.mark.asyncio
async def test_an_approval_a_newer_answer_replaced_is_superseded(
    legacy_database: _LegacyDatabase,
    *,
    failing: bool,
    with_turn_rows: bool,
    delivered: bool,
) -> None:
    """The regenerated answer stands: the approval is superseded, never shown again, and its cleanup settles it.

    One that already failed is superseded too, so its failure is never published over the newer answer, and the
    placeholder row of the turn it paused does not make that turn look in flight. An answer still owed is the
    reply's next write.
    """
    legacy_database.execute(_ATTEMPT_OWNER)
    legacy_database.execute(_NEWER_ANSWER)
    if not delivered:
        legacy_database.execute(
            "UPDATE matrix_delivery_outbox SET acknowledged_event_id = NULL WHERE delivery_id = '$edit'",
        )
    if with_turn_rows:
        legacy_database.execute(_paused_turn_rows())
    if failing:
        legacy_database.execute(
            "UPDATE approval_continuations SET state = 'failing', failure_reason = 'expired' WHERE approval_id = 'approval'",
        )
    store = legacy_database.open()
    try:
        principal = store.principal("@bot:example.org")
        approval = await principal.approval_continuation("approval")
        assert approval is not None
        assert (approval.state, approval.failure_reason) == ("failing", SUPERSEDED_FAILURE_REASON)
        await principal.replies.write_generation("gen-new", now_ns=10)
        adopted = await principal.adopt_legacy_replies(entity_name="bot", presentations=LEGACY_PRESENTATIONS, now_ns=10)
        assert [effect for applied in adopted for effect in applied.post_commit] == [WakeApproval("approval")]
        reply = await principal.replies.for_event("$answer")
        assert reply is not None
        assert reply.state is ReplyState.COMPLETED
        assert reply.approval_id is None
        spans = [(span.kind, span.outcome, span.delivery_id) for span in await principal.replies.spans(reply.reply_id)]
        # The answer the newer edit delivered is the reply's own span, so a later edit rolls back to it; one still owed
        # is the reply's next write.
        answer = (SpanKind.TURN, SpanOutcome.COMPLETED, "$answer")
        owed = (SpanKind.REGENERATION, SpanOutcome.COMPLETED, "$edit")
        expected = [answer] if delivered else [answer, owed]
        assert spans[-len(expected) :] == expected
        row = await principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
        assert row is not None
        assert row.reply_id == (None if delivered else reply.reply_id)
        assert await principal.finish_approval_continuation("approval") is not None
        assert not await principal.is_pending("$first")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a_card_of_an_unclassified_continuation_names_its_adopted_entity(
    journal_store: EventJournalStore,
) -> None:
    """Until reply classification names its span, a continuation's card is authorized for the entity it adopted."""
    principal = journal_store.principal("agent@alice")
    await _ApprovalContinuations.admit_sources(principal)
    # The run that published its cards still holds it.
    continuation = replace(_ApprovalContinuations.continuation(state="waiting"), runtime_generation="runtime-a")
    await store_main_continuation(principal, continuation)
    await _ApprovalContinuations.remember_card(principal)

    (card,) = await principal.pending_approval_cards(room_id=continuation.room_id)

    assert isinstance(card, StoredApprovalCard)
    assert card.continuation_entity_name == continuation.entity_name
