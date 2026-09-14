"""Rejected native subagent calls settle their visible Matrix tool traces."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation_execution import drive_delegation_stream, drive_delegations
from mindroom.delegation_state import DelegationState
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, HookRegistry, ToolBeforeCallContext, hook
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rejection", "expected_result"),
    [
        ("arguments", "task must be a string"),
        ("authorization", "Cannot delegate to 'child'"),
        ("policy", "denied by requester"),
        ("plugin", "Subagents disabled by plugin"),
    ],
)
async def test_rejected_subagent_closes_streaming_tool_trace(  # noqa: C901
    tmp_path: Path,
    rejection: str,
    expected_result: str,
) -> None:
    """Every rejection must deliver its result through the real stream presentation."""
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
        defaults={"tools": []},
        memory={"backend": "none"},
        tool_approval={
            "rules": [{"match": "run_subagent", "action": "require_approval"}] if rejection == "policy" else [],
        },
    )
    paths = _runtime_paths(tmp_path)
    identity = _identity()

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(context: ToolBeforeCallContext) -> None:
        if rejection == "plugin":
            context.decline("Subagents disabled by plugin")

    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=identity),
        hook_registry=HookRegistry.from_plugins([_plugin("subagent-policy", [before])]),
    )
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
                ModelResponse(tool_calls=[_call("run_subagent", "delegate", agent_name="child", task="Do work")]),
                ModelResponse(content="Parent done"),
            ],
        ),
    )
    options = {"agent_name": "leader", "config": config, "runtime_paths": paths, "execution_identity": identity}
    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    try:
        with tool_runtime_context(context):
            response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
            if rejection == "arguments":
                assert response.requirements
                assert response.requirements[0].tool_execution is not None
                response.requirements[0].tool_execution.tool_args = {"agent_name": "child", "task": 1}
            elif rejection == "authorization":
                config.agents["leader"].delegate_to = []
            decisions = None
            if rejection == "policy":
                response = await drive_delegations(parent, response, **options)
                assert isinstance(response, RunOutput)
                assert response.status == RunStatus.paused
                state = DelegationState.from_metadata(response.metadata)
                decisions = {str(tool["tool_call_id"]): False for tool in state.pending_tools}

            async def stored_run() -> AsyncIterator[RunOutput]:
                yield response

            completed = None
            async for event in drive_delegation_stream(
                parent,
                stored_run(),
                **options,
                decisions=decisions,
                denial_reasons=dict.fromkeys(decisions or {}),
            ):
                if isinstance(event, ToolCallStartedEvent):
                    presentation.start_tool(event.tool)
                elif isinstance(event, ToolCallCompletedEvent):
                    presentation.complete_tool(event.tool)
                elif isinstance(event, RunOutput):
                    completed = event

        assert completed is not None
        assert completed.status == RunStatus.completed
        assert len(presentation.tool_trace) == 1
        trace = presentation.tool_trace[0]
        assert trace.tool_name == "run_subagent"
        assert trace.type == "tool_call_completed"
        assert expected_result in trace.result_preview
        assert not list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/run.json"))
    finally:
        storage.close()
