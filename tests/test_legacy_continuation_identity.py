"""Released continuations keep the reply identity they answer across the upgrade that names their span."""

from collections.abc import Sequence
from typing import Any
from unittest.mock import patch

import pytest

from mindroom.event_journal import legacy_response_attempts, postgres_backend, sqlite_backend
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

# What v2026.10.178 wrote: the continuation's identity in its response attempt tables.
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
    """The identity v2026.10.178 kept in response attempts moves onto the continuation, and the tables go."""
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


@pytest.mark.asyncio
async def test_a_claimed_continuation_keeps_its_claim_across_the_upgrade(legacy_database: _LegacyDatabase) -> None:
    """A claim v2026.10.178 stored on the continuation reads as claimed by no running instance until classification."""
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
