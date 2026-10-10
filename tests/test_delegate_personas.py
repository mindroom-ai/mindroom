"""Agents author subagent prompts, tool subsets, and profiles through run_subagent."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

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
from mindroom.delegation.lifecycle import MAX_DELEGATION_DEPTH, prepare_child_turn
from mindroom.delegation.sessions import reserve_subagent_turn, update_subagent_turn
from mindroom.delegation.state import DelegationState, SubagentPersona
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_envelopes import _InstructionRecordingModel
from tests.test_delegation_execution import DelegationModel, _call, _saved_approval_calls

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterator
    from pathlib import Path

    from agno.db.base import BaseDb

    from mindroom.constants import RuntimePaths

_CRITIC = """---
description: Finds the biggest risk.
tools: [file]
model: haiku
---
You are a hostile critic.
"""


def _config(*, tools: tuple[str | dict[str, object], ...] = ("file",), approval: bool = False) -> Config:
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


@contextmanager
def _native_parent(
    config: Config,
    paths: RuntimePaths,
    workspace: Path,
    responses: list[ModelResponse],
) -> Iterator[tuple[Agent, BaseDb]]:
    """A native-path leader whose delegate tool may run itself, scripted with ``responses``."""
    identity = _identity()
    toolkit = DelegateTools("leader", ["leader"], paths, config, execution_identity=identity, workspace_root=workspace)
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    try:
        model = DelegationModel(id="test-parent", responses=responses)
        yield Agent(name="leader", db=storage, tools=[toolkit], model=model), storage
    finally:
        storage.close()


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
        harness.toolkit.run_subagent(task="Review the plan", system_prompt="Inline {x} prompt", tools=["file"]),
    )

    assert "Reply 0." in result
    assert harness.model.system_prompts == ["Inline {x} prompt"]
    child = harness.session_child()
    assert child["child_agent_name"] == "leader"
    assert child["persona"] == {
        "source_kind": "inline",
        "source_name": "",
        "system_prompt": "Inline {x} prompt",
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
@pytest.mark.parametrize(
    ("arguments", "refusal"),
    [
        ({"profile": "absent"}, "Cannot delegate: subagent profile 'absent' was not found in subagents/."),
        (
            {"agent_name": "child", "system_prompt": "P"},
            "Cannot author a subagent for 'child': system_prompt, tools, and profile apply only to yourself.",
        ),
        (
            {"profile": "critic", "system_prompt": "P"},
            "Cannot delegate: pass either profile or system_prompt and tools",
        ),
        ({"profile": "critic", "tools": ["file"]}, "Cannot delegate: pass either profile or system_prompt and tools"),
        (
            {"system_prompt": "P", "tools": ["shell"]},
            "Cannot delegate: unknown tool 'shell'. Your tools: delegate, file",
        ),
    ],
)
async def test_invalid_authoring_is_refused_before_a_child_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, object],
    refusal: str,
) -> None:
    """A missing profile, another agent, a profile with inline fields, or an unknown tool is refused up front."""
    harness = _Harness(tmp_path, monkeypatch, _config())

    result = await harness.run(harness.toolkit.run_subagent(task="Do it", **arguments))

    assert result.startswith(refusal)
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


def test_run_subagent_description_lists_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller's run_subagent tool, which every model request carries, lists each saved profile and each one to fix."""
    _write_profile(tmp_path, "critic", _CRITIC)
    _write_profile(tmp_path, "broken", "---\ndescription: D\n---\n\n")
    harness = _Harness(tmp_path, monkeypatch, _config())

    description = harness.tools(["leader"]).async_functions["run_subagent"].description or ""

    assert "critic: Finds the biggest risk." in description
    assert "broken (invalid: " in description
    assert "critic" not in (harness.tools(["child"]).async_functions["run_subagent"].description or "")


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
@pytest.mark.parametrize(
    ("tools", "reduced", "refusal"),
    [
        (["file"], (), "Subagent tool 'file' is no longer available to you; start a new subagent."),
        (["file.save_file"], ({"file": {"include_tools": ["read_file"]}},), "'file.save_file' is not available to you"),
    ],
)
async def test_follow_up_after_caller_lost_a_tool_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tools: list[str],
    reduced: tuple[str | dict[str, object], ...],
    refusal: str,
) -> None:
    """A follow-up never runs once the caller lost a toolkit or function the persona names."""
    harness = _Harness(tmp_path, monkeypatch, _config())
    await harness.run(harness.toolkit.run_subagent(task="Work", system_prompt="P", tools=tools))
    subagent_id = str(harness.session_child()["subagent_id"])

    result = await harness.run(
        harness.toolkit.continue_subagent(subagent_id=subagent_id, message="Again"),
        config=_config(tools=reduced),
    )

    assert refusal in result
    assert harness.model.system_prompts == ["P"]


@pytest.mark.asyncio
async def test_persona_naming_a_function_its_caller_lacks_never_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A function the caller's configuration removes is refused instead of starting a child without it."""
    harness = _Harness(tmp_path, monkeypatch, _config(tools=({"file": {"include_tools": ["read_file"]}},)))

    result = await harness.run(
        harness.toolkit.run_subagent(task="Save it", system_prompt="P", tools=["file.save_file"]),
    )

    assert "'file.save_file' is not available to you" in result
    assert harness.model.system_prompts == []


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
    model = _InstructionRecordingModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("add", "approved-add", a=1, b=2)]),
            ModelResponse(content="Sum is 3."),
        ],
    )
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    identity = _identity()
    options = {
        "run_child": run_delegated_child_response,
        "agent_name": "leader",
        "config": config,
        "runtime_paths": paths,
        "execution_identity": identity,
    }
    parent_responses = [
        ModelResponse(tool_calls=[_call("run_subagent", "delegate", task="Add 1 and 2", profile="adder")]),
        ModelResponse(content="Parent finished."),
    ]
    with (
        _native_parent(config, paths, _workspace(tmp_path), parent_responses) as (parent, storage),
        tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)),
    ):
        response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
        paused = await drive_delegations(parent, response, **options)
        assert paused.status == RunStatus.paused
        state = DelegationState.from_metadata(paused.metadata)
        profile.unlink()
        persisted = storage.get_run(paused.run_id)
        assert isinstance(persisted, RunOutput)
        decisions = {str(tool["tool_call_id"]): True for tool in state.pending_tools}
        completed = await drive_delegations(
            parent,
            persisted,
            decisions=decisions,
            denial_reasons=dict.fromkeys(decisions),
            approval_calls=_saved_approval_calls(state),
            **options,
        )

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


@pytest.mark.asyncio
async def test_nested_persona_stays_within_parent_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An authored subagent with delegate can author copies only within its own tools."""
    harness = _Harness(tmp_path, monkeypatch, _config(tools=("file", "calculator")))
    harness.model.responses = [
        ModelResponse(tool_calls=[_call("run_subagent", "n1", task="Add.", system_prompt="Q", tools=["calculator"])]),
        ModelResponse(tool_calls=[_call("run_subagent", "n2", task="Plain copy.")]),
        ModelResponse(tool_calls=[_call("run_subagent", "n3", task="Read.", system_prompt="Q")]),
        ModelResponse(content="Grandchild answer."),
        ModelResponse(content="Child answer."),
    ]

    result = await harness.run(
        harness.toolkit.run_subagent(task="Coordinate.", system_prompt="P", tools=["delegate", "file"]),
    )

    assert "Child answer." in result
    assert harness.model.system_prompts == ["P", "P", "P", "Q", "P"]
    records = [
        json.loads(path.read_text())["child"]
        for path in (harness.paths.storage_root / "subagent_sessions").glob("*.json")
    ]
    grandchild = next(record for record in records if record["persona"]["system_prompt"] == "Q")
    assert grandchild["persona"]["tools"] == ["delegate", "file"]
    assert len(records) == 2
    tool_results = [str(message.content) for message in harness.model.seen_messages if message.role == "tool"]
    assert any(
        "Cannot delegate: unknown tool 'calculator'. Your tools: delegate, file." in text for text in tool_results
    )
    assert any("pass system_prompt or profile so the copy stays within your tools" in text for text in tool_results)


@pytest.mark.asyncio
async def test_copy_at_max_depth_inherits_tools_without_delegate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy started at the maximum depth inherits its parent's tools except delegate, which it cannot have."""
    harness = _Harness(tmp_path, monkeypatch, _config(tools=("file", "calculator")))
    harness.toolkit = DelegateTools(
        "leader",
        ["leader", "child"],
        harness.paths,
        harness.config,
        execution_identity=harness.identity,
        workspace_root=_workspace(harness.paths.storage_root),
        delegation_depth=MAX_DELEGATION_DEPTH - 2,
    )
    harness.model.responses = [
        ModelResponse(tool_calls=[_call("run_subagent", "n1", task="Read.", system_prompt="Q")]),
        ModelResponse(content="Grandchild answer."),
        ModelResponse(content="Child answer."),
    ]

    result = await harness.run(
        harness.toolkit.run_subagent(task="Coordinate.", system_prompt="P", tools=["delegate", "file"]),
    )

    assert "Child answer." in result
    assert harness.model.system_prompts == ["P", "Q", "P"]
    records = [
        json.loads(path.read_text())["child"]
        for path in (harness.paths.storage_root / "subagent_sessions").glob("*.json")
    ]
    grandchild = next(record for record in records if record["persona"]["system_prompt"] == "Q")
    assert grandchild["persona"]["tools"] == ["file"]


@pytest.mark.asyncio
async def test_native_copy_at_max_depth_inherits_tools_without_delegate(tmp_path: Path) -> None:
    """On the native path a copy started at the maximum depth inherits its parent's tools except delegate."""
    config = _config(tools=("file", "calculator"))
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=identity),
        persona_tools=("delegate", "file"),
    )

    with tool_runtime_context(context):
        target = await _resolve_delegation_target(
            ToolExecution(tool_name="run_subagent", tool_args={"task": "Read.", "system_prompt": "Q"}),
            None,
            caller_identity=identity,
            config=config,
            runtime_paths=paths,
            depth=MAX_DELEGATION_DEPTH - 1,
        )

    assert not isinstance(target, str), target
    assert target.persona is not None
    assert target.persona.tools == ("file",)


@pytest.mark.asyncio
async def test_native_follow_up_checks_the_current_config(tmp_path: Path) -> None:
    """A native follow-up refuses once the caller's current config lost a persona tool."""
    paths = _runtime_paths(tmp_path)
    with_file = _config()
    without_file = _config(tools=())
    identity = _identity()
    child = prepare_child_turn(
        "leader",
        "leader",
        "Read.",
        owner=identity,
        config=with_file,
        runtime_paths=paths,
        depth=0,
        persona=SubagentPersona(source_kind="inline", source_name="", system_prompt="P", tools=("file",)),
    )
    await reserve_subagent_turn(child, owner=identity, runtime_paths=paths)
    child.status = "completed"
    await update_subagent_turn(child, paths)
    follow_up = ToolExecution(
        tool_name="continue_subagent",
        tool_args={"subagent_id": child.subagent_id, "message": "Again"},
    )

    with tool_runtime_context(_delegate_runtime_context(without_file, paths, execution_identity=identity)):
        result = await _resolve_delegation_target(
            follow_up,
            None,
            caller_identity=identity,
            config=with_file,
            runtime_paths=paths,
            depth=0,
        )

    assert result == "Subagent tool 'file' is no longer available to you; start a new subagent."


@pytest.mark.asyncio
async def test_native_nested_persona_stays_within_running_child_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the native path an authored child's tools cap the copies it authors, and it cannot start a plain copy."""
    config = _config(tools=("file", "calculator"))
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    model = _InstructionRecordingModel(
        id="test",
        responses=[
            ModelResponse(
                tool_calls=[_call("run_subagent", "n1", task="Add.", system_prompt="Q", tools=["calculator"])],
            ),
            ModelResponse(tool_calls=[_call("run_subagent", "n2", task="Plain copy.")]),
            ModelResponse(tool_calls=[_call("run_subagent", "n3", task="Read.", system_prompt="Q")]),
            ModelResponse(content="Grandchild answer."),
            ModelResponse(content="Child finished."),
        ],
    )
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    identity = _identity()
    parent_responses = [
        ModelResponse(
            tool_calls=[
                _call("run_subagent", "delegate", task="Coordinate.", system_prompt="P", tools=["delegate", "file"]),
            ],
        ),
        ModelResponse(content="Parent finished."),
    ]
    with (
        _native_parent(config, paths, _workspace(tmp_path), parent_responses) as (parent, _storage),
        tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)),
    ):
        response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
        completed = await drive_delegations(
            parent,
            response,
            run_child=run_delegated_child_response,
            agent_name="leader",
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
        )

    assert completed.status == RunStatus.completed
    assert model.system_prompts == ["P", "P", "P", "Q", "P"]
    records = [
        json.loads(path.read_text())["child"] for path in (paths.storage_root / "subagent_sessions").glob("*.json")
    ]
    assert len(records) == 2
    grandchild = next(record for record in records if record["persona"]["system_prompt"] == "Q")
    assert grandchild["persona"]["tools"] == ["delegate", "file"]
    tool_results = [str(message.content) for message in model.seen_messages if message.role == "tool"]
    assert any(
        "Cannot delegate: unknown tool 'calculator'. Your tools: delegate, file." in text for text in tool_results
    )
    assert any("stays within your tools" in text for text in tool_results)


@pytest.mark.asyncio
async def test_fresh_direct_persona_checks_the_current_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh authored subagent is validated against the caller's current tools, not the toolkit's build-time config."""
    harness = _Harness(tmp_path, monkeypatch, _config())

    result = await harness.run(
        harness.toolkit.run_subagent(task="Read.", system_prompt="P", tools=["file"]),
        config=_config(tools=()),
    )

    assert result.startswith("Cannot delegate: unknown tool 'file'. Your tools: ")
    assert harness.model.system_prompts == []


@pytest.mark.asyncio
async def test_plain_delegation_never_lists_caller_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Delegating without authoring arguments does not depend on resolving the caller's tool surface."""
    harness = _Harness(tmp_path, monkeypatch, _config())

    def unavailable(*_args: object, **_kwargs: object) -> list[str]:
        msg = "caller tools must not be listed for a plain delegation"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.custom_tools.delegate.caller_toolkit_names", unavailable)
    monkeypatch.setattr("mindroom.delegation.execution.caller_toolkit_names", unavailable)

    result = await harness.run(harness.toolkit.run_subagent(task="Summarize.", agent_name="child"))

    assert "Reply 0." in result
