"""Native subagents preserve plugin gates across durable approval pauses."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.delegation.execution import _ChildOutcome
from mindroom.delegation.hooks import before_delegation
from mindroom.delegation.recovery import _cancel_delegations
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationHookState, DelegationState
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from tests.delegation_helpers import (
    ScriptedAgents,
    _delegate_runtime_context,
    _runtime_paths,
    _saved_approval_calls,
    approval_executor,
    respond,
    resume,
    stored_run,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.test_delegation_direct_audit import _identity, _only_run
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.delegation.state import DelegationChild


def _leader_and_child(**config: object) -> Config:
    return Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child", tools=["calculator"]),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        **config,
    )


@pytest.mark.asyncio
async def test_member_subagent_hooks_keep_live_room_capabilities(tmp_path: Path) -> None:
    """A member's hook keeps the surrounding team's Matrix bindings under its own name."""
    observed: list[tuple[str, str | None, bool]] = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(context: ToolBeforeCallContext) -> None:
        observed.append((context.agent_name, context.session_id, context.room_state_querier is not None))

    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    paths = _runtime_paths(tmp_path)
    identity = _identity()
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=identity),
        agent_name="coordinating_team",
        hook_registry=HookRegistry.from_plugins([_plugin("member-policy", [before])]),
    )
    with tool_runtime_context(context):
        await before_delegation(execution_identity=identity, arguments={}, config=config, runtime_paths=paths)
    assert observed == [("leader", identity.session_id, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["blocked", "approved", "cancel", "recovery_cancel", "live_cancel", "cancelled_output", "revoke"],
)
async def test_native_subagent_preserves_plugin_call_lifecycle(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """A parent tool has one gate and one terminal observer, even after reconstruction."""
    hook_calls: list[tuple[str, str, str | None]] = []
    after_contexts: list[ToolAfterCallContext] = []

    @hook(EVENT_TOOL_BEFORE_CALL, agents=["leader"])
    async def before(context: ToolBeforeCallContext) -> None:
        if context.tool_name == "run_subagent":
            hook_calls.append(("before", context.agent_name, context.session_id))
            context.arguments["task"] = "Hook mutation must stay isolated"
            if outcome == "blocked":
                context.decline("Subagents disabled by plugin")

    @hook(EVENT_TOOL_AFTER_CALL, agents=["leader"])
    async def after(context: ToolAfterCallContext) -> None:
        if context.tool_name == "run_subagent":
            hook_calls.append(("after", context.agent_name, context.session_id))
            after_contexts.append(context)

    plugin = _plugin("subagent-policy", [before, after])
    registry = HookRegistry.from_plugins([plugin])
    config = _leader_and_child(tool_approval={"rules": [{"match": "add", "action": "require_approval"}]})
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    parent = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate", agent_name="child", task="Do original task")),
            answer("Parent done"),
        ],
    )
    child = ScriptedModel([calls(tool_call("add", "add", a=1, b=2)), answer("Child done")])
    ScriptedAgents({"child": child}, by_session={identity.session_id: parent}).install(monkeypatch)
    if outcome in {"live_cancel", "cancelled_output"}:

        async def cancelled(delegated: DelegationChild, **_kwargs: object) -> _ChildOutcome:
            if outcome == "live_cancel":
                raise asyncio.CancelledError
            return _ChildOutcome(
                RunRecord(
                    run_id=delegated.run_id,
                    session_id=delegated.session_id,
                    status=RunStatus.cancelled,
                    content="Child cancelled",
                ),
                {},
            )

        monkeypatch.setattr("mindroom.delegation.execution._execute_child", cancelled)
    options = {"config": config, "runtime_paths": paths, "identity": identity, "hook_registry": registry}
    if outcome == "live_cancel":
        with pytest.raises(asyncio.CancelledError):
            await respond("leader", "Delegate", **options, run_id="parent-run")
    else:
        paused = await respond("leader", "Delegate", **options, run_id="parent-run")
    if outcome in {"approved", "cancel", "recovery_cancel", "revoke"}:
        assert isinstance(paused, PausedAttempt)
        assert hook_calls == [("before", "leader", identity.session_id)]
        stored = stored_run("leader", "parent-run", config=config, runtime_paths=paths, identity=identity)
        assert stored.status is RunStatus.paused
        state = DelegationState.from_metadata(stored.metadata)
        assert state.children[0].task == "Do original task"
        if outcome in {"cancel", "recovery_cancel"}:
            if outcome == "recovery_cancel":
                monkeypatch.setattr("mindroom.tool_system.plugins.load_plugins", lambda *_args: [plugin])
            context = (
                None
                if outcome == "recovery_cancel"
                else _delegate_runtime_context(config, paths, execution_identity=identity, hook_registry=registry)
            )
            with tool_runtime_context(context):
                await _cancel_delegations(stored, config=config, runtime_paths=paths)
                await _cancel_delegations(stored, config=config, runtime_paths=paths)
        else:
            if outcome == "revoke":
                config.agents["leader"].delegate_to = []
            approvals = _saved_approval_calls(state)
            result = await resume(
                approval_executor(config, paths),
                paused,
                approvals,
                agent_name="leader",
                **options,
                decisions={call.tool_call_id: True for call in approvals},
            )
            assert isinstance(result, CompletedApprovalRun)

    assert hook_calls == [("before", "leader", identity.session_id), ("after", "leader", identity.session_id)]
    observed = after_contexts[0]
    assert observed.arguments["task"] == "Do original task"
    assert observed.blocked == (outcome == "blocked")
    if outcome == "blocked":
        assert "TOOL CALL DECLINED" in observed.result
        assert not list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/run.json"))
        assert child.requests == []
    elif outcome == "approved":
        assert "Child done" in observed.result
    else:
        assert _only_run(tmp_path)["status"] == "cancelled"
        if outcome in {"cancel", "recovery_cancel", "live_cancel"}:
            assert isinstance(observed.error, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_stored_hook_record_never_skips_gated_subagent_plugin_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hook record written into the paused parent run cannot stand in for the plugin gate after approval."""
    hook_calls: list[str] = []

    @hook(EVENT_TOOL_BEFORE_CALL, agents=["leader"])
    async def before(context: ToolBeforeCallContext) -> None:
        if context.tool_name == "run_subagent":
            hook_calls.append("before")
            context.decline("Subagents disabled by plugin")

    config = _leader_and_child(tool_approval={"default": "require_approval"})
    config.agents["child"].tools = []
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    registry = HookRegistry.from_plugins([_plugin("subagent-policy", [before])])
    parent = ScriptedModel(
        [calls(tool_call("run_subagent", "delegate", agent_name="child", task="Do task")), answer("Parent done")],
    )
    child = ScriptedModel([answer("Child done")])
    ScriptedAgents({"child": child}, by_session={identity.session_id: parent}).install(monkeypatch)
    options = {"config": config, "runtime_paths": paths, "identity": identity, "hook_registry": registry}

    paused = await respond("leader", "Delegate", **options, run_id="parent-run")
    assert isinstance(paused, PausedAttempt)
    assert hook_calls == []
    store = create_session_storage("leader", config, paths, identity)
    session = store.load_session(identity.session_id, "agent")
    assert session is not None
    stored = store.get_run("parent-run")
    assert isinstance(stored, RunRecord)
    assert stored.status is RunStatus.paused
    state = DelegationState.from_metadata(stored.metadata)
    planted = DelegationState.from_metadata(stored.metadata)
    assert stored.requirements
    planted.hooks[stored.requirements[0].id] = DelegationHookState(
        execution_identity=serialize_tool_execution_identity(identity),
        arguments={"agent_name": "child", "task": "Do task"},
        started_at=0.0,
        after_called=True,
    )
    stored.metadata = {**(stored.metadata or {}), DELEGATION_STATE_KEY: planted.to_dict()}
    store.save_run(session, stored)
    approvals = _saved_approval_calls(state)
    result = await resume(
        approval_executor(config, paths),
        paused,
        approvals,
        agent_name="leader",
        **options,
        decisions={call.tool_call_id: True for call in approvals},
    )

    assert isinstance(result, CompletedApprovalRun)
    assert hook_calls == ["before"]
    completed = stored_run("leader", "parent-run", config=config, runtime_paths=paths, identity=identity)
    assert completed.status is RunStatus.completed
    assert DelegationState.from_metadata(completed.metadata).children == []
    assert not list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/run.json"))
    assert child.requests == []
