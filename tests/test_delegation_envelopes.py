"""Subagent lifecycle regressions through the actual MindRoom response envelope."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput, ToolCallCompletedEvent
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.ai import run_delegated_child_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation_execution import drive_delegations
from mindroom.delegation_state import DelegationState
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity, _only_run
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _InstructionRecordingModel(DelegationModel):
    """Keep every child system prompt, including its first resumed model request."""

    system_prompts: list[str] = field(default_factory=list)

    async def ainvoke(self, *_args: object, **kwargs: object) -> ModelResponse:
        if not self.responses:
            msg = "Child provider unavailable."
            raise RuntimeError(msg)
        response = await super().ainvoke(*_args, **kwargs)
        self.system_prompts.append(
            "\n".join(str(message.content) for message in self.seen_messages if message.role == "system"),
        )
        return response


@pytest.mark.asyncio
async def test_direct_subagent_success_keeps_real_terminal_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real child envelope must complete its audit with its actual final response."""
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    model = DelegationModel(id="test", responses=[ModelResponse(content="Child completed the work.")])
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    identity = _identity()
    toolkit = DelegateTools("leader", ["child"], paths, config, execution_identity=identity)

    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        result = await toolkit.run_subagent(agent_name="child", task="Do the work")

    assert "Child completed the work." in result
    run = _only_run(tmp_path)
    assert run["status"] == "completed"
    assert run["output"] == "Child completed the work."
    assert run["error"] is None
    receipts = list(tmp_path.glob("agents/leader/workspace/.mindroom/delegation_receipts/*/*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["status"] == "completed"


def _continuation_responses(continuation: str) -> list[ModelResponse]:
    """Plan provider events for approval, nested work, deferred tools, and failures."""
    responses = [ModelResponse(tool_calls=[_call("add", "approved-add", a=1, b=2)])]
    if continuation == "model_switch":
        responses.insert(
            0,
            ModelResponse(
                tool_calls=[
                    _call("switch_thread_model", "switch-model", model_name="alternate", when="after-toolcall"),
                ],
            ),
        )
    if continuation == "nested_deferred_tool":
        responses.insert(
            0,
            ModelResponse(tool_calls=[_call("run_subagent", "nested", agent_name="child", task="Add then list files")]),
        )
    if continuation in {"deferred_tool", "nested_deferred_tool", "preparation_error"}:
        responses.extend(
            [
                ModelResponse(tool_calls=[_call("load_tool", "load-file", tool_name="file")]),
                # A later run may reuse the approved call's local ID.
                ModelResponse(tool_calls=[_call("list_files", "approved-add")]),
            ],
        )
    if continuation == "nested_subagent":
        responses.extend(
            [
                ModelResponse(tool_calls=[_call("run_subagent", "nested", agent_name="child", task="Check the sum")]),
                ModelResponse(content="Nested check finished."),
            ],
        )
    if continuation == "nested_deferred_tool":
        responses.append(ModelResponse(content="Nested check finished."))
    if continuation != "provider_error":
        responses.append(ModelResponse(content="Child finished the requested work."))
    return responses


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("continuation", "approval"),
    [
        ("none", True),
        ("deferred_tool", True),
        ("nested_subagent", True),
        ("nested_deferred_tool", True),
        ("model_switch", True),
        ("provider_error", True),
        ("provider_error", False),
        ("preparation_error", True),
    ],
)
async def test_native_child_finishes_through_its_normal_envelope(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    continuation: str,
    approval: bool,
) -> None:
    """Native children settle real failures and preserve tool results across continuation."""
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(
                display_name="Child",
                tools=["calculator", {"file": {"defer": True}}, "thread_model"],
                delegate_to=["child"],
            ),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "action": "require_approval"}] if approval else []},
        prompts={"INTERACTIVE_QUESTION_PROMPT": "UNSUPPORTED_INTERACTIVE_MARKER"},
        models={
            "default": ModelConfig(provider="test", id="default-model"),
            "alternate": ModelConfig(provider="test", id="alternate-model"),
        },
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    model = _InstructionRecordingModel(id="test", responses=_continuation_responses(continuation))
    models_loaded: list[str] = []

    def load_model(*_args: object) -> _InstructionRecordingModel:
        assert isinstance(_args[2], str)
        models_loaded.append(_args[2])
        if continuation == "preparation_error" and len(model.system_prompts) >= 2:
            msg = "Child model preparation failed."
            raise RuntimeError(msg)
        return model

    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", load_model)
    identity = _identity()
    toolkit = DelegateTools("leader", ["child"], paths, config, execution_identity=identity)
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    parent = Agent(
        name="leader",
        db=storage,
        tools=[toolkit],
        model=DelegationModel(
            id="test-parent",
            responses=[
                ModelResponse(
                    tool_calls=[_call("run_subagent", "delegate", agent_name="child", task="Add then inspect files")],
                ),
                ModelResponse(content="Parent finished."),
            ],
        ),
    )
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
            paused = await drive_delegations(
                parent,
                response,
                run_child=run_delegated_child_response,
                agent_name="leader",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
            )
            state = DelegationState.from_metadata(paused.metadata)
            events: list[object] = []
            completed = paused
            if approval:
                assert paused.status == RunStatus.paused
                persisted = storage.get_run(paused.run_id)
                assert isinstance(persisted, RunOutput)
                decisions = {str(tool["tool_call_id"]): True for tool in state.pending_tools}
                completed = await drive_delegations(
                    parent,
                    persisted,
                    run_child=run_delegated_child_response,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=identity,
                    decisions=decisions,
                    denial_reasons=dict.fromkeys(decisions),
                    on_event=events.append,
                )

        assert completed.status == RunStatus.completed
        if continuation == "model_switch":
            assert state.children[0].model_name == "alternate"
            assert models_loaded == ["default", "alternate", "alternate"]
        child_id = state.children[0].delegation_id
        run_path = next(tmp_path.glob(f"agents/child/workspace/.mindroom/delegations/*/{child_id}/run.json"))
        if continuation in {"provider_error", "preparation_error"}:
            settled = DelegationState.from_metadata(completed.metadata).children[0]
            assert settled.status == "failed"
            assert json.loads(run_path.read_text())["status"] == "failed"
            assert settled.result
            assert any(
                "failed" in str(tool.result) for tool in completed.tools or () if tool.tool_name == "run_subagent"
            )
            return
        assert model.responses == []
        assert json.loads(run_path.read_text())["output"] == "Child finished the requested work."
        approved = next(
            event.tool
            for event in events
            if isinstance(event, ToolCallCompletedEvent) and event.tool is not None and event.tool.tool_name == "add"
        )
        assert not approved.tool_call_error
        assert "3" in str(approved.result)
        assert model.system_prompts
        assert all("UNSUPPORTED_INTERACTIVE_MARKER" not in prompt for prompt in model.system_prompts)
        if continuation in {"deferred_tool", "nested_deferred_tool"}:
            events = [
                json.loads(line)
                for event_path in tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/events.jsonl")
                for line in event_path.read_text().splitlines()
            ]
            assert any(
                event["kind"] == "tool_result" and event["data"]["tool_name"] == "list_files" for event in events
            )
    finally:
        storage.close()
