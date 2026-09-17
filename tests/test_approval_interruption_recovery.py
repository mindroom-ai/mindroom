"""Approval interruption handoff to replacement recovery."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio
from structlog.testing import capture_logs

from mindroom.constants import ROUTER_AGENT_NAME, STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.delivery_gateway import DeliveryGateway, EditTextRequest
from mindroom.event_journal import (
    ApprovalContinuation,
    DeliveryStage,
    EventClass,
    EventKind,
    InboundEvent,
    ProjectedEvent,
)
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.handled_turns import TurnRecordCodec
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.response_sources import ResponseAttempt, ResponseSources
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE
from mindroom.turn_record import TurnRecord
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _target
from tests.test_response_runner_focused import _admit_approval_source

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.bot import AgentBot


@pytest.fixture(params=["active_cancel", "stale_claim"])
def entry(request: pytest.FixtureRequest) -> str:
    """Exercise both shared approval interruption entry paths."""
    return request.param


@pytest_asyncio.fixture
async def approval(tmp_path: Path, entry: str) -> tuple[AgentBot, ApprovalContinuation]:
    """Create real journal ownership and an acknowledged visible INITIAL."""
    bot = _bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    store = runner.deps.approval_store
    await _admit_approval_source(store)
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        room_id="!room:localhost",
        thread_id="$thread",
        payload={"body": "Waiting"},
    )
    await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.INITIAL)
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.INITIAL,
        event_id="$waiting",
        delivered_projections=(),
    )
    continuation = ApprovalContinuation(
        approval_id="approval-recovery",
        run_id="run-1",
        session_id="session-1",
        entity_kind="agent",
        entity_name="general",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        response_event_id="$waiting",
        sources=ResponseSources(("$source",), ("$source",)),
        calls=(),
        state="ready",
    )
    assert await store.create_approval_continuation(continuation) == continuation
    claimed = await store.claim_approval_continuation(
        continuation.approval_id,
        runtime_generation=runner.deps.approval_runtime_generation if entry == "active_cancel" else "previous-runtime",
    )
    assert claimed is not None
    return bot, claimed


async def _acknowledge(bot: AgentBot, request: EditTextRequest) -> bool:
    """Persist an actual FINAL ACK at the mocked Matrix transport seam."""
    store = bot.journal_principal()
    await store.enqueue_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        room_id="!room:localhost",
        thread_id="$thread",
        edits_event_id="$waiting",
        payload={"body": "* " + request.new_text, "m.new_content": {"body": request.new_text, **request.extra_content}},
    )
    await store.claim_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
    await store.acknowledge_matrix_delivery(
        delivery_id="$source",
        stage=DeliveryStage.FINAL,
        event_id="$final-edit",
        delivered_projections=(),
    )
    return True


async def _settle(
    bot: AgentBot,
    claimed: ApprovalContinuation,
    entry: str,
    edit: AsyncMock,
    *,
    body: str | None = "partial answer",
) -> None:
    """Run real settlement with only Matrix/body and approval-card I/O replaced."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch.object(DeliveryGateway, "edit_text", new=edit),
        patch("mindroom.response_runner.fetch_latest_visible_body", new=AsyncMock(return_value=body)),
        patch(
            "mindroom.approval_response.approval_manager.get_approval_store",
            return_value=MagicMock(cards=None, expire_continuation_cards=AsyncMock(return_value=True)),
        ),
    ):
        if entry == "active_cancel":
            await runner._settle_failed_approval_outcome(
                claimed,
                FinalDeliveryOutcome(
                    terminal_status="cancelled",
                    event_id="$waiting",
                    is_visible_response=True,
                    failure_reason="sync_restart_cancelled",
                ),
            )
        else:
            await runner._recover_claimed_approval_lifecycle(
                claimed,
                target=_target(thread_id="$thread", reply_to_event_id="$source"),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["resume", "disabled", "newer_human"])
async def test_approval_interruption_hands_off_to_replacement(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
    tmp_path: Path,
    policy: str,
) -> None:
    """The real replacement scan resumes acknowledged approvals under existing policy."""
    bot, claimed = approval

    async def edit_notice(request: EditTextRequest) -> bool:
        return await _acknowledge(bot, request)

    edit = AsyncMock(side_effect=edit_notice)
    await _settle(bot, claimed, entry, edit)
    store = bot.journal_principal()
    assert await store.approval_continuation(claimed.approval_id) is None
    assert not await store.is_pending("$source")
    assert bot.pending_sync_restart_retry_room_ids == {"!room:localhost"}
    assert await store.recovery_initial_deliveries() == ()
    assert len(await store.recovery_initial_deliveries(include_interrupted_finals=True)) == 1
    async with bot.response_recovery_scope("!room:localhost", "$waiting") as permitted:
        assert not permitted
    await _assert_replacement_resumes(bot, tmp_path, edit.await_args.args[0].new_text, policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["unacknowledged", "failed", "wrong_body", "no_body"])
async def test_approval_interruption_requires_confirmed_notice(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
    transport: str,
) -> None:
    """A transport boolean or missing visible text cannot prove an interruption ACK."""
    bot, claimed = approval

    async def edit_notice(request: EditTextRequest) -> bool:
        if transport == "wrong_body":
            return await _acknowledge(bot, replace(request, new_text="another notice"))
        return transport == "unacknowledged"

    edit = AsyncMock(side_effect=edit_notice)
    await _settle(bot, claimed, entry, edit, body=None if transport == "no_body" else "partial answer")
    if transport == "no_body":
        edit.assert_not_awaited()
    assert not bot.pending_sync_restart_retry_room_ids


@pytest.mark.asyncio
async def test_missing_approval_owner_does_not_register_recovery(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
) -> None:
    """Successful no-op retirement is not evidence of a visible interruption."""
    bot, claimed = approval
    runner = unwrap_extracted_collaborator(bot._response_runner)
    failing = await runner._approval_responses.request_failure(claimed, "sync_restart_cancelled")
    assert failing is not None
    await bot._journal_store.backend.write(lambda tx: tx.execute("DELETE FROM approval_continuations"))
    edit = AsyncMock()
    await _settle(bot, failing, entry, edit)
    edit.assert_not_awaited()
    assert not bot.pending_sync_restart_retry_room_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ownership",
    ["retired", "wrong_target", "successful", "stopped", "superseded", "permanent_failure", "deleted"],
)
async def test_terminal_ownership_blocks_approval_recovery(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
    ownership: str,
) -> None:
    """Terminal ownership wins even when the interruption transport reports success."""
    bot, claimed = approval

    async def edit_notice(request: EditTextRequest) -> bool:
        await _acknowledge(bot, request)
        await _alter_delivery_ownership(bot, ownership)
        return True

    await _settle(bot, claimed, entry, AsyncMock(side_effect=edit_notice))
    assert not bot.pending_sync_restart_retry_room_ids
    async with bot.response_recovery_scope("!room:localhost", "$waiting", allow_interrupted_final=True) as permitted:
        assert not permitted


@pytest.mark.asyncio
async def test_replacement_stale_claim_after_first_scan_retries_after_claim_release(
    approval: tuple[AgentBot, ApprovalContinuation],
    tmp_path: Path,
) -> None:
    """A replacement-born interruption must wake recovery after the reload scan and live claim."""
    bot, _claimed = approval
    assert not bot.pending_sync_restart_retry_room_ids
    await _assert_replacement_resumes(
        bot,
        tmp_path,
        "partial answer\n\n" + RESTART_INTERRUPTED_RESPONSE_NOTE,
        "resume",
        late_claim=True,
    )


async def _assert_replacement_resumes(
    bot: AgentBot,
    tmp_path: Path,
    body: str,
    policy: str,
    *,
    late_claim: bool = False,
) -> None:
    """Capture the retiring bot's registration and scan real outbox debt on its replacement."""
    config = bot.config
    config.defaults.auto_resume_after_restart = policy != "disabled"
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = config
    orchestrator._capture_replacement_recovery_rooms({"general": bot})
    replacement = _bot(tmp_path)
    replacement.running = True
    assert not replacement.pending_sync_restart_retry_room_ids
    router_client = AsyncMock(spec=nio.AsyncClient)
    router_client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost")}
    router_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
    router_client.room_send.return_value = nio.RoomSendResponse(event_id="$resume", room_id="!room:localhost")
    router = MagicMock(running=True, client=router_client)
    orchestrator.agent_bots = {"general": replacement, ROUTER_AGENT_NAME: router}
    source = nio.RoomMessageText.from_dict(
        {
            "event_id": "$source",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "type": "m.room.message",
            "room_id": "!room:localhost",
            "content": {"msgtype": "m.text", "body": "run it"},
        },
    )
    source.source = source.__dict__["source"]
    source_response = nio.RoomGetEventResponse()
    source_response.event = source
    replacement.client.room_get_event.side_effect = None
    replacement.client.room_get_event.return_value = source_response

    async def no_relations(*_args: object, **_kwargs: object) -> AsyncIterator[nio.Event]:
        for event in ():
            yield event

    replacement.client.room_get_event_relations = no_relations
    visible = ResolvedVisibleMessage.synthetic(
        event_id="$waiting",
        sender="@mindroom_general:localhost",
        body=body,
        timestamp=2,
        thread_id="$thread",
        content={
            STREAM_STATUS_KEY: STREAM_STATUS_ERROR,
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread",
                "m.in_reply_to": {"event_id": "$source"},
            },
        },
    )
    with (
        patch("mindroom.matrix.stale_stream_cleanup.fetch_latest_visible_message", new=AsyncMock(return_value=visible)),
        patch(
            "mindroom.matrix.stale_stream_cleanup.fetch_thread_messages_from_source",
            new=AsyncMock(
                return_value=[
                    visible,
                    *(
                        [
                            ResolvedVisibleMessage.synthetic(
                                event_id="$new-human",
                                sender="@user:localhost",
                                body="new request",
                                timestamp=3,
                            ),
                        ]
                        if policy == "newer_human"
                        else []
                    ),
                ],
            ),
        ) as source_history,
        capture_logs() as logs,
    ):
        if late_claim:
            await _settle_after_reload_scan(orchestrator, replacement, router_client)
            resume_content = router_client.room_send.await_args.kwargs["content"]
            source_history.return_value.append(
                ResolvedVisibleMessage.synthetic(
                    event_id="$resume",
                    sender="@mindroom_router:localhost",
                    body=resume_content["body"],
                    content=resume_content,
                    timestamp=3,
                ),
            )
            orchestrator._capture_replacement_recovery_rooms({"general": replacement})
            await orchestrator._recover_pending_replacement_rooms(config)
        else:
            await orchestrator._recover_pending_replacement_rooms(config)
    assert router_client.room_send.await_count == int(policy == "resume"), logs
    if policy == "resume":
        content = router_client.room_send.await_args.kwargs["content"]
        assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$waiting"
    assert not orchestrator._pending_replacement_recovery_room_ids


async def _alter_delivery_ownership(bot: AgentBot, delivery_state: str) -> None:
    """Simulate terminal ownership changes after a transport ACK."""
    store = bot.journal_principal()
    if delivery_state in {"stopped", "superseded"}:
        record = TurnRecord.create(
            ("$source",),
            response_owner="general",
            response_event_id="$waiting",
            conversation_target=_target(thread_id="$thread", reply_to_event_id="$source"),
            user_stop_receipt_order=5 if delivery_state == "stopped" else None,
        )
        await bot._journal_store.turn_records("general").upsert(
            index_event_ids=("$source",),
            anchor_event_id="$source",
            record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
        )
    if delivery_state == "superseded":
        await _admit_approval_source(store, event_id="$newer")
        await store.enqueue_matrix_delivery(
            delivery_id="$newer",
            stage=DeliveryStage.FINAL,
            room_id="!room:localhost",
            thread_id="$thread",
            payload={"body": "new answer"},
            edits_event_id="$waiting",
            response_attempt=ResponseAttempt(
                "general",
                ResponseSources(("$newer",), ("$source",), edit_receipt_order=2),
            ),
            result={"body": "new answer"},
        )
        await store.claim_matrix_delivery(delivery_id="$newer", stage=DeliveryStage.FINAL)
        await store.acknowledge_matrix_delivery(
            delivery_id="$newer",
            stage=DeliveryStage.FINAL,
            event_id="$newer-final",
            delivered_projections=(),
        )
    if delivery_state == "wrong_target":
        await bot._journal_store.backend.write(
            lambda tx: tx.execute(
                "UPDATE matrix_delivery_outbox SET edits_event_id = '$other' WHERE stage = 'final'",
            ),
        )
    if delivery_state == "permanent_failure":
        await bot._journal_store.backend.write(
            lambda tx: tx.execute(
                "UPDATE matrix_delivery_outbox SET permanent_failure_reason = 'failed' WHERE stage = 'final'",
            ),
        )
    if delivery_state == "retired":
        await bot._journal_store.backend.write(
            lambda tx: tx.execute("UPDATE matrix_delivery_outbox SET retired = 1 WHERE stage = 'final'"),
        )
    if delivery_state == "successful":
        await bot._journal_store.backend.write(
            lambda tx: tx.execute(
                "UPDATE matrix_delivery_outbox SET result_json = ? WHERE stage = 'final'",
                (json.dumps({"body": "finished"}),),
            ),
        )
    if delivery_state == "deleted":
        await store.admit(
            InboundEvent(
                event_id="$redaction",
                room_id="!room:localhost",
                thread_id=None,
                kind=EventKind.REDACTION,
                event_class=EventClass.CONTEXT_ONLY,
                sender="@user:localhost",
                origin_server_ts=3,
                source={"event_id": "$redaction", "redacts": "$source", "content": {}},
            ),
            ProjectedEvent(
                event_id="$redaction",
                room_id="!room:localhost",
                thread_id=None,
                sender="@user:localhost",
                origin_server_ts=3,
                content={},
                replaces_event_id=None,
                redacts_event_id="$source",
            ),
        )


async def _settle_after_reload_scan(  # noqa: PLR0915 - Keep the ordered reload and claim-release regression together.
    orchestrator: _MultiAgentOrchestrator,
    replacement: AgentBot,
    router_client: AsyncMock,
) -> None:
    """Use the actual approval admission path, retaining its live claim past registration."""
    replacement.orchestrator = orchestrator
    replacement.admission_gate = gate = orchestrator._response_admission_gate
    orchestrator._runtime_ready_event.set()
    gate.close()
    await orchestrator._recover_pending_replacement_rooms(replacement.config)
    runner = unwrap_extracted_collaborator(replacement._response_runner)
    waiting = asyncio.Event()
    settled = asyncio.Event()
    release_claim = asyncio.Event()
    wait_for_admission = gate.wait_until_open
    claim = TurnRecord.create(
        ("$source",),
        completed=False,
        response_owner="general",
        response_event_id="$waiting",
        conversation_target=_target(thread_id="$thread", reply_to_event_id="$source"),
    )
    await replacement._turn_store.record_pending_turn(claim)

    async def wait() -> None:
        waiting.set()
        await wait_for_admission()

    async def edit_notice(request: EditTextRequest) -> bool:
        return await _acknowledge(replacement, request)

    async def resume() -> None:
        assert replacement._turn_store.try_claim_turn(claim)
        try:
            await runner._resume_approval_source("$source")
            settled.set()
            await release_claim.wait()
        finally:
            replacement._turn_store.release_pending_turn_claim(claim)

    with (
        patch.object(gate, "wait_until_open", new=wait),
        patch.object(DeliveryGateway, "edit_text", new=AsyncMock(side_effect=edit_notice)),
        patch("mindroom.response_runner.fetch_latest_visible_body", new=AsyncMock(return_value="partial answer")),
        patch(
            "mindroom.approval_response.approval_manager.get_approval_store",
            return_value=MagicMock(cards=None, expire_continuation_cards=AsyncMock(return_value=True)),
        ),
    ):
        response_task = asyncio.create_task(resume())
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            assert not replacement.pending_sync_restart_retry_room_ids
            gate.reopen()
            await asyncio.wait_for(settled.wait(), 5)
            assert replacement.pending_sync_restart_retry_room_ids == {"!room:localhost"}
            # An early capture cannot resume the still-owned turn, and consumes its room.
            orchestrator._capture_replacement_recovery_rooms({"general": replacement})
            await orchestrator._recover_pending_replacement_rooms(replacement.config)
            assert not orchestrator._pending_replacement_recovery_room_ids
            router_client.room_send.assert_not_awaited()
            waiting.clear()
            gate.close()
        finally:
            if not settled.is_set():
                gate.reopen()
            release_claim.set()
            await asyncio.wait_for(response_task, 5)
        recovery_task = orchestrator._dispatch_recovery_task
        assert recovery_task is not None
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            assert gate.closed
            assert not recovery_task.done()
            router_client.room_send.assert_not_awaited()
        finally:
            gate.reopen()
        await asyncio.wait_for(recovery_task, 5)


def test_interruption_registration_without_running_loop_retains_room(tmp_path: Path) -> None:
    """Synchronous registry callers retain capture state without starting async work."""
    bot = _bot(tmp_path)
    orchestrator = MagicMock()
    bot.orchestrator = orchestrator
    assert bot._interrupted_turn_rooms.register("$source", room_id="!room:localhost")
    assert bot.pending_sync_restart_retry_room_ids == {"!room:localhost"}
    orchestrator.request_interrupted_turn_recovery.assert_not_called()
