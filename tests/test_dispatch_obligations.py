"""Durable exact Matrix callback obligations and restart recovery."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom.dispatch_obligations import (
    DispatchCallbackKind,
    DispatchObligationRunner,
    DispatchObligationStore,
    _DispatchCreateResult,
    _DispatchObligation,
    _DispatchTerminalOutcome,
)
from mindroom.dispatch_obligations import (
    _DispatchCallbackResult as DispatchCallbackResult,
)

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
    terminal_limit: int = 10_000,
) -> DispatchObligationStore:
    return DispatchObligationStore(
        tracking_path=tmp_path / "tracking",
        principal_id=principal_id,
        entity_name=entity_name,
        terminal_limit=terminal_limit,
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
    turn_is_persisted: Callable[[str], bool] = lambda _event_id: False,
) -> DispatchObligationRunner:
    return DispatchObligationRunner(
        store=store,
        callbacks={DispatchCallbackKind.MESSAGE: callback},
        room_for_id=lambda room_id: nio.MatrixRoom(room_id, "@code:example.org"),
        turn_is_persisted=turn_is_persisted,
    )


def test_pending_row_survives_new_store_instance(tmp_path: Path) -> None:
    """Dropping process memory must not drop callback work already accepted."""
    first = _store(tmp_path)
    obligation = _message_obligation("$message")

    assert first.create_pending(obligation) is _DispatchCreateResult.CREATED

    restarted = _store(tmp_path)

    assert restarted.pending() == (obligation,)
    assert restarted.has_pending("$message", DispatchCallbackKind.MESSAGE)


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


def test_existing_pending_payload_cannot_be_reassigned(tmp_path: Path) -> None:
    """An exact key must keep the original room and raw event it promises to replay."""
    store = _store(tmp_path)
    obligation = _message_obligation("$fixed")
    store.create_pending(obligation)
    conflicting = replace(obligation, room_id="!other:example.org")

    with pytest.raises(ValueError, match="payload"):
        store.create_pending(conflicting)

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


def test_turn_store_terminal_truth_removes_only_message_and_media_rows(tmp_path: Path) -> None:
    """Turn truth may replace message/media obligations, never unrelated callback kinds."""
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
    with pytest.raises(ValueError, match="message or media"):
        store.settle_from_turn_store("$turn", DispatchCallbackKind.REACTION)


def test_terminal_pruning_never_removes_pending_work(tmp_path: Path) -> None:
    """Bounding dedupe history must not bound still-owed callback work."""
    store = _store(tmp_path, terminal_limit=2)
    pending = _message_obligation("$pending")
    store.create_pending(pending)
    for index in range(4):
        obligation = _message_obligation(f"$terminal-{index}")
        store.create_pending(obligation)
        store.settle(obligation.key, _DispatchTerminalOutcome.SUCCEEDED)

    restarted = _store(tmp_path, terminal_limit=2)

    assert restarted.pending() == (pending,)
    assert restarted.create_pending(_message_obligation("$terminal-3")) is _DispatchCreateResult.ALREADY_TERMINAL
    assert restarted.create_pending(_message_obligation("$terminal-0")) is _DispatchCreateResult.CREATED


def test_malformed_persisted_source_is_not_invented_into_recovery(tmp_path: Path) -> None:
    """Invalid durable JSON must stay unresolved instead of becoming a guessed callback."""
    store = _store(tmp_path)
    obligation = _message_obligation("$broken")
    store.create_pending(obligation)
    database_path = tmp_path / "tracking" / "dispatch_obligations.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE dispatch_obligations SET event_source_json = ? WHERE source_event_id = ?",
            ("{", "$broken"),
        )

    assert _store(tmp_path).pending() == ()
    assert _store(tmp_path).has_pending("$broken", DispatchCallbackKind.MESSAGE)


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
        turn_is_persisted=lambda _event_id: False,
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

    runner = _runner(_store(tmp_path), callback, turn_is_persisted=handled.__contains__)
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
        turn_is_persisted=handled.__contains__,
    )
    room = nio.MatrixRoom(_ROOM_ID, _PRINCIPAL_ID)
    event = _message_event("$deferred")

    await runner.dispatch(room, event, DispatchCallbackKind.MESSAGE)
    assert _store(tmp_path).has_pending("$deferred", DispatchCallbackKind.MESSAGE)

    handled.add("$deferred")
    await runner.recover_pending()

    assert not _store(tmp_path).has_pending("$deferred", DispatchCallbackKind.MESSAGE)
