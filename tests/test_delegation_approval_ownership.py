"""Saved Matrix approvals reconstruct the exact delegated executable owner."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.team import Team
from agno.tools.calculator import CalculatorTools

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.ai import run_delegated_child_response
from mindroom.approval_tools import toolkit_owners_for_agents
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.state import DelegationState
from mindroom.event_journal import ApprovalCall, ApprovalContinuation
from mindroom.history.session_context import open_resolved_scope_session_context
from mindroom.history.types import HistoryScope
from mindroom.response_sources import ResponseSources
from mindroom.response_turn import CompletedApprovalRun, paused_attempt_from_response
from mindroom.teams import TeamMode, _attach_team_pause_presentation, continue_paused_team_run
from mindroom.tool_system import dynamic_toolkits
from mindroom.tool_system.runtime_context import LiveToolDispatchContext, tool_runtime_context
from tests.conftest import unwrap_extracted_collaborator
from tests.identity_helpers import entity_ids
from tests.response_runner_helpers import _bot, _noop_typing
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_kind", ["agent", "self", "team"])
@pytest.mark.parametrize(
    "decision",
    ["approve", "deny_removed", "approve_removed", "wrong_owner", "deny_gate", "approve_gate"],
)
async def test_saved_child_approval_preserves_executable_ownership(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_kind: str,
    decision: str,
) -> None:
    """Reconstruction may restore an approved owner, but cannot substitute or revive a removed tool."""
    child_name = "leader" if parent_kind == "self" else "child"
    gate = decision.endswith("gate")
    config = Config(
        agents={
            name: AgentConfig(
                display_name=name.title(),
                tools=[{"calculator": {"defer": True}}],
                delegate_to=[child_name],
            )
            for name in {"leader", child_name}
        },
        teams={"squad": {"display_name": "Squad", "role": "Coordinate", "agents": ["leader"]}},
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "run_subagent" if gate else "add", "action": "require_approval"}]},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    child_model = DelegationModel(
        id="test-child",
        responses=[
            ModelResponse(tool_calls=[_call("load_tool", "load", tool_name="calculator")]),
            ModelResponse(tool_calls=[_call("add", "child-add", a=2, b=3)]),
            ModelResponse(content="Child finished."),
        ],
    )
    parent_model = DelegationModel(
        id="test-parent",
        responses=[
            ModelResponse(tool_calls=[_call("run_subagent", "delegate", agent_name=child_name, task="Add 2 and 3")]),
            ModelResponse(content="Parent finished."),
        ],
    )
    team_model = DelegationModel(
        id="test-team",
        responses=[
            ModelResponse(
                tool_calls=[_call("delegate_task_to_member", "member", member_id="leader", task="Delegate the sum")],
            ),
            ModelResponse(content="Team finished."),
        ],
    )
    monkeypatch.setattr(
        "mindroom.agents._load_agent_model_instance",
        lambda _config, _paths, _name, owner: parent_model if owner.session_id == identity.session_id else child_model,
    )
    monkeypatch.setattr("mindroom.model_loading.get_model_instance", lambda *_args, **_kwargs: team_model)
    executed = []
    original_add = CalculatorTools.add

    def add(self: CalculatorTools, a: float, b: float) -> str:
        executed.append((a, b))
        return original_add(self, a, b)

    monkeypatch.setattr(CalculatorTools, "add", add)
    toolkit = DelegateTools("leader", [child_name], paths, config, execution_identity=identity)
    for function in toolkit.get_async_functions().values():
        function.owning_toolkit = "delegate"
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    context = _delegate_runtime_context(config, paths, execution_identity=identity)
    history_scope = HistoryScope(kind="team", scope_id="squad")
    with open_resolved_scope_session_context(
        agent_name="leader",
        scope=history_scope,
        session_id=identity.session_id,
        config=config,
        runtime_paths=paths,
        execution_identity=identity,
        create_session_if_missing=True,
    ) as scope:
        assert scope is not None
        storage = scope.storage if parent_kind == "team" else create_session_storage("leader", config, paths, identity)
        try:
            member = Agent(id="leader", name="Leader", model=parent_model, tools=[toolkit], db=storage)
            parent = (
                Team(id="squad", name="Squad", members=[member], model=team_model, db=storage)
                if parent_kind == "team"
                else member
            )
            with tool_runtime_context(context):
                response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
                response = await drive_delegations(
                    parent,
                    response,
                    run_child=run_delegated_child_response,
                    agent_name="leader",
                    config=config,
                    runtime_paths=paths,
                    execution_identity=identity,
                    member_config_names={"leader": "leader"},
                )
            assert response.status == RunStatus.paused
            assert executed == []
            paused = paused_attempt_from_response(
                response,
                fallback_session_id=identity.session_id,
                fallback_run_id=response.run_id,
                toolkit_owners=toolkit_owners_for_agents([member]),
            )
            assert paused is not None
            if parent_kind == "team":
                paused = _attach_team_pause_presentation(
                    paused,
                    response=response,
                    config_names=["leader"],
                    display_names=["Leader"],
                    show_tool_calls=True,
                )
            invoking_agent = "leader" if gate else child_name
            function_name = "run_subagent" if gate else "add"
            toolkit_name = "delegate" if gate else "calculator"
            assert paused.toolkit_owners[(invoking_agent, function_name)] == toolkit_name
            call = ApprovalCall(
                tool_call_id=paused.tools[0].tool_call_id,
                tool_name=function_name,
                invoking_agent=invoking_agent,
                toolkit_name="file" if decision == "wrong_owner" else toolkit_name,
                expires_at_ns=2**62,
            )
            if not gate:
                child = DelegationState.from_metadata(response.metadata).children[0]
                dynamic_toolkits.save_loaded_tools_for_session(
                    agent_name=child_name,
                    session_id=child.session_id,
                    loaded_tools=[],
                )
            dynamic_toolkits._loaded_tools.clear()
            if decision.endswith("removed"):
                config.agents[child_name].tools = []
            decisions = {call.tool_call_id: not decision.startswith("deny")}
            reasons = {call.tool_call_id: "Requester declined"}
            with tool_runtime_context(context):
                if parent_kind == "team":
                    result = await continue_paused_team_run(
                        member_names=("leader",),
                        mode=TeamMode.COORDINATE,
                        config=config,
                        runtime_paths=paths,
                        execution_identity=replace(identity, agent_name="squad"),
                        session_id=identity.session_id,
                        run_id=paused.run_id,
                        user_id=identity.requester_id,
                        configured_team_name="squad",
                        model_name="default",
                        decisions=decisions,
                        denial_reasons=reasons,
                        refresh_scheduler=None,
                        approval_calls=(call,),
                        history_scope=history_scope,
                        prior_presentation_state=paused.response_presentation_state,
                        prior_response_text=paused.response_text,
                        prior_tool_trace=paused.tool_trace,
                    )
                else:
                    runner = unwrap_extracted_collaborator(_bot(tmp_path / "runner")._response_runner)
                    execution = replace(runner._approval_execution, config=lambda: config, runtime_paths=paths)
                    monkeypatch.setattr(
                        execution.knowledge_access,
                        "resolve_for_agent_async",
                        AsyncMock(return_value=SimpleNamespace(knowledge=None)),
                    )
                    monkeypatch.setattr("mindroom.approval_execution.typing_indicator", _noop_typing)
                    continuation = ApprovalContinuation(
                        approval_id="saved-child-approval",
                        run_id=paused.run_id,
                        session_id=identity.session_id,
                        entity_kind="agent",
                        entity_name="leader",
                        room_id=identity.room_id,
                        thread_id=identity.thread_id,
                        requester_id=identity.requester_id,
                        response_event_id="$waiting",
                        sources=ResponseSources(("$source",), ("$source",)),
                        state="claimed",
                        calls=(call,),
                    )
                    result = await execution.continue_run(
                        continuation,
                        execution_identity=identity,
                        tool_dispatch=LiveToolDispatchContext(execution_identity=identity, runtime_context=context),
                        decisions=decisions,
                        denial_reasons=reasons,
                        tool_trace_collector=[],
                        typing_log_context={},
                    )
            assert isinstance(result, CompletedApprovalRun)
            assert executed == ([(2, 3)] if decision in {"approve", "approve_gate"} else [])
            child_result = str(
                next(message.content for message in parent_model.seen_messages if message.role == "tool"),
            )
            if decision == "deny_gate":
                assert "child was not executed" in child_result
                assert not list(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
            elif decision in {"approve_removed", "wrong_owner"}:
                assert "failed" in child_result
            else:
                assert "Child finished." in child_result
        finally:
            if storage is not scope.storage:
                storage.close()
