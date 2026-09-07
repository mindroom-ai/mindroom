"""Projection backpressure must preserve durable admission and the receive loop."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from nio import TimelineEventProvenance
from nio.durable import RecordKind, SyncBatch, SyncRecord

from mindroom.event_journal import DeliveryStage
from mindroom.matrix.durable_ingestion import run_ingestion_pump
from mindroom.matrix_delivery import MatrixDeliveryWorker
from tests.test_bot_ready_hook import _agent_bot
from tests.test_durable_ingestion_admission import ACCOUNT as ACCOUNT_ID
from tests.test_durable_ingestion_admission import ROOM as ROOM_ID
from tests.test_durable_ingestion_admission import Session as _AdapterSession
from tests.test_event_journal_store import (
    _interactive_selection_rows,
    admit,
    interactive_edit,
    interactive_prompt,
    projection,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore, MatrixDelivery, PrincipalStore, ProjectedEvent


CONSUMER_GENERATION = UUID(int=1)
STREAM_ID = UUID(int=2)
SENDER = "@alice:example.org"


async def _projection_case(store: EventJournalStore) -> tuple[PrincipalStore, dict[str, object], SyncBatch]:
    principal_id = f"agent@{ACCOUNT_ID}"
    principal = store.principal(principal_id)
    await principal.load_or_create_ingestion_consumer(new_generation=CONSUMER_GENERATION)
    await principal.bind_ingestion_stream(generation=CONSUMER_GENERATION, stream_id=STREAM_ID)
    await admit(principal, "$turn", sender=SENDER)
    await admit(
        principal,
        "$prompt",
        sender=ACCOUNT_ID,
        content=interactive_prompt("Old?", "old", source_event_id="$turn"),
        ts=2_000,
    )
    edit = interactive_edit("$prompt", "New?", "new", source_event_id="$turn")
    await principal.enqueue_matrix_delivery(
        delivery_id="$edit",
        stage=DeliveryStage.FINAL,
        room_id=ROOM_ID,
        thread_id=None,
        payload=edit,
        edits_event_id="$prompt",
    )
    await principal.claim_matrix_delivery(delivery_id="$edit", stage=DeliveryStage.FINAL)
    batch = SyncBatch(
        STREAM_ID,
        1,
        (
            SyncRecord(
                RecordKind.TIMELINE,
                ROOM_ID,
                {
                    "type": "m.reaction",
                    "event_id": "$reaction",
                    "sender": SENDER,
                    "origin_server_ts": 3_000,
                    "content": {
                        "m.relates_to": {
                            "rel_type": "m.annotation",
                            "event_id": "$prompt",
                            "key": "1",
                        },
                    },
                },
                provenance=TimelineEventProvenance.RECOVERED,
            ),
        ),
    )
    return principal, edit, batch


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_pump_waits_for_projection_without_committing_or_spinning(  # noqa: PLR0915 - one causal admission/projection sequence
    journal_database: Callable[[], EventJournalStore],
    *,
    cancel: bool,
) -> None:
    store = journal_database()
    principal, edit, batch = await _projection_case(store)
    principal_id = f"agent@{ACCOUNT_ID}"
    session = _AdapterSession(batch)
    blocked = asyncio.Event()
    projected = asyncio.Event()
    idle = asyncio.Event()
    waits = 0
    wakes = []

    async def wait_for_projection() -> None:
        nonlocal waits
        waits += 1
        blocked.set()
        await projected.wait()

    async def wait_for_work() -> None:
        idle.set()
        await asyncio.Event().wait()

    pumping = asyncio.create_task(
        run_ingestion_pump(
            session,
            principal,
            account_id=ACCOUNT_ID,
            wait_for_work=wait_for_work,
            wake_semantic_dispatch=lambda: wakes.append(None),
            wait_for_delivery_projection=wait_for_projection,
        ),
    )
    try:
        entered = asyncio.create_task(blocked.wait())
        done, _ = await asyncio.wait((entered, pumping), timeout=2, return_when=asyncio.FIRST_COMPLETED)
        assert entered in done, f"projection wait was not reached: {pumping.exception() if pumping.done() else None}"
        await entered
        assert await principal.load_event("$reaction") is None
        assert await _interactive_selection_rows(store) == []
        frontier = await store.backend.read(
            lambda tx: tx.fetchone(
                "SELECT next_sequence FROM matrix_sync_consumers WHERE principal_id = ?",
                (principal_id,),
            ),
        )
        assert frontier is not None
        assert frontier["next_sequence"] == 1
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert waits == 1
        assert len(session.next_calls) == 1
        assert session.acked == []
        assert session.batch is batch
        if cancel:
            pumping.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pumping, timeout=1)
            assert session.batch is batch
            assert session.acked == []
            return
        await principal.acknowledge_matrix_delivery(
            delivery_id="$edit",
            stage=DeliveryStage.FINAL,
            event_id="$edit-event",
            delivered_projections=(projection("$edit-event", sender=ACCOUNT_ID, ts=2_500, content=edit),),
        )
        projected.set()
        await asyncio.wait_for(idle.wait(), timeout=2)
        assert session.acked == [batch]
        assert wakes == [None]
        selections = await _interactive_selection_rows(store)
        assert [(row["source_event_id"], row["revision_event_id"]) for row in selections] == [
            ("$reaction", "$edit-event"),
        ]
        await admit(
            principal,
            "$later-edit",
            sender=ACCOUNT_ID,
            ts=4_000,
            content=interactive_edit("$prompt", "Later?", "later", source_event_id="$turn"),
        )
        selection = await principal.claim_interactive_reaction(source_event_id="$reaction")
        assert selection is not None
        assert (selection.question_text, selection.selected_value) == ("New?", "new")
    finally:
        pumping.cancel()
        with suppress(asyncio.CancelledError):
            await pumping


@pytest.mark.asyncio
async def test_bot_projection_wait_reuses_recovery_and_shields_pump_cancellation(tmp_path: Path) -> None:
    bot = _agent_bot(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def recover() -> bool:
        entered.set()
        await release.wait()
        return True

    with (
        patch.object(bot, "_recover_unacknowledged_matrix_deliveries", AsyncMock(side_effect=recover)),
        patch.object(bot, "_refresh_agent_reply_memberships_if_needed", AsyncMock()) as refresh,
    ):
        bot._schedule_delivery_recovery()
        await asyncio.wait_for(entered.wait(), timeout=2)
        recovery_task = bot._delivery_recovery_task
        assert recovery_task is not None
        waiting = asyncio.create_task(bot._wait_for_delivery_projection())
        try:
            await asyncio.sleep(0)
            assert bot._delivery_recovery_task is recovery_task
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, timeout=1)
            assert not recovery_task.done()
            release.set()
            await asyncio.wait_for(recovery_task, timeout=2)
            refresh.assert_not_awaited()
        finally:
            release.set()
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting


@pytest.mark.asyncio
async def test_projected_reaction_settles_while_unrelated_outbox_debt_keeps_retrying(
    journal_database: Callable[[], EventJournalStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Prove progress during backoff without imposing a subsecond DB deadline.
    monkeypatch.setattr("mindroom.bot._DELIVERY_RECOVERY_RETRY_INITIAL_DELAY_SECONDS", 30.0)
    store = journal_database()
    principal, edit, batch = await _projection_case(store)
    await principal.enqueue_matrix_delivery(
        delivery_id="$unrelated",
        stage=DeliveryStage.FINAL,
        room_id="!other:example.org",
        thread_id=None,
        payload={"msgtype": "m.text", "body": "unrelated"},
    )
    session = _AdapterSession(batch)
    bot = _agent_bot(tmp_path)
    pass_done = asyncio.Event()
    idle = asyncio.Event()

    async def send(delivery: MatrixDelivery) -> str:
        if delivery.delivery_id == "$unrelated":
            message = "unrelated room is unavailable"
            raise OSError(message)
        return "$edit-event"

    async def observe(_delivery: MatrixDelivery, _event_id: str) -> tuple[ProjectedEvent, ...]:
        return (projection("$edit-event", sender=ACCOUNT_ID, ts=2_500, content=edit),)

    worker = MatrixDeliveryWorker(store=principal, send=send, observe_delivered=observe)

    async def recover() -> bool:
        outcome = await worker.recover()
        pass_done.set()
        return outcome.complete

    async def wait_for_work() -> None:
        idle.set()
        await asyncio.Event().wait()

    with patch.object(bot, "_recover_unacknowledged_matrix_deliveries", AsyncMock(side_effect=recover)):
        pumping = asyncio.create_task(
            run_ingestion_pump(
                session,
                principal,
                account_id=ACCOUNT_ID,
                wait_for_work=wait_for_work,
                wake_semantic_dispatch=lambda: None,
                wait_for_delivery_projection=bot._wait_for_delivery_projection,
            ),
        )
        try:
            await asyncio.wait_for(pass_done.wait(), timeout=2)
            await asyncio.wait_for(idle.wait(), timeout=2)
            assert session.acked == [batch]
            assert bot._delivery_recovery_task is not None
            assert not bot._delivery_recovery_task.done()
            assert not bot._delivery_recovery_wake.is_set()
            selections = await _interactive_selection_rows(store)
            assert [row["revision_event_id"] for row in selections] == ["$edit-event"]
        finally:
            pumping.cancel()
            with suppress(asyncio.CancelledError):
                await pumping
            bot._sync_shutting_down = True
            bot._delivery_recovery_wake.set()
            if bot._delivery_recovery_task is not None:
                await asyncio.wait_for(bot._delivery_recovery_task, timeout=2)


@pytest.mark.asyncio
async def test_bot_projection_wait_cancels_during_shutdown(tmp_path: Path) -> None:
    bot = _agent_bot(tmp_path)
    bot._sync_shutting_down = True
    with patch.object(bot, "_schedule_delivery_recovery") as schedule, pytest.raises(asyncio.CancelledError):
        await bot._wait_for_delivery_projection()
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_projection_wait_consumes_prior_pass_without_resetting_active_backoff(tmp_path: Path) -> None:
    bot = _agent_bot(tmp_path)
    pass_done = asyncio.Event()

    async def recover() -> bool:
        pass_done.set()
        return False

    with patch.object(bot, "_recover_unacknowledged_matrix_deliveries", AsyncMock(side_effect=recover)) as recovery:
        bot._schedule_delivery_recovery()
        await asyncio.wait_for(pass_done.wait(), timeout=2)
        await bot._wait_for_delivery_projection()
        assert recovery.await_count == 1
        assert not bot._delivery_recovery_wake.is_set()
        waiting = asyncio.create_task(bot._wait_for_delivery_projection())
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not waiting.done()
            assert recovery.await_count == 1
            assert not bot._delivery_recovery_wake.is_set()
        finally:
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting
            bot._sync_shutting_down = True
            bot._delivery_recovery_wake.set()
            if bot._delivery_recovery_task is not None:
                await asyncio.wait_for(bot._delivery_recovery_task, timeout=2)


@pytest.mark.asyncio
async def test_bot_projection_wait_cancels_if_shutdown_starts_while_waiting(tmp_path: Path) -> None:
    bot = _agent_bot(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def recover() -> bool:
        entered.set()
        await release.wait()
        return True

    with patch.object(bot, "_recover_unacknowledged_matrix_deliveries", AsyncMock(side_effect=recover)):
        waiting = asyncio.create_task(bot._wait_for_delivery_projection())
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            bot._sync_shutting_down = True
            recovery_task = bot._delivery_recovery_task
            assert recovery_task is not None
            recovery_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await recovery_task
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, timeout=1)
        finally:
            release.set()
            waiting.cancel()
            with suppress(asyncio.CancelledError):
                await waiting
