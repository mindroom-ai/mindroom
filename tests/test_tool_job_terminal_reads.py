"""Terminal job reads release wait ownership and preserve compact receipts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agent_storage import create_session_storage
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.background import start_delegation
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.message_target import MessageTarget
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.delegation.state import DelegationChild
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


@pytest.fixture
def delegation_context(tmp_path: Path) -> ToolRuntimeContext:
    """Provide a real requester with access to both configured delegation agents."""
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["code"]),
            "code": AgentConfig(display_name="Code"),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    context = _delegate_runtime_context(config, paths)
    for name in config.agents:
        with_responder_access(config, name, users=[context.requester_id])
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_completed_wait_releases_signal_and_idle_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: Literal["completed", "failed", "cancelled"],
) -> None:
    """An active wait retains its conversation; every terminal outcome releases it."""
    runtime = ToolJobRuntime(tmp_path)
    owner = ToolExecutionIdentity(
        channel="matrix",
        agent_name="parent",
        requester_id="@alice:test",
        room_id="!room:test",
        thread_id=None,
        resolved_thread_id="$root",
        session_id="parent-session",
    )
    coordinator = ResponseLifecycleCoordinator()
    target = MessageTarget.resolve("!room:test", "$root", "$source")
    signal = coordinator._get_or_create_queued_signal(target).human_signal
    lock = coordinator._response_lifecycle_lock(target)
    subscribed, release = asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        await release.wait()
        return BackgroundOutcome(terminal, "saved outcome")

    waiter = None
    try:
        await runtime.start(JobSpec("work", "tool", 0), owner=owner, operation=operation, human_signal=signal)
        subscribe = signal.subscribe

        def record_subscription(callback: Callable[[], None]) -> None:
            subscribe(callback)
            subscribed.set()

        monkeypatch.setattr(signal, "subscribe", record_subscription)
        waiter = asyncio.create_task(runtime.wait("work", owner=owner, depth=0))
        await asyncio.wait_for(subscribed.wait(), 30)
        for index in range(100):
            coordinator._response_lifecycle_lock(MessageTarget.resolve("!room:test", f"$other-{index}", "$source"))
        assert coordinator._response_lifecycle_lock(target) is lock
        assert coordinator._get_or_create_queued_signal(target).human_signal is signal
        if terminal == "cancelled":
            await runtime.cancel("work", owner=owner, depth=0)
        else:
            release.set()
        waited = await asyncio.wait_for(waiter, 30)
        assert waited.job.status == terminal
        await runtime.acknowledge_wait("work", waited.token)
        assert not signal.has_subscribers
        coordinator._response_lifecycle_lock(MessageTarget.resolve("!room:test", "$after", "$source"))
        assert target.lifecycle_key not in coordinator._thread_queued_signals
        assert target.lifecycle_key not in coordinator._response_lifecycle_locks
    finally:
        release.set()
        if waiter is not None:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_projection", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
async def test_native_expired_receipt_survives_sdk_wait_and_restart(
    delegation_context: ToolRuntimeContext,
    after_projection: bool,
    revoked: bool,
) -> None:
    """Retrieval checks current grants, not erased task input, even after SDK projection."""
    config, paths = delegation_context.config, delegation_context.runtime_paths
    owner = build_execution_identity_from_runtime_context(delegation_context)
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: config, lambda _: None, AgentReplyMembershipIndex())
    await coordinator.initialize()
    runtime = coordinator.runtime
    register_background_runtime(paths, runtime)
    child = prepare_child_turn(
        "leader",
        "code",
        "Original private task",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
    )
    storage = create_session_storage("leader", config, paths, owner)
    executions = 0

    async def operation() -> BackgroundOutcome:
        nonlocal executions
        executions += 1
        return BackgroundOutcome("completed", "Saved native output")

    async def source_finished(_job: object) -> bool:
        return True

    async def forbidden_child(_child: DelegationChild, **_kwargs: object) -> str:
        pytest.fail("Retrieval must never re-execute the child")

    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("job", "retrieve", action="wait", job_id=child.delegation_id)]),
            ModelResponse(content="Receipt read."),
        ],
    )
    install_tool_job_execution(model)
    actor = Agent(id="leader", model=model, tools=[JobTools(paths, owner)], db=storage, telemetry=False)
    paused = None
    try:
        await start_delegation(runtime, child, owner=owner, operation=operation)
        waited = await runtime.wait(child.delegation_id, owner=owner, depth=0)
        await runtime.acknowledge_wait(child.delegation_id, waited.token)
        async with execution_resources():
            with tool_runtime_context(delegation_context):
                if after_projection:
                    paused = await actor.arun(
                        "Read the receipt",
                        session_id=owner.session_id,
                        user_id=owner.requester_id,
                    )
                    assert paused.status is RunStatus.paused
                await runtime.expire_consumed(
                    before=datetime.now(UTC) + timedelta(days=31),
                    source_finished=source_finished,
                )
                expired = await runtime.lookup(child.delegation_id, owner=owner, depth=0)
                assert expired.result_expired
                assert expired.adapter["child"]["task"] == ""
                await coordinator.stop()
                await coordinator.initialize()
                runtime = coordinator.runtime
                await runtime.recover()
                register_background_runtime(paths, runtime)
                if revoked:
                    config.agents["leader"].delegate_to.clear()
                response = paused or await actor.arun(
                    "Read the receipt",
                    session_id=owner.session_id,
                    user_id=owner.requester_id,
                )
                response = await drive_delegations(
                    actor,
                    response,
                    run_child=forbidden_child,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=owner,
                )
        assert response.status is RunStatus.completed
        retrieved = next(tool for tool in response.tools if tool.tool_call_id == "retrieve")
        assert retrieved.result == ("Tool job is not available in this conversation." if revoked else expired.result)
        assert executions == 1
    finally:
        await coordinator.stop()
        storage.close()
