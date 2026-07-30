"""Transport recovery contract for durable terminal Matrix edits."""

# ruff: noqa: D103

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.handled_turns import TurnRecord
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.terminal_delivery import (
    TerminalDeliveryCommit,
    TerminalDeliveryCoordinator,
    TerminalDeliveryCoordinatorDeps,
    TerminalDeliveryIntent,
)
from mindroom.turn_store import TurnStore, TurnStoreDeps

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:localhost"
SOURCE = "$source"
TARGET = "$visible"
WIRE_CONTENT = {
    "body": "* final answer",
    "m.new_content": {
        "body": "final answer",
        "msgtype": "m.text",
    },
}


def _target(
    *,
    room_id: str = ROOM,
    source_event_id: str = SOURCE,
) -> MessageTarget:
    return MessageTarget.resolve(
        room_id,
        f"$thread-{source_event_id}",
        source_event_id,
    )


def _intent(
    *,
    source_event_ids: tuple[str, ...] = (SOURCE,),
    target_event_id: str = TARGET,
    correlation_id: str = "corr-1",
) -> TerminalDeliveryIntent:
    return TerminalDeliveryIntent(
        source_event_ids=source_event_ids,
        target_event_id=target_event_id,
        correlation_id=correlation_id,
        wire_content=WIRE_CONTENT,
    )


def _store(tmp_path: Path) -> TurnStore:
    return TurnStore(
        TurnStoreDeps(
            agent_name="code",
            tracking_base_path=tmp_path / "tracking",
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )


def _record_pending(
    store: TurnStore,
    *,
    source_event_id: str = SOURCE,
    room_id: str = ROOM,
) -> TurnRecord:
    pending = store.record_pending_turn(
        TurnRecord.create(
            [source_event_id],
            completed=False,
            response_owner="code",
            requester_id="@user:localhost",
            correlation_id="corr-1",
            conversation_target=_target(
                room_id=room_id,
                source_event_id=source_event_id,
            ),
        ),
    )
    assert pending is not None
    return pending


def _coordinator(
    store: TurnStore,
    *,
    ready: bool = True,
) -> TerminalDeliveryCoordinator:
    return TerminalDeliveryCoordinator(
        TerminalDeliveryCoordinatorDeps(
            runtime=SimpleNamespace(client=AsyncMock() if ready else None),
            turn_store=store,
            conversation_cache=MagicMock(notify_outbound_message=MagicMock()),
            redact_message_event=AsyncMock(return_value=True),
            is_ready=lambda: ready,
            logger=MagicMock(),
            poll_interval_seconds=0.01,
        ),
    )


def _delivered() -> DeliveredMatrixEvent:
    return DeliveredMatrixEvent(
        event_id="$edit",
        content_sent=WIRE_CONTENT,
    )


@pytest.mark.asyncio
async def test_checkpoint_reaches_disk_before_first_matrix_attempt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store)
    coordinator = _coordinator(store)

    async def assert_disk_first(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        [record] = _store(tmp_path).terminal_checkpoint_records()
        assert record.response_event_id == TARGET
        assert record.terminal_edit_checkpoint is not None
        assert record.terminal_edit_checkpoint.wire_content == WIRE_CONTENT
        return _delivered()

    with patch(
        "mindroom.terminal_delivery.send_message_result",
        side_effect=assert_disk_first,
    ):
        result = await coordinator.commit_and_attempt(_intent())

    assert result.status == "delivered"
    assert store.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_cancelled_first_attempt_returns_durable_debt(tmp_path: Path) -> None:
    """Cancellation after checkpoint commit must not make the turn rerunnable."""
    store = _store(tmp_path)
    _record_pending(store)
    coordinator = _coordinator(store)

    with patch(
        "mindroom.terminal_delivery.send_message_result",
        side_effect=asyncio.CancelledError("restart"),
    ):
        result = await coordinator.commit_and_attempt(_intent())

    assert result == TerminalDeliveryCommit("deferred", "attempt_cancelled")
    [checkpoint_owner] = store.terminal_checkpoint_records()
    assert checkpoint_owner.terminal_edit_checkpoint is not None


@pytest.mark.asyncio
async def test_restart_retries_exact_wire_content_and_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store)
    blocked = _coordinator(store, ready=False)

    result = await blocked.commit_and_attempt(_intent())

    assert result.status == "deferred"
    [checkpoint_owner] = store.terminal_checkpoint_records()
    checkpoint = checkpoint_owner.terminal_edit_checkpoint
    assert checkpoint is not None

    restarted = _store(tmp_path)
    active = _coordinator(restarted)
    with patch(
        "mindroom.terminal_delivery.send_message_result",
        return_value=_delivered(),
    ) as send:
        await active.retry_pending()

    send.assert_awaited_once()
    assert send.await_args.args[2] == WIRE_CONTENT
    assert send.await_args.kwargs["transaction_id"] == checkpoint.transaction_id
    assert send.await_args.kwargs["content_is_prepared"] is True
    assert restarted.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_retry_after_accepted_edit_reuses_transaction(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store)
    coordinator = _coordinator(store)
    original_clear = store.clear_terminal_checkpoint
    clear_calls = 0

    def leave_first_checkpoint(*args: object, **kwargs: object) -> TurnRecord | None:
        nonlocal clear_calls
        clear_calls += 1
        if clear_calls == 1:
            return None
        return original_clear(*args, **kwargs)

    with (
        patch(
            "mindroom.terminal_delivery.send_message_result",
            return_value=_delivered(),
        ) as send,
        patch.object(
            store,
            "clear_terminal_checkpoint",
            side_effect=leave_first_checkpoint,
        ),
    ):
        assert (await coordinator.commit_and_attempt(_intent())).status == "delivered"
        assert len(store.terminal_checkpoint_records()) == 1
        await coordinator.retry_pending()

    assert send.await_count == 2
    first_transaction = send.await_args_list[0].kwargs["transaction_id"]
    second_transaction = send.await_args_list[1].kwargs["transaction_id"]
    assert first_transaction == second_transaction
    assert store.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_sequential_retry_continues_after_one_transport_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store)
    _record_pending(
        store,
        source_event_id="$second",
        room_id="!second:localhost",
    )
    blocked = _coordinator(store, ready=False)
    assert (await blocked.commit_and_attempt(_intent())).status == "deferred"
    assert (
        await blocked.commit_and_attempt(
            _intent(
                source_event_ids=("$second",),
                target_event_id="$second-visible",
                correlation_id="corr-2",
            ),
        )
    ).status == "deferred"

    active = _coordinator(store)
    with patch(
        "mindroom.terminal_delivery.send_message_result",
        side_effect=[None, _delivered()],
    ) as send:
        await active.retry_pending()

    assert send.await_count == 2
    remaining = store.terminal_checkpoint_records()
    assert len(remaining) == 1
    assert remaining[0].source_event_ids == (SOURCE,)


@pytest.mark.asyncio
async def test_source_redaction_never_publishes_frozen_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store)
    blocked = _coordinator(store, ready=False)
    assert (await blocked.commit_and_attempt(_intent())).status == "deferred"
    blocked.deps.redact_message_event.return_value = False

    await blocked.redact(room_id=ROOM, event_id=SOURCE)

    blocked.deps.redact_message_event.assert_awaited_once()
    restarted = _store(tmp_path)
    active = _coordinator(restarted)
    with patch(
        "mindroom.terminal_delivery.send_message_result",
        new_callable=AsyncMock,
    ) as send:
        await active.retry_pending()

    send.assert_not_awaited()
    active.deps.redact_message_event.assert_awaited_once_with(
        room_id=ROOM,
        event_id=TARGET,
        reason="Source event was redacted",
    )
    owner = restarted.get_turn_record(SOURCE)
    assert owner is not None
    assert owner.terminal_edit_checkpoint is None
    assert owner.response_event_id is None


@pytest.mark.asyncio
async def test_target_redaction_clears_checkpoint_before_retry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store)
    blocked = _coordinator(store, ready=False)
    assert (await blocked.commit_and_attempt(_intent())).status == "deferred"

    await blocked.redact(room_id=ROOM, event_id=TARGET)

    with patch(
        "mindroom.terminal_delivery.send_message_result",
        new_callable=AsyncMock,
    ) as send:
        await _coordinator(_store(tmp_path)).retry_pending()

    send.assert_not_awaited()
    tombstone = store.get_turn_record(TARGET)
    assert tombstone is not None
    assert tombstone.redacted_source_event_ids == (TARGET,)
    assert store.terminal_checkpoint_records() == ()


@pytest.mark.asyncio
async def test_redaction_waits_for_inflight_send_and_removes_target(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store)
    coordinator = _coordinator(store)
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def blocked_send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        send_started.set()
        await release_send.wait()
        return _delivered()

    with patch(
        "mindroom.terminal_delivery.send_message_result",
        side_effect=blocked_send,
    ):
        delivery = asyncio.create_task(coordinator.commit_and_attempt(_intent()))
        await send_started.wait()
        redaction = asyncio.create_task(
            coordinator.redact(room_id=ROOM, event_id=SOURCE),
        )
        await asyncio.sleep(0)
        assert not redaction.done()
        release_send.set()
        assert (await delivery).status == "delivered"
        await redaction

    coordinator.deps.redact_message_event.assert_awaited_once_with(
        room_id=ROOM,
        event_id=TARGET,
        reason="Source event was redacted",
    )
    owner = store.get_turn_record(SOURCE)
    assert owner is not None
    assert owner.redacted_source_event_ids == (SOURCE,)
    assert owner.terminal_edit_checkpoint is None
    assert owner.response_event_id is None


@pytest.mark.asyncio
async def test_stale_cleanup_rechecks_ownership_under_delivery_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _record_pending(store)
    coordinator = _coordinator(store)

    with patch(
        "mindroom.terminal_delivery.send_message_result",
        return_value=_delivered(),
    ):
        async with coordinator.stale_cleanup_guard(TARGET) as may_clean:
            assert may_clean is True
            delivery = asyncio.create_task(
                coordinator.commit_and_attempt(_intent()),
            )
            await asyncio.sleep(0)
            assert not delivery.done()

        assert (await delivery).status == "delivered"

    async with coordinator.stale_cleanup_guard(TARGET) as may_clean:
        assert may_clean is False


@pytest.mark.asyncio
async def test_wake_during_retry_scan_triggers_another_scan(tmp_path: Path) -> None:
    coordinator = _coordinator(_store(tmp_path))
    coordinator.deps = replace(
        coordinator.deps,
        poll_interval_seconds=60,
    )
    entered = asyncio.Event()
    release = asyncio.Event()
    second_scan = asyncio.Event()
    calls = 0

    async def retry_pending() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        elif calls == 2:
            second_scan.set()

    coordinator.retry_pending = retry_pending  # type: ignore[method-assign]
    worker = asyncio.create_task(coordinator._run())
    try:
        await entered.wait()
        coordinator.wake(reason="checkpoint-added-during-scan")
        release.set()
        async with asyncio.timeout(1):
            await second_scan.wait()
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    assert calls == 2
