"""Tests for direct background-script tool-call brokering."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agno.tools import Toolkit

import mindroom.tools  # noqa: F401
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import RuntimePaths, tracking_dir
from mindroom.hooks import (
    EVENT_TOOL_AFTER_CALL,
    EVENT_TOOL_BEFORE_CALL,
    HookRegistry,
    ToolAfterCallContext,
    ToolBeforeCallContext,
    hook,
)
from mindroom.message_target import MessageTarget
from mindroom.script_runs import broker as broker_module
from mindroom.script_runs.broker import ScriptToolBroker, ScriptToolCallRequest
from mindroom.script_runs.models import ScriptCallState, ScriptRunRecord, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunStore, mint_script_capability
from mindroom.tool_approval import ToolApprovalDecision
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_approval import BackgroundScriptToolOrigin
    from mindroom.tool_system.runtime_context import ToolRuntimeContext


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
    )


def _hook_registry(events: list[str]) -> HookRegistry:
    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(_context: ToolBeforeCallContext) -> None:
        events.append("tool:before_call")

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(_context: ToolAfterCallContext) -> None:
        events.append("tool:after_call")

    plugin = SimpleNamespace(
        name="script-broker-test",
        discovered_hooks=(before, after),
        entry_config=PluginEntryConfig(path="./plugins/script-broker-test"),
        plugin_order=0,
    )
    return HookRegistry.from_plugins([plugin])


def _context(
    tmp_path: Path,
    *,
    hook_registry: HookRegistry,
    require_approval: bool = False,
    log_tool_calls: bool = False,
) -> ToolRuntimeContext:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"watcher": AgentConfig(display_name="Watcher", tools=["calculator"])},
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval={
                "rules": [
                    {
                        "match": "add",
                        "action": "require_approval" if require_approval else "auto_approve",
                    },
                ],
            },
            debug={"log_llm_requests": log_tool_calls},
        ),
        runtime_paths,
    )
    return make_test_tool_runtime_context(
        agent_name="watcher",
        target=MessageTarget.resolve(
            room_id="!room:example.test",
            thread_id="$thread:example.test",
            reply_to_event_id="$event:example.test",
        ),
        requester_id="@alice:example.test",
        client=SimpleNamespace(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        hook_registry=hook_registry,
    )


class _RuntimeResolver:
    def __init__(
        self,
        context: ToolRuntimeContext,
        approval_events: list[str] | None = None,
        approval_decision: ToolApprovalDecision | None = None,
    ) -> None:
        self.context = context
        self.approval_events = approval_events
        self.approval_decision = approval_decision or ToolApprovalDecision(approved=True)

    def resolve(self, run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        assert run.agent_name == "watcher"
        return replace(self.context, correlation_id=correlation_id)

    async def request_approval(
        self,
        *,
        origin: BackgroundScriptToolOrigin,
        context: ToolRuntimeContext,
        grant: ScriptToolGrant,
        arguments: dict[str, object],
        timeout_seconds: float,
    ) -> ToolApprovalDecision:
        assert origin.requester_id == "@alice:example.test"
        assert origin.toolkit_name == "calculator"
        assert origin.function_name == "add"
        assert context.requester_id == "@alice:example.test"
        assert grant == ScriptToolGrant("calculator", "add")
        assert arguments == {"a": 1, "b": 2}
        assert timeout_seconds > 0
        if self.approval_events is not None:
            self.approval_events.append(f"approval:{origin.run_id}:{origin.call_id}")
        return self.approval_decision


def _broker(
    tmp_path: Path,
    *,
    events: list[str],
    require_approval: bool = False,
    log_tool_calls: bool = False,
    approval_decision: ToolApprovalDecision | None = None,
) -> tuple[ScriptToolBroker, str]:
    context = _context(
        tmp_path,
        hook_registry=_hook_registry(events),
        require_approval=require_approval,
        log_tool_calls=log_tool_calls,
    )
    store = ScriptRunStore(context.runtime_paths)
    token, token_hash = mint_script_capability()
    store.create_run(
        ScriptRunRecord(
            run_id="run-1",
            agent_name="watcher",
            owner_user_id=context.requester_id,
            room_id=context.room_id,
            thread_root_event_id=context.resolved_thread_id,
            execution_identity={"worker_key": "durable-worker"},
            source_digest="source-digest",
            grants=(ScriptToolGrant("calculator", "add"),),
            token_hash=token_hash,
        ),
    )
    return (
        ScriptToolBroker(
            store=store,
            runtime_resolver=_RuntimeResolver(
                context,
                approval_events=events,
                approval_decision=approval_decision,
            ),
        ),
        token,
    )


def _request(token: str, *, call_id: str = "call-1", b: int = 2) -> ScriptToolCallRequest:
    return ScriptToolCallRequest(
        run_id="run-1",
        call_id=call_id,
        grant=ScriptToolGrant("calculator", "add"),
        arguments={"a": 1, "b": b},
        token=token,
    )


@pytest.mark.asyncio
async def test_script_broker_runs_normal_hook_and_wire_result_path(tmp_path: Path) -> None:
    """Removing the canonical hook bridge would skip events around the real registered tool."""
    events: list[str] = []
    broker, token = _broker(tmp_path, events=events)

    receipt = await broker.submit_call(_request(token))

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == '{"operation": "addition", "result": 3}'
    assert events == ["tool:before_call", "tool:after_call"]


@pytest.mark.asyncio
async def test_script_broker_requests_approval_before_body_and_denial_prevents_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable background origin must reach approval before the selected tool body."""
    events: list[str] = []

    class RecordingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def add(self, a: int, b: int) -> int:
            events.append("tool:body")
            return a + b

    monkeypatch.setattr(broker_module, "_build_current_toolkit", lambda _context, _grant: RecordingToolkit())
    broker, token = _broker(
        tmp_path,
        events=events,
        require_approval=True,
        approval_decision=ToolApprovalDecision(approved=False, reason="Not this time."),
    )

    receipt = await broker.submit_call(_request(token, call_id="approval-call"))

    assert receipt.state is ScriptCallState.DECLINED
    assert receipt.error == {
        "kind": "approval_declined",
        "message": "Not this time.",
        "retryable": False,
    }
    assert events == [
        "tool:before_call",
        "approval:run-1:approval-call",
        "tool:after_call",
    ]


@pytest.mark.asyncio
async def test_script_broker_returns_existing_receipt_without_reexecution(tmp_path: Path) -> None:
    """A duplicate stable call ID must not invoke the registered tool a second time."""
    events: list[str] = []
    broker, token = _broker(tmp_path, events=events)
    request = _request(token, call_id="stable-call")

    first = await broker.submit_call(request)
    second = await broker.submit_call(request)

    assert second == first
    assert events == ["tool:before_call", "tool:after_call"]


@pytest.mark.asyncio
async def test_script_broker_records_background_origin_and_durable_request_provenance(tmp_path: Path) -> None:
    """The ordinary audit log must correlate a script call without exposing its capability token."""
    broker, token = _broker(tmp_path, events=[], log_tool_calls=True)

    receipt = await broker.submit_call(_request(token, call_id="audited-call"))

    assert receipt.state is ScriptCallState.COMPLETED
    [record] = [
        json.loads(line)
        for line in (tracking_dir(_runtime_paths(tmp_path)) / "tool_calls.jsonl").read_text().splitlines()
    ]
    assert record["origin"] == "background_script"
    assert record["run_id"] == "run-1"
    assert record["call_id"] == "audited-call"
    assert record["requester_id"] == "@alice:example.test"
    assert record["toolkit_name"] == "calculator"
    assert record["function_name"] == "add"
    assert record["tool_name"] == "add"
    assert record["arguments"] == {"a": 1, "b": 2}
    assert record["correlation_id"] == "background-script:run-1:audited-call"
    assert token not in json.dumps(record)


@pytest.mark.asyncio
async def test_script_broker_keeps_toolkit_connected_while_materializing_generator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A generator result may still depend on its toolkit connection while it is consumed."""
    lifecycle: list[str] = []

    class ConnectedToolkit(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            self.connected = False
            super().__init__(name="calculator", tools=[self.add])

        def connect(self) -> None:
            self.connected = True
            lifecycle.append("connect")

        def close(self) -> None:
            self.connected = False
            lifecycle.append("close")

        def add(self, a: int, b: int) -> object:
            assert self.connected
            lifecycle.append("body")
            yield a
            yield b

    monkeypatch.setattr(broker_module, "_build_current_toolkit", lambda _context, _grant: ConnectedToolkit())
    broker, token = _broker(tmp_path, events=[])

    receipt = await broker.submit_call(_request(token, call_id="stream-call"))

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == [1, 2]
    assert lifecycle == ["connect", "body", "close"]


@pytest.mark.asyncio
async def test_script_broker_forgets_retained_execution_after_submitter_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shielded accepted work must finish durably without leaking its in-process replay owner."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        async def add(self, a: int, b: int) -> int:
            entered.set()
            await release.wait()
            return a + b

    monkeypatch.setattr(broker_module, "_build_current_toolkit", lambda _context, _grant: BlockingToolkit())
    broker, token = _broker(tmp_path, events=[])
    submission = asyncio.create_task(broker.submit_call(_request(token, call_id="cancelled-waiter")))
    await entered.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission

    [execution] = broker._tasks.values()
    release.set()
    receipt = await execution

    assert receipt.state is ScriptCallState.COMPLETED
    assert broker._tasks == {}


@pytest.mark.asyncio
async def test_script_broker_marks_unowned_pending_claim_indeterminate(tmp_path: Path) -> None:
    """A pending claim left by an unknown executor must never be resubmitted after ambiguity."""
    broker, token = _broker(tmp_path, events=[])
    request = _request(token, call_id="accepted-before-restart")
    broker.store.claim_call(
        run_id=request.run_id,
        call_id=request.call_id,
        grant=request.grant,
        arguments_digest=request.arguments_digest,
    )

    receipt = await broker.submit_call(request)

    assert receipt.state is ScriptCallState.INDETERMINATE
    assert receipt.error == {
        "kind": "indeterminate",
        "message": "The call was accepted, but its terminal result cannot be determined safely.",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_script_broker_rechecks_current_grants_before_execution(tmp_path: Path) -> None:
    """Removing the live function surface must revoke a launch-time grant before tool execution."""
    events: list[str] = []
    broker, token = _broker(tmp_path, events=events)
    removed = broker.runtime_resolver.context.config.model_copy(update={"agents": {}})
    broker.runtime_resolver.context = replace(
        broker.runtime_resolver.context,
        config_provider=lambda: removed,
    )

    receipt = await broker.submit_call(_request(token))

    assert receipt.state is ScriptCallState.FAILED
    assert receipt.error == {
        "kind": "capability_revoked",
        "message": "The requested tool is no longer available to this script run.",
        "retryable": False,
    }
    assert events == []
