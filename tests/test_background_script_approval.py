"""End-to-end durable Matrix approvals for background-script calls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

import pytest

from mindroom.approval_manager import _ApprovalManager
from mindroom.event_journal import DeliveryStage, EventJournalStore, MatrixDelivery
from mindroom.tool_approval import BackgroundScriptToolOrigin
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _origin() -> BackgroundScriptToolOrigin:
    return BackgroundScriptToolOrigin(
        run_id="run-1",
        call_id="call-1",
        requester_id="@alice:localhost",
        toolkit_name="calculator",
        function_name="add",
    )


async def _approval_manager(
    tmp_path: Path,
    *,
    database_name: str = "background-approval.db",
    fail_final: bool = False,
) -> tuple[_ApprovalManager, EventJournalStore, asyncio.Event]:
    journal = EventJournalStore.open_sqlite(tmp_path / database_name)
    cards = journal.principal("router@shared")
    initial_sent = asyncio.Event()

    async def prepare_event(
        _room_id: str,
        _thread_id: str | None,
        content: dict[str, object],
    ) -> dict[str, object]:
        return content

    async def send(delivery: MatrixDelivery) -> str:
        if delivery.stage is DeliveryStage.INITIAL:
            initial_sent.set()
            return "$approval"
        if fail_final:
            message = "homeserver unavailable"
            raise RuntimeError(message)
        return "$terminal"

    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        prepare_event=prepare_event,
        send_delivery=send,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: "DEVICE",
    )
    return manager, journal, initial_sent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["approved", "denied"],
)
async def test_background_script_approval_uses_exact_matrix_actor_and_first_decision(
    tmp_path: Path,
    status: Literal["approved", "denied"],
) -> None:
    """Only the exact Matrix actor can commit the first decision for one call."""
    manager, journal, initial_sent = await _approval_manager(tmp_path)
    decision_task = asyncio.create_task(
        manager.request_background_approval(
            origin=_origin(),
            room_id="!room:localhost",
            thread_id="$thread",
            agent_name="watcher",
            requester_id="@alice:localhost",
            approver_user_id="@alice:localhost",
            tool_name="add",
            arguments={"a": 1, "b": 2},
            timeout_seconds=30.0,
        ),
    )
    try:
        await asyncio.wait_for(initial_sent.wait(), timeout=1.0)
        await asyncio.sleep(0.01)
        stored = await journal.principal("router@shared").pending_approval_card(
            room_id="!room:localhost",
            card_event_id="$approval",
        )
        assert stored is not None, await journal.principal("router@shared").pending_approval_cards(
            room_id="!room:localhost",
        )
        assert stored.target_kind == "background_script"
        wrong_actor = await manager.handle_card_response(
            room_id="!room:localhost",
            sender_id="@mallory:localhost",
            card_event_id="$approval",
            status="approved",
            reason=None,
        )
        assert wrong_actor.consumed is False
        assert decision_task.done() is False

        result = await manager.handle_card_response(
            room_id="!room:localhost",
            sender_id="@alice:localhost",
            card_event_id="$approval",
            status=status,
            reason="operator decision",
        )
        assert result.consumed is True
        assert result.resolved is True
        decision = await asyncio.wait_for(decision_task, timeout=1.0)
        assert decision.status == status
        assert decision.reason == "operator decision"
        assert await journal.principal("router@shared").is_terminal_approval_card(
            room_id="!room:localhost",
            card_event_id="$approval",
        )
        repeated = await manager.handle_card_response(
            room_id="!room:localhost",
            sender_id="@alice:localhost",
            card_event_id="$approval",
            status="denied" if status == "approved" else "approved",
            reason="late conflicting decision",
        )
        assert repeated.consumed is True
        assert repeated.resolved is False
        persisted = await journal.principal("router@shared").background_approval_decision(
            run_id="run-1",
            call_id="call-1",
        )
        assert persisted is not None
        assert persisted.status == status
        assert persisted.reason == "operator decision"
    finally:
        if not decision_task.done():
            decision_task.cancel()
            await asyncio.gather(decision_task, return_exceptions=True)
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_background_script_approval_expires_and_retires_without_a_response(tmp_path: Path) -> None:
    """An unanswered exact-call card expires through the shared deadline sweep."""
    manager, journal, _initial_sent = await _approval_manager(tmp_path)
    try:
        decision = await manager.request_background_approval(
            origin=_origin(),
            room_id="!room:localhost",
            thread_id="$thread",
            agent_name="watcher",
            requester_id="@alice:localhost",
            approver_user_id="@alice:localhost",
            tool_name="add",
            arguments={"a": 1, "b": 2},
            timeout_seconds=0.01,
        )

        assert decision.status == "expired"
        assert decision.reason == "Tool approval request timed out."
        assert await journal.principal("router@shared").is_terminal_approval_card(
            room_id="!room:localhost",
            card_event_id="$approval",
        )
    finally:
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_background_script_terminal_edit_is_recovered_after_restart(tmp_path: Path) -> None:
    """Restart recovery retries a failed terminal edit and retires the card."""
    first, first_journal, _initial_sent = await _approval_manager(tmp_path, fail_final=True)
    decision = await first.request_background_approval(
        origin=_origin(),
        room_id="!room:localhost",
        thread_id="$thread",
        agent_name="watcher",
        requester_id="@alice:localhost",
        approver_user_id="@alice:localhost",
        tool_name="add",
        arguments={"a": 1, "b": 2},
        timeout_seconds=0.01,
    )
    assert decision.status == "expired"
    await first.shutdown()
    await first_journal.close()

    recovered, recovered_journal, _initial_sent = await _approval_manager(tmp_path)
    try:
        sweep = await recovered.recover_cards_on_startup()

        assert sweep.complete is True
        assert await recovered_journal.principal("router@shared").is_terminal_approval_card(
            room_id="!room:localhost",
            card_event_id="$approval",
        )
    finally:
        await recovered.shutdown()
        await recovered_journal.close()
