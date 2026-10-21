"""A delegating parent run is stored without the history replayed into its request.

The team parent's scrub belongs to the team runtime cutover, which runs team delegation on the owned loop.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.delegation.state import DELEGATION_STATE_KEY
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.response_turn import CompletedApprovalRun, PausedAttempt
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.access_schema_support import with_responder_access
from tests.delegation_helpers import (
    ScriptedAgents,
    _runtime_paths,
    approval_calls_for,
    approval_executor,
    respond,
    resume,
)
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

REQUESTER = "@alice:example.org"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["child_runs", "policy_denied"])
async def test_delegating_run_is_stored_without_replayed_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    """The stored parent row keeps its own turn only, also when the delegation driver finishes it after a pause."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"]),
            "child": AgentConfig(display_name="Child"),
        },
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
        tool_approval={"default": "require_approval"} if outcome == "policy_denied" else {},
    )
    if outcome == "policy_denied":
        config = with_responder_access(config, "child", users=[REQUESTER])
    entity_ids(config, paths)
    identity = ToolExecutionIdentity("matrix", "leader", REQUESTER, "!room:example.org", None, None, "s1")
    leader = ScriptedModel(
        [
            answer("Hi there"),
            calls(tool_call("run_subagent", "d1", agent_name="child", task="Write report")),
            answer("Parent done"),
        ],
    )
    ScriptedAgents({"leader": leader, "child": ScriptedModel([answer("Child report")])}).install(monkeypatch)
    storage = create_session_storage("leader", config, paths, identity)
    options = {"config": config, "runtime_paths": paths, "identity": identity}

    first = await respond("leader", "Hello", run_id="run-1", **options)
    assert first == "Hi there"
    result = await respond("leader", "Delegate", run_id="run-2", **options)
    if outcome == "policy_denied":
        assert isinstance(result, PausedAttempt)
        mid_row = storage.get_run("run-2")
        assert isinstance(mid_row, RunRecord)
        assert mid_row.status is RunStatus.paused
        assert not any(message.from_history for message in mid_row.messages)
        [call] = approval_calls_for(result, default_agent="leader", toolkit_names={"run_subagent": "delegate"})
        result = await resume(
            approval_executor(config, paths),
            result,
            (call,),
            agent_name="leader",
            decisions={call.tool_call_id: False},
            denial_reasons={call.tool_call_id: "No"},
            **options,
        )
        assert isinstance(result, CompletedApprovalRun)

    stored = storage.get_run("run-2")
    assert isinstance(stored, RunRecord)
    assert stored.status is RunStatus.completed
    assert stored.metadata is not None
    assert DELEGATION_STATE_KEY in stored.metadata
    assert [message.content for message in stored.messages if message.from_history] == []
    own_turn = [message.content for message in stored.messages if message.role in {"user", "assistant"}]
    assert own_turn[-1] == "Parent done"
    assert any(message.role == "user" and "Delegate" in str(message.content) for message in stored.messages)
    # The request that delegated, and the one that resumed, both replayed the earlier turn.
    for request in leader.requests[1:]:
        replayed = [message.content for message in request.messages if message.from_history]
        assert replayed == ["Hello", "Hi there"] or any("Hello" in str(item) for item in replayed), replayed
