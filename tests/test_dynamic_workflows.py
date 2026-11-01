"""Tests for Dynamic Workflow storage and tools."""

from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
import yaml
from pydantic import ValidationError

import mindroom.tools  # noqa: F401
from mindroom import agents
from mindroom.api.credentials_target import RequestCredentialsTarget, save_credentials_for_target
from mindroom.approval.tool_approval import _matching_tool_approval_rule
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools import dynamic_workflow as dynamic_workflow_module
from mindroom.custom_tools.dynamic_workflow import _MINIMAL_SPEC_EXAMPLE, DynamicWorkflowTools, _WorkflowRun
from mindroom.dynamic_workflows import service as service_module
from mindroom.dynamic_workflows.runner import (
    DynamicWorkflowExecutionError,
    ParticipantOutput,
    execute_workflow_spec,
)
from mindroom.dynamic_workflows.service import DynamicWorkflowService
from mindroom.dynamic_workflows.spec import (
    DynamicWorkflowError,
    Permissions,
    RoomAgentParticipant,
    WorkflowSpec,
    parse_workflow_spec,
)
from mindroom.dynamic_workflows.store import DynamicWorkflowRun, DynamicWorkflowStore
from mindroom.entity_resolution import entity_identity_registry
from mindroom.llm.errors import ProviderError
from mindroom.llm.messages import system_text
from mindroom.matrix.state import MatrixState
from mindroom.message_target import MessageTarget
from mindroom.tool_system.authoring import toolkit_functions
from mindroom.tool_system.automation_approval import NEVER_PREAPPROVE_TOOLKITS, build_automation_approval_config
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.access_schema_support import with_responder_access
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    loop_runs_while_blocked,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.scripted_model import Await, ScriptedModel, answer
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.dynamic_workflows.runner import ParticipantExecutor
    from mindroom.dynamic_workflows.spec import Participant
    from tests.fakes.scripted_model import ScriptStep


def _room_agent_model(monkeypatch: pytest.MonkeyPatch, *turns: list[ScriptStep]) -> tuple[ScriptedModel, list[str]]:
    """Answer every agent the test builds with one scripted model, and record the model names they load."""
    model = ScriptedModel(list(turns))
    loaded: list[str] = []

    def load(_config: Config, _runtime_paths: object, model_name: str, **_kwargs: object) -> ScriptedModel:
        loaded.append(model_name)
        return model

    monkeypatch.setattr("mindroom.providers.registry.build_model_provider", load)
    return model, loaded


@dataclass
class LoopFreedom:
    """The wrapped file operations that ran, and the first one the event loop could not run other work beside."""

    called: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


@contextmanager
def file_work_waiting_for_the_loop(methods: dict[object, tuple[str, ...]]) -> Iterator[LoopFreedom]:
    """Make each named method wait, before its file work, until the event loop has run other work.

    The loop runs other work only while the method runs off it, so a method called on the loop thread records itself
    as blocked; the methods after it skip the check, so a loop that stays blocked costs at most one wait.
    """
    loop = asyncio.get_running_loop()
    freedom = LoopFreedom()

    def waiting(method: Callable[..., object], name: str) -> Callable[..., object]:
        def call(*args: object, **kwargs: object) -> object:
            freedom.called.append(name)
            if not freedom.blocked and not loop_runs_while_blocked(loop):
                freedom.blocked.append(name)
            return method(*args, **kwargs)

        return call

    with ExitStack() as stack:
        for owner, names in methods.items():
            for name in names:
                stack.enter_context(patch.object(owner, name, waiting(getattr(owner, name), name)))
        yield freedom


def _only_run_record(tmp_path: Path) -> dict[str, Any]:
    """Return the one run record the test workflow has."""
    [run_file] = (tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report/runs").glob(
        "*.json",
    )
    return json.loads(run_file.read_text(encoding="utf-8"))


def _answering(content: object) -> ParticipantExecutor:
    """Return a participant executor whose every participant answers ``content``."""

    async def execute(**_kwargs: object) -> ParticipantOutput:
        return ParticipantOutput(content)

    return execute


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
                "kind": "subagent",
                "system_prompt": "You write cited reports.",
                "model": "claude-sonnet-5",
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
            "models": ["claude-sonnet-5"],
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


def _parsed_spec(**overrides: object) -> WorkflowSpec:
    return parse_workflow_spec(_workflow_spec(**overrides))


_RESEARCH_BRIEF_STEPS: dict[str, object] = {
    "workflow": [{"id": "research", "type": "transform_step", "template": "Research brief for {input.topic}."}],
    "outputs": [{"id": "brief", "type": "text", "from_step": "research"}],
}


def _make_context(tmp_path: Path) -> ToolRuntimeContext:
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths = replace(
        runtime_paths,
        # The test's paths keep no control state root.
        control_state_root=None,
        process_env={**runtime_paths.process_env, "MINDROOM_PUBLIC_URL": "https://acme.mindroom.chat"},
    )
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"])},
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
        runtime_paths,
    )
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id="$event:localhost",
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=None,
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
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
        ),
        runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, runtime_paths)
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id=registry.current_id("general").full_id)
    for agent_name in room_agents:
        room.add_member(registry.current_id(agent_name).full_id, config.agents[agent_name].display_name, None)
    room.members_synced = True
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread:localhost",
            reply_to_event_id="$event:localhost",
        ),
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=room,
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
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5")},
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


def _context_with_allowed_tools(tmp_path: Path, allowed_tools: object, **config_fields: object) -> ToolRuntimeContext:
    """Return a context whose agent authors ``allowed_tools`` on its dynamic_workflow tool."""
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=[{"dynamic_workflow": {"allowed_tools": allowed_tools}}],
                ),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
            **config_fields,
        ),
        context.runtime_paths,
    )
    return replace(context, config=config, runtime_paths=runtime_paths_for(config))


def _save_dashboard_allowed_tools(context: ToolRuntimeContext, value: object, *, agent_name: str | None = None) -> None:
    """Save dynamic_workflow's ``allowed_tools`` from the dashboard, globally or with a shared-scope agent selected."""
    target = RequestCredentialsTarget(
        runtime_paths=context.runtime_paths,
        base_manager=get_runtime_credentials_manager(context.runtime_paths),
        worker_scope="shared" if agent_name else None,
        agent_name=agent_name,
        execution_identity=None,
        tools=builtin_tool_registry(),
    )
    save_credentials_for_target("dynamic_workflow", {"allowed_tools": value}, target)


def _run_config(context: ToolRuntimeContext, function_owners: dict[str, frozenset[str]]) -> Config:
    """Build the participant overlay with the allowed tools the caller's config and dashboard grant."""
    allowed = dynamic_workflow_module._workflow_allowed_tools(context)
    return dynamic_workflow_module._participant_run_config(
        context,
        function_owners,
        allowed=allowed,
        reads_skills=False,
    )


def test_dynamic_workflow_tool_registered() -> None:
    """Dynamic Workflow tool metadata should be visible to config and dashboard surfaces."""
    metadata = builtin_tool_registry().entries["dynamic_workflow"]

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
        spec=_parsed_spec(),
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
        spec=_parsed_spec(description="Original description."),
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
        spec=_parsed_spec(description="Original description."),
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
        spec=_parsed_spec(),
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


@pytest.mark.asyncio
async def test_run_workflow_writes_run_record_and_private_html_report(tmp_path: Path) -> None:
    """Running a workflow should pin the active revision and write a private report artifact."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(
        store,
        participant_executor=_answering("Report about Agno factories."),
    )
    store.create_workflow(
        spec=_parsed_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.run_workflow(
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


@pytest.mark.asyncio
async def test_run_workflow_persists_failed_run_when_completion_persistence_fails(tmp_path: Path) -> None:
    """Completion persistence failures should not leave the run stuck as running."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store, participant_executor=_answering(object()))
    store.create_workflow(
        spec=_parsed_spec(
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

    run = await service.run_workflow(
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


@pytest.mark.parametrize("reject_policy", [False, True], ids=["input", "policy"])
@pytest.mark.asyncio
async def test_service_records_validation_failure_before_execution(
    tmp_path: Path,
    reject_policy: bool,
) -> None:
    """Policy and input failures should persist without executing participants."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    executions: list[str] = []

    async def execute(**_kwargs: object) -> str:
        executions.append("executed")
        return "unexpected execution"

    def validate_policy(_spec: WorkflowSpec) -> None:
        if reject_policy:
            msg = "Participant is no longer allowed."
            raise DynamicWorkflowError(msg)

    service = DynamicWorkflowService(
        store,
        participant_executor=execute,
        spec_validator=validate_policy,
    )
    store.create_workflow(
        spec=_parsed_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.run_workflow(
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
    assert run == loaded
    assert loaded.status == "failed"
    assert loaded.error == (
        "Participant is no longer allowed." if reject_policy else "Input field 'topic' is required."
    )
    assert loaded.steps == []
    assert loaded.completed_at is not None
    assert executions == []


@pytest.mark.parametrize("stage", ["validation", "execution", "completion"])
@pytest.mark.asyncio
async def test_service_records_a_failure_without_text_by_its_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """A run that fails with an exception without text records the exception's type name as its error."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    def fail(*_arguments: object, **_options: object) -> None:
        raise TimeoutError

    async def afail(*_arguments: object, **_options: object) -> None:
        raise TimeoutError

    if stage == "execution":
        monkeypatch.setattr(service_module, "execute_workflow_spec", afail)
    if stage == "completion":
        monkeypatch.setattr(store, "complete_workflow_run", fail)
    service = DynamicWorkflowService(
        store,
        participant_executor=_answering("written"),
        spec_validator=fail if stage == "validation" else None,
    )
    store.create_workflow(
        spec=_parsed_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "pricing"},
        requested_by="general",
    )

    assert (run.status, run.error) == ("failed", "TimeoutError")


@pytest.mark.asyncio
async def test_service_uses_revision_pinned_when_run_started(tmp_path: Path) -> None:
    """An update after admission must not replace the run's spec or input schema."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_parsed_spec(
            workflow=[{"id": "write", "type": "transform_step", "template": "Original {input.topic}."}],
        ),
        scope="agent",
        owner_id="general",
        created_by="general",
    )
    started = store.start_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "report"},
        requested_by="general",
    )
    store.update_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        patch={
            "name": "Updated report",
            "inputs": {"required": ["new_field"], "properties": {"new_field": {"type": "string"}}},
            "workflow": [{"id": "write", "type": "transform_step", "template": "Updated {input.new_field}."}],
        },
        updated_by="general",
        reason="Update report inputs",
    )
    validated_names: list[str] = []
    service = DynamicWorkflowService(store, spec_validator=lambda spec: validated_names.append(spec.name))

    with patch.object(store, "start_workflow_run", return_value=started):
        run = await service.run_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            input_data={"topic": "report"},
            requested_by="general",
        )

    loaded = store.get_workflow_run(
        workflow_id=run.workflow_id,
        scope=run.scope,
        owner_id=run.owner_id,
        run_id=run.run_id,
    )
    assert run == loaded
    assert run.status == "completed"
    assert run.revision == "000001"
    assert run.outputs == {"report_html": "Original report."}
    assert validated_names == ["Competitor Research Report"]
    assert "Competitor Research Report" in store.run_report_html_artifact_path(run).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_service_propagates_run_start_failure(tmp_path: Path) -> None:
    """Admission errors must escape without attempting to persist a failed run."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)

    with pytest.raises(DynamicWorkflowError, match="YAML mapping was not found"):
        await service.run_workflow(
            workflow_id="missing-workflow",
            scope="agent",
            owner_id="general",
            input_data={},
            requested_by="general",
        )

    assert not (tmp_path / "mindroom_data").exists()


@pytest.mark.asyncio
async def test_run_workflow_enforces_input_schema_enum(tmp_path: Path) -> None:
    """Declared enum constraints should be enforced before workflow execution."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_parsed_spec(
            inputs={
                "type": "object",
                "required": ["topic", "visibility"],
                "properties": {
                    "topic": {"type": "string"},
                    "visibility": {"type": "string", "enum": ["private"]},
                },
            },
            **_RESEARCH_BRIEF_STEPS,
        ),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "Agno factories", "visibility": "public"},
        requested_by="general",
        base_url="https://acme.mindroom.chat",
    )

    assert run.status == "failed"
    assert "declared enum values" in str(run.error)


@pytest.mark.asyncio
async def test_validate_workflow_tool_policy_rejects_unknown_tool(tmp_path: Path) -> None:
    """The context-aware policy layer rejects tool grants that name no registered tool."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()

    with tool_runtime_context(context):
        unknown_payload = _tool_payload(
            await tool.validate_workflow(
                _workflow_spec(
                    participants=[
                        {"id": "writer", "kind": "subagent", "system_prompt": "P", "tools": ["not_a_real_tool"]},
                    ],
                    permissions={"tools": ["not_a_real_tool"]},
                ),
            ),
        )

    assert unknown_payload["status"] == "error"
    assert "not a registered tool" in unknown_payload["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restricted_tool",
    [
        "agent_manager",
        "compact_context",
        "contacts",
        "delegate",
        "dynamic_tools",
        "dynamic_workflow",
        "invite_router",
        "memory",
        "self_config",
        "skill_manage",
        "thread_model",
    ],
)
async def test_validate_workflow_tool_policy_rejects_each_restricted_tool(tmp_path: Path, restricted_tool: str) -> None:
    """Every agent-infrastructure tool must be rejected as a participant grant."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()

    with tool_runtime_context(context):
        # Place the grant only in permissions.tools, with no participant using it: the
        # policy layer is the sole gate for this case (store validation is shape-only and
        # the subset rule passes trivially when no participant references the tool).
        permissions_only_payload = _tool_payload(
            await tool.validate_workflow(
                _workflow_spec(
                    participants=[{"id": "writer", "kind": "subagent", "system_prompt": "P"}],
                    permissions={"tools": [restricted_tool]},
                ),
            ),
        )
        participant_payload = _tool_payload(
            await tool.validate_workflow(
                _workflow_spec(
                    participants=[
                        {"id": "writer", "kind": "subagent", "system_prompt": "P", "tools": [restricted_tool]},
                    ],
                    permissions={"tools": [restricted_tool]},
                ),
            ),
        )

    assert permissions_only_payload["status"] == "error"
    assert "agent-infrastructure" in permissions_only_payload["message"]
    assert participant_payload["status"] == "error"
    assert "agent-infrastructure" in participant_payload["message"]


@pytest.mark.asyncio
async def test_run_workflow_executes_steps_and_persists_outputs(tmp_path: Path) -> None:
    """Running a workflow should execute declared steps and persist their outputs."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_parsed_spec(
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

    run = await service.run_workflow(
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


@pytest.mark.asyncio
async def test_agent_step_uses_participant_executor_instead_of_prompt_template() -> None:
    """Agent steps should invoke the resolved participant instead of echoing the prompt."""

    async def participant_executor(*, participant: Participant, prompt: str) -> ParticipantOutput:
        assert participant.id == "writer"
        assert prompt == "Write about Agno factories."
        return ParticipantOutput("Executed by Report Writer.")

    execution = await execute_workflow_spec(
        _parsed_spec(
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "text"),
    [(RuntimeError("model unavailable"), "model unavailable"), (TimeoutError(), "TimeoutError")],
    ids=["with_text", "without_text"],
)
async def test_a_participant_that_raises_fails_its_step_with_why(error: Exception, text: str) -> None:
    """A participant's exception fails its step with the exception's text, or its type name when it has none."""
    spec = _parsed_spec(
        workflow=[
            {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write about {input.topic}."},
        ],
    )

    async def fail(**_arguments: object) -> ParticipantOutput:
        raise error

    execution = await execute_workflow_spec(spec, {"topic": "pricing"}, participant_executor=fail)

    assert (execution.status, execution.error) == ("failed", f"Agent step 'write' participant 'writer' failed: {text}")


@pytest.mark.asyncio
async def test_agent_step_fails_without_participant_executor() -> None:
    """Agent steps should not silently degrade into template-only execution."""
    execution = await execute_workflow_spec(
        _parsed_spec(
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


@pytest.mark.asyncio
async def test_service_completes_tool_runs_without_raw_background_thread(tmp_path: Path) -> None:
    """Tool-triggered workflow runs should complete on the managed execution path."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_parsed_spec(**_RESEARCH_BRIEF_STEPS),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.run_workflow(
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


@pytest.mark.asyncio
async def test_service_persists_why_an_execution_failed(tmp_path: Path) -> None:
    """A run whose execution fails is stored failed, with the failed step's reason."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")

    async def fail(**_arguments: object) -> ParticipantOutput:
        msg = "model unavailable"
        raise RuntimeError(msg)

    service = DynamicWorkflowService(store, participant_executor=fail)
    store.create_workflow(
        spec=_parsed_spec(
            workflow=[
                {"id": "write", "type": "agent_step", "participant": "writer", "prompt": "Write about {input.topic}."},
            ],
        ),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.run_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        input_data={"topic": "pricing"},
        requested_by="general",
    )
    loaded = store.get_workflow_run(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        run_id=run.run_id,
    )

    assert (loaded.status, loaded.error) == (
        "failed",
        "Agent step 'write' participant 'writer' failed: model unavailable",
    )


@pytest.mark.asyncio
async def test_service_run_enforces_runtime_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Async service runs should cancel participant work when the runtime cap is exceeded."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    participant_cancelled = asyncio.Event()

    async def slow_executor(**_kwargs: object) -> object:
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            participant_cancelled.set()
            raise
        return ParticipantOutput("late")

    service = DynamicWorkflowService(store, participant_executor=slow_executor)
    monkeypatch.setattr(Permissions, "runtime_seconds", property(lambda _permissions: 0.01))
    store.create_workflow(
        spec=_parsed_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.run_workflow(
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
async def test_service_run_fails_at_deadline_when_cancellation_is_suppressed(
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
            return ParticipantOutput("late")
        return ParticipantOutput("late")

    service = DynamicWorkflowService(store, participant_executor=stubborn_executor)
    monkeypatch.setattr(Permissions, "runtime_seconds", property(lambda _permissions: 0.01))
    store.create_workflow(
        spec=_parsed_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )

    run = await service.run_workflow(
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


@pytest.mark.parametrize("ending", ["timeout", "cancelled", "failure"])
@pytest.mark.asyncio
async def test_service_records_how_a_run_ended_while_the_loop_stays_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """However a run ends, its records are written while other coroutines keep running on the event loop."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(spec=_parsed_spec(), scope="agent", owner_id="general", created_by="general")

    async def executor(**_kwargs: object) -> ParticipantOutput:
        if ending == "cancelled":
            raise asyncio.CancelledError
        await asyncio.sleep(1)
        return ParticipantOutput("late")

    async def fail(*_arguments: object, **_options: object) -> None:
        msg = "runner broke"
        raise RuntimeError(msg)

    if ending == "timeout":
        monkeypatch.setattr(Permissions, "runtime_seconds", property(lambda _permissions: 0.01))
    if ending == "failure":
        monkeypatch.setattr(service_module, "execute_workflow_spec", fail)
    service = DynamicWorkflowService(store, participant_executor=executor)

    with file_work_waiting_for_the_loop(
        {DynamicWorkflowStore: ("start_workflow_run", "load_workflow_revision", "fail_workflow_run")},
    ) as freedom:
        running = service.run_workflow(
            workflow_id="competitor-research-report",
            scope="agent",
            owner_id="general",
            input_data={"topic": "pricing"},
            requested_by="general",
        )
        if ending == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            await running

    run_data = _only_run_record(tmp_path)
    assert freedom.blocked == []
    assert freedom.called == ["start_workflow_run", "load_workflow_revision", "fail_workflow_run"]
    assert run_data["status"] == "failed"
    assert (
        run_data["error"]
        == {
            "timeout": "Workflow run exceeded permissions.max_runtime_seconds (0.01).",
            "cancelled": "Workflow run was cancelled.",
            "failure": "runner broke",
        }[ending]
    )


@pytest.mark.parametrize("ending", ["timeout", "failure", "completion"])
@pytest.mark.asyncio
async def test_service_keeps_the_final_record_a_cancellation_interrupts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    """A run cancelled while its final record is written keeps that record, and the call waits for the write."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(spec=_parsed_spec(), scope="agent", owner_id="general", created_by="general")
    loop = asyncio.get_running_loop()
    final_write_started = asyncio.Event()
    release_final_write = threading.Event()
    writes: list[str] = []
    final_method = "complete_workflow_run" if ending == "completion" else "fail_workflow_run"
    original = getattr(DynamicWorkflowStore, final_method)

    def recorded(*args: object, **kwargs: object) -> DynamicWorkflowRun:
        error = kwargs.get("error")
        kind = "cancelled" if error == "Workflow run was cancelled." else ending
        writes.append(f"{kind} started")
        if kind == ending:
            loop.call_soon_threadsafe(final_write_started.set)
            release_final_write.wait(timeout=5)
        written = original(*args, **kwargs)
        writes.append(f"{kind} written")
        return written

    async def executor(**_kwargs: object) -> ParticipantOutput:
        if ending == "timeout":
            await asyncio.sleep(1)
        return ParticipantOutput("written")

    async def fail(*_arguments: object, **_options: object) -> None:
        msg = "runner broke"
        raise RuntimeError(msg)

    if ending == "timeout":
        monkeypatch.setattr(Permissions, "runtime_seconds", property(lambda _permissions: 0.01))
    if ending == "failure":
        monkeypatch.setattr(service_module, "execute_workflow_spec", fail)
    service = DynamicWorkflowService(store, participant_executor=executor)
    with patch.object(DynamicWorkflowStore, final_method, recorded):
        running = asyncio.create_task(
            service.run_workflow(
                workflow_id="competitor-research-report",
                scope="agent",
                owner_id="general",
                input_data={"topic": "pricing"},
                requested_by="general",
            ),
        )
        await final_write_started.wait()
        running.cancel()
        # The call ends only once the write under way has finished.
        await asyncio.wait({running}, timeout=0.5)
        still_writing = not running.done()
        release_final_write.set()
        with pytest.raises(asyncio.CancelledError):
            await running

    run_data = _only_run_record(tmp_path)
    assert still_writing
    assert writes == [f"{ending} started", f"{ending} written"]
    assert (run_data["status"], run_data["error"]) == {
        "timeout": ("failed", "Workflow run exceeded permissions.max_runtime_seconds (0.01)."),
        "failure": ("failed", "runner broke"),
        "completion": ("completed", None),
    }[ending]


@pytest.mark.parametrize(
    ("window", "rejection"),
    [("start_workflow_run", None), ("load_workflow_revision", None), ("load_workflow_revision", "Not allowed.")],
)
@pytest.mark.asyncio
async def test_service_records_a_run_cancelled_while_it_is_prepared_as_cancelled(
    tmp_path: Path,
    window: str,
    rejection: str | None,
) -> None:
    """A run cancelled while it is being prepared ends cancelled, unless its preparation already rejected it."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(spec=_parsed_spec(), scope="agent", owner_id="general", created_by="general")
    loop = asyncio.get_running_loop()
    preparation_waiting = asyncio.Event()
    release_preparation = threading.Event()
    executions: list[object] = []
    original = getattr(DynamicWorkflowStore, window)

    def waiting(*args: object, **kwargs: object) -> object:
        loop.call_soon_threadsafe(preparation_waiting.set)
        release_preparation.wait(timeout=5)
        return original(*args, **kwargs)

    async def executor(**kwargs: object) -> ParticipantOutput:
        executions.append(kwargs)
        return ParticipantOutput("written")

    def reject(_spec: WorkflowSpec) -> None:
        if rejection is not None:
            raise DynamicWorkflowError(rejection)

    service = DynamicWorkflowService(store, participant_executor=executor, spec_validator=reject)
    with patch.object(DynamicWorkflowStore, window, waiting):
        running = asyncio.create_task(
            service.run_workflow(
                workflow_id="competitor-research-report",
                scope="agent",
                owner_id="general",
                input_data={"topic": "pricing"},
                requested_by="general",
            ),
        )
        await preparation_waiting.wait()
        running.cancel()
        release_preparation.set()
        with pytest.raises(asyncio.CancelledError):
            await running

    run_data = _only_run_record(tmp_path)
    assert (run_data["status"], run_data["error"]) == ("failed", rejection or "Workflow run was cancelled.")
    assert executions == []


@pytest.mark.asyncio
async def test_service_ends_cancelled_when_a_cancelled_preparation_raises(tmp_path: Path) -> None:
    """A cancellation during a preparation that fails, such as for a deleted workflow, still ends the call cancelled."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    loop = asyncio.get_running_loop()
    preparation_waiting = asyncio.Event()
    release_preparation = threading.Event()

    def deleted(*_args: object, **_kwargs: object) -> DynamicWorkflowRun:
        loop.call_soon_threadsafe(preparation_waiting.set)
        release_preparation.wait(timeout=5)
        msg = "YAML mapping was not found."
        raise DynamicWorkflowError(msg)

    service = DynamicWorkflowService(store, participant_executor=_answering("written"))
    with patch.object(DynamicWorkflowStore, "start_workflow_run", deleted):
        running = asyncio.create_task(
            service.run_workflow(
                workflow_id="competitor-research-report",
                scope="agent",
                owner_id="general",
                input_data={"topic": "pricing"},
                requested_by="general",
            ),
        )
        await preparation_waiting.wait()
        running.cancel()
        release_preparation.set()
        with pytest.raises(asyncio.CancelledError) as cancelled:
            await running

    assert isinstance(cancelled.value.__cause__, DynamicWorkflowError)
    assert not (tmp_path / "mindroom_data/dynamic_workflows").exists()


def test_get_workflow_run_rejects_traversal_run_id(tmp_path: Path) -> None:
    """Run lookup should reject path traversal before building the run filename."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_parsed_spec(),
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
        spec=_parsed_spec(),
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
        spec=_parsed_spec(),
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


def test_revision_with_non_text_descriptions_reads_them_as_text(tmp_path: Path) -> None:
    """A saved non-text workflow or input-property description reads as its text, a null stays null, and updates save text."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    store.create_workflow(
        spec=_parsed_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    revisions = tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report/revisions"
    revision = yaml.safe_load((revisions / "000001.yaml").read_text(encoding="utf-8"))
    revision["description"] = {"summary": 3}
    revision["inputs"]["properties"] = {"topic": {"type": "string", "description": 7}, "depth": {"description": None}}
    (revisions / "000001.yaml").write_text(yaml.safe_dump(revision, sort_keys=False), encoding="utf-8")

    spec = store.load_workflow_revision(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        revision="000001",
    )
    store.update_workflow(
        workflow_id="competitor-research-report",
        scope="agent",
        owner_id="general",
        patch={"name": "Renamed"},
        updated_by="general",
        reason="rename",
    )

    assert spec.description == "{'summary': 3}"
    assert spec.inputs is not None
    assert spec.inputs.properties["topic"].description == "7"
    assert spec.inputs.properties["depth"].description is None
    updated = yaml.safe_load((revisions / "000002.yaml").read_text(encoding="utf-8"))
    assert updated["description"] == "{'summary': 3}"
    assert updated["inputs"]["properties"] == {"topic": {"type": "string", "description": "7"}, "depth": {}}


@pytest.mark.asyncio
async def test_run_workflow_records_failed_run_when_stored_step_reference_is_missing(tmp_path: Path) -> None:
    """Failed workflow execution should still persist a run record and error report."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_parsed_spec(),
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

    run = await service.run_workflow(
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


@pytest.mark.asyncio
async def test_run_workflow_records_failed_run_when_active_revision_is_missing(tmp_path: Path) -> None:
    """Revision load failures after run creation should not leave run records stuck as running."""
    store = DynamicWorkflowStore(tmp_path / "mindroom_data")
    service = DynamicWorkflowService(store)
    store.create_workflow(
        spec=_parsed_spec(),
        scope="agent",
        owner_id="general",
        created_by="general",
        reason="initial design",
    )
    revision_path = (
        tmp_path / "mindroom_data/dynamic_workflows/agent/general/competitor-research-report/revisions/000001.yaml"
    )
    revision_path.unlink()

    run = await service.run_workflow(
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


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_uses_runtime_context(tmp_path: Path) -> None:
    """Runtime-aware tool should scope workflows to current agent and storage root."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    transform_spec = _workflow_spec(**_RESEARCH_BRIEF_STEPS)

    with tool_runtime_context(context):
        created = _tool_payload(await tool.create_workflow(transform_spec, reason="initial design"))
        listed = _tool_payload(await tool.list_workflows())
        run = _tool_payload(
            await tool.run_workflow(
                workflow_id="competitor-research-report",
                input={"topic": "Agno factories"},
            ),
        )

    assert created["status"] == "ok"
    assert created["workflow_id"] == "competitor-research-report"
    # Every workflow is the calling agent's, so results name no scope the tools would not take.
    assert "scope" not in created
    assert "scope" not in listed
    assert listed["workflows"][0]["workflow_id"] == "competitor-research-report"
    assert run["status"] == "completed"
    assert run["outputs"]["brief"] == "Research brief for Agno factories."
    assert run["report_url"].startswith(
        "https://acme.mindroom.chat/reports/private/agent/general/competitor-research-report/run_",
    )


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_denies_run_read_for_different_requester(tmp_path: Path) -> None:
    """Agent-scoped run details should not leak across Matrix requesters."""
    tool = DynamicWorkflowTools()
    alice_context = _make_context(tmp_path)
    bob_context = replace(alice_context, requester_id="@bob:localhost")
    transform_spec = _workflow_spec(**_RESEARCH_BRIEF_STEPS)

    with tool_runtime_context(alice_context):
        _tool_payload(await tool.create_workflow(transform_spec, reason="initial design"))
        run = _tool_payload(await tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}))
    with tool_runtime_context(bob_context):
        read_result = _tool_payload(await tool.get_workflow_run("competitor-research-report", run["run_id"]))

    assert read_result["status"] == "error"
    assert "not available to the current requester" in read_result["message"]


@pytest.mark.asyncio
async def test_dynamic_workflow_json_arguments_take_objects_of_any_json_values() -> None:
    """spec, patch, and input take an object of any JSON values; a non-object or an unknown argument fails validation."""
    functions = toolkit_functions(DynamicWorkflowTools())
    value = {
        "text": "a",
        "number": 1.5,
        "integer": 2,
        "flag": True,
        "none": None,
        "list": [1, "b"],
        "object": {"c": []},
    }
    calls = {
        "create_workflow": ("spec", {}),
        "validate_workflow": ("spec", {}),
        "update_workflow": ("patch", {"workflow_id": "flow", "reason": "edit"}),
        "run_workflow": ("input", {"workflow_id": "flow"}),
    }

    for name, (argument, others) in calls.items():
        # Outside a tool call the body answers that it has no runtime context, so the arguments validated.
        accepted = json.loads(await functions[name](**others, **{argument: value}))
        assert "tool context is unavailable" in accepted["message"]
        with pytest.raises(ValidationError):
            await functions[name](**others, **{argument: ["not", "an", "object"]})
    with pytest.raises(ValidationError):
        await functions["list_workflows"](scope="agent")


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_scopes_private_agent_workflows_by_requester(tmp_path: Path) -> None:
    """Private agents should not share agent-scoped workflows across requesters."""
    tool = DynamicWorkflowTools()
    alice_context = _make_private_context(tmp_path, requester_id="@alice:localhost")
    bob_context = _make_private_context(tmp_path, requester_id="@bob:localhost")

    with tool_runtime_context(alice_context):
        created = _tool_payload(await tool.create_workflow(_workflow_spec(), reason="initial design"))
        alice_listed = _tool_payload(await tool.list_workflows())
    with tool_runtime_context(bob_context):
        bob_listed = _tool_payload(await tool.list_workflows())

    assert created["status"] == "ok"
    assert created["owner_id"].startswith("private_")
    assert alice_listed["workflows"][0]["workflow_id"] == "competitor-research-report"
    assert bob_listed["workflows"] == []


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_accepts_any_configured_participant_model(tmp_path: Path) -> None:
    """Subagent participants may choose any configured model, like subagents started with run_subagent."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], model="default")},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-5"),
                "opus": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config), active_model_name="default")

    with tool_runtime_context(context):
        result = _tool_payload(
            await tool.validate_workflow(
                _workflow_spec(
                    participants=[
                        {
                            "id": "writer",
                            "kind": "subagent",
                            "system_prompt": "P",
                            "model": "opus",
                            "tools": [],
                        },
                    ],
                    permissions={"models": ["claude-opus-5"], "tools": []},
                ),
            ),
        )

    assert result["status"] == "ok", result


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_enforces_permission_models_for_default_participant_model(tmp_path: Path) -> None:
    """Omitted participant models should still be checked against workflow permissions.models."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], model="default")},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-5"),
                "opus": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config), active_model_name="default")

    with tool_runtime_context(context):
        result = _tool_payload(
            await tool.validate_workflow(
                _workflow_spec(
                    participants=[
                        {
                            "id": "writer",
                            "kind": "subagent",
                            "system_prompt": "P",
                            "tools": [],
                        },
                    ],
                    permissions={"models": ["claude-opus-5"], "tools": []},
                ),
            ),
        )

    assert result["status"] == "error"
    assert "permissions.models" in result["message"]


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_defaults_participant_model_to_caller_runtime_model(tmp_path: Path) -> None:
    """Omitted participant models should not fall back to the global default model."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], model="opus")},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-5"),
                "opus": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config), active_model_name=None)

    with tool_runtime_context(context):
        result = _tool_payload(
            await tool.validate_workflow(
                _workflow_spec(
                    participants=[
                        {
                            "id": "writer",
                            "kind": "subagent",
                            "system_prompt": "P",
                            "tools": [],
                        },
                    ],
                    permissions={"models": ["claude-sonnet-5"], "tools": []},
                ),
            ),
        )

    assert result["status"] == "error"
    assert "permissions.models" in result["message"]


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_rejects_unknown_room_agent_during_validation(tmp_path: Path) -> None:
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
        validated = _tool_payload(await tool.validate_workflow(spec))
        created = _tool_payload(await tool.create_workflow(spec, reason="initial design"))

    assert validated["status"] == "error"
    assert "unknown room agent 'missing'" in validated["message"]
    assert created["status"] == "error"
    assert "unknown room agent 'missing'" in created["message"]


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_rejects_unavailable_room_agent_during_validation(tmp_path: Path) -> None:
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
        result = _tool_payload(await tool.validate_workflow(spec))

    assert result["status"] == "error"
    assert "not available to this requester in this room" in result["message"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_dynamic_workflow_validation_uses_current_authorization_after_reload(tmp_path: Path) -> None:
    """A long-lived tool context must reject room agents revoked by a config reload."""
    tool = DynamicWorkflowTools()
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    current_config = context.config.model_copy(deep=True)
    with_responder_access(current_config, "specialist", users=[])
    context = replace(context, config_provider=lambda: current_config)
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
        result = _tool_payload(await tool.validate_workflow(spec))

    assert result["status"] == "error"
    assert "not available to this requester in this room" in result["message"]


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_uses_cached_client_room_when_context_room_is_missing(tmp_path: Path) -> None:
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
        result = _tool_payload(await tool.validate_workflow(spec))

    assert result["status"] == "ok"


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_revalidates_saved_revision_policy_before_run(tmp_path: Path) -> None:
    """Saved revisions are rechecked against permissions.models with the caller's current model."""
    tool = DynamicWorkflowTools()
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], model="default")},
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-5"),
                "opus": ModelConfig(provider="anthropic", id="claude-opus-5"),
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

    spec = _workflow_spec(participants=[{"id": "writer", "kind": "subagent", "system_prompt": "P", "tools": []}])
    with tool_runtime_context(create_context):
        created = _tool_payload(await tool.create_workflow(spec, reason="initial design"))
    with tool_runtime_context(run_context):
        run = _tool_payload(await tool.run_workflow("competitor-research-report", {"topic": "Agno factories"}))

    assert created["status"] == "ok"
    assert run["status"] == "failed"
    assert "permissions.models" in str(run["error"])


@pytest.mark.asyncio
async def test_dynamic_workflow_tool_returns_payload_when_agent_name_is_missing(tmp_path: Path) -> None:
    """Runtime-aware tool should fail cleanly when required context owner data is missing."""
    tool = DynamicWorkflowTools()
    context = replace(_make_context(tmp_path), agent_name="")

    with tool_runtime_context(context):
        result = _tool_payload(await tool.list_workflows())

    assert result["status"] == "error"
    assert "Agent name is missing" in result["message"]


def test_room_agent_participant_must_be_available_to_requester_in_room(tmp_path: Path) -> None:
    """Room-agent participants should not bypass normal room responder eligibility."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general"])

    with pytest.raises(DynamicWorkflowError, match="not available to this requester in this room"):
        asyncio.run(
            dynamic_workflow_module._execute_room_agent_participant(
                context,
                RoomAgentParticipant.model_validate({"id": "specialist", "kind": "room_agent", "agent": "specialist"}),
                "Write a report.",
                run_key="report:run_1",
            ),
        )


def test_room_agent_participant_runs_as_that_agent_without_durable_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Room-agent participants run as that agent on its room model, with the prompt alone and nothing stored."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"]),
                "specialist": AgentConfig(
                    display_name="Specialist Agent",
                    model="default",
                    rooms=["lobby"],
                    tools=["memory"],
                    knowledge_bases=["reference"],
                ),
            },
            models={
                "default": ModelConfig(provider="anthropic", id="claude-sonnet-5"),
                "large": ModelConfig(provider="anthropic", id="claude-opus-5"),
            },
            room_models={"lobby": "large"},
            knowledge_bases={"reference": {"path": str(tmp_path / "knowledge")}},
        ),
        context.runtime_paths,
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_room("lobby", room_id="!room:localhost", alias="#lobby:localhost", name="Lobby")
    save_matrix_state(state, runtime_paths)
    context = replace(context, config=config, runtime_paths=runtime_paths)
    parent_loop = asyncio.new_event_loop()

    async def on_the_callers_loop() -> None:
        assert asyncio.get_running_loop() is parent_loop

    model, loaded = _room_agent_model(monkeypatch, [Await(on_the_callers_loop), *answer("done")])
    with patch("mindroom.agents.create_agent", wraps=agents.create_agent) as create_agent_mock:
        asyncio.set_event_loop(parent_loop)
        try:
            result = parent_loop.run_until_complete(
                dynamic_workflow_module._execute_room_agent_participant(
                    context,
                    RoomAgentParticipant.model_validate(
                        {"id": "writer_a", "kind": "room_agent", "agent": "specialist"},
                    ),
                    "Write a report.",
                    run_key="competitor-research-report:run_1",
                ),
            )
        finally:
            asyncio.set_event_loop(None)
            parent_loop.close()

    assert result == "done"
    assert loaded == ["large"]
    [request] = model.requests
    *system, prompt = request.messages
    assert "You are Specialist Agent" in system_text(system)
    assert (prompt.role, prompt.content) == ("user", "Write a report.")
    assert request.tools == ()
    create_kwargs = create_agent_mock.call_args.kwargs
    assert create_kwargs["execution_identity"].session_id.endswith(
        ":dynamic_workflow:competitor-research-report:run_1:writer_a",
    )
    assert create_kwargs["model"].model_name == "large"
    assert create_kwargs["knowledge"] is None
    assert create_kwargs["disable_runtime_capabilities"] is True
    assert create_kwargs["execution_identity"].agent_name == "specialist"


@pytest.mark.asyncio
async def test_room_agent_participant_is_resolved_and_built_while_the_loop_stays_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolving a room-agent participant's model and building it, which walk the filesystem, leave the loop free."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])
    _room_agent_model(monkeypatch, answer("done"))

    with file_work_waiting_for_the_loop(
        {agents: ("create_agent",), dynamic_workflow_module: ("resolve_turn_model",)},
    ) as freedom:
        result = await dynamic_workflow_module._execute_room_agent_participant(
            context,
            RoomAgentParticipant.model_validate({"id": "writer_a", "kind": "room_agent", "agent": "specialist"}),
            "Write a report.",
            run_key="report:run_1",
        )

    assert result == "done"
    assert freedom.blocked == []
    assert {"create_agent", "resolve_turn_model"} <= set(freedom.called)


def test_resolve_participant_toolkits_builds_real_instances_with_caller_routing(tmp_path: Path) -> None:
    """Granted tools should resolve through the agent toolkit builder keyed by registry name."""
    context = _make_context(tmp_path)

    toolkits = dynamic_workflow_module._resolve_participant_toolkits(context, ["website"])

    assert list(toolkits) == ["website"]
    assert "read_url" in toolkits["website"].functions


def test_participant_run_config_requires_approval_for_granted_tools(tmp_path: Path) -> None:
    """Without pre-approval config, granted tool calls must default to require_approval."""
    context = _make_context(tmp_path)

    run_config = _run_config(context, {"run_shell_command": frozenset({"shell"})})

    assert run_config.tool_approval.default == "require_approval"
    assert run_config.tool_approval.rules == []
    assert context.config.tool_approval.default == "auto_approve"


def test_dynamic_workflow_uses_shared_automation_approval_policy(tmp_path: Path) -> None:
    """Automation approval requires approval by default and auto-approves functions of pre-approved toolkits."""
    context = _make_context(tmp_path)
    shared = build_automation_approval_config(
        context.config,
        function_owners={"read_url": frozenset({"website"})},
        preapproved_toolkits=frozenset({"website"}),
        never_preapprove_toolkits=NEVER_PREAPPROVE_TOOLKITS,
    )

    assert shared.tool_approval.default == "require_approval"
    assert [(rule.match, rule.action) for rule in shared.tool_approval.rules] == [("read_url", "auto_approve")]


def test_participant_tools_must_be_pre_approved_or_auto_approved(tmp_path: Path) -> None:
    """Named toolkits need a pre-approval; a named function may instead be auto-approved by an operator rule."""
    context = _make_context(tmp_path)
    config = context.config.model_copy(
        update={
            "tool_approval": context.config.tool_approval.model_copy(
                update={
                    "rules": [
                        ApprovalRuleConfig(match="safe", action="auto_approve"),
                        ApprovalRuleConfig(match="dangerous", action="require_approval"),
                    ],
                },
            ),
        },
    )
    context = replace(context, config=config)
    reject = dynamic_workflow_module._reject_unapproved_participant_tools

    reject(context, ("mixed", "other.safe"), allowed=frozenset({"mixed"}))
    with pytest.raises(DynamicWorkflowExecutionError, match="tool 'mixed' is not pre-approved"):
        reject(context, ("mixed",), allowed=frozenset())
    with pytest.raises(DynamicWorkflowExecutionError, match="dangerous require approval and cannot suspend"):
        reject(context, ("mixed.dangerous",), allowed=frozenset({"mixed"}))
    with pytest.raises(DynamicWorkflowExecutionError, match="tool 'scheduler' always requires approval") as never:
        reject(context, ("scheduler",), allowed=frozenset({"*"}))
    assert "allowed_tools" not in str(never.value)


def test_participant_run_config_pre_approves_allowed_tools(tmp_path: Path) -> None:
    """Tools listed in the dynamic_workflow allowed_tools config skip per-call approval."""
    context = _context_with_allowed_tools(tmp_path, ["website"])

    run_config = _run_config(context, {"read_url": frozenset({"website"}), "run_shell_command": frozenset({"shell"})})

    assert run_config.tool_approval.default == "require_approval"
    assert [(rule.match, rule.action) for rule in run_config.tool_approval.rules] == [("read_url", "auto_approve")]


@pytest.mark.parametrize("agent_selected", [True, False])
def test_participant_run_config_pre_approves_dashboard_allowed_tools_for_shared_scope_agent(
    tmp_path: Path,
    *,
    agent_selected: bool,
) -> None:
    """Dashboard pre-approvals reach a shared-scope agent whether saved with that agent selected or globally."""
    context = _make_context(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", tools=["dynamic_workflow"], worker_scope="shared"),
            },
            models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
        ),
        context.runtime_paths,
    )
    context = replace(context, config=config, runtime_paths=runtime_paths_for(config))
    _save_dashboard_allowed_tools(context, ["website"], agent_name="general" if agent_selected else None)

    run_config = _run_config(context, {"read_url": frozenset({"website"})})

    assert [(rule.match, rule.action) for rule in run_config.tool_approval.rules] == [("read_url", "auto_approve")]


def test_workflow_allowed_tools_splits_a_comma_separated_authored_value(tmp_path: Path) -> None:
    """An authored allowed_tools string lists toolkits as the registry's other string-array settings do."""
    context = _context_with_allowed_tools(tmp_path, "calculator, website")

    assert dynamic_workflow_module._workflow_allowed_tools(context) == frozenset({"calculator", "website"})


def test_workflow_allowed_tools_layers_dashboard_values_under_authored_ones(tmp_path: Path) -> None:
    """The agent's dashboard value overrides the global one, and authored config overrides both."""
    context = _make_context(tmp_path)

    def allowed(tools: list[str | dict[str, object]], *, defaults: list[str | dict[str, object]] | None = None) -> set:
        config = bind_runtime_paths(
            Config(
                agents={"general": AgentConfig(display_name="General Agent", tools=tools, worker_scope="shared")},
                defaults={"tools": defaults or []},
                models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
            ),
            context.runtime_paths,
        )
        return set(dynamic_workflow_module._workflow_allowed_tools(replace(context, config=config)))

    _save_dashboard_allowed_tools(context, ["website"])
    assert allowed(["dynamic_workflow"]) == {"website"}
    _save_dashboard_allowed_tools(context, ["calculator"], agent_name="general")
    assert allowed(["dynamic_workflow"]) == {"calculator"}
    assert allowed([{"dynamic_workflow": {"allowed_tools": ["*"]}}]) == {"*"}
    assert allowed([], defaults=[{"dynamic_workflow": {"allowed_tools": ["shell"]}}]) == {"shell"}


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (" calculator ", {"calculator"}),
        (["website", 3, " ", " shell "], {"website", "shell"}),
        (5, set()),
    ],
)
def test_workflow_allowed_tools_reads_only_string_names_of_a_stored_value(
    tmp_path: Path,
    stored: object,
    expected: set[str],
) -> None:
    """A dashboard-saved allowed_tools is unvalidated JSON, so only its non-blank string names count."""
    context = _make_context(tmp_path)
    _save_dashboard_allowed_tools(context, stored)

    assert dynamic_workflow_module._workflow_allowed_tools(context) == expected


def test_participant_run_config_wildcard_pre_approves_all_granted_tools(tmp_path: Path) -> None:
    """allowed_tools ["*"] pre-approves every granted tool's functions."""
    context = _context_with_allowed_tools(tmp_path, ["*"])

    run_config = _run_config(context, {"run_shell_command": frozenset({"shell"})})

    assert run_config.tool_approval.default == "require_approval"
    assert [(rule.match, rule.action) for rule in run_config.tool_approval.rules] == [
        ("run_shell_command", "auto_approve"),
    ]


def test_participant_run_config_does_not_pre_approve_colliding_function_names(tmp_path: Path) -> None:
    """A function name shared with a non-pre-approved toolkit must not be auto-approved."""
    context = _context_with_allowed_tools(tmp_path, ["python"])

    run_config = _run_config(
        context,
        {"read_file": frozenset({"python", "file"}), "run_python_code": frozenset({"python"})},
    )

    rules = {rule.match: rule.action for rule in run_config.tool_approval.rules}
    # run_python_code is unique to the pre-approved python toolkit -> auto-approved.
    assert rules == {"run_python_code": "auto_approve"}
    # read_file collides with the non-pre-approved file toolkit, so it must still require approval.
    assert "read_file" not in rules


def test_participant_run_config_never_pre_approves_system_mutating_tools(tmp_path: Path) -> None:
    """allowed_tools (wildcard or explicit) must not pre-approve system-mutating tools."""
    context = _context_with_allowed_tools(tmp_path, ["*", "scheduler"])

    run_config = _run_config(context, {"schedule_task": frozenset({"scheduler"}), "read_url": frozenset({"website"})})

    assert run_config.tool_approval.default == "require_approval"
    assert [(rule.match, rule.action) for rule in run_config.tool_approval.rules] == [("read_url", "auto_approve")]


def test_participant_run_config_preserves_operator_rule_precedence(tmp_path: Path) -> None:
    """An operator require_approval rule must win over workflow pre-approval (first-match)."""
    context = _context_with_allowed_tools(
        tmp_path,
        ["*"],
        tool_approval={"rules": [{"match": "run_shell_command", "action": "require_approval"}]},
    )

    run_config = _run_config(context, {"run_shell_command": frozenset({"shell"})})

    ordered = [(rule.match, rule.action) for rule in run_config.tool_approval.rules]
    assert ordered[0] == ("run_shell_command", "require_approval")
    assert ordered[1] == ("run_shell_command", "auto_approve")
    matched = _matching_tool_approval_rule(run_config, "run_shell_command")
    assert matched is not None
    assert matched.action == "require_approval"


@pytest.mark.asyncio
async def test_a_room_agent_whose_model_fails_fails_its_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant's provider error fails its workflow step with the provider's message, never as its content."""
    context = _make_multi_agent_context(tmp_path, room_agents=["general", "specialist"])

    async def fail() -> None:
        msg = "provider auth failed"
        raise ProviderError(msg, provider="Synthetic")

    _room_agent_model(monkeypatch, [Await(fail)])
    spec = _parsed_spec(participants=[{"id": "writer", "kind": "room_agent", "agent": "specialist"}])

    run = await execute_workflow_spec(
        spec,
        {"topic": "pricing"},
        participant_executor=dynamic_workflow_module._participant_executor(context, _WorkflowRun("run_1")),
    )

    assert run.status == "failed"
    assert run.error == "Agent step 'write' participant 'writer' failed: provider auth failed"
    assert [(step.status, step.content) for step in run.steps] == [("failed", "")]


@pytest.mark.asyncio
async def test_validate_workflow_reports_all_spec_errors_in_one_call(tmp_path: Path) -> None:
    """One validate_workflow call returns the full repair list, not one error per call."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()

    with tool_runtime_context(context):
        payload = _tool_payload(
            await tool.validate_workflow(
                {
                    "steps": [],
                    "schema_version": 2,
                    "id": "x",
                    "name": "X",
                },
            ),
        )

    assert payload["status"] == "error"
    assert payload["errors"] == [
        "Workflow spec contains unsupported field 'steps'.",
        "Workflow spec field 'schema_version' must be 1.",
        "Workflow spec field 'kind': Field required.",
        "Workflow spec field 'participants': Field required.",
        "Workflow spec field 'workflow': Field required.",
    ]
    assert payload["message"] == "\n".join(payload["errors"])
    assert payload["minimal_valid_spec_example"] == _MINIMAL_SPEC_EXAMPLE


@pytest.mark.asyncio
async def test_create_workflow_reports_all_spec_errors_in_one_call(tmp_path: Path) -> None:
    """create_workflow rejects invalid specs with the same full error list."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()

    with tool_runtime_context(context):
        payload = _tool_payload(await tool.create_workflow({"schema_version": 1, "kind": "workflow"}))

    assert payload["status"] == "error"
    assert payload["errors"] == [
        "Workflow spec field 'id': Field required.",
        "Workflow spec field 'name': Field required.",
        "Workflow spec field 'participants': Field required.",
        "Workflow spec field 'workflow': Field required.",
    ]


@pytest.mark.asyncio
async def test_update_workflow_reports_all_spec_errors_in_one_call(tmp_path: Path) -> None:
    """update_workflow rejects a patch that breaks the spec with the same full error list, and saves nothing."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()

    with tool_runtime_context(context):
        created = _tool_payload(await tool.create_workflow(_workflow_spec(), reason="initial design"))
        payload = _tool_payload(
            await tool.update_workflow(
                created["workflow_id"],
                {"name": "", "permissions": {"max_runtime_seconds": 0, "budget": 1}},
                "break it",
            ),
        )
        revisions = _tool_payload(await tool.list_workflow_revisions(created["workflow_id"]))

    assert payload["status"] == "error"
    assert payload["workflow_id"] == "competitor-research-report"
    assert payload["errors"] == [
        "Workflow permission contains unsupported field 'budget'.",
        "Workflow spec field 'name': String should have at least 1 character.",
        "Workflow permission field 'max_runtime_seconds': Input should be greater than or equal to 1.",
    ]
    assert payload["message"] == "\n".join(payload["errors"])
    assert revisions["revisions"] == ["000001"]


@pytest.mark.asyncio
async def test_minimal_spec_example_in_tool_description_is_valid(tmp_path: Path) -> None:
    """The minimal example advertised in the tool schema validates as-is."""
    context = _make_context(tmp_path)
    tool = DynamicWorkflowTools()

    with tool_runtime_context(context):
        payload = _tool_payload(await tool.validate_workflow(json.loads(_MINIMAL_SPEC_EXAMPLE)))

    assert payload["status"] == "ok"
    assert payload["workflow_id"] == "my_flow"


def test_participant_function_owners_count_only_selected_functions() -> None:
    """A function a participant did not select never makes a selected function look shared."""
    owners = dynamic_workflow_module._selected_function_owners(
        ("file.read_file", "python.run_python_code"),
        {"file": ("read_file", "save_file"), "python": ("read_file", "run_python_code")},
    )
    assert owners == {"read_file": frozenset({"file"}), "run_python_code": frozenset({"python"})}
    whole = dynamic_workflow_module._selected_function_owners(
        ("file", "python"),
        {"file": ("read_file",), "python": ("read_file",)},
    )
    assert whole == {"read_file": frozenset({"file", "python"})}


@pytest.mark.parametrize("scope", ["room", "tenant"])
def test_the_store_has_only_the_agent_scope(tmp_path: Path, scope: str) -> None:
    """No tool creates another scope, so the store refuses one, as a report URL naming it does."""
    with pytest.raises(DynamicWorkflowError, match="Unsupported Dynamic Workflow scope"):
        DynamicWorkflowStore(tmp_path).list_workflows(scope=scope, owner_id="general")
