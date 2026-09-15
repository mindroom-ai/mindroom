"""Native delegated-run audit adaptation tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

import pytest
from agno.metrics import RunMetrics
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, RunOutput, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.base import RunStatus

from mindroom.config.agent import AgentConfig, AgentPrivateConfig, AgentPrivateKnowledgeConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.delegation.audit import (
    child_audit_context,
    finish_child_record,
    observe_child_event,
    record_child_response,
)
from mindroom.delegation.lifecycle import settle_child_response, start_child_turn
from mindroom.delegation.records import DelegationRecordLocator, DelegationRecordOwner
from mindroom.delegation.recovery import interrupt_child
from mindroom.delegation.sessions import reserve_subagent_turn
from mindroom.delegation.state import DelegationChild
from mindroom.delegation.storage import freeze_delegation_storage
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    serialize_tool_execution_identity,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _config() -> Config:
    return Config(
        agents={
            "leader": AgentConfig(display_name="Leader"),
            "child": AgentConfig(display_name="Child"),
        },
        models={"default": ModelConfig(provider="test", id="test-model")},
    )


def _identity(agent_name: str, session_id: str) -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id="@alice:localhost",
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=session_id,
    )


def _child() -> DelegationChild:
    identity = _identity("child", "child-session")
    return DelegationChild(
        delegation_id="child-delegation",
        parent_tool_call_id="parent-call",
        caller_agent_name="leader",
        child_agent_name="child",
        task="Inspect the report",
        session_id="child-session",
        run_id="child-run",
        model_name="test-model",
        depth=1,
        execution_identity=serialize_tool_execution_identity(identity),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.paused, RunStatus.completed, RunStatus.error, RunStatus.cancelled])
async def test_audit_snapshot_cannot_settle_child_or_release_followup(tmp_path: Path, status: RunStatus) -> None:
    """Audit projection cannot change execution state or release a follow-up reservation."""
    config = _config()
    paths = test_runtime_paths(tmp_path)
    child = _child()
    child.subagent_id = uuid4().hex
    await reserve_subagent_turn(child, owner=_identity("leader", "parent-session"), runtime_paths=paths)
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )
    before_child = asdict(child)
    handle_path = paths.storage_root / "subagent_sessions" / f"{child.subagent_id}.json"
    before_handle = handle_path.read_bytes()
    await record_child_response(
        child,
        RunOutput(run_id=child.run_id, session_id=child.session_id, status=status, content="retained outcome"),
        config=config,
        runtime_paths=paths,
    )
    assert asdict(child) == before_child
    assert handle_path.read_bytes() == before_handle
    record_dir = await _record_dir(child, config, paths)
    assert not any(event["kind"] == "delegation_finished" for event in _events(record_dir))


def _events(record_dir: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in (record_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]


async def _record_dir(child: DelegationChild, config: Config, runtime_paths: RuntimePaths) -> Path:
    locator = DelegationRecordLocator.from_dict(child.record_locator)
    return (await DelegationRecordOwner(config, runtime_paths).reopen(locator)).record_dir


@pytest.mark.asyncio
async def test_start_child_record_persists_restart_locator_and_source_scope(tmp_path: Path) -> None:
    """Omitting the restart locator or source metadata must fail this test."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    child = _child()

    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=runtime_paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )

    assert child.record_locator["delegation_id"] == child.delegation_id
    record_dir = await _record_dir(child, config, runtime_paths)
    run = json.loads((record_dir / "run.json").read_text(encoding="utf-8"))
    assert run["parent_run_id"] == "parent-run"
    assert run["parent_tool_call_id"] == "parent-call"
    assert run["source_room_id"] == "!room:localhost"
    assert run["source_thread_id"] == "$thread"
    assert run["requester_id"] == "@alice:localhost"


@pytest.mark.asyncio
@pytest.mark.parametrize("removed_config", [False, True])
@pytest.mark.parametrize("status", ["cancelled", "failed"])
async def test_interruption_recovery_preserves_workspace_knowledge_links(
    tmp_path: Path,
    removed_config: bool,
    status: Literal["cancelled", "failed"],
) -> None:
    """Settling audit records must not reconcile workspaces with stripped recovery config."""
    config = _config()
    paths = test_runtime_paths(tmp_path)
    child = _child()
    links: list[Path] = []
    for name in ("leader", "child"):
        config.agents[name].private = AgentPrivateConfig(
            per="user",
            knowledge=AgentPrivateKnowledgeConfig(path="docs"),
        )
        resolved = resolve_agent_runtime(name, config, paths, _identity(name, f"{name}-session"), create=True)
        assert resolved.workspace is not None
        link = resolved.workspace.root / "knowledge" / f"__agent_private__:{name}"
        assert link.is_symlink()
        links.append(link)
    child.storage_bindings = freeze_delegation_storage(config, ("leader", "child"))
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )
    record_dir = await _record_dir(child, config, paths)
    if removed_config:
        config = config.model_copy(update={"agents": {}})

    await interrupt_child(child, config=config, runtime_paths=paths, reason="Interrupted", status=status)

    assert all(link.is_symlink() for link in links)
    run = json.loads((record_dir / "run.json").read_text())
    assert run["status"] == status
    assert run["error"] == "Interrupted"


@pytest.mark.asyncio
async def test_record_child_response_orders_tools_approval_output_usage_and_finish(tmp_path: Path) -> None:
    """Losing full payloads, approval order, deduplication, or terminal state must fail this test."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    child = _child()
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=runtime_paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )
    completed_tool = ToolExecution(
        tool_call_id="first-tool",
        tool_name="lookup",
        tool_args={"query": "complete query"},
        result="complete result",
    )
    pending_tool = ToolExecution(
        tool_call_id="pending-tool",
        tool_name="shell",
        tool_args={"command": "pwd"},
        requires_confirmation=True,
    )
    paused = RunOutput(
        run_id=child.run_id,
        session_id=child.session_id,
        status=RunStatus.paused,
        content="partial response",
        tools=[completed_tool, pending_tool],
        metrics=RunMetrics(input_tokens=5, output_tokens=2),
    )

    await settle_child_response(child, paused, config=config, runtime_paths=runtime_paths)
    await settle_child_response(child, paused, config=config, runtime_paths=runtime_paths)

    resumed_tool = ToolExecution(
        tool_call_id="pending-tool",
        tool_name="shell",
        tool_args={"command": "pwd"},
        confirmed=True,
        confirmation_note="approved for this call",
        result="/workspace",
    )
    completed = RunOutput(
        run_id=child.run_id,
        session_id=child.session_id,
        status=RunStatus.completed,
        content="final response",
        tools=[completed_tool, resumed_tool],
        metrics=RunMetrics(input_tokens=8, output_tokens=4),
    )
    await settle_child_response(
        child,
        completed,
        config=config,
        runtime_paths=runtime_paths,
        decisions={"pending-tool": True},
        denial_reasons={"pending-tool": None},
    )

    record_dir = await _record_dir(child, config, runtime_paths)
    events = _events(record_dir)
    kinds = [event["kind"] for event in events]
    assert kinds == [
        "delegation_started",
        "tool_call",
        "tool_result",
        "tool_call",
        "approval_requested",
        "output",
        "usage",
        "approval_decision",
        "tool_result",
        "output",
        "usage",
        "delegation_finished",
    ]
    first_call = next(event for event in events if event["event_id"] == "run:child-run:tool:first-tool:call")
    assert first_call["data"]["arguments"] == {"query": "complete query"}
    first_result = next(event for event in events if event["event_id"] == "run:child-run:tool:first-tool:result")
    assert first_result["data"]["result"] == "complete result"
    decision = next(event for event in events if event["event_id"] == "run:child-run:approval:pending-tool:decision")
    assert decision["data"] == {
        "tool_call_id": "pending-tool",
        "approved": True,
        "reason": None,
    }
    run = json.loads((record_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == "completed"
    assert run["output"] == "final response"
    assert child.status == "completed"
    assert child.result == "final response"
    assert run["usage"]["input_tokens"] == 8
    assert run["usage"]["output_tokens"] == 4


@pytest.mark.asyncio
async def test_live_observer_records_matching_child_events_once(tmp_path: Path) -> None:
    """Recording parent events, missing live tool data, or replaying an event must fail this test."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    child = _child()
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=runtime_paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )
    tool = ToolExecution(
        tool_call_id="live-tool",
        tool_name="lookup",
        tool_args={"query": "live query"},
        result="live result",
    )
    started = ToolCallStartedEvent(
        run_id=child.run_id,
        session_id=child.session_id,
        tool=tool,
    )
    started.event_index = 10
    completed = ToolCallCompletedEvent(
        run_id=child.run_id,
        session_id=child.session_id,
        tool=tool,
    )
    completed.event_index = 11
    content = RunContentEvent(
        run_id=child.run_id,
        session_id=child.session_id,
        content="visible chunk",
    )
    content.event_index = 12
    parent_event = ToolCallStartedEvent(
        run_id="parent-run",
        session_id="parent-session",
        tool=tool,
    )
    parent_event.event_index = 13

    async with child_audit_context(child, config=config, runtime_paths=runtime_paths):
        await observe_child_event(parent_event)
        await observe_child_event(started)
        await observe_child_event(content)
        await observe_child_event(completed)
        await observe_child_event(completed)

    events = _events(await _record_dir(child, config, runtime_paths))
    assert [(event["kind"], event["event_id"]) for event in events[1:]] == [
        ("tool_call", "run:child-run:tool:live-tool:call"),
        ("output", "run:child-run:stream:12"),
        ("tool_result", "run:child-run:tool:live-tool:result"),
    ]
    assert events[1]["data"]["arguments"] == {"query": "live query"}
    assert events[2]["data"]["content"] == "visible chunk"
    assert events[3]["data"]["result"] == "live result"


@pytest.mark.asyncio
async def test_live_observer_flushes_buffered_partial_output_during_cancellation(tmp_path: Path) -> None:
    """Cancellation between content and the next tool must not discard visible partial output."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    child = _child()
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=runtime_paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )
    content = RunContentEvent(
        run_id=child.run_id,
        session_id=child.session_id,
        content="partial before cancel",
    )
    content.event_index = 20

    async def cancel_during_context() -> None:
        async with child_audit_context(child, config=config, runtime_paths=runtime_paths):
            await observe_child_event(content)
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cancel_during_context()

    events = _events(await _record_dir(child, config, runtime_paths))
    assert events[-1]["kind"] == "output"
    assert events[-1]["event_id"] == "run:child-run:stream:20"
    assert events[-1]["data"]["content"] == "partial before cancel"


@pytest.mark.asyncio
async def test_live_observer_distinguishes_retried_attempt_tool_ids(tmp_path: Path) -> None:
    """Reusing a provider tool ID in a later attempt must retain both attempts."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    child = _child()
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=runtime_paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )
    tool = ToolExecution(tool_call_id="reused-tool", tool_name="lookup", tool_args={"value": 1})
    first_metrics = RunMetrics(input_tokens=9, output_tokens=3)

    async with child_audit_context(child, config=config, runtime_paths=runtime_paths):
        await observe_child_event(
            ToolCallStartedEvent(run_id="child-run", session_id=child.session_id, tool=tool),
        )
        await observe_child_event(
            RunOutput(
                run_id="child-run",
                session_id=child.session_id,
                status=RunStatus.completed,
                content="discarded attempt",
                metrics=first_metrics,
            ),
        )
        child.run_id = "retried-run"
        await observe_child_event(
            ToolCallStartedEvent(run_id="retried-run", session_id=child.session_id, tool=tool),
        )

    events = _events(await _record_dir(child, config, runtime_paths))
    assert [event["event_id"] for event in events if event["kind"] == "tool_call"] == [
        "run:child-run:tool:reused-tool:call",
        "run:retried-run:tool:reused-tool:call",
    ]
    assert next(event for event in events if event["kind"] == "output")["data"]["content"] == ("discarded attempt")
    assert next(event for event in events if event["kind"] == "usage")["data"]["input_tokens"] == 9


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "cancelled", "denied"])
async def test_finish_child_record_settles_non_success_outcome_idempotently(
    status: str,
    tmp_path: Path,
) -> None:
    """Converting a failed, cancelled, or denied child into success must fail this test."""
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    child = _child()
    await start_child_turn(
        child,
        parent_run_id="parent-run",
        config=config,
        runtime_paths=runtime_paths,
        caller_execution_identity=_identity("leader", "parent-session"),
    )
    child.status = status
    child.result = f"child {status}"

    first_receipt = await finish_child_record(child, config=config, runtime_paths=runtime_paths)
    second_receipt = await finish_child_record(child, config=config, runtime_paths=runtime_paths)

    assert first_receipt == second_receipt
    record_dir = await _record_dir(child, config, runtime_paths)
    run = json.loads((record_dir / "run.json").read_text(encoding="utf-8"))
    assert run["status"] == status
    assert run["output"] is None
    assert run["error"] == f"child {status}"
    assert [event["kind"] for event in _events(record_dir)].count("delegation_finished") == 1
