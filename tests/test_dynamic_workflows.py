"""Tests for Dynamic Workflow storage and tools."""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest
import yaml
from agno.factory import RequestContext
from agno.run.agent import RunOutput, RunStatus
from agno.tools import Toolkit
from agno.workflow import Workflow, WorkflowFactory
from agno.workflow.types import StepInput, StepOutput

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools import dynamic_workflow as dynamic_workflow_module
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.dynamic_workflows.agno_adapter import build_agno_workflow_factory
from mindroom.dynamic_workflows.runner import DynamicWorkflowExecutionError, execute_workflow_spec
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.store import DynamicWorkflowError, DynamicWorkflowStore
from mindroom.entity_resolution import entity_identity_registry
from mindroom.tool_approval import _matching_tool_approval_rule
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path


def _fake_stream_agent(
    *,
    content: str,
    status: RunStatus = RunStatus.completed,
    on_run: Callable[..., None] | None = None,
) -> SimpleNamespace:
    """Build a fake Agent matching the streaming participant run contract.

    ``_arun_agent`` calls ``agent.arun(..., stream=True, yield_run_output=True)`` (not awaited)
    and consumes the event iterator, treating the final ``RunOutput`` as the result.
    """

    def arun(prompt: str, *, user_id: str, session_id: str, **_kwargs: object) -> AsyncIterator[RunOutput]:
        if on_run is not None:
            on_run(prompt, user_id=user_id, session_id=session_id)

        async def _events() -> AsyncIterator[RunOutput]:
            yield RunOutput(content=content, status=status)

        return _events()

    return SimpleNamespace(arun=arun)


def _workflow_spec(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "schema_version": 1,
        "id": "competitor-research-report",
        "name": "Competitor Research Report",
        "description": "Create a cited HTML report about competitors.",
        "kind": "workflow",
        "inputs": {
            "type": "object",
            "required": ["topic"],
            "properties": {"topic": {"type": "string"}},
        },
        "participants": [
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "model": "claude-sonnet-4-6",
                "tools": [],
            },
        ],
        "workflow": [
            {
                "id": "write",
                "type": "agent_step",
                "participant": "writer",
                "prompt": "Write a cited report in Markdown.",
            },
        ],
        "outputs": [{"id": "report_html", "type": "html_report", "from_step": "write"}],
        "permissions": {
            "max_runtime_seconds": 1800,
            "max_concurrent_agents": 4,
            "max_total_agents": 16,
            "models": ["claude-sonnet-4-6"],
            "tools": [],
            "data": {
                "matrix_history": "none",
                "attachments": "none",
                "knowledge_bases": [],
            },
        },
    }
    spec.update(overrides)
    return spec


def _make_context(tmp_path: Path) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths = runtime_paths.__class__(
        config_path=runtime_paths.config_path,
        config_dir=runtime_paths.config_dir,
        env_path=runtime_paths.env_path,
        storage_root=runtime_paths.storage_root,
        process_env={
            **dict(runtime_paths.process_env),
            "MINDROOM_PUBLIC_URL": "https://acme.mindroom.chat",
        },
        env_file_values=runtime_paths.env_file_values,
    )
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])},
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        runtime_paths,
    )
    return ToolRuntimeContext(
        agent_name="general",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=None,
        reply_to_event_id="$event:localhost",
        storage_path=None,
    )


def _make_multi_agent_context(tmp_path: Path, *, room_agents: list[str]) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"]),
                "specialist": AgentConfig(display_name="Specialist Agent"),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(config, runtime_paths)
    registry = entity_identity_registry(config, runtime_paths)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id=registry.current_id("general").full_id)
    for agent_name in room_agents:
        room.add_member(registry.current_id(agent_name).full_id, config.agents[agent_name].display_name, None)
    room.members_synced = True
    return ToolRuntimeContext(
        agent_name="general",
        room_id="!room:localhost",
        thread_id="$thread:localhost",
        resolved_thread_id="$thread:localhost",
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        conversation_cache=AsyncMock(),
        event_cache=make_event_cache_mock(),
        room=room,
        reply_to_event_id="$event:localhost",
        storage_path=None,
    )


def _make_private_context(tmp_path: Path, *, requester_id: str) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["dynamic_workflow"],
                    private=AgentPrivateConfig(per="user_agent", root="mind_data"),
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        runtime_paths,
    )
    return replace(
        _make_context(tmp_path),
        requester_id=requester_id,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )


def _tool_payload(result: str) -> dict[str, Any]:
    return json.loads(result)


def test_dynamic_workflow_tool_registered() -> None:
    """Dynamic Workflow tool metadata should be visible to config and dashboard surfaces."""
    metadata = TOOL_METADATA["dynamic_workflow"]

    assert metadata.display_name == "Dynamic Workflows"
    assert metadata.function_names == (
        "create_workflow",
        "validate_workflow",
        "update_workflow",
        "run_workflow",
        "get_workflow_run",
        "list_workflows",
        "list_workflow_revisions",
    )


def test_create_workflow_persists_immutable_revision(tmp_path: Path) -> None:
    """Creating a workflow should write a pointer file and immutable revision file."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    created = store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    workflow_dir = tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report"
    pointer = yaml.safe_load((workflow_dir / "workflow.yaml").read_text(encoding="utf-8"))
    revision = yaml.safe_load((workflow_dir / "revisions/000001.yaml").read_text(encoding="utf-8"))
    assert created.workflow_id == "competitor-research-report"
    assert created.active_revision == "000001"
    assert pointer["active_revision"] == "000001"
    assert pointer["created_by"] == "general"
    assert revision["name"] == "Competitor Research Report"


def test_update_workflow_creates_new_revision_without_mutating_old_one(tmp_path: Path) -> None:
    """Updating a workflow should create a new active revision and keep old specs unchanged."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_workflow_spec(description="Original description."),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    updated = store.update_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        patch={"description": "Updated description."},
        updated_by="general",
        reason="tighten report style",
    )

    workflow_dir = tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report"
    first_revision = yaml.safe_load((workflow_dir / "revisions/000001.yaml").read_text(encoding="utf-8"))
    second_revision = yaml.safe_load((workflow_dir / "revisions/000002.yaml").read_text(encoding="utf-8"))
    pointer = yaml.safe_load((workflow_dir / "workflow.yaml").read_text(encoding="utf-8"))
    assert updated.active_revision == "000002"
    assert pointer["active_revision"] == "000002"
    assert first_revision["description"] == "Original description."
    assert second_revision["description"] == "Updated description."
    assert second_revision["revision_reason"] == "tighten report style"


def test_concurrent_update_workflow_creates_distinct_revisions(tmp_path: Path) -> None:
    """Concurrent updates should serialize revision numbering for one workflow."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_workflow_spec(description="Original description."),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    def update_description(description: str) -> str:
        summary = store.update_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            patch={"description": description},
            updated_by="general",
            reason=description,
        )
        return summary.active_revision

    with ThreadPoolExecutor(max_workers=2) as executor:
        revisions = sorted(
            future.result()
            for future in [
                executor.submit(update_description, "First update."),
                executor.submit(update_description, "Second update."),
            ]
        )

    assert revisions == ["000002", "000003"]
    assert store.list_workflow_revisions(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
    ) == ["000001", "000002", "000003"]


def test_update_workflow_rejects_workflow_id_changes(tmp_path: Path) -> None:
    """Workflow revisions should not mutate the persisted workflow identity."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    with pytest.raises(DynamicWorkflowError, match="Workflow ID is immutable"):
        store.update_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            patch={"id": "different-workflow"},
            updated_by="general",
            reason="bad patch",
        )


def test_run_workflow_writes_run_record_and_private_html_report(tmp_path: Path) -> None:
    """Running a workflow should pin the active revision and write a private report artifact."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(
        store,
        participant_executor=lambda **_: "Report about Agno factories.",
    )
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert run.status == "completed"
    assert run.revision == "000001"
    assert run.report_url is not None
    assert run.report_url.startswith(
        f"https://acme.mindroom.chat/reports/private/agent/general/competitor-research-report/{run.run_id}",
    )
    assert "access_token=" not in run.report_url
    assert loaded.status == "completed"
    assert loaded.artifacts["report_html"].endswith("/report.html")
    report_path = tmp_path / "mindroom_data" / loaded.artifacts["report_html"]
    report_html = report_path.read_text(encoding="utf-8")
    assert "Competitor Research Report" in report_html
    assert "Agno factories" in report_html


def test_run_workflow_persists_failed_run_when_completion_persistence_fails(tmp_path: Path) -> None:
    """Completion persistence failures should not leave the run stuck as running."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store, participant_executor=lambda **_: object())
    store.create_workflow(
        spec=_workflow_spec(
            workflow=[
                {"id": "collect", "type": "agent_step", "participant": "writer", "prompt": "Collect raw data."},
                {"id": "report", "type": "report_step", "body_template": "Report completed."},
            ],
            outputs=[{"id": "report_html", "type": "html_report", "from_step": "report"}],
        ),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
    )
    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )

    assert loaded.status == "failed"
    assert "not JSON serializable" in str(loaded.error)


def test_run_workflow_rejects_missing_required_input_before_execution(tmp_path: Path) -> None:
    """Workflow runs should validate declared input schema before executing any step."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert loaded.status == "failed"
    assert loaded.error == "Input field 'topic' is required."
    assert loaded.steps == []


def test_validate_workflow_spec_rejects_invalid_input_schema_type(tmp_path: Path) -> None:
    """Workflow input schemas should be validated before specs are persisted."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="Unsupported workflow input schema type"):
        store.create_workflow(
            spec=_workflow_spec(
                inputs={
                    "type": "object",
                    "properties": {"topic": {"type": "secret_string"}},
                },
            ),
            scope="agent",
            owner_id="general",
            created_by="general",
            reason="bad schema",
        )


def test_validate_workflow_spec_rejects_empty_input_schema_type_list(tmp_path: Path) -> None:
    """Empty input type lists should not silently disable type validation."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="type list must be non-empty"):
        store.validate_workflow(
            _workflow_spec(
                inputs={
                    "type": "object",
                    "properties": {"topic": {"type": []}},
                },
            ),
        )


def test_validate_workflow_spec_requires_supported_schema_version(tmp_path: Path) -> None:
    """Workflow specs should not silently run missing or future schema versions."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="schema_version"):
        store.validate_workflow(_workflow_spec(schema_version=2))
    with pytest.raises(DynamicWorkflowError, match="schema_version"):
        store.validate_workflow(_workflow_spec(schema_version=True))
    with pytest.raises(DynamicWorkflowError, match="schema_version"):
        store.validate_workflow(_workflow_spec(schema_version=1.0))

    spec_without_version = _workflow_spec()
    del spec_without_version["schema_version"]
    with pytest.raises(DynamicWorkflowError, match="schema_version"):
        store.validate_workflow(spec_without_version)


def test_validate_workflow_spec_rejects_unsupported_input_schema_keywords(tmp_path: Path) -> None:
    """Workflow input schemas should not accept constraints the validator ignores."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="default"):
        store.validate_workflow(
            _workflow_spec(
                inputs={
                    "type": "object",
                    "properties": {"topic": {"type": "string", "default": "Agno"}},
                },
            ),
        )


def test_run_workflow_enforces_input_schema_enum(tmp_path: Path) -> None:
    """Declared enum constraints should be enforced before workflow execution."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(
            inputs={
                "type": "object",
                "required": ["topic", "visibility"],
                "properties": {
                    "topic": {"type": "string"},
                    "visibility": {"type": "string", "enum": ["private"]},
                },
            },
            workflow=[
                {
                    "id": "research",
                    "type": "transform_step",
                    "template": "Research brief for {input.topic}.",
                },
            ],
            outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
        ),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories", "visibility": "public"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    assert run.status == "failed"
    assert "declared enum values" in str(run.error)


def test_validate_workflow_spec_rejects_excessive_agent_steps(tmp_path: Path) -> None:
    """Workflow permissions should cap the amount of LLM work one run can trigger."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="max_total_agents"):
        store.validate_workflow(
            _workflow_spec(
                workflow=[
                    {"id": "write_a", "type": "agent_step", "participant": "writer", "prompt": "Write A."},
                    {"id": "write_b", "type": "agent_step", "participant": "writer", "prompt": "Write B."},
                ],
                outputs=[{"id": "report", "type": "text", "from_step": "write_b"}],
                permissions={"max_total_agents": 1, "tools": []},
            ),
        )


def test_validate_workflow_spec_normalizes_tool_grants(tmp_path: Path) -> None:
    """Tool grants of any registered name should validate, strip, and dedupe at the store layer."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    validated = store.validate_workflow(
        _workflow_spec(
            participants=[
                {
                    "id": "writer",
                    "kind": "ephemeral_agent",
                    "name": "Report Writer",
                    "model": "claude-sonnet-4-6",
                    "tools": [" shell ", "website", "shell"],
                },
            ],
            permissions={"tools": ["shell", "website"]},
        ),
    )

    participants = validated["participants"]
    assert isinstance(participants, list)
    assert participants[0]["tools"] == ["shell", "website"]
    permissions = validated["permissions"]
    assert isinstance(permissions, dict)
    assert permissions["tools"] == ["shell", "website"]


def test_validate_workflow_spec_rejects_participant_tool_not_granted_by_permissions(tmp_path: Path) -> None:
    """Participant tools must be a subset of the workflow-level permissions.tools grant."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match=r"not granted by permissions\.tools"):
        store.validate_workflow(
            _workflow_spec(
                participants=[{"id": "writer", "kind": "ephemeral_agent", "tools": ["duckduckgo"]}],
                permissions={"tools": ["website"]},
            ),
        )


def test_validate_workflow_tool_policy_rejects_unknown_tool(tmp_path: Path) -> None:
    """The context-aware policy layer rejects tool grants that name no registered tool."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()

    with tool_runtime_context(context):
        unknown_payload = _tool_payload(
            tool.validate_workflow(
                _workflow_spec(
                    participants=[{"id": "writer", "kind": "ephemeral_agent", "tools": ["not_a_real_tool"]}],
                    permissions={"tools": ["not_a_real_tool"]},
                ),
            ),
        )

    assert unknown_payload["status"] == "error"
    assert "not a registered tool" in unknown_payload["message"]


@pytest.mark.parametrize(
    "restricted_tool",
    ["compact_context", "delegate", "dynamic_tools", "dynamic_workflow", "memory", "self_config"],
)
def test_validate_workflow_tool_policy_rejects_each_restricted_tool(tmp_path: Path, restricted_tool: str) -> None:
    """Every agent-infrastructure tool must be rejected as a participant grant."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()

    with tool_runtime_context(context):
        # Place the grant only in permissions.tools, with no participant using it: the
        # policy layer is the sole gate for this case (store validation is shape-only and
        # the subset rule passes trivially when no participant references the tool).
        permissions_only_payload = _tool_payload(
            tool.validate_workflow(
                _workflow_spec(
                    participants=[{"id": "writer", "kind": "ephemeral_agent"}],
                    permissions={"tools": [restricted_tool]},
                ),
            ),
        )
        participant_payload = _tool_payload(
            tool.validate_workflow(
                _workflow_spec(
                    participants=[{"id": "writer", "kind": "ephemeral_agent", "tools": [restricted_tool]}],
                    permissions={"tools": [restricted_tool]},
                ),
            ),
        )

    assert permissions_only_payload["status"] == "error"
    assert "agent-infrastructure" in permissions_only_payload["message"]
    assert participant_payload["status"] == "error"
    assert "agent-infrastructure" in participant_payload["message"]


def test_validate_workflow_spec_rejects_room_agent_participant_tools(tmp_path: Path) -> None:
    """Room-agent participants must stay tool-less even for allowlisted tools."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="only available to ephemeral participants"):
        store.validate_workflow(
            _workflow_spec(
                participants=[{"id": "writer", "kind": "room_agent", "agent": "general", "tools": ["duckduckgo"]}],
                permissions={"tools": ["duckduckgo"]},
            ),
        )


def test_validate_workflow_spec_rejects_unimplemented_thread_data_permissions(tmp_path: Path) -> None:
    """Workflow specs should not declare data access the runner does not provide."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="matrix_history"):
        store.validate_workflow(
            _workflow_spec(
                permissions={
                    "tools": [],
                    "data": {
                        "matrix_history": "current_thread",
                        "attachments": "none",
                        "knowledge_bases": [],
                    },
                },
            ),
        )


def test_validate_workflow_spec_rejects_unimplemented_knowledge_data_permissions(tmp_path: Path) -> None:
    """Workflow specs should not declare knowledge access the runner does not provide."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="knowledge_bases"):
        store.validate_workflow(
            _workflow_spec(
                permissions={
                    "tools": [],
                    "data": {
                        "matrix_history": "none",
                        "attachments": "none",
                        "knowledge_bases": ["reference"],
                    },
                },
            ),
        )


def test_validate_workflow_spec_rejects_unknown_data_permissions(tmp_path: Path) -> None:
    """Workflow specs should not accept unimplemented data permission namespaces."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="external_db"):
        store.validate_workflow(
            _workflow_spec(
                permissions={
                    "tools": [],
                    "data": {
                        "matrix_history": "none",
                        "attachments": "none",
                        "knowledge_bases": [],
                        "external_db": "customers",
                    },
                },
            ),
        )


def test_validate_workflow_spec_rejects_unsupported_participant_fields(tmp_path: Path) -> None:
    """Workflow specs should not accept participant fields the runner ignores."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="memory_mode"):
        store.validate_workflow(
            _workflow_spec(
                participants=[
                    {
                        "id": "researcher",
                        "kind": "room_agent",
                        "agent": "general",
                        "memory_mode": "read_only",
                    },
                ],
                workflow=[
                    {
                        "id": "write",
                        "type": "agent_step",
                        "participant": "researcher",
                        "prompt": "Write a cited report.",
                    },
                ],
            ),
        )


def test_validate_workflow_spec_rejects_unsupported_agent_step_fields(tmp_path: Path) -> None:
    """Workflow specs should not accept control-flow fields the runner ignores."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="input_from"):
        store.validate_workflow(
            _workflow_spec(
                workflow=[
                    {
                        "id": "plan",
                        "type": "transform_step",
                        "template": "Plan for {input.topic}.",
                    },
                    {
                        "id": "write",
                        "type": "agent_step",
                        "participant": "writer",
                        "input_from": "plan",
                        "prompt": "Write from {steps.plan}.",
                    },
                ],
            ),
        )


def test_validate_workflow_spec_rejects_ambiguous_report_step_source(tmp_path: Path) -> None:
    """Report steps should not accept a source field the runner would ignore."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="report source"):
        store.validate_workflow(
            _workflow_spec(
                workflow=[
                    {
                        "id": "research",
                        "type": "transform_step",
                        "template": "Research brief for {input.topic}.",
                    },
                    {
                        "id": "write",
                        "type": "report_step",
                        "from_step": "research",
                        "body_template": "{steps.research}",
                    },
                ],
            ),
        )


def test_validate_workflow_spec_rejects_output_without_source_step(tmp_path: Path) -> None:
    """Declared outputs should not disappear at runtime because from_step is missing."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="from_step"):
        store.validate_workflow(
            _workflow_spec(
                outputs=[
                    {
                        "id": "report",
                        "type": "text",
                    },
                ],
            ),
        )


def test_validate_workflow_spec_rejects_output_without_type(tmp_path: Path) -> None:
    """Declared outputs should include the documented output type."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="field 'type' is missing"):
        store.validate_workflow(
            _workflow_spec(
                outputs=[
                    {
                        "id": "report",
                        "from_step": "write",
                    },
                ],
            ),
        )


def test_validate_workflow_spec_rejects_unsupported_output_type(tmp_path: Path) -> None:
    """Output type docs and validation should stay aligned."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="unsupported type"):
        store.validate_workflow(
            _workflow_spec(
                outputs=[
                    {
                        "id": "report",
                        "type": "pdf_report",
                        "from_step": "write",
                    },
                ],
            ),
        )


def test_run_workflow_executes_steps_and_persists_outputs(tmp_path: Path) -> None:
    """Running a workflow should execute declared steps and persist their outputs."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(
            workflow=[
                {
                    "id": "research",
                    "type": "transform_step",
                    "template": "Research brief for {input.topic}: sources checked.",
                },
                {
                    "id": "write",
                    "type": "report_step",
                    "title": "Report for {input.topic}",
                    "body_template": "{steps.research}",
                },
            ],
            outputs=[
                {"id": "brief", "type": "text", "from_step": "research"},
                {"id": "report_html", "type": "html_report", "from_step": "write"},
            ],
        ),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    step_outputs_path = tmp_path / "mindroom_data" / loaded.artifacts["step_outputs"]
    step_outputs = json.loads(step_outputs_path.read_text(encoding="utf-8"))
    report_html = (tmp_path / "mindroom_data" / loaded.artifacts["report_html"]).read_text(encoding="utf-8")
    assert loaded.status == "completed"
    assert loaded.outputs["brief"] == "Research brief for Agno factories: sources checked."
    assert loaded.steps[0]["id"] == "research"
    assert loaded.steps[0]["status"] == "completed"
    assert step_outputs["research"]["content"] == "Research brief for Agno factories: sources checked."
    assert "Report for Agno factories" in report_html
    assert "Research brief for Agno factories: sources checked." in report_html


def test_agent_step_uses_participant_executor_instead_of_prompt_template() -> None:
    """Agent steps should invoke the resolved participant instead of echoing the prompt."""

    def participant_executor(
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> str:
        assert participant["id"] == "writer"
        assert prompt == "Write about Agno factories."
        assert input_data == {"topic": "Agno factories"}
        assert step_outputs == {}
        return "Executed by Report Writer."

    execution = execute_workflow_spec(
        _workflow_spec(
            workflow=[
                {
                    "id": "write",
                    "type": "agent_step",
                    "participant": "writer",
                    "prompt": "Write about {input.topic}.",
                },
            ],
            outputs=[{"id": "report", "type": "text", "from_step": "write"}],
        ),
        {"topic": "Agno factories"},
        participant_executor=participant_executor,
    )

    assert execution.status == "completed"
    assert execution.outputs["report"] == "Executed by Report Writer."


def test_agent_step_fails_without_participant_executor() -> None:
    """Agent steps should not silently degrade into template-only execution."""
    execution = execute_workflow_spec(
        _workflow_spec(
            workflow=[
                {
                    "id": "write",
                    "type": "agent_step",
                    "participant": "writer",
                    "prompt": "Write about {input.topic}.",
                },
            ],
        ),
        {"topic": "Agno factories"},
    )

    assert execution.status == "failed"
    assert execution.error == "Agent step 'write' requires a participant executor."


def test_service_completes_tool_runs_without_raw_background_thread(tmp_path: Path) -> None:
    """Tool-triggered workflow runs should complete on the managed execution path."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(
            workflow=[
                {
                    "id": "research",
                    "type": "transform_step",
                    "template": "Research brief for {input.topic}.",
                },
            ],
            outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
        ),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )
    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )

    assert run.status == "completed"
    assert loaded.status == "completed"
    assert loaded.outputs["brief"] == "Research brief for Agno factories."
    assert run.report_url is not None
    assert run.report_url.startswith(
        f"https://acme.mindroom.chat/reports/private/agent/general/competitor-research-report/{run.run_id}",
    )
    assert "access_token=" not in run.report_url


def test_service_sync_run_executes_inline_without_detached_timeout_thread(tmp_path: Path) -> None:
    """Sync service runs should not leave detached participant work behind."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(
        store,
        participant_executor=lambda **_kwargs: "inline",
    )
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    assert run.status == "completed"
    assert run.outputs["report_html"] == "inline"


def test_service_sync_run_enforces_runtime_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sync service runs should fail instead of completing work that exceeds the runtime cap."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    def slow_executor(**_kwargs: object) -> object:
        time.sleep(1)
        return "late"

    service = DynamicWorkflowService(store, participant_executor=slow_executor)
    monkeypatch.setattr("mindroom.dynamic_workflows.service.workflow_runtime_seconds", lambda _spec: 0.01)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    assert run.status == "failed"
    assert "max_runtime_seconds" in str(run.error)


@pytest.mark.asyncio
async def test_service_async_run_enforces_runtime_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Async service runs should cancel participant work when the runtime cap is exceeded."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    participant_cancelled = asyncio.Event()

    async def slow_executor(**_kwargs: object) -> object:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            participant_cancelled.set()
            raise
        return "late"

    service = DynamicWorkflowService(store, async_participant_executor=slow_executor)
    monkeypatch.setattr("mindroom.dynamic_workflows.service.workflow_runtime_seconds", lambda _spec: 0.01)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.arun_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    assert run.status == "failed"
    assert "max_runtime_seconds" in str(run.error)
    await asyncio.wait_for(participant_cancelled.wait(), timeout=1)


@pytest.mark.asyncio
async def test_service_async_run_fails_at_deadline_when_cancellation_is_suppressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async runtime caps should fail the run even if participant code swallows cancellation."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    late_participant_finished = asyncio.Event()

    async def stubborn_executor(**_kwargs: object) -> object:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            await asyncio.sleep(0.02)
            late_participant_finished.set()
            return "late"
        return "late"

    service = DynamicWorkflowService(store, async_participant_executor=stubborn_executor)
    monkeypatch.setattr("mindroom.dynamic_workflows.service.workflow_runtime_seconds", lambda _spec: 0.01)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.arun_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )
    await asyncio.wait_for(late_participant_finished.wait(), timeout=1)
    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )

    assert run.status == "failed"
    assert loaded.status == "failed"
    assert "max_runtime_seconds" in str(loaded.error)


@pytest.mark.asyncio
async def test_service_async_run_persists_cancelled_status(tmp_path: Path) -> None:
    """Cancelling an async workflow run should not leave the run stuck as running."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    async def cancelled_executor(**_kwargs: object) -> object:
        raise asyncio.CancelledError

    service = DynamicWorkflowService(store, async_participant_executor=cancelled_executor)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    with pytest.raises(asyncio.CancelledError):
        await service.arun_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            input_data={"topic": "Agno factories"},
            requested_by="general",
            base_url="https://acme.mindroom.chat",
        )

    run_files = list(
        (tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report/runs").glob("*.json"),
    )
    assert len(run_files) == 1
    run_data = json.loads(run_files[0].read_text(encoding="utf-8"))
    assert run_data["status"] == "failed"
    assert run_data["error"] == "Workflow run was cancelled."


def test_validate_workflow_spec_rejects_missing_step_id(tmp_path: Path) -> None:
    """Workflow specs should reject malformed step entries before they are persisted."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="Workflow step at index 0 field 'id' is missing"):
        store.validate_workflow(
            _workflow_spec(
                workflow=[
                    {
                        "type": "transform_step",
                        "template": "Research brief for {input.topic}.",
                    },
                ],
            ),
        )


def test_validate_workflow_spec_rejects_ambiguous_agent_step_template(tmp_path: Path) -> None:
    """Validation and execution should not disagree about which agent-step template wins."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="only one template field"):
        store.validate_workflow(
            _workflow_spec(
                workflow=[
                    {
                        "id": "write",
                        "type": "agent_step",
                        "participant": "writer",
                        "response_template": "Safe template.",
                        "prompt": "{steps.future}",
                    },
                ],
            ),
        )


def test_validate_workflow_spec_rejects_unsupported_participant_kind(tmp_path: Path) -> None:
    """Participant kind errors should fail at create/update time."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="unsupported kind"):
        store.validate_workflow(
            _workflow_spec(
                participants=[
                    {
                        "id": "writer",
                        "kind": "team_agent",
                    },
                ],
            ),
        )


def test_get_workflow_run_rejects_traversal_run_id(tmp_path: Path) -> None:
    """Run lookup should reject path traversal before building the run filename."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    with pytest.raises(DynamicWorkflowError, match="run_id must match"):
        store.get_workflow_run(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            run_id="../run_secret",
        )


def test_load_workflow_revision_rejects_traversal_revision(tmp_path: Path) -> None:
    """Revision lookup should reject path traversal before building the revision filename."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    with pytest.raises(DynamicWorkflowError, match="revision must match"):
        store.load_workflow_revision(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            revision="../workflow",
        )


def test_get_workflow_run_wraps_json_decoder_errors(tmp_path: Path) -> None:
    """Corrupt run JSON should return a Dynamic Workflow storage error."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    run_path = (
        tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report/runs/run_corrupt.json"
    )
    run_path.parent.mkdir(parents=True, exist_ok=True)
    run_path.write_text("{", encoding="utf-8")

    with pytest.raises(DynamicWorkflowError, match="Failed to parse JSON mapping") as exc_info:
        store.get_workflow_run(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            run_id="run_corrupt",
        )
    assert str(tmp_path) not in str(exc_info.value)


def test_run_workflow_records_failed_run_when_stored_step_reference_is_missing(tmp_path: Path) -> None:
    """Failed workflow execution should still persist a run record and error report."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    revision_path = (
        tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report/revisions/000001.yaml"
    )
    revision = yaml.safe_load(revision_path.read_text(encoding="utf-8"))
    revision["workflow"] = [
        {
            "id": "write",
            "type": "report_step",
            "body_template": "{steps.missing}",
        },
    ]
    revision_path.write_text(yaml.safe_dump(revision, sort_keys=False), encoding="utf-8")

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    report_html = (tmp_path / "mindroom_data" / loaded.artifacts["report_html"]).read_text(encoding="utf-8")
    assert loaded.status == "failed"
    assert loaded.error == "Workflow step at index 0 field 'body_template' references unknown prior step 'missing'."
    assert loaded.steps == []
    assert "unknown prior step" in report_html


def test_run_workflow_records_failed_run_when_active_revision_is_missing(tmp_path: Path) -> None:
    """Revision load failures after run creation should not leave run records stuck as running."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    revision_path = (
        tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report/revisions/000001.yaml"
    )
    revision_path.unlink()

    run = service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert loaded.status == "failed"
    assert loaded.error == "YAML mapping was not found."
    assert loaded.artifacts["report_html"].endswith("/report.html")


def test_declarative_spec_compiles_to_agno_workflow_factory(tmp_path: Path) -> None:
    """Dynamic Workflow specs should compile to real Agno WorkflowFactory objects."""
    factory = build_agno_workflow_factory(
        _workflow_spec(),
        db_file=tmp_path / "dynamic-workflow-agno.db",
    )

    workflow = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno factories"}), Workflow)

    assert isinstance(factory, WorkflowFactory)
    assert factory.id == "competitor-research-report"
    assert workflow.id == "competitor-research-report"
    assert workflow.name == "Competitor Research Report"
    assert workflow.metadata == {
        "mindroom_dynamic_workflow": True,
        "workflow_id": "competitor-research-report",
    }


def test_agno_workflow_factory_step_executor_renders_declared_output(tmp_path: Path) -> None:
    """Agno factory steps should execute declared Dynamic Workflow step behavior."""
    factory = build_agno_workflow_factory(
        _workflow_spec(
            workflow=[
                {
                    "id": "research",
                    "type": "transform_step",
                    "template": "Research brief for {input.topic}.",
                },
            ],
            outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
        ),
        db_file=tmp_path / "dynamic-workflow-agno.db",
    )
    workflow = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno factories"}), Workflow)

    output = workflow.steps[0].execute(StepInput(input={"topic": "Agno factories"}))

    assert isinstance(output, StepOutput)
    assert output.success is True
    assert output.content == "Research brief for Agno factories."


def test_agno_workflow_factory_step_executor_runs_participant(tmp_path: Path) -> None:
    """Agno factory agent steps should use the supplied participant executor."""

    def participant_executor(
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> str:
        assert participant["id"] == "writer"
        assert prompt == "Write about Agno factories."
        assert input_data == {"topic": "Agno factories"}
        assert step_outputs == {}
        return "Executed by Agno factory participant."

    factory = build_agno_workflow_factory(
        _workflow_spec(
            workflow=[
                {
                    "id": "write",
                    "type": "agent_step",
                    "participant": "writer",
                    "prompt": "Write about {input.topic}.",
                },
            ],
            outputs=[{"id": "report", "type": "text", "from_step": "write"}],
        ),
        db_file=tmp_path / "dynamic-workflow-agno.db",
        participant_executor=participant_executor,
    )
    workflow = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno factories"}), Workflow)

    output = workflow.steps[0].execute(StepInput(input={"topic": "Agno factories"}))

    assert isinstance(output, StepOutput)
    assert output.success is True
    assert output.content == "Executed by Agno factory participant."


def test_agno_workflow_run_fails_and_stops_when_step_execution_fails(tmp_path: Path) -> None:
    """Agno workflow runs should not continue after a Dynamic Workflow step failure."""
    prompts: list[str] = []

    def participant_executor(
        *,
        participant: dict[str, object],
        prompt: str,
        input_data: dict[str, object],
        step_outputs: dict[str, object],
    ) -> str:
        del participant, input_data, step_outputs
        prompts.append(prompt)
        msg = "provider auth failed"
        raise DynamicWorkflowExecutionError(msg)

    factory = build_agno_workflow_factory(
        _workflow_spec(
            workflow=[
                {
                    "id": "write",
                    "type": "agent_step",
                    "participant": "writer",
                    "prompt": "Write about {input.topic}.",
                },
                {
                    "id": "after",
                    "type": "transform_step",
                    "template": "Should not run for {input.topic}.",
                },
            ],
            outputs=[{"id": "result", "type": "text", "from_step": "after"}],
        ),
        db_file=tmp_path / "dynamic-workflow-agno.db",
        participant_executor=participant_executor,
    )
    workflow = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno factories"}), Workflow)

    with pytest.raises(DynamicWorkflowExecutionError, match="provider auth failed"):
        workflow.run(input={"topic": "Agno factories"}, user_id="@user:localhost")

    assert prompts == ["Write about Agno factories."]


def test_dynamic_workflow_tool_uses_runtime_context(tmp_path: Path) -> None:
    """Runtime-aware tool should scope workflows to current agent and storage root."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    transform_spec = _workflow_spec(
        workflow=[
            {
                "id": "research",
                "type": "transform_step",
                "template": "Research brief for {input.topic}.",
            },
        ],
        outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
    )

    with tool_runtime_context(context):
        created = _tool_payload(tool.create_workflow(transform_spec, reason="initial design"))
        listed = _tool_payload(tool.list_workflows())
        run = _tool_payload(
            tool.run_workflow(
                workflow_id="competitor-research-report",
                input={"topic": "Agno factories"},
            ),
        )

    assert created["status"] == "ok"
    assert created["workflow_id"] == "competitor-research-report"
    assert listed["workflows"][0]["workflow_id"] == "competitor-research-report"
    assert run["status"] == "completed"
    assert run["outputs"]["brief"] == "Research brief for Agno factories."
    assert run["report_url"].startswith(
        "https://acme.mindroom.chat/reports/private/agent/general/competitor-research-report/run_",
    )


def test_dynamic_workflow_tool_denies_run_read_for_different_requester(tmp_path: Path) -> None:
    """Agent-scoped run details should not leak across Matrix requesters."""
    tool = DynamicWorkflowTools()
    alice_context = _make_context(tmp_path)
    bob_context = replace(alice_context, requester_id="@bob:localhost")
    transform_spec = _workflow_spec(
        workflow=[
            {
                "id": "research",
                "type": "transform_step",
                "template": "Research brief for {input.topic}.",
            },
        ],
        outputs=[{"id": "brief", "type": "text", "from_step": "research"}],
    )

    with tool_runtime_context(alice_context):
        _tool_payload(tool.create_workflow(transform_spec, reason="initial design"))
        run = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}))
    with tool_runtime_context(bob_context):
        read_result = _tool_payload(tool.get_workflow_run("competitor-research-report", run["run_id"]))

    assert read_result["status"] == "error"
    assert "not available to the current requester" in read_result["message"]


def test_dynamic_workflow_tool_json_schemas_allow_arbitrary_json_values() -> None:
    """Tool-call schemas should accept scalar, array, and object values inside JSON payload fields."""
    tool = DynamicWorkflowTools()
    async_functions = tool.get_async_functions()

    spec_schema = async_functions["create_workflow"].parameters["properties"]["spec"]
    patch_schema = async_functions["update_workflow"].parameters["properties"]["patch"]
    input_schema = async_functions["run_workflow"].parameters["properties"]["input"]

    for schema in (spec_schema, patch_schema, input_schema):
        value_schema = schema["additionalProperties"]
        allowed_types = {entry["type"] for entry in value_schema["anyOf"]}
        assert {"object", "array", "string", "boolean", "null"} <= allowed_types
        assert allowed_types & {"number", "integer"}


def test_dynamic_workflow_tool_scopes_private_agent_workflows_by_requester(tmp_path: Path) -> None:
    """Private agents should not share agent-scoped workflows across requesters."""
    tool = DynamicWorkflowTools()
    alice_context = _make_private_context(tmp_path, requester_id="@alice:localhost")
    bob_context = _make_private_context(tmp_path, requester_id="@bob:localhost")

    with tool_runtime_context(alice_context):
        created = _tool_payload(tool.create_workflow(_workflow_spec(), reason="initial design"))
        alice_listed = _tool_payload(tool.list_workflows())
    with tool_runtime_context(bob_context):
        bob_listed = _tool_payload(tool.list_workflows())

    assert created["status"] == "ok"
    assert created["owner_id"].startswith("private_")
    assert alice_listed["workflows"][0]["workflow_id"] == "competitor-research-report"
    assert bob_listed["workflows"] == []


def test_dynamic_workflow_tool_rejects_ephemeral_model_outside_caller_policy(tmp_path: Path) -> None:
    """Ephemeral participants should not escalate to arbitrary configured models."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], model="default")},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "opus": ModelConfig(provider="anthropic", id="claude-opus-4-8"),
            },
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config), active_model_name="default")

    with tool_runtime_context(context):
        result = _tool_payload(
            tool.validate_workflow(
                _workflow_spec(
                    participants=[
                        {
                            "id": "writer",
                            "kind": "ephemeral_agent",
                            "name": "Report Writer",
                            "model": "opus",
                            "tools": [],
                        },
                    ],
                    permissions={"models": ["claude-opus-4-8"], "tools": []},
                ),
            ),
        )

    assert result["status"] == "error"
    assert "not allowed for agent 'general'" in result["message"]


def test_dynamic_workflow_tool_enforces_permission_models_for_default_participant_model(tmp_path: Path) -> None:
    """Omitted participant models should still be checked against workflow permissions.models."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], model="default")},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "opus": ModelConfig(provider="anthropic", id="claude-opus-4-8"),
            },
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config), active_model_name="default")

    with tool_runtime_context(context):
        result = _tool_payload(
            tool.validate_workflow(
                _workflow_spec(
                    participants=[
                        {
                            "id": "writer",
                            "kind": "ephemeral_agent",
                            "name": "Report Writer",
                            "tools": [],
                        },
                    ],
                    permissions={"models": ["claude-opus-4-8"], "tools": []},
                ),
            ),
        )

    assert result["status"] == "error"
    assert "permissions.models" in result["message"]


def test_dynamic_workflow_tool_defaults_ephemeral_model_to_caller_runtime_model(tmp_path: Path) -> None:
    """Omitted participant models should not fall back to the global default model."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], model="opus")},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "opus": ModelConfig(provider="anthropic", id="claude-opus-4-8"),
            },
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config), active_model_name=None)

    with tool_runtime_context(context):
        result = _tool_payload(
            tool.validate_workflow(
                _workflow_spec(
                    participants=[
                        {
                            "id": "writer",
                            "kind": "ephemeral_agent",
                            "name": "Report Writer",
                            "tools": [],
                        },
                    ],
                    permissions={"models": ["claude-sonnet-4-6"], "tools": []},
                ),
            ),
        )

    assert result["status"] == "error"
    assert "permissions.models" in result["message"]


def test_dynamic_workflow_tool_rejects_unknown_room_agent_during_validation(tmp_path: Path) -> None:
    """Room-agent participants should fail before an invalid workflow is saved."""
    tool = DynamicWorkflowTools()
    context = _make_multi_agent_context(tmp_path, room_agents=["general"])
    spec = _workflow_spec(
        participants=[
            {
                "id": "writer",
                "kind": "room_agent",
                "agent": "missing",
            },
        ],
    )

    with tool_runtime_context(context):
        validated = _tool_payload(tool.validate_workflow(spec))
        created = _tool_payload(tool.create_workflow(spec, reason="initial design"))

    assert validated["status"] == "error"
    assert "unknown room agent 'missing'" in validated["message"]
    assert created["status"] == "error"
    assert "unknown room agent 'missing'" in created["message"]


def test_dynamic_workflow_tool_rejects_unavailable_room_agent_during_validation(tmp_path: Path) -> None:
    """Room-agent participants should match the requester-visible agents in the current room."""
    tool = DynamicWorkflowTools()
    context = _make_multi_agent_context(tmp_path, room_agents=["general"])
    spec = _workflow_spec(
        participants=[
            {
                "id": "writer",
                "kind": "room_agent",
                "agent": "specialist",
            },
        ],
    )

    with tool_runtime_context(context):
        result = _tool_payload(tool.validate_workflow(spec))

    assert result["status"] == "error"
    assert "not available to this requester in this room" in result["message"]


def test_dynamic_workflow_tool_uses_cached_client_room_when_context_room_is_missing(tmp_path: Path) -> None:
    """Room-agent validation should use the client's cached current room before falling back to an empty room."""
    tool = DynamicWorkflowTools()
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    client = AsyncMock()
    client.rooms = {context.room_id: context.room}
    context = replace(context, client=client, room=None)
    spec = _workflow_spec(
        participants=[
            {
                "id": "writer",
                "kind": "room_agent",
                "agent": "specialist",
            },
        ],
    )

    with tool_runtime_context(context):
        result = _tool_payload(tool.validate_workflow(spec))

    assert result["status"] == "ok"


def test_dynamic_workflow_tool_revalidates_saved_revision_policy_before_run(tmp_path: Path) -> None:
    """Saved revisions should not bypass the caller's current active model policy."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], model="default")},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "opus": ModelConfig(provider="anthropic", id="claude-opus-4-8"),
            },
        ),
        context.runtime_paths,
    )
    create_context = replace(
        context,
        config=config,
        runtime_paths=runtime_paths_for(config),
        active_model_name="default",
    )
    run_context = replace(create_context, active_model_name="opus")

    with tool_runtime_context(create_context):
        created = _tool_payload(tool.create_workflow(_workflow_spec(), reason="initial design"))
    with tool_runtime_context(run_context):
        run = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}))

    assert created["status"] == "ok"
    assert run["status"] == "failed"
    assert "not allowed for agent 'general'" in str(run["error"])


def test_dynamic_workflow_tool_returns_payload_for_invalid_scope(tmp_path: Path) -> None:
    """Tool calls should return JSON payload errors instead of raising runtime exceptions."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        result = _tool_payload(tool.list_workflows(scope="global"))

    assert result["status"] == "error"
    assert "Unsupported Dynamic Workflow scope" in result["message"]


def test_dynamic_workflow_tool_returns_payload_when_agent_name_is_missing(tmp_path: Path) -> None:
    """Runtime-aware tool should fail cleanly when required context owner data is missing."""
    tool = DynamicWorkflowTools()
    context = replace(_make_context(tmp_path), agent_name="")

    with tool_runtime_context(context):
        result = _tool_payload(tool.list_workflows())

    assert result["status"] == "error"
    assert "Agent name is missing" in result["message"]


def test_dynamic_workflow_tool_denies_shared_scopes_without_policy(tmp_path: Path) -> None:
    """Agent tools should not mutate room or tenant workflow scopes without an approval policy."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        room_result = _tool_payload(tool.create_workflow(_workflow_spec(), scope="room"))
        tenant_result = _tool_payload(tool.create_workflow(_workflow_spec(), scope="tenant"))

    assert room_result["status"] == "error"
    assert "scope requires Dynamic Workflow approval policy" in room_result["message"]
    assert tenant_result["status"] == "error"
    assert "scope requires Dynamic Workflow approval policy" in tenant_result["message"]


def test_room_agent_participant_must_be_available_to_requester_in_room(tmp_path: Path) -> None:
    """Room-agent participants should not bypass normal room responder eligibility."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general"])

    with pytest.raises(DynamicWorkflowError, match="not available to this requester in this room"):
        dynamic_workflow_module._execute_room_agent_participant(
            context,
            {"id": "specialist", "kind": "room_agent", "agent": "specialist"},
            "Write a report.",
        )


def test_room_agent_participant_rejects_model_override(tmp_path: Path) -> None:
    """Room-agent participants should run with their configured model only."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])

    with pytest.raises(DynamicWorkflowError, match="configured model"):
        dynamic_workflow_module._execute_room_agent_participant(
            context,
            {"id": "specialist", "kind": "room_agent", "agent": "specialist", "model": "default"},
            "Write a report.",
        )


def test_room_agent_participant_rebinds_context_and_uses_isolated_state(tmp_path: Path) -> None:
    """Room-agent participants should execute as that agent without durable workflow side effects."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"]),
                "specialist": AgentConfig(
                    display_name="Specialist Agent",
                    model="default",
                    tools=["memory"],
                    knowledge_bases=["reference"],
                ),
            },
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6"),
                "large": ModelConfig(provider="anthropic", id="claude-opus-4-8"),
            },
            room_models={"lobby": "large"},
            knowledge_bases={"reference": {"path": str(tmp_path / "knowledge")}},
        ),
        context.runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    (runtime_paths.storage_root / "matrix_state.yaml").write_text(
        yaml.safe_dump(
            {"rooms": {"lobby": {"room_id": "!room:localhost", "alias": "#lobby:localhost", "name": "Lobby"}}},
        ),
        encoding="utf-8",
    )
    persist_entity_accounts(config, runtime_paths)
    context = replace(context, config=config, runtime_paths=runtime_paths)
    parent_loop = asyncio.new_event_loop()

    def assert_run(prompt: str, *, user_id: str, session_id: str) -> None:
        runtime_context = get_tool_runtime_context()
        assert runtime_context is not None
        assert asyncio.get_running_loop() is parent_loop
        assert runtime_context.agent_name == "specialist"
        assert runtime_context.session_id == session_id
        assert runtime_context.active_model_name == "large"
        assert "competitor-research-report:run_1:writer_a" in session_id
        assert prompt == "Write a report."
        assert user_id == "@user:localhost"

    fake_agent = _fake_stream_agent(content="done", on_run=assert_run)
    with patch("mindroom.agents.create_agent", return_value=fake_agent) as create_agent_mock:
        asyncio.set_event_loop(parent_loop)
        try:
            result = parent_loop.run_until_complete(
                dynamic_workflow_module._aexecute_room_agent_participant(
                    context,
                    {"id": "writer_a", "kind": "room_agent", "agent": "specialist"},
                    "Write a report.",
                    run_scope="competitor-research-report:run_1",
                ),
            )
        finally:
            asyncio.set_event_loop(None)
            parent_loop.close()

    assert result == "done"
    create_kwargs = create_agent_mock.call_args.kwargs
    assert create_kwargs["session_id"].endswith(":dynamic_workflow:competitor-research-report:run_1:writer_a")
    assert create_kwargs["active_model_name"] == "large"
    assert create_kwargs["knowledge"] is None
    assert create_kwargs["persist_runtime_state"] is False
    assert create_kwargs["disable_runtime_capabilities"] is True
    assert create_kwargs["execution_identity"].agent_name == "specialist"
    assert create_kwargs["execution_identity"].session_id == create_kwargs["session_id"]


def test_resolve_participant_toolkits_rejects_unavailable_tools(tmp_path: Path) -> None:
    """Executor-level grant resolution must re-reject bad names for store-bypassing callers."""
    context = _make_context(tmp_path)

    with pytest.raises(DynamicWorkflowError, match="not a registered tool"):
        dynamic_workflow_module._resolve_participant_toolkits(context, {"id": "writer", "tools": ["not_a_real_tool"]})

    with pytest.raises(DynamicWorkflowError, match="agent-infrastructure"):
        dynamic_workflow_module._resolve_participant_toolkits(context, {"id": "writer", "tools": ["memory"]})

    with pytest.raises(DynamicWorkflowError, match="list of non-empty strings"):
        dynamic_workflow_module._resolve_participant_toolkits(context, {"id": "writer", "tools": "duckduckgo"})

    # Falsy non-list values (e.g. "") are malformed, not "no tools" — they must raise, not degrade silently.
    with pytest.raises(DynamicWorkflowError, match="list of non-empty strings"):
        dynamic_workflow_module._resolve_participant_toolkits(context, {"id": "writer", "tools": ""})


def test_resolve_participant_toolkits_returns_empty_for_missing_grants(tmp_path: Path) -> None:
    """Participants without grants (missing, null, or empty tools) resolve to no toolkits."""
    context = _make_context(tmp_path)

    for participant in ({"id": "writer"}, {"id": "writer", "tools": None}, {"id": "writer", "tools": []}):
        assert dynamic_workflow_module._resolve_participant_toolkits(context, participant) == {}


def test_resolve_participant_toolkits_builds_real_instances_with_caller_routing(tmp_path: Path) -> None:
    """Granted tools should resolve through the agent toolkit builder keyed by registry name."""
    context = _make_context(tmp_path)

    toolkits = dynamic_workflow_module._resolve_participant_toolkits(context, {"id": "writer", "tools": ["website"]})

    assert list(toolkits) == ["website"]
    assert "read_url" in toolkits["website"].functions


def test_participant_run_config_requires_approval_for_granted_tools(tmp_path: Path) -> None:
    """Without pre-approval config, granted tool calls must default to require_approval."""
    context = _make_context(tmp_path)
    toolkit = Toolkit(name="fake_shell")
    toolkit.functions["run_shell_command"] = SimpleNamespace(name="run_shell_command")

    run_config = dynamic_workflow_module._participant_run_config(context, {"shell": toolkit})

    assert run_config.tool_approval.default == "require_approval"
    assert run_config.tool_approval.rules == []
    assert context.config.tool_approval.default == "auto_approve"


def test_participant_run_config_pre_approves_allowed_tools(tmp_path: Path) -> None:
    """Tools listed in the dynamic_workflow allowed_tools config skip per-call approval."""
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=[{"dynamic_workflow": {"allowed_tools": ["website"]}}],
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config))
    website = Toolkit(name="fake_website")
    website.functions["read_url"] = SimpleNamespace(name="read_url")
    shell = Toolkit(name="fake_shell")
    shell.functions["run_shell_command"] = SimpleNamespace(name="run_shell_command")

    run_config = dynamic_workflow_module._participant_run_config(context, {"website": website, "shell": shell})

    assert run_config.tool_approval.default == "require_approval"
    assert [(rule.match, rule.action) for rule in run_config.tool_approval.rules] == [("read_url", "auto_approve")]


def test_participant_run_config_wildcard_pre_approves_all_granted_tools(tmp_path: Path) -> None:
    """allowed_tools ["*"] pre-approves every granted tool's functions."""
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=[{"dynamic_workflow": {"allowed_tools": ["*"]}}],
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config))
    shell = Toolkit(name="fake_shell")
    shell.functions["run_shell_command"] = SimpleNamespace(name="run_shell_command")

    run_config = dynamic_workflow_module._participant_run_config(context, {"shell": shell})

    assert run_config.tool_approval.default == "require_approval"
    assert [(rule.match, rule.action) for rule in run_config.tool_approval.rules] == [
        ("run_shell_command", "auto_approve"),
    ]


def test_participant_run_config_does_not_pre_approve_colliding_function_names(tmp_path: Path) -> None:
    """A function name shared with a non-pre-approved toolkit must not be auto-approved."""
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=[{"dynamic_workflow": {"allowed_tools": ["python"]}}],
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config))
    python = Toolkit(name="fake_python")
    python.functions["read_file"] = SimpleNamespace(name="read_file")
    python.functions["run_python_code"] = SimpleNamespace(name="run_python_code")
    file = Toolkit(name="fake_file")
    file.functions["read_file"] = SimpleNamespace(name="read_file")

    run_config = dynamic_workflow_module._participant_run_config(context, {"python": python, "file": file})

    rules = {rule.match: rule.action for rule in run_config.tool_approval.rules}
    # run_python_code is unique to the pre-approved python toolkit -> auto-approved.
    assert rules == {"run_python_code": "auto_approve"}
    # read_file collides with the non-pre-approved file toolkit, so it must still require approval.
    assert "read_file" not in rules


def test_participant_run_config_preserves_operator_rule_precedence(tmp_path: Path) -> None:
    """An operator require_approval rule must win over workflow pre-approval (first-match)."""
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=[{"dynamic_workflow": {"allowed_tools": ["*"]}}],
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
            tool_approval={"rules": [{"match": "run_shell_command", "action": "require_approval"}]},
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config))
    shell = Toolkit(name="fake_shell")
    shell.functions["run_shell_command"] = SimpleNamespace(name="run_shell_command")

    run_config = dynamic_workflow_module._participant_run_config(context, {"shell": shell})

    ordered = [(rule.match, rule.action) for rule in run_config.tool_approval.rules]
    assert ordered[0] == ("run_shell_command", "require_approval")
    assert ordered[1] == ("run_shell_command", "auto_approve")
    matched = _matching_tool_approval_rule(run_config, "run_shell_command")
    assert matched is not None
    assert matched.action == "require_approval"


def test_ephemeral_participant_runs_with_granted_toolkits(tmp_path: Path) -> None:
    """run_workflow should hand granted toolkit instances to the participant with caller routing parity."""
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["dynamic_workflow", {"website": {"knowledge": "kb"}}, "shell"],
                    worker_tools=["shell"],
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-4-6")},
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config))
    tool = DynamicWorkflowTools()
    spec = _workflow_spec(
        participants=[
            {
                "id": "writer",
                "kind": "ephemeral_agent",
                "name": "Report Writer",
                "model": "claude-sonnet-4-6",
                "tools": ["website", "shell"],
            },
        ],
        permissions={"models": ["claude-sonnet-4-6"], "tools": ["website", "shell"]},
    )
    sentinel_toolkits = {name: Toolkit(name=f"fake_{name}") for name in ("website", "shell")}

    def assert_run(prompt: str, *, user_id: str, session_id: str) -> None:
        assert "Write a cited report" in prompt
        assert user_id == "@user:localhost"
        assert session_id
        runtime_context = get_tool_runtime_context()
        assert runtime_context is not None
        assert runtime_context.config.tool_approval.default == "require_approval"

    agent_mock = Mock(return_value=_fake_stream_agent(content="researched", on_run=assert_run))
    with (
        tool_runtime_context(context),
        patch(
            "mindroom.agents.build_agent_toolkit",
            side_effect=lambda name, **_kwargs: sentinel_toolkits[name],
        ) as build_toolkit_mock,
        patch.object(dynamic_workflow_module.model_loading, "get_model_instance", return_value=SimpleNamespace()),
        patch.object(dynamic_workflow_module, "Agent", agent_mock),
    ):
        create_payload = _tool_payload(tool.create_workflow(spec))
        run_payload = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno"}))

    assert create_payload["status"] == "ok"
    assert run_payload["status"] == "completed"
    tools_kwarg = agent_mock.call_args.kwargs["tools"]
    assert tools_kwarg == [sentinel_toolkits["website"], sentinel_toolkits["shell"]]
    build_calls = {call.args[0]: call.kwargs for call in build_toolkit_mock.call_args_list}
    assert list(build_calls) == ["website", "shell"]
    # Caller parity: authored per-tool config, worker routing, and session reach the builder.
    assert build_calls["website"]["agent_name"] == "general"
    assert build_calls["website"]["execution_identity"] is not None
    assert build_calls["website"]["tool_config_overrides"] == {"knowledge": "kb"}
    assert build_calls["website"]["worker_tools"] == ["shell"]
    assert build_calls["website"]["session_id"] == context.session_id
    assert build_calls["shell"]["worker_tools"] == ["shell"]


def test_ephemeral_participant_without_grants_runs_with_empty_tools(tmp_path: Path) -> None:
    """Tool-less participants (empty or missing tools key) must keep running with tools=[]."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()
    spec = _workflow_spec(
        participants=[
            {"id": "writer", "kind": "ephemeral_agent", "model": "claude-sonnet-4-6", "tools": []},
            {"id": "editor", "kind": "ephemeral_agent", "model": "claude-sonnet-4-6"},
        ],
        workflow=[
            {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write."},
            {"id": "edit", "type": "agent_step", "participant": "editor", "prompt": "Edit."},
        ],
        outputs=[{"id": "report_html", "type": "html_report", "from_step": "edit"}],
    )

    def assert_run(_prompt: str, *, user_id: str, session_id: str) -> None:
        assert user_id == "@user:localhost"
        assert session_id

    agent_mock = Mock(return_value=_fake_stream_agent(content="done", on_run=assert_run))
    with (
        tool_runtime_context(context),
        patch("mindroom.agents.build_agent_toolkit") as build_toolkit_mock,
        patch.object(dynamic_workflow_module.model_loading, "get_model_instance", return_value=SimpleNamespace()),
        patch.object(dynamic_workflow_module, "Agent", agent_mock),
    ):
        create_payload = _tool_payload(tool.create_workflow(spec))
        run_payload = _tool_payload(tool.run_workflow("competitor-research-report", {"topic": "Agno"}))

    assert create_payload["status"] == "ok"
    assert run_payload["status"] == "completed"
    assert agent_mock.call_count == 2
    assert [call.kwargs["tools"] for call in agent_mock.call_args_list] == [[], []]
    build_toolkit_mock.assert_not_called()


def test_run_agent_raises_on_failed_agno_status(tmp_path: Path) -> None:
    """Participant failures from Agno should become failed workflow steps, not normal content."""
    context = _make_context(tmp_path)

    def assert_run(_prompt: str, *, user_id: str, session_id: str) -> None:
        assert user_id == "@user:localhost"
        assert session_id == context.session_id

    fake_agent = _fake_stream_agent(content="provider auth failed", status=RunStatus.error, on_run=assert_run)

    with pytest.raises(dynamic_workflow_module.DynamicWorkflowExecutionError, match="provider auth failed"):
        asyncio.run(dynamic_workflow_module._arun_agent(context, fake_agent, "Write."))
