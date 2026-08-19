"""End-to-end durable Matrix approvals for background-script calls."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

import pytest

from mindroom.approval_manager import _ApprovalManager
from mindroom.config.agent import AgentConfig
from mindroom.event_journal import (
    ApprovalCardReservation,
    BackgroundApprovalDecision,
    DeliveryStage,
    EventJournalStore,
    MatrixDelivery,
    StoredApprovalCard,
)
from mindroom.script_runs.broker import ScriptToolBroker, ScriptToolCallRequest
from mindroom.script_runs.models import ScriptCallState, ScriptToolGrant
from mindroom.tool_approval import BackgroundScriptToolOrigin
from tests.conftest import test_runtime_paths
from tests.test_script_run_manager import _context as _manager_context
from tests.test_script_run_manager import _manager
from tests.test_script_tool_broker import _call_through_gateway, _RuntimeResolver

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_launch_preapproval_does_not_expand_from_live_script_config(tmp_path: Path) -> None:
    """A live allowlist added after launch cannot bypass Matrix approval."""
    manager, _backend, worker_client = _manager(tmp_path)
    launch_context = _manager_context(tmp_path)
    launched = await manager.run(launch_context, source="print('ok')\n")
    durable_run = manager.store.get_run(launched.run_id)
    assert durable_run.preapprove_launch_grants is False

    live_watcher = AgentConfig(
        display_name="Watcher",
        worker_scope="user_agent",
        tools=["calculator", {"script": {"allowed_tools": ["calculator"]}}],
    )
    live_config = launch_context.config.model_copy(
        update={"agents": {**launch_context.config.agents, "watcher": live_watcher}},
    )
    approval_events: list[str] = []
    broker = ScriptToolBroker(
        store=manager.store,
        runtime_resolver=_RuntimeResolver(
            replace(launch_context, config_provider=lambda: live_config),
            approval_events=approval_events,
            worker_id=durable_run.worker_id,
        ),
    )
    token_path = worker_client.launch_paths[durable_run.run_id][1]
    request = ScriptToolCallRequest(
        run_id=durable_run.run_id,
        call_id="live-allowlist",
        grant=ScriptToolGrant("calculator", "add"),
        arguments={"a": 1, "b": 2},
    )

    receipt = await _call_through_gateway(broker, request, token_path.read_text(encoding="utf-8"))

    assert receipt.state is ScriptCallState.COMPLETED
    assert approval_events == [f"approval:{durable_run.run_id}:live-allowlist"]


def _origin(*, run_id: str = "run-1", call_id: str = "call-1") -> BackgroundScriptToolOrigin:
    return BackgroundScriptToolOrigin(
        run_id=run_id,
        call_id=call_id,
        requester_id="@alice:localhost",
        toolkit_name="calculator",
        function_name="add",
    )


async def _approval_manager(
    tmp_path: Path,
    *,
    database_name: str = "background-approval.db",
    fail_final: bool = False,
    unique_event_ids: bool = False,
    expected_initial_count: int = 1,
) -> tuple[_ApprovalManager, EventJournalStore, asyncio.Event]:
    journal = EventJournalStore.open_sqlite(tmp_path / database_name)
    cards = journal.principal("router@shared")
    initial_sent = asyncio.Event()
    initial_count = 0

    async def prepare_event(
        _room_id: str,
        _thread_id: str | None,
        content: dict[str, object],
    ) -> dict[str, object]:
        return content

    async def send(delivery: MatrixDelivery) -> str:
        nonlocal initial_count
        if delivery.stage is DeliveryStage.INITIAL:
            initial_count += 1
            if initial_count >= expected_initial_count:
                initial_sent.set()
            return f"$approval:{delivery.delivery_id}" if unique_event_ids else "$approval"
        if fail_final:
            message = "homeserver unavailable"
            raise RuntimeError(message)
        return f"$terminal:{delivery.delivery_id}" if unique_event_ids else "$terminal"

    manager = _ApprovalManager(
        test_runtime_paths(tmp_path),
        prepare_event=prepare_event,
        send_delivery=send,
        cards=cards,
        transport_sender=lambda: "@mindroom_router:localhost",
        sending_device=lambda: "DEVICE",
    )
    return manager, journal, initial_sent


async def _wait_for_pending_card(journal: EventJournalStore) -> StoredApprovalCard:
    cards = journal.principal("router@shared")
    for _attempt in range(1000):
        stored = await cards.pending_approval_card(
            room_id="!room:localhost",
            card_event_id="$approval",
        )
        if stored is not None:
            return stored
        await asyncio.sleep(0.001)
    message = "Approval delivery was not durably acknowledged."
    raise AssertionError(message)


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
        stored = await _wait_for_pending_card(journal)
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


@pytest.mark.asyncio
async def test_cancelled_background_call_is_denied_retired_and_pruned_with_run(tmp_path: Path) -> None:
    """Broker cancellation closes the exact card through shared resolution and recovery."""
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

        settled = await manager.settle_background_approval(
            _origin(),
            reason="Background script ownership was cancelled.",
        )
        decision = await asyncio.wait_for(decision_task, timeout=1.0)

        assert settled is True
        assert decision.status == "denied"
        assert decision.reason == "Background script ownership was cancelled."
        assert await journal.principal("router@shared").is_terminal_approval_card(
            room_id="!room:localhost",
            card_event_id="$approval",
        )
        assert await manager.prune_background_approvals("run-1") is True
        assert (
            await journal.principal("router@shared").background_approval_decision(
                run_id="run-1",
                call_id="call-1",
            )
            is None
        )
    finally:
        if not decision_task.done():
            decision_task.cancel()
            await asyncio.gather(decision_task, return_exceptions=True)
        await manager.shutdown()
        await journal.close()


@pytest.mark.asyncio
async def test_run_settlement_denies_only_pending_calls_without_revisiting_history(tmp_path: Path) -> None:
    """One run-level settlement ignores historical decisions and other runs."""
    historical_count = 20
    manager, journal, all_initial_sent = await _approval_manager(
        tmp_path,
        unique_event_ids=True,
        expected_initial_count=2,
    )
    run_cards = journal.principal("router@shared")
    pending_run_origin = _origin(call_id="pending")
    other_run_origin = _origin(run_id="run-2", call_id="pending")
    pending_run_task: asyncio.Task[BackgroundApprovalDecision] | None = None
    other_run_task: asyncio.Task[BackgroundApprovalDecision] | None = None
    try:
        for index in range(historical_count):
            call_id = f"history-{index}"
            assert await run_cards.reserve_background_approval_card(
                room_id="!room:localhost",
                thread_id="$thread",
                run_id="run-1",
                call_id=call_id,
                expires_at_ns=0,
                card=ApprovalCardReservation(
                    delivery_id=f"script-approval:run-1:{call_id}",
                    tool_call_id=call_id,
                    event_type="m.room.message",
                    payload={
                        "approval_target": "background_script",
                        "background_run_id": "run-1",
                        "background_call_id": call_id,
                        "tool_name": "add",
                        "status": "pending",
                    },
                ),
            )
            recorded = await run_cards.resolve_background_approval_call(
                run_id="run-1",
                call_id=call_id,
                requested_status="expired",
                reason="historical timeout",
            )
            assert recorded.recorded is True

        pending_run_task = asyncio.create_task(
            manager.request_background_approval(
                origin=pending_run_origin,
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
        other_run_task = asyncio.create_task(
            manager.request_background_approval(
                origin=other_run_origin,
                room_id="!room:localhost",
                thread_id="$thread",
                agent_name="watcher",
                requester_id="@alice:localhost",
                approver_user_id="@alice:localhost",
                tool_name="add",
                arguments={"a": 2, "b": 3},
                timeout_seconds=30.0,
            ),
        )
        await asyncio.wait_for(all_initial_sent.wait(), timeout=2.0)

        settled = await manager.settle_pending_background_approvals(
            "run-1",
            reason="Background script ownership was cancelled.",
        )
        decision = await asyncio.wait_for(pending_run_task, timeout=1.0)

        assert settled == 1
        assert decision.status == "denied"
        assert other_run_task.done() is False
        for index in range(historical_count):
            historical = await run_cards.background_approval_decision(
                run_id="run-1",
                call_id=f"history-{index}",
            )
            assert historical is not None
            assert historical.status == "expired"
        assert await manager.settle_pending_background_approvals("run-1", reason="repeated") == 0
    finally:
        await manager.settle_pending_background_approvals("run-2", reason="test cleanup")
        for task in (pending_run_task, other_run_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (pending_run_task, other_run_task) if task is not None),
            return_exceptions=True,
        )
        await manager.shutdown()
        await journal.close()
