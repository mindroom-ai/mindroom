"""Agents author subagent prompts, tool subsets, and profiles through run_subagent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse, ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.ai import run_delegated_child_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.execution import _resolve_delegation_target, drive_delegations
from mindroom.delegation.personas import PersonaRequest, resolve_persona_request
from mindroom.delegation.state import DelegationState, SubagentPersona
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_envelopes import _InstructionRecordingModel
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_CRITIC = """---
description: Finds the biggest risk.
tools: [file]
model: haiku
---
You are a hostile critic.
"""


def _config(*, tools: tuple[str, ...] = ("file",), approval: bool = False) -> Config:
    return Config(
        agents={
            "leader": AgentConfig(
                display_name="Leader",
                role="Configured role",
                tools=list(tools),
                delegate_to=["leader", "child"],
                memory_backend="file",
            ),
            "child": AgentConfig(display_name="Child"),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "action": "require_approval"}] if approval else []},
        models={
            "default": ModelConfig(provider="test", id="default-model"),
            "haiku": ModelConfig(provider="test", id="haiku-model"),
            "sonnet": ModelConfig(provider="test", id="sonnet-model"),
        },
    )


def _workspace(tmp_path: Path) -> Path:
    return tmp_path / "agents" / "leader" / "workspace"


def _write_profile(tmp_path: Path, name: str, content: str) -> Path:
    path = _workspace(tmp_path) / "subagents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@dataclass
class _ToolRecordingModel(_InstructionRecordingModel):
    """Also keep the function names offered on each child request."""

    offered: list[list[str]] = field(default_factory=list)

    async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
        tools = cast("list[dict[str, Any]]", kwargs.get("tools") or [])
        self.offered.append(sorted(str(tool.get("function", tool).get("name")) for tool in tools))
        return await super().ainvoke(*args, **kwargs)


class _Harness:
    """One leader with a scripted child model, recording each model the child loads."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: Config, replies: int = 2) -> None:
        self.paths: RuntimePaths = _runtime_paths(tmp_path)
        self.config = config
        entity_ids(config, self.paths)
        self.model = _InstructionRecordingModel(
            id="test",
            responses=[ModelResponse(content=f"Reply {index}.") for index in range(replies)],
        )
        self.models_loaded: list[str] = []

        def load_model(*args: object) -> _InstructionRecordingModel:
            self.models_loaded.append(str(args[2]))
            return self.model

        monkeypatch.setattr("mindroom.agents._load_agent_model_instance", load_model)
        self.identity = _identity()
        self.toolkit = self.tools(["leader", "child"])

    def tools(self, delegate_to: list[str]) -> DelegateTools:
        return DelegateTools(
            "leader",
            delegate_to,
            self.paths,
            self.config,
            execution_identity=self.identity,
            workspace_root=_workspace(self.paths.storage_root),
        )

    async def run(self, call: Awaitable[str], config: Config | None = None) -> str:
        context = _delegate_runtime_context(config or self.config, self.paths, execution_identity=self.identity)
        with tool_runtime_context(context):
            return await call

    def session_child(self) -> dict[str, object]:
        [record] = (self.paths.storage_root / "subagent_sessions").glob("*.json")
        return json.loads(record.read_text())["child"]


@pytest.mark.asyncio
async def test_inline_persona_starts_self_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An inline prompt and tool subset start a fresh copy of the caller with exactly that prompt."""
    harness = _Harness(tmp_path, monkeypatch, _config())

    result = await harness.run(
        harness.toolkit.run_subagent(task="Review the plan", system_prompt="Inline prompt", tools=["file"]),
    )

    assert "Reply 0." in result
    assert harness.model.system_prompts == ["Inline prompt"]
    child = harness.session_child()
    assert child["child_agent_name"] == "leader"
    assert child["persona"] == {
        "source_kind": "inline",
        "source_name": "",
        "system_prompt": "Inline prompt",
        "tools": ["file"],
    }


@pytest.mark.asyncio
async def test_profile_persona_uses_profile_prompt_and_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile supplies the prompt and model; an explicit model argument overrides the profile model."""
    _write_profile(tmp_path, "critic", _CRITIC)
    harness = _Harness(tmp_path, monkeypatch, _config())

    await harness.run(harness.toolkit.run_subagent(task="Find the risk", profile="critic"))
    await harness.run(harness.toolkit.run_subagent(task="Find another risk", profile="critic", model="sonnet"))

    assert harness.model.system_prompts == ["You are a hostile critic.", "You are a hostile critic."]
    assert harness.models_loaded == ["haiku", "sonnet"]


@pytest.mark.asyncio
async def test_unknown_profile_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing profile names the subagents/ directory and runs nothing."""
    harness = _Harness(tmp_path, monkeypatch, _config())
    result = await harness.run(harness.toolkit.run_subagent(task="Find the risk", profile="absent"))
    assert result == "Cannot delegate: subagent profile 'absent' was not found in subagents/."
    assert harness.model.system_prompts == []


@pytest.mark.asyncio
async def test_persona_for_other_agent_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Authoring applies only to the caller itself, never to another agent's tools."""
    harness = _Harness(tmp_path, monkeypatch, _config())
    result = await harness.run(harness.toolkit.run_subagent(task="Do it", agent_name="child", system_prompt="P"))
    assert result == "Cannot author a subagent for 'child': system_prompt, tools, and profile apply only to yourself."
    assert harness.model.system_prompts == []


@pytest.mark.asyncio
async def test_profile_and_inline_conflict_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A profile cannot be combined with an inline prompt or tool list."""
    _write_profile(tmp_path, "critic", _CRITIC)
    harness = _Harness(tmp_path, monkeypatch, _config())
    for extra in ({"system_prompt": "P"}, {"tools": ["file"]}):
        result = await harness.run(harness.toolkit.run_subagent(task="Do it", profile="critic", **extra))
        assert result == "Cannot delegate: pass either profile or system_prompt and tools, not both."


@pytest.mark.asyncio
async def test_unknown_tool_is_refused_with_caller_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A persona cannot name a tool the caller lacks."""
    harness = _Harness(tmp_path, monkeypatch, _config())
    result = await harness.run(harness.toolkit.run_subagent(task="Do it", system_prompt="P", tools=["shell"]))
    assert result.startswith("Cannot delegate: unknown tool 'shell'. Your tools: ")
    assert "file" in result
    assert harness.model.system_prompts == []


def test_persona_parameters_hidden_without_self_delegation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a caller that may run itself sees the authoring parameters."""
    harness = _Harness(tmp_path, monkeypatch, _config())
    other_only = harness.tools(["child"])
    self_allowed = harness.tools(["leader", "child"])

    def properties(toolkit: DelegateTools) -> set[str]:
        return set(toolkit.async_functions["run_subagent"].parameters["properties"])

    assert {"system_prompt", "tools", "profile"}.isdisjoint(properties(other_only))
    assert {"system_prompt", "tools", "profile"} <= properties(self_allowed)
    assert "system_prompt" not in other_only.async_functions["run_subagent"].description
    assert "system_prompt" in self_allowed.async_functions["run_subagent"].description


def test_instructions_list_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller sees each saved profile and each one it must fix."""
    _write_profile(tmp_path, "critic", _CRITIC)
    _write_profile(tmp_path, "broken", "---\ndescription: D\n---\n\n")
    harness = _Harness(tmp_path, monkeypatch, _config())

    instructions = harness.tools(["leader"]).instructions or ""

    assert "critic: Finds the biggest risk." in instructions
    assert "broken (invalid: " in instructions
    assert "critic" not in (harness.tools(["child"]).instructions or "")


@pytest.mark.asyncio
async def test_follow_up_keeps_snapshot_after_profile_edit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Editing a profile never changes a running subagent's prompt."""
    profile = _write_profile(tmp_path, "critic", _CRITIC)
    harness = _Harness(tmp_path, monkeypatch, _config())
    await harness.run(harness.toolkit.run_subagent(task="Find the risk", profile="critic"))
    subagent_id = str(harness.session_child()["subagent_id"])
    profile.write_text(_CRITIC.replace("hostile critic", "friendly helper"))

    await harness.run(harness.toolkit.continue_subagent(subagent_id=subagent_id, message="And another?"))

    assert harness.model.system_prompts == ["You are a hostile critic.", "You are a hostile critic."]


@pytest.mark.asyncio
async def test_follow_up_after_caller_lost_tool_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A follow-up never runs once the caller lost a tool the persona names."""
    harness = _Harness(tmp_path, monkeypatch, _config())
    await harness.run(harness.toolkit.run_subagent(task="Read", system_prompt="P", tools=["file"]))
    subagent_id = str(harness.session_child()["subagent_id"])

    result = await harness.run(
        harness.toolkit.continue_subagent(subagent_id=subagent_id, message="Again"),
        config=_config(tools=()),
    )

    assert result == "Subagent tool 'file' is no longer available to you; start a new subagent."
    assert harness.model.system_prompts == ["P"]


def test_resolve_persona_request_mode_and_model_rules(tmp_path: Path) -> None:
    """Profiles supply model and mode; explicit arguments override them; plain calls pass through."""
    _write_profile(tmp_path, "fast", "---\ndescription: D\nmode: minimal\nmodel: haiku\n---\nBe quick.\n")
    _write_profile(tmp_path, "plain", "---\ndescription: D\n---\nBe careful.\n")
    options = {
        "caller_name": "leader",
        "agent_name": "leader",
        "system_prompt": None,
        "tools": None,
        "workspace_root": _workspace(tmp_path),
        "available_toolkits": ["file"],
    }

    fast = resolve_persona_request(profile="fast", model=None, minimal=False, **options)
    plain_minimal = resolve_persona_request(profile="plain", model="sonnet", minimal=True, **options)
    configured = resolve_persona_request(profile=None, model=None, minimal=True, **options)

    assert isinstance(fast, PersonaRequest)
    assert (fast.model, fast.agent_mode, fast.persona.system_prompt if fast.persona else None) == (
        "haiku",
        "minimal",
        "Be quick.",
    )
    assert isinstance(plain_minimal, PersonaRequest)
    assert (plain_minimal.model, plain_minimal.agent_mode) == ("sonnet", "minimal")
    assert configured == PersonaRequest(persona=None, model=None, agent_mode="minimal")
    no_workspace = resolve_persona_request(
        profile="fast",
        model=None,
        minimal=False,
        **{**options, "workspace_root": None},
    )
    assert no_workspace == "Cannot delegate: subagent profiles need an agent workspace."


@pytest.mark.asyncio
async def test_native_team_member_authors_persona_for_itself(tmp_path: Path) -> None:
    """On the native path the caller is the requirement's own member, so its persona is for itself."""
    config = _config()
    paths = _runtime_paths(tmp_path)
    member_identity = replace(_identity(), agent_name="child")
    tool = ToolExecution(tool_name="run_subagent", tool_args={"task": "Check", "system_prompt": "Member prompt"})

    target = await _resolve_delegation_target(
        tool,
        None,
        caller_identity=member_identity,
        config=config,
        runtime_paths=paths,
        depth=0,
    )
    refused = await _resolve_delegation_target(
        ToolExecution(
            tool_name="run_subagent",
            tool_args={"task": "Check", "agent_name": "leader", "system_prompt": "Member prompt"},
        ),
        None,
        caller_identity=member_identity,
        config=config,
        runtime_paths=paths,
        depth=0,
    )

    assert not isinstance(target, str)
    assert target.agent_name == "child"
    assert target.persona == SubagentPersona(source_kind="inline", source_name="", system_prompt="Member prompt")
    assert refused == "Cannot author a subagent for 'leader': system_prompt, tools, and profile apply only to yourself."


@pytest.mark.asyncio
async def test_native_resume_uses_frozen_persona_after_profile_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile child paused for approval resumes with its frozen persona after the file is deleted."""
    profile = _write_profile(
        tmp_path,
        "adder",
        "---\ndescription: Adds numbers.\ntools: [calculator]\n---\nYou add numbers.\n",
    )
    config = _config(tools=("calculator", "file"), approval=True)
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    model = _ToolRecordingModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("add", "approved-add", a=1, b=2)]),
            ModelResponse(content="Sum is 3."),
        ],
    )
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    identity = _identity()
    toolkit = DelegateTools(
        "leader",
        ["leader"],
        paths,
        config,
        execution_identity=identity,
        workspace_root=_workspace(tmp_path),
    )
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    parent = Agent(
        name="leader",
        db=storage,
        tools=[toolkit],
        model=DelegationModel(
            id="test-parent",
            responses=[
                ModelResponse(tool_calls=[_call("run_subagent", "delegate", task="Add 1 and 2", profile="adder")]),
                ModelResponse(content="Parent finished."),
            ],
        ),
    )
    options = {
        "run_child": run_delegated_child_response,
        "agent_name": "leader",
        "config": config,
        "runtime_paths": paths,
        "execution_identity": identity,
    }
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
            paused = await drive_delegations(parent, response, **options)
            assert paused.status == RunStatus.paused
            state = DelegationState.from_metadata(paused.metadata)
            profile.unlink()
            persisted = storage.get_run(paused.run_id)
            assert isinstance(persisted, RunOutput)
            decisions = {str(tool["tool_call_id"]): True for tool in state.pending_tools}
            approval_calls = tuple(
                ApprovalCall(
                    tool_call_id=str(tool["tool_call_id"]),
                    tool_name=str(tool["tool_name"]),
                    invoking_agent="leader",
                    toolkit_name="calculator",
                    expires_at_ns=2**62,
                    arguments_digest=approval_arguments_digest(tool["tool_args"]),
                )
                for tool in state.pending_tools
            )
            completed = await drive_delegations(
                parent,
                persisted,
                decisions=decisions,
                denial_reasons=dict.fromkeys(decisions),
                approval_calls=approval_calls,
                **options,
            )
    finally:
        storage.close()

    assert completed.status == RunStatus.completed
    assert model.system_prompts == ["You add numbers.", "You add numbers."]
    assert len(model.offered) == 2
    assert all("add" in offered and "read_file" not in offered for offered in model.offered)
    assert DelegationState.from_metadata(completed.metadata).children[0].persona == SubagentPersona(
        source_kind="profile",
        source_name="adder",
        system_prompt="You add numbers.",
        tools=("calculator",),
    )
