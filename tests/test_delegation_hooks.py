"""Native subagents preserve plugin gates across durable approval pauses."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation_execution import _cancel_delegations, drive_delegations
from mindroom.delegation_hooks import before_delegation
from mindroom.delegation_state import DelegationState
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity, _only_run
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from pathlib import Path


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
async def test_native_subagent_preserves_plugin_call_lifecycle(  # noqa: C901, PLR0912, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """A parent tool has one gate and one terminal observer, even after reconstruction."""
    calls: list[tuple[str, str, str | None]] = []
    after_contexts: list[ToolAfterCallContext] = []

    @hook(EVENT_TOOL_BEFORE_CALL, agents=["leader"])
    async def before(context: ToolBeforeCallContext) -> None:
        if context.tool_name == "run_subagent":
            calls.append(("before", context.agent_name, context.session_id))
            context.arguments["task"] = "Hook mutation must stay isolated"
            if outcome == "blocked":
                context.decline("Subagents disabled by plugin")

    @hook(EVENT_TOOL_AFTER_CALL, agents=["leader"])
    async def after(context: ToolAfterCallContext) -> None:
        if context.tool_name == "run_subagent":
            calls.append(("after", context.agent_name, context.session_id))
            after_contexts.append(context)

    plugin = _plugin("subagent-policy", [before, after])
    registry = HookRegistry.from_plugins([plugin])
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child", tools=["calculator"]),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "action": "require_approval"}]},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    context = replace(_delegate_runtime_context(config, paths, execution_identity=identity), hook_registry=registry)
    model = DelegationModel(
        id="test-child",
        responses=[ModelResponse(tool_calls=[_call("add", "add", a=1, b=2)]), ModelResponse(content="Child done")],
    )
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: model)
    if outcome in {"live_cancel", "cancelled_output"}:

        async def cancelled(child: object, **_kwargs: object) -> RunOutput:
            if outcome == "live_cancel":
                raise asyncio.CancelledError
            return RunOutput(
                run_id=child.run_id,
                session_id=child.session_id,
                status=RunStatus.cancelled,
                content="Child cancelled",
            )

        monkeypatch.setattr("mindroom.delegation_execution._execute_child", cancelled)
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
                    tool_calls=[_call("run_subagent", "delegate", agent_name="child", task="Do original task")],
                ),
                ModelResponse(content="Parent done"),
            ],
        ),
    )
    options = {"agent_name": "leader", "config": config, "runtime_paths": paths, "execution_identity": identity}
    try:
        with tool_runtime_context(context):
            response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
            if outcome == "live_cancel":
                with pytest.raises(asyncio.CancelledError):
                    await drive_delegations(parent, response, **options)
                paused = response
            else:
                paused = await drive_delegations(parent, response, **options)
            if outcome in {"approved", "cancel", "recovery_cancel", "revoke"}:
                assert paused.status == RunStatus.paused
                assert calls == [("before", "leader", identity.session_id)]
                stored = storage.get_run(paused.run_id)
                assert isinstance(stored, RunOutput)
                state = DelegationState.from_metadata(stored.metadata)
                assert state.children[0].task == "Do original task"
                if outcome in {"cancel", "recovery_cancel"}:
                    if outcome == "recovery_cancel":
                        monkeypatch.setattr("mindroom.tool_system.plugins.load_plugins", lambda *_args: [plugin])
                    with tool_runtime_context(None if outcome == "recovery_cancel" else context):
                        await _cancel_delegations(stored, config=config, runtime_paths=paths)
                        await _cancel_delegations(stored, config=config, runtime_paths=paths)
                else:
                    if outcome == "revoke":
                        config.agents["leader"].delegate_to = []
                    decisions = {str(tool["tool_call_id"]): True for tool in state.pending_tools}
                    await drive_delegations(
                        parent,
                        stored,
                        **options,
                        decisions=decisions,
                        denial_reasons=dict.fromkeys(decisions),
                    )

        assert calls == [("before", "leader", identity.session_id), ("after", "leader", identity.session_id)]
        observed = after_contexts[0]
        assert observed.arguments["task"] == "Do original task"
        assert observed.blocked == (outcome == "blocked")
        if outcome == "blocked":
            assert "TOOL CALL DECLINED" in observed.result
            assert not list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/run.json"))
        elif outcome == "approved":
            assert "Child done" in observed.result
        else:
            assert _only_run(tmp_path)["status"] == "cancelled"
            if outcome in {"cancel", "recovery_cancel", "live_cancel"}:
                assert isinstance(observed.error, asyncio.CancelledError)
    finally:
        storage.close()
