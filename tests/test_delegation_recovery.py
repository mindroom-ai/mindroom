"""Restart cleanup follows exact retained attempts without crossing follow-up turns."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from agno.models.response import ToolExecution
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession

from mindroom.agent_storage import create_session_storage
from mindroom.delegation.lifecycle import (
    finish_child_turn,
    note_child_run_id,
    settle_child_response,
    start_child_turn,
)
from mindroom.delegation.records import DelegationRecordLocator, DelegationRecordOwner
from mindroom.delegation.recovery import _cancel_delegations, read_child_run
from mindroom.delegation.sessions import reserve_subagent_turn
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationState
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from tests.conftest import test_runtime_paths
from tests.test_delegation_audit import _child, _config, _identity, _record_dir

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("followup", [False, True])
@pytest.mark.parametrize("removed_config", [False, True])
async def test_restart_cleanup_uses_latest_attempt_of_the_same_delegation(  # noqa: PLR0915
    tmp_path: Path,
    followup: bool,
    removed_config: bool,
) -> None:
    """A stale parent must settle its advanced attempt and descendants, but never a later turn."""
    config = _config()
    paths = test_runtime_paths(tmp_path)
    owner = _identity("leader", "parent-session")
    child = _child()
    child.subagent_id = uuid4().hex
    child.storage_bindings = freeze_delegation_storage(config, ("leader", "child"))
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=paths,
        caller_execution_identity=owner,
    )
    await reserve_subagent_turn(child, owner=owner, runtime_paths=paths)
    # The parent's last checkpoint still points at the previously paused attempt.
    child.status = "paused"
    parent = RunOutput(metadata={DELEGATION_STATE_KEY: DelegationState(children=[child]).to_dict()})
    original = replace(child)
    completed = RunOutput(
        agent_id="child",
        run_id=child.run_id,
        session_id=child.session_id,
        user_id=owner.requester_id,
        status=RunStatus.completed,
        content="Previous attempt finished",
    )
    if followup:
        await settle_child_response(child, completed, config=config, runtime_paths=paths)
        child = replace(
            child,
            delegation_id=uuid4().hex,
            previous_delegation_id=child.delegation_id,
            status="running",
            result=None,
            record_locator={},
        )
        await start_child_turn(
            child,
            parent_run_id="followup-parent-run",
            config=config,
            runtime_paths=paths,
            caller_execution_identity=owner,
        )
        await reserve_subagent_turn(child, owner=owner, runtime_paths=paths)
    note_child_run_id(child, "advanced-run", paths, model_name="advanced-model")

    descendant = replace(
        child,
        delegation_id=uuid4().hex,
        subagent_id=uuid4().hex,
        previous_delegation_id=None,
        caller_agent_name="child",
        session_id="descendant-session",
        run_id="descendant-run",
        depth=2,
        execution_identity=serialize_tool_execution_identity(_identity("child", "descendant-session")),
        record_locator={},
        status="paused",
    )
    await start_child_turn(
        descendant,
        parent_run_id=child.run_id,
        config=config,
        runtime_paths=paths,
        caller_execution_identity=_identity("child", child.session_id),
        parent_delegation_id=child.delegation_id,
    )
    await reserve_subagent_turn(
        descendant,
        owner=_identity("child", child.session_id),
        runtime_paths=paths,
    )
    advanced = RunOutput(
        agent_id="child",
        run_id=child.run_id,
        session_id=child.session_id,
        user_id=owner.requester_id,
        status=RunStatus.paused,
        metadata={DELEGATION_STATE_KEY: DelegationState(children=[descendant]).to_dict()},
    )
    nested = RunOutput(
        agent_id="child",
        run_id=descendant.run_id,
        session_id=descendant.session_id,
        user_id=owner.requester_id,
        status=RunStatus.paused,
        tools=[ToolExecution(tool_call_id="pending", tool_name="shell", requires_confirmation=True)],
    )
    storage = create_session_storage("child", config, paths, _identity("child", child.session_id))
    try:
        for turn in (child, descendant):
            storage.upsert_session(AgentSession(session_id=turn.session_id, user_id=owner.requester_id))
        for run in (completed, advanced, nested):
            storage.upsert_run(run=run, session_id=run.session_id, user_id=run.user_id)
    finally:
        storage.close()
    record_dir = await _record_dir(child, config, paths)
    if removed_config:
        config = config.model_copy(update={"agents": {}})

    # Exact approval-source reads must still address the original attempt.
    original_run = await read_child_run(original, config, paths)
    assert original_run is not None
    assert original_run.content == "Previous attempt finished"
    await _cancel_delegations(parent, config=config, runtime_paths=paths, reason="Stopped after restart")

    retained = DelegationState.from_metadata(parent.metadata).children[0]
    assert retained.run_id == ("child-run" if followup else "advanced-run")
    assert retained.status == ("completed" if followup else "cancelled")
    assert retained.model_name == ("test-model" if followup else "advanced-model")
    for turn in (child, descendant):
        result = await read_child_run(turn, config, paths)
        assert result is not None
        assert result.status == (RunStatus.paused if followup else RunStatus.cancelled)
        if not followup:
            assert result.content == "Stopped after restart"
            assert not any(tool.is_paused for tool in result.tools or ())
    handle = json.loads((paths.storage_root / "subagent_sessions" / f"{child.subagent_id}.json").read_text())["child"]
    assert handle["delegation_id"] == child.delegation_id
    assert handle["run_id"] == "advanced-run"
    assert handle["model_name"] == "advanced-model"
    assert handle["status"] == ("running" if followup else "cancelled")
    audit = json.loads((record_dir / "run.json").read_text())
    assert audit["status"] == ("running" if followup else "cancelled")
    original_run = await read_child_run(original, config, paths)
    assert original_run is not None
    assert original_run.status == RunStatus.completed


@pytest.mark.asyncio
async def test_a_child_that_plants_a_directory_over_its_record_export_is_still_settled(tmp_path: Path) -> None:
    """The approval decision commits, and cancelling from a stale parent snapshot of a failed child returns."""
    config = _config()
    paths = test_runtime_paths(tmp_path)
    child = _child()
    child.storage_bindings = freeze_delegation_storage(config, ("leader", "child"))
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )
    handle = await DelegationRecordOwner(config, paths).reopen(DelegationRecordLocator.from_dict(child.record_locator))
    (handle.child_workspace / handle.scoped_path / "run.json").unlink()
    (handle.child_workspace / handle.scoped_path / "run.json").mkdir()
    paused = RunOutput(
        run_id=child.run_id,
        session_id=child.session_id,
        status=RunStatus.paused,
        tools=[ToolExecution(tool_call_id="gated", tool_name="shell", requires_confirmation=True)],
    )

    await settle_child_response(child, paused, config=config, runtime_paths=paths, decisions={"gated": True})
    # The parent's stored snapshot still holds the paused child after the child's failure settled its record.
    parent = RunOutput(metadata={DELEGATION_STATE_KEY: DelegationState(children=[replace(child)]).to_dict()})
    await finish_child_turn(child, config=config, runtime_paths=paths, status="failed", reason="Child failed")
    await _cancel_delegations(parent, config=config, runtime_paths=paths, reason="Stopped")

    assert DelegationState.from_metadata(parent.metadata).children[0].status == "cancelled"
    events = [json.loads(line) for line in (handle.state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "approval_decision" in [event["kind"] for event in events]
    assert (events[-1]["kind"], events[-1]["data"]["status"]) == ("delegation_finished", "failed")
