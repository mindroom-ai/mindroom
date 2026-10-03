"""Terminal job reads release wait ownership, and expired jobs stay unavailable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from functools import partial
from typing import TYPE_CHECKING

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
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobAccessError, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.identity_helpers import entity_ids
from tests.tool_job_helpers import (
    lookup,
    start_delegation_job,
    tool_job_journal,
)

if TYPE_CHECKING:
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
async def test_expired_native_job_stays_unavailable_to_sdk_wait_after_restart(
    delegation_context: ToolRuntimeContext,
) -> None:
    """Retrieving a deleted delegation is unavailable and never reruns it."""
    config, paths = delegation_context.config, delegation_context.runtime_paths
    owner = build_execution_identity_from_runtime_context(delegation_context)
    coordinator = ToolJobRuntimeCoordinator(
        paths,
        lambda: config,
        lambda _: None,
        AgentReplyMembershipIndex(),
        partial(tool_job_journal, paths.storage_root),
    )
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
    try:
        await start_delegation_job(runtime, child, owner=owner, operation=operation)
        waited = await runtime.wait(child.delegation_id, owner=owner, depth=0)
        await runtime.acknowledge_wait(child.delegation_id, waited.claim)
        async with execution_resources():
            with tool_runtime_context(delegation_context):
                await runtime.expire_consumed(
                    before=datetime.now(UTC) + timedelta(days=31),
                    source_finished=source_finished,
                )
                await coordinator.stop()
                await coordinator.initialize()
                runtime = coordinator.runtime
                await runtime.recover()
                register_background_runtime(paths, runtime)
                with pytest.raises(JobAccessError, match="not available"):
                    await lookup(runtime, child.delegation_id, owner=owner, depth=0)
                response = await actor.arun(
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
        assert retrieved.result == "Tool job is not available in this conversation."
        assert executions == 1
    finally:
        await coordinator.stop()
        storage.close()
