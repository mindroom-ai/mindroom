"""Durable subagent handles enforce ownership and serialize follow-up turns."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import subagent_sessions_dir
from mindroom.delegation.lifecycle import reserve_child_turn
from mindroom.delegation.recovery import load_settled_subagent
from mindroom.delegation.sessions import (
    SubagentSessionError,
    load_subagent,
    reserve_subagent_turn,
    resolve_subagent_id,
    subagent_liveness,
    update_subagent_turn,
)
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationChild, DelegationState, SubagentPersona
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.tool_system.worker_routing import serialize_tool_execution_identity
from tests.delegation_helpers import _runtime_paths
from tests.test_delegation_direct_audit import _identity

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


@pytest.mark.asyncio
async def test_handle_reservations_are_scoped_and_reject_overlapping_followups(tmp_path: Path) -> None:
    """A stable handle has one owner and only one unfinished turn across independent loads."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"])})
    owner = _identity()
    child = _self_child(config, owner, task="First task")
    subagent_id = child.delegation_id
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
    child = _self_child(config, owner, task="First task")
    subagent_id = child.delegation_id
    options = {"owner": owner, "config": config, "runtime_paths": paths, "depth": 0}
    async with subagent_liveness(child, paths):
        await reserve_subagent_turn(child, owner=owner, runtime_paths=paths)
        assert (await load_subagent(subagent_id, **options)).status == "running"
    handle_path = paths.storage_root / "subagent_sessions" / f"{subagent_id}.json"
    before = handle_path.read_bytes()
    assert (await load_subagent(subagent_id, **options)).status == "running"
    assert handle_path.read_bytes() == before
    recovered = await load_settled_subagent(subagent_id, **options)
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


def _self_child(
    config: Config,
    owner: ToolExecutionIdentity,
    persona: SubagentPersona | None = None,
    *,
    task: str = "Review the plan",
) -> DelegationChild:
    """A fresh child of ``leader`` running itself."""
    subagent_id = uuid4().hex
    session_id = f"delegate:leader:leader:{subagent_id}"
    return DelegationChild(
        delegation_id=subagent_id,
        subagent_id=subagent_id,
        parent_tool_call_id="call",
        caller_agent_name="leader",
        child_agent_name="leader",
        task=task,
        session_id=session_id,
        run_id=uuid4().hex,
        model_name="default",
        requested_model_name="default",
        depth=1,
        execution_identity=serialize_tool_execution_identity(replace(owner, session_id=session_id)),
        storage_bindings=freeze_delegation_storage(config, ("leader",)),
        agent_mode="standard",
        persona=persona,
    )


@pytest.mark.parametrize("field", ["tools", "skills", "knowledge"])
@pytest.mark.parametrize("value", ["docs", [1]])
def test_persona_snapshot_with_malformed_names_is_rejected(field: str, value: object) -> None:
    """A snapshot whose name lists are not lists of strings is refused rather than read as characters."""
    names = {"tools": None, "skills": None, "knowledge": None}
    snapshot = {"source_kind": "inline", "source_name": "", "system_prompt": "P", **names, field: value}

    with pytest.raises(TypeError, match="Invalid subagent persona snapshot"):
        SubagentPersona.from_dict(snapshot)


@pytest.mark.asyncio
async def test_persona_round_trips_through_session_record(tmp_path: Path) -> None:
    """A retained handle rebuilds the exact authored persona after a reload."""
    paths = _runtime_paths(tmp_path)
    config = Config(agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"])})
    owner = _identity()
    persona = SubagentPersona(
        source_kind="profile",
        source_name="critic",
        system_prompt="You are a critic {x}.",
        tools=("file",),
        skills=("research",),
        knowledge=("docs",),
    )
    child = _self_child(config, owner, persona)
    await reserve_subagent_turn(child, owner=owner, runtime_paths=paths)
    child.status = "completed"
    await update_subagent_turn(child, paths)

    restored = await load_subagent(child.delegation_id, owner=owner, config=config, runtime_paths=paths, depth=0)

    assert restored.persona == persona
    assert restored == child
    assert DelegationState.from_metadata(
        {DELEGATION_STATE_KEY: DelegationState(children=[child]).to_dict()},
    ).children == [
        child,
    ]


async def _save_handle(
    paths: RuntimePaths,
    config: Config,
    owner: ToolExecutionIdentity,
    subagent_id: str,
    *,
    depth: int = 1,
) -> None:
    child = replace(
        _self_child(config, owner),
        delegation_id=subagent_id,
        subagent_id=subagent_id,
        session_id=f"delegate:leader:leader:{subagent_id}",
        depth=depth,
        status="completed",
        result="Done",
    )
    await reserve_subagent_turn(child, owner=owner, runtime_paths=paths)


@pytest.mark.asyncio
async def test_subagent_prefix_resolves_only_unique_owned_handle(tmp_path: Path) -> None:
    """An 8+ lowercase hex prefix resolves one owned handle, ignores foreign handles, and rejects ambiguity/invalid input."""
    paths, owner = _runtime_paths(tmp_path), _identity()
    config = Config(agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"])})
    prefix = "c6256cac"
    own_first, own_second, foreign_id, deeper_id = (f"{prefix}{d * 24}" for d in ("1", "2", "9", "8"))

    await _save_handle(paths, config, owner, own_first)
    await _save_handle(paths, config, replace(owner, requester_id="@other:example.org"), foreign_id)
    await _save_handle(paths, config, replace(owner, room_id="!other:example.org"), f"{prefix}{'7' * 24}")
    await _save_handle(paths, config, owner, deeper_id, depth=2)

    assert await resolve_subagent_id(prefix, owner=owner, runtime_paths=paths, depth=0) == own_first
    with pytest.raises(SubagentSessionError, match="not available"):
        await resolve_subagent_id(
            prefix,
            owner=replace(owner, requester_id="@stranger:example.org"),
            runtime_paths=paths,
            depth=0,
        )
    for invalid in (prefix[:7], prefix.upper(), "../c6256cac", "c6256ca*", "zzzzzzzz"):
        with pytest.raises(SubagentSessionError, match="not available"):
            await resolve_subagent_id(invalid, owner=owner, runtime_paths=paths, depth=0)

    await _save_handle(paths, config, owner, own_second)
    with pytest.raises(SubagentSessionError) as exc_info:
        await resolve_subagent_id(prefix, owner=owner, runtime_paths=paths, depth=0)
    msg = str(exc_info.value)
    assert f'Subagent ID prefix "{prefix}" matches several subagents in this conversation:' in msg
    assert {own_first in msg, own_second in msg, foreign_id not in msg, deeper_id not in msg} == {True}


@pytest.mark.asyncio
async def test_subagent_prefix_skips_corrupt_and_symlinked_siblings(tmp_path: Path) -> None:
    """Corrupt, missing-key, symlinked-json, and symlinked-lock sibling handles are skipped cleanly."""
    paths, owner = _runtime_paths(tmp_path), _identity()
    config = Config(agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"])})
    root, prefix = subagent_sessions_dir(paths), "deadbeef"
    clean_own = f"{prefix}{'1' * 24}"
    await _save_handle(paths, config, owner, clean_own)
    (root / f"{prefix}{'2' * 24}.json").write_text("{not valid json")
    (root / f"{prefix}{'3' * 24}.json").write_text("{}")

    async def symlink_handle(subagent_id: str) -> Path:
        await _save_handle(paths, config, owner, subagent_id)
        target = tmp_path / f"{subagent_id}.external.json"
        (root / f"{subagent_id}.json").replace(target)
        (root / f"{subagent_id}.json").symlink_to(target)
        return target

    # Target carries the symlink stem as stored subagent_id so only the symlink guard rejects it.
    ext = await symlink_handle(f"{prefix}{'4' * 24}")
    for digit, suffix in (("5", ".lock"), ("6", ".active.lock")):
        sid = f"{prefix}{digit * 24}"
        await _save_handle(paths, config, owner, sid)
        (root / f"{sid}{suffix}").unlink(missing_ok=True)
        (root / f"{sid}{suffix}").symlink_to(ext)
    assert await resolve_subagent_id(prefix, owner=owner, runtime_paths=paths, depth=0) == clean_own

    await symlink_handle(f"cafebabe{'0' * 24}")
    with pytest.raises(SubagentSessionError, match="not available"):
        await resolve_subagent_id("cafebabe", owner=owner, runtime_paths=paths, depth=0)

    (root / f"{clean_own}.lock").unlink(missing_ok=True)
    (root / f"{clean_own}.lock").symlink_to(ext)
    with pytest.raises(SubagentSessionError, match="symlinks"):
        await load_subagent(clean_own, owner=owner, config=config, runtime_paths=paths, depth=0)
