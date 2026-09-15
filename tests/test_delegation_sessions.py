"""Durable subagent handles enforce ownership and serialize follow-up turns."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.delegation.lifecycle import reserve_child_turn
from mindroom.delegation.recovery import resolve_subagent
from mindroom.delegation.sessions import (
    SubagentSessionError,
    load_subagent,
    reserve_subagent_turn,
    subagent_liveness,
    update_subagent_turn,
)
from mindroom.delegation.state import DelegationChild
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from tests.test_delegate_tools import _runtime_paths
from tests.test_delegation_direct_audit import _identity

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_handle_reservations_are_scoped_and_reject_overlapping_followups(tmp_path: Path) -> None:
    """A stable handle has one owner and only one unfinished turn across independent loads."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"])})
    owner = _identity()
    subagent_id = uuid4().hex
    child = DelegationChild(
        delegation_id=subagent_id,
        subagent_id=subagent_id,
        parent_tool_call_id="call",
        caller_agent_name="leader",
        child_agent_name="leader",
        task="First task",
        session_id=f"delegate:leader:leader:{subagent_id}",
        run_id=uuid4().hex,
        model_name="default",
        depth=1,
        execution_identity=serialize_tool_execution_identity(
            replace(owner, session_id=f"delegate:leader:leader:{subagent_id}"),
        ),
        storage_bindings=freeze_delegation_storage(config, ("leader",)),
    )
    await reserve_subagent_turn(child, owner=owner, runtime_paths=paths)
    child.status = "completed"
    child.result = "First answer"
    await update_subagent_turn(child, paths)
    options = {"config": config, "runtime_paths": paths, "depth": 0}
    restored = await load_subagent(subagent_id, owner=replace(owner, thread_id=None), **options)
    assert restored == child

    for changed in (
        {"requester_id": "@other:example.org"},
        {"session_id": "another-session"},
        {"agent_name": "other"},
        {"channel": "openai_compat"},
        {"tenant_id": "other"},
        {"account_id": "other"},
        {"room_id": "!other:example.org"},
        {"resolved_thread_id": "$other"},
        {"session_id": None},
    ):
        with pytest.raises(SubagentSessionError, match="not available"):
            await load_subagent(subagent_id, owner=replace(owner, **changed), **options)
    with pytest.raises(SubagentSessionError, match="not available"):
        await load_subagent("../escape", owner=owner, **options)
    with pytest.raises(SubagentSessionError, match="not available"):
        await load_subagent(subagent_id, owner=owner, **{**options, "depth": 1})

    turns = [
        replace(
            child,
            delegation_id=uuid4().hex,
            previous_delegation_id=child.delegation_id,
            run_id=uuid4().hex,
            status="running",
            result=None,
            task=f"Follow-up {index}",
        )
        for index in range(2)
    ]
    results = await asyncio.gather(
        *(reserve_subagent_turn(turn, owner=owner, runtime_paths=paths) for turn in turns),
        return_exceptions=True,
    )
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, SubagentSessionError) for result in results) == 1
    winner = turns[results.index(None)]
    winner.status = "paused"
    await update_subagent_turn(winner, paths)
    stale = replace(winner, run_id="older-attempt", model_name="older-model")
    retained = await reserve_subagent_turn(stale, owner=owner, runtime_paths=paths)
    assert retained == winner
    assert stale.run_id == "older-attempt"
    assert stale.model_name == "older-model"
    await reserve_child_turn(stale, owner=owner, runtime_paths=paths)
    assert stale.run_id == winner.run_id
    assert stale.model_name == winner.model_name
    # Re-finishing an old audit cannot unlock or overwrite the pending approval.
    await update_subagent_turn(child, paths)
    retained = await load_subagent(subagent_id, owner=owner, **options)
    assert retained.delegation_id == winner.delegation_id
    assert retained.status == "paused"
    with pytest.raises(SubagentSessionError, match="busy or awaiting approval"):
        await reserve_subagent_turn(turns[1 - results.index(None)], owner=owner, runtime_paths=paths)
    config.agents["leader"].worker_scope = "user"
    with pytest.raises(SubagentSessionError, match="storage scope changed"):
        await load_subagent(subagent_id, owner=owner, **options)


@pytest.mark.asyncio
async def test_liveness_distinguishes_active_from_abandoned_turn(tmp_path: Path) -> None:
    """Live execution cannot be recovered; an abandoned claim fails without replaying tools."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader")})
    owner = _identity()
    subagent_id = uuid4().hex
    child = DelegationChild(
        delegation_id=subagent_id,
        subagent_id=subagent_id,
        parent_tool_call_id="call",
        caller_agent_name="leader",
        child_agent_name="leader",
        task="First task",
        session_id=f"delegate:leader:leader:{subagent_id}",
        run_id=uuid4().hex,
        model_name="default",
        depth=1,
        execution_identity=serialize_tool_execution_identity(
            replace(owner, session_id=f"delegate:leader:leader:{subagent_id}"),
        ),
        storage_bindings=freeze_delegation_storage(config, ("leader",)),
    )
    options = {"owner": owner, "config": config, "runtime_paths": paths, "depth": 0}
    async with subagent_liveness(child, paths):
        await reserve_subagent_turn(child, owner=owner, runtime_paths=paths)
        assert (await load_subagent(subagent_id, **options)).status == "running"
    handle_path = paths.storage_root / "subagent_sessions" / f"{subagent_id}.json"
    before = handle_path.read_bytes()
    assert (await load_subagent(subagent_id, **options)).status == "running"
    assert handle_path.read_bytes() == before
    recovered = await resolve_subagent(subagent_id, **options)
    assert recovered.status == "failed"
    assert not recovered.record_locator
    assert (await load_subagent(subagent_id, **options)).status == "failed"
    assert "interrupted by a restart" in str(recovered.result)
    next_turn = replace(
        recovered,
        delegation_id=uuid4().hex,
        previous_delegation_id=recovered.delegation_id,
        status="running",
        result=None,
    )
    await reserve_subagent_turn(next_turn, owner=owner, runtime_paths=paths)
    del config.agents["leader"]
    with pytest.raises(SubagentSessionError, match="no longer configured"):
        await load_subagent(subagent_id, **options)
