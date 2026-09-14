"""Optional subagent targets preserve caller identity and authorization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.team import Team

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
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import DelegationModel, _call

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
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "action": "require_approval"}]} if execution != "direct" else {},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    toolkit = DelegateTools("leader", allowed, paths, config, execution_identity=identity)
    child_model = DelegationModel(
        id="child",
        responses=[ModelResponse(tool_calls=[_call("add", "sum", a=1, b=2)]), ModelResponse(content="Self report")],
    )
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: child_model)
    arguments = {"task": "Write an independent report"}
    if target in {"null", "empty"}:
        arguments["agent_name"] = None if target == "null" else ""
    expect_child = target in {"omitted", "null"}

    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        if execution == "direct":
            result = await toolkit.run_subagent(**arguments)
            assert ("Self report" in result) == expect_child
            if not expect_child:
                assert "Cannot delegate" in result
        else:
            apply_tool_approval_capability(
                toolkit,
                config,
                supports_native_tool_approval=True,
                registered_tool_name="delegate",
            )
            storage = create_session_storage("leader", config, paths, identity)
            member = Agent(
                id="leader",
                name="Leader",
                db=storage,
                tools=[toolkit],
                model=DelegationModel(
                    id="parent",
                    responses=[
                        ModelResponse(tool_calls=[_call("run_subagent", "delegate", **arguments)]),
                        ModelResponse(content="Parent done"),
                    ],
                ),
            )
            parent = (
                Team(
                    id="squad",
                    name="Squad",
                    db=storage,
                    members=[member],
                    model=DelegationModel(
                        id="team",
                        responses=[
                            ModelResponse(
                                tool_calls=[
                                    _call("delegate_task_to_member", "member", member_id="leader", task="Write report"),
                                ],
                            ),
                            ModelResponse(content="Team done"),
                        ],
                    ),
                )
                if execution == "team"
                else member
            )
            options = {
                "agent_name": "squad" if execution == "team" else "leader",
                "member_config_names": {"leader": "leader"},
                "config": config,
                "runtime_paths": paths,
                "execution_identity": identity,
            }
            try:
                response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
                response = await drive_delegations(parent, response, **options)
                if expect_child:
                    assert response.status == RunStatus.paused
                    state = DelegationState.from_metadata(response.metadata)
                    assert state.children[0].child_agent_name == "leader"
                    assert state.children[0].caller_agent_name == "leader"
                    response = await drive_delegations(
                        parent,
                        response,
                        **options,
                        decisions={str(tool["tool_call_id"]): True for tool in state.pending_tools},
                        denial_reasons={str(tool["tool_call_id"]): None for tool in state.pending_tools},
                    )
                assert response.status == RunStatus.completed
            finally:
                storage.close()

    records = list(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
    assert len(records) == int(expect_child)
    if expect_child:
        record = json.loads(records[0].read_text())
        assert record["child_agent_name"] == "leader"
        assert record["caller_agent_name"] == "leader"
        assert record["status"] == "completed"
        assert record["output"] == "Self report"
    else:
        assert len(child_model.responses) == 2
