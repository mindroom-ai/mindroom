"""Subagent lifecycle regressions through the actual MindRoom response envelope."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
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
        result = await toolkit.run_subagent("child", "Do the work")

    assert "Child completed the work." in result
    run = _only_run(tmp_path)
    assert run["status"] == "completed"
    assert run["output"] == "Child completed the work."
    assert run["error"] is None
    receipts = list(tmp_path.glob("agents/leader/workspace/.mindroom/delegation_receipts/*/*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("load_deferred", [False, True])
async def test_approved_child_finishes_through_its_normal_envelope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    load_deferred: bool,
) -> None:
    """An approved child keeps its instructions and uses a tool loaded after approval."""
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child", tools=["calculator", {"file": {"defer": True}}]),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "action": "require_approval"}]},
        prompts={"INTERACTIVE_QUESTION_PROMPT": "UNSUPPORTED_INTERACTIVE_MARKER"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    responses = [ModelResponse(tool_calls=[_call("add", "approved-add", a=1, b=2)])]
    if load_deferred:
        responses.extend(
            [
                ModelResponse(tool_calls=[_call("load_tool", "load-file", tool_name="file")]),
                ModelResponse(tool_calls=[_call("list_files", "list-files")]),
            ],
        )
    responses.append(ModelResponse(content="Child finished the requested work."))
    model = _InstructionRecordingModel(id="test", responses=responses)
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
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
                agent_name="leader",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
            )
            state = DelegationState.from_metadata(paused.metadata)
            assert paused.status == RunStatus.paused
            decisions = {str(tool["tool_call_id"]): True for tool in state.pending_tools}
            completed = await drive_delegations(
                parent,
                paused,
                agent_name="leader",
                config=config,
                runtime_paths=paths,
                execution_identity=identity,
                decisions=decisions,
                denial_reasons=dict.fromkeys(decisions),
            )

        assert completed.status == RunStatus.completed
        assert model.responses == []
        assert _only_run(tmp_path)["output"] == "Child finished the requested work."
        assert model.system_prompts
        assert all("UNSUPPORTED_INTERACTIVE_MARKER" not in prompt for prompt in model.system_prompts)
        if load_deferred:
            event_path = next(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/events.jsonl"))
            events = [json.loads(line) for line in event_path.read_text().splitlines()]
            assert any(
                event["kind"] == "tool_result" and event["data"]["tool_name"] == "list_files" for event in events
            )
    finally:
        storage.close()
