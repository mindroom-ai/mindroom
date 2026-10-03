"""Approval interruption handoff to replacement recovery."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio

from mindroom.approval_manager import initialize_approval_store
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
from mindroom.runtime_shutdown import ENTITY_REMOVED_SHUTDOWN, SYNC_RESTART_SHUTDOWN
from mindroom.streaming import RESTART_INTERRUPTED_RESPONSE_NOTE
from mindroom.turn_record import TurnRecord
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _target
from tests.test_response_runner_focused import _admit_approval_source

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.runtime_shutdown import RuntimeShutdownIntent


@pytest.fixture(params=["active_cancel", "stale_claim"])
def entry(request: pytest.FixtureRequest) -> str:
    """Exercise both shared approval interruption entry paths."""
    return request.param


@pytest_asyncio.fixture
async def approval(tmp_path: Path, entry: str) -> tuple[AgentBot, ApprovalContinuation]:
    """Create real journal ownership and an acknowledged visible INITIAL."""
    bot = _bot(tmp_path)
    initialize_approval_store(bot.runtime_paths, cards=bot.journal_principal())
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
    """Run real journal/card settlement with Matrix transport and body reads replaced."""
    runner = unwrap_extracted_collaborator(bot._response_runner)
    with (
        patch.object(DeliveryGateway, "edit_text", new=edit),
        patch("mindroom.response_runner.fetch_latest_visible_body", new=AsyncMock(return_value=body)),
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
    assert len(await store.recovery_initial_deliveries()) == 1
    async with bot.response_recovery_scope("!room:localhost", "$waiting") as permitted:
        assert permitted
    with _recovery_runtime(bot, tmp_path, edit.await_args.args[0].new_text, policy) as (fleet, replacement, client):
        fleet._capture_replacement_recovery_rooms({"general": bot})
        await fleet._recover_pending_replacement_rooms(fleet.config)
        assert client.room_send.await_count == int(policy == "resume")
        if policy == "resume":
            assert (
                client.room_send.await_args.kwargs["content"]["m.relates_to"]["m.in_reply_to"]["event_id"] == "$waiting"
            )
        assert not replacement.pending_sync_restart_retry_room_ids
        assert not fleet._pending_replacement_recovery_room_ids


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
    async with bot.response_recovery_scope("!room:localhost", "$waiting") as permitted:
        assert not permitted


@pytest.mark.asyncio
async def test_replacement_stale_claim_after_first_scan_retries_after_claim_release(
    approval: tuple[AgentBot, ApprovalContinuation],
    tmp_path: Path,
) -> None:
    """A replacement-born interruption must wake recovery after the reload scan and live claim."""
    bot, _claimed = approval
    assert not bot.pending_sync_restart_retry_room_ids
    with _recovery_runtime(bot, tmp_path, "partial answer\n\n" + RESTART_INTERRUPTED_RESPONSE_NOTE) as (
        fleet,
        replacement,
        client,
    ):
        await _settle_after_reload_scan(fleet, replacement, client)
        fleet._capture_replacement_recovery_rooms({"general": replacement})
        await fleet._recover_pending_replacement_rooms(fleet.config)
        assert client.room_send.await_count == 1


@pytest.mark.asyncio
async def test_startup_recovers_approval_interruption_without_memory_markers(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
    tmp_path: Path,
) -> None:
    """Fresh runtime discovers committed interruption debt and resumes it only once."""
    bot, claimed = approval

    async def edit_notice(request: EditTextRequest) -> bool:
        return await _acknowledge(bot, request)

    edit = AsyncMock(side_effect=edit_notice)
    await _settle(bot, claimed, entry, edit)
    with _recovery_runtime(bot, tmp_path, edit.await_args.args[0].new_text) as (fleet, replacement, client):
        assert not fleet._pending_replacement_recovery_room_ids
        assert not replacement.pending_sync_restart_retry_room_ids
        await fleet._recover_stale_streams_after_restart([replacement], fleet.config, None, set())
        assert client.room_send.await_count == 1
        await fleet._recover_stale_streams_after_restart([replacement], fleet.config, None, set())
        assert client.room_send.await_count == 1


@contextmanager
def _recovery_runtime(
    bot: AgentBot,
    tmp_path: Path,
    body: str,
    policy: str = "resume",
) -> Iterator[tuple[_MultiAgentOrchestrator, AgentBot, AsyncMock]]:
    """Recreate runtime from the journal, with Matrix transport and history at their I/O seam."""
    config = bot.config
    config.defaults.auto_resume_after_restart = policy != "disabled"
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = config
    replacement = _bot(tmp_path)
    replacement.running = True
    assert not replacement.pending_sync_restart_retry_room_ids
    router_client = AsyncMock(spec=nio.AsyncClient)
    router_client.rooms = {"!room:localhost": nio.MatrixRoom("!room:localhost", "@mindroom_router:localhost")}
    router_client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!room:localhost"])
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
    history = [visible]
    if policy == "newer_human":
        history.append(
            ResolvedVisibleMessage.synthetic(
                event_id="$new-human",
                sender="@user:localhost",
                body="new request",
                timestamp=3,
            ),
        )

    async def send(
        *,
        room_id: str,
        message_type: str,
        content: dict[str, object],
        **_kwargs: object,
    ) -> nio.RoomSendResponse:
        assert room_id == "!room:localhost"
        assert message_type == "m.room.message"
        history.append(
            ResolvedVisibleMessage.synthetic(
                event_id="$resume",
                sender="@mindroom_router:localhost",
                body=str(content["body"]),
                content=content,
                timestamp=3,
            ),
        )
        return nio.RoomSendResponse(event_id="$resume", room_id="!room:localhost")

    router_client.room_send.side_effect = send
    with (
        patch("mindroom.matrix.stale_stream_cleanup.fetch_latest_visible_message", new=AsyncMock(return_value=visible)),
        patch(
            "mindroom.matrix.stale_stream_cleanup.fetch_thread_messages_from_source",
            new=AsyncMock(return_value=history),
        ),
    ):
        yield orchestrator, replacement, router_client


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
    bot._register_approval_interruption("$source", "!room:localhost")
    assert bot.pending_sync_restart_retry_room_ids == {"!room:localhost"}
    orchestrator.request_interrupted_turn_recovery.assert_not_called()


async def _begin_shutdown(orchestrator: _MultiAgentOrchestrator) -> None:
    """Exercise real shutdown admission, stopping before unrelated subsystem cleanup."""
    with (
        patch.object(type(orchestrator._script_runtime), "shutdown", new=AsyncMock()),
        patch(
            "mindroom.orchestrator.shutdown_approval_runtime",
            new=AsyncMock(side_effect=RuntimeError("stop boundary")),
        ),
        pytest.raises(RuntimeError, match="stop boundary"),
    ):
        await orchestrator.stop()


@pytest.mark.asyncio
async def test_late_interruption_completion_during_shutdown_does_not_start_recovery(tmp_path: Path) -> None:
    """Late response completion keeps its marker without reviving a stopped fleet worker."""
    bot = _bot(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = bot.config
    orchestrator.running = True
    orchestrator._runtime_ready_event.set()
    bot.orchestrator = orchestrator
    await _begin_shutdown(orchestrator)

    async def settle() -> None:
        bot._register_approval_interruption("$source", "!room:localhost")

    with patch.object(orchestrator, "_recover_pending_replacement_rooms", new=AsyncMock()) as scan:
        await asyncio.create_task(settle())
        await asyncio.sleep(0)  # Run the response task's registered completion callback.
        recovery_task = orchestrator._dispatch_recovery_task
        if recovery_task is not None:
            await recovery_task
        assert recovery_task is None
        scan.assert_not_awaited()
    assert bot.pending_sync_restart_retry_room_ids == {"!room:localhost"}
    assert orchestrator._pending_replacement_recovery_room_ids == {"general": {"!room:localhost"}}


@pytest.mark.asyncio
async def test_shutdown_while_recovery_waits_for_admission_preserves_pending_room(tmp_path: Path) -> None:
    """An existing worker must not scan after shutdown begins during its gate wait."""
    bot = _bot(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = bot.config
    orchestrator.running = True
    orchestrator._runtime_ready_event.set()
    waiting = asyncio.Event()
    gate = orchestrator._response_admission_gate
    gate.close()
    wait_for_admission = gate.wait_until_open

    async def wait() -> None:
        waiting.set()
        await wait_for_admission()

    with (
        patch.object(gate, "wait_until_open", new=wait),
        patch.object(orchestrator, "_recover_pending_replacement_rooms", new=AsyncMock()) as scan,
    ):
        orchestrator.request_interrupted_turn_recovery("general", "!room:localhost")
        recovery_task = orchestrator._dispatch_recovery_task
        assert recovery_task is not None
        try:
            await asyncio.wait_for(waiting.wait(), 5)
            await _begin_shutdown(orchestrator)
        finally:
            gate.reopen()
            await recovery_task
        scan.assert_not_awaited()
    assert orchestrator._pending_replacement_recovery_room_ids == {"general": {"!room:localhost"}}


@pytest.mark.asyncio
async def test_interruption_scan_error_retries_without_second_notification(tmp_path: Path) -> None:
    """A transient candidate failure restores the room and retries under the same worker."""
    bot = _bot(tmp_path)
    bot.running = True
    orchestrator = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    orchestrator.config = bot.config
    orchestrator.running = True
    orchestrator._runtime_ready_event.set()
    orchestrator.agent_bots = {"general": bot, ROUTER_AGENT_NAME: MagicMock(running=True, first_sync_complete=False)}
    calls = 0

    async def scan(*args: object, **_kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            message = "temporary candidate read failure"
            raise OSError(message)
        scanned_room_ids = args[3]
        assert isinstance(scanned_room_ids, set)
        scanned_room_ids.add("!room:localhost")

    with (
        patch.object(orchestrator, "_recover_stale_streams_after_restart", new=AsyncMock(side_effect=scan)),
        patch("mindroom.orchestration.runtime.retry_delay_seconds", return_value=0) as retry_delay,
    ):
        orchestrator.request_interrupted_turn_recovery("general", "!room:localhost")
        recovery_task = orchestrator._dispatch_recovery_task
        assert recovery_task is not None
        await asyncio.wait_for(recovery_task, 5)
    assert calls == 2
    retry_delay.assert_called_once()
    assert orchestrator._pending_replacement_recovery_room_ids == {}
    assert orchestrator._dispatch_recovery_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("collaborator", ["_turn_controller", "_edit_regenerator"])
async def test_ordinary_interruption_registry_does_not_schedule_recovery(tmp_path: Path, collaborator: str) -> None:
    """Ordinary and edit cancellation markers retain their passive capture-only behavior."""
    bot = _bot(tmp_path)
    fleet = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    bot.orchestrator = fleet
    owner = bot._turn_controller if collaborator == "_turn_controller" else bot._edit_regenerator
    rooms = unwrap_extracted_collaborator(owner).deps.interrupted_turn_rooms

    async def register() -> None:
        assert rooms.register("$source", room_id="!room:localhost")

    await asyncio.create_task(register())
    await asyncio.sleep(0)
    assert bot.pending_sync_restart_retry_room_ids == {"!room:localhost"}
    assert not fleet._pending_replacement_recovery_room_ids
    assert fleet._dispatch_recovery_task is None


async def _fence_bot(bot: AgentBot, intent: RuntimeShutdownIntent) -> None:
    """Enter the real bot shutdown boundary before unrelated response drains."""
    with (
        patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(side_effect=RuntimeError("drain boundary"))),
        pytest.raises(RuntimeError, match="drain boundary"),
    ):
        await bot.prepare_for_sync_shutdown(shutdown_intent=intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("readd", [False, True])
async def test_removed_lifecycle_deferred_approval_notification_is_ignored(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
    tmp_path: Path,
    readd: bool,
) -> None:
    """A removed bot cannot target either an absent name or a later bot reusing it."""
    bot, claimed = approval
    fleet = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    bot.orchestrator = fleet
    fleet.agent_bots = {"general": bot}
    settled = asyncio.Event()
    release = asyncio.Event()

    async def edit(request: EditTextRequest) -> bool:
        return await _acknowledge(bot, request)

    async def settle() -> None:
        await _settle(bot, claimed, entry, AsyncMock(side_effect=edit))
        settled.set()
        await release.wait()

    task = asyncio.create_task(settle())
    try:
        await asyncio.wait_for(settled.wait(), 5)
        await _fence_bot(bot, ENTITY_REMOVED_SHUTDOWN)
        fleet.agent_bots.pop("general")
        if readd:
            fleet.agent_bots["general"] = _bot(tmp_path)
    finally:
        release.set()
        await task
        await asyncio.sleep(0)
    assert not fleet._pending_replacement_recovery_room_ids


@pytest.mark.asyncio
async def test_approval_settlement_after_removal_keeps_only_durable_proof(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
) -> None:
    """Already removed lifecycles cannot create new recovery markers."""
    bot, claimed = approval
    fleet = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    bot.orchestrator = fleet
    await _fence_bot(bot, ENTITY_REMOVED_SHUTDOWN)

    async def edit(request: EditTextRequest) -> bool:
        return await _acknowledge(bot, request)

    await _settle(bot, claimed, entry, AsyncMock(side_effect=edit))
    assert not bot.pending_sync_restart_retry_room_ids
    assert not fleet._pending_replacement_recovery_room_ids


@pytest.mark.asyncio
async def test_replaced_bot_can_notify_approval_recovery_after_its_task_finishes(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
    tmp_path: Path,
) -> None:
    """A normal replacement retains the old bot's late approved recovery handoff."""
    bot, claimed = approval
    fleet = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    bot.orchestrator = fleet
    await _fence_bot(bot, SYNC_RESTART_SHUTDOWN)
    fleet.agent_bots = {"general": _bot(tmp_path)}

    async def edit(request: EditTextRequest) -> bool:
        return await _acknowledge(bot, request)

    await asyncio.create_task(_settle(bot, claimed, entry, AsyncMock(side_effect=edit)))
    await asyncio.sleep(0)
    assert fleet._pending_replacement_recovery_room_ids == {"general": {"!room:localhost"}}


@pytest.mark.asyncio
async def test_removal_clears_callback_delivered_while_cancelling_startup(
    approval: tuple[AgentBot, ApprovalContinuation],
    entry: str,
) -> None:
    """Removal clears late notifications before installing the bot's monotonic fence."""
    bot, claimed = approval
    fleet = _MultiAgentOrchestrator(runtime_paths=bot.runtime_paths)
    fleet.agent_bots = {"general": bot}
    bot.orchestrator = fleet
    settled = asyncio.Event()
    release = asyncio.Event()

    async def edit(request: EditTextRequest) -> bool:
        return await _acknowledge(bot, request)

    async def settle() -> None:
        await _settle(bot, claimed, entry, AsyncMock(side_effect=edit))
        settled.set()
        await release.wait()

    task = asyncio.create_task(settle())

    starting = asyncio.Event()

    async def startup() -> None:
        starting.set()
        try:
            await asyncio.Event().wait()
        finally:
            release.set()
            await task
            await asyncio.sleep(0)
            assert fleet._pending_replacement_recovery_room_ids == {"general": {"!room:localhost"}}

    startup_task = asyncio.create_task(startup())
    fleet._bot_start_tasks["general"] = startup_task
    try:
        await asyncio.wait_for(settled.wait(), 5)
        await asyncio.wait_for(starting.wait(), 5)
        with (
            patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock(side_effect=RuntimeError("drain boundary"))),
            pytest.raises(RuntimeError, match="drain boundary"),
        ):
            await fleet._remove_deleted_entities({"general"})
    finally:
        release.set()
        startup_task.cancel()
        await asyncio.gather(startup_task, return_exceptions=True)
        await task
    assert not fleet._pending_replacement_recovery_room_ids
