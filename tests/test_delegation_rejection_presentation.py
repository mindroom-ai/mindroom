"""Rejected native subagent calls settle their visible Matrix tool traces."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.delegation.execution import _drive_delegations, drive_delegation_stream
from mindroom.delegation.state import DelegationState
from mindroom.hooks import EVENT_TOOL_BEFORE_CALL, HookRegistry, ToolBeforeCallContext, hook
from mindroom.llm.events import RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.runs import RunStatus
from mindroom.private_storage.private_instance_identity import private_instances_for_agent
from mindroom.tool_system.events import CollectedStreamPresentation
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths, pause_parent
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids, fixture_entity_matrix_id
from tests.test_delegation_direct_audit import _identity
from tests.test_tool_hooks import _plugin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.events import RunEvent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rejection", "expected_result"),
    [
        ("arguments", "task must be a string"),
        ("authorization", "Cannot delegate to 'child'"),
        ("policy", "denied by requester"),
        ("plugin", "Subagents disabled by plugin"),
        ("private", "Cannot delegate to 'child': a private agent runs only for a person or bot account."),
    ],
)
async def test_rejected_subagent_closes_streaming_tool_trace(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rejection: str,
    expected_result: str,
) -> None:
    """Every rejection must deliver its result through the real stream presentation."""
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(
                display_name="Child",
                private=AgentPrivateConfig(per="user") if rejection == "private" else None,
            ),
        },
        defaults={"tools": []},
        memory={"backend": "none"},
        tool_approval={
            "rules": [{"match": "run_subagent", "action": "require_approval"}] if rejection == "policy" else [],
        },
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    if rejection == "private":
        # A shared agent's turn without a human requester, such as its automation, runs as the agent's own account.
        leader_id = fixture_entity_matrix_id("leader", config.get_domain(paths), paths).full_id
        identity = replace(identity, requester_id=leader_id)

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(context: ToolBeforeCallContext) -> None:
        if rejection == "plugin":
            context.decline("Subagents disabled by plugin")

    context = _delegate_runtime_context(
        config,
        paths,
        execution_identity=identity,
        hook_registry=HookRegistry.from_plugins([_plugin("subagent-policy", [before])]),
    )
    parent_model = ScriptedModel(
        [calls(tool_call("run_subagent", "delegate", agent_name="child", task="Do work")), answer("Parent done")],
    )
    child_model = ScriptedModel([])
    ScriptedAgents({"child": child_model}, by_session={identity.session_id: parent_model}).install(monkeypatch)

    async def unused_child(**_kwargs: object) -> str:
        pytest.fail("A rejected delegation must not execute a child")

    presentation = CollectedStreamPresentation(show_tool_calls=True, track_hidden_tools=True)
    with tool_runtime_context(context):
        parent = await pause_parent("leader", "Delegate", config=config, runtime_paths=paths, identity=identity)
        response = parent.record
        options = {
            "store": parent.store,
            "session": parent.session,
            "continue_parent": parent.runner.continue_run,
            "agent_name": "leader",
            "run_child": unused_child,
            "config": config,
            "runtime_paths": paths,
            "execution_identity": identity,
        }
        if rejection == "arguments":
            assert response.requirements
            response.requirements[0].tool_execution.tool_args = {"agent_name": "child", "task": 1}
        elif rejection == "authorization":
            config.agents["leader"].delegate_to = []
        decisions = None
        if rejection == "policy":
            response = await _drive_delegations(response, **options)
            assert response.status is RunStatus.paused
            state = DelegationState.from_metadata(response.metadata)
            decisions = {str(tool["tool_call_id"]): False for tool in state.pending_tools}

        async def stored_run() -> AsyncIterator[RunEvent]:
            yield RunFinished(response)

        completed = None
        async for event in drive_delegation_stream(
            stored_run(),
            **options,
            decisions=decisions,
            denial_reasons=dict.fromkeys(decisions or {}),
        ):
            if isinstance(event, ToolStarted):
                presentation.start_tool(event.tool)
            elif isinstance(event, ToolCompleted):
                presentation.complete_tool(event.tool)
            elif isinstance(event, RunFinished):
                completed = event.record

    assert completed is not None
    assert completed.status is RunStatus.completed
    assert completed.content == "Parent done"
    assert len(presentation.tool_trace) == 1
    trace = presentation.tool_trace[0]
    assert trace.tool_name == "run_subagent"
    assert trace.type == "tool_call_completed"
    assert expected_result in trace.result_preview
    assert child_model.requests == []
    assert not list(tmp_path.glob("agents/child/workspace/.mindroom/delegations/*/*/run.json"))
    assert private_instances_for_agent(paths.storage_root, "child", "user") == ()
