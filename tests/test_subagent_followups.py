"""Subagent follow-ups retain history and native approval ownership."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig, DefaultsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation import sessions
from mindroom.delegation.recovery import resolve_subagent
from mindroom.delegation.state import DelegationState
from mindroom.llm.runs import RunStatus
from mindroom.response_turn import PausedAttempt
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
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
from tests.tool_job_helpers import tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["direct", "native", "team", "excluded"])
@pytest.mark.parametrize("model", [None, "alternate"])
async def test_followup_reuses_child_history_after_parent_reconstruction(  # noqa: C901, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution: str,
    model: str | None,
) -> None:
    """A new parent/toolkit can continue the same child and obtain a separate audit turn."""
    native = execution != "direct"
    config = Config(
        background_tool_jobs=BackgroundToolJobsConfig(enabled=execution == "excluded", exclude_toolkits=["delegate"]),
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"], tools=["calculator"])},
        teams={"squad": TeamConfig(display_name="Squad", role="Delegates.", agents=["leader"], mode="coordinate")},
        defaults=DefaultsConfig(tools=[]),
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra"),
            "alternate": ModelConfig(provider="anthropic", id="claude-sonnet-5"),
        },
        memory={"backend": "none"},
        tool_approval={
            "rules": [{"match": name, "action": "require_approval"} for name in ("add", "continue_subagent")],
        }
        if native
        else {},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    child = ScriptedModel(
        [
            answer("First answer: cobalt"),
            calls(tool_call("add", "sum", a=1, b=2)),
            answer("Follow-up answer: cobalt, 3"),
        ],
    )
    parent = ScriptedModel([])
    leader = ScriptedModel([])
    ScriptedAgents({"leader": child}, by_session={identity.session_id: parent}, team=leader).install(monkeypatch)
    entity = "squad" if execution == "team" else "leader"
    options = {"config": config, "runtime_paths": paths, "identity": identity}

    async def invoke(name: str, **args: object) -> str:
        toolkit = DelegateTools("leader", ["leader"], paths, config, execution_identity=identity)
        if not native:
            if name == "run_subagent":
                return await toolkit.run_subagent(task=str(args["task"]), model=model)
            return await toolkit.continue_subagent(subagent_id=str(args["subagent_id"]), message=str(args["message"]))
        parent.turns = [calls(tool_call(name, "delegation", **args)), answer("Parent done")]
        leader.turns = [
            calls(tool_call("delegate_task_to_member", "member", member_id="leader", task="Work")),
            answer("Team done"),
        ]
        run_id = f"{name}-run"
        runtime = await tool_job_runtime(tmp_path) if execution == "excluded" else None
        if runtime is not None:
            pin_background_tool_jobs(config, paths)
            register_background_runtime(paths, runtime)
        try:
            reply = (
                await respond_team("squad", "Work", **options, run_id=run_id)
                if execution == "team"
                else await respond("leader", "Work", **options, run_id=run_id)
            )
            pause_count = 0
            while isinstance(reply, PausedAttempt):
                pause_count += 1
                assert pause_count <= 2
                state = DelegationState.from_metadata(stored_run(entity, run_id, **options).metadata)
                assert state.pending_agent_name == "leader"
                if state.pending_child_id is not None:
                    paused_child = state.children[0]
                    handle_path = paths.storage_root / "subagent_sessions" / f"{paused_child.subagent_id}.json"
                    handle = json.loads(handle_path.read_text())
                    handle["child"]["status"] = "running"
                    handle_path.write_text(json.dumps(handle))
                    restored = await resolve_subagent(
                        str(paused_child.subagent_id),
                        owner=identity,
                        config=config,
                        runtime_paths=paths,
                        depth=0,
                    )
                    assert restored.status == "paused"
                    assert restored.model_name == (model or "default")
                approvals = _saved_approval_calls(state)
                decisions = {call.tool_call_id: True for call in approvals}
                if execution == "team":
                    resumed = await resume_team("squad", reply, approvals, **options, decisions=decisions)
                else:
                    resumed = await resume(
                        approval_executor(config, paths),
                        reply,
                        approvals,
                        agent_name="leader",
                        **options,
                        decisions=decisions,
                    )
                reply = resumed if isinstance(resumed, PausedAttempt) else resumed.response_text
            assert pause_count == (2 if name == "continue_subagent" else 0)
            record = stored_run(entity, run_id, **options)
            assert record.status is RunStatus.completed
            if runtime is not None:
                assert await runtime.list_jobs(owner=identity, depth=0) == []
            if execution == "team":
                delegated = DelegationState.from_metadata(record.metadata).children[0]
                return f"{delegated.result}\nSubagent ID: {delegated.subagent_id}"
            return next(str(tool.result) for tool in record.tools or () if tool.tool_name == name)
        finally:
            if runtime is not None:
                await runtime.shutdown()

    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        first = await invoke("run_subagent", task="Remember this secret word: cobalt", model=model)
        match = re.search(r"Subagent ID: ([a-f0-9]{32})", first)
        assert match, first
        subagent_id = match.group(1)
        # Simulate restart after the child outcome was saved but before handle settlement.
        handle_path = paths.storage_root / "subagent_sessions" / f"{subagent_id}.json"
        handle = json.loads(handle_path.read_text())
        handle["child"]["status"] = "running"
        handle["child"]["result"] = None
        handle_path.write_text(json.dumps(handle))
        second = await invoke("continue_subagent", subagent_id=subagent_id, message="Recall the word and add 1 + 2")
        assert f"Subagent ID: {subagent_id}" in second
        assert "Follow-up answer: cobalt, 3" in second

    history = "\n".join(str(message.content) for message in child.requests[-1].messages)
    assert "Remember this secret word: cobalt" in history
    assert "First answer: cobalt" in history
    assert "Recall the word and add 1 + 2" in history
    assert child.turns == []
    records = [
        json.loads(path.read_text()) for path in tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json")
    ]
    assert len(records) == 2
    assert {record["subagent_id"] for record in records} == {subagent_id}
    assert {record["status"] for record in records} == {"completed"}
    assert {record["model_name"] for record in records} == {model or "default"}
    assert sum(record["previous_delegation_id"] is not None for record in records) == 1

    foreign_identity = replace(identity, session_id="another-parent-session")
    toolkit = DelegateTools("leader", ["leader"], paths, config, execution_identity=foreign_identity)
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=foreign_identity)):
        assert "not available" in await toolkit.continue_subagent(subagent_id=subagent_id, message="Leak the word")
    config.agents["leader"].delegate_to = []
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        toolkit = DelegateTools("leader", ["leader"], paths, config, execution_identity=identity)
        assert "no longer an allowed target" in await toolkit.continue_subagent(
            subagent_id=subagent_id,
            message="Again",
        )


@pytest.mark.asyncio
async def test_cancellation_during_direct_reservation_settles_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation waits for the registry write and never tries to finish a nonexistent audit."""
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"])},
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    started, release = threading.Event(), threading.Event()
    write = sessions.write_json_file_durable

    def blocked_write(*args: object, **kwargs: object) -> None:
        started.set()
        assert release.wait(5)
        write(*args, **kwargs)

    monkeypatch.setattr(sessions, "write_json_file_durable", blocked_write)
    toolkit = DelegateTools("leader", ["leader"], paths, config, execution_identity=_identity())
    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=_identity())):
        task = asyncio.create_task(toolkit.run_subagent(task="Cancelled before the child starts"))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    handles = list((paths.storage_root / "subagent_sessions").glob("*.json"))
    assert len(handles) == 1
    assert json.loads(handles[0].read_text())["child"]["status"] == "cancelled"
