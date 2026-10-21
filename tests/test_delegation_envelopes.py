"""Subagent lifecycle regressions through the actual MindRoom response envelope."""

from __future__ import annotations

import asyncio
import json
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom import agents
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.delegation.records import DelegationRecordOwner
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.llm.errors import ProviderError
from mindroom.llm.runs import RunStatus
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.delegation_helpers import (
    ScriptedAgents,
    _deferred_call,
    _delegate_runtime_context,
    _runtime_paths,
    approval_executor,
    pause_parent,
    respond,
    resume,
    stored_run,
    system_prompts,
)
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.test_delegation_direct_audit import _identity, _only_run

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.records import DelegationMetadata, DelegationRecordHandle
    from mindroom.delegation.state import DelegationChild
    from mindroom.llm.provider import ModelProvider
    from mindroom.llm.runs import ToolExecution
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


def _leader_and_child(*, gate: str | None = None) -> Config:
    return Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": gate, "action": "require_approval"}] if gate else []},
    )


def _child_tools(child: DelegationChild, config: Config, paths: RuntimePaths) -> list[ToolExecution]:
    """Return every tool call the ``child`` agent's store recorded, across its own and its nested runs."""
    store = create_session_storage("child", config, paths, child_execution_identity(child))
    return [
        tool
        for session_id, _updated in store.list_updated_sessions("agent", "child", since=-1)
        for run in store.list_runs(session_id)
        for tool in run.tools or ()
    ]


@pytest.mark.asyncio
async def test_direct_subagent_success_keeps_real_terminal_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real child envelope must complete its audit with its actual final response."""
    config = _leader_and_child()
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    ScriptedAgents({"child": ScriptedModel([answer("Child completed the work.")])}).install(monkeypatch)
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
    config = _leader_and_child()
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
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

    async def unused_child(*_args: object, **_kwargs: object) -> str:
        pytest.fail("The child must not start before its audit exists")

    parent_model = ScriptedModel([calls(tool_call("run_subagent", "delegate", agent_name="child", task="Inspect"))])
    ScriptedAgents({}, by_session={identity.session_id: parent_model}).install(monkeypatch)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        parent = await pause_parent("leader", "Delegate", config=config, runtime_paths=paths, identity=identity)
    monkeypatch.setattr(DelegationRecordOwner, "_start", delayed_start)
    if native:
        # The parent's run stored its pause; the driver owns the delegation from there.
        operation = drive_delegations(
            parent.record,
            store=parent.store,
            session=parent.session,
            continue_parent=parent.runner.continue_run,
            agent_name="leader",
            run_child=unused_child,
            config=config,
            runtime_paths=paths,
            execution_identity=identity,
        )
    else:
        toolkit = DelegateTools("leader", ["child"], paths, config, execution_identity=identity)
        operation = toolkit.run_subagent(agent_name="child", task="Inspect")
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
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
        retained = parent.store.get_run("parent-run")
        assert retained is not None
        child = DelegationState.from_metadata(retained.metadata).children[0]
        assert child.status == "cancelled"
        assert child.record_locator
    else:
        handle_path = next((paths.storage_root / "subagent_sessions").glob("*.json"))
        child_payload = json.loads(handle_path.read_text())["child"]
        assert child_payload["status"] == "cancelled"
        assert child_payload["record_locator"]


def _provider_unavailable() -> list[ScriptStep]:
    async def fail() -> None:
        msg = "Child provider unavailable."
        raise ProviderError(msg, provider="Synthetic")

    return [Await(fail)]


def _continuation_turns(continuation: str) -> list[list[ScriptStep]]:
    """Script the child's requests for approval, nested work, deferred tools, and failures."""
    turns = [calls(tool_call("add", "approved-add", a=1, b=2))]
    if continuation == "model_switch":
        turns.insert(
            0,
            calls(tool_call("switch_thread_model", "switch-model", model_name="alternate", when="after-toolcall")),
        )
    if continuation == "nested_deferred_tool":
        turns.insert(0, calls(tool_call("run_subagent", "nested", agent_name="child", task="Add then list files")))
    if continuation in {"deferred_tool", "nested_deferred_tool"}:
        turns.append(calls(_deferred_call("list-files", "list_files")))
    if continuation == "preparation_error":
        # The continuation after this switch rebuilds the child, whose model preparation then fails.
        turns.append(calls(tool_call("switch_thread_model", "switch-model", model_name="default", when="next-turn")))
    if continuation == "nested_subagent":
        turns.extend(
            [
                calls(tool_call("run_subagent", "nested", agent_name="child", task="Check the sum")),
                answer("Nested check finished."),
            ],
        )
    if continuation == "nested_deferred_tool":
        turns.append(answer("Nested check finished."))
    turns.append(
        _provider_unavailable() if continuation == "provider_error" else answer("Child finished the requested work."),
    )
    return turns


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
        defaults=DefaultsConfig(tools=[]),
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
    identity = _identity()
    parent = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate", agent_name="child", task="Add then inspect files")),
            answer("Parent finished."),
        ],
    )
    model = ScriptedModel(_continuation_turns(continuation))
    child_models: list[str] = []

    def build(
        _config: Config,
        _paths: RuntimePaths,
        model_name: str,
        execution_identity: ToolExecutionIdentity | None,
    ) -> ModelProvider:
        assert execution_identity is not None
        if execution_identity.session_id == identity.session_id:
            return parent
        child_models.append(model_name)
        if continuation == "preparation_error" and len(model.requests) >= 2:
            msg = "Child model preparation failed."
            raise RuntimeError(msg)
        return model

    monkeypatch.setattr(agents, "_build_agent_model_provider", build)
    options = {"config": config, "runtime_paths": paths, "identity": identity}
    reply = await respond("leader", "Delegate", **options, run_id="parent-run")
    paused = stored_run("leader", "parent-run", **options)
    state = DelegationState.from_metadata(paused.metadata)
    if approval:
        assert isinstance(reply, PausedAttempt)
        assert paused.status is RunStatus.paused
        assert {source.toolkit_name for source in state.pending_tool_sources.values()} == {"calculator"}
        approval_calls = tuple(
            ApprovalCall(
                tool_call_id=str(tool["tool_call_id"]),
                tool_name=str(tool["tool_name"]),
                invoking_agent="child",
                toolkit_name="calculator",
                expires_at_ns=2**62,
                arguments_digest=approval_arguments_digest(tool["tool_args"]),
            )
            for tool in state.pending_tools
        )
        result = await resume(
            approval_executor(config, paths),
            reply,
            approval_calls,
            agent_name="leader",
            **options,
            decisions={call.tool_call_id: True for call in approval_calls},
        )
        assert isinstance(result, CompletedApprovalRun)

    completed = stored_run("leader", "parent-run", **options)
    assert completed.status is RunStatus.completed
    if continuation == "model_switch":
        assert state.children[0].model_name == "alternate"
        assert child_models == ["default", "alternate", "alternate"]
    child_id = state.children[0].delegation_id
    run_path = next(tmp_path.glob(f"agents/child/workspace/.mindroom/delegations/*/{child_id}/run.json"))
    if continuation in {"provider_error", "preparation_error"}:
        settled = DelegationState.from_metadata(completed.metadata).children[0]
        assert settled.status == "failed"
        assert json.loads(run_path.read_text())["status"] == "failed"
        assert settled.result
        assert any("failed" in str(tool.result) for tool in completed.tools or () if tool.tool_name == "run_subagent")
        return
    assert model.turns == []
    assert json.loads(run_path.read_text())["output"] == "Child finished the requested work."
    [approved] = [tool for tool in _child_tools(state.children[0], config, paths) if tool.tool_name == "add"]
    assert not approved.tool_call_error
    assert "3" in str(approved.result)
    prompts = system_prompts(model)
    assert prompts
    assert all("UNSUPPORTED_INTERACTIVE_MARKER" not in prompt for prompt in prompts)
    if continuation in {"deferred_tool", "nested_deferred_tool"}:
        events = [
            json.loads(line)
            for event_path in tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/events.jsonl")
            for line in event_path.read_text().splitlines()
        ]
        assert any(event["kind"] == "tool_result" and event["data"]["tool_name"] == "list_files" for event in events)
