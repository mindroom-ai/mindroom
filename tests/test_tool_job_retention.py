"""Completed-result retention follows durable response and approval ownership."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, read_job_snapshot
from mindroom.turn_record import TurnRecord
from tests.response_runner_helpers import _bot
from tests.test_subagent_runtime import _job


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True])
@pytest.mark.parametrize("approval", [False, True])
async def test_released_consumed_result_expires_without_source_history(
    tmp_path: Path,
    consumed: bool,
    approval: bool,
) -> None:
    """Released source-less results may expire only after consumption and with no live approval owner."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    paths = bot.runtime_paths
    directory = paths.storage_root / "tool_jobs"
    directory.mkdir()
    path = directory / "completed.json"
    fixture = Path(__file__).parent / "fixtures/tool_jobs/v2026.9.165/completed.json"
    payload = json.loads(fixture.read_text())
    payload["owner"]["agent_name"] = "general"
    payload["wait_acknowledged"] = consumed
    payload["updated_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
    path.write_text(json.dumps(payload))
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: bot.config, lambda _: bot, AgentReplyMembershipIndex())
    await coordinator.initialize(bot._journal_store)
    try:
        await coordinator.runtime.recover()
        if approval:
            principal = bot.journal_principal()
            await principal.admit(
                InboundEvent(
                    "$approval-source",
                    "!room:test",
                    "$root",
                    EventKind.MESSAGE,
                    EventClass.ACTIONABLE,
                    "@alice:test",
                    1,
                    {"content": {"body": "continue"}},
                ),
                None,
            )
            continuation = ApprovalContinuation(
                approval_id="pending",
                run_id="paused-run",
                session_id="parent-session",
                entity_kind="agent",
                entity_name="general",
                room_id="!room:test",
                thread_id="$root",
                requester_id="@alice:test",
                response_event_id="$response",
                sources=ResponseSources(("$approval-source",), ("$approval-source",)),
                calls=(ApprovalCall("call", "tool", "general", 2**62, decision=ApprovalDecision.APPROVED),),
                state="ready",
            )
            assert await principal.create_approval_continuation(continuation) is not None
        await coordinator._expire_consumed_results()
        saved = read_job_snapshot(path)
        assert saved.result_expired is (consumed and not approval)
        if not saved.result_expired:
            assert saved.result_payload == {"value": [1]}
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("source_completed", "approval"), [(False, False), (True, True), (True, False)])
@pytest.mark.parametrize("source_pruned", [False, True])
async def test_retention_preserves_pending_turns_and_conversation_approvals(
    tmp_path: Path,
    source_completed: bool,
    approval: bool,
    source_pruned: bool,
) -> None:
    """An old acknowledged value remains available to an unfinished or paused SDK run."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    paths = bot.runtime_paths
    runtime = ToolJobRuntime(paths.storage_root)
    owner = replace(_job().owner, agent_name="general", transport_agent_name=None)
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: bot.config, lambda _: bot, AgentReplyMembershipIndex())
    coordinator._runtime = runtime
    await coordinator.initialize(bot._journal_store)

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved result", result_payload={"value": "saved result"})

    await runtime.start(
        JobSpec("old", "tool", 0, adapter={"source_event_id": "$original"}),
        owner=owner,
        operation=operation,
    )
    waited = await runtime.wait("old", owner=owner, depth=0)
    await runtime.acknowledge_wait("old", waited.token)
    runtime._entries["old"].job.updated_at = (datetime.now(UTC) - timedelta(days=31)).isoformat()
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
        saved = await runtime.lookup("old", owner=owner, depth=0)
        assert saved.result_expired is (source_completed and not approval)
        if not saved.result_expired:
            assert saved.result_payload == {"value": "saved result"}
    finally:
        await coordinator.stop()
