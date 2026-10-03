"""SDK-generated knowledge, skill, and learning functions keep their native schemas and run inline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from agno.knowledge.knowledge import Knowledge
from agno.learn.stores.user_memory import UserMemoryStore
from agno.models.response import ModelResponse

from mindroom.agent_storage import get_agent_runtime_state_dbs
from mindroom.agents import create_agent
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.resources import execution_resources
from mindroom.tool_jobs.runtime import saved_job_paths
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import tool_execution_identity
from tests.delegation_helpers import _call, _delegate_runtime_context
from tests.history_helpers import RecordingModel
from tests.identity_helpers import entity_ids
from tests.test_skills import _write_skill
from tests.tool_job_helpers import completed_delegation_job, managed_team_config, team_coordinator

if TYPE_CHECKING:
    from pathlib import Path

    from agno.models.message import Message

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

_SDK_FUNCTIONS = ("search_knowledge_base", "get_skill_instructions", "update_user_memory")


@dataclass
class _SdkFunctionModel(RecordingModel):
    """Call each SDK-generated function once and answer the extraction that learning runs internally."""

    schemas: dict[str, dict[str, Any]] = field(default_factory=dict)

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        messages = cast("list[Message]", kwargs["messages"])
        tools = cast("list[dict[str, Any]]", kwargs.get("tools") or [])
        schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
        if messages[-1].role == "tool":
            return ModelResponse(content="done")
        if "add_memory" in schemas:
            return ModelResponse(tool_calls=[_call("add_memory", "extract", memory="User prefers jasmine tea.")])
        self.schemas = schemas
        return ModelResponse(
            tool_calls=[
                _call("search_knowledge_base", "knowledge", query="tea"),
                _call("get_skill_instructions", "skill", skill_name="demo"),
                _call("update_user_memory", "learning", task="User prefers jasmine tea."),
            ],
        )


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
    lead.learning = True
    lead.learning_mode = "agentic"
    coordinator = team_coordinator(tmp_path, config)
    paths, owner = coordinator.runtime_paths, completed_delegation_job().owner
    entity_ids(config, paths)
    workspace = resolve_agent_runtime("lead", config, paths, execution_identity=owner).workspace
    assert workspace is not None
    _write_skill(workspace.root / "skills", "demo", "Sample skill")
    model = _SdkFunctionModel(id="test")
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    agent = create_agent("lead", config, paths, owner, session_id=owner.session_id, knowledge=Knowledge(name="probe"))

    async def retrieve(query: str, num_documents: int | None = None) -> list[dict[str, str]]:
        del num_documents
        return [{"content": f"Knowledge about {query}", "name": "synthetic document"}]

    agent.knowledge_retriever = retrieve
    try:
        await coordinator.sync()
        async with execution_resources():
            with (
                tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=owner)),
                tool_execution_identity(owner),
            ):
                response = await agent.arun(
                    "Look up tea, read the demo skill, and remember my preference.",
                    session_id=owner.session_id,
                    user_id=owner.requester_id,
                )
        assert response.tools is not None
        results = {tool.tool_name: tool for tool in response.tools}
        assert set(results) == set(_SDK_FUNCTIONS)
        assert all(not tool.tool_call_error for tool in results.values())
        assert "Knowledge about tea" in str(results["search_knowledge_base"].result)
        assert "# Body" in str(results["get_skill_instructions"].result)
        assert set(model.schemas["search_knowledge_base"]["properties"]) == {"query"}
        assert all("wait_timeout" not in model.schemas[name]["properties"] for name in _SDK_FUNCTIONS)
        assert saved_job_paths(paths.storage_root / "tool_jobs") == []
        machine = agent.learning_machine
        assert machine is not None
        store = machine.user_memory_store
        assert isinstance(store, UserMemoryStore)
        saved = await store.aget(user_id=owner.requester_id)
        assert saved is not None
        assert [memory["content"] for memory in saved.memories] == ["User prefers jasmine tea."]
    finally:
        await coordinator.stop()
        for db in get_agent_runtime_state_dbs(agent):
            if db is not None:
                db.close()
