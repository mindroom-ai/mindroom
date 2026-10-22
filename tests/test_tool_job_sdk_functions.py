"""Framework-generated knowledge and skill functions keep their native schemas and run inline."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Never

import pytest

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.knowledge.read_protocol import SearchHit
from mindroom.knowledge.search import AgentKnowledge, KnowledgeSource, KnowledgeSourceDescription
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.control import JobControl, job_control_context
from mindroom.tool_jobs.execution import with_job_admission
from mindroom.tool_jobs.execution_authority import JobCall, authorized_tool_call
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import JobAccessError, register_background_runtime
from mindroom.tool_system.agno_toolkits import Function, adapt_function
from mindroom.tool_system.invocation import ToolCall, ToolCallContext
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from mindroom.tool_system.worker_routing import tool_execution_identity
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.test_skills import _write_skill
from tests.tool_job_helpers import (
    completed_delegation_job,
    job_actor,
    job_tool,
    managed_team_config,
    run_built_turn,
    saved_jobs,
    team_coordinator,
    tool_job_runtime,
)

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

_SDK_FUNCTIONS = ("search_knowledge_base", "get_skill_instructions")


class _KnowledgeIndex:
    """A published index that answers every query with one synthetic document."""

    def search(self, query: str, *, limit: int) -> list[SearchHit]:
        del limit
        return [SearchHit(content=f"Knowledge about {query}")]

    async def asearch(self, query: str, *, limit: int) -> list[SearchHit]:
        return self.search(query, limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_sdk_generated_functions_run_inline_with_native_schemas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    """Only MindRoom-assembled toolkits become jobs; knowledge search keeps main's schema either way."""
    config = managed_team_config(tmp_path)
    config.background_tool_jobs.enabled = enabled
    config.memory.backend = "file"  # A file-memory agent has the workspace its skills load from.
    config.defaults.tools = []
    config.knowledge_bases = {"probe": KnowledgeBaseConfig(path=str(tmp_path / "knowledge"))}
    lead = config.agents["lead"]
    lead.delegate_to = []
    lead.knowledge_bases = ["probe"]
    coordinator = team_coordinator(tmp_path, config)
    paths, owner = coordinator.runtime_paths, completed_delegation_job().owner
    entity_ids(config, paths)
    workspace = resolve_agent_runtime("lead", config, paths, execution_identity=owner).workspace
    assert workspace is not None
    _write_skill(workspace.root / "skills", "demo", "Sample skill")
    model = ScriptedModel(
        [
            calls(
                tool_call("search_knowledge_base", "knowledge", query="tea"),
                tool_call("get_skill_instructions", "skill", skill_name="demo"),
            ),
            answer("done"),
        ],
    )
    ScriptedAgents({"lead": model}).install(monkeypatch)
    knowledge = AgentKnowledge(
        sources=(
            KnowledgeSource(
                description=KnowledgeSourceDescription(base_id="probe", description=""),
                index=_KnowledgeIndex(),
            ),
        ),
    )
    try:
        await coordinator.sync()
        async with execution_resources():
            with (
                tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)),
                tool_execution_identity(owner),
            ):
                build = create_agent("lead", config, paths, owner, knowledge=knowledge)
                record = await run_built_turn(
                    build,
                    config=config,
                    runtime_paths=paths,
                    prompt="Look up tea and read the demo skill.",
                    session_id=owner.session_id or "session",
                )
        assert record.tools is not None
        results = {tool.tool_name: tool for tool in record.tools}
        assert set(results) == set(_SDK_FUNCTIONS)
        assert all(not tool.tool_call_error for tool in results.values())
        assert "Knowledge about tea" in str(results["search_knowledge_base"].result)
        assert "# Body" in str(results["get_skill_instructions"].result)
        schemas = {definition.name: definition.parameters for definition in model.requests[0].tools}
        assert set(schemas["search_knowledge_base"]["properties"]) == {"query"}
        assert all("background_after" not in schemas[name]["properties"] for name in _SDK_FUNCTIONS)
        assert await saved_jobs(paths.storage_root) == {}
    finally:
        await coordinator.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_by", ["cancel", "revoked grant"])
async def test_an_sdk_function_inside_a_stopped_job_never_runs(tmp_path: Path, stopped_by: str) -> None:
    """Generated functions have no tool hooks, so the job's own stop and grant checks keep them from running."""
    ran: list[str] = []

    def search(query: str) -> str:
        return query

    async def leaf(arguments: dict[str, object]) -> Never:
        ran.append(str(arguments))
        raise AssertionError

    def authorize_execution(*_args: object) -> None:
        if stopped_by == "revoked grant":
            msg = "No longer authorized"
            raise JobAccessError(msg)

    paths = _runtime_paths(tmp_path)
    context = _delegate_runtime_context(Config(agents={"leader": AgentConfig(display_name="Leader")}), paths)
    runtime = await tool_job_runtime(tmp_path, authorize_execution=authorize_execution)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    control = JobControl()
    if stopped_by == "cancel":
        control.cancel()
    run = ToolCallContext(run_id="job-run", session_id="session", call_id="sdk-call")
    job_call = JobCall(tool=job_tool(search), arguments={}, context=run, team=False)
    generated = adapt_function(Function.from_callable(search))
    (admit,) = with_job_admission(job_actor(), ())
    try:
        async with execution_resources():
            with (
                tool_runtime_context(context),
                job_control_context(control),
                authorized_tool_call(build_execution_identity_from_runtime_context(context), job_call),
                pytest.raises(asyncio.CancelledError if stopped_by == "cancel" else JobAccessError),
            ):
                await admit(ToolCall(function=generated, arguments={"query": "tea"}, context=run), leaf)
    finally:
        await runtime.shutdown()
    assert ran == []
