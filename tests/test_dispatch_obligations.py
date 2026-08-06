"""Durable exact Matrix callback obligations and restart recovery."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.background_tasks import run_blocking_until_complete, wait_for_background_tasks
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_obligations import DispatchObligationRunner
from mindroom.dispatch_obligations.events import (
    DispatchCallbackResult,
    _RoomIdEvent,
    callback_kind_for_source_kind,
)
from mindroom.dispatch_obligations.storage import (
    DispatchCallbackKind,
    DispatchCreateResult,
    DispatchObligation,
    DispatchObligationStore,
    DispatchSemanticConsumer,
    DispatchTerminalOutcome,
)
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active
from mindroom.dispatch_source import IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND
from mindroom.handled_turns import HandledTurnLedger, TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.matrix.media import MatrixMediaEvent, parse_matrix_media_event_source

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from pathlib import Path

_PRINCIPAL_ID = "@code:example.org"
_ENTITY_NAME = "code"
_ROOM_ID = "!room:example.org"


def _store(
    tmp_path: Path,
    *,
    principal_id: str = _PRINCIPAL_ID,
    entity_name: str = _ENTITY_NAME,
) -> DispatchObligationStore:
    return DispatchObligationStore(
        tracking_path=tmp_path / "tracking",
        principal_id=principal_id,
        entity_name=entity_name,
    )


def _database_path(store: DispatchObligationStore) -> Path:
    return store._database_path


def _message_obligation(
    event_id: str,
    *,
    principal_id: str = _PRINCIPAL_ID,
    entity_name: str = _ENTITY_NAME,
    room_id: str = _ROOM_ID,
) -> DispatchObligation:
    return DispatchObligation(
        principal_id=principal_id,
        entity_name=entity_name,
        source_event_id=event_id,
        callback_kind=DispatchCallbackKind.MESSAGE,
        room_id=room_id,
        event_source={
            "type": "m.room.message",
            "event_id": event_id,
            "sender": "@user:example.org",
            "origin_server_ts": 1_234,
            "content": {"msgtype": "m.text", "body": "hello"},
        },
    )


def _message_event(event_id: str) -> nio.RoomMessageText:
    event = nio.Event.parse_event(_message_obligation(event_id).event_source)
    assert isinstance(event, nio.RoomMessageText)
    return event


def _reaction_obligation(event_id: str) -> DispatchObligation:
    """Build one replayable reaction obligation."""
    return replace(
        _message_obligation(event_id),
        callback_kind=DispatchCallbackKind.REACTION,
        event_source={
            "type": "m.reaction",
            "event_id": event_id,
            "sender": "@user:example.org",
            "origin_server_ts": 1_234,
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$target",
                    "key": "✅",
                },
            },
        },
    )


def test_store_database_name_identifies_entity_and_principal(tmp_path: Path) -> None:
    """Operators must be able to map one leaf database back to its entity."""
    store = _store(tmp_path)

    assert store._database_path.name.startswith("dispatch_obligations-code-")
    assert store._database_path.name.endswith(".sqlite3")


def test_store_operations_do_not_repeat_tracking_directory_creation(tmp_path: Path) -> None:
    """Only store construction should create its tracking directory."""
    store = _store(tmp_path)

    with patch("pathlib.Path.mkdir") as mkdir:
        store.pending()

    mkdir.assert_not_called()


def test_receipt_order_is_durable_across_callback_kinds_and_settlement(tmp_path: Path) -> None:
    """STOP/edit ordering must use admission order, never opaque Matrix event IDs."""
    store = _store(tmp_path)
    edit = _message_obligation("$z-edit")
    stop = _reaction_obligation("$a-stop")

    store.create_pending(edit)
    store.create_pending(stop)
    edit_order = store.receipt_order(edit.key)
    stop_order = store.receipt_order(stop.key)
    store.settle(edit.key, DispatchTerminalOutcome.SUCCEEDED)
    restarted = _store(tmp_path)

    assert edit_order < stop_order
    assert restarted.receipt_order(edit.key) == edit_order
    assert restarted.receipt_order(stop.key) == stop_order


def test_only_invites_can_discard_receipt_order_rows(tmp_path: Path) -> None:
    """Deleting ordered callbacks would allow SQLite to reuse their receipt order."""
    store = _store(tmp_path)
    message = _message_obligation("$message")
    store.create_pending(message)

    with pytest.raises(ValueError, match="Only successful invite obligations"):
        store.discard_pending(message.key)

    assert store.receipt_order(message.key) > 0


@pytest.mark.asyncio
async def test_successful_invite_is_deleted_and_identical_reinvite_runs(tmp_path: Path) -> None:
    """Successful synthetic invite keys must not suppress a later identical invite."""
    attempts = 0

    async def callback(_room: nio.MatrixRoom, event: nio.InviteEvent) -> DispatchCallbackResult:
        nonlocal attempts
        assert isinstance(event, nio.InviteMemberEvent)
        attempts += 1
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.INVITE: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, _PRINCIPAL_ID),
        turn_is_terminal=lambda _event_id: False,
    )
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = nio.InviteEvent.parse_event(
        {
            "type": "m.room.member",
            "sender": "@owner:example.org",
            "state_key": _PRINCIPAL_ID,
            "content": {"membership": "invite"},
        },
    )
    assert isinstance(event, nio.InviteMemberEvent)

    await runner.dispatch(room, event, DispatchCallbackKind.INVITE)
    await runner.dispatch(room, event, DispatchCallbackKind.INVITE)

    with store._connection() as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM dispatch_obligations").fetchone()[0]
    assert attempts == 2
    assert row_count == 0


@pytest.mark.parametrize("source_kind", [IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND, VOICE_SOURCE_KIND])
def test_media_source_kinds_map_to_media_dispatch(source_kind: str) -> None:
    """Coalescing retries must use the callback kind owned by dispatch obligations."""
    assert callback_kind_for_source_kind(source_kind) is DispatchCallbackKind.MEDIA


def test_text_source_kind_maps_to_message_dispatch() -> None:
    """Ordinary text retries must return to the message callback owner."""
    assert callback_kind_for_source_kind("message") is DispatchCallbackKind.MESSAGE


def _unknown_event(event_id: str, event_type: str) -> nio.UnknownEvent:
    event = nio.Event.parse_event(
        {
            "type": event_type,
            "event_id": event_id,
            "sender": "@user:example.org",
            "origin_server_ts": 1_234,
            "content": {},
        },
    )
    assert isinstance(event, nio.UnknownEvent)
    return event


def _encrypted_image_source(event_id: str) -> dict[str, object]:
    return {
        "type": "m.room.message",
        "event_id": event_id,
        "sender": "@user:example.org",
        "origin_server_ts": 1_234,
        "content": {
            "msgtype": "m.image",
            "body": "image.bin",
            "file": {
                "url": "mxc://example.org/image",
                "key": {
                    "alg": "A256CTR",
                    "ext": True,
                    "key_ops": ["encrypt", "decrypt"],
                    "kty": "oct",
                    "k": "SYNTHETIC_FILE_KEY_DO_NOT_USE",
                },
                "iv": "SYNTHETIC_FILE_IV_DO_NOT_USE",
                "hashes": {"sha256": "SYNTHETIC_FILE_HASH_DO_NOT_USE"},
                "v": "v2",
            },
        },
    }


async def _drop_historical_event(_room: nio.MatrixRoom, _event: nio.Event) -> None:
    """Ignore the non-live events a runner would otherwise cache."""


def _runner(
    store: DispatchObligationStore,
    callback: Callable[[nio.MatrixRoom, nio.Event], Awaitable[DispatchCallbackResult]],
    *,
    turn_is_terminal: Callable[[str], bool] = lambda _event_id: False,
    background_task_owner: object | None = None,
    retry_initial_delay_seconds: float = 1.0,
    retry_max_delay_seconds: float = 30.0,
    cache_historical_event: Callable[[nio.MatrixRoom, nio.Event], Awaitable[None]] = _drop_historical_event,
) -> DispatchObligationRunner:
    return DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.MESSAGE: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, "@code:example.org"),
        turn_is_terminal=turn_is_terminal,
        cache_historical_event=cache_historical_event,
        background_task_owner=background_task_owner,
        _retry_initial_delay_seconds=retry_initial_delay_seconds,
        _retry_max_delay_seconds=retry_max_delay_seconds,
    )


def test_pending_row_survives_new_store_instance(tmp_path: Path) -> None:
    """Dropping process memory must not drop callback work already accepted."""
    first = _store(tmp_path)
    obligation = _message_obligation("$message")

    assert first.create_pending(obligation) is DispatchCreateResult.CREATED

    restarted = _store(tmp_path)

    assert restarted.pending() == (obligation,)
    assert restarted.has_pending("$message", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_aged_interrupted_command_journal_survives_pending_obligation_recovery(tmp_path: Path) -> None:
    """Pending callback recovery must keep the command checkpoint it depends on."""
    event_id = "$interrupted-command"
    ledger = HandledTurnLedger(_ENTITY_NAME, base_path=tmp_path / "tracking")
    ledger.record_handled_turn(
        TurnRecord.create(
            [event_id],
            completed=False,
            command_execution_started=True,
            timestamp=time.time() - (40 * 24 * 60 * 60),
        ),
    )
    ledger.flush()
    store = _store(tmp_path)
    store.create_pending(_message_obligation(event_id))

    restarted_ledger = HandledTurnLedger(_ENTITY_NAME, base_path=tmp_path / "tracking")
    restarted_ledger.cleanup()
    recovered_records: list[TurnRecord | None] = []

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        recovered_records.append(restarted_ledger.get_turn_record(event.event_id))
        return DispatchCallbackResult.DEFERRED

    await _runner(store, callback, turn_is_terminal=restarted_ledger.has_responded).recover_pending(turn_backed=True)

    assert len(recovered_records) == 1
    recovered_record = recovered_records[0]
    assert recovered_record is not None
    assert recovered_record.command_execution_started
    assert store.has_pending(event_id, DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_startup_recovers_aged_completed_turn_before_cleanup(tmp_path: Path) -> None:
    """Cleanup must not erase terminal truth before it settles the other durable store."""
    event_id = "$completed-command"
    tracking_path = tmp_path / "tracking"
    ledger = HandledTurnLedger(_ENTITY_NAME, base_path=tracking_path)
    ledger.record_handled_turn(
        TurnRecord.create(
            [event_id],
            response_event_id="$response",
            timestamp=time.time() - (40 * 24 * 60 * 60),
        ),
    )
    ledger.flush()
    store = _store(tmp_path)
    obligation = _message_obligation(event_id)
    store.create_pending(obligation)
    store.mark_callback_deferred(obligation.key)
    _reset_handled_turn_ledger_runtime()

    restarted_ledger = HandledTurnLedger(_ENTITY_NAME, base_path=tracking_path)
    restarted_ledger.load()
    callback = AsyncMock(return_value=DispatchCallbackResult.DEFERRED)
    runner = _runner(store, callback, turn_is_terminal=restarted_ledger.has_durably_responded)

    await runner.recover_pending(turn_backed=True)
    unsettled_source_event_ids = store.unsettled_source_event_ids()
    restarted_ledger.cleanup(unsettled_source_event_ids=unsettled_source_event_ids)

    callback.assert_not_awaited()
    assert not store.has_pending(event_id, DispatchCallbackKind.MESSAGE)
    assert event_id not in unsettled_source_event_ids
    assert restarted_ledger.get_turn_record(event_id) is None


def test_store_connections_close_and_configure_concurrent_writes(tmp_path: Path) -> None:
    """Every short-lived connection must close after using explicit concurrency settings."""
    store = _store(tmp_path)

    with store._connection() as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode == "wal"
    assert busy_timeout == 5_000
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


def test_entity_admission_store_is_not_blocked_by_another_entity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One entity's write transaction must not block another entity's admission."""
    first = _store(tmp_path)
    second_principal = "@other:example.org"
    second_entity = "other"
    second = _store(
        tmp_path,
        principal_id=second_principal,
        entity_name=second_entity,
    )
    lock_connection = sqlite3.connect(_database_path(first), isolation_level=None)
    lock_connection.execute("BEGIN IMMEDIATE")

    def connect_without_waiting() -> sqlite3.Connection:
        connection = sqlite3.connect(_database_path(second), timeout=0.01)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(second, "_connect", connect_without_waiting)
    try:
        result = second.create_pending(
            _message_obligation(
                "$other-entity",
                principal_id=second_principal,
                entity_name=second_entity,
            ),
        )
    finally:
        lock_connection.rollback()
        lock_connection.close()

    assert result is DispatchCreateResult.CREATED


def test_pending_recovery_query_uses_pending_order_index(tmp_path: Path) -> None:
    """Permanent tombstones must not be scanned or sorted to recover pending work."""
    store = _store(tmp_path)
    database_path = _database_path(store)
    with sqlite3.connect(database_path) as connection:
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT source_event_id, callback_kind, room_id, event_source_json
            FROM dispatch_obligations
            WHERE principal_id = ?
              AND entity_name = ?
              AND state IN ('pending', 'deferred')
            ORDER BY created_at_ns, rowid
            """,
            (_PRINCIPAL_ID, _ENTITY_NAME),
        ).fetchall()

    details = tuple(row[3] for row in plan)
    assert any("USING INDEX dispatch_obligations_pending_recovery" in detail for detail in details)
    assert all("USE TEMP B-TREE" not in detail for detail in details)


def test_exact_callback_kind_keeps_distinct_obligations_for_one_event(tmp_path: Path) -> None:
    """Two callback purposes for one Matrix event must not settle each other."""
    store = _store(tmp_path)
    message = _message_obligation("$same")
    approval = replace(message, callback_kind=DispatchCallbackKind.APPROVAL)

    assert store.create_pending(message) is DispatchCreateResult.CREATED
    assert store.create_pending(approval) is DispatchCreateResult.CREATED

    assert store.has_pending("$same", DispatchCallbackKind.MESSAGE)
    assert store.has_pending("$same", DispatchCallbackKind.APPROVAL)


@pytest.mark.parametrize(
    "outcome",
    [DispatchTerminalOutcome.SUCCEEDED, DispatchTerminalOutcome.INTENTIONALLY_IGNORED],
)
def test_terminal_settlement_survives_restart_and_blocks_recreation(
    tmp_path: Path,
    outcome: DispatchTerminalOutcome,
) -> None:
    """A cold replay must not recreate work explicitly settled before restart."""
    store = _store(tmp_path)
    obligation = _message_obligation("$terminal")
    store.create_pending(obligation)

    store.settle(obligation.key, outcome)

    restarted = _store(tmp_path)
    assert restarted.pending() == ()
    assert restarted.create_pending(obligation) is DispatchCreateResult.ALREADY_TERMINAL


def test_terminal_settlement_compacts_payload_before_invalid_replay_check(tmp_path: Path) -> None:
    """Terminal exact keys need no replay payload and must bypass later payload validation."""
    store = _store(tmp_path)
    obligation = _message_obligation("$compact")
    store.create_pending(obligation)

    store.settle(obligation.key, DispatchTerminalOutcome.SUCCEEDED)

    database_path = _database_path(store)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT room_id, event_source_json, semantic_consumer FROM dispatch_obligations WHERE source_event_id = ?",
            (obligation.source_event_id,),
        ).fetchone()
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert row == ("", "", None)
    assert schema_version == 1
    invalid_replay = replace(
        obligation,
        room_id="!different:example.org",
        event_source={"event_id": obligation.source_event_id, "not_json_safe": object()},
    )
    assert store.create_pending(invalid_replay) is DispatchCreateResult.ALREADY_TERMINAL


def test_semantic_consumer_claim_is_durable_and_single_owner(tmp_path: Path) -> None:
    """One accepted callback may choose exactly one application consumer."""
    store = _store(tmp_path)
    obligation = _message_obligation("$consumer")
    store.create_pending(obligation)

    assert (
        store.claim_semantic_consumer(obligation.key, DispatchSemanticConsumer.APPROVAL_REPLY)
        is DispatchSemanticConsumer.APPROVAL_REPLY
    )
    assert (
        store.claim_semantic_consumer(obligation.key, DispatchSemanticConsumer.APPROVAL_REPLY)
        is DispatchSemanticConsumer.APPROVAL_REPLY
    )
    with pytest.raises(ValueError, match="cannot consume"):
        store.claim_semantic_consumer(obligation.key, DispatchSemanticConsumer.STOP_REACTION)
    reaction = _reaction_obligation("$reaction-consumer")
    store.create_pending(reaction)
    assert (
        store.claim_semantic_consumer(reaction.key, DispatchSemanticConsumer.STOP_REACTION)
        is DispatchSemanticConsumer.STOP_REACTION
    )
    assert (
        store.claim_semantic_consumer(reaction.key, DispatchSemanticConsumer.REACTION_HOOKS)
        is DispatchSemanticConsumer.STOP_REACTION
    )
    assert _store(tmp_path).pending() == (
        replace(obligation, semantic_consumer=DispatchSemanticConsumer.APPROVAL_REPLY, requires_pending_check=True),
        replace(reaction, semantic_consumer=DispatchSemanticConsumer.STOP_REACTION, requires_pending_check=True),
    )


@pytest.mark.asyncio
async def test_semantic_consumer_claim_requires_durable_callback_context(tmp_path: Path) -> None:
    """Application ownership cannot be claimed outside its persisted callback."""
    runner = _runner(_store(tmp_path), AsyncMock(return_value=DispatchCallbackResult.SUCCEEDED))

    with pytest.raises(RuntimeError, match="only inside a durable callback"):
        await runner.claim_semantic_consumer(DispatchSemanticConsumer.APPROVAL_REPLY)


def test_existing_pending_payload_keeps_first_accepted_source(tmp_path: Path) -> None:
    """Transport-variant replays must keep the first durable source without failing."""
    store = _store(tmp_path)
    obligation = _message_obligation("$fixed")
    store.create_pending(obligation)
    replay_source = dict(obligation.event_source)
    replay_source["unsigned"] = {"age": 123}
    conflicting = replace(obligation, event_source=replay_source)

    assert store.create_pending(conflicting) is DispatchCreateResult.ALREADY_PENDING

    assert store.pending() == (obligation,)


def test_principal_and_entity_are_part_of_the_exact_identity(tmp_path: Path) -> None:
    """One account/entity must never observe another account/entity's pending callback."""
    code = _store(tmp_path)
    code.create_pending(_message_obligation("$isolated"))

    other_principal = _store(tmp_path, principal_id="@other:example.org")
    other_entity = _store(tmp_path, entity_name="other")

    assert other_principal.pending() == ()
    assert other_entity.pending() == ()
    assert not other_principal.has_pending("$isolated", DispatchCallbackKind.MESSAGE)
    assert not other_entity.has_pending("$isolated", DispatchCallbackKind.MESSAGE)


def test_turn_store_terminal_truth_tombstones_only_message_and_media_rows(tmp_path: Path) -> None:
    """Turn truth must block message/media replay without settling unrelated callbacks."""
    store = _store(tmp_path)
    message = _message_obligation("$turn")
    media = replace(message, callback_kind=DispatchCallbackKind.MEDIA)
    reaction = replace(message, callback_kind=DispatchCallbackKind.REACTION)
    for obligation in (message, media, reaction):
        store.create_pending(obligation)

    store.settle_from_turn_store("$turn", DispatchCallbackKind.MESSAGE)
    store.settle_from_turn_store("$turn", DispatchCallbackKind.MEDIA)

    assert not store.has_pending("$turn", DispatchCallbackKind.MESSAGE)
    assert not store.has_pending("$turn", DispatchCallbackKind.MEDIA)
    assert store.has_pending("$turn", DispatchCallbackKind.REACTION)
    assert store.create_pending(message) is DispatchCreateResult.ALREADY_TERMINAL
    assert store.create_pending(media) is DispatchCreateResult.ALREADY_TERMINAL
    assert store.create_pending(reaction) is DispatchCreateResult.ALREADY_PENDING
    with pytest.raises(ValueError, match="message or media"):
        store.settle_from_turn_store("$turn", DispatchCallbackKind.REACTION)


def test_turn_store_terminal_truth_creates_missing_compact_tombstone(tmp_path: Path) -> None:
    """TurnStore truth must permanently block exact replay even without a transient row."""
    store = _store(tmp_path)

    store.settle_from_turn_store("$turn-only", DispatchCallbackKind.MESSAGE)

    invalid_replay = replace(
        _message_obligation("$turn-only"),
        event_source={"event_id": "$turn-only", "not_json_safe": object()},
    )
    assert store.create_pending(invalid_replay) is DispatchCreateResult.ALREADY_TERMINAL
    database_path = _database_path(store)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT room_id, event_source_json, state FROM dispatch_obligations WHERE source_event_id = ?",
            ("$turn-only",),
        ).fetchone()
    assert row == ("", "", DispatchTerminalOutcome.SUCCEEDED.value)


def test_terminal_tombstones_are_not_globally_pruned(tmp_path: Path) -> None:
    """Settling new work must never evict an older exact terminal identity."""
    store = _store(tmp_path)
    trigger = _message_obligation("$trigger")
    store.create_pending(trigger)
    database_path = _database_path(store)
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO dispatch_obligations (
                principal_id,
                entity_name,
                source_event_id,
                callback_kind,
                room_id,
                event_source_json,
                state,
                created_at_ns,
                settled_at_ns
            ) VALUES (?, ?, ?, ?, '', '', ?, ?, ?)
            """,
            (
                (
                    _PRINCIPAL_ID,
                    _ENTITY_NAME,
                    f"$terminal-{index}",
                    DispatchCallbackKind.MESSAGE.value,
                    DispatchTerminalOutcome.SUCCEEDED.value,
                    index,
                    index,
                )
                for index in range(10_001)
            ),
        )

    store.settle(trigger.key, DispatchTerminalOutcome.SUCCEEDED)

    assert store.create_pending(_message_obligation("$terminal-0")) is DispatchCreateResult.ALREADY_TERMINAL


def test_malformed_persisted_source_is_not_invented_into_recovery(tmp_path: Path) -> None:
    """Invalid durable JSON must be logged, retained, and isolated from valid work."""
    store = _store(tmp_path)
    broken = _message_obligation("$broken")
    valid = _message_obligation("$valid")
    store.create_pending(broken)
    store.create_pending(valid)
    database_path = _database_path(store)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE dispatch_obligations SET event_source_json = ? WHERE source_event_id = ?",
            ("{", "$broken"),
        )

    restarted = _store(tmp_path)
    with patch("mindroom.dispatch_obligations.storage.logger") as logger:
        assert restarted.pending() == (valid,)

    logger.error.assert_called_once_with(
        "dispatch_obligation_pending_row_corrupt",
        source_event_id="$broken",
        callback_kind=DispatchCallbackKind.MESSAGE.value,
    )
    assert restarted.has_pending("$broken", DispatchCallbackKind.MESSAGE)
    assert restarted.unsettled_source_event_ids() == frozenset({"$broken", "$valid"})


@pytest.mark.asyncio
async def test_recovery_isolates_unreplayable_matrix_source(tmp_path: Path) -> None:
    """A parseable corrupt row must remain pending without blocking valid recovery."""
    store = _store(tmp_path)
    broken = _message_obligation("$broken-event")
    valid = _message_obligation("$valid-event")
    store.create_pending(broken)
    store.create_pending(valid)
    with sqlite3.connect(_database_path(store)) as connection:
        connection.execute(
            "UPDATE dispatch_obligations SET event_source_json = ? WHERE source_event_id = ?",
            (
                '{"content":{"body":"bad","msgtype":"m.text"},"event_id":"$different",'
                '"origin_server_ts":1234,"sender":"@user:example.org","type":"m.room.message"}',
                "$broken-event",
            ),
        )
    recovered: list[str] = []

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        recovered.append(event.event_id)
        return DispatchCallbackResult.SUCCEEDED

    with patch("mindroom.dispatch_obligations.runner.logger") as logger:
        await _runner(store, callback).recover_pending()

    assert recovered == ["$valid-event"]
    logger.error.assert_called_once_with(
        "dispatch_obligation_recovery_corrupt",
        source_event_id="$broken-event",
        callback_kind=DispatchCallbackKind.MESSAGE.value,
        room_id=_ROOM_ID,
    )
    assert store.has_pending("$broken-event", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_cancellation_leaves_callback_obligation_pending(tmp_path: Path) -> None:
    """Cancellation after callback entry must leave exact work for restart recovery."""
    entered = asyncio.Event()
    blocker = asyncio.Event()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        entered.set()
        await blocker.wait()
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(_store(tmp_path), callback)
    task = asyncio.create_task(
        runner.dispatch(
            nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
            _message_event("$cancelled"),
            DispatchCallbackKind.MESSAGE,
        ),
    )
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _store(tmp_path).has_pending("$cancelled", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_cancelled_store_operation_preserves_cancellation_when_worker_fails() -> None:
    """A drained worker failure must not replace the caller's cancellation."""
    worker_started = threading.Event()
    release_worker = threading.Event()

    def failing_store_operation() -> None:
        worker_started.set()
        assert release_worker.wait(timeout=5)
        message = "store write failed"
        raise RuntimeError(message)

    task = asyncio.create_task(run_blocking_until_complete(failing_store_operation))
    assert await asyncio.to_thread(worker_started.wait, 5)

    task.cancel()
    release_worker.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert task.cancelled()
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "store write failed"


@pytest.mark.asyncio
async def test_cancelled_store_operation_preserves_caller_cancellation_when_worker_task_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled worker wrapper must stay the cause of the caller's cancellation."""
    worker_started = threading.Event()
    release_worker = threading.Event()
    original_create_task = asyncio.create_task
    worker_tasks: list[asyncio.Task[None]] = []

    def capture_worker_task(coro: Coroutine[object, object, None]) -> asyncio.Task[None]:
        task = original_create_task(coro)
        worker_tasks.append(task)
        return task

    def blocking_store_operation() -> None:
        worker_started.set()
        assert release_worker.wait(timeout=5)

    monkeypatch.setattr("mindroom.background_tasks.asyncio.create_task", capture_worker_task)
    caller_task = original_create_task(run_blocking_until_complete(blocking_store_operation))
    assert await asyncio.to_thread(worker_started.wait, 5)

    caller_task.cancel("caller cancelled")
    await asyncio.sleep(0)
    assert len(worker_tasks) == 1
    worker_tasks[0].cancel("worker cancelled")
    release_worker.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await caller_task

    assert exc_info.value.args == ("caller cancelled",)
    assert isinstance(exc_info.value.__cause__, asyncio.CancelledError)
    assert exc_info.value.__cause__.args == ("worker cancelled",)


@pytest.mark.asyncio
async def test_callback_settlement_drains_repeated_cancellation_before_releasing_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate cannot reclaim one exact key while its cancelled settlement still writes."""
    attempts = 0

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(store, callback)
    original_settle = store.settle
    settle_started = threading.Event()
    release_settle = threading.Event()
    settle_calls = 0

    def blocking_first_settle(
        key: object,
        outcome: DispatchTerminalOutcome,
    ) -> None:
        nonlocal settle_calls
        settle_calls += 1
        if settle_calls == 1:
            settle_started.set()
            assert release_settle.wait(timeout=2)
        original_settle(cast("Any", key), outcome)

    monkeypatch.setattr(store, "settle", blocking_first_settle)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$cancelled-settlement")
    task = asyncio.create_task(runner.dispatch(room, event, DispatchCallbackKind.MESSAGE))
    duplicate: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(settle_started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        duplicate = asyncio.create_task(runner.dispatch(room, event, DispatchCallbackKind.MESSAGE))
        await asyncio.wait_for(duplicate, timeout=1)

        assert attempts == 1
        assert not task.done()
    finally:
        release_settle.set()
        if duplicate is not None:
            await asyncio.gather(duplicate, return_exceptions=True)

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.create_pending(_message_obligation(event.event_id)) is DispatchCreateResult.ALREADY_TERMINAL


@pytest.mark.asyncio
async def test_turn_store_settlement_drains_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation cannot outrun a TurnStore-owned permanent tombstone write."""

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        pytest.fail("terminal TurnStore truth must bypass callback execution")

    store = _store(tmp_path)
    runner = _runner(store, callback, turn_is_terminal=lambda _event_id: True)
    original_settle = store.settle_from_turn_store
    settle_started = threading.Event()
    release_settle = threading.Event()
    settle_finished = threading.Event()

    def blocking_settle(source_event_id: str, callback_kind: DispatchCallbackKind) -> None:
        settle_started.set()
        assert release_settle.wait(timeout=2)
        original_settle(source_event_id, callback_kind)
        settle_finished.set()

    monkeypatch.setattr(store, "settle_from_turn_store", blocking_settle)
    task = asyncio.create_task(
        runner.persist(
            nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
            _message_event("$turn-store-cancelled"),
            DispatchCallbackKind.MESSAGE,
        ),
    )
    try:
        assert await asyncio.to_thread(settle_started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()

        assert not task.done()
    finally:
        release_settle.set()
        assert await asyncio.to_thread(settle_finished.wait, 2)

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.create_pending(_message_obligation("$turn-store-cancelled")) is DispatchCreateResult.ALREADY_TERMINAL


@pytest.mark.asyncio
async def test_persisted_work_can_be_scheduled_after_durable_acceptance(tmp_path: Path) -> None:
    """The sync callback must be able to persist before creating background work."""
    attempts = 0

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(store, callback)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$scheduled")

    obligation = await runner.persist(room, event, DispatchCallbackKind.MESSAGE)

    assert obligation is not None
    assert store.has_pending("$scheduled", DispatchCallbackKind.MESSAGE)
    assert attempts == 0

    await runner._run_persisted(obligation, room=room, event=event)

    assert attempts == 1
    assert not store.has_pending("$scheduled", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_pending_duplicate_runs_first_durably_accepted_payload(tmp_path: Path) -> None:
    """A conflicting duplicate must execute the payload already accepted on disk."""
    received: list[tuple[str, str]] = []

    async def callback(room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        assert isinstance(event, nio.RoomMessageText)
        received.append((room.room_id, event.body))
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(store, callback)
    first_room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    first_event = _message_event("$first-payload")
    assert await runner.persist(first_room, first_event, DispatchCallbackKind.MESSAGE) is not None

    conflicting_source = dict(first_event.source)
    conflicting_source["content"] = {"msgtype": "m.text", "body": "conflicting"}
    conflicting_event = nio.Event.parse_event(conflicting_source)
    assert isinstance(conflicting_event, nio.RoomMessageText)

    await runner.dispatch(
        nio.MatrixRoom("!conflicting:example.org", _PRINCIPAL_ID),
        conflicting_event,
        DispatchCallbackKind.MESSAGE,
    )

    assert received == [(_ROOM_ID, "hello")]


@pytest.mark.asyncio
async def test_failed_callback_retries_directly_without_later_sync_response(tmp_path: Path) -> None:
    """Restart recovery must invoke pending work from durable input alone."""
    attempts = 0

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "worker failed"
            raise RuntimeError(message)
        return DispatchCallbackResult.SUCCEEDED

    first = _runner(_store(tmp_path), callback)
    with pytest.raises(RuntimeError, match="worker failed"):
        await first.dispatch(
            nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
            _message_event("$retry"),
            DispatchCallbackKind.MESSAGE,
        )

    restarted = _runner(_store(tmp_path), callback)
    await restarted.recover_pending()

    assert attempts == 2
    assert not _store(tmp_path).has_pending("$retry", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_recovery_failure_retries_autonomously_without_blocking_later_work(tmp_path: Path) -> None:
    """One failed recovery row must retry without parking later durable work."""
    attempts: list[str] = []
    failed_attempts = {"$first", "$second"}
    retries_finished = asyncio.Event()

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        attempts.append(event.event_id)
        if event.event_id in failed_attempts and attempts.count(event.event_id) == 1:
            message = "transient worker failure"
            raise RuntimeError(message)
        if event.event_id == "$second":
            retries_finished.set()
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    store.create_pending(_message_obligation("$first"))
    store.create_pending(_message_obligation("$second"))
    store.create_pending(_message_obligation("$later"))
    retry_owner = object()
    runner = _runner(
        store,
        callback,
        background_task_owner=retry_owner,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    await runner.recover_pending()

    assert attempts == ["$first", "$second", "$later"]
    await asyncio.wait_for(retries_finished.wait(), timeout=1)
    await wait_for_background_tasks(timeout=1, owner=retry_owner)
    assert attempts == ["$first", "$second", "$later", "$first", "$second"]
    assert not store.has_pending("$first", DispatchCallbackKind.MESSAGE)
    assert not store.has_pending("$second", DispatchCallbackKind.MESSAGE)
    assert not store.has_pending("$later", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_autonomous_retry_continues_until_transient_failure_recovers(tmp_path: Path) -> None:
    """Ordinary callback failures must stay retry-owned beyond the old attempt cap."""
    attempts = 0
    retry_owner = object()
    recovered = asyncio.Event()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        if attempts <= 6:
            message = "transient worker failure"
            raise RuntimeError(message)
        recovered.set()
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(
        store,
        callback,
        background_task_owner=retry_owner,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    with pytest.raises(RuntimeError, match="transient worker failure"):
        await runner.dispatch(
            nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
            _message_event("$bounded-retry"),
            DispatchCallbackKind.MESSAGE,
        )
    await asyncio.wait_for(recovered.wait(), timeout=1)
    await wait_for_background_tasks(timeout=1, owner=retry_owner)

    assert attempts == 7
    assert not store.has_pending("$bounded-retry", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_retry_worker_survives_transient_pending_discovery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed liveness read must leave the exact retry owned by the same worker."""
    store = _store(tmp_path)
    obligation = _message_obligation("$retry-read")
    store.create_pending(obligation)
    owner = object()
    callback = AsyncMock(return_value=DispatchCallbackResult.SUCCEEDED)
    runner = _runner(
        store,
        callback,
        background_task_owner=owner,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
    )
    real_has_pending = store.has_pending
    reads = 0

    def flaky_has_pending(source_event_id: str, callback_kind: DispatchCallbackKind) -> bool:
        nonlocal reads
        reads += 1
        if reads == 1:
            message = "transient sqlite read failure"
            raise OSError(message)
        return real_has_pending(source_event_id, callback_kind)

    monkeypatch.setattr(store, "has_pending", flaky_has_pending)

    runner._schedule_retry(obligation.key)
    await wait_for_background_tasks(timeout=1, owner=owner)

    assert reads >= 2
    callback.assert_awaited_once()
    assert not real_has_pending(obligation.source_event_id, obligation.callback_kind)


@pytest.mark.asyncio
async def test_replay_observes_consumer_claimed_before_interrupted_side_effect(tmp_path: Path) -> None:
    """A crash after a side effect must not let replay rediscover another consumer."""
    store = _store(tmp_path)
    obligation = _message_obligation("$claimed-before-crash")
    store.create_pending(obligation)
    first_runner: DispatchObligationRunner

    async def interrupted_callback(
        _room: nio.MatrixRoom,
        _event: nio.Event,
    ) -> DispatchCallbackResult:
        await first_runner.claim_semantic_consumer(DispatchSemanticConsumer.APPROVAL_REPLY)
        message = "crash after approval side effect"
        raise RuntimeError(message)

    first_runner = _runner(
        store,
        interrupted_callback,
        retry_initial_delay_seconds=3_600,
        retry_max_delay_seconds=3_600,
    )
    await first_runner.recover_pending()
    retry_task = first_runner._retry_task
    assert retry_task is not None
    retry_task.cancel()
    with suppress(asyncio.CancelledError):
        await retry_task

    replayed_consumers: list[DispatchSemanticConsumer | None] = []
    restarted_store = _store(tmp_path)
    restarted_runner: DispatchObligationRunner

    async def replay_callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        replayed_consumers.append(restarted_runner.semantic_consumer())
        with pytest.raises(ValueError, match="cannot consume"):
            await restarted_runner.claim_semantic_consumer(DispatchSemanticConsumer.REACTION_HOOKS)
        return DispatchCallbackResult.SUCCEEDED

    restarted_runner = _runner(restarted_store, replay_callback)
    await restarted_runner.recover_pending()

    assert replayed_consumers == [DispatchSemanticConsumer.APPROVAL_REPLY]
    assert not restarted_store.has_pending(obligation.source_event_id, obligation.callback_kind)


@pytest.mark.asyncio
async def test_encrypted_media_recovery_uses_media_source_parser(tmp_path: Path) -> None:
    """Direct recovery must reconstruct encrypted media without a new sync response."""
    store = _store(tmp_path)
    event_id = "$encrypted-media"
    obligation = replace(
        _message_obligation(event_id),
        callback_kind=DispatchCallbackKind.MEDIA,
        event_source=_encrypted_image_source(event_id),
    )
    store.create_pending(obligation)
    recovered: list[nio.Event] = []

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        recovered.append(event)
        return DispatchCallbackResult.SUCCEEDED

    runner = DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.MEDIA: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, _PRINCIPAL_ID),
        turn_is_terminal=lambda _event_id: False,
    )

    await runner.recover_pending()

    assert len(recovered) == 1
    assert isinstance(recovered[0], nio.RoomEncryptedImage)
    assert not store.has_pending(event_id, DispatchCallbackKind.MEDIA)


@pytest.mark.asyncio
async def test_decrypted_message_recovery_preserves_nio_security_metadata(tmp_path: Path) -> None:
    """Restart replay must retain nio's authenticated decrypted-event facts."""
    received: list[tuple[bool, bool, str | None, str | None, str | None]] = []

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        received.append(
            (
                event.decrypted,
                event.verified,
                event.sender_key,
                event.session_id,
                cast("_RoomIdEvent", event).room_id,
            ),
        )
        return DispatchCallbackResult.SUCCEEDED

    event = _message_event("$decrypted-message")
    event.decrypted = True
    event.verified = True
    event.sender_key = "curve25519:sender"
    event.session_id = "megolm-session"
    cast("_RoomIdEvent", event).room_id = _ROOM_ID
    store = _store(tmp_path)
    first_runner = _runner(store, callback)
    await first_runner.persist(
        nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
        event,
        DispatchCallbackKind.MESSAGE,
    )

    await _runner(_store(tmp_path), callback).recover_pending()

    assert received == [(True, True, "curve25519:sender", "megolm-session", _ROOM_ID)]


@pytest.mark.asyncio
async def test_megolm_recovery_restores_room_id_before_key_request(tmp_path: Path) -> None:
    """Recovered undecryptable events need their durable room for key requests."""
    event_id = "$undecryptable"
    store = _store(tmp_path)
    store.create_pending(
        replace(
            _message_obligation(event_id),
            callback_kind=DispatchCallbackKind.DECRYPTION_FAILURE,
            event_source={
                "type": "m.room.encrypted",
                "event_id": event_id,
                "sender": "@user:example.org",
                "origin_server_ts": 1_234,
                "content": {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "ciphertext": "ciphertext",
                    "device_id": "DEVICE",
                    "sender_key": "curve25519:sender",
                    "session_id": "megolm-session",
                },
            },
        ),
    )
    request_room_key = AsyncMock()

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        assert isinstance(event, nio.MegolmEvent)
        await request_room_key(event)
        return DispatchCallbackResult.SUCCEEDED

    runner = DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.DECRYPTION_FAILURE: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, _PRINCIPAL_ID),
        turn_is_terminal=lambda _event_id: False,
    )

    await runner.recover_pending()

    recovered_event = request_room_key.await_args.args[0]
    assert recovered_event.room_id == _ROOM_ID


@pytest.mark.asyncio
async def test_concurrent_duplicate_dispatch_runs_callback_once(tmp_path: Path) -> None:
    """Live and recovery delivery of one exact key must not execute concurrently."""
    entered = asyncio.Event()
    release = asyncio.Event()
    attempts = 0

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        entered.set()
        await release.wait()
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(_store(tmp_path), callback)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$duplicate")
    first = asyncio.create_task(runner.dispatch(room, event, DispatchCallbackKind.MESSAGE))
    await entered.wait()

    await runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)
    assert attempts == 1

    release.set()
    await first

    assert not _store(tmp_path).has_pending("$duplicate", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_queued_duplicate_does_not_run_after_first_copy_settles(tmp_path: Path) -> None:
    """A duplicate queued before settlement must not execute after the active claim releases."""
    attempts = 0

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(store, callback)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$queued-duplicate")
    first = await runner.persist(room, event, DispatchCallbackKind.MESSAGE)
    duplicate = await runner.persist(room, event, DispatchCallbackKind.MESSAGE)
    assert first is not None
    assert duplicate is not None

    await runner._run_persisted(first, room=room, event=event)
    await runner._run_persisted(duplicate, room=room, event=event)

    assert attempts == 1


@pytest.mark.asyncio
async def test_intentional_ignore_is_explicit_terminal_outcome(tmp_path: Path) -> None:
    """A callback may suppress replay only by explicitly declaring intentional ignore."""

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.INTENTIONALLY_IGNORED

    runner = _runner(_store(tmp_path), callback)
    event = _message_event("$ignored")
    await runner.dispatch(
        nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
        event,
        DispatchCallbackKind.MESSAGE,
    )
    await runner.dispatch(
        nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
        event,
        DispatchCallbackKind.MESSAGE,
    )

    assert not _store(tmp_path).has_pending("$ignored", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_turn_store_terminal_truth_replaces_message_obligation(tmp_path: Path) -> None:
    """Handled-turn truth must settle the transient message obligation without duplicate work."""
    handled: set[str] = set()
    attempts = 0

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        handled.add(event.event_id)
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(_store(tmp_path), callback, turn_is_terminal=handled.__contains__)
    event = _message_event("$handled")
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)

    await runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)
    await runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)

    assert attempts == 1
    assert not _store(tmp_path).has_pending("$handled", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_deferred_message_remains_pending_until_turn_store_is_terminal(tmp_path: Path) -> None:
    """Queue acceptance alone must not settle work before downstream dispatch finishes."""
    handled: set[str] = set()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.DEFERRED

    runner = _runner(
        _store(tmp_path),
        callback,
        turn_is_terminal=handled.__contains__,
    )
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$deferred")

    await runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)
    assert _store(tmp_path).has_pending("$deferred", DispatchCallbackKind.MESSAGE)

    handled.add("$deferred")
    await runner.recover_pending()

    assert not _store(tmp_path).has_pending("$deferred", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_terminal_turn_does_not_skip_failed_callback_tail(tmp_path: Path) -> None:
    """A callback that fails after terminal turn persistence must rerun its unfinished tail."""
    handled: set[str] = set()
    attempts = 0

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        handled.add(event.event_id)
        if attempts == 1:
            msg = "post-turn callback tail failed"
            raise RuntimeError(msg)
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(store, callback, turn_is_terminal=handled.__contains__)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$failed-tail")
    obligation = await runner.persist(room, event, DispatchCallbackKind.MESSAGE)
    assert obligation is not None

    with pytest.raises(RuntimeError, match="post-turn callback tail failed"):
        await runner._run_persisted(obligation, room=room, event=event)

    await runner.recover_pending()

    assert attempts == 2
    assert not store.has_pending(event.event_id, DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_recovery_readiness_retains_only_blocked_obligations(tmp_path: Path) -> None:
    """A blocked runtime dependency must not park unrelated durable callbacks."""
    ready_room_id = "!ready:example.org"
    blocked_room_id = "!blocked:example.org"
    store = _store(tmp_path)
    ready = _message_obligation("$ready", room_id=ready_room_id)
    blocked = _message_obligation("$blocked", room_id=blocked_room_id)
    store.create_pending(ready)
    store.create_pending(blocked)
    recovered: list[str] = []

    async def callback(_room: nio.MatrixRoom, event: nio.Event) -> DispatchCallbackResult:
        if event.event_id == "$blocked":
            return DispatchCallbackResult.DEFERRED
        recovered.append(event.event_id)
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(store, callback)
    await runner.recover_pending(turn_backed=True)

    assert recovered == ["$ready"]
    assert not store.has_pending("$ready", DispatchCallbackKind.MESSAGE)
    assert store.has_pending("$blocked", DispatchCallbackKind.MESSAGE)
    assert not runner._retry_keys


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_outcome", "callback_result"),
    [
        (TurnDispatchOutcome.DEFERRED, DispatchCallbackResult.DEFERRED),
        (TurnDispatchOutcome.INTENTIONALLY_IGNORED, DispatchCallbackResult.INTENTIONALLY_IGNORED),
    ],
)
async def test_bound_message_callback_uses_explicit_turn_disposition(
    turn_outcome: TurnDispatchOutcome,
    callback_result: DispatchCallbackResult,
) -> None:
    """Typed callbacks use the turn controller's explicit ownership disposition."""

    async def on_message(_room: nio.MatrixRoom, _event: nio.RoomMessageText) -> TurnDispatchOutcome:
        return turn_outcome

    async def noop(_room: nio.MatrixRoom, _event: nio.Event) -> None:
        pass

    callbacks = DispatchObligationRunner.callbacks_for(
        on_message=on_message,
        on_media=cast("Any", noop),
        on_reaction=cast("Any", noop),
        on_approval=cast("Any", noop),
        on_invite=cast("Any", noop),
        on_room_lifecycle=cast("Any", noop),
        on_redaction=cast("Any", noop),
        on_decryption_failure=cast("Any", noop),
        source_has_live_owner=lambda _event_id: False,
    )
    callback = callbacks[DispatchCallbackKind.MESSAGE]
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$bound")

    assert await callback(room, event) is callback_result


@pytest.mark.asyncio
async def test_sequential_media_dispatch_does_not_reenter_deferred_source(tmp_path: Path) -> None:
    """A lane-owned media source must enter downstream dispatch only once."""
    deferred_sources: set[str] = set()
    queued_media_events: list[MatrixMediaEvent] = []

    async def noop(_room: nio.MatrixRoom, _event: nio.Event) -> None:
        pass

    async def on_media(_room: nio.MatrixRoom, event: MatrixMediaEvent) -> TurnDispatchOutcome:
        if event.event_id in deferred_sources:
            return TurnDispatchOutcome.DEFERRED
        queued_media_events.append(event)
        deferred_sources.add(event.event_id)
        return TurnDispatchOutcome.DEFERRED

    callbacks = DispatchObligationRunner.callbacks_for(
        on_message=cast("Any", noop),
        on_media=on_media,
        on_reaction=cast("Any", noop),
        on_approval=cast("Any", noop),
        on_invite=cast("Any", noop),
        on_room_lifecycle=cast("Any", noop),
        on_redaction=cast("Any", noop),
        on_decryption_failure=cast("Any", noop),
        source_has_live_owner=deferred_sources.__contains__,
    )
    store = _store(tmp_path)
    runner = DispatchObligationRunner(
        store=store,
        callbacks=callbacks,
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, _PRINCIPAL_ID),
        turn_is_terminal=lambda _event_id: False,
    )
    event_id = "$deferred-media"
    event = parse_matrix_media_event_source(_encrypted_image_source(event_id))
    assert isinstance(event, nio.RoomEncryptedImage)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)

    await runner.dispatch(room, event, DispatchCallbackKind.MEDIA)
    await runner.dispatch(room, event, DispatchCallbackKind.MEDIA)

    assert [queued.event_id for queued in queued_media_events] == [event_id]
    assert store.has_pending(event_id, DispatchCallbackKind.MEDIA)


@pytest.mark.asyncio
async def test_retry_survives_contention_with_callback_marking_deferred(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry scheduled by downstream failure must survive its callback's live claim."""
    event_id = "$retry-while-active"
    obligation = _message_obligation(event_id)
    store = _store(tmp_path)
    assert store.create_pending(obligation) is DispatchCreateResult.CREATED
    attempts = 0
    retry_contended = asyncio.Event()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            runner._schedule_retry(obligation.key)
            await asyncio.wait_for(retry_contended.wait(), timeout=1)
            return DispatchCallbackResult.DEFERRED
        return DispatchCallbackResult.INTENTIONALLY_IGNORED

    runner = _runner(
        store,
        callback,
        retry_initial_delay_seconds=0.0,
        retry_max_delay_seconds=0.0,
    )
    original_claim = runner._claim

    async def track_claim_contention(key: object) -> bool:
        claimed = await original_claim(cast("Any", key))
        if not claimed:
            retry_contended.set()
        return claimed

    monkeypatch.setattr(runner, "_claim", track_claim_contention)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)

    await runner._run_persisted(obligation, room=room, event=_message_event(event_id))
    await asyncio.wait_for(wait_for_background_tasks(), timeout=1)

    assert attempts == 2
    assert not store.has_pending(event_id, DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provenance", "cached"),
    [
        (nio.TimelineEventProvenance.LIVE, False),
        (nio.TimelineEventProvenance.RECOVERED, True),
        (nio.TimelineEventProvenance.HISTORY, True),
    ],
)
async def test_only_live_events_skip_the_admission_history_cache(
    tmp_path: Path,
    provenance: nio.TimelineEventProvenance,
    cached: bool,
) -> None:
    """Live events reach the cache through sync; every other provenance must not."""
    cache = AsyncMock(return_value=None)

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(_store(tmp_path), callback, cache_historical_event=cache)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event(f"$provenance-{provenance.value}")

    await runner._admit_source_event(room, event, provenance)

    assert cache.await_count == int(cached)


@pytest.mark.asyncio
async def test_admission_persists_once_before_event_callback_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Admission must persist once; the later event callback may only execute it."""
    entered = asyncio.Event()
    release = asyncio.Event()
    owner = object()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        entered.set()
        await release.wait()
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(store, callback)
    create_pending = MagicMock(wraps=store.create_pending)
    monkeypatch.setattr(store, "create_pending", create_pending)
    admission = runner._admit_source_event
    wrapper = runner.task_wrapper(DispatchCallbackKind.MESSAGE, owner=owner)
    event = _message_event("$durable")
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)

    await admission(room, event, nio.TimelineEventProvenance.LIVE)

    assert store.has_pending("$durable", DispatchCallbackKind.MESSAGE)
    assert not entered.is_set()
    await wrapper(room, event)
    await entered.wait()
    release.set()
    await wait_for_background_tasks(timeout=1.0, owner=owner)
    create_pending.assert_called_once()
    assert not store.has_pending("$durable", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_live_admission_and_callback_use_two_sqlite_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal callback must write admission and settlement without intermediate reloads."""

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.SUCCEEDED

    owner = object()
    store = _store(tmp_path)
    runner = _runner(store, callback)
    connect = MagicMock(wraps=store._connect)
    monkeypatch.setattr(store, "_connect", connect)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$two-connections")

    await runner._admit_source_event(room, event, nio.TimelineEventProvenance.LIVE)
    await runner.task_wrapper(DispatchCallbackKind.MESSAGE, owner=owner)(room, event)
    await wait_for_background_tasks(timeout=1.0, owner=owner)

    assert connect.call_count == 2


@pytest.mark.asyncio
async def test_task_wrapper_failure_retries_autonomously(tmp_path: Path) -> None:
    """A failed live callback task must retain autonomous retry ownership."""
    attempts = 0
    owner = object()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "transient worker failure"
            raise RuntimeError(message)
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(
        store,
        callback,
        background_task_owner=owner,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$task-retry")
    await runner._admit_source_event(room, event, nio.TimelineEventProvenance.LIVE)
    await runner.task_wrapper(DispatchCallbackKind.MESSAGE, owner=owner)(room, event)
    await wait_for_background_tasks(timeout=1, owner=owner)

    assert attempts == 2
    assert not store.has_pending("$task-retry", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_direct_dispatch_failure_retries_autonomously(tmp_path: Path) -> None:
    """A failed direct callback must retain same-runtime retry ownership."""
    attempts = 0
    owner = object()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "transient worker failure"
            raise RuntimeError(message)
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(
        store,
        callback,
        background_task_owner=owner,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    with pytest.raises(RuntimeError, match="transient worker failure"):
        await runner.dispatch(
            nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
            _message_event("$direct-retry"),
            DispatchCallbackKind.MESSAGE,
        )
    await wait_for_background_tasks(timeout=1, owner=owner)

    assert attempts == 2
    assert not store.has_pending("$direct-retry", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_turn_callback_retry_preserves_recovery_deferral(tmp_path: Path) -> None:
    """A retried turn must not settle work that startup replay would defer."""
    attempts = 0
    owner = object()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            message = "transient worker failure"
            raise RuntimeError(message)
        return DispatchCallbackResult.DEFERRED if turn_dispatch_recovery_active() else DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(
        store,
        callback,
        background_task_owner=owner,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
    )

    with pytest.raises(RuntimeError, match="transient worker failure"):
        await runner.dispatch(
            nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
            _message_event("$recovery-scope"),
            DispatchCallbackKind.MESSAGE,
        )
    await wait_for_background_tasks(timeout=1, owner=owner)

    assert attempts == 2
    assert store.has_pending("$recovery-scope", DispatchCallbackKind.MESSAGE)


@pytest.mark.asyncio
async def test_repeated_pending_turn_admission_has_recovery_context(tmp_path: Path) -> None:
    """A live duplicate of persisted turn work must use startup-replay semantics."""
    observed_context: list[bool] = []

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        observed_context.append(turn_dispatch_recovery_active())
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    store.create_pending(_message_obligation("$repeated-message"))
    runner = _runner(store, callback)

    await runner.dispatch(
        nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
        _message_event("$repeated-message"),
        DispatchCallbackKind.MESSAGE,
    )

    assert observed_context == [True]
    assert not store.has_pending("$repeated-message", DispatchCallbackKind.MESSAGE)


def test_deferred_turn_retry_schedules_only_the_exact_callback_kind(tmp_path: Path) -> None:
    """A failed gate handoff must not fabricate a second callback identity."""

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(_store(tmp_path), callback)
    runner._schedule_retry = MagicMock()

    runner.retry_pending_turn_source("$media", DispatchCallbackKind.MEDIA)

    runner._schedule_retry.assert_called_once()
    key = runner._schedule_retry.call_args.args[0]
    assert key.source_event_id == "$media"
    assert key.callback_kind is DispatchCallbackKind.MEDIA


@pytest.mark.asyncio
async def test_deferred_turn_retry_without_obligation_skips_backoff(tmp_path: Path) -> None:
    """A stale retry request must not hold runtime shutdown through its backoff."""

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.SUCCEEDED

    owner = object()
    runner = _runner(
        _store(tmp_path),
        callback,
        background_task_owner=owner,
        retry_initial_delay_seconds=60,
        retry_max_delay_seconds=60,
    )

    runner.retry_pending_turn_source("$missing", DispatchCallbackKind.MESSAGE)

    assert await wait_for_background_tasks(timeout=1, owner=owner) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["direct", "admission"])
async def test_persist_failure_notifies_once_for_every_runner_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
) -> None:
    """Direct and task-backed acceptance must share one persistence-failure boundary."""
    failure_notifications = 0

    def notify_failure() -> None:
        nonlocal failure_notifications
        failure_notifications += 1

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.MESSAGE: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, _PRINCIPAL_ID),
        turn_is_terminal=lambda _event_id: False,
        on_persist_failure=notify_failure,
    )

    def fail_create(_obligation: DispatchObligation) -> DispatchCreateResult:
        message = "dispatch database unavailable"
        raise OSError(message)

    monkeypatch.setattr(store, "create_pending", fail_create)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$persist-failure")

    if entrypoint == "direct":
        persist = runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)
    else:
        persist = runner._admit_source_event(room, event, nio.TimelineEventProvenance.LIVE)
    expected_error = OSError if entrypoint == "direct" else nio.CallbackNotAcceptedError
    with pytest.raises(expected_error, match="dispatch database unavailable") as exc_info:
        await persist

    if entrypoint == "admission":
        assert isinstance(exc_info.value.__cause__, OSError)
    assert failure_notifications == 1


@pytest.mark.asyncio
async def test_source_callbacks_register_one_real_nio_admission_owner(tmp_path: Path) -> None:
    """MindRoom must compose every durable event kind behind nio's single owner."""

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(_store(tmp_path), callback)
    client = nio.AsyncClient(
        "https://example.org",
        _PRINCIPAL_ID,
        config=nio.AsyncClientConfig(backfill_limited_timelines=True),
    )

    try:
        with patch.object(
            client,
            "add_event_admission_callback",
            wraps=client.add_event_admission_callback,
        ) as add_admission:
            runner.register_source_callbacks(client, owner=object())
    finally:
        await client.close()

    add_admission.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "expected_attempts"),
    [
        ("io.example.unrelated", 0),
        ("io.mindroom.tool_approval_response", 1),
    ],
)
async def test_only_tool_approval_unknown_event_reaches_durable_acceptance(
    tmp_path: Path,
    event_type: str,
    expected_attempts: int,
) -> None:
    """Only the exact custom approval event type may reach the durable callback."""
    attempts = 0
    observed_provenance: list[tuple[str, nio.TimelineEventProvenance]] = []

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        nonlocal attempts
        attempts += 1
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.APPROVAL: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, _PRINCIPAL_ID),
        turn_is_terminal=lambda _event_id: False,
        observe_event_provenance=lambda event_id, provenance: observed_provenance.append(
            (event_id, provenance),
        ),
    )
    client = MagicMock()
    owner = object()
    runner.register_source_callbacks(client, owner=owner)
    admission = client.add_event_admission_callback.call_args.args[0]
    registered = next(
        callback
        for callback, event_type in (call.args for call in client.add_event_callback.call_args_list)
        if event_type is nio.UnknownEvent
    )

    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _unknown_event("$unknown", event_type)
    await admission(room, event, nio.TimelineEventProvenance.LIVE)
    await registered(room, event)
    await wait_for_background_tasks(timeout=1.0, owner=owner)

    assert attempts == expected_attempts
    assert observed_provenance == [
        ("$unknown", nio.TimelineEventProvenance.LIVE),
    ]
    assert not store.has_pending("$unknown", DispatchCallbackKind.APPROVAL)
