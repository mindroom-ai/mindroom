"""Tests for rewriting the event journal after a Matrix server renamed an agent's account in place."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from mindroom.agent_rename.journal import journal_account_steps, other_principal_user_ids
from mindroom.agent_rename.steps import RenamePlan, RenameRefusedError, SqliteEdit, apply_plan
from mindroom.event_journal import (
    DepartureSource,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    IngestionBatchAdmission,
    IngestionBatchIntegrityError,
    IngestionRecordAdmission,
    IngestionRecordDisposition,
    PrincipalStore,
    RoomMembershipPosition,
    encode_thread_id,
)
from mindroom.event_journal.opening import event_journal_sqlite_path
from tests.agent_rename_helpers import tree_snapshot

if TYPE_CHECKING:
    from collections.abc import Callable

AGENT = "nova"
OLD = "@old:x"
NEW = "@new:x"
# bot.py joins the agent name and the Matrix ID with one "@", and the ID starts with "@".
OLD_PRINCIPAL = "nova@@old:x"
NEW_PRINCIPAL = "nova@@new:x"
ROUTER = "router@@router:x"
ROOM = "!room:x"
UNTHREADED = encode_thread_id(None)
NOW = datetime(2026, 10, 19, 21, 45, 58, tzinfo=UTC)


def _insert(connection: sqlite3.Connection, table: str, **values: object) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(values.values()))  # noqa: S608


def _event(connection: sqlite3.Connection, principal: str, event_id: str, *, sender: str) -> None:
    _insert(
        connection,
        "journal_events",
        principal_id=principal,
        event_id=event_id,
        room_id=ROOM,
        thread_id=UNTHREADED,
        kind=EventKind.MESSAGE.value,
        sender=sender,
        origin_server_ts=10,
        source_json=json.dumps({"event_id": event_id, "sender": sender}),
        membership_epoch=0,
        state="settled",
    )


def _grant(connection: sqlite3.Connection, principal: str, grant_id: str, *, responder: str) -> None:
    _insert(
        connection,
        "approval_grants",
        principal_id=principal,
        grant_id=grant_id,
        scope_key="scope",
        room_id=ROOM,
        thread_id="$thread",
        card_event_id=f"$card-{grant_id}",
        requester_id="@alice:x",
        entity_name=AGENT,
        invoking_agent=AGENT,
        operation="shell:run",
        responder_principal_id=responder,
        responder_epoch=0,
        membership_epoch=0,
        expires_at_ns=1,
        resolution_json="{}",
        original_delivery_id=f"$delivery-{grant_id}",
    )


def _reply(connection: sqlite3.Connection, principal: str) -> None:
    _insert(
        connection,
        "reply_messages",
        principal_id=principal,
        reply_id="reply-1",
        entity_name=AGENT,
        room_id=ROOM,
        membership_epoch=0,
        state="completed",
        last_span_id="span-1",
        presentation_json="{}",
        revision=1,
        placeholder_only=False,
        reply_sequence=1,
        created_at_ns=1,
        updated_at_ns=1,
    )


def _outbox(connection: sqlite3.Connection, principal: str) -> None:
    _insert(
        connection,
        "matrix_delivery_outbox",
        principal_id=principal,
        delivery_id="$from-alice",
        stage="initial",
        event_type="m.room.message",
        room_id=ROOM,
        membership_epoch=0,
        thread_id=UNTHREADED,
        transaction_id="txn-1",
        payload_json=json.dumps({"body": f"Answered by https://matrix.to/#/{OLD}"}),
        created_at_ns=1,
    )


async def _journal(storage_root: Path) -> Path:
    """Build a journal with the real schema, holding rows for nova under his old ID and for the router."""
    database = event_journal_sqlite_path(storage_root)
    await EventJournalStore.open_sqlite(database).close()
    with closing(sqlite3.connect(database)) as connection, connection:
        # nova's own message, as both principals saw it.
        _event(connection, OLD_PRINCIPAL, "$from-nova", sender=OLD)
        _event(connection, ROUTER, "$from-nova", sender=OLD)
        _insert(
            connection,
            "visible_messages",
            principal_id=ROUTER,
            logical_event_id="$from-nova",
            room_id=ROOM,
            thread_id=UNTHREADED,
            sender=OLD,
            created_ts=10,
            revision_event_id="$from-nova",
            revision_ts=10,
            content_json=json.dumps({"body": "hello", "sender": OLD}),
            membership_epoch=0,
        )
        _outbox(connection, OLD_PRINCIPAL)
        _insert(
            connection,
            "turn_records",
            agent_name=AGENT,
            index_event_id="$from-alice",
            anchor_event_id="$from-alice",
            record_json=json.dumps({"response_owner": OLD_PRINCIPAL, "sender": OLD}),
        )
        # The router tracked nova joining under both IDs, so the rewritten old row collides with the new one.
        joins = "principal_id, room_id, user_id, baseline_receipt_order, completed"
        connection.executemany(
            f"INSERT INTO room_member_joins ({joins}) VALUES (?, ?, ?, ?, ?)",  # noqa: S608
            [(ROUTER, ROOM, OLD, 1, 1), (ROUTER, ROOM, NEW, 7, 0), (OLD_PRINCIPAL, ROOM, OLD, 1, 1)],
        )
        for principal, sequence in ((OLD_PRINCIPAL, 5), (ROUTER, 9)):
            _insert(
                connection,
                "matrix_sync_consumers",
                principal_id=principal,
                consumer_generation=f"generation-{principal}",
                stream_id=f"stream-{principal}",
                next_sequence=sequence,
            )
        _grant(connection, OLD_PRINCIPAL, "nova-grant", responder=OLD_PRINCIPAL)
        _grant(connection, ROUTER, "router-grant-for-nova", responder=OLD_PRINCIPAL)
        _grant(connection, ROUTER, "router-grant", responder=ROUTER)
        _insert(
            connection,
            "approval_grant_cards",
            principal_id=OLD_PRINCIPAL,
            continuation_id="continuation-1",
            continuation_generation=0,
            tool_call_id="call-1",
            delivery_id="$card-delivery",
            scope_key="scope",
            room_id=ROOM,
            thread_id="$thread",
            requester_id="@alice:x",
            entity_name=AGENT,
            invoking_agent=AGENT,
            operation="shell:run",
            responder_principal_id=OLD_PRINCIPAL,
            responder_epoch=0,
            membership_epoch=0,
        )
    return database


def _steps(
    database: Path,
    *,
    new_user_id: str = NEW,
    discard_new_principal: bool = False,
    new_device_kept: bool = False,
) -> list[SqliteEdit]:
    steps, _reports = journal_account_steps(
        database,
        agent=AGENT,
        old_user_id=OLD,
        new_user_id=new_user_id,
        discard_new_principal=discard_new_principal,
        new_device_kept=new_device_kept,
    )
    return steps


def _apply(steps: list[SqliteEdit], storage_root: Path) -> None:
    apply_plan(RenamePlan(storage_root=storage_root, steps=tuple(steps), reports=()), kind="rename-account", now=NOW)


def _query(database: Path, sql: str, *params: object) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute(sql, params).fetchall()


def _rows(database: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every table, for comparing a database before and after."""
    with closing(sqlite3.connect(database)) as connection:
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {table: sorted(connection.execute(f'SELECT * FROM "{table}"'), key=repr) for table in tables}  # noqa: S608


def _cells_containing(database: Path, needle: str) -> list[tuple[str, object]]:
    return [
        (table, value)
        for table, rows in _rows(database).items()
        for row in rows
        for value in row
        if isinstance(value, str) and needle in value
    ]


@pytest.mark.asyncio
async def test_rewrites_principal_senders_and_payloads(tmp_path: Path) -> None:
    """The principal is renamed everywhere and every mirrored sender and payload holds the new ID."""
    database = await _journal(tmp_path)

    _apply(_steps(database), tmp_path)

    assert _cells_containing(database, OLD) == []
    assert _query(database, "SELECT principal_id, sender, source_json FROM journal_events ORDER BY principal_id") == [
        (NEW_PRINCIPAL, NEW, json.dumps({"event_id": "$from-nova", "sender": NEW})),
        (ROUTER, NEW, json.dumps({"event_id": "$from-nova", "sender": NEW})),
    ]
    assert _query(database, "SELECT principal_id, sender, content_json FROM visible_messages") == [
        (ROUTER, NEW, json.dumps({"body": "hello", "sender": NEW})),
    ]
    assert _query(database, "SELECT principal_id, payload_json FROM matrix_delivery_outbox") == [
        (NEW_PRINCIPAL, json.dumps({"body": f"Answered by https://matrix.to/#/{NEW}"})),
    ]
    assert _query(database, "SELECT agent_name, record_json FROM turn_records") == [
        (AGENT, json.dumps({"response_owner": NEW_PRINCIPAL, "sender": NEW})),
    ]
    # The router's old-ID row collided with its new-ID row and was dropped; the new-ID row kept its own state.
    assert _query(database, "SELECT * FROM room_member_joins ORDER BY principal_id") == [
        (NEW_PRINCIPAL, ROOM, NEW, 1, 1),
        (ROUTER, ROOM, NEW, 7, 0),
    ]


@pytest.mark.asyncio
async def test_remote_ids_extending_the_old_id_are_left_alone(tmp_path: Path) -> None:
    """IDs whose server name continues past the old ID are other users; the server's rename leaves them alone."""
    database = await _journal(tmp_path)
    remote = json.dumps({"sender": "@old:x.example.org", "body": "see @old:x:8448 and @old:xy, thanks @old:x."})
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, ROUTER, "$from-remote", sender="@old:x.example.org")
        connection.execute("UPDATE journal_events SET source_json = ? WHERE event_id = '$from-remote'", (remote,))

    _apply(_steps(database), tmp_path)

    assert _query(database, "SELECT sender, source_json FROM journal_events WHERE event_id = '$from-remote'") == [
        ("@old:x.example.org", remote.replace("thanks @old:x.", "thanks @new:x.")),
    ]


@pytest.mark.asyncio
async def test_new_id_extending_the_old_id_is_adopted(tmp_path: Path) -> None:
    """A new ID that starts with the old one does not count as a leftover of it."""
    database = await _journal(tmp_path)
    extended = "@old:x.renamed"

    _apply(_steps(database, new_user_id=extended), tmp_path)

    assert _query(database, "SELECT principal_id, sender FROM journal_events ORDER BY principal_id") == [
        ("nova@@old:x.renamed", extended),
        (ROUTER, extended),
    ]


@pytest.mark.asyncio
async def test_existing_new_principal_refused_without_flag(tmp_path: Path) -> None:
    """MindRoom ran after the server rename: refuse with the counts, and discard those rows only when asked."""
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, NEW_PRINCIPAL, "$seen-after-rename", sender="@alice:x")
        _insert(
            connection,
            "room_membership",
            principal_id=NEW_PRINCIPAL,
            room_id=ROOM,
            membership_epoch=3,
        )
    before = tree_snapshot(tmp_path)

    with pytest.raises(RenameRefusedError) as refused:
        _steps(database)

    message = str(refused.value)
    assert f"rows for both {OLD_PRINCIPAL} (" in message
    assert f"Rerun with --discard-new-principal to delete the {NEW_PRINCIPAL} rows" in message
    assert "journal_events: 1" in message
    assert "room_membership: 1" in message
    assert "--discard-new-principal" in message
    assert tree_snapshot(tmp_path) == before

    _apply(_steps(database, discard_new_principal=True), tmp_path)

    assert _query(database, "SELECT event_id FROM journal_events WHERE principal_id = ?", NEW_PRINCIPAL) == [
        ("$from-nova",),
    ]
    assert _query(database, "SELECT * FROM room_membership WHERE principal_id = ?", NEW_PRINCIPAL) == []
    assert _cells_containing(database, OLD) == []


@pytest.mark.asyncio
async def test_new_principal_rows_written_after_planning_are_refused_at_apply(tmp_path: Path) -> None:
    """The edit checks for new-principal rows again inside its transaction."""
    database = await _journal(tmp_path)
    steps = _steps(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, NEW_PRINCIPAL, "$seen-after-rename", sender="@alice:x")
    before = _rows(database)

    with pytest.raises(RenameRefusedError, match="journal_events: 1"):
        _apply(steps, tmp_path)

    assert _rows(database) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("table", "write_sent_reply"),
    [("reply_messages", _reply), ("matrix_delivery_outbox", _outbox)],
)
async def test_discard_refused_when_new_principal_has_replies(
    tmp_path: Path,
    table: str,
    write_sent_reply: Callable[[sqlite3.Connection, str], None],
) -> None:
    """Replies already sent under the new ID are never discarded, even with the flag."""
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        write_sent_reply(connection, NEW_PRINCIPAL)

    for discard in (False, True):
        with pytest.raises(RenameRefusedError, match=f"{table}: 1"):
            _steps(database, discard_new_principal=discard)


@pytest.mark.asyncio
async def test_collision_outside_member_joins_rolls_back(tmp_path: Path) -> None:
    """A rewritten row that collides anywhere but room_member_joins aborts the whole edit and names the table."""
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        for sender, edit_event_id in ((OLD, "$edit-old"), (NEW, "$edit-new")):
            _insert(
                connection,
                "unresolved_edits",
                principal_id=ROUTER,
                room_id=ROOM,
                target_event_id="$target",
                sender=sender,
                edit_event_id=edit_event_id,
                edit_ts=1,
                content_json="{}",
            )
    steps = _steps(database)
    before = _rows(database)

    with pytest.raises(RenameRefusedError, match=r"unresolved_edits\.sender"):
        _apply(steps, tmp_path)

    assert _rows(database) == before


@pytest.mark.asyncio
async def test_consumer_row_and_grants_removed(tmp_path: Path) -> None:
    """The renamed principal binds a fresh sync stream, and grants bound to the old principal are gone."""
    database = await _journal(tmp_path)

    _apply(_steps(database), tmp_path)

    assert _query(database, "SELECT principal_id, stream_id, next_sequence FROM matrix_sync_consumers") == [
        (ROUTER, f"stream-{ROUTER}", 9),
    ]
    assert _query(database, "SELECT principal_id, grant_id, responder_principal_id FROM approval_grants") == [
        (ROUTER, "router-grant", ROUTER),
    ]
    assert _query(database, "SELECT * FROM approval_grant_cards") == []


def _delivery(stream: UUID, sequence: int, *, sender: str) -> IngestionBatchAdmission:
    """One sync batch carrying nova's message, as the router receives it."""
    event = InboundEvent(
        event_id="$from-nova",
        room_id=ROOM,
        thread_id=None,
        kind=EventKind.MESSAGE,
        event_class=EventClass.ACTIONABLE,
        sender=sender,
        origin_server_ts=10,
        source={"event_id": "$from-nova", "sender": sender},
    )
    record = IngestionRecordAdmission(IngestionRecordDisposition.SEMANTIC_EVENT, event=event)
    return IngestionBatchAdmission(stream, sequence, (record,))


@pytest.mark.asyncio
async def test_readmitting_rewritten_event_is_duplicate_not_integrity_error(tmp_path: Path) -> None:
    """After the rewrite, the server's re-delivery of an event with the renamed sender deduplicates cleanly."""
    database = event_journal_sqlite_path(tmp_path)
    stream = uuid4()
    store = EventJournalStore.open_sqlite(database)
    try:
        router = store.principal(ROUTER)
        consumer = await router.load_or_create_ingestion_consumer(new_generation=uuid4())
        await router.bind_ingestion_stream(generation=consumer.generation, stream_id=stream)
        assert (await router.admit_ingestion_batch(_delivery(stream, 1, sender=OLD))).semantic_event_new
        # What production hit: the stored sender is the old ID and the server now sends the new one.
        with pytest.raises(IngestionBatchIntegrityError):
            await router.admit_ingestion_batch(_delivery(stream, 2, sender=NEW))
    finally:
        await store.close()

    _apply(_steps(database), tmp_path)

    store = EventJournalStore.open_sqlite(database)
    try:
        facts = await store.principal(ROUTER).admit_ingestion_batch(_delivery(stream, 2, sender=NEW))
    finally:
        await store.close()
    assert facts.receipt_new
    assert not facts.semantic_event_new


def _membership(previous: str | None, current: str, previous_epoch: int, epoch: int) -> IngestionRecordAdmission:
    """One own-membership transition in the room, as the sync stream reports it."""
    return IngestionRecordAdmission(
        IngestionRecordDisposition.ROOM_LIFECYCLE,
        source=DepartureSource.REPORTED,
        room_id=ROOM,
        previous_membership=previous,
        membership=current,
        previous_membership_epoch=previous_epoch,
        membership_epoch=epoch,
    )


def _message(event_id: str) -> IngestionRecordAdmission:
    """One message from alice in the room."""
    event = InboundEvent(
        event_id=event_id,
        room_id=ROOM,
        thread_id=None,
        kind=EventKind.MESSAGE,
        event_class=EventClass.CONTEXT_ONLY,
        sender="@alice:x",
        origin_server_ts=10,
        source={"event_id": event_id, "sender": "@alice:x"},
    )
    return IngestionRecordAdmission(IngestionRecordDisposition.SEMANTIC_EVENT, event=event)


# A stream that saw nova join, leave, and join again ends at producer epoch 1, like the production journal did.
_REJOINED = (_membership(None, "join", 0, 0), _membership("join", "leave", 0, 1), _membership("leave", "join", 1, 1))
_CONSUMER = "SELECT consumer_generation, stream_id, next_sequence FROM matrix_sync_consumers WHERE principal_id = ?"
_PRODUCER = "SELECT room_id, membership, membership_epoch FROM matrix_ingestion_membership WHERE principal_id = ?"


async def _bound(store: EventJournalStore, principal_id: str, stream: UUID) -> PrincipalStore:
    """Bind ``stream`` to the principal, as a device's first start does."""
    principal = store.principal(principal_id)
    consumer = await principal.load_or_create_ingestion_consumer(new_generation=uuid4())
    await principal.bind_ingestion_stream(generation=consumer.generation, stream_id=stream)
    return principal


@pytest.mark.asyncio
async def test_renamed_principal_admits_the_fresh_stream_of_its_new_device(tmp_path: Path) -> None:
    """The old stream's producer positions go with its sync consumer, so the new device's stream admits every room.

    A fresh stream reports each room from membership epoch 0. Kept positions of the old stream failed every
    room's admission with IngestionBatchIntegrityError, which is what production hit after a clean adoption.
    """
    database = event_journal_sqlite_path(tmp_path)
    old_stream, fresh_stream = uuid4(), uuid4()
    store = EventJournalStore.open_sqlite(database)
    try:
        nova = await _bound(store, OLD_PRINCIPAL, old_stream)
        await nova.admit_ingestion_batch(IngestionBatchAdmission(old_stream, 1, (*_REJOINED, _message("$before"))))
    finally:
        await store.close()

    _apply(_steps(database), tmp_path)

    assert _query(database, "SELECT * FROM matrix_sync_consumers") == []
    store = EventJournalStore.open_sqlite(database)
    try:
        nova = await _bound(store, NEW_PRINCIPAL, fresh_stream)
        facts = await nova.admit_ingestion_batch(
            IngestionBatchAdmission(fresh_stream, 1, (_membership(None, "join", 0, 0),)),
        )
        producer = await nova.ingestion_membership_position(ROOM)
        tenure = await nova.membership_position(ROOM)
        history = await nova.load_event("$before")
    finally:
        await store.close()
    assert facts.receipt_new
    assert producer == RoomMembershipPosition("join", 0)
    # The journal's own tenure and history are the old principal's, renamed.
    assert tenure == RoomMembershipPosition("join", 1)
    assert history is not None


@pytest.mark.asyncio
async def test_discard_keeps_the_stream_of_the_device_already_running_under_the_new_id(tmp_path: Path) -> None:
    """With the new ID's device kept, its sync consumer and producer positions stay, and the old principal's history joins them.

    The old principal's own stream state is dropped: no device continues that stream any more.
    """
    database = event_journal_sqlite_path(tmp_path)
    old_stream, new_stream = uuid4(), uuid4()
    store = EventJournalStore.open_sqlite(database)
    try:
        nova = await _bound(store, OLD_PRINCIPAL, old_stream)
        await nova.admit_ingestion_batch(IngestionBatchAdmission(old_stream, 1, (*_REJOINED, _message("$before"))))
        renamed = await _bound(store, NEW_PRINCIPAL, new_stream)
        await renamed.admit_ingestion_batch(
            IngestionBatchAdmission(new_stream, 1, (_membership(None, "join", 0, 0), _message("$after"))),
        )
    finally:
        await store.close()
    new_consumer = _query(database, _CONSUMER, NEW_PRINCIPAL)
    new_producer = _query(database, _PRODUCER, NEW_PRINCIPAL)
    assert [row[1:] for row in new_consumer] == [(str(new_stream), 2)]
    assert new_producer == [(ROOM, "join", 0)]

    (step,) = _steps(database, discard_new_principal=True, new_device_kept=True)
    _apply([step], tmp_path)

    assert _query(database, _CONSUMER, NEW_PRINCIPAL) == new_consumer
    assert _query(database, _PRODUCER, NEW_PRINCIPAL) == new_producer
    assert _query(database, "SELECT event_id FROM journal_events WHERE principal_id = ?", NEW_PRINCIPAL) == [
        ("$before",),
    ]
    assert other_principal_user_ids(database, agent=AGENT, user_id=NEW) == {}
    store = EventJournalStore.open_sqlite(database)
    try:
        renamed = store.principal(NEW_PRINCIPAL)
        facts = await renamed.admit_ingestion_batch(
            IngestionBatchAdmission(new_stream, 2, (_membership("join", "leave", 0, 1),)),
        )
        producer = await renamed.ingestion_membership_position(ROOM)
    finally:
        await store.close()
    assert facts.receipt_new
    assert producer == RoomMembershipPosition("leave", 1)
    assert f"keep that of {NEW_PRINCIPAL} (matrix_ingestion_membership: 1, matrix_sync_consumers: 1)" in (
        step.description
    )


@pytest.mark.asyncio
async def test_discard_drops_the_new_id_stream_when_the_next_start_uses_a_fresh_device(tmp_path: Path) -> None:
    """Without the new ID's device kept, neither principal's stream is continued, so both stream states go."""
    database = event_journal_sqlite_path(tmp_path)
    old_stream, new_stream, fresh_stream = uuid4(), uuid4(), uuid4()
    store = EventJournalStore.open_sqlite(database)
    try:
        nova = await _bound(store, OLD_PRINCIPAL, old_stream)
        await nova.admit_ingestion_batch(IngestionBatchAdmission(old_stream, 1, _REJOINED))
        renamed = await _bound(store, NEW_PRINCIPAL, new_stream)
        await renamed.admit_ingestion_batch(IngestionBatchAdmission(new_stream, 1, (_membership(None, "join", 0, 0),)))
    finally:
        await store.close()

    _apply(_steps(database, discard_new_principal=True), tmp_path)

    assert _query(database, "SELECT * FROM matrix_sync_consumers") == []
    store = EventJournalStore.open_sqlite(database)
    try:
        nova = await _bound(store, NEW_PRINCIPAL, fresh_stream)
        facts = await nova.admit_ingestion_batch(
            IngestionBatchAdmission(fresh_stream, 1, (_membership(None, "join", 0, 0),)),
        )
    finally:
        await store.close()
    assert facts.receipt_new


@pytest.mark.asyncio
async def test_full_text_tables_are_left_alone(tmp_path: Path) -> None:
    """Virtual tables and their shadow tables are never updated, nor counted as leftovers."""
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE VIRTUAL TABLE notes USING fts5(body)")
        connection.execute("INSERT INTO notes (body) VALUES (?)", (f"written by {OLD}",))

    _apply(_steps(database), tmp_path)

    assert _query(database, "SELECT body FROM notes") == [(f"written by {OLD}",)]
    assert {table for table, _ in _cells_containing(database, OLD)} == {"notes", "notes_content"}


@pytest.mark.asyncio
async def test_planning_writes_nothing(tmp_path: Path) -> None:
    """Building the steps reads the journal without creating WAL or shared-memory files next to it."""
    database = await _journal(tmp_path)
    before = tree_snapshot(tmp_path)

    steps = _steps(database)

    assert tree_snapshot(tmp_path) == before
    assert len(steps) == 1


def _leave_wal_behind(database: Path, *, keep_shm: bool) -> None:
    """Commit a new-principal row that only the WAL holds, as a MindRoom that crashed would leave it."""
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    with connection:
        _event(connection, NEW_PRINCIPAL, "$only-in-wal", sender="@alice:x")
    saved = {suffix: Path(f"{database}{suffix}").read_bytes() for suffix in ("", "-wal", "-shm")}
    # Closing checkpoints the WAL into the main file and removes the WAL and its index; put them back.
    connection.close()
    for suffix, content in saved.items():
        if suffix != "-shm" or keep_shm:
            Path(f"{database}{suffix}").write_bytes(content)


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_shm", [False, True], ids=["wal", "wal-and-shm"])
async def test_planning_with_a_leftover_wal_reads_it_and_writes_nothing(tmp_path: Path, keep_shm: bool) -> None:
    """Commits that only a leftover WAL holds are seen, and the live files are not touched."""
    database = await _journal(tmp_path)
    _leave_wal_behind(database, keep_shm=keep_shm)
    before = tree_snapshot(tmp_path)

    with pytest.raises(RenameRefusedError, match="journal_events: 1"):
        _steps(database)

    assert tree_snapshot(tmp_path) == before


def test_missing_journal_needs_no_steps(tmp_path: Path) -> None:
    """An install whose journal was never created has nothing to rewrite."""
    assert _steps(event_journal_sqlite_path(tmp_path)) == []


def test_unreadable_journal_is_refused(tmp_path: Path) -> None:
    """A journal file SQLite cannot read refuses at plan time instead of failing mid-apply."""
    database = event_journal_sqlite_path(tmp_path)
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a database")

    with pytest.raises(RenameRefusedError, match="Cannot read the event journal"):
        _steps(database)


@pytest.mark.asyncio
async def test_other_principal_user_ids_lists_ids_the_agent_was_keyed_by(tmp_path: Path) -> None:
    """Principals of the agent under another ID are listed with their row counts, read without writing anything."""
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, NEW_PRINCIPAL, "$after-rename", sender=NEW)
        # An agent whose name only matches "nova" as a LIKE pattern.
        _event(connection, "hXrry@@other:x", "$other-agent", sender="@other:x")
    before = tree_snapshot(tmp_path)

    found = other_principal_user_ids(database, agent="h_rry", user_id=NEW)
    assert found == {}
    found = other_principal_user_ids(database, agent=AGENT, user_id=NEW)

    assert found == {
        OLD: "approval_grant_cards: 1, approval_grants: 2, journal_events: 1, matrix_delivery_outbox: 1, "
        "matrix_sync_consumers: 1, room_member_joins: 1",
    }
    assert tree_snapshot(tmp_path) == before
    assert other_principal_user_ids(event_journal_sqlite_path(tmp_path / "none"), agent=AGENT, user_id=NEW) == {}


def _drop_principal(database: Path, principal: str) -> None:
    """Delete every row that names ``principal`` in a principal column."""
    with closing(sqlite3.connect(database)) as connection, connection:
        for table in [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]:
            columns = {row[0] for row in connection.execute("SELECT name FROM pragma_table_info(?)", (table,))}
            for column in columns & {"principal_id", "responder_principal_id"}:
                connection.execute(f'DELETE FROM "{table}" WHERE "{column}" = ?', (principal,))  # noqa: S608


@pytest.mark.asyncio
async def test_moved_journal_is_left_alone_even_with_the_flag(tmp_path: Path) -> None:
    """Once the old principal has no rows, the new principal's rows are the agent's own history and are never deleted.

    That is the journal after a run killed past its journal step, or after a successful run that is repeated.
    """
    database = await _journal(tmp_path)
    _apply(_steps(database), tmp_path)
    moved = _rows(database)

    for discard in (False, True):
        steps, reports = journal_account_steps(
            database,
            agent=AGENT,
            old_user_id=OLD,
            new_user_id=NEW,
            discard_new_principal=discard,
            new_device_kept=False,
        )
        assert steps == []
        assert reports == [
            f"The event journal {database} has no rows for {OLD_PRINCIPAL}, so it stays as it is; its rows for "
            f"{NEW_PRINCIPAL} (journal_events: 1, matrix_delivery_outbox: 1, "
            "room_member_joins: 1) are kept.",
        ]
    assert _rows(database) == moved


@pytest.mark.asyncio
async def test_mistyped_old_id_deletes_nothing(tmp_path: Path) -> None:
    """An old ID with no rows under its principal leaves the journal alone, even with the flag."""
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, NEW_PRINCIPAL, "$seen-after-rename", sender="@alice:x")
    before = _rows(database)

    steps, reports = journal_account_steps(
        database,
        agent=AGENT,
        old_user_id="@typo:x",
        new_user_id=NEW,
        discard_new_principal=True,
        new_device_kept=False,
    )

    assert steps == []
    assert reports == [
        f"The event journal {database} has no rows for nova@@typo:x, so it stays as it is; "
        f"its rows for {NEW_PRINCIPAL} (journal_events: 1) are kept.",
    ]
    assert _rows(database) == before


@pytest.mark.asyncio
async def test_old_principal_gone_at_apply_refuses_and_keeps_new_rows(tmp_path: Path) -> None:
    """The edit checks the old principal again in its transaction and never discards when it has no rows."""
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, NEW_PRINCIPAL, "$seen-after-rename", sender="@alice:x")
    steps = _steps(database, discard_new_principal=True)
    _drop_principal(database, OLD_PRINCIPAL)
    before = _rows(database)

    with pytest.raises(RenameRefusedError, match=f"no longer has rows for {OLD_PRINCIPAL}"):
        _apply(steps, tmp_path)

    assert _rows(database) == before


@pytest.mark.asyncio
async def test_old_id_text_is_rewritten_without_touching_the_new_principal(tmp_path: Path) -> None:
    """Without old-principal rows, the old ID left in other principals' cells is still replaced.

    Nothing of the new principal is deleted, not even with the flag: its rows, sync consumer and grants stay.
    """
    database = await _journal(tmp_path)
    _drop_principal(database, OLD_PRINCIPAL)
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, NEW_PRINCIPAL, "$seen-after-rename", sender="@alice:x")
        _insert(
            connection,
            "matrix_sync_consumers",
            principal_id=NEW_PRINCIPAL,
            consumer_generation="generation-new",
            stream_id="stream-new",
            next_sequence=2,
        )
        _grant(connection, NEW_PRINCIPAL, "new-grant", responder=NEW_PRINCIPAL)

    (step,) = _steps(database, discard_new_principal=True)
    assert f"{OLD_PRINCIPAL} has no rows, so the rows of {NEW_PRINCIPAL} stay" in step.description
    _apply([step], tmp_path)

    assert _cells_containing(database, OLD) == []
    assert _query(database, "SELECT sender FROM journal_events WHERE principal_id = ?", ROUTER) == [(NEW,)]
    assert _query(database, "SELECT event_id FROM journal_events WHERE principal_id = ?", NEW_PRINCIPAL) == [
        ("$seen-after-rename",),
    ]
    assert _query(database, "SELECT stream_id FROM matrix_sync_consumers WHERE principal_id = ?", NEW_PRINCIPAL) == [
        ("stream-new",),
    ]
    assert _query(database, "SELECT grant_id FROM approval_grants WHERE principal_id = ?", NEW_PRINCIPAL) == [
        ("new-grant",),
    ]
