"""A reply that read a job outcome stays parked with that outcome while background jobs are disabled."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import nio
import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_storage import create_session_storage
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.execution import drive_delegations
from mindroom.event_journal import EventClass, EventKind, InboundEvent
from mindroom.handled_turns import TurnRecordCodec
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.disabled import event_is_parked
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import BackgroundOutcome, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.turn_record import TurnRecord
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context
from tests.response_runner_helpers import _bot, _target
from tests.tool_job_helpers import start_job, tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb

    from mindroom.bot import AgentBot
    from mindroom.delegation.state import DelegationChild


def _reader(bot: AgentBot, owner: ToolExecutionIdentity, storage: BaseDb, answer: str) -> Agent:
    """Build a real SDK agent with a deterministic provider result."""
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("job", "read", action="wait", job_id="recover-me")]),
            ModelResponse(content=answer),
        ],
    )
    install_tool_job_execution(model)
    return Agent(id="general", model=model, tools=[JobTools(bot.runtime_paths, owner)], db=storage, telemetry=False)


async def _read_saved_job(bot: AgentBot, owner: ToolExecutionIdentity, source: str) -> None:
    """Read through real SDK execution and verify its saved consumption receipt."""

    def storage_factory() -> BaseDb:
        return create_session_storage("general", bot.config, bot.runtime_paths, owner)

    storage = storage_factory()
    agent = _reader(bot, owner, storage, "The saved result is ready.")
    context = replace(
        _delegate_runtime_context(bot.config, bot.runtime_paths, execution_identity=owner),
        agent_name="general",
        membership_turn_id=source,
    )

    async def unexpected_child(_child: DelegationChild, **_kwargs: object) -> str:
        msg = "Reading a saved result must not replay its child."
        raise AssertionError(msg)

    @owned_tool_execution
    async def run() -> None:
        set_consumption_storage(storage_factory)
        result = await agent.arun("Read the result.", session_id=owner.session_id, user_id=owner.requester_id)
        result = await drive_delegations(
            agent,
            result,
            agent_name="general",
            run_child=unexpected_child,
            config=bot.config,
            runtime_paths=bot.runtime_paths,
            execution_identity=owner,
        )
        assert result.tools[0].result == "saved output"

    try:
        with tool_runtime_context(context):
            await run()
    finally:
        storage.close()


def _completion_bot(tmp_path: Path) -> tuple[AgentBot, ToolExecutionIdentity]:
    """Prepare an ordinary non-streaming Matrix agent with real journal storage."""
    bot = _bot(tmp_path)
    bot.config.background_tool_jobs.enabled = True
    bot.config.memory.backend = "none"
    bot.config.agents["general"].show_tool_calls = False
    bot.enable_streaming = False
    target = _target(thread_id="$thread")
    bot.client.room_send.return_value = nio.RoomSendResponse(event_id="$sent", room_id=target.room_id)
    owner = ToolExecutionIdentity(
        "matrix",
        "general",
        "@user:localhost",
        target.room_id,
        "$thread",
        "$thread",
        target.session_id,
    )
    return bot, owner


@pytest.mark.asyncio
@pytest.mark.parametrize("grouped", [False, True])
async def test_disabled_startup_parks_consuming_followup_and_its_group(
    tmp_path: Path,
    grouped: bool,
) -> None:
    """Disabling jobs cannot replay a pending consumer through ordinary execution."""
    bot, owner = _completion_bot(tmp_path)
    runtime = tool_job_runtime(bot.runtime_paths.storage_root)
    pin_background_tool_jobs(bot.config, bot.runtime_paths)
    register_background_runtime(bot.runtime_paths, runtime)
    coordinator = ToolJobRuntimeCoordinator(
        bot.runtime_paths,
        lambda: bot.config,
        lambda _: bot,
        AgentReplyMembershipIndex(),
    )

    async def operation() -> BackgroundOutcome:
        return BackgroundOutcome("completed", "saved output")

    store = bot.journal_principal()
    try:
        await start_job(
            runtime,
            "recover-me",
            tool_name="tool",
            depth=0,
            source_event_id="$original",
            adapter={},
            owner=owner,
            operation=operation,
        )
        for source in ("$original", "$followup", "$grouped", "$unrelated"):
            await store.admit(
                InboundEvent(
                    source,
                    owner.room_id,
                    owner.resolved_thread_id,
                    EventKind.MESSAGE,
                    EventClass.ACTIONABLE,
                    owner.requester_id,
                    1,
                    {"content": {"body": "Read the earlier result."}},
                ),
            )
        if grouped:
            record = TurnRecord.create(("$followup", "$grouped"), anchor_event_id="$followup", completed=False)
            await bot._journal_store.turn_records("general").upsert(
                index_event_ids=record.indexed_event_ids,
                anchor_event_id="$followup",
                record_json=json.dumps(TurnRecordCodec._to_ledger_record(record)),
            )
        await _read_saved_job(bot, owner, "$followup")
        await runtime.shutdown()
        bot.config.background_tool_jobs.enabled = False
        await coordinator.initialize(bot._journal_store)
        for source, parked in (("$original", True), ("$followup", True), ("$grouped", grouped), ("$unrelated", False)):
            event = await store.load_event(source)
            assert event is not None
            assert event_is_parked(bot.config, bot.runtime_paths, "general", event) is parked
            assert await store.is_pending(source)
    finally:
        await runtime.shutdown()
        await coordinator.stop()
