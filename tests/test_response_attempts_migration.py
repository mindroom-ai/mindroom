"""Released SQL and JSON gain explicit ownership once under backend startup locks."""

import pytest

from mindroom.event_journal import DeliveryStage, response_attempts
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
) -> None:
    """Unreadable success proof cannot authorize a required live owner."""
    legacy_database.execute(_OLD_OWNERS)
    legacy_database.execute("""
        UPDATE matrix_delivery_outbox
        SET delivery_id = '$edit', payload_json = '{}', result_json = '[]';
    """)
    with pytest.raises(ValueError, match="identity"):
        legacy_database.open()
    query = (
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = 'response_attempts'"
        if legacy_database.postgres
        else "SELECT name FROM sqlite_master WHERE name = 'response_attempts'"
    )
    assert legacy_database.query(query) == []
    assert legacy_database.query(
        "SELECT delivery_id, payload_json, result_json FROM matrix_delivery_outbox",
    ) == [("$edit", "{}", "[]")]
    assert legacy_database.query("SELECT state FROM journal_events WHERE event_id = '$edit'") == [("pending",)]


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
