"""Dynamic Workflow subagent participants run as authored delegation children of their caller."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
import yaml
from agno.models.response import ModelResponse

from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.dynamic_workflow import DynamicWorkflowTools
from mindroom.dynamic_workflows.store import DynamicWorkflowStore
from mindroom.dynamic_workflows.validation import DynamicWorkflowError
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_personas import _ToolRecordingModel
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import _call

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _config(*, tools: list[object] | None = None) -> Config:
    return Config(
        agents={
            "leader": AgentConfig(
                display_name="Leader",
                role="Configured role",
                tools=tools if tools is not None else ["dynamic_workflow", "file", "calculator"],
                memory_backend="file",
            ),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        models={
            "default": ModelConfig(provider="test", id="default-model"),
            "haiku": ModelConfig(provider="test", id="haiku-model"),
        },
    )


def _spec(participants: list[dict[str, object]], steps: int = 1, **permissions: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "workflow",
        "id": "review",
        "name": "Review",
        "participants": participants,
        "workflow": [
            {"id": f"step{index}", "participant": participants[0]["id"], "prompt": f"Step {index}."}
            for index in range(steps)
        ],
        "permissions": permissions,
    }


class _Workflow:
    """One leader with dynamic_workflow and a scripted child model."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        config: Config,
        responses: list[ModelResponse] | None = None,
    ) -> None:
        self.paths: RuntimePaths = _runtime_paths(tmp_path)
        self.config = config
        entity_ids(config, self.paths)
        self.model = _ToolRecordingModel(
            id="test",
            responses=responses if responses is not None else [ModelResponse(content=f"Answer {i}.") for i in range(4)],
        )
        self.models_loaded: list[str] = []

        def load_model(*args: object) -> _ToolRecordingModel:
            self.models_loaded.append(str(args[2]))
            return self.model

        monkeypatch.setattr("mindroom.agents._load_agent_model_instance", load_model)
        self.tools = DynamicWorkflowTools()

    def context(self) -> AbstractContextManager[object]:
        return tool_runtime_context(_delegate_runtime_context(self.config, self.paths, execution_identity=_identity()))

    async def create(self, spec: dict[str, object]) -> dict[str, Any]:
        with self.context():
            return json.loads(await self.tools.acreate_workflow(spec))

    async def run(self, spec: dict[str, object]) -> dict[str, Any]:
        created = await self.create(spec)
        assert created["status"] == "ok", created
        with self.context():
            return json.loads(await self.tools.arun_workflow(workflow_id="review", input={}))

    def workspace(self) -> Path:
        return self.paths.storage_root / "agents" / "leader" / "workspace"


@pytest.mark.asyncio
async def test_participant_tools_must_be_caller_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant can never be granted a tool its caller lacks."""
    workflow = _Workflow(tmp_path, monkeypatch, _config())
    spec = _spec([{"id": "mailer", "system_prompt": "Send mail.", "tools": ["gmail"]}], tools=["gmail"])

    created = await workflow.create(spec)

    assert created["status"] == "error"
    assert created["message"].startswith("Cannot delegate: unknown tool 'gmail'. Your tools: ")


@pytest.mark.asyncio
async def test_participant_steps_write_delegation_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each step is an audited child turn, and one participant keeps one subagent session across steps."""
    workflow = _Workflow(tmp_path, monkeypatch, _config())

    run = await workflow.run(_spec([{"id": "critic", "system_prompt": "Critic prompt", "tools": []}], steps=2))

    assert run["status"] == "completed", run
    assert workflow.model.system_prompts == ["Critic prompt", "Critic prompt"]
    with workflow.context():
        stored = json.loads(await workflow.tools.aget_workflow_run(workflow_id="review", run_id=run["run_id"]))
    delegation_ids = [step["delegation_id"] for step in stored["steps"]]
    assert all(delegation_ids)
    assert len(set(delegation_ids)) == 2
    records = [
        json.loads(next(workflow.workspace().glob(f".mindroom/delegations/*/{delegation_id}/run.json")).read_text())
        for delegation_id in delegation_ids
    ]
    assert records[0]["subagent_id"] == records[1]["subagent_id"]
    assert records[0]["persona"]["source_kind"] == "workflow"
    assert records[0]["persona"]["source_name"] == "review/critic"


@pytest.mark.asyncio
async def test_participant_may_use_any_configured_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Participants choose any configured model, like subagents."""
    workflow = _Workflow(tmp_path, monkeypatch, _config())

    run = await workflow.run(_spec([{"id": "fast", "system_prompt": "Be quick.", "tools": [], "model": "haiku"}]))

    assert run["status"] == "completed", run
    assert workflow.models_loaded == ["haiku"]


@pytest.mark.asyncio
async def test_unapproved_gated_participant_tool_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant cannot pause, so a named tool that is not pre-approved stops the run."""
    workflow = _Workflow(tmp_path, monkeypatch, _config())

    run = await workflow.run(
        _spec([{"id": "adder", "system_prompt": "Add.", "tools": ["calculator"]}], tools=["calculator"]),
    )

    assert run["status"] == "failed"
    assert "tool 'calculator' is not pre-approved" in run["error"]
    assert workflow.model.system_prompts == []


@pytest.mark.asyncio
async def test_participant_without_tools_gets_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant uses only the tools it names, so one that names none is offered no functions."""
    config = _config(tools=[{"dynamic_workflow": {"allowed_tools": ["*"]}}, "file", "calculator"])
    workflow = _Workflow(tmp_path, monkeypatch, config)

    run = await workflow.run(_spec([{"id": "thinker", "system_prompt": "Think."}]))

    assert run["status"] == "completed", run
    assert workflow.model.offered == [[]]


@pytest.mark.asyncio
async def test_profile_participant_runs_workspace_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant can run a saved subagents/<name>.md profile from the caller's workspace."""
    workflow = _Workflow(tmp_path, monkeypatch, _config())
    profile = workflow.workspace() / "subagents" / "critic.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("---\ndescription: Critic.\ntools: []\n---\nYou criticize.\n")

    run = await workflow.run(_spec([{"id": "critic", "profile": "critic"}]))

    assert run["status"] == "completed", run
    assert workflow.model.system_prompts == ["You criticize."]


@pytest.mark.asyncio
async def test_preapproved_participant_tool_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tool listed in the caller's dynamic_workflow allowed_tools runs without approval."""
    config = _config(tools=[{"dynamic_workflow": {"allowed_tools": ["calculator"]}}, "calculator"])
    workflow = _Workflow(
        tmp_path,
        monkeypatch,
        config,
        responses=[ModelResponse(tool_calls=[_call("add", "add-1", a=1, b=2)]), ModelResponse(content="3")],
    )

    run = await workflow.run(_spec([{"id": "adder", "system_prompt": "Add.", "tools": ["calculator"]}]))

    assert run["status"] == "completed", run
    assert workflow.model.responses == []
    assert "add" in workflow.model.offered[0]


def test_subagent_participant_accepts_inline_and_profile(tmp_path: Path) -> None:
    """Inline and profile subagent participants validate."""
    store = DynamicWorkflowStore(tmp_path)
    inline = {"id": "critic", "kind": "subagent", "system_prompt": "P", "tools": ["file"], "model": "haiku"}
    store.validate_workflow(_spec([inline], tools=["file"]))
    store.validate_workflow(_spec([{"id": "critic", "kind": "subagent", "profile": "critic"}]))
    store.validate_workflow(_spec([{"id": "critic", "system_prompt": "P"}]))


@pytest.mark.parametrize(
    "participant",
    [
        {"id": "critic", "profile": "critic", "system_prompt": "P"},
        {"id": "critic", "profile": "critic", "tools": []},
        {"id": "critic"},
        {"id": "critic", "kind": "ephemeral_agent", "name": "Critic", "role": "Criticize"},
        {"id": "critic", "system_prompt": "P", "mode": "fast"},
    ],
)
def test_invalid_subagent_participants_are_rejected(tmp_path: Path, participant: dict[str, object]) -> None:
    """Profiles exclude inline fields, a prompt is required, and the retired kind is rejected for new specs."""
    with pytest.raises(DynamicWorkflowError):
        DynamicWorkflowStore(tmp_path).validate_workflow(_spec([participant]))


def _write_legacy_revision(tmp_path: Path) -> DynamicWorkflowStore:
    store = DynamicWorkflowStore(tmp_path)
    store.create_workflow(
        spec=_spec([{"id": "writer", "system_prompt": "placeholder", "tools": []}]),
        scope="agent",
        owner_id="leader",
        created_by="leader",
        reason=None,
    )
    revision = tmp_path / "dynamic_workflows" / "agent" / "leader" / "review" / "revisions" / "000001.yaml"
    data = yaml.safe_load(revision.read_text())
    data["participants"] = [
        {
            "id": "writer",
            "kind": "ephemeral_agent",
            "name": "Writer",
            "role": "Writes",
            "instructions": ["Cite sources"],
            "description": "Writes reports.",
            "model": "haiku",
        },
    ]
    revision.write_text(yaml.safe_dump(data))
    return store


def test_legacy_revision_loads_as_subagent(tmp_path: Path) -> None:
    """A revision saved with an ephemeral participant runs as an equivalent toolless subagent participant."""
    store = _write_legacy_revision(tmp_path)

    spec = store.load_workflow_revision(workflow_id="review", scope="agent", owner_id="leader", revision="000001")

    assert spec["participants"] == [
        {
            "id": "writer",
            "kind": "subagent",
            "system_prompt": "You are Writer.\n\nWrites\n\n- Cite sources",
            "description": "Writes reports.",
            "model": "haiku",
            "tools": [],
        },
    ]


def test_update_of_legacy_revision_writes_current_format(tmp_path: Path) -> None:
    """Updating a legacy workflow publishes a revision without the retired participant fields."""
    store = _write_legacy_revision(tmp_path)

    store.update_workflow(
        workflow_id="review",
        scope="agent",
        owner_id="leader",
        patch={"description": "Updated."},
        updated_by="leader",
        reason="refresh",
    )

    revision = tmp_path / "dynamic_workflows" / "agent" / "leader" / "review" / "revisions" / "000002.yaml"
    [participant] = yaml.safe_load(revision.read_text())["participants"]
    assert participant["kind"] == "subagent"
    assert not {"name", "role", "instructions"} & set(participant)


@pytest.mark.asyncio
async def test_profile_tools_must_be_granted_by_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile's own tool list obeys a workflow's permissions.tools like an inline list."""
    workflow = _Workflow(tmp_path, monkeypatch, _config())
    profile = workflow.workspace() / "subagents" / "adder.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("---\ndescription: Adds.\ntools: [calculator]\n---\nYou add.\n")

    created = await workflow.create(_spec([{"id": "adder", "profile": "adder"}], tools=["file"]))

    assert created["status"] == "error"
    assert "tool 'calculator' is not granted by permissions.tools" in created["message"]


@pytest.mark.asyncio
async def test_participant_runs_the_profile_validated_at_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An earlier step that rewrites a later participant's profile cannot change how that participant runs."""
    config = _config(tools=[{"dynamic_workflow": {"allowed_tools": ["file"]}}, "file"])
    rewritten = "---\ndescription: B.\ntools: []\nmodel: haiku\n---\nRewritten prompt.\n"
    workflow = _Workflow(
        tmp_path,
        monkeypatch,
        config,
        responses=[
            ModelResponse(
                tool_calls=[_call("save_file", "save-1", contents=rewritten, file_name="subagents/second.md")],
            ),
            ModelResponse(content="Rewrote it."),
            ModelResponse(content="Second answer."),
        ],
    )
    profile = workflow.workspace() / "subagents" / "second.md"
    profile.parent.mkdir(parents=True)
    profile.write_text("---\ndescription: B.\ntools: []\n---\nOriginal prompt.\n")
    spec = _spec(
        [{"id": "first", "system_prompt": "You edit files.", "tools": ["file"]}, {"id": "second", "profile": "second"}],
    )
    spec["workflow"] = [
        {"id": "edit", "participant": "first", "prompt": "Rewrite the profile."},
        {"id": "answer", "participant": "second", "prompt": "Answer."},
    ]

    run = await workflow.run(spec)

    assert run["status"] == "completed", run
    assert profile.read_text() == rewritten
    assert workflow.model.system_prompts[-1] == "Original prompt."
    assert workflow.models_loaded[-1] == "default"


@pytest.mark.asyncio
async def test_declared_toolkits_are_never_built_just_for_approvals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approvals for a declared toolkit come from its metadata, and its configured filters still apply."""
    import mindroom.custom_tools.dynamic_workflow as workflow_module  # noqa: PLC0415 - patched where it is looked up

    built: list[list[str]] = []
    original = workflow_module._resolve_participant_toolkits

    def resolve(context: object, tool_names: list[str]) -> object:
        built.append(list(tool_names))
        return original(context, tool_names)

    monkeypatch.setattr(workflow_module, "_resolve_participant_toolkits", resolve)
    config = _config(
        tools=[
            {"dynamic_workflow": {"allowed_tools": ["file"]}},
            {"file": {"include_tools": ["read_file", "list_files"]}},
        ],
    )
    config = config.model_copy(
        update={
            "tool_approval": config.tool_approval.model_copy(
                update={"rules": [ApprovalRuleConfig(match="save_file", action="require_approval")]},
            ),
        },
    )
    workflow = _Workflow(tmp_path, monkeypatch, config)

    run = await workflow.run(_spec([{"id": "reader", "system_prompt": "Read files.", "tools": ["file"]}], steps=2))

    assert run["status"] == "completed", run
    assert all(not names for names in built)
    assert all("read_file" in offered and "save_file" not in offered for offered in workflow.model.offered)


@pytest.mark.asyncio
async def test_function_entry_allowed_by_operator_rule_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant may name one function an operator auto-approves even when its toolkit is not pre-approved."""
    config = _config(tools=["dynamic_workflow", "calculator"])
    config = config.model_copy(
        update={
            "tool_approval": config.tool_approval.model_copy(
                update={"rules": [ApprovalRuleConfig(match="add", action="auto_approve")]},
            ),
        },
    )
    workflow = _Workflow(tmp_path, monkeypatch, config)

    run = await workflow.run(_spec([{"id": "adder", "system_prompt": "Add.", "tools": ["calculator.add"]}]))

    assert run["status"] == "completed", run
    assert workflow.model.offered == [["add"]]


@pytest.mark.asyncio
async def test_named_function_an_operator_gates_fails_loudly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant that names a function an operator rule still gates fails instead of silently losing it."""
    config = _config(tools=[{"dynamic_workflow": {"allowed_tools": ["file"]}}, "file"])
    config = config.model_copy(
        update={
            "tool_approval": config.tool_approval.model_copy(
                update={"rules": [ApprovalRuleConfig(match="save_file", action="require_approval")]},
            ),
        },
    )
    workflow = _Workflow(tmp_path, monkeypatch, config)

    run = await workflow.run(_spec([{"id": "writer", "system_prompt": "Write.", "tools": ["file.save_file"]}]))

    assert run["status"] == "failed"
    assert "save_file require approval and cannot suspend" in run["error"]
    assert workflow.model.system_prompts == []


@pytest.mark.asyncio
async def test_later_step_fails_when_its_named_function_becomes_gated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An operator gating a named function during a run fails the next step instead of silently hiding the function."""
    auto = _config(tools=["dynamic_workflow", "calculator"])
    auto = auto.model_copy(
        update={
            "tool_approval": auto.tool_approval.model_copy(
                update={"rules": [ApprovalRuleConfig(match="add", action="auto_approve")]},
            ),
        },
    )
    gated = auto.model_copy(
        update={
            "tool_approval": auto.tool_approval.model_copy(
                update={"rules": [ApprovalRuleConfig(match="add", action="require_approval")]},
            ),
        },
    )
    workflow = _Workflow(tmp_path, monkeypatch, auto)
    live = [auto]
    record = workflow.model

    def load_model(*_args: object) -> _ToolRecordingModel:
        live[0] = gated
        return record

    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", load_model)
    context = _delegate_runtime_context(auto, workflow.paths, execution_identity=_identity())
    monkeypatch.setattr(
        workflow,
        "context",
        lambda: tool_runtime_context(replace(context, config_provider=lambda: live[0])),
    )

    run = await workflow.run(_spec([{"id": "adder", "system_prompt": "Add.", "tools": ["calculator.add"]}], steps=2))

    assert run["status"] == "failed", run
    assert "add require approval and cannot suspend" in run["error"]
    assert workflow.model.offered == [["add"]]


@pytest.mark.asyncio
async def test_failed_step_keeps_its_delegation_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A participant turn that fails still links its step to the delegation record it wrote."""
    workflow = _Workflow(tmp_path, monkeypatch, _config(), responses=[])

    run = await workflow.run(_spec([{"id": "critic", "system_prompt": "Critic prompt", "tools": []}]))

    assert run["status"] == "failed", run
    with workflow.context():
        stored = json.loads(await workflow.tools.aget_workflow_run(workflow_id="review", run_id=run["run_id"]))
    [step] = stored["steps"]
    assert step["delegation_id"]
    assert next(workflow.workspace().glob(f".mindroom/delegations/*/{step['delegation_id']}/run.json"))


def test_participant_from_authored_child_stays_within_its_tools(tmp_path: Path) -> None:
    """A workflow started by an authored subagent can grant participants only that subagent's tools."""
    import mindroom.custom_tools.dynamic_workflow as workflow_module  # noqa: PLC0415 - private resolution seam

    paths = _runtime_paths(tmp_path)
    config = _config()
    entity_ids(config, paths)
    context = replace(_delegate_runtime_context(config, paths, execution_identity=_identity()), persona_tools=("file",))

    request = workflow_module._participant_request(
        context,
        {"id": "p", "system_prompt": "P", "tools": ["file"]},
        workflow_id="w",
    )
    assert request.persona is not None
    assert request.persona.tools == ("file",)
    with pytest.raises(DynamicWorkflowError, match="unknown tool 'calculator'"):
        workflow_module._participant_request(
            context,
            {"id": "p", "system_prompt": "P", "tools": ["calculator"]},
            workflow_id="w",
        )
