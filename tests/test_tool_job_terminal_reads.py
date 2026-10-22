"""Terminal job reads release wait ownership, and expired jobs stay unavailable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.lifecycle import prepare_child_turn
from mindroom.llm.runs import RunStatus
from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import BackgroundOutcome, JobAccessError, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.tool_job_helpers import (
    job_actor,
    lookup,
    run_job_turn,
    start_delegation_job,
    tool_job_journal,
    toolkit_job_tools,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.event_journal import EventJournalStore
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
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    context = _delegate_runtime_context(config, paths)
    for name in config.agents:
        with_responder_access(config, name, users=[context.requester_id])
    return context


@pytest.mark.asyncio
async def test_expired_native_job_stays_unavailable_to_a_wait_after_restart(
    delegation_context: ToolRuntimeContext,
) -> None:
    """Retrieving a deleted delegation is unavailable and never reruns it."""
    config, paths = delegation_context.config, delegation_context.runtime_paths
    owner = build_execution_identity_from_runtime_context(delegation_context)
    journals: list[EventJournalStore] = []

    def open_journal() -> EventJournalStore:
        # Each start opens the journal again, as a restarted process does.
        journals.append(tool_job_journal(paths.storage_root))
        return journals[-1]

    coordinator = ToolJobRuntimeCoordinator(
        paths,
        lambda: config,
        lambda _: None,
        AgentReplyMembershipIndex(),
        open_journal,
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
        budget_monitor=None,
    )
    storage = create_session_storage("leader", config, paths, owner)
    executions = 0

    async def operation() -> BackgroundOutcome:
        nonlocal executions
        executions += 1
        return BackgroundOutcome("completed", "Saved native output")

    async def source_finished(_job: object) -> bool:
        return True

    model = ScriptedModel(
        [calls(tool_call("job", "retrieve", action="wait", job_id=child.delegation_id)), answer("Receipt read.")],
    )
    actor = job_actor(*toolkit_job_tools(JobTools(paths, owner), authored_name="job").values())
    try:
        await start_delegation_job(runtime, child, owner=owner, operation=operation)
        waited = await runtime.wait(child.delegation_id, owner=owner, depth=0)
        await runtime.acknowledge_wait(child.delegation_id, waited.claim)
        async with execution_resources():
            with tool_runtime_context(delegation_context):
                await runtime.expire_ended(
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
                response = await run_job_turn(
                    actor,
                    model,
                    "Read the receipt",
                    session_id=owner.session_id or "session",
                    store=storage,
                )
        assert response.status is RunStatus.completed
        retrieved = next(tool for tool in response.tools or [] if tool.tool_call_id == "retrieve")
        assert str(retrieved.result).startswith("No job with this ID is available to you in this conversation.")
        assert executions == 1
    finally:
        await coordinator.stop()
        for journal in journals:
            await journal.close()
