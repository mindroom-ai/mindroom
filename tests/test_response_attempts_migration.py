"""Released SQL and JSON gain explicit ownership once under backend startup locks."""

from collections.abc import Sequence
from typing import Any
from unittest.mock import patch

import pytest

from mindroom.event_journal import (
    DeliveryStage,
    legacy_response_attempts,
    postgres_backend,
    response_attempts,
    sqlite_backend,
)
from mindroom.response_sources import ResponseAttempt, ResponseSources
from tests.test_journal_upgrade_boundary import _LegacyDatabase
from tests.test_journal_upgrade_boundary import legacy_database as _legacy_database

legacy_database = _legacy_database

_OLD_OWNERS = """
CREATE TABLE matrix_sync_consumers (
    principal_id TEXT PRIMARY KEY, consumer_generation TEXT NOT NULL,
    stream_id TEXT UNIQUE, next_sequence BIGINT NOT NULL DEFAULT 1
);
INSERT INTO room_membership VALUES ('@bot:example.org', '!room:example.org', 7, 0, 2);
INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender,
    origin_server_ts, source_json, membership_epoch, state) VALUES
    ('@bot:example.org', '$edit', '!room:example.org', '', 'message', '@user:example.org', 1, '{}', 7, 'pending'),
    ('@bot:example.org', '$finished', '!room:example.org', '', 'message', '@user:example.org', 2, '{}', 7, 'settled');
INSERT INTO approval_continuations (principal_id, approval_id, entity_name, state, context_json, created_at_ns)
VALUES ('@bot:example.org', 'approval', 'bot', 'waiting',
    '{"run_id":"run","session_id":"session","entity_kind":"agent","room_id":"!room:example.org","requester_id":"@user:example.org","response_event_id":"$answer","prepared_edit_record":{"anchor_event_id":"$source","source_event_ids":["$source","$second"],"discovery_event_ids":["$alias"],"completed":false,"timestamp":1,"latest_edit_receipt_order":1,"source_event_revisions":{"$source":[1,"$edit"]},"response_event_id":"$answer","response_owner":"bot","conversation_target":{"room_id":"!room:example.org","session_id":"session","source_thread_id":null,"resolved_thread_id":null,"reply_to_event_id":"$source"}}}', 1);
INSERT INTO approval_continuation_sources VALUES ('@bot:example.org', 'approval', '$edit', 0);
INSERT INTO matrix_delivery_outbox (principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
    thread_id, transaction_id, payload_json, result_json, edits_event_id, attempted, acknowledged_event_id, created_at_ns)
VALUES ('@bot:example.org', '$finished', 'final', 'm.room.message', '!room:example.org', 7, '', 'original-transaction',
    '{"body":"answer","m.new_content":{"io.mindroom.final_delivery":{"prepared_edit_record":{"anchor_event_id":"$source","source_event_ids":["$source","$second"],"completed":true,"timestamp":1,"latest_edit_receipt_order":2,"source_event_revisions":{"$source":[2,"$finished"]},"response_event_id":"$answer","response_owner":"bot","conversation_target":{"room_id":"!room:example.org","session_id":"session","source_thread_id":null,"resolved_thread_id":null,"reply_to_event_id":"$source"}}}}}',
    NULL, '$answer', 1, '$final-edit-ack', 1);
"""

_PAGED_FINALS = """
INSERT INTO room_membership VALUES ('@second:example.org', '!other:example.org', 11, 0, 2);
INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender,
    origin_server_ts, source_json, membership_epoch, state) VALUES
    ('@bot:example.org', '$A', '!room:example.org', '', 'message', '@user:example.org', 3, '{}', 7, 'settled'),
    ('@bot:example.org', '$z', '!room:example.org', '', 'message', '@user:example.org', 4, '{}', 7, 'settled'),
    ('@second:example.org', '$B', '!other:example.org', '', 'message', '@user:example.org', 5, '{}', 11, 'settled'),
    ('@second:example.org', '$y', '!other:example.org', '', 'message', '@user:example.org', 6, '{}', 11, 'settled');
INSERT INTO approval_continuations
    (principal_id, approval_id, entity_name, state, context_json, created_at_ns) VALUES
    ('@bot:example.org', 'approval-A', 'agent-A', 'waiting',
        '{"room_id":"!room:example.org","response_event_id":"$answer-A"}', 2),
    ('@bot:example.org', 'approval-z', 'agent-z', 'waiting',
        '{"room_id":"!room:example.org","response_event_id":"$answer-z"}', 3),
    ('@second:example.org', 'approval-B', 'agent-B', 'waiting',
        '{"room_id":"!other:example.org","response_event_id":"$answer-B"}', 4),
    ('@second:example.org', 'approval-y', 'agent-y', 'waiting',
        '{"room_id":"!other:example.org","response_event_id":"$answer-y"}', 5);
INSERT INTO approval_continuation_sources VALUES
    ('@bot:example.org', 'approval-A', '$A', 0),
    ('@bot:example.org', 'approval-z', '$z', 0),
    ('@second:example.org', 'approval-B', '$B', 0),
    ('@second:example.org', 'approval-y', '$y', 0);
INSERT INTO matrix_delivery_outbox (principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
    thread_id, transaction_id, payload_json, result_json, attempted, acknowledged_event_id, created_at_ns) VALUES
    ('@bot:example.org', '$z', 'initial', 'm.room.message', '!room:example.org', 7, '',
        'initial-z', '{ "body" : "initial-z" }', NULL, 1, '$answer-z', 1),
    ('@bot:example.org', '$A', 'final', 'm.room.message', '!room:example.org', 7, '',
        'final-A', '{ "body" : "A" }', '{ "frozen" : "A" }', 1, '$answer-A', 2),
    ('@bot:example.org', '$z', 'final', 'm.room.message', '!room:example.org', 7, '',
        'final-z', '{ "body" : "z" }', '{ "frozen" : "z" }', 1, '$answer-z', 3),
    ('@second:example.org', '$B', 'final', 'm.room.message', '!other:example.org', 11, '',
        'final-B', '{ "body" : "B" }', '{ "frozen" : "B" }', 1, '$answer-B', 4),
    ('@second:example.org', '$y', 'final', 'm.room.message', '!other:example.org', 11, '',
        'final-y', '{ "body" : "y" }', '{ "frozen" : "y" }', 1, '$answer-y', 5);
INSERT INTO turn_records VALUES
    ('agent-A', '$A', '$A',
        '{"anchor_event_id":"$A","source_event_ids":["$A"],"completed":true,"timestamp":1,
          "response_owner":"agent-A","response_event_id":"$answer-A",
          "conversation_target":{"room_id":"!room:example.org","session_id":"session-A",
          "source_thread_id":null,"resolved_thread_id":null,"reply_to_event_id":"$A"}}'),
    ('agent-z', '$z', '$z',
        '{"anchor_event_id":"$z","source_event_ids":["$z"],"completed":true,"timestamp":1,
          "response_owner":"agent-z","response_event_id":"$answer-z",
          "conversation_target":{"room_id":"!room:example.org","session_id":"session-z",
          "source_thread_id":null,"resolved_thread_id":null,"reply_to_event_id":"$z"}}'),
    ('agent-B', '$B', '$B',
        '{"anchor_event_id":"$B","source_event_ids":["$B"],"completed":true,"timestamp":1,
          "response_owner":"agent-B","response_event_id":"$answer-B",
          "conversation_target":{"room_id":"!other:example.org","session_id":"session-B",
          "source_thread_id":null,"resolved_thread_id":null,"reply_to_event_id":"$B"}}'),
    ('agent-y', '$y', '$y',
        '{"anchor_event_id":"$y","source_event_ids":["$y"],"completed":true,"timestamp":1,
          "response_owner":"agent-y","response_event_id":"$answer-y",
          "conversation_target":{"room_id":"!other:example.org","session_id":"session-y",
          "source_thread_id":null,"resolved_thread_id":null,"reply_to_event_id":"$y"}}');
"""


def _migration_index_query(postgres: bool) -> str:
    return (
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
        "AND indexname = 'legacy_response_attempts_turn_lookup'"
        if postgres
        else "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'legacy_response_attempts_turn_lookup'"
    )


def _observe_turn_lookup_plan(transaction: Any, *, postgres: bool) -> str:  # noqa: ANN401
    """Explain the real migration lookup immediately after its helper index is installed."""
    if postgres:
        transaction.cursor.execute("SET LOCAL enable_seqscan = off")
        transaction.cursor.execute(
            "EXPLAIN SELECT record_json FROM turn_records WHERE index_event_id = %s",
            ("$probe",),
        )
        plan = " | ".join(str(value) for row in transaction.cursor.fetchall() for value in row.values())
        transaction.cursor.execute("SET LOCAL enable_seqscan = on")
        return plan
    return " | ".join(
        str(tuple(row))
        for row in transaction.connection.execute(
            "EXPLAIN QUERY PLAN SELECT record_json FROM turn_records WHERE index_event_id = ?",
            ("$probe",),
        ).fetchall()
    )


@pytest.mark.asyncio
async def test_migration_pages_retained_owners_and_preserves_frozen_bytes(
    legacy_database: _LegacyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All owners cross native-collation pages without retaining the complete old corpus."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute(_PAGED_FINALS)
    original = legacy_database.query(
        """SELECT principal_id, delivery_id, stage, transaction_id, payload_json, result_json
        FROM matrix_delivery_outbox WHERE transaction_id != 'original-transaction'
        ORDER BY principal_id, delivery_id, stage""",
    )
    module = postgres_backend if legacy_database.postgres else sqlite_backend
    transaction_type = module._PostgresTransaction if legacy_database.postgres else module._SqliteTransaction
    original_fetchall = transaction_type.fetchall
    original_execute = transaction_type.execute
    page_sizes: dict[str, list[int]] = {"continuations": [], "finals": []}
    plans: list[str] = []

    def bounded_fetchall(
        transaction: Any,  # noqa: ANN401
        sql: str,
        params: Sequence[Any] = (),
    ) -> tuple[dict[str, Any], ...]:
        rows = original_fetchall(transaction, sql, params)
        normalized = " ".join(sql.split())
        if "FROM approval_continuations" in normalized:
            page_sizes["continuations"].append(len(rows))
        elif "FROM matrix_delivery_outbox" in normalized and "stage = 'final'" in normalized:
            page_sizes["finals"].append(len(rows))
        else:
            return rows
        assert len(rows) <= 2
        return rows

    def observe_index_install(transaction: Any, sql: str, params: Sequence[Any] = ()) -> None:  # noqa: ANN401
        original_execute(transaction, sql, params)
        normalized = " ".join(sql.split())
        if normalized.startswith("CREATE INDEX") and "turn_records (index_event_id)" in normalized:
            plans.append(_observe_turn_lookup_plan(transaction, postgres=legacy_database.postgres))

    monkeypatch.setattr(legacy_response_attempts, "_PAGE_SIZE", 2, raising=False)
    with (
        patch.object(transaction_type, "fetchall", bounded_fetchall),
        patch.object(transaction_type, "execute", observe_index_install),
    ):
        store = legacy_database.open()
    try:
        expected = {
            ("@bot:example.org", "$A"): "agent-A",
            ("@bot:example.org", "$edit"): "bot",
            ("@bot:example.org", "$finished"): "bot",
            ("@bot:example.org", "$z"): "agent-z",
            ("@second:example.org", "$B"): "agent-B",
            ("@second:example.org", "$y"): "agent-y",
        }
        for key, entity_name in expected.items():
            attempt = await store.backend.read(lambda tx, key=key: response_attempts.load_response_attempt(tx, *key))
            assert attempt is not None
            assert attempt.entity_name == entity_name
        pending_sources = await store.backend.read(
            lambda tx: tx.fetchall(
                "SELECT principal_id, approval_id, event_id, source_ordinal FROM approval_continuation_sources",
            ),
        )
        assert {
            (str(row["principal_id"]), str(row["approval_id"]), str(row["event_id"]), int(row["source_ordinal"]))
            for row in pending_sources
        } == {
            ("@bot:example.org", "approval", "$edit", 0),
            ("@bot:example.org", "approval-A", "$A", 0),
            ("@bot:example.org", "approval-z", "$z", 0),
            ("@second:example.org", "approval-B", "$B", 0),
            ("@second:example.org", "approval-y", "$y", 0),
        }
    finally:
        await store.close()

    assert page_sizes == {"continuations": [2, 2, 1], "finals": [2, 2, 1]}
    assert plans
    assert "INDEX" in plans[0].upper()
    assert (
        legacy_database.query(
            """SELECT principal_id, delivery_id, stage, transaction_id, payload_json, result_json
        FROM matrix_delivery_outbox WHERE transaction_id != 'original-transaction'
        ORDER BY principal_id, delivery_id, stage""",
        )
        == original
    )
    assert legacy_database.query(_migration_index_query(legacy_database.postgres)) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("competing_local", [False, True])
async def test_literal_owners_survive_migration_and_reopen(
    legacy_database: _LegacyDatabase,
    competing_local: bool,
) -> None:
    """Pending and completed sources survive without rewriting frozen wire bytes."""
    legacy_database.execute(_OLD_OWNERS)
    if competing_local:
        legacy_database.execute("UPDATE matrix_delivery_outbox SET result_json = '{}'")
    original = legacy_database.query("SELECT payload_json, transaction_id FROM matrix_delivery_outbox")
    store = legacy_database.open()
    try:
        approval = await store.principal("@bot:example.org").approval_continuation("approval")
        assert approval.sources.logical_source_event_ids == ("$source", "$second")
        assert approval.sources.discovery_event_ids == ("$alias",)
        assert approval.source_event_ids == ("$edit",)
        finished = await store.backend.read(
            lambda tx: response_attempts.load_response_attempt(tx, "@bot:example.org", "$finished"),
        )
        assert finished.response_event_id == "$answer"
        assert finished.logical_source_event_ids == ("$source", "$second")
        assert await store.backend.read(
            lambda tx: response_attempts.edited_attempt_sources_before_stop(
                tx,
                "@bot:example.org",
                room_id="!room:example.org",
                response_event_id="$answer",
                source_event_id="$source",
                stop_receipt_order=2,
            ),
        ) == ("$edit", "$finished")
    finally:
        await store.close()
    assert legacy_database.query("SELECT payload_json, transaction_id FROM matrix_delivery_outbox") == original
    if competing_local:
        assert legacy_database.query("SELECT result_json FROM matrix_delivery_outbox") == [("{}",)]
    legacy_database.execute("UPDATE approval_continuations SET context_json = '{}' WHERE approval_id = 'approval'")
    reopened = legacy_database.open()
    try:
        assert (await reopened.backend.read(lambda tx: tx.fetchone("SELECT COUNT(*) AS count FROM response_attempts")))[
            "count"
        ] == 2
    finally:
        await reopened.close()


def test_corrupt_required_live_identity_rolls_back_schema(legacy_database: _LegacyDatabase) -> None:
    """Unprovable live ownership aborts the whole schema transaction."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute("UPDATE approval_continuations SET context_json = '{}' WHERE approval_id = 'approval'")
    with pytest.raises(ValueError, match="identity"):
        legacy_database.open()
    query = (
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = 'response_attempts'"
        if legacy_database.postgres
        else "SELECT name FROM sqlite_master WHERE name = 'response_attempts'"
    )
    assert legacy_database.query(query) == []
    assert legacy_database.query("SELECT state FROM journal_events WHERE event_id = '$edit'") == [("pending",)]


def test_unreadable_final_for_surviving_continuation_rolls_back_schema(
    legacy_database: _LegacyDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable required ownership on a late page rolls back the whole migration."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute("""
        UPDATE matrix_delivery_outbox
        SET delivery_id = '$edit', payload_json = '{}', result_json = '[]';
        INSERT INTO matrix_delivery_outbox (principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
            thread_id, transaction_id, payload_json, result_json, attempted, created_at_ns) VALUES
            ('@bot:example.org', '$aa', 'final', 'm.room.message', '!room:example.org', 7, '',
                'skip-aa', '{}', NULL, 1, 2),
            ('@bot:example.org', '$ab', 'final', 'm.room.message', '!room:example.org', 7, '',
                'skip-ab', '{}', NULL, 1, 3),
            ('@bot:example.org', '$ac', 'final', 'm.room.message', '!room:example.org', 7, '',
                'skip-ac', '{}', NULL, 1, 4),
            ('@bot:example.org', '$ad', 'final', 'm.room.message', '!room:example.org', 7, '',
                'skip-ad', '{}', NULL, 1, 5);
    """)
    original = legacy_database.query(
        """SELECT principal_id, delivery_id, stage, transaction_id, payload_json, result_json
        FROM matrix_delivery_outbox ORDER BY principal_id, delivery_id, stage""",
    )
    module = postgres_backend if legacy_database.postgres else sqlite_backend
    transaction_type = module._PostgresTransaction if legacy_database.postgres else module._SqliteTransaction
    original_fetchall = transaction_type.fetchall
    final_page_sizes: list[int] = []

    def observe_pages(
        transaction: Any,  # noqa: ANN401
        sql: str,
        params: Sequence[Any] = (),
    ) -> tuple[dict[str, Any], ...]:
        rows = original_fetchall(transaction, sql, params)
        normalized = " ".join(sql.split())
        if "FROM matrix_delivery_outbox" in normalized and "stage = 'final'" in normalized:
            final_page_sizes.append(len(rows))
            assert len(rows) <= 2
        return rows

    monkeypatch.setattr(legacy_response_attempts, "_PAGE_SIZE", 2, raising=False)
    with (
        patch.object(transaction_type, "fetchall", observe_pages),
        pytest.raises(ValueError, match="identity"),
    ):
        legacy_database.open()
    query = (
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = 'response_attempts'"
        if legacy_database.postgres
        else "SELECT name FROM sqlite_master WHERE name = 'response_attempts'"
    )
    assert final_page_sizes == [2, 2, 1]
    assert legacy_database.query(query) == []
    assert legacy_database.query(_migration_index_query(legacy_database.postgres)) == []
    assert (
        legacy_database.query(
            """SELECT principal_id, delivery_id, stage, transaction_id, payload_json, result_json
        FROM matrix_delivery_outbox ORDER BY principal_id, delivery_id, stage""",
        )
        == original
    )
    assert legacy_database.query("SELECT state FROM journal_events WHERE event_id = '$edit'") == [("pending",)]


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("room_id", "'!wrong:example.org'"),
        ("membership_epoch", "8"),
        ("edits_event_id", "'$wrong-answer'"),
    ],
)
def test_final_conflicting_with_required_live_owner_rolls_back_schema(
    legacy_database: _LegacyDatabase,
    column: str,
    value: str,
) -> None:
    """A readable FINAL cannot replace the room, epoch, or visible target of live ownership."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute(
        f"UPDATE matrix_delivery_outbox SET delivery_id = '$edit', {column} = {value}",  # noqa: S608
    )
    original = legacy_database.query(
        "SELECT room_id, membership_epoch, edits_event_id, payload_json, result_json FROM matrix_delivery_outbox",
    )

    with pytest.raises(ValueError, match="identity"):
        legacy_database.open()

    query = (
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = 'response_attempts'"
        if legacy_database.postgres
        else "SELECT name FROM sqlite_master WHERE name = 'response_attempts'"
    )
    assert legacy_database.query(query) == []
    assert (
        legacy_database.query(
            "SELECT room_id, membership_epoch, edits_event_id, payload_json, result_json FROM matrix_delivery_outbox",
        )
        == original
    )


@pytest.mark.asyncio
async def test_inline_final_for_surviving_continuation_retains_success_proof(legacy_database: _LegacyDatabase) -> None:
    """An ACK before continuation deletion still proves newer successful ownership."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute(
        "UPDATE matrix_delivery_outbox SET delivery_id = '$edit', payload_json = replace(payload_json, '$finished', '$edit')",
    )
    original = legacy_database.query("SELECT payload_json, transaction_id FROM matrix_delivery_outbox")
    store = legacy_database.open()
    try:
        result = await store.backend.read(lambda tx: tx.fetchone("SELECT result_json FROM matrix_delivery_outbox"))
        assert result["result_json"] is not None
    finally:
        await store.close()
    assert legacy_database.query("SELECT payload_json, transaction_id FROM matrix_delivery_outbox") == original


@pytest.mark.asyncio
async def test_inline_final_precedes_unreadable_local_result_for_surviving_continuation(
    legacy_database: _LegacyDatabase,
) -> None:
    """A substantive frozen outcome rescues malformed local result text."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute("""
        UPDATE matrix_delivery_outbox
        SET delivery_id = '$edit', payload_json = replace(payload_json, '$finished', '$edit'), result_json = '[]';
    """)
    original = legacy_database.query("SELECT payload_json, result_json, transaction_id FROM matrix_delivery_outbox")
    store = legacy_database.open()
    try:
        delivery = await store.principal("@bot:example.org").load_matrix_delivery(
            delivery_id="$edit",
            stage=DeliveryStage.FINAL,
        )
        assert delivery is not None
        assert delivery.result is not None
        assert delivery.result["prepared_edit_record"]["response_owner"] == "bot"
    finally:
        await store.close()
    assert (
        legacy_database.query("SELECT payload_json, result_json, transaction_id FROM matrix_delivery_outbox")
        == original
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_secondary", [False, True])
async def test_ordinary_final_does_not_inherit_current_selected_edit(
    legacy_database: _LegacyDatabase,
    missing_secondary: bool,
) -> None:
    """Current selection cannot relabel an ordinary ACK; incomplete optional identity is ignored."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute("""
        INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender,
            origin_server_ts, source_json, membership_epoch, state)
        VALUES ('@bot:example.org', '$ordinary', '!room:example.org', '', 'message', '@user:example.org', 3, '{}', 7, 'settled');
        INSERT INTO matrix_delivery_outbox (principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
            thread_id, transaction_id, payload_json, result_json, attempted, acknowledged_event_id, created_at_ns)
        VALUES ('@bot:example.org', '$ordinary', 'final', 'm.room.message', '!room:example.org', 7, '', 'ordinary-transaction',
            '{"body":"original"}', '{"body":"original"}', 1, '$ordinary-answer', 1);
    """)
    record_sql = """
        INSERT INTO turn_records VALUES ('bot', '$ordinary', '$ordinary',
        '{"anchor_event_id":"$ordinary","source_event_ids":["$ordinary"],"completed":true,
          "timestamp":1,"latest_edit_receipt_order":99,"response_owner":"bot","response_event_id":"$ordinary-answer",
          "conversation_target":{"room_id":"!room:example.org","session_id":"session","source_thread_id":null,
                                 "resolved_thread_id":null,"reply_to_event_id":"$ordinary"}}');
    """
    if missing_secondary:
        record_sql = record_sql.replace('["$ordinary"]', '["$ordinary","$missing"]')
    legacy_database.execute(record_sql)
    store = legacy_database.open()
    try:
        ordinary = await store.backend.read(
            lambda tx: response_attempts.load_response_attempt(tx, "@bot:example.org", "$ordinary"),
        )
        if missing_secondary:
            assert ordinary is None
        else:
            assert ordinary.edit_receipt_order is None
        assert (
            await store.backend.read(
                lambda tx: response_attempts.load_response_attempt(tx, "@bot:example.org", "$edit"),
            )
            is not None
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_ordinary_coalesced_final_preserves_chronological_logical_sources(
    legacy_database: _LegacyDatabase,
) -> None:
    """The delivery driver need not be the earliest coalesced logical source."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute("""
        INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender,
            origin_server_ts, source_json, membership_epoch, state) VALUES
            ('@bot:example.org', '$earlier', '!room:example.org', '', 'message', '@user:example.org', 3, '{}', 7, 'settled'),
            ('@bot:example.org', '$ordinary', '!room:example.org', '', 'message', '@user:example.org', 4, '{}', 7, 'settled');
        INSERT INTO matrix_delivery_outbox (principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
            thread_id, transaction_id, payload_json, result_json, attempted, acknowledged_event_id, created_at_ns)
        VALUES ('@bot:example.org', '$ordinary', 'final', 'm.room.message', '!room:example.org', 7, '',
            'coalesced-transaction', '{"body":"coalesced"}', '{"body":"coalesced"}', 1, '$coalesced-answer', 1);
        INSERT INTO turn_records VALUES ('bot', '$ordinary', '$ordinary',
            '{"anchor_event_id":"$earlier","source_event_ids":["$earlier","$ordinary"],"completed":true,
              "timestamp":1,"response_owner":"bot","response_event_id":"$coalesced-answer",
              "conversation_target":{"room_id":"!room:example.org","session_id":"session","source_thread_id":null,
                                     "resolved_thread_id":null,"reply_to_event_id":"$earlier"}}');
    """)
    store = legacy_database.open()
    try:
        ordinary = await store.backend.read(
            lambda tx: response_attempts.load_response_attempt(tx, "@bot:example.org", "$ordinary"),
        )
        assert ordinary is not None
        assert ordinary.logical_source_event_ids == ("$earlier", "$ordinary")
        assert ordinary.edit_receipt_order is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_frozen_legacy_initial_recovery_can_register_its_final(legacy_database: _LegacyDatabase) -> None:
    """An old placeholder needs no guessed attempt before ordinary FINAL registration."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute("""
        DELETE FROM approval_continuation_sources;
        DELETE FROM approval_continuations;
        DELETE FROM matrix_delivery_outbox;
        INSERT INTO matrix_delivery_outbox (principal_id, delivery_id, stage, event_type, room_id, membership_epoch,
            thread_id, transaction_id, payload_json, attempted, created_at_ns)
        VALUES ('@bot:example.org', '$edit', 'initial', 'm.room.message', '!room:example.org', 7, '', 'placeholder-transaction',
            '{"body":"Thinking..."}', 1, 1);
    """)
    store = legacy_database.open()
    try:
        principal = store.principal("@bot:example.org")
        assert (
            await store.backend.read(
                lambda tx: response_attempts.load_response_attempt(tx, "@bot:example.org", "$edit"),
            )
            is None
        )
        assert await principal.claim_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.INITIAL) is not None
        await principal.acknowledge_matrix_delivery(
            delivery_id="$edit",
            stage=DeliveryStage.INITIAL,
            event_id="$placeholder",
            delivered_projections=(),
        )
        assert await principal.enqueue_matrix_delivery(
            delivery_id="$edit",
            stage=DeliveryStage.FINAL,
            room_id="!room:example.org",
            thread_id=None,
            payload={"body": "answer"},
            edits_event_id="$placeholder",
            response_attempt=ResponseAttempt("bot", ResponseSources(("$edit",), ("$edit",))),
        )
        attempt = await store.backend.read(
            lambda tx: response_attempts.load_response_attempt(tx, "@bot:example.org", "$edit"),
        )
        assert attempt.response_event_id == "$placeholder"
        initial = await principal.load_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.INITIAL)
        assert initial.transaction_id == "placeholder-transaction"
        assert initial.payload == {"body": "Thinking..."}
    finally:
        await store.close()
