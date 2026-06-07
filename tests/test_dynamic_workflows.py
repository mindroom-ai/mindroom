"""Tests for Dynamic Workflow storage and tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import yaml
from agno.factory import RequestContext
from agno.workflow import Workflow, WorkflowFactory
from agno.workflow.types import StepInput, StepOutput

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.dynamic_workflows.agno_adapter import build_agno_workflow_factory
from mindroom.dynamic_workflows.store import DynamicWorkflowStore
from mindroom.tool_system.metadata import TOOL_METADATA
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import bind_runtime_paths, make_event_cache_mock, runtime_paths_for, test_runtime_paths

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
                "matrix_history": "current_thread",
                "attachments": "current_thread",
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
        Config(agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])}),
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
    )

    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )
    assert run.status == "completed"
    assert run.revision == "000001"
    assert run.report_url == f"https://acme.mindroom.chat/reports/private/{run.run_id}"
    assert loaded.status == "completed"
    assert loaded.artifacts["report_html"].endswith("/report.html")
    report_path = tmp_path / "mindroom_data" / loaded.artifacts["report_html"]
    report_html = report_path.read_text(encoding="utf-8")
    assert "Competitor Research Report" in report_html
    assert "Agno factories" in report_html


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


def test_run_workflow_records_failed_run_when_step_reference_is_missing(tmp_path: Path) -> None:
    """Failed workflow execution should still persist a run record and error report."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_workflow_spec(
            workflow=[
                {
                    "id": "write",
                    "type": "report_step",
                    "body_template": "{steps.missing}",
                },
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
    report_html = (tmp_path / "mindroom_data" / loaded.artifacts["report_html"]).read_text(encoding="utf-8")
    assert loaded.status == "failed"
    assert loaded.error == "Unknown template reference 'steps.missing'."
    assert loaded.steps[0]["status"] == "failed"
    assert "Unknown template reference" in report_html


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
        ),
        db_file=tmp_path / "dynamic-workflow-agno.db",
    )
    workflow = factory.resolve(RequestContext(user_id="@user:localhost", input={"topic": "Agno factories"}), Workflow)

    output = workflow.steps[0].execute(StepInput(input={"topic": "Agno factories"}))

    assert isinstance(output, StepOutput)
    assert output.success is True
    assert output.content == "Research brief for Agno factories."


def test_dynamic_workflow_tool_uses_runtime_context(tmp_path: Path) -> None:
    """Runtime-aware tool should scope workflows to current agent and storage root."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        created = _tool_payload(tool.create_workflow(_workflow_spec(), reason="initial design"))
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
    assert run["report_url"].startswith("https://acme.mindroom.chat/reports/private/run_")


def test_dynamic_workflow_tool_returns_payload_for_invalid_scope(tmp_path: Path) -> None:
    """Tool calls should return JSON payload errors instead of raising runtime exceptions."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)

    with tool_runtime_context(context):
        result = _tool_payload(tool.list_workflows(scope="global"))

    assert result["status"] == "error"
    assert "Unsupported Dynamic Workflow scope" in result["message"]
