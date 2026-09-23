"""Subagent follow-ups retain history and native approval ownership."""

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus
from agno.team import Team

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.ai import run_delegated_child_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation import sessions
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.recovery import resolve_subagent
from mindroom.delegation.state import DelegationState
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import DelegationModel, _call, _saved_approval_calls

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("execution", ["direct", "native", "team"])
@pytest.mark.parametrize("model", [None, "alternate"])
async def test_followup_reuses_child_history_after_parent_reconstruction(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    execution: str,
    model: str | None,
) -> None:
    """A new parent/toolkit can continue the same child and obtain a separate audit turn."""
    native = execution != "direct"
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", delegate_to=["leader"], tools=["calculator"])},
        defaults=DefaultsConfig(tools=[], learning=False),
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
    child_model = DelegationModel(
        id="child",
        responses=[
            ModelResponse(content="First answer: cobalt"),
            ModelResponse(tool_calls=[_call("add", "sum", a=1, b=2)]),
            ModelResponse(content="Follow-up answer: cobalt, 3"),
        ],
    )
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: child_model)

    async def invoke(name: str, **args: object) -> str:
        toolkit = DelegateTools("leader", ["leader"], paths, config, execution_identity=identity)
        if not native:
            if name == "run_subagent":
                return await toolkit.run_subagent(task=str(args["task"]), model=model)
            return await toolkit.continue_subagent(subagent_id=str(args["subagent_id"]), message=str(args["message"]))
        apply_tool_approval_capability(
            toolkit,
            config,
            supports_native_tool_approval=True,
            registered_tool_name="delegate",
        )
        storage = create_session_storage("leader", config, paths, identity)
        parent = Agent(
            id="leader",
            name="Leader",
            db=storage,
            tools=[toolkit],
            model=DelegationModel(
                id="parent",
                responses=[
                    ModelResponse(tool_calls=[_call(name, "delegation", **args)]),
                    ModelResponse(content="Parent done"),
                ],
            ),
        )
        if execution == "team":
            parent = Team(
                id="squad",
                name="Squad",
                db=storage,
                members=[parent],
                model=DelegationModel(
                    id="team",
                    responses=[
                        ModelResponse(
                            tool_calls=[_call("delegate_task_to_member", "member", member_id="leader", task="Work")],
                        ),
                        ModelResponse(content="Team done"),
                    ],
                ),
            )
        options = {
            "agent_name": "squad" if execution == "team" else "leader",
            "member_config_names": {"leader": "leader"},
            "config": config,
            "runtime_paths": paths,
            "execution_identity": identity,
        }
        try:
            response = await parent.arun("Work", session_id=identity.session_id, user_id=identity.requester_id)
            response = await drive_delegations(parent, response, run_child=run_delegated_child_response, **options)
            pause_count = 0
            while response.status == RunStatus.paused:
                pause_count += 1
                assert pause_count <= 2
                state = DelegationState.from_metadata(response.metadata)
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
                response = await drive_delegations(
                    parent,
                    response,
                    run_child=run_delegated_child_response,
                    **options,
                    decisions={str(tool["tool_call_id"]): True for tool in state.pending_tools},
                    approval_calls=_saved_approval_calls(state),
                    denial_reasons={str(tool["tool_call_id"]): None for tool in state.pending_tools},
                )
            assert pause_count == (2 if name == "continue_subagent" else 0)
            assert response.status == RunStatus.completed
            if execution == "team":
                child = DelegationState.from_metadata(response.metadata).children[0]
                return f"{child.result}\nSubagent ID: {child.subagent_id}"
            return next(str(tool.result) for tool in response.tools or () if tool.tool_name == name)
        finally:
            storage.close()

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

    history = "\n".join(str(message.content) for message in child_model.seen_messages)
    assert "Remember this secret word: cobalt" in history
    assert "First answer: cobalt" in history
    assert "Recall the word and add 1 + 2" in history
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
        defaults=DefaultsConfig(tools=[], learning=False),
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
