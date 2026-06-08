"""Tests for Dynamic Workflow storage and tools."""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
import yaml
from agno.factory import RequestContext
from agno.run.agent import RunStatus
from agno.workflow import Workflow, WorkflowFactory
from agno.workflow.types import StepInput, StepOutput

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.custom_tools import dynamic_workflow as dynamic_workflow_module
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.dynamic_workflows.agno_adapter import build_agno_workflow_factory
from mindroom.dynamic_workflows.runner import execute_workflow_spec
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.store import DynamicWorkflowError, DynamicWorkflowStore
from mindroom.entity_resolution import entity_identity_registry
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


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
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = store.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
        participant_executor=lambda **_: "Report about Agno factories.",
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
    assert "access_token=" in run.report_url
    assert loaded.status == "completed"
    assert loaded.artifacts["report_html"].endswith("/report.html")
    report_path = tmp_path / "mindroom_data" / loaded.artifacts["report_html"]
    report_html = report_path.read_text(encoding="utf-8")
    assert "Competitor Research Report" in report_html
    assert "Agno factories" in report_html


def test_store_run_workflow_enforces_runtime_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Store-level sync runs should share the same runtime cap as service runs."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    monkeypatch.setattr("mindroom.dynamic_workflows.store.workflow_runtime_seconds", lambda _spec: 0.01)
    store.create_workflow(
        spec=_workflow_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = store.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories"},
        requested_by="general",
        participant_executor=lambda **_kwargs: time.sleep(1),
    )

    assert run.status == "failed"
    assert "max_runtime_seconds" in str(run.error)


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


def test_validate_workflow_spec_rejects_tool_permission_until_grants_exist(tmp_path: Path) -> None:
    """Workflow specs should not request ambient tool access before grants are modeled."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    with pytest.raises(DynamicWorkflowError, match="tools"):
        store.validate_workflow(_workflow_spec(permissions={"tools": ["shell"]}))


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


def test_run_workflow_executes_steps_and_persists_outputs(tmp_path: Path) -> None:
    """Running a workflow should execute declared steps and persist their outputs."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
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

    run = store.run_workflow(
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
    assert "access_token=" in run.report_url


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
    monkeypatch.setattr("mindroom.dynamic_workflows.store.workflow_runtime_seconds", lambda _spec: 0.01)
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
    assert participant_cancelled.is_set()


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

    with pytest.raises(DynamicWorkflowError, match="Failed to parse JSON mapping"):
        store.get_workflow_run(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            run_id="run_corrupt",
        )


def test_run_workflow_records_failed_run_when_stored_step_reference_is_missing(tmp_path: Path) -> None:
    """Failed workflow execution should still persist a run record and error report."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
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

    run = store.run_workflow(
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
        assert {entry["type"] for entry in value_schema["anyOf"]} >= {
            "object",
            "array",
            "string",
            "number",
            "integer",
            "boolean",
            "null",
        }


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

    async def fake_arun(prompt: str, *, user_id: str, session_id: str) -> SimpleNamespace:
        runtime_context = get_tool_runtime_context()
        assert runtime_context is not None
        assert asyncio.get_running_loop() is parent_loop
        assert runtime_context.agent_name == "specialist"
        assert runtime_context.session_id == session_id
        assert runtime_context.active_model_name == "large"
        assert "competitor-research-report:run_1:writer_a" in session_id
        assert prompt == "Write a report."
        assert user_id == "@user:localhost"
        return SimpleNamespace(content="done", status=RunStatus.completed)

    fake_agent = SimpleNamespace(arun=fake_arun)
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


def test_run_agent_raises_on_failed_agno_status(tmp_path: Path) -> None:
    """Participant failures from Agno should become failed workflow steps, not normal content."""
    context = _make_context(tmp_path)

    async def fake_arun(_prompt: str, *, user_id: str, session_id: str) -> SimpleNamespace:
        assert user_id == "@user:localhost"
        assert session_id == context.session_id
        return SimpleNamespace(content="provider auth failed", status=RunStatus.error)

    with pytest.raises(dynamic_workflow_module.DynamicWorkflowExecutionError, match="provider auth failed"):
        asyncio.run(dynamic_workflow_module._arun_agent(context, SimpleNamespace(arun=fake_arun), "Write."))
