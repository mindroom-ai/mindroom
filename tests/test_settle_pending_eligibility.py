"""``settle_pending_from_turn_store`` must only compact deferred callback obligations.

A pending row means the callback body never ran: TurnStore terminal truth only
proves the turn finished downstream, so settling such a row would silently drop
callback work that never executed. Only deferred rows (callback ran, turn work
owns the source) may be compacted into tombstones by the settlement path driven
from ``TurnSettlementRetry``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.dispatch_obligations.storage import (
    DispatchCallbackKind,
    DispatchCreateResult,
    DispatchObligation,
    DispatchObligationStore,
    DispatchTerminalOutcome,
)
from mindroom.turn_settlement_retry import TurnSettlementRetry

if TYPE_CHECKING:
    from pathlib import Path

_PRINCIPAL_ID = "@code:example.org"
_ENTITY_NAME = "code"
_ROOM_ID = "!room:example.org"


def _store(tmp_path: Path) -> DispatchObligationStore:
    return DispatchObligationStore(
        tracking_path=tmp_path / "tracking",
        principal_id=_PRINCIPAL_ID,
        entity_name=_ENTITY_NAME,
    )


def _message_obligation(event_id: str) -> DispatchObligation:
    return DispatchObligation(
        principal_id=_PRINCIPAL_ID,
        entity_name=_ENTITY_NAME,
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


def _settle_from_terminal_turn_truth(store: DispatchObligationStore, source_event_ids: tuple[str, ...]) -> None:
    """Settle exactly as ``TurnStore.on_terminal_turn_persisted`` does without a running loop."""
    TurnSettlementRetry(store).retry(source_event_ids)


def test_never_run_pending_row_survives_terminal_turn_settlement(tmp_path: Path) -> None:
    """A pending obligation whose callback body never ran must not be tombstoned."""
    store = _store(tmp_path)
    obligation = _message_obligation("$never-ran")
    assert store.create_pending(obligation) is DispatchCreateResult.CREATED

    _settle_from_terminal_turn_truth(store, ("$never-ran",))

    assert store.has_pending("$never-ran", DispatchCallbackKind.MESSAGE)
    assert store.unsettled_source_event_ids() == frozenset({"$never-ran"})
    (surviving,) = store.pending()
    assert surviving == obligation
    assert not surviving.callback_completed


def test_deferred_row_settles_and_tombstones_with_terminal_turn_truth(tmp_path: Path) -> None:
    """A deferred obligation is owned by downstream turn work and must compact away."""
    store = _store(tmp_path)
    obligation = _message_obligation("$deferred")
    assert store.create_pending(obligation) is DispatchCreateResult.CREATED
    store.mark_callback_deferred(obligation.key)

    _settle_from_terminal_turn_truth(store, ("$deferred",))

    assert not store.has_pending("$deferred", DispatchCallbackKind.MESSAGE)
    assert store.unsettled_source_event_ids() == frozenset()
    assert store.pending() == ()
    with store._connection() as connection:
        row = connection.execute(
            """
            SELECT state, room_id, event_source_json, semantic_consumer, settled_at_ns
            FROM dispatch_obligations
            WHERE source_event_id = ?
            """,
            ("$deferred",),
        ).fetchone()
    assert row is not None
    assert row["state"] == DispatchTerminalOutcome.SUCCEEDED.value
    assert row["room_id"] == ""
    assert row["event_source_json"] == ""
    assert row["semantic_consumer"] is None
    assert row["settled_at_ns"] is not None


def test_deferred_row_without_terminal_turn_truth_survives(tmp_path: Path) -> None:
    """Settlement is scoped to the exact terminal sources a terminal turn names."""
    store = _store(tmp_path)
    settled = _message_obligation("$settled")
    waiting = _message_obligation("$still-waiting")
    assert store.create_pending(settled) is DispatchCreateResult.CREATED
    assert store.create_pending(waiting) is DispatchCreateResult.CREATED
    store.mark_callback_deferred(settled.key)
    store.mark_callback_deferred(waiting.key)

    _settle_from_terminal_turn_truth(store, ("$settled",))

    assert not store.has_pending("$settled", DispatchCallbackKind.MESSAGE)
    assert store.has_pending("$still-waiting", DispatchCallbackKind.MESSAGE)
    assert store.unsettled_source_event_ids() == frozenset({"$still-waiting"})
    (surviving,) = store.pending()
    assert surviving.source_event_id == "$still-waiting"
    assert surviving.callback_completed
    assert surviving.room_id == _ROOM_ID
    assert surviving.event_source == waiting.event_source
