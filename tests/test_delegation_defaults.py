"""Optional subagent targets preserve caller identity and authorization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.state import DelegationState
from mindroom.llm.runs import RunStatus
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.delegation_helpers import (
    ScriptedAgents,
    _delegate_runtime_context,
    _runtime_paths,
    _saved_approval_calls,
    approval_executor,
    respond,
    respond_team,
    resume,
    resume_team,
    stored_run,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.test_delegation_direct_audit import _identity

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["direct", "native", "team"])
@pytest.mark.parametrize("target", ["omitted", "null", "empty", "not_allowed"])
async def test_default_subagent_target_keeps_caller_scope_and_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution: str,
    target: str,
) -> None:
    """Omitted/null targets run self, including after approval; empty/disallowed targets cannot execute."""
    allowed = ["other"] if target == "not_allowed" else ["leader"]
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", tools=["calculator"], delegate_to=allowed),
            "other": AgentConfig(display_name="Other"),
        },
        teams={"squad": TeamConfig(display_name="Squad", role="Delegates.", agents=["leader"], mode="coordinate")},
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "action": "require_approval"}]} if execution != "direct" else {},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    child = ScriptedModel([calls(tool_call("add", "sum", a=1, b=2)), answer("Self report")])
    arguments: dict[str, object] = {"task": "Write an independent report"}
    if target in {"null", "empty"}:
        arguments["agent_name"] = None if target == "null" else ""
    expect_child = target in {"omitted", "null"}
    parent = ScriptedModel([calls(tool_call("run_subagent", "delegate", **arguments)), answer("Parent done")])
    team = ScriptedModel(
        [
            calls(tool_call("delegate_task_to_member", "member", member_id="leader", task="Write report")),
            answer("Done"),
        ],
    )
    agents = ScriptedAgents({"leader": child}, by_session={identity.session_id: parent}, team=team)
    agents.install(monkeypatch)

    if execution == "direct":
        toolkit = DelegateTools("leader", allowed, paths, config, execution_identity=identity)
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            result = await toolkit.run_subagent(**arguments)
        assert ("Self report" in result) == expect_child
        if not expect_child:
            assert "Cannot delegate" in result
    else:
        options = {"config": config, "runtime_paths": paths, "identity": identity}
        entity = "squad" if execution == "team" else "leader"
        reply = (
            await respond_team("squad", "Delegate", **options, run_id="parent-run")
            if execution == "team"
            else await respond("leader", "Delegate", **options, run_id="parent-run")
        )
        if expect_child:
            assert isinstance(reply, PausedAttempt)
            stored = stored_run(entity, reply.run_id, **options)
            assert stored.status is RunStatus.paused
            state = DelegationState.from_metadata(stored.metadata)
            assert state.children[0].child_agent_name == "leader"
            assert state.children[0].caller_agent_name == "leader"
            approvals = _saved_approval_calls(state)
            decisions = {call.tool_call_id: True for call in approvals}
            result = (
                await resume_team("squad", reply, approvals, **options, decisions=decisions)
                if execution == "team"
                else await resume(
                    approval_executor(config, paths),
                    reply,
                    approvals,
                    agent_name="leader",
                    **options,
                    decisions=decisions,
                )
            )
            assert isinstance(result, CompletedApprovalRun)
        else:
            assert isinstance(reply, str)
        assert stored_run(entity, "parent-run", **options).status is RunStatus.completed

    records = list(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
    assert len(records) == int(expect_child)
    if expect_child:
        record = json.loads(records[0].read_text())
        assert record["child_agent_name"] == "leader"
        assert record["caller_agent_name"] == "leader"
        assert record["status"] == "completed"
        assert record["output"] == "Self report"
    else:
        assert child.requests == []
