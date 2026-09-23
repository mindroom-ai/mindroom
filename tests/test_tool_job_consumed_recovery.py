"""An SDK read does not discharge its response's durable delivery obligation."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import nio
import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse

from mindroom.agent_storage import create_session_storage
from mindroom.custom_tools.job import JobTools
from mindroom.event_journal import DeliveryStage
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.completion import completion_event
from mindroom.tool_jobs.consumption import set_consumption_storage
from mindroom.tool_jobs.execution_scope import owned_tool_execution
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import unwrap_extracted_collaborator
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context
from tests.response_runner_helpers import _bot, _target

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb

    from mindroom.bot import AgentBot


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

    @owned_tool_execution
    async def run() -> None:
        set_consumption_storage(storage_factory)
        result = await agent.arun("Read the result.", session_id=owner.session_id, user_id=owner.requester_id)
        assert result.tools[0].result == "saved output"

    try:
        with tool_runtime_context(context):
            await run()
    finally:
        storage.close()


async def _save_visible_response(bot: AgentBot, owner: ToolExecutionIdentity, source: str) -> None:
    """Seed an acknowledged placeholder whose current Matrix edit already contains prose."""
    store = bot.journal_principal()
    await store.enqueue_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.INITIAL,
        room_id=owner.room_id,
        thread_id=owner.resolved_thread_id,
        payload={"msgtype": "m.notice", "body": "Thinking..."},
    )
    assert await store.claim_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.INITIAL,
        sending_device_id=bot.client.device_id,
    )
    await store.acknowledge_matrix_delivery(
        delivery_id=source,
        stage=DeliveryStage.INITIAL,
        event_id="$placeholder",
        delivered_projections=(),
    )

    def get_event(room_id: str, event_id: str) -> nio.RoomGetEventResponse:
        content = (
            {"msgtype": "m.text", "body": "Already visible."}
            if event_id == "$placeholder"
            else bot.client.room_send.call_args.kwargs["content"]
        )
        return nio.RoomGetEventResponse.from_dict(
            {
                "event_id": event_id,
                "type": "m.room.message",
                "room_id": room_id,
                "sender": bot.matrix_id.full_id,
                "origin_server_ts": 1,
                "content": content,
            },
        )

    bot.client.room_get_event.side_effect = get_event


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
@pytest.mark.parametrize("visible", [False, True])
@pytest.mark.parametrize("read_state", ["own", "reread", "other", "revoked", "settled", "stopped"])
async def test_consumed_completion_recovers_only_its_unfinished_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    visible: bool,
    read_state: str,
) -> None:
    """Restart finishes only the exact authorized, unfinished response without replaying work."""
    bot, owner = _completion_bot(tmp_path)
    runner = unwrap_extracted_collaborator(bot._response_runner)
    runtime = ToolJobRuntime(bot.runtime_paths.storage_root)
    register_background_runtime(bot.runtime_paths, runtime)
    executions = []

    async def operation() -> BackgroundOutcome:
        executions.append("executed")
        return BackgroundOutcome("completed", "saved output")

    async def stopped_job(_job: object) -> bool:
        return True

    try:
        await runtime.start(JobSpec("recover-me", "tool", 0), owner=owner, operation=operation)
        waited = await runtime.wait("recover-me", owner=owner, depth=0)
        await runtime.release_wait("recover-me", waited.token)
        event = completion_event(waited.job, sender_id=bot.matrix_id.full_id)
        store = runner.deps.approval_store
        await store.admit(event)
        if visible:
            await _save_visible_response(bot, owner, event.event_id)
        await _read_saved_job(bot, owner, "$human-followup" if read_state == "other" else event.event_id)
        if read_state == "reread":
            await _read_saved_job(bot, owner, "$human-followup")
        if read_state == "settled":
            await store.settle(event.event_id)
        if read_state == "stopped":
            await runtime.stop_jobs(receipt_order=2, matches=stopped_job)
        assert not await runtime.pending_outcomes()
        await runtime.shutdown()
        runtime = ToolJobRuntime(bot.runtime_paths.storage_root, authorize=lambda _job: read_state != "revoked")
        await runtime.recover()
        register_background_runtime(bot.runtime_paths, runtime)

        monkeypatch.setattr(
            "mindroom.ai.create_agent",
            lambda *_args, **kwargs: _reader(bot, owner, kwargs["history_storage"], "Recovered answer."),
        )
        admitted = await store.load_event(event.event_id)
        assert admitted is not None
        await runner._resume_tool_job_completion(admitted, "recover-me", 0)
        final = await store.load_matrix_delivery(delivery_id=event.event_id, stage=DeliveryStage.FINAL)
        assert (final is not None) is (read_state in {"own", "reread"})
        if final is not None:
            assert final.acknowledged_event_id is not None
            assert "Recovered answer." in final.payload["body"]
            if visible:
                assert final.edits_event_id == "$placeholder"
                assert "Already visible." in final.payload["body"]
        assert not await store.is_pending(event.event_id)
        assert executions == ["executed"]
    finally:
        register_background_runtime(bot.runtime_paths, None)
        await runtime.shutdown()
