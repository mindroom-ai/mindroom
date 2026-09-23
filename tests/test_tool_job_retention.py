"""Completed-result retention follows durable response and approval ownership."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.event_journal import (
    ApprovalCall,
    ApprovalContinuation,
    ApprovalDecision,
    EventClass,
    EventKind,
    InboundEvent,
)
from mindroom.handled_turns import TurnRecordCodec
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_sources import ResponseSources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobAccessError
from mindroom.turn_record import TurnRecord
from tests.response_runner_helpers import _bot
from tests.test_subagent_runtime import _job
from tests.tool_job_helpers import start_job, tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize(("source_completed", "approval"), [(False, False), (True, True), (True, False)])
@pytest.mark.parametrize("source_pruned", [False, True])
async def test_retention_preserves_pending_turns_and_conversation_approvals(
    tmp_path: Path,
    source_completed: bool,
    approval: bool,
    source_pruned: bool,
) -> None:
    """An old consumed job is deleted only once its turn finished and its conversation has no pending approval."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    paths = bot.runtime_paths
    runtime = tool_job_runtime(paths.storage_root)
    owner = replace(_job().owner, agent_name="general", transport_agent_name=None)
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: bot.config, lambda _: bot, AgentReplyMembershipIndex())
    # An authorize-all runtime stands in for the one initialize would create for this bot's stricter grants.
    coordinator._runtime, coordinator._journal = runtime, bot._journal_store

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved result", result_payload={"value": "saved result"})

    await start_job(
        runtime,
        "old",
        tool_name="tool",
        depth=0,
        source_event_id="$original",
        adapter={},
        owner=owner,
        operation=operation,
    )
    waited = await runtime.wait("old", owner=owner, depth=0)
    await runtime.acknowledge_wait("old", waited.claim)
    entry = runtime._entries["old"]
    entry.job = replace(entry.job, updated_at=(datetime.now(UTC) - timedelta(days=31)).isoformat())
    record = TurnRecord.create(("$original",), anchor_event_id="$original", completed=source_completed)
    principal = bot.journal_principal()
    await principal.admit(
        InboundEvent(
            "$original",
            "!room:localhost",
            "$thread",
            EventKind.MESSAGE,
            EventClass.ACTIONABLE,
            "@human:localhost",
            1,
            {"content": {"body": "original request"}},
        ),
        None,
    )
    if source_completed:
        await principal.settle("$original")
    await bot._journal_store.turn_records("general").upsert(
        index_event_ids=record.indexed_event_ids,
        anchor_event_id="$original",
        record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
    )
    if source_pruned:
        await bot._journal_store.turn_records("general").forget(index_event_ids=record.indexed_event_ids)
    if approval:
        principal = bot.journal_principal()
        await principal.admit(
            InboundEvent(
                "$later",
                "!room:localhost",
                "$thread",
                EventKind.MESSAGE,
                EventClass.ACTIONABLE,
                "@human:localhost",
                1,
                {"content": {"body": "continue"}},
            ),
            None,
        )
        continuation = ApprovalContinuation(
            approval_id="pending",
            run_id="paused-run",
            session_id=owner.session_id,
            entity_kind="agent",
            entity_name="general",
            room_id=owner.room_id,
            thread_id=owner.thread_id,
            requester_id=owner.requester_id,
            response_event_id="$response",
            sources=ResponseSources(("$later",), ("$later",)),
            calls=(ApprovalCall("call", "tool", "general", 2**62, decision=ApprovalDecision.APPROVED),),
            state="ready",
        )
        assert await principal.create_approval_continuation(continuation) is not None
    try:
        await coordinator._expire_consumed_results()
        expired = source_completed and not approval
        directory = paths.storage_root / "tool_jobs"
        assert ("old" in runtime._entries) is not expired
        assert {path.name for path in directory.glob("old.*")} == (
            set() if expired else {"old.json", "old.g0.result.json"}
        )
        if expired:
            with pytest.raises(JobAccessError, match="not available"):
                await runtime.lookup("old", owner=owner, depth=0)
        else:
            saved = await runtime.lookup("old", owner=owner, depth=0)
            assert await runtime.read_payload(saved) == {"value": "saved result"}
    finally:
        await coordinator.stop()
