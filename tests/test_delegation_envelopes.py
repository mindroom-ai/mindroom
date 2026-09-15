"""Subagent lifecycle regressions through the actual MindRoom response envelope."""

from __future__ import annotations

import asyncio
import json
import threading
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
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.records import DelegationRecordOwner
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import ApprovalCall
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity, _only_run
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.delegation.records import DelegationMetadata, DelegationRecordHandle
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


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


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_cancel_during_audit_creation_settles_record_and_receipt(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
) -> None:
    """Cancelling a durable startup write cannot strand an audit without its locator."""
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    identity = _identity()
    toolkit = DelegateTools("leader", ["child"], paths, config, execution_identity=identity)
    apply_tool_approval_capability(
        toolkit,
        config,
        supports_native_tool_approval=native,
        registered_tool_name="delegate",
    )
    storage = create_session_storage("leader", config, paths, identity)
    parent = Agent(
        name="leader",
        db=storage,
        tools=[toolkit],
        model=DelegationModel(
            id="test-parent",
            responses=[
                ModelResponse(tool_calls=[_call("run_subagent", "delegate", agent_name="child", task="Inspect")]),
            ],
        ),
    )
    created = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_start = DelegationRecordOwner._start

    def delayed_start(
        owner: DelegationRecordOwner,
        metadata: DelegationMetadata,
        *,
        caller_execution_identity: ToolExecutionIdentity | None,
        child_execution_identity: ToolExecutionIdentity | None,
        delegation_id: str | None,
    ) -> DelegationRecordHandle:
        handle = original_start(
            owner,
            metadata,
            caller_execution_identity=caller_execution_identity,
            child_execution_identity=child_execution_identity,
            delegation_id=delegation_id,
        )
        loop.call_soon_threadsafe(created.set)
        if not release.wait(10):
            msg = "Audit creation was not released by the test"
            raise TimeoutError(msg)
        return handle

    monkeypatch.setattr(DelegationRecordOwner, "_start", delayed_start)
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            if native:
                response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
                operation = drive_delegations(
                    parent,
                    response,
                    run_child=run_delegated_child_response,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=identity,
                )
            else:
                operation = toolkit.run_subagent(agent_name="child", task="Inspect")
            task = asyncio.create_task(operation)
            try:
                await asyncio.wait_for(created.wait(), timeout=10)
                task.cancel()
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        run = _only_run(tmp_path)
        assert run["status"] == "cancelled"
        receipts = list(tmp_path.glob("agents/leader/workspace/.mindroom/delegation_receipts/*/*.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text())["status"] == "cancelled"
        if native:
            retained = storage.get_run(response.run_id)
            assert isinstance(retained, RunOutput)
            child = DelegationState.from_metadata(retained.metadata).children[0]
            assert child.status == "cancelled"
            assert child.record_locator
        else:
            handle_path = next((paths.storage_root / "subagent_sessions").glob("*.json"))
            child_payload = json.loads(handle_path.read_text())["child"]
            assert child_payload["status"] == "cancelled"
            assert child_payload["record_locator"]
    finally:
        release.set()
        storage.close()


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
                assert {source.toolkit_name for source in state.pending_tool_sources.values()} == {"calculator"}
                approval_calls = tuple(
                    ApprovalCall(
                        tool_call_id=str(tool["tool_call_id"]),
                        tool_name=str(tool["tool_name"]),
                        invoking_agent="child",
                        toolkit_name="calculator",
                        expires_at_ns=2**62,
                    )
                    for tool in state.pending_tools
                )
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
                    approval_calls=approval_calls,
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
