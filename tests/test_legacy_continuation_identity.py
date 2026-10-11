"""Approvals an earlier release left pending are cancelled by the upgrade that names their paused span."""

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
    '{"run_id":"run","session_id":"session","entity_kind":"agent","show_tool_calls":true,"room_id":"!room:example.org","thread_id":"$thread","requester_id":"@user:example.org","response_event_id":"$answer","prepared_edit_record":{"anchor_event_id":"$source","source_event_ids":["$source","$second"],"discovery_event_ids":["$alias"],"completed":false,"timestamp":1,"latest_edit_receipt_order":1,"source_event_revisions":{"$source":[1,"$edit"]},"response_event_id":"$answer","response_owner":"bot","conversation_target":{"room_id":"!room:example.org","session_id":"session","source_thread_id":null,"resolved_thread_id":null,"reply_to_event_id":"$source"}}}', 1);
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
    '{"run_id":"run","session_id":"session","entity_kind":"agent","show_tool_calls":true,"thread_id":null,"requester_id":"@user:example.org"}', 1);
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


def _columns(database: _LegacyDatabase, table: str) -> set[str]:
    sql = (
        f"SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = '{table}'"  # noqa: S608
        if database.postgres
        else f"SELECT name FROM pragma_table_info('{table}')"  # noqa: S608
    )
    return {str(row[0]) for row in database.query(sql)}


# A card and a call of the pending approval, as approval cards and calls were kept before the upgrade.
_CARD_AND_CALL = """
INSERT INTO approval_cards VALUES ('@router:example.org', '$card', 'approval', 0, 'call', 7);
INSERT INTO approval_continuation_calls (
    principal_id, approval_id, generation, tool_call_id, call_ordinal, tool_name, invoking_agent, expires_at_ns
) VALUES ('@bot:example.org', 'approval', 0, 'call', 0, 'shell', 'bot', 1);
"""


@pytest.mark.parametrize("state", ["waiting", "ready", "claimed", "failing"])
@pytest.mark.parametrize("owner", [_CONTEXT_OWNER, _ATTEMPT_OWNER], ids=["context", "response-attempt"])
@pytest.mark.asyncio
async def test_an_approval_an_earlier_release_left_pending_is_cancelled(
    legacy_database: _LegacyDatabase,
    owner: str,
    state: str,
) -> None:
    """The upgrade drops the approval with its cards and calls and settles its source; its answer gets no record."""
    legacy_database.execute(owner)
    legacy_database.execute(_CARD_AND_CALL)
    legacy_database.execute(f"UPDATE approval_continuations SET state = '{state}' WHERE approval_id = 'approval'")  # noqa: S608
    source = "$edit" if owner is _CONTEXT_OWNER else "$first"
    for _ in range(2):
        store = legacy_database.open()
        try:
            principal = store.principal("@bot:example.org")
            assert await principal.approval_continuation("approval") is None
            assert not await principal.is_pending(source)
            # Nothing edits the answer the approval paused: it keeps what it showed.
            assert await principal.replies.for_event("$answer") is None
        finally:
            await store.close()
        assert legacy_database.query("SELECT * FROM approval_cards") == []
        assert legacy_database.query("SELECT * FROM approval_continuation_calls") == []
        # The emptied call table comes back with the columns the earlier release lacked.
        assert {"toolkit_name", "arguments_digest"} <= _columns(legacy_database, "approval_continuation_calls")


@pytest.mark.asyncio
async def test_the_upgrade_drops_what_kept_the_identity_of_a_pending_approval(legacy_database: _LegacyDatabase) -> None:
    """The response attempt tables, the continuation sources, and the continuation's entity column go."""
    legacy_database.execute(_ATTEMPT_OWNER)
    await legacy_database.open().close()
    for table in ("response_attempts", "response_attempt_sources", "approval_continuation_sources"):
        assert legacy_database.query(_table_query(legacy_database.postgres, table)) == []
    assert (
        legacy_database.query(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'approval_continuations' AND column_name = 'entity_name'"
            if legacy_database.postgres
            else "SELECT name FROM pragma_table_info('approval_continuations') WHERE name = 'entity_name'",
        )
        == []
    )


def test_the_upgrade_cancels_every_approval_in_bounded_pages(
    legacy_database: _LegacyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each pending approval is cancelled, a bounded page at a time."""
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
    assert page_sizes == [2, 1, 0]
    assert legacy_database.query("SELECT approval_id FROM approval_continuations") == []
    assert (
        legacy_database.query(
            "SELECT event_id FROM journal_events WHERE state = 'pending' ORDER BY event_id",
        )
        == []
    )
