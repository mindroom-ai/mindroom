"""Focused durable-state tests for suspended tool approval continuations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from mindroom.approval_continuation import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalContinuationStore,
    ApprovalDecision,
    ApprovalMemoryTurn,
)
from mindroom.approval_manager import ApprovalStartupSweep
from mindroom.approval_transport import ApprovalMatrixTransport
from mindroom.constants import RuntimePaths
from mindroom.event_journal import DepartureSource, EventJournalStore, StoredApprovalCard
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.turn_origin import SenderKind, TurnIntent, TurnOrigin, TurnTrust

if TYPE_CHECKING:
    from pathlib import Path


def _continuation() -> ApprovalContinuation:
    return ApprovalContinuation(
        approval_id="approval-1",
        run_id="run-1",
        session_id="session-1",
        entity_kind="team",
        entity_name="research",
        room_id="!room:example.org",
        thread_id="$thread",
        requester_id="@owner:example.org",
        response_event_id="$waiting",
        execution_identity={"channel": "matrix", "agent_name": "research"},
        source_event_ids=("$source",),
        calls=(
            ApprovalCall(
                tool_call_id="call-1",
                tool_name="dangerous",
                invoking_agent="researcher",
                expires_at="2099-08-12T00:00:00+00:00",
            ),
            ApprovalCall(
                tool_call_id="call-2",
                tool_name="safe",
                invoking_agent="analyst",
                expires_at="2099-08-12T00:00:00+00:00",
                decision=ApprovalDecision.APPROVED,
                decision_recorded=True,
            ),
        ),
        request_body="Run the dangerous tool",
        transport_sender_id="@owner:example.org",
        source_kind="message",
    )


def _finish_test_failure(store: ApprovalContinuationStore, approval_id: str, reason: str) -> None:
    current = store.get(approval_id)
    assert current is not None
    requested = (
        store.fence_claimed_failure(approval_id, current.claimant_id)
        if current.state == "claimed" and current.claimant_id is not None
        else store.request_failure(
            approval_id,
            reason,
            claimant_id=current.claimant_id,
            runtime_generation="test-runtime",
        )
    )
    assert requested is not None
    assert requested.state == "settling"
    failed = store.finish_failure(approval_id, reason)
    assert failed is not None
    assert failed.state == "failed"


def test_continuation_store_commits_first_call_decision_and_one_claim(tmp_path: Path) -> None:
    """Removing either guard would let duplicate Matrix actions execute a tool twice."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())

    resolved = store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    duplicate = store.resolve_call("approval-1", "call-1", ApprovalDecision.DENIED)
    acknowledged = store.acknowledge_call("approval-1", "call-1")

    assert resolved is not None
    assert resolved.state == "pending"
    assert duplicate is not None
    assert duplicate.calls[0].decision is ApprovalDecision.APPROVED
    assert acknowledged is not None
    assert acknowledged.state == "ready"
    assert store.claim("approval-1", "worker-1") is not None
    assert store.claim("approval-1", "worker-2") is None


def test_approval_commit_rechecks_exact_call_deadline(tmp_path: Path) -> None:
    """An approval that reaches the durable boundary after expiry must not authorize execution."""
    store = ApprovalContinuationStore(tmp_path)
    continuation = _continuation()
    store.create(
        replace(
            continuation,
            calls=(replace(continuation.calls[0], expires_at="2000-01-01T00:00:00+00:00"),),
        ),
    )

    resolved = store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)

    assert resolved is not None
    assert resolved.calls[0].decision is ApprovalDecision.EXPIRED
    assert resolved.calls[0].reason == "Tool approval request timed out."


def test_approval_update_atomically_guards_exact_call_deadline(tmp_path: Path) -> None:
    """The SQL update must reject expiry even when the earlier Python check observed validity."""
    store = ApprovalContinuationStore(tmp_path)
    continuation = _continuation()
    store.create(
        replace(
            continuation,
            calls=(replace(continuation.calls[0], expires_at="2000-01-01T00:00:00+00:00"),),
        ),
    )

    with patch("mindroom.approval_continuation._call_expired", return_value=False):
        resolved = store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)

    assert resolved is not None
    assert resolved.calls[0].decision is ApprovalDecision.EXPIRED
    assert resolved.calls[0].reason == "Tool approval request timed out."


def test_failure_fence_cannot_take_a_claim_won_by_another_owner(tmp_path: Path) -> None:
    """A stale failure path must not invalidate a claim whose tool execution already won."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())
    store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = store.acknowledge_call("approval-1", "call-1")
    assert ready is not None
    claimed = store.claim("approval-1", "resume-owner")
    assert claimed is not None

    fenced = store.request_failure(
        "approval-1",
        "stale publication failure",
        claimant_id=None,
        runtime_generation="runtime-current",
    )

    assert fenced is None
    persisted = store.get("approval-1")
    assert persisted is not None
    assert persisted.state == "claimed"
    assert persisted.claimant_id == "resume-owner"


def test_claim_failure_waits_for_final_outbox_reconciliation_before_settling(tmp_path: Path) -> None:
    """A failed runner records its reason without overwriting a possibly frozen successful FINAL."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())
    store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    store.acknowledge_call("approval-1", "call-1")
    claimed = store.claim("approval-1", "resume-owner")
    assert claimed is not None

    noted = store.request_failure(
        "approval-1",
        "final delivery failed",
        claimant_id="resume-owner",
        runtime_generation="runtime-current",
    )
    fenced = store.fence_claimed_failure("approval-1", "resume-owner")

    assert noted is not None
    assert noted.state == "claimed"
    assert noted.failure_reason == "final delivery failed"
    assert fenced is not None
    assert fenced.state == "settling"
    assert fenced.failure_reason == "final delivery failed"


def test_failure_request_is_idempotent_and_preserves_its_first_reason(tmp_path: Path) -> None:
    """The transport's one dispatcher may safely repeat one durable settlement request."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())

    first = store.request_failure(
        "approval-1",
        "first reason",
        claimant_id=None,
        runtime_generation="runtime-current",
    )
    repeated = store.request_failure(
        "approval-1",
        "replacement reason",
        claimant_id=None,
        runtime_generation="runtime-current",
    )

    assert first is not None
    assert first.state == "settling"
    assert first.failure_reason == "first reason"
    assert repeated is not None
    assert repeated == first


def test_distinct_store_handles_serialize_pending_context_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Card attachment and a decision must not overwrite each other through separate store handles."""
    card_store = ApprovalContinuationStore(tmp_path)
    decision_store = ApprovalContinuationStore(tmp_path)
    card_store.create(_continuation())
    card_read = threading.Event()
    allow_card_write = threading.Event()
    decision_done = threading.Event()
    original_get = card_store.get

    def blocking_get(approval_id: str) -> ApprovalContinuation | None:
        current = original_get(approval_id)
        if not card_read.is_set():
            card_read.set()
            assert allow_card_write.wait(timeout=5)
        return current

    monkeypatch.setattr(card_store, "get", blocking_get)
    card_thread = threading.Thread(
        target=card_store.attach_card,
        args=("approval-1", "call-1", "$card"),
    )

    def resolve() -> None:
        decision_store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
        decision_done.set()

    decision_thread = threading.Thread(target=resolve)
    card_thread.start()
    assert card_read.wait(timeout=5)
    decision_thread.start()
    interleaved = decision_done.wait(timeout=0.1)
    allow_card_write.set()
    card_thread.join(timeout=5)
    decision_thread.join(timeout=5)

    assert not interleaved
    persisted = card_store.get("approval-1")
    assert persisted is not None
    assert persisted.calls[0].card_event_id == "$card"
    assert persisted.calls[0].decision is ApprovalDecision.APPROVED


def test_same_state_write_rejects_a_stale_context_revision(tmp_path: Path) -> None:
    """Two pending-state writers must not overwrite the first committed context update."""
    store = ApprovalContinuationStore(tmp_path)
    original = store.create(_continuation())
    first = replace(original, request_body="first writer")
    stale_second = replace(original, request_body="stale second writer")

    assert store._persist(original, first) == first
    assert store._persist(original, stale_second) is None
    assert store.get(original.approval_id) == first


def test_legacy_context_without_additive_fields_remains_mutable(tmp_path: Path) -> None:
    """Additive metadata must not make rows written by an earlier release immutable."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())
    table = store._approval_table()
    assert table is not None
    with store._db.Session() as session, session.begin():
        row = session.execute(select(table).where(table.c.id == "approval-1")).fetchone()
        assert row is not None
        legacy_context = dict(row._mapping["context"])
        legacy_context.pop("history_scope")
        legacy_context.pop("delivery_principal_id")
        legacy_context.pop("origin")
        legacy_context.pop("memory_prompt")
        legacy_context.pop("memory_thread_history")
        legacy_context.pop("thread_summary_message_count_hint")
        session.execute(table.update().where(table.c.id == "approval-1").values(context=legacy_context))

    loaded = store.get("approval-1")
    assert loaded is not None
    assert loaded.history_scope is None
    assert loaded.delivery_principal_id is None
    assert loaded.origin is None
    assert loaded.memory_prompt is None
    assert loaded.memory_thread_history == ()
    assert loaded.thread_summary_message_count_hint is None
    resolved = store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)

    assert resolved is not None
    assert resolved.calls[0].decision is ApprovalDecision.APPROVED


def test_decision_write_failure_never_reads_as_a_winning_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation write error must surface instead of releasing a tool."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())
    monkeypatch.setattr(store._db, "Session", MagicMock(side_effect=RuntimeError("database unavailable")))

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)

    monkeypatch.undo()
    persisted = store.get("approval-1")
    assert persisted is not None
    assert persisted.calls[0].decision is None


def test_continuation_store_recovers_pending_and_claimed_without_copying_arguments(tmp_path: Path) -> None:
    """Reload recovery needs routing metadata, while exact arguments remain owned by the Agno run."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())
    store.resolve_call("approval-1", "call-1", ApprovalDecision.DENIED)
    store.acknowledge_call("approval-1", "call-1")
    store.claim("approval-1", "worker-1")

    recovered = ApprovalContinuationStore(tmp_path).recoverable()

    assert len(recovered) == 1
    assert recovered[0].state == "claimed"
    assert recovered[0].calls[0].tool_call_id == "call-1"
    assert "arguments" not in recovered[0]._to_context()


def test_continuation_store_preserves_runtime_model_snapshot(tmp_path: Path) -> None:
    """A thread-model run must resume with the model that created the persisted pause."""
    store = ApprovalContinuationStore(tmp_path)
    continuation = replace(_continuation(), runtime_model_name="thread-model")

    store.create(continuation)

    assert store.get(continuation.approval_id) == continuation


def test_continuation_store_preserves_resumed_lifecycle_inputs(tmp_path: Path) -> None:
    """Restart must preserve the exact hook identity and memory inputs of the paused turn."""
    store = ApprovalContinuationStore(tmp_path)
    origin = TurnOrigin(
        transport_sender_id="@mindroom_router:example.org",
        requester_id="@owner:example.org",
        sender_entity_name="router",
        requester_entity_name=None,
        sender_kind=SenderKind.MANAGED_ENTITY,
        requester_kind=SenderKind.USER,
        intent=TurnIntent.ROUTER_HANDOFF,
        source_kind="message",
        trust=TurnTrust.TRUSTED_INTERNAL,
    )
    continuation = replace(
        _continuation(),
        origin=origin,
        memory_prompt="Remember the deployment outcome",
        memory_thread_history=(
            ApprovalMemoryTurn(sender="@owner:example.org", body="Ship it"),
            ApprovalMemoryTurn(sender="@mindroom_general:example.org", body="Checking"),
        ),
        thread_summary_message_count_hint=7,
    )

    store.create(continuation)
    restored = store.get(continuation.approval_id)

    assert restored is not None
    assert restored.origin == origin
    assert restored.memory_prompt == "Remember the deployment outcome"
    assert restored.memory_thread_history == continuation.memory_thread_history
    assert restored.thread_summary_message_count_hint == 7


def test_claimed_continuation_atomically_advances_to_next_pause(tmp_path: Path) -> None:
    """A second gated tool replaces the claimed run without a crash window between rows."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())
    store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    store.acknowledge_call("approval-1", "call-1")
    assert store.claim("approval-1", "worker-1") is not None
    next_call = ApprovalCall(
        tool_call_id="call-next",
        tool_name="dangerous_again",
        invoking_agent="researcher",
        expires_at="2026-08-12T01:00:00+00:00",
    )

    advanced = store.advance_pause(
        "approval-1",
        "worker-1",
        run_id="run-2",
        session_id="session-1",
        calls=(next_call,),
    )

    assert advanced is not None
    assert advanced.state == "pending"
    assert advanced.run_id == "run-2"
    assert advanced.generation == 1
    assert advanced.calls == (next_call,)


def test_continuation_store_owns_source_before_outer_turn_settlement(tmp_path: Path) -> None:
    """A replay after durable suspension must adopt the continuation instead of running the tool turn again."""
    store = ApprovalContinuationStore(tmp_path)
    continuation = _continuation()
    store.create(continuation)

    assert store.for_source_event("$source") == continuation
    assert store.for_source_event("$unrelated") is None


def test_publishing_continuation_binds_visible_response_atomically(tmp_path: Path) -> None:
    """The source owner must exist before delivery and become actionable only with its event ID."""
    store = ApprovalContinuationStore(tmp_path)
    publishing = replace(_continuation(), response_event_id=None, state="publishing")
    store.create(publishing)

    published = store.bind_response_event(
        publishing.approval_id,
        "$waiting",
        state="pending",
        calls=publishing.calls,
    )

    assert published is not None
    assert published.state == "pending"
    assert published.response_event_id == "$waiting"
    assert store.for_source_event("$source") == published


def test_terminal_continuation_releases_source_replay_ownership(tmp_path: Path) -> None:
    """Settled rows must not suppress a later legitimate delivery that reuses the source lookup."""
    store = ApprovalContinuationStore(tmp_path)
    continuation = _continuation()
    store.create(continuation)
    _finish_test_failure(store, continuation.approval_id, "settled")

    assert store.for_source_event("$source") is None


def test_recovery_pages_through_every_continuation(tmp_path: Path) -> None:
    """Startup recovery must not silently strand rows beyond Agno's first result page."""
    store = ApprovalContinuationStore(tmp_path)
    template = _continuation()
    for index in range(101):
        store.create(replace(template, approval_id=f"approval-{index}"))

    recovered = store.recoverable()

    assert len(recovered) == 101


def test_recovery_read_failure_is_not_misreported_as_an_empty_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup must retry a failed continuation read instead of replaying owned source events."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())
    monkeypatch.setattr(store._db, "Session", MagicMock(side_effect=RuntimeError("database unavailable")))

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.recoverable()


@pytest.mark.asyncio
async def test_recovery_attaches_card_delivered_before_crash(tmp_path: Path) -> None:
    """A crash after Matrix delivery but before attachment must not strand the continuation."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    cards = AsyncMock()
    cards.pending_approval_cards.side_effect = [
        (
            StoredApprovalCard(
                card={
                    "content": {
                        "continuation_id": "approval-1",
                        "tool_call_id": "call-1",
                    },
                },
                resolution=None,
                transaction_id="mindroom-approval-approval-1-0",
                card_event_id="$approval",
                attempted=True,
                sending_device_id="DEVICE",
                created_at_ns=1,
            ),
        ),
        (),
    ]
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: None,
        cards_provider=lambda: cards,
    )
    transport._continuations.create(_continuation())

    recovered, publication_unresolved = await transport._attach_recovered_cards(_continuation())

    assert recovered.calls[0].card_event_id == "$approval"
    assert publication_unresolved is False
    assert transport._continuations.get("approval-1") == recovered


@pytest.mark.asyncio
async def test_startup_identifies_unacknowledged_card_before_continuation_recovery(tmp_path: Path) -> None:
    """A Matrix-accepted card must be adopted before missing-card recovery can fail its continuation."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    unacknowledged = StoredApprovalCard(
        card={
            "content": {
                "continuation_id": "approval-1",
                "tool_call_id": "call-1",
            },
        },
        resolution=None,
        transaction_id="mindroom-approval-approval-1-0",
        card_event_id=None,
        attempted=True,
        sending_device_id="DEVICE",
        created_at_ns=1,
    )
    acknowledged = replace(unacknowledged, card_event_id="$approval")
    cards = AsyncMock()
    cards.pending_approval_cards.side_effect = [(unacknowledged,), ()]
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: None,
        cards_provider=lambda: cards,
    )
    transport._continuations.create(_continuation())
    transport._startup_router_ready_for_cleanup = True
    transport._startup_runtime_support_ready_for_cleanup = True

    async def identify_matrix_card() -> ApprovalStartupSweep:
        cards.pending_approval_cards.side_effect = None
        cards.pending_approval_cards.return_value = (acknowledged,)
        return ApprovalStartupSweep(discarded=0, failed=0, scanned=1)

    with patch(
        "mindroom.approval_transport.expire_orphaned_approval_cards_on_startup",
        new=AsyncMock(side_effect=identify_matrix_card),
    ) as sweep:
        await transport._run_startup_cleanup_if_ready()

    await transport.cancel_startup_cleanup_retry()
    recovered = transport._continuations.get("approval-1")
    assert recovered is not None
    assert recovered.state == "pending"
    assert recovered.calls[0].card_event_id == "$approval"
    sweep.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_startup_defers_continuation_while_attempted_card_outcome_is_unknown(tmp_path: Path) -> None:
    """A transient card-recovery miss must not terminalize a possibly visible approval."""
    unacknowledged = StoredApprovalCard(
        card={
            "content": {
                "continuation_id": "approval-1",
                "tool_call_id": "call-1",
            },
        },
        resolution=None,
        transaction_id="mindroom-approval-approval-1-0",
        card_event_id=None,
        attempted=True,
        sending_device_id="DEVICE",
        created_at_ns=1,
    )
    cards = AsyncMock()
    cards.pending_approval_cards.side_effect = [(unacknowledged,), ()]
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda _name: None,
        cards_provider=lambda: cards,
    )
    continuation = _continuation()
    transport._continuations.create(continuation)

    with patch.object(
        transport,
        "_fail_recovered_continuation",
        new=AsyncMock(return_value=True),
    ) as fail:
        complete = await transport._recover_pending_continuation(continuation)

    assert complete is False
    fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_decision_waits_through_reload_gap_and_resumes_once(tmp_path: Path) -> None:
    """A missing replacement bot is transient, while duplicate clicks must not duplicate execution."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    bots: dict[str, object] = {}
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda name: cast("Any", bots.get(name)),
        cards_provider=lambda: None,
        entity_configured=lambda name: name == "research",
    )
    transport._continuations.create(_continuation())

    await asyncio.gather(
        transport._handle_continuation_decision("approval-1", "call-1", "approved", None),
        transport._handle_continuation_decision("approval-1", "call-1", "denied", "duplicate"),
    )
    await transport._handle_continuation_decision_ready("approval-1", "call-1")
    await asyncio.sleep(0.3)

    async def resume(continuation: ApprovalContinuation) -> None:
        claimed = transport._continuations.claim(continuation.approval_id, "replacement")
        assert claimed is not None
        transport._continuations.complete(continuation.approval_id, "replacement")

    resume_mock = AsyncMock(side_effect=resume)
    bots["research"] = SimpleNamespace(running=True, resume_approval_continuation=resume_mock)
    for _attempt in range(20):
        current = transport._continuations.get("approval-1")
        if current is not None and current.state == "completed":
            break
        await asyncio.sleep(0.05)
    await transport.cancel_startup_cleanup_retry()

    assert resume_mock.await_count == 1
    completed = transport._continuations.get("approval-1")
    assert completed is not None
    assert completed.calls[0].decision is ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_unpersisted_approval_is_reported_as_denied(tmp_path: Path) -> None:
    """The Matrix card must never show approval when the continuation write failed."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: None,
        cards_provider=lambda: None,
    )
    transport._continuations.resolve_call = MagicMock(return_value=None)

    status, reason = await transport._handle_continuation_decision(
        "approval-1",
        "call-1",
        "approved",
        None,
    )

    assert status == "denied"
    assert reason == "Approval decision could not be persisted; the tool was denied safely."


@pytest.mark.asyncio
async def test_unknown_exact_call_is_reported_as_denied(tmp_path: Path) -> None:
    """A stale or forged tool-call ID must never make a card visibly approved."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: None,
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())

    status, reason = await transport._handle_continuation_decision(
        "approval-1",
        "unknown-call",
        "approved",
        None,
    )

    assert status == "denied"
    assert reason == "The exact paused tool call no longer exists; the tool was denied safely."


@pytest.mark.asyncio
async def test_startup_recovery_does_not_fail_a_live_continuation_dispatch(tmp_path: Path) -> None:
    """A decision replayed during startup must not be mistaken for a pre-crash claim."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    claimed = asyncio.Event()
    release = asyncio.Event()
    transport: ApprovalMatrixTransport

    async def resume(continuation: ApprovalContinuation) -> None:
        owned = transport._continuations.claim(continuation.approval_id, "live-startup-dispatch")
        assert owned is not None
        claimed.set()
        await release.wait()
        transport._continuations.complete(continuation.approval_id, "live-startup-dispatch")

    bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(side_effect=resume),
        settle_approval_continuation_failure=AsyncMock(),
    )
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None
    assert ready.state == "ready"
    transport._schedule_continuation(ready)
    await asyncio.wait_for(claimed.wait(), timeout=1)

    assert await transport._recover_continuations() is True
    during_resume = transport._continuations.get("approval-1")
    assert during_resume is not None
    assert during_resume.state == "claimed"
    bot.settle_approval_continuation_failure.assert_not_awaited()

    release.set()
    await asyncio.gather(*tuple(transport._continuation_tasks))
    completed = transport._continuations.get("approval-1")
    assert completed is not None
    assert completed.state == "completed"


@pytest.mark.asyncio
async def test_startup_retry_ignores_rows_owned_by_the_current_runtime(tmp_path: Path) -> None:
    """A repeated startup sweep must not terminalize live publication or inline execution."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    bot = SimpleNamespace(running=True, settle_approval_continuation_failure=AsyncMock())
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(
        replace(
            _continuation(),
            approval_id="live-publishing",
            response_event_id=None,
            state="publishing",
            runtime_generation=transport.runtime_generation,
        ),
    )
    transport._continuations.create(
        replace(
            _continuation(),
            approval_id="live-dispatch-claim",
            state="claimed",
            claimant_id="research:live",
            runtime_generation=transport.runtime_generation,
        ),
    )

    assert await transport._recover_continuations() is True

    publishing = transport._continuations.get("live-publishing")
    claimed = transport._continuations.get("live-dispatch-claim")
    assert publishing is not None
    assert publishing.state == "publishing"
    assert claimed is not None
    assert claimed.state == "claimed"
    bot.settle_approval_continuation_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_entity_reconciliation_settles_same_runtime_pending_approval(
    tmp_path: Path,
) -> None:
    """Removing an entity must not leave its live approval card actionable until timeout."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    transport: ApprovalMatrixTransport

    async def settle_failure(continuation: ApprovalContinuation, reason: str) -> None:
        _finish_test_failure(transport._continuations, continuation.approval_id, reason)

    router = SimpleNamespace(
        running=True,
        settle_approval_continuation_failure=AsyncMock(side_effect=settle_failure),
    )
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda name: cast("Any", router if name == "router" else None),
        cards_provider=lambda: None,
        entity_configured=lambda name: name != "research",
    )
    transport._continuations.create(
        replace(_continuation(), runtime_generation=transport.runtime_generation),
    )

    await transport.reconcile_unavailable_entities({"research"})
    await asyncio.gather(*tuple(transport._continuation_tasks))

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    assert failed.failure_reason == "Requesting agent 'research' is no longer available."
    router.settle_approval_continuation_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_owner_that_cannot_claim_is_polled_with_backoff(tmp_path: Path) -> None:
    """A stopping bot that returns without claiming must not create a tight dispatcher loop."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    resumed = asyncio.Event()

    async def signal_resume(_continuation: ApprovalContinuation) -> None:
        resumed.set()

    resume = AsyncMock(side_effect=signal_resume)
    bot = SimpleNamespace(running=True, resume_approval_continuation=resume)
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
        entity_configured=lambda _name: True,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None
    transport._schedule_continuation(ready)

    await asyncio.wait_for(resumed.wait(), timeout=1)
    await asyncio.sleep(0.1)
    await transport.cancel_startup_cleanup_retry()

    assert 1 <= resume.await_count <= 2


@pytest.mark.asyncio
async def test_transient_resume_exception_retries_ready_continuation(tmp_path: Path) -> None:
    """A transient pre-claim failure must not retire the only READY dispatcher."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    transport: ApprovalMatrixTransport
    attempts = 0

    async def resume(continuation: ApprovalContinuation) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            msg = "temporary continuation-store failure"
            raise RuntimeError(msg)
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None
        completed = transport._continuations.complete(continuation.approval_id, "worker")
        assert completed is not None

    bot = SimpleNamespace(running=True, resume_approval_continuation=resume)
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None

    await asyncio.wait_for(transport._dispatch_continuation("approval-1"), timeout=2)

    completed = transport._continuations.get("approval-1")
    assert completed is not None
    assert completed.state == "completed"
    assert attempts == 2


@pytest.mark.asyncio
async def test_transient_dispatch_store_read_retries_ready_continuation(tmp_path: Path) -> None:
    """A detached dispatcher must retain its retry owner across a transient read failure."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    transport: ApprovalMatrixTransport

    async def resume(continuation: ApprovalContinuation) -> None:
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None
        completed = transport._continuations.complete(continuation.approval_id, "worker")
        assert completed is not None

    bot = SimpleNamespace(running=True, resume_approval_continuation=AsyncMock(side_effect=resume))
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None
    original_get = transport._continuations.get
    reads = 0

    def flaky_get(approval_id: str) -> ApprovalContinuation | None:
        nonlocal reads
        reads += 1
        if reads == 1:
            msg = "temporary continuation read failure"
            raise RuntimeError(msg)
        return original_get(approval_id)

    with patch.object(transport._continuations, "get", side_effect=flaky_get):
        await asyncio.wait_for(transport._dispatch_continuation("approval-1"), timeout=2)

    completed = transport._continuations.get("approval-1")
    assert completed is not None
    assert completed.state == "completed"
    bot.resume_approval_continuation.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_exception_after_claim_fails_closed(tmp_path: Path) -> None:
    """An exception after claim must settle failure instead of retrying possible execution."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    transport: ApprovalMatrixTransport

    async def resume(continuation: ApprovalContinuation) -> None:
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None
        msg = "failed after claim"
        raise RuntimeError(msg)

    async def fail(continuation: ApprovalContinuation, reason: str) -> None:
        assert continuation.state == "claimed"
        assert "failed after claim" in reason
        _finish_test_failure(transport._continuations, continuation.approval_id, reason)

    bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(side_effect=resume),
        settle_approval_continuation_failure=AsyncMock(side_effect=fail),
    )
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None

    await transport._dispatch_continuation("approval-1")

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    bot.resume_approval_continuation.assert_awaited_once()
    bot.settle_approval_continuation_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_settlement_exception_retries_claimed_continuation(tmp_path: Path) -> None:
    """A transient terminal-settlement failure must not strand a claimed continuation."""
    transport: ApprovalMatrixTransport

    async def resume(continuation: ApprovalContinuation) -> None:
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None
        msg = "failed after claim"
        raise RuntimeError(msg)

    settlement_attempts = 0

    async def fail(continuation: ApprovalContinuation, reason: str) -> None:
        nonlocal settlement_attempts
        settlement_attempts += 1
        if settlement_attempts == 1:
            msg = "temporary failure delivery outage"
            raise RuntimeError(msg)
        _finish_test_failure(transport._continuations, continuation.approval_id, reason)

    bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(side_effect=resume),
        settle_approval_continuation_failure=AsyncMock(side_effect=fail),
    )
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None

    await asyncio.wait_for(transport._dispatch_continuation("approval-1"), timeout=2)

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    assert settlement_attempts == 2


@pytest.mark.asyncio
async def test_normal_runner_return_with_claim_is_settled_by_transport(tmp_path: Path) -> None:
    """The dispatcher, not the runner, terminalizes an otherwise unfinished claim."""
    transport: ApprovalMatrixTransport

    async def resume(continuation: ApprovalContinuation) -> None:
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None

    async def fail(continuation: ApprovalContinuation, reason: str) -> None:
        assert continuation.state == "claimed"
        _finish_test_failure(transport._continuations, continuation.approval_id, reason)

    bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(side_effect=resume),
        settle_approval_continuation_failure=AsyncMock(side_effect=fail),
    )
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None

    await asyncio.wait_for(transport._dispatch_continuation("approval-1"), timeout=2)

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    bot.settle_approval_continuation_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_completed_runner_outcome_is_committed_only_by_transport(tmp_path: Path) -> None:
    """A successful runner outcome becomes durable only after dispatcher reconciliation."""
    transport: ApprovalMatrixTransport

    async def resume(continuation: ApprovalContinuation) -> FinalDeliveryOutcome:
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None
        return FinalDeliveryOutcome(terminal_status="completed", event_id="$answer")

    bot = SimpleNamespace(running=True, resume_approval_continuation=AsyncMock(side_effect=resume))
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None

    await transport._dispatch_continuation("approval-1")

    completed = transport._continuations.get("approval-1")
    assert completed is not None
    assert completed.state == "completed"


@pytest.mark.asyncio
async def test_transient_completion_write_keeps_dispatcher_until_outbox_reconciliation(tmp_path: Path) -> None:
    """A transient terminal mutation failure must not drop the only dispatcher task."""
    transport: ApprovalMatrixTransport

    async def resume(continuation: ApprovalContinuation) -> FinalDeliveryOutcome:
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None
        return FinalDeliveryOutcome(terminal_status="completed", event_id="$answer")

    async def reconcile_success(continuation: ApprovalContinuation, _reason: str) -> None:
        assert continuation.claimant_id is not None
        completed = original_complete(continuation.approval_id, continuation.claimant_id)
        assert completed is not None

    bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(side_effect=resume),
        settle_approval_continuation_failure=AsyncMock(side_effect=reconcile_success),
    )
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    assert transport._continuations.acknowledge_call("approval-1", "call-1") is not None
    original_complete = transport._continuations.complete
    completion_attempts = 0

    def flaky_complete(approval_id: str, claimant_id: str) -> ApprovalContinuation | None:
        nonlocal completion_attempts
        completion_attempts += 1
        if completion_attempts == 1:
            msg = "temporary completion write failure"
            raise RuntimeError(msg)
        return original_complete(approval_id, claimant_id)

    with patch.object(transport._continuations, "complete", side_effect=flaky_complete):
        await asyncio.wait_for(transport._dispatch_continuation("approval-1"), timeout=2)

    completed = transport._continuations.get("approval-1")
    assert completed is not None
    assert completed.state == "completed"
    bot.settle_approval_continuation_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_failure_settlement_re_resolves_replacement_bot_after_reload(tmp_path: Path) -> None:
    """A config reload must not pin settlement to the stopped runner."""
    transport: ApprovalMatrixTransport
    bots: dict[str, Any] = {}

    async def resume(continuation: ApprovalContinuation) -> None:
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None
        requested = transport._continuations.request_failure(
            continuation.approval_id,
            "temporary Matrix failure",
            claimant_id="worker",
            runtime_generation=transport.runtime_generation,
        )
        assert requested is not None
        old_bot.running = False
        bots["research"] = replacement_bot

    async def settle(continuation: ApprovalContinuation, reason: str) -> None:
        assert continuation.claimant_id is not None
        fenced = transport._continuations.fence_claimed_failure(
            continuation.approval_id,
            continuation.claimant_id,
        )
        assert fenced is not None
        assert transport._continuations.finish_failure(continuation.approval_id, reason) is not None

    old_bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(side_effect=resume),
        settle_approval_continuation_failure=AsyncMock(),
    )
    replacement_bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(),
        settle_approval_continuation_failure=AsyncMock(side_effect=settle),
    )
    bots["research"] = old_bot
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda name: cast("Any", bots.get(name)),
        cards_provider=lambda: None,
        entity_configured=lambda name: name == "research",
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    assert transport._continuations.acknowledge_call("approval-1", "call-1") is not None

    await asyncio.wait_for(transport._dispatch_continuation("approval-1"), timeout=2)

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    old_bot.settle_approval_continuation_failure.assert_not_awaited()
    replacement_bot.settle_approval_continuation_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_entity_attempt_keeps_transport_dispatch_alive(tmp_path: Path) -> None:
    """Per-bot shutdown may cancel its child attempt without cancelling durable dispatch."""
    transport: ApprovalMatrixTransport
    bots: dict[str, Any] = {}
    resume_started = asyncio.Event()
    resume_tasks: list[asyncio.Task[FinalDeliveryOutcome | None]] = []

    async def resume(continuation: ApprovalContinuation) -> None:
        task = asyncio.current_task()
        assert task is not None
        resume_tasks.append(cast("asyncio.Task[FinalDeliveryOutcome | None]", task))
        claimed = transport._continuations.claim(continuation.approval_id, "worker")
        assert claimed is not None
        requested = transport._continuations.request_failure(
            continuation.approval_id,
            "temporary Matrix failure",
            claimant_id="worker",
            runtime_generation=transport.runtime_generation,
        )
        assert requested is not None
        resume_started.set()
        try:
            await asyncio.Future()
        finally:
            old_bot.running = False
            bots["research"] = replacement_bot

    async def settle(continuation: ApprovalContinuation, reason: str) -> None:
        assert continuation.claimant_id is not None
        assert (
            transport._continuations.fence_claimed_failure(
                continuation.approval_id,
                continuation.claimant_id,
            )
            is not None
        )
        assert transport._continuations.finish_failure(continuation.approval_id, reason) is not None

    old_bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(side_effect=resume),
        settle_approval_continuation_failure=AsyncMock(),
    )
    replacement_bot = SimpleNamespace(
        running=True,
        resume_approval_continuation=AsyncMock(),
        settle_approval_continuation_failure=AsyncMock(side_effect=settle),
    )
    bots["research"] = old_bot
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda name: cast("Any", bots.get(name)),
        cards_provider=lambda: None,
        entity_configured=lambda name: name == "research",
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None
    transport.schedule_continuation(ready)

    await asyncio.wait_for(resume_started.wait(), timeout=2)
    resume_tasks[0].cancel()
    await asyncio.wait_for(asyncio.gather(*tuple(transport._continuation_tasks)), timeout=2)

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    old_bot.settle_approval_continuation_failure.assert_not_awaited()
    replacement_bot.settle_approval_continuation_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_removed_owner_waits_for_router_before_retiring_ready_continuation(tmp_path: Path) -> None:
    """A temporarily absent router must not lose visible terminal settlement for a removed owner."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    bots: dict[str, object] = {}
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda name: cast("Any", bots.get(name)),
        cards_provider=lambda: None,
        entity_configured=lambda _name: False,
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None
    assert ready.state == "ready"

    dispatch = asyncio.create_task(transport._dispatch_continuation("approval-1"))
    await asyncio.sleep(0.3)
    still_ready = transport._continuations.get("approval-1")
    assert still_ready is not None
    assert still_ready.state == "ready"

    async def fail(owned: ApprovalContinuation, reason: str) -> None:
        _finish_test_failure(transport._continuations, owned.approval_id, reason)

    fail_mock = AsyncMock(side_effect=fail)
    bots["router"] = SimpleNamespace(running=True, settle_approval_continuation_failure=fail_mock)
    await asyncio.wait_for(dispatch, timeout=2)

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    fail_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_permanently_failed_configured_owner_is_terminalized_by_router(tmp_path: Path) -> None:
    """A configured bot with a permanent startup error must not be retried forever."""
    transport: ApprovalMatrixTransport

    async def fail(owned: ApprovalContinuation, reason: str) -> None:
        assert "could not start" in reason
        _finish_test_failure(transport._continuations, owned.approval_id, reason)

    router = SimpleNamespace(running=True, settle_approval_continuation_failure=AsyncMock(side_effect=fail))
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda name: cast("Any", router if name == "router" else None),
        cards_provider=lambda: None,
        entity_configured=lambda name: name == "research",
        entity_permanently_unavailable=lambda name: name == "research",
    )
    transport._continuations.create(_continuation())
    transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    ready = transport._continuations.acknowledge_call("approval-1", "call-1")
    assert ready is not None
    assert ready.state == "ready"

    await transport._dispatch_continuation("approval-1")

    settled = transport._continuations.get("approval-1")
    assert settled is not None
    assert settled.state == "failed"
    router.settle_approval_continuation_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_fails_closed_when_attached_card_was_membership_fenced(tmp_path: Path) -> None:
    """A stale attached card id must not leave its continuation pending forever."""
    journal = EventJournalStore.open_sqlite(tmp_path / "event-journal.db")
    cards = journal.principal("router@@mindroom_router:example.org")
    card = {
        "event_id": "$departed-card",
        "sender": "@mindroom_router:example.org",
        "type": "io.mindroom.tool_approval",
        "content": {"approval_id": "approval-1-0-0", "status": "pending"},
    }
    await cards.claim_approval_card(
        room_id="!room:example.org",
        transaction_id="approval-1-0-0",
        card=card,
    )
    await cards.mark_approval_card_attempted(
        transaction_id="approval-1-0-0",
        sending_device_id="DEVICE",
    )
    await cards.acknowledge_approval_card(
        transaction_id="approval-1-0-0",
        card_event_id="$departed-card",
        card=card,
    )
    transport: ApprovalMatrixTransport

    async def fail(continuation: ApprovalContinuation, reason: str) -> None:
        assert "no longer exists" in reason
        _finish_test_failure(transport._continuations, continuation.approval_id, reason)

    bot = SimpleNamespace(running=True, settle_approval_continuation_failure=AsyncMock(side_effect=fail))
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: cards,
    )
    continuation = _continuation()
    transport._continuations.create(
        replace(
            continuation,
            calls=(replace(continuation.calls[0], card_event_id="$departed-card"), continuation.calls[1]),
        ),
    )
    await cards.fence_departure("!room:example.org", source=DepartureSource.LOCAL)
    await cards.note_membership_restarted("!room:example.org")
    assert (
        await cards.pending_approval_card(
            room_id="!room:example.org",
            card_event_id="$departed-card",
        )
        is None
    )

    assert await transport._recover_continuations() is True
    await asyncio.gather(*tuple(transport._continuation_tasks))

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    await journal.close()


@pytest.mark.asyncio
async def test_transport_close_releases_continuation_store(tmp_path: Path) -> None:
    """The transport owns its SQLite handle and must release it at shutdown."""
    transport = ApprovalMatrixTransport(
        runtime_paths=RuntimePaths(
            config_path=tmp_path / "config.yaml",
            config_dir=tmp_path,
            env_path=tmp_path / ".env",
            storage_root=tmp_path,
        ),
        bot_provider=lambda _name: None,
        cards_provider=lambda: None,
    )
    close = MagicMock()
    transport._continuations.close = close

    await transport.close()

    close.assert_called_once_with()
