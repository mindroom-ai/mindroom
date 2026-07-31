"""Durable exact Matrix callback obligations and restart recovery."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import MagicMock

import nio
import pytest

from mindroom.background_tasks import wait_for_background_tasks
from mindroom.dispatch_obligations import (
    DispatchCallbackKind,
    DispatchObligationRunner,
    DispatchObligationStore,
    _DispatchCreateResult,
    _DispatchObligation,
    _DispatchTerminalOutcome,
    _run_owned_store_operation,
)
from mindroom.dispatch_obligations import (
    _DispatchCallbackResult as DispatchCallbackResult,
)
from mindroom.dispatch_obligations import (
    _DispatchObligationTaskWrapper as DispatchObligationTaskWrapper,
)
from mindroom.matrix.media import MATRIX_MEDIA_EVENT_TYPES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
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


def _message_obligation(
    event_id: str,
    *,
    principal_id: str = _PRINCIPAL_ID,
    entity_name: str = _ENTITY_NAME,
) -> _DispatchObligation:
    return _DispatchObligation(
        principal_id=principal_id,
        entity_name=entity_name,
        source_event_id=event_id,
        callback_kind=DispatchCallbackKind.MESSAGE,
        room_id=_ROOM_ID,
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


def _runner(
    store: DispatchObligationStore,
    callback: Callable[[nio.MatrixRoom, nio.Event], Awaitable[DispatchCallbackResult]],
    *,
    turn_is_terminal: Callable[[str], bool] = lambda _event_id: False,
    background_task_owner: object | None = None,
    retry_initial_delay_seconds: float = 1.0,
    retry_max_delay_seconds: float = 30.0,
) -> DispatchObligationRunner:
    return DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.MESSAGE: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, "@code:example.org"),
        turn_is_terminal=turn_is_terminal,
        background_task_owner=background_task_owner,
        _retry_initial_delay_seconds=retry_initial_delay_seconds,
        _retry_max_delay_seconds=retry_max_delay_seconds,
    )


def test_pending_row_survives_new_store_instance(tmp_path: Path) -> None:
    """Dropping process memory must not drop callback work already accepted."""
    first = _store(tmp_path)
    obligation = _message_obligation("$message")

    assert first.create_pending(obligation) is _DispatchCreateResult.CREATED

    restarted = _store(tmp_path)

    assert restarted.pending() == (obligation,)
    assert restarted.has_pending("$message", DispatchCallbackKind.MESSAGE)


def test_pending_recovery_query_uses_pending_order_index(tmp_path: Path) -> None:
    """Permanent tombstones must not be scanned or sorted to recover pending work."""
    _store(tmp_path)
    database_path = tmp_path / "tracking" / "dispatch_obligations.sqlite3"
    with sqlite3.connect(database_path) as connection:
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT source_event_id, callback_kind, room_id, event_source_json
            FROM dispatch_obligations
            WHERE principal_id = ?
              AND entity_name = ?
              AND state = ?
            ORDER BY created_at_ns, rowid
            """,
            (_PRINCIPAL_ID, _ENTITY_NAME, "pending"),
        ).fetchall()

    details = tuple(row[3] for row in plan)
    assert any("USING INDEX dispatch_obligations_pending_recovery" in detail for detail in details)
    assert all("USE TEMP B-TREE" not in detail for detail in details)


def test_exact_callback_kind_keeps_distinct_obligations_for_one_event(tmp_path: Path) -> None:
    """Two callback purposes for one Matrix event must not settle each other."""
    store = _store(tmp_path)
    message = _message_obligation("$same")
    approval = replace(message, callback_kind=DispatchCallbackKind.APPROVAL)

    assert store.create_pending(message) is _DispatchCreateResult.CREATED
    assert store.create_pending(approval) is _DispatchCreateResult.CREATED

    assert store.has_pending("$same", DispatchCallbackKind.MESSAGE)
    assert store.has_pending("$same", DispatchCallbackKind.APPROVAL)


@pytest.mark.parametrize(
    "outcome",
    [_DispatchTerminalOutcome.SUCCEEDED, _DispatchTerminalOutcome.INTENTIONALLY_IGNORED],
)
def test_terminal_settlement_survives_restart_and_blocks_recreation(
    tmp_path: Path,
    outcome: _DispatchTerminalOutcome,
) -> None:
    """A cold replay must not recreate work explicitly settled before restart."""
    store = _store(tmp_path)
    obligation = _message_obligation("$terminal")
    store.create_pending(obligation)

    store.settle(obligation.key, outcome)

    restarted = _store(tmp_path)
    assert restarted.pending() == ()
    assert restarted.create_pending(obligation) is _DispatchCreateResult.ALREADY_TERMINAL


def test_terminal_settlement_compacts_payload_before_invalid_replay_check(tmp_path: Path) -> None:
    """Terminal exact keys need no replay payload and must bypass later payload validation."""
    store = _store(tmp_path)
    obligation = _message_obligation("$compact")
    store.create_pending(obligation)

    store.settle(obligation.key, _DispatchTerminalOutcome.SUCCEEDED)

    database_path = tmp_path / "tracking" / "dispatch_obligations.sqlite3"
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT room_id, event_source_json FROM dispatch_obligations WHERE source_event_id = ?",
            (obligation.source_event_id,),
        ).fetchone()
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert row == ("", "")
    assert schema_version == 2
    invalid_replay = replace(
        obligation,
        room_id="!different:example.org",
        event_source={"event_id": obligation.source_event_id, "not_json_safe": object()},
    )
    assert store.create_pending(invalid_replay) is _DispatchCreateResult.ALREADY_TERMINAL


def test_store_initialization_compacts_legacy_terminal_payloads(tmp_path: Path) -> None:
    """Opening an existing store must scrub payload retained by older terminal rows."""
    store = _store(tmp_path)
    obligation = _message_obligation("$legacy-terminal")
    store.create_pending(obligation)
    store.settle(obligation.key, _DispatchTerminalOutcome.SUCCEEDED)
    database_path = tmp_path / "tracking" / "dispatch_obligations.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            UPDATE dispatch_obligations
            SET room_id = ?, event_source_json = ?
            WHERE source_event_id = ?
            """,
            (
                obligation.room_id,
                '{"event_id":"$legacy-terminal","content":{"body":"legacy"}}',
                obligation.source_event_id,
            ),
        )
        connection.execute("PRAGMA user_version = 1")

    _store(tmp_path)

    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT room_id, event_source_json FROM dispatch_obligations WHERE source_event_id = ?",
            (obligation.source_event_id,),
        ).fetchone()
    assert row == ("", "")


def test_existing_pending_payload_keeps_first_accepted_source(tmp_path: Path) -> None:
    """Transport-variant replays must keep the first durable source without failing."""
    store = _store(tmp_path)
    obligation = _message_obligation("$fixed")
    store.create_pending(obligation)
    replay_source = dict(obligation.event_source)
    replay_source["unsigned"] = {"age": 123}
    conflicting = replace(obligation, event_source=replay_source)

    assert store.create_pending(conflicting) is _DispatchCreateResult.ALREADY_PENDING

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
    assert store.create_pending(message) is _DispatchCreateResult.ALREADY_TERMINAL
    assert store.create_pending(media) is _DispatchCreateResult.ALREADY_TERMINAL
    assert store.create_pending(reaction) is _DispatchCreateResult.ALREADY_PENDING
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
    assert store.create_pending(invalid_replay) is _DispatchCreateResult.ALREADY_TERMINAL
    database_path = tmp_path / "tracking" / "dispatch_obligations.sqlite3"
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT room_id, event_source_json, state FROM dispatch_obligations WHERE source_event_id = ?",
            ("$turn-only",),
        ).fetchone()
    assert row == ("", "", _DispatchTerminalOutcome.SUCCEEDED.value)


def test_terminal_tombstones_are_not_globally_pruned(tmp_path: Path) -> None:
    """Settling new work must never evict an older exact terminal identity."""
    store = _store(tmp_path)
    trigger = _message_obligation("$trigger")
    store.create_pending(trigger)
    database_path = tmp_path / "tracking" / "dispatch_obligations.sqlite3"
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
                    _DispatchTerminalOutcome.SUCCEEDED.value,
                    index,
                    index,
                )
                for index in range(10_001)
            ),
        )

    store.settle(trigger.key, _DispatchTerminalOutcome.SUCCEEDED)

    assert store.create_pending(_message_obligation("$terminal-0")) is _DispatchCreateResult.ALREADY_TERMINAL


def test_malformed_persisted_source_is_not_invented_into_recovery(tmp_path: Path) -> None:
    """Invalid durable JSON must abort recovery and remain repairable pending work."""
    store = _store(tmp_path)
    obligation = _message_obligation("$broken")
    store.create_pending(obligation)
    database_path = tmp_path / "tracking" / "dispatch_obligations.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE dispatch_obligations SET event_source_json = ? WHERE source_event_id = ?",
            ("{", "$broken"),
        )

    restarted = _store(tmp_path)
    with pytest.raises(RuntimeError, match="corrupt dispatch obligation"):
        restarted.pending()
    assert restarted.has_pending("$broken", DispatchCallbackKind.MESSAGE)


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

    task = asyncio.create_task(_run_owned_store_operation(failing_store_operation))
    assert await asyncio.to_thread(worker_started.wait, 5)

    task.cancel()
    release_worker.set()

    with pytest.raises(asyncio.CancelledError) as exc_info:
        await task

    assert task.cancelled()
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert str(exc_info.value.__cause__) == "store write failed"


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
        outcome: _DispatchTerminalOutcome,
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
    assert store.create_pending(_message_obligation(event.event_id)) is _DispatchCreateResult.ALREADY_TERMINAL


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
    assert store.create_pending(_message_obligation("$turn-store-cancelled")) is _DispatchCreateResult.ALREADY_TERMINAL


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

    await runner.run_persisted(obligation, room=room, event=event)

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

    await runner.run_persisted(first, room=room, event=event)
    await runner.run_persisted(duplicate, room=room, event=event)

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
async def test_bound_message_callback_defers_for_persisted_turn_store_record() -> None:
    """Typed callbacks defer persisted turn truth to the runner's durable settlement gate."""
    persisted = {"$bound"}

    async def noop(_room: nio.MatrixRoom, _event: nio.Event) -> None:
        pass

    callbacks = DispatchObligationRunner.callbacks_for(
        on_message=cast("Any", noop),
        on_media=cast("Any", noop),
        on_reaction=cast("Any", noop),
        on_approval=cast("Any", noop),
        on_invite=cast("Any", noop),
        on_room_lifecycle=cast("Any", noop),
        on_redaction=cast("Any", noop),
        on_decryption_failure=cast("Any", noop),
        turn_is_persisted=persisted.__contains__,
        source_is_deferred=lambda _event_id: False,
    )
    callback = callbacks[DispatchCallbackKind.MESSAGE]
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$bound")

    assert await callback(room, event) is DispatchCallbackResult.DEFERRED


@pytest.mark.asyncio
async def test_task_wrapper_persists_before_background_execution(tmp_path: Path) -> None:
    """Returning to nio must require durable acceptance before background execution."""
    entered = asyncio.Event()
    release = asyncio.Event()
    owner = object()

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        entered.set()
        await release.wait()
        return DispatchCallbackResult.SUCCEEDED

    store = _store(tmp_path)
    runner = _runner(store, callback)
    wrapper = runner.task_wrapper(DispatchCallbackKind.MESSAGE, owner=owner)
    event = _message_event("$durable")

    await wrapper(nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID), event)

    assert store.has_pending("$durable", DispatchCallbackKind.MESSAGE)
    await entered.wait()
    release.set()
    await wait_for_background_tasks(timeout=1.0, owner=owner)
    assert not store.has_pending("$durable", DispatchCallbackKind.MESSAGE)


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

    await runner.task_wrapper(DispatchCallbackKind.MESSAGE, owner=owner)(
        nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
        _message_event("$task-retry"),
    )
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
@pytest.mark.parametrize("entrypoint", ["direct", "task-wrapper"])
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

    def fail_create(_obligation: _DispatchObligation) -> _DispatchCreateResult:
        message = "dispatch database unavailable"
        raise OSError(message)

    monkeypatch.setattr(store, "create_pending", fail_create)
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$persist-failure")

    if entrypoint == "direct":
        persist = runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)
    else:
        persist = runner.task_wrapper(DispatchCallbackKind.MESSAGE, owner=object())(room, event)
    expected_error = OSError if entrypoint == "direct" else nio.CallbackNotAcceptedError
    with pytest.raises(expected_error, match="dispatch database unavailable") as exc_info:
        await persist

    if entrypoint == "task-wrapper":
        assert isinstance(exc_info.value.__cause__, OSError)
    assert failure_notifications == 1


def test_correctness_callbacks_register_with_explicit_durable_kinds(tmp_path: Path) -> None:
    """Every source-backed correctness callback must use the durable runner seam."""

    async def callback(_room: nio.MatrixRoom, _event: nio.Event) -> DispatchCallbackResult:
        return DispatchCallbackResult.SUCCEEDED

    runner = _runner(_store(tmp_path), callback)
    client = MagicMock(spec=nio.AsyncClient)

    runner.register_source_callbacks(client, owner=object())

    registrations = {
        event_type: registered
        for registered, event_type in (call.args for call in client.add_event_callback.call_args_list)
    }
    expected_kinds = {
        nio.RoomMessageText: DispatchCallbackKind.MESSAGE,
        nio.ReactionEvent: DispatchCallbackKind.REACTION,
        nio.RedactionEvent: DispatchCallbackKind.REDACTION,
        nio.MegolmEvent: DispatchCallbackKind.DECRYPTION_FAILURE,
        **dict.fromkeys(MATRIX_MEDIA_EVENT_TYPES, DispatchCallbackKind.MEDIA),
    }
    assert {*expected_kinds, nio.UnknownEvent} <= registrations.keys()
    for event_type, callback_kind in expected_kinds.items():
        registered = registrations[event_type]
        assert isinstance(registered, DispatchObligationTaskWrapper)
        assert registered.callback_kind is callback_kind


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
    )
    client = MagicMock(spec=nio.AsyncClient)
    owner = object()
    runner.register_source_callbacks(client, owner=owner)
    registered = next(
        callback
        for callback, event_type in (call.args for call in client.add_event_callback.call_args_list)
        if event_type is nio.UnknownEvent
    )

    await registered(
        nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID),
        _unknown_event("$unknown", event_type),
    )
    await wait_for_background_tasks(timeout=1.0, owner=owner)

    assert attempts == expected_attempts
    assert not store.has_pending("$unknown", DispatchCallbackKind.APPROVAL)
