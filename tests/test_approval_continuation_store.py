"""Focused durable-state tests for suspended tool approval continuations."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock

import pytest

from mindroom.approval_continuation import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalContinuationStore,
    ApprovalDecision,
)
from mindroom.approval_transport import ApprovalMatrixTransport
from mindroom.constants import RuntimePaths

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
            ),
        ),
    )


def test_continuation_store_commits_first_call_decision_and_one_claim(tmp_path: Path) -> None:
    """Removing either guard would let duplicate Matrix actions execute a tool twice."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())

    resolved = store.resolve_call("approval-1", "call-1", ApprovalDecision.APPROVED)
    duplicate = store.resolve_call("approval-1", "call-1", ApprovalDecision.DENIED)

    assert resolved is not None
    assert resolved.state == "ready"
    assert duplicate is not None
    assert duplicate.calls[0].decision is ApprovalDecision.APPROVED
    assert store.claim("approval-1", "worker-1") is not None
    assert store.claim("approval-1", "worker-2") is None


def test_continuation_store_recovers_pending_and_claimed_without_copying_arguments(tmp_path: Path) -> None:
    """Reload recovery needs routing metadata, while exact arguments remain owned by the Agno run."""
    store = ApprovalContinuationStore(tmp_path)
    store.create(_continuation())
    store.resolve_call("approval-1", "call-1", ApprovalDecision.DENIED)
    store.claim("approval-1", "worker-1")

    recovered = ApprovalContinuationStore(tmp_path).recoverable()

    assert len(recovered) == 1
    assert recovered[0].state == "claimed"
    assert recovered[0].calls[0].tool_call_id == "call-1"
    assert "arguments" not in recovered[0].to_context()


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
