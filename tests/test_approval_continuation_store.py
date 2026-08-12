"""Focused durable-state tests for suspended tool approval continuations."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.approval_continuation import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalContinuationStore,
    ApprovalDecision,
)
from mindroom.approval_transport import ApprovalMatrixTransport
from mindroom.constants import RuntimePaths
from mindroom.event_journal import StoredApprovalCard

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
                expires_at="2026-08-12T00:00:00+00:00",
            ),
            ApprovalCall(
                tool_call_id="call-2",
                tool_name="safe",
                invoking_agent="analyst",
                expires_at="2026-08-12T00:00:00+00:00",
                decision=ApprovalDecision.APPROVED,
                decision_recorded=True,
            ),
        ),
    )


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


def test_terminal_continuation_releases_source_replay_ownership(tmp_path: Path) -> None:
    """Settled rows must not suppress a later legitimate delivery that reuses the source lookup."""
    store = ApprovalContinuationStore(tmp_path)
    continuation = _continuation()
    store.create(continuation)
    store.fail(continuation.approval_id, "settled")

    assert store.for_source_event("$source") is None


def test_recovery_pages_through_every_continuation(tmp_path: Path) -> None:
    """Startup recovery must not silently strand rows beyond Agno's first result page."""
    store = ApprovalContinuationStore(tmp_path)
    template = _continuation()
    rows = [
        {
            "id": f"approval-{index}",
            "run_id": template.run_id,
            "session_id": template.session_id,
            "status": "pending",
            "context": template._to_context(),
        }
        for index in range(101)
    ]
    db = MagicMock()

    def get_approvals(*, status: str, page: int, **_kwargs: object) -> tuple[list[dict[str, object]], int]:
        if status != "pending":
            return [], 0
        start = (page - 1) * 100
        return rows[start : start + 100], len(rows)

    db.get_approvals.side_effect = get_approvals
    store._db = db

    recovered = store.recoverable()

    assert len(recovered) == 101
    assert db.get_approvals.call_count == 4


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

    recovered = await transport._attach_recovered_cards(_continuation())

    assert recovered.calls[0].card_event_id == "$approval"
    assert transport._continuations.get("approval-1") == recovered


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
async def test_recovered_partial_card_set_remains_recoverable_until_card_is_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart during card settlement must not retire continuation recovery ownership."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path,
    )
    continuation = _continuation()
    continuation = replace(
        continuation,
        calls=(replace(continuation.calls[0], card_event_id="$approval"), continuation.calls[1]),
    )
    transport: ApprovalMatrixTransport

    async def fail(owned: ApprovalContinuation, reason: str) -> None:
        transport._continuations.fail(owned.approval_id, reason)

    bot = SimpleNamespace(running=True, fail_approval_continuation=AsyncMock(side_effect=fail))
    transport = ApprovalMatrixTransport(
        runtime_paths=runtime_paths,
        bot_provider=lambda _name: cast("Any", bot),
        cards_provider=lambda: None,
    )
    transport._continuations.create(continuation)

    monkeypatch.setattr(
        "mindroom.approval_transport.expire_suspended_tool_approval",
        AsyncMock(side_effect=RuntimeError("crash before terminal edit")),
    )
    settled = await transport._fail_recovered_continuation(continuation, "card set incomplete")

    recoverable = transport._continuations.get(continuation.approval_id)
    assert settled is False
    assert recoverable is not None
    assert recoverable.state == "pending"
    bot.fail_approval_continuation.assert_not_awaited()


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
        transport._continuations.fail(owned.approval_id, reason)

    fail_mock = AsyncMock(side_effect=fail)
    bots["router"] = SimpleNamespace(running=True, fail_approval_continuation=fail_mock)
    await asyncio.wait_for(dispatch, timeout=2)

    failed = transport._continuations.get("approval-1")
    assert failed is not None
    assert failed.state == "failed"
    fail_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_expiry_task_failure_is_retired_and_observed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Matrix expiry must not leave a stale key or an unobserved task exception."""
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
    continuation = replace(
        _continuation(),
        calls=(
            replace(
                _continuation().calls[0],
                card_event_id="$approval",
                expires_at="2020-01-01T00:00:00+00:00",
            ),
        ),
    )
    expire = AsyncMock(side_effect=[RuntimeError("Matrix unavailable"), True])
    monkeypatch.setattr("mindroom.approval_transport.expire_suspended_tool_approval", expire)

    transport._continuations.create(continuation)
    transport._schedule_expiry(continuation)
    tasks = tuple(transport._expiry_tasks.values())
    await asyncio.gather(*tasks, return_exceptions=True)

    assert expire.await_count == 2
    assert transport._expiry_tasks == {}


@pytest.mark.asyncio
async def test_expiry_task_retries_false_outcome_until_settled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A false settlement result must keep the expiry task alive for another attempt."""
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
    continuation = replace(
        _continuation(),
        calls=(
            replace(
                _continuation().calls[0],
                card_event_id="$approval",
                expires_at="2020-01-01T00:00:00+00:00",
            ),
        ),
    )
    expire = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("mindroom.approval_transport.expire_suspended_tool_approval", expire)

    transport._continuations.create(continuation)
    transport._schedule_expiry(continuation)
    tasks = tuple(transport._expiry_tasks.values())
    await asyncio.gather(*tasks)

    assert expire.await_count == 2
    assert transport._expiry_tasks == {}


@pytest.mark.asyncio
async def test_expiry_task_keeps_retrying_after_continuation_becomes_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Durable continuation readiness does not prove its Matrix card edit landed."""
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
    continuation = replace(
        _continuation(),
        calls=(
            replace(
                _continuation().calls[0],
                card_event_id="$approval",
                expires_at="2020-01-01T00:00:00+00:00",
            ),
        ),
    )
    transport._continuations.create(continuation)
    attempts = 0

    async def expire(_room_id: str, _card_event_id: str) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            transport._continuations.resolve_call("approval-1", "call-1", ApprovalDecision.EXPIRED)
            transport._continuations.acknowledge_call("approval-1", "call-1")
            return False
        return True

    monkeypatch.setattr("mindroom.approval_transport.expire_suspended_tool_approval", AsyncMock(side_effect=expire))

    transport._schedule_expiry(continuation)
    tasks = tuple(transport._expiry_tasks.values())
    await asyncio.gather(*tasks)

    assert attempts == 2
    assert transport._expiry_tasks == {}


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
