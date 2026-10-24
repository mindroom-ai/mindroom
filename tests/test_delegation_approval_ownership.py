"""Saved Matrix approvals reconstruct the exact delegated executable owner on the owned loop."""

from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.calculator import CalculatorTools
from mindroom.delegation.lifecycle import child_execution_identity
from mindroom.delegation.recovery import read_child_run
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState
from mindroom.event_journal import ApprovalCall, approval_arguments_digest
from mindroom.llm.runs import RunRecord, RunRequirement, ToolExecution
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.session_storage.agent_storage import create_session_storage
from tests.delegation_helpers import (
    ScriptedAgents,
    _deferred_call,
    _runtime_paths,
    approval_executor,
    respond,
    resume,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.test_delegation_direct_audit import _identity

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _stored_parent(config: Config, paths: RuntimePaths, run_id: str) -> RunRecord:
    record = create_session_storage("leader", config, paths, _identity()).get_run(run_id)
    assert isinstance(record, RunRecord)
    return record


def _save_parent(config: Config, paths: RuntimePaths, record: RunRecord) -> None:
    storage = create_session_storage("leader", config, paths, _identity())
    session = storage.load_session(_identity().session_id or "", "agent")
    assert session is not None
    storage.save_runs(session, [record])


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_kind", ["agent", "self"])
@pytest.mark.parametrize(
    "decision",
    [
        "approve",
        "deny_removed",
        "approve_removed",
        "wrong_owner",
        "approve_planted",
        "deny_gate",
        "approve_gate",
        "rewrite_gate",
        "approve_planted_gate",
        "deny_planted_gate",
    ],
)
async def test_saved_child_approval_preserves_executable_ownership(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_kind: str,
    decision: str,
) -> None:
    """Reconstruction may restore an approved owner, but cannot substitute, add, or revive a tool call."""
    child_name = "leader" if parent_kind == "self" else "child"
    gate = decision.endswith("gate")
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            name: AgentConfig(
                display_name=name.title(),
                tools=[{"calculator": {"defer": True}}],
                delegate_to=[child_name],
            )
            for name in {"leader", child_name}
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "run_subagent" if gate else "add", "action": "require_approval"}]},
    )
    entity_ids(config, paths)
    identity = _identity()
    child_model = ScriptedModel([calls(_deferred_call("child-add", "add", a=2, b=3)), answer("Child finished.")])
    parent_model = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate", agent_name=child_name, task="Add 2 and 3")),
            answer("Parent finished."),
        ],
    )
    ScriptedAgents(
        {"leader": parent_model if parent_kind == "agent" else child_model, "child": child_model},
        by_session={identity.session_id or "": parent_model},
    ).install(monkeypatch)
    executed: list[tuple[float, float]] = []
    original_add = CalculatorTools.add

    def add(self: CalculatorTools, a: float, b: float) -> str:
        executed.append((a, b))
        return original_add(self, a, b)

    monkeypatch.setattr(CalculatorTools, "add", add)

    paused = await respond(
        "leader",
        "Delegate",
        config=config,
        runtime_paths=paths,
        identity=identity,
        run_id="parent-run",
    )

    assert isinstance(paused, PausedAttempt)
    assert executed == []
    stored = _stored_parent(config, paths, "parent-run")
    invoking_agent = "leader" if gate else child_name
    function_name = "run_subagent" if gate else "add"
    toolkit_name = "delegate" if gate else "calculator"
    assert paused.toolkit_owners[(invoking_agent, function_name)] == toolkit_name
    call = ApprovalCall(
        tool_call_id=str(paused.tools[0].tool_call_id),
        tool_name=function_name,
        invoking_agent=invoking_agent,
        toolkit_name="file" if decision == "wrong_owner" else toolkit_name,
        expires_at_ns=2**62,
        arguments_digest=approval_arguments_digest(paused.tools[0].tool_args),
    )
    if decision.endswith("removed"):
        config.agents[child_name].tools = []
    planted_key = "planted"
    if decision == "rewrite_gate":
        # Only the separate external requirement changes; the approved gate card keeps its arguments.
        rewritten = deepcopy(stored)
        external = next(item for item in rewritten.requirements or () if item.needs_external_execution)
        external.tool_execution.tool_args = {**(external.tool_execution.tool_args or {}), "task": "Add 40 and 2"}
        _save_parent(config, paths, rewritten)
    elif decision.endswith("planted_gate"):
        # A second delegation and an approved gate for it appear only in the stored run.
        planted_run = deepcopy(stored)
        external = next(item for item in planted_run.requirements or () if item.needs_external_execution)
        planted_tool = deepcopy(external.tool_execution)
        planted_tool.tool_call_id = "planted"
        planted_tool.tool_args = {**(planted_tool.tool_args or {}), "task": "Add 40 and 2"}
        planted_run.requirements = [*(planted_run.requirements or ()), RunRequirement(planted_tool)]
        planted_state = DelegationState.from_metadata(planted_run.metadata)
        planted_state.gates[planted_key] = True
        planted_run.metadata = {**(planted_run.metadata or {}), DELEGATION_STATE_KEY: planted_state.to_dict()}
        _save_parent(config, paths, planted_run)
    elif decision == "approve_planted":
        child = DelegationState.from_metadata(stored.metadata).children[0]
        child_run = await read_child_run(child, config, paths)
        assert child_run is not None
        child_run.tools = [
            *(child_run.tools or ()),
            ToolExecution(
                tool_call_id="planted",
                tool_name="add",
                tool_args={"a": 40, "b": 2},
                requires_confirmation=True,
                confirmed=True,
            ),
        ]
        child_storage = create_session_storage(child_name, config, paths, child_execution_identity(child))
        child_session = child_storage.load_session(child.session_id, "agent")
        assert child_session is not None
        child_storage.save_runs(child_session, [child_run])
    approved = not decision.startswith("deny")
    with pytest.raises(RuntimeError, match="pending arguments") if decision == "rewrite_gate" else nullcontext():
        result = await resume(
            approval_executor(config, paths),
            paused,
            (call,),
            agent_name="leader",
            identity=identity,
            config=config,
            runtime_paths=paths,
            decisions={call.tool_call_id: approved},
            denial_reasons={call.tool_call_id: "Requester declined"},
            approval_id="saved-child-approval",
        )
    if decision == "rewrite_gate":
        assert executed == []
        assert not list(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
        return
    if decision.endswith("planted_gate"):
        # Only the approved delegation may start; the planted one waits for its own card.
        runs = list(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
        assert len(runs) == (1 if approved else 0)
        assert executed == ([(2, 3)] if approved else [])
        assert isinstance(result, PausedAttempt)
        assert [tool.tool_call_id for tool in result.tools] == [planted_key]
        return
    assert isinstance(result, CompletedApprovalRun)
    assert executed == ([(2, 3)] if decision in {"approve", "approve_gate"} else [])
    child_result = str(
        next(message.content for message in parent_model.requests[-1].messages if message.role == "tool"),
    )
    if decision == "deny_gate":
        assert "child was not executed" in child_result
        assert not list(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
    elif decision in {"approve_removed", "wrong_owner", "approve_planted"}:
        assert "failed" in child_result
    else:
        assert "Child finished." in child_result


@pytest.mark.asyncio
@pytest.mark.parametrize("delegation_pause", ["gate", "child"])
async def test_parent_call_beside_a_pausing_delegation_runs_once_every_card_is_approved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    delegation_pause: str,
) -> None:
    """The delegation's card comes first, so the parent's own card is approved in the continuation that runs it."""
    child_pauses = delegation_pause == "child"
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", tools=["calculator"], delegate_to=["child"]),
            "child": AgentConfig(display_name="Child", tools=["calculator"] if child_pauses else []),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        tool_approval={
            "rules": [
                {"match": "add", "action": "require_approval"},
                *([] if child_pauses else [{"match": "run_subagent", "action": "require_approval"}]),
            ],
        },
    )
    entity_ids(config, paths)
    identity = _identity()
    child_model = ScriptedModel(
        [
            *([calls(tool_call("add", "child-add", a=5, b=6))] if child_pauses else []),
            answer("Child finished."),
        ],
    )
    parent_model = ScriptedModel(
        [
            calls(
                tool_call("add", "parent-add", a=1, b=2),
                tool_call("run_subagent", "delegate", agent_name="child", task="Add 5 and 6"),
            ),
            answer("Parent finished."),
        ],
    )
    ScriptedAgents({"leader": parent_model, "child": child_model}).install(monkeypatch)
    executed: list[tuple[float, float]] = []
    original_add = CalculatorTools.add

    def add(self: CalculatorTools, a: float, b: float) -> str:
        executed.append((a, b))
        return original_add(self, a, b)

    monkeypatch.setattr(CalculatorTools, "add", add)
    executor = approval_executor(config, paths)

    result = await respond(
        "leader",
        "Delegate",
        config=config,
        runtime_paths=paths,
        identity=identity,
        run_id="parent-run",
    )

    cards = []
    for index in range(3):
        if not isinstance(result, PausedAttempt):
            break
        calls_for_cards = tuple(
            ApprovalCall(
                tool_call_id=str(tool.tool_call_id),
                tool_name=str(tool.tool_name),
                invoking_agent=result.approval_agent_name or "leader",
                toolkit_name="delegate" if tool.tool_name == "run_subagent" else "calculator",
                expires_at_ns=2**62,
                arguments_digest=approval_arguments_digest(tool.tool_args),
            )
            for tool in result.tools
        )
        cards.append([(call.invoking_agent, call.tool_name) for call in calls_for_cards])
        result = await resume(
            executor,
            result,
            calls_for_cards,
            agent_name="leader",
            identity=identity,
            config=config,
            runtime_paths=paths,
            decisions={call.tool_call_id: True for call in calls_for_cards},
            approval_id=f"card-{index}",
        )

    assert isinstance(result, CompletedApprovalRun)
    first_card = [("child", "add")] if child_pauses else [("leader", "run_subagent")]
    assert cards == [first_card, [("leader", "add")]]
    assert executed == ([(5, 6), (1, 2)] if child_pauses else [(1, 2)])
