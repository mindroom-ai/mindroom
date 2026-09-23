"""Accepted memory work obeys the current effective memory setting."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.agents import build_agent_toolkit
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.memory import list_all_agent_memories
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.authorization import (
    authority_snapshot,
    bind_actor_authority,
    bind_toolkit_authority,
    function_authority,
)
from mindroom.tool_jobs.provenance import function_provenance
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")


@pytest.fixture
def memory_context(tmp_path: Path) -> ToolRuntimeContext:
    """Configure a real file-memory store and an explicitly authorized requester."""
    config = Config(
        agents={
            "leader": AgentConfig(
                display_name="Leader",
                tools=["memory"],
                learning=False,
                access=ResponderAccessConfig(users=["@alice:example.org"], current_room_members=False),
            ),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "file"},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    return _delegate_runtime_context(config, paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_setting", [False, True])
@pytest.mark.parametrize("revoked", [False, True])
async def test_memory_disabled_after_acceptance_cannot_write_retained_store(
    memory_context: ToolRuntimeContext,
    agent_setting: bool,
    revoked: bool,
) -> None:
    """A real SDK memory call uses current grants even though its toolkit retains old config."""
    config, paths = memory_context.config, memory_context.runtime_paths
    if agent_setting:
        config.agents["leader"].memory_backend = "file"
    current = config
    owner = build_execution_identity_from_runtime_context(memory_context)
    coordinator = ToolJobRuntimeCoordinator(paths, lambda: current, lambda _: None, AgentReplyMembershipIndex())
    toolkit = build_agent_toolkit(
        "memory",
        agent_name="leader",
        config=config,
        runtime_paths=paths,
        worker_tools=[],
        runtime_overrides=None,
        execution_identity=owner,
    )
    assert toolkit is not None
    bind_toolkit_authority(toolkit, authored_name="memory")
    function = toolkit.get_async_functions()["add_memory"]
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("add_memory", "write-memory", content="accepted memory marker")]),
            ModelResponse(content="done"),
        ],
    )
    install_tool_job_execution(model)
    actor = bind_actor_authority(Agent(id="leader", model=model, tools=[toolkit]), authority_snapshot(config, "leader"))
    function._agent = actor
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def operation() -> BackgroundOutcome:
        try:
            started.set()
            await release.wait()
            response = await actor.arun("Store the marker", session_id=owner.session_id)
            return BackgroundOutcome("completed", str(response.content))
        finally:
            finished.set()

    try:
        await coordinator.sync()
        async with execution_resources():
            with tool_runtime_context(replace(memory_context, config_provider=lambda: current)):
                await coordinator.runtime.start(
                    JobSpec(
                        "accepted-memory",
                        "add_memory",
                        0,
                        toolkit_name="memory",
                        adapter={"origin": function_provenance(function), "authority": function_authority(function)},
                    ),
                    owner=owner,
                    operation=operation,
                )
                await asyncio.wait_for(started.wait(), 30)
                current = config.model_copy(deep=True)
                if revoked:
                    if agent_setting:
                        current.agents["leader"].memory_backend = "none"
                    else:
                        current.memory.backend = "none"
                release.set()
                await asyncio.wait_for(finished.wait(), 30)
                current = config
                waited = await coordinator.runtime.wait("accepted-memory", owner=owner, depth=0)
                await coordinator.runtime.release_wait("accepted-memory", waited.token)
                memories = await list_all_agent_memories(
                    "leader",
                    paths.storage_root,
                    config,
                    paths,
                    execution_identity=owner,
                )
                assert any("accepted memory marker" in memory["memory"] for memory in memories) is not revoked
    finally:
        release.set()
        await coordinator.stop()
