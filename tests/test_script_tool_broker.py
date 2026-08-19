"""Tests for direct background-script tool-call brokering."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from agno.tools import Toolkit
from agno.tools.function import Function, FunctionCall

import mindroom.agents as agents_module
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
from mindroom.orchestration.script_runtime import ScriptRuntimeLifecycle
from mindroom.script_runs import broker as broker_module
from mindroom.script_runs.broker import (
    ScriptCallPreparationPendingError,
    ScriptRuntimeWorkerAuthority,
    ScriptToolBroker,
    ScriptToolCallRequest,
    drain_script_tool_cleanup,
)
from mindroom.script_runs.models import (
    ScriptCallRecord,
    ScriptCallState,
    ScriptRunRecord,
    ScriptRunState,
    ScriptToolGrant,
)
from mindroom.script_runs.store import ScriptCallConflictError, ScriptRunStore, mint_script_capability
from mindroom.tool_approval import ToolApprovalDecision
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.worker_routing import (
    ResolvedWorkerTarget,
    WorkerScope,
    resolved_worker_key_scope,
    serialize_tool_execution_identity,
)
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable
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


async def _cancel_cleanup_owner(
    broker: ScriptToolBroker,
    owner: asyncio.Task[None],
) -> asyncio.Task[None]:
    """Cancel one retained owner and return its explicit replacement."""
    replacement_ready = asyncio.Event()
    replacement: list[asyncio.Task[None]] = []

    def capture_replacement(_completed: asyncio.Task[None]) -> None:
        replacement.extend(task for task in broker._cleanup_tasks if task is not owner and not task.done())
        replacement_ready.set()

    owner.add_done_callback(capture_replacement)
    owner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner
    await asyncio.wait_for(replacement_ready.wait(), timeout=1.0)
    assert len(replacement) == 1
    return replacement[0]


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
    preapprove_script_tool: bool = False,
    tool_function_filter: Callable[[Function], bool] | None = None,
    worker_scope: WorkerScope | None = None,
) -> ToolRuntimeContext:
    runtime_paths = _runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "watcher": AgentConfig(
                    display_name="Watcher",
                    tools=["calculator"],
                    worker_scope=worker_scope,
                ),
            },
            defaults=DefaultsConfig(tools=[]),
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval={
                "rules": (
                    []
                    if preapprove_script_tool
                    else [
                        {
                            "match": "add",
                            "action": "require_approval" if require_approval else "auto_approve",
                        },
                    ]
                ),
            },
            debug={"log_llm_requests": log_tool_calls},
        ),
        runtime_paths,
    )
    context = make_test_tool_runtime_context(
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
    return replace(context, tool_function_filter=tool_function_filter)


class _RuntimeResolver:
    def __init__(
        self,
        context: ToolRuntimeContext,
        approval_events: list[str] | None = None,
        approval_decision: ToolApprovalDecision | None = None,
        worker_id: str | None = None,
        private_agent_names: frozenset[str] | None = None,
        local_unsafe: bool = False,
        resolved_worker_targets: list[ResolvedWorkerTarget] | None = None,
    ) -> None:
        self.context = context
        self.approval_events = approval_events
        self.approval_decision = approval_decision or ToolApprovalDecision(approved=True)
        self.worker_id = worker_id
        self.private_agent_names = private_agent_names
        self.local_unsafe = local_unsafe
        self.resolved_worker_targets = resolved_worker_targets
        self.approval_wait: asyncio.Event | None = None
        self.approval_started: asyncio.Event | None = None
        self.settled_approvals: list[tuple[BackgroundScriptToolOrigin, str]] = []
        self.settled_runs: list[tuple[str, str]] = []

    def resolve(self, run: ScriptRunRecord, *, correlation_id: str) -> ToolRuntimeContext:
        assert run.agent_name == "watcher"
        return replace(self.context, correlation_id=correlation_id)

    def resolve_worker_authority(
        self,
        run: ScriptRunRecord,
        *,
        context: ToolRuntimeContext,
    ) -> ScriptRuntimeWorkerAuthority:
        del run
        worker_target = context.resolve_worker_target()
        if self.private_agent_names is not None:
            worker_target = replace(worker_target, private_agent_names=self.private_agent_names)
        if self.resolved_worker_targets is not None:
            self.resolved_worker_targets.append(worker_target)
        return ScriptRuntimeWorkerAuthority(
            worker_id=self.worker_id,
            local_unsafe=self.local_unsafe,
            worker_target=worker_target,
        )

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
        if self.approval_started is not None:
            self.approval_started.set()
        if self.approval_wait is not None:
            await self.approval_wait.wait()
        return self.approval_decision

    async def settle_approval(self, origin: BackgroundScriptToolOrigin, *, reason: str) -> None:
        self.settled_approvals.append((origin, reason))

    async def settle_run_approvals(self, run_id: str, *, reason: str) -> None:
        self.settled_runs.append((run_id, reason))


def _broker(
    tmp_path: Path,
    *,
    events: list[str],
    require_approval: bool = False,
    log_tool_calls: bool = False,
    approval_decision: ToolApprovalDecision | None = None,
    execution_identity: dict[str, object] | None = None,
    thread_root_event_id: str | None = "$thread:example.test",
    durable_worker_id: str | None = None,
    durable_worker_key: str | None = None,
    live_worker_id: str | None = None,
    live_private_agent_names: frozenset[str] | None = None,
    preapprove_script_tool: bool = False,
    tool_function_filter: Callable[[Function], bool] | None = None,
    durable_local_unsafe: bool | None = None,
    live_local_unsafe: bool | None = None,
    worker_scope: WorkerScope | None = None,
    resolved_worker_targets: list[ResolvedWorkerTarget] | None = None,
) -> tuple[ScriptToolBroker, str]:
    context = _context(
        tmp_path,
        hook_registry=_hook_registry(events),
        require_approval=require_approval,
        log_tool_calls=log_tool_calls,
        preapprove_script_tool=preapprove_script_tool,
        tool_function_filter=tool_function_filter,
        worker_scope=worker_scope,
    )
    store = ScriptRunStore(context.runtime_paths)
    token, token_hash = mint_script_capability()
    resolved_durable_local_unsafe = durable_worker_key is None if durable_local_unsafe is None else durable_local_unsafe
    resolved_live_local_unsafe = resolved_durable_local_unsafe if live_local_unsafe is None else live_local_unsafe
    store.create_run(
        ScriptRunRecord(
            run_id="run-1",
            agent_name="watcher",
            owner_user_id=context.requester_id,
            room_id=context.room_id,
            thread_root_event_id=thread_root_event_id,
            execution_identity=(
                execution_identity
                if execution_identity is not None
                else serialize_tool_execution_identity(build_execution_identity_from_runtime_context(context))
            ),
            source_digest="source-digest",
            grants=(ScriptToolGrant("calculator", "add"),),
            token_hash=token_hash,
            preapprove_launch_grants=preapprove_script_tool,
            worker_key=durable_worker_key,
            worker_id=durable_worker_id,
            local_unsafe=resolved_durable_local_unsafe,
        ),
    )
    return (
        ScriptToolBroker(
            store=store,
            runtime_resolver=_RuntimeResolver(
                context,
                approval_events=events,
                approval_decision=approval_decision,
                worker_id=live_worker_id,
                private_agent_names=live_private_agent_names,
                local_unsafe=resolved_live_local_unsafe,
                resolved_worker_targets=resolved_worker_targets,
            ),
        ),
        token,
    )


def _request(*, call_id: str = "call-1", b: int = 2) -> ScriptToolCallRequest:
    return ScriptToolCallRequest(
        run_id="run-1",
        call_id=call_id,
        grant=ScriptToolGrant("calculator", "add"),
        arguments={"a": 1, "b": b},
    )


async def _call_through_gateway(
    broker: ScriptToolBroker,
    request: ScriptToolCallRequest,
    token: str,
) -> ScriptCallRecord:
    authorization = f"Bearer {token}"
    receipt = await broker.accept_authenticated(request, authorization)
    while receipt.state is ScriptCallState.PENDING:
        await asyncio.sleep(0)
        receipt = await broker.get_authenticated(request.run_id, request.call_id, authorization)
    return receipt


@pytest.mark.asyncio
async def test_script_broker_acceptance_returns_the_durable_call_record(tmp_path: Path) -> None:
    """Broker acceptance must not project a second field-for-field receipt type."""
    broker, token = _broker(tmp_path, events=[])

    accepted = await broker.accept_authenticated(_request(), f"Bearer {token}")

    assert isinstance(accepted, ScriptCallRecord)


def _replace_calculator_toolkit(
    monkeypatch: pytest.MonkeyPatch,
    build_replacement: Callable[[], Toolkit],
) -> None:
    original_build = agents_module.build_agent_toolkit

    def build_toolkit(tool_name: str, **kwargs: object) -> Toolkit | None:
        if tool_name == "calculator":
            return build_replacement()
        return original_build(tool_name, **kwargs)

    monkeypatch.setattr(agents_module, "build_agent_toolkit", build_toolkit)


@pytest.mark.asyncio
async def test_script_broker_runs_normal_hook_and_wire_result_path(tmp_path: Path) -> None:
    """Removing the canonical hook bridge would skip events around the real registered tool."""
    events: list[str] = []
    broker, token = _broker(tmp_path, events=events)

    receipt = await _call_through_gateway(broker, _request(), token)

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == '{"operation": "addition", "result": 3}'
    assert events == ["tool:before_call", "tool:after_call"]


@pytest.mark.parametrize("tool_worker_scope", ["shared", "user"])
@pytest.mark.asyncio
async def test_script_broker_separates_process_scope_from_tool_routing(
    tmp_path: Path,
    tool_worker_scope: WorkerScope,
) -> None:
    """The isolated script process does not override the called tool's configured worker target."""
    events: list[str] = []
    resolved_worker_targets: list[ResolvedWorkerTarget] = []
    broker, token = _broker(
        tmp_path,
        events=events,
        worker_scope=tool_worker_scope,
        durable_worker_key="v1:default:user_agent:@alice:example.test:watcher",
        durable_worker_id="script-process-worker",
        live_worker_id="script-process-worker",
        resolved_worker_targets=resolved_worker_targets,
    )

    receipt = await _call_through_gateway(broker, _request(), token)

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == '{"operation": "addition", "result": 3}'
    assert len(resolved_worker_targets) == 1
    resolved_worker_key = resolved_worker_targets[0].worker_key
    assert resolved_worker_key is not None
    assert resolved_worker_key_scope(resolved_worker_key) == tool_worker_scope
    assert resolved_worker_key != "v1:default:user_agent:@alice:example.test:watcher"


@pytest.mark.asyncio
async def test_script_broker_builds_the_selected_live_toolkit_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live authority resolution and execution must share one freshly built toolkit instance."""
    builds = 0
    original_build = agents_module.build_agent_toolkit

    def counting_build(tool_name: str, **kwargs: object) -> Toolkit | None:
        nonlocal builds
        if tool_name == "calculator":
            builds += 1
        return original_build(tool_name, **kwargs)

    monkeypatch.setattr(agents_module, "build_agent_toolkit", counting_build)
    broker, token = _broker(tmp_path, events=[])

    receipt = await _call_through_gateway(broker, _request(call_id="single-build"), token)

    assert receipt.state is ScriptCallState.COMPLETED
    assert builds == 1


@pytest.mark.asyncio
async def test_script_broker_closes_a_filtered_requested_toolkit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected live toolkit must finish its asynchronous close lifecycle."""
    closed = asyncio.Event()

    class AsyncClosingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])
            self._requires_connect = True

        def add(self, a: int, b: int) -> int:
            return a + b

    toolkit = AsyncClosingToolkit()

    async def close() -> None:
        closed.set()

    monkeypatch.setattr(toolkit, "close", close)
    _replace_calculator_toolkit(monkeypatch, lambda: toolkit)
    broker, token = _broker(
        tmp_path,
        events=[],
        tool_function_filter=lambda _function: False,
    )

    receipt = await _call_through_gateway(broker, _request(call_id="filtered-toolkit"), token)

    assert receipt.state is ScriptCallState.FAILED
    await asyncio.wait_for(closed.wait(), timeout=1.0)


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

    _replace_calculator_toolkit(monkeypatch, RecordingToolkit)
    broker, token = _broker(
        tmp_path,
        events=events,
        require_approval=True,
        approval_decision=ToolApprovalDecision(approved=False, reason="Not this time."),
    )

    receipt = await _call_through_gateway(broker, _request(call_id="approval-call"), token)

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == (
        "[TOOL CALL DECLINED]\n"
        "Tool: add\n"
        "Reason: Not this time.\n\n"
        "Adjust your approach — try a different tool or different arguments."
    )
    assert events == [
        "tool:before_call",
        "approval:run-1:approval-call",
        "tool:after_call",
    ]


@pytest.mark.asyncio
async def test_cancel_run_settles_pending_exact_approval(tmp_path: Path) -> None:
    """Cancelling broker ownership also makes its durable approval non-actionable."""
    events: list[str] = []
    broker, token = _broker(tmp_path, events=events, require_approval=True)
    resolver = cast("_RuntimeResolver", broker.runtime_resolver)
    resolver.approval_wait = asyncio.Event()
    resolver.approval_started = asyncio.Event()
    request = _request(call_id="cancelled-approval")
    accepted = await broker.accept_authenticated(request, f"Bearer {token}")
    assert accepted.state is ScriptCallState.PENDING
    await resolver.approval_started.wait()
    broker.store.request_cancel(request.run_id, reason="run cancelled")

    await asyncio.wait_for(broker.cancel_run(request.run_id), timeout=1.0)

    receipt = broker.get_call(request.run_id, request.call_id)
    assert receipt.state is ScriptCallState.INDETERMINATE
    assert broker._cleanup_tasks == set()
    [(run_id, reason)] = resolver.settled_runs
    assert run_id == request.run_id
    assert reason == "Background script ownership was cancelled."


@pytest.mark.asyncio
async def test_orphaned_pending_receipt_settles_exact_approval(tmp_path: Path) -> None:
    """Restart orphan detection closes the approval paired with its indeterminate receipt."""
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="orphaned-approval")
    broker.store.claim_call(
        run_id=request.run_id,
        call_id=request.call_id,
        grant=request.grant,
        arguments_digest=request.arguments_digest,
    )

    receipt = await broker.get_authenticated(
        request.run_id,
        request.call_id,
        f"Bearer {token}",
    )

    assert receipt.state is ScriptCallState.INDETERMINATE
    resolver = cast("_RuntimeResolver", broker.runtime_resolver)
    assert resolver.settled_approvals[0][0].call_id == request.call_id


@pytest.mark.asyncio
async def test_script_broker_honors_function_authored_confirmation_when_overlay_auto_approves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authored confirmation requirement cannot be erased by script preapproval."""
    events: list[str] = []

    class ConfirmingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])
            self.functions["add"].requires_confirmation = True

        def add(self, a: int, b: int) -> int:
            events.append("tool:body")
            return a + b

    _replace_calculator_toolkit(monkeypatch, ConfirmingToolkit)
    broker, token = _broker(
        tmp_path,
        events=events,
        approval_decision=ToolApprovalDecision(approved=False, reason="Authored confirmation denied."),
        preapprove_script_tool=True,
    )

    receipt = await _call_through_gateway(broker, _request(call_id="authored-confirmation"), token)

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == (
        "[TOOL CALL DECLINED]\n"
        "Tool: add\n"
        "Reason: Authored confirmation denied.\n\n"
        "Adjust your approach — try a different tool or different arguments."
    )
    assert events == [
        "approval:run-1:authored-confirmation",
        "tool:before_call",
        "tool:after_call",
    ]


@pytest.mark.asyncio
async def test_script_broker_honors_authored_confirmation_before_agno_cache_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached result cannot bypass a function-authored confirmation requirement."""
    events: list[str] = []

    class ConfirmingCachedToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])
            function = self.functions["add"]
            function.requires_confirmation = True
            function.cache_results = True
            function.cache_dir = str(tmp_path / "agno-cache")

        def add(self, a: int, b: int) -> int:
            events.append("tool:body")
            return a + b

    toolkit = ConfirmingCachedToolkit()
    cached = await FunctionCall(
        function=toolkit.functions["add"],
        arguments={"a": 1, "b": 2},
        call_id="cache-primer",
    ).aexecute()
    assert cached.result == 3
    events.clear()
    _replace_calculator_toolkit(monkeypatch, lambda: toolkit)
    broker, token = _broker(
        tmp_path,
        events=events,
        approval_decision=ToolApprovalDecision(approved=False, reason="Cached result denied."),
        preapprove_script_tool=True,
    )

    receipt = await _call_through_gateway(broker, _request(call_id="cached-confirmation"), token)

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == (
        "[TOOL CALL DECLINED]\n"
        "Tool: add\n"
        "Reason: Cached result denied.\n\n"
        "Adjust your approach — try a different tool or different arguments."
    )
    assert events == [
        "approval:run-1:cached-confirmation",
        "tool:before_call",
        "tool:after_call",
    ]


@pytest.mark.asyncio
async def test_script_broker_returns_existing_receipt_without_reexecution(tmp_path: Path) -> None:
    """A duplicate stable call ID must not invoke the registered tool a second time."""
    events: list[str] = []
    broker, token = _broker(tmp_path, events=events)
    request = _request(call_id="stable-call")

    first = await _call_through_gateway(broker, request, token)
    second = await _call_through_gateway(broker, request, token)

    assert second == first
    assert events == ["tool:before_call", "tool:after_call"]


@pytest.mark.asyncio
async def test_script_broker_records_background_origin_and_durable_request_provenance(tmp_path: Path) -> None:
    """The ordinary audit log must correlate a script call without exposing its capability token."""
    broker, token = _broker(tmp_path, events=[], log_tool_calls=True)

    receipt = await _call_through_gateway(broker, _request(call_id="audited-call"), token)

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

    _replace_calculator_toolkit(monkeypatch, ConnectedToolkit)
    broker, token = _broker(tmp_path, events=[])

    receipt = await _call_through_gateway(broker, _request(call_id="stream-call"), token)

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == [1, 2]
    assert lifecycle == ["connect", "body", "close"]


def test_append_bounded_result_tracks_exact_incremental_json_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream accounting includes UTF-8 bytes, brackets, and separators exactly once."""
    monkeypatch.setattr(broker_module, "_MAX_MATERIALIZED_RESULT_BYTES", 10)
    items: list[object] = []

    encoded_bytes = broker_module._append_bounded_result(items, "é", 2)
    encoded_bytes = broker_module._append_bounded_result(items, "x", encoded_bytes)

    assert encoded_bytes == 10
    assert items == ["é", "x"]
    with pytest.raises(ValueError, match="bounded result byte limit"):
        broker_module._append_bounded_result(items, "z", encoded_bytes)


@pytest.mark.asyncio
async def test_materialize_result_enforces_exact_stream_item_limit() -> None:
    """Exactly 1,000 stream items fit while item 1,001 is rejected."""
    expected = list(range(1_000))

    assert await broker_module._materialize_result(iter(expected)) == expected
    with pytest.raises(ValueError, match="bounded result item limit"):
        await broker_module._materialize_result(iter(range(1_001)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("maximum_bytes", "expected_state", "expected_error_kind"),
    [
        (10, ScriptCallState.COMPLETED, None),
        (9, ScriptCallState.FAILED, "call_rejected"),
    ],
)
async def test_script_broker_enforces_exact_stream_json_byte_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    maximum_bytes: int,
    expected_state: ScriptCallState,
    expected_error_kind: str | None,
) -> None:
    """The stream byte limit is exact for UTF-8 data and JSON list punctuation."""

    class BoundedToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def add(self, a: int, b: int) -> object:
            del a, b
            yield "é"
            yield "x"

    _replace_calculator_toolkit(monkeypatch, BoundedToolkit)
    monkeypatch.setattr(broker_module, "_MAX_MATERIALIZED_RESULT_BYTES", maximum_bytes)
    broker, token = _broker(tmp_path, events=[])

    receipt = await _call_through_gateway(broker, _request(call_id=f"stream-bytes-{maximum_bytes}"), token)

    assert receipt.state is expected_state
    if expected_error_kind is None:
        assert receipt.result == ["é", "x"]
    else:
        assert isinstance(receipt.error, dict)
        assert receipt.error["kind"] == expected_error_kind


@pytest.mark.asyncio
async def test_script_broker_rejects_nonfinite_stream_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Incremental stream encoding must retain strict non-finite rejection."""

    class NonFiniteStreamToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def add(self, a: int, b: int) -> object:
            del a, b
            yield float("nan")

    _replace_calculator_toolkit(monkeypatch, NonFiniteStreamToolkit)
    broker, token = _broker(tmp_path, events=[])

    receipt = await _call_through_gateway(broker, _request(call_id="nonfinite-stream"), token)

    assert receipt.state is ScriptCallState.FAILED
    assert isinstance(receipt.error, dict)
    assert receipt.error["kind"] == "call_rejected"


@pytest.mark.asyncio
async def test_script_broker_offloads_blocking_sync_toolkit_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous toolkit connect cannot block unrelated event-loop timers."""

    class BlockingConnectToolkit(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def connect(self) -> None:
            time.sleep(0.1)

        def close(self) -> None:
            pass

        def add(self, a: int, b: int) -> int:
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingConnectToolkit)
    broker, token = _broker(tmp_path, events=[])
    started = time.monotonic()
    submission = asyncio.create_task(_call_through_gateway(broker, _request(call_id="blocking-connect"), token))

    await asyncio.sleep(0.01)

    assert time.monotonic() - started < 0.07
    assert (await submission).state is ScriptCallState.COMPLETED


@pytest.mark.asyncio
async def test_cancelled_sync_connect_closes_only_after_connect_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation transfers a synchronous connect operation to the toolkit cleanup owner."""
    connect_started = threading.Event()
    release_connect = threading.Event()
    close_started = threading.Event()
    body_called = False

    class BlockingConnectToolkit(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def connect(self) -> None:
            connect_started.set()
            release_connect.wait()

        def close(self) -> None:
            close_started.set()

        def add(self, a: int, b: int) -> int:
            nonlocal body_called
            body_called = True
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingConnectToolkit)
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="cancelled-connect")
    accepted = await broker.accept_authenticated(request, f"Bearer {token}")
    assert accepted.state is ScriptCallState.PENDING
    assert await asyncio.to_thread(connect_started.wait, 1.0)
    broker.store.request_cancel(request.run_id)

    await asyncio.wait_for(broker.cancel_run(request.run_id), timeout=1.0)

    assert body_called is False
    assert close_started.is_set() is False
    cleanup_retained = bool(broker._cleanup_tasks)
    release_connect.set()
    close_after_connect = await asyncio.to_thread(close_started.wait, 1.0)
    cleanup_drained = await drain_script_tool_cleanup(broker, timeout_seconds=1.0)

    assert cleanup_retained is True
    assert close_after_connect is True
    assert cleanup_drained is True
    assert broker.get_call(request.run_id, request.call_id).state is ScriptCallState.INDETERMINATE
    assert broker._cleanup_tasks == set()


@pytest.mark.asyncio
async def test_script_broker_offloads_blocking_sync_toolkit_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous toolkit close cannot block unrelated event-loop timers."""

    class BlockingCloseToolkit(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def connect(self) -> None:
            pass

        def close(self) -> None:
            time.sleep(0.1)

        async def add(self, a: int, b: int) -> int:
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingCloseToolkit)
    broker, token = _broker(tmp_path, events=[])
    started = time.monotonic()
    submission = asyncio.create_task(_call_through_gateway(broker, _request(call_id="blocking-close"), token))

    await asyncio.sleep(0.01)

    assert time.monotonic() - started < 0.07
    assert (await submission).state is ScriptCallState.COMPLETED


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

    _replace_calculator_toolkit(monkeypatch, BlockingToolkit)
    broker, token = _broker(tmp_path, events=[])
    submission = asyncio.create_task(_call_through_gateway(broker, _request(call_id="cancelled-waiter"), token))
    await entered.wait()
    submission.cancel()

    with pytest.raises(asyncio.CancelledError):
        await submission

    [execution] = broker._tasks.values()
    release.set()
    receipt = await execution

    assert receipt.state is ScriptCallState.COMPLETED
    assert broker._tasks == {}
    assert broker._run_locks == {}


@pytest.mark.asyncio
async def test_script_broker_cancellation_waits_for_claim_schedule_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled submitter cannot return while a new durable claim lacks an execution owner."""
    claim_returned = threading.Event()
    release_claim = threading.Event()
    execution_started = asyncio.Event()
    release_execution = asyncio.Event()

    class BlockingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        async def add(self, a: int, b: int) -> int:
            execution_started.set()
            await release_execution.wait()
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingToolkit)
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="cancelled-claim")
    original_claim_call = broker.store.claim_call

    def claim_call(**kwargs: object) -> object:
        claim = original_claim_call(**kwargs)  # type: ignore[arg-type]
        claim_returned.set()
        assert release_claim.wait(timeout=1.0)
        return claim

    monkeypatch.setattr(broker.store, "claim_call", claim_call)
    submission = asyncio.create_task(broker.accept_authenticated(request, f"Bearer {token}"))
    assert await asyncio.to_thread(claim_returned.wait, 1.0)
    submission.cancel()
    await asyncio.sleep(0)

    assert not submission.done()

    release_claim.set()
    with pytest.raises(asyncio.CancelledError):
        await submission
    await execution_started.wait()

    execution = broker._tasks[(request.run_id, request.call_id)]
    release_execution.set()
    assert (await execution).state is ScriptCallState.COMPLETED


@pytest.mark.asyncio
async def test_script_broker_cancel_run_closes_accepted_receipt_as_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revocation cancellation cannot leave an accepted side-effecting call pending forever."""
    entered = asyncio.Event()

    class BlockingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        async def add(self, a: int, b: int) -> int:
            del a, b
            entered.set()
            await asyncio.Event().wait()
            return 0

    _replace_calculator_toolkit(monkeypatch, BlockingToolkit)
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="cancelled-run-call")
    accepted = await broker.accept_authenticated(request, f"Bearer {token}")
    assert accepted.state is ScriptCallState.PENDING
    await entered.wait()
    broker.store.request_cancel(request.run_id)

    await broker.cancel_run(request.run_id)

    receipt = broker.get_call(request.run_id, request.call_id)
    assert receipt.state is ScriptCallState.INDETERMINATE
    assert broker._tasks == {}


@pytest.mark.asyncio
async def test_cancelled_sync_tool_retains_cleanup_until_body_and_close_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation returns promptly while the detached sync body still owns its toolkit."""
    body_entered = threading.Event()
    release_body = threading.Event()
    close_started = threading.Event()
    release_close = threading.Event()
    close_finished = threading.Event()

    class BlockingSyncToolkit(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def connect(self) -> None:
            return None

        def close(self) -> None:
            close_started.set()
            release_close.wait()
            close_finished.set()

        def add(self, a: int, b: int) -> int:
            body_entered.set()
            release_body.wait()
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingSyncToolkit)
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="cancelled-sync-call")
    accepted = await broker.accept_authenticated(request, f"Bearer {token}")
    assert accepted.state is ScriptCallState.PENDING
    assert await asyncio.to_thread(body_entered.wait, 1.0)
    assert close_started.is_set() is False
    broker.store.request_cancel(request.run_id)

    cancellation = asyncio.create_task(broker.cancel_run(request.run_id))
    try:
        await asyncio.wait_for(asyncio.shield(cancellation), timeout=1.0)
    finally:
        close_started_before_body_returned = close_started.is_set()
        release_body.set()
        close_did_start = await asyncio.to_thread(close_started.wait, 1.0)
        close_finished_before_release = close_finished.is_set()
        release_close.set()
        await asyncio.wait_for(cancellation, timeout=1.0)
    assert await asyncio.to_thread(close_finished.wait, 1.0)
    cleanup_drained = await drain_script_tool_cleanup(broker, timeout_seconds=1.0)

    assert close_started_before_body_returned is False
    assert close_did_start is True
    assert close_finished_before_release is False
    receipt = broker.get_call(request.run_id, request.call_id)
    assert receipt.state is ScriptCallState.INDETERMINATE
    assert cleanup_drained is True
    assert broker._cleanup_tasks == set()


@pytest.mark.asyncio
async def test_cancelled_sync_close_keeps_one_owner_through_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated owner cancellation cannot make a blocking sync close appear drained."""
    body_entered = threading.Event()
    release_body = threading.Event()
    close_started = threading.Event()
    release_close = threading.Event()
    close_finished = threading.Event()
    close_calls = 0

    class BlockingSyncToolkit(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def connect(self) -> None:
            return None

        def close(self) -> None:
            nonlocal close_calls
            close_calls += 1
            close_started.set()
            release_close.wait()
            close_finished.set()

        def add(self, a: int, b: int) -> int:
            body_entered.set()
            release_body.wait()
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingSyncToolkit)
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="cancelled-sync-close")
    await broker.accept_authenticated(request, f"Bearer {token}")
    assert await asyncio.to_thread(body_entered.wait, 1.0)
    broker.store.request_cancel(request.run_id)
    await broker.cancel_run(request.run_id)

    [body_owner] = broker._cleanup_tasks
    first_close_owner = await _cancel_cleanup_owner(broker, body_owner)
    release_body.set()
    assert await asyncio.to_thread(close_started.wait, 1.0)

    try:
        second_close_owner = await _cancel_cleanup_owner(broker, first_close_owner)
        owned_after_first_cancel = bool(broker._cleanup_tasks)
        first_drain = await drain_script_tool_cleanup(broker, timeout_seconds=0)

        await _cancel_cleanup_owner(broker, second_close_owner)
        owned_after_second_cancel = bool(broker._cleanup_tasks)
        second_drain = await drain_script_tool_cleanup(broker, timeout_seconds=0)
        close_finished_before_release = close_finished.is_set()
    finally:
        release_close.set()

    assert await asyncio.to_thread(close_finished.wait, 1.0)
    assert await drain_script_tool_cleanup(broker, timeout_seconds=1.0)

    assert owned_after_first_cancel is True
    assert first_drain is False
    assert owned_after_second_cancel is True
    assert second_drain is False
    assert close_finished_before_release is False
    assert close_calls == 1
    assert broker._cleanup_tasks == set()


@pytest.mark.asyncio
async def test_cancelled_async_tool_close_keeps_one_owner_through_repeated_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async-body cancellation cannot leave a blocking synchronous close unowned."""
    body_entered = asyncio.Event()
    close_started = threading.Event()
    release_close = threading.Event()
    close_finished = threading.Event()

    class BlockingCloseToolkit(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def connect(self) -> None:
            return None

        def close(self) -> None:
            close_started.set()
            release_close.wait()
            close_finished.set()

        async def add(self, a: int, b: int) -> int:
            body_entered.set()
            await asyncio.Event().wait()
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingCloseToolkit)
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="cancelled-async-close")
    await broker.accept_authenticated(request, f"Bearer {token}")
    await body_entered.wait()
    broker.store.request_cancel(request.run_id)

    cancellation = asyncio.create_task(broker.cancel_run(request.run_id))
    assert await asyncio.to_thread(close_started.wait, 1.0)
    try:
        await asyncio.wait_for(cancellation, timeout=1.0)
        [first_close_owner] = broker._cleanup_tasks
        second_close_owner = await _cancel_cleanup_owner(broker, first_close_owner)
        await _cancel_cleanup_owner(broker, second_close_owner)
        cleanup_retained = bool(broker._cleanup_tasks)
        drained_before_release = await drain_script_tool_cleanup(broker, timeout_seconds=0)
        close_finished_before_release = close_finished.is_set()
    finally:
        release_close.set()

    assert await asyncio.to_thread(close_finished.wait, 1.0)
    assert await drain_script_tool_cleanup(broker, timeout_seconds=1.0)
    assert cleanup_retained is True
    assert drained_before_release is False
    assert close_finished_before_release is False
    assert broker._cleanup_tasks == set()


@pytest.mark.asyncio
async def test_lifecycle_shutdown_bounds_and_preserves_cancelled_sync_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded shutdown drains retained cleanup without cancelling its completion owner."""
    body_entered = threading.Event()
    release_body = threading.Event()
    close_started = threading.Event()

    class BlockingSyncToolkit(Toolkit):
        _requires_connect = True

        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def connect(self) -> None:
            return None

        def close(self) -> None:
            close_started.set()

        def add(self, a: int, b: int) -> int:
            body_entered.set()
            release_body.wait()
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingSyncToolkit)
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="shutdown-sync-cleanup")
    await broker.accept_authenticated(request, f"Bearer {token}")
    assert await asyncio.to_thread(body_entered.wait, 1.0)
    broker.store.request_cancel(request.run_id)
    await broker.cancel_run(request.run_id)
    [original_cleanup] = broker._cleanup_tasks

    lifecycle = ScriptRuntimeLifecycle(
        runtime_paths=_runtime_paths(tmp_path),
        store=broker.store,
        broker=broker,
        manager=SimpleNamespace(worker_backend=None, worker_backend_generation=None),
        resolver=SimpleNamespace(),
        config_provider=lambda: None,
        worker_lease_provider=lambda: None,
    )
    lifecycle._activated_once = True
    monkeypatch.setattr(ScriptRuntimeLifecycle, "_complete_pass", AsyncMock())
    drain_started = asyncio.Event()
    original_drain = broker_module.drain_script_tool_cleanup

    async def observing_drain(
        observed_broker: ScriptToolBroker,
        *,
        timeout_seconds: float,
    ) -> bool:
        drain_started.set()
        return await original_drain(observed_broker, timeout_seconds=timeout_seconds)

    monkeypatch.setattr("mindroom.orchestration.script_runtime.drain_script_tool_cleanup", observing_drain)
    shutdown = asyncio.create_task(lifecycle.shutdown(timeout_seconds=0.05))
    await asyncio.wait_for(drain_started.wait(), timeout=1.0)

    shutdown_waited_for_cleanup = shutdown.done() is False
    await _cancel_cleanup_owner(broker, original_cleanup)
    close_started_before_body_returned = close_started.is_set()
    await asyncio.wait_for(shutdown, timeout=0.2)
    cleanup_owned_after_timeout = bool(broker._cleanup_tasks)

    release_body.set()
    assert await asyncio.to_thread(close_started.wait, 1.0)
    cleanup_drained = await drain_script_tool_cleanup(broker, timeout_seconds=1.0)

    assert shutdown_waited_for_cleanup is True
    assert close_started_before_body_returned is False
    assert cleanup_owned_after_timeout is True
    assert cleanup_drained is True
    assert broker._cleanup_tasks == set()


@pytest.mark.asyncio
async def test_queued_script_call_rechecks_durable_revocation_after_run_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued accepted call never enters its tool after the run is revoked."""
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    executed_values: list[int] = []

    class SerialToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        async def add(self, a: int, b: int) -> int:
            del a
            executed_values.append(b)
            if b == 2:
                first_entered.set()
                await release_first.wait()
            return b

    _replace_calculator_toolkit(monkeypatch, SerialToolkit)
    broker, token = _broker(tmp_path, events=[])
    first_request = _request(call_id="first", b=2)
    second_request = _request(call_id="queued", b=3)
    await broker.accept_authenticated(first_request, f"Bearer {token}")
    await first_entered.wait()
    await broker.accept_authenticated(second_request, f"Bearer {token}")
    broker.store.request_cancel(first_request.run_id)
    release_first.set()

    [first_task, second_task] = list(broker._tasks.values())
    await first_task
    second_receipt = await second_task

    assert executed_values == [2]
    assert second_receipt.state is ScriptCallState.FAILED
    assert second_receipt.error == {
        "kind": "capability_revoked",
        "message": "The requested tool is no longer available to this script run.",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_queued_starting_call_dispatches_with_fresh_durable_worker_identity(tmp_path: Path) -> None:
    """A call accepted during launch uses worker identity persisted before its eventual dispatch."""
    broker, token = _broker(tmp_path, events=[], live_worker_id="worker-after-launch")
    run_lock = broker._run_locks.setdefault("run-1", asyncio.Lock())
    await run_lock.acquire()
    request = _request(call_id="accepted-while-starting")

    accepted = await broker.accept_authenticated(request, f"Bearer {token}")
    assert accepted.state is ScriptCallState.PENDING
    broker.store.transition_run(
        request.run_id,
        state=ScriptRunState.RUNNING,
        worker_id="worker-after-launch",
        supervisor_handle="shell:1234abcd",
    )
    run_lock.release()
    [execution] = broker._tasks.values()

    receipt = await execution

    assert receipt.state is ScriptCallState.COMPLETED


@pytest.mark.asyncio
async def test_script_broker_marks_unowned_pending_claim_indeterminate(tmp_path: Path) -> None:
    """A pending claim left by an unknown executor must never be resubmitted after ambiguity."""
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="accepted-before-restart")
    broker.store.claim_call(
        run_id=request.run_id,
        call_id=request.call_id,
        grant=request.grant,
        arguments_digest=request.arguments_digest,
    )

    receipt = await _call_through_gateway(broker, request, token)

    assert receipt.state is ScriptCallState.INDETERMINATE
    assert receipt.error == {
        "kind": "indeterminate",
        "message": "The call was accepted, but its terminal result cannot be determined safely.",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_duplicate_post_returns_terminal_winner_when_claim_races_orphan_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A duplicate stale pending claim must return the receipt that won durably."""
    broker, token = _broker(tmp_path, events=[])
    request = _request(call_id="racing-duplicate-post")
    broker.store.claim_call(
        run_id=request.run_id,
        call_id=request.call_id,
        grant=request.grant,
        arguments_digest=request.arguments_digest,
    )
    original_claim_call = broker.store.claim_call
    stale_claim_returned = threading.Event()
    release_stale_claim = threading.Event()

    def pause_after_stale_claim(**kwargs: object) -> object:
        claim = original_claim_call(**kwargs)  # type: ignore[arg-type]
        stale_claim_returned.set()
        assert release_stale_claim.wait(timeout=1.0)
        return claim

    monkeypatch.setattr(broker.store, "claim_call", pause_after_stale_claim)
    submission = asyncio.create_task(broker.accept_authenticated(request, f"Bearer {token}"))
    assert await asyncio.to_thread(stale_claim_returned.wait, 1.0)
    published = broker.store.publish_call_result(
        run_id=request.run_id,
        call_id=request.call_id,
        state=ScriptCallState.COMPLETED,
        result=3,
    )
    release_stale_claim.set()
    receipt = await submission

    assert published.state is ScriptCallState.COMPLETED
    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == 3
    assert broker.store.get_call(request.run_id, request.call_id) == published


def test_script_broker_get_marks_unowned_pending_claim_indeterminate(tmp_path: Path) -> None:
    """GET polling must resolve an accepted claim whose in-process owner disappeared."""
    broker, _token = _broker(tmp_path, events=[])
    request = _request(call_id="orphaned-before-get")
    broker.store.claim_call(
        run_id=request.run_id,
        call_id=request.call_id,
        grant=request.grant,
        arguments_digest=request.arguments_digest,
    )

    receipt = broker.get_call(request.run_id, request.call_id)

    assert receipt.state is ScriptCallState.INDETERMINATE
    with pytest.raises(ScriptCallConflictError, match="terminal receipt"):
        broker.store.publish_call_result(
            run_id=request.run_id,
            call_id=request.call_id,
            state=ScriptCallState.COMPLETED,
            result=3,
        )
    assert broker.get_call(request.run_id, request.call_id) == receipt


def test_script_broker_get_returns_terminal_winner_when_publish_races_orphan_settlement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale pending read cannot conflict with an execution receipt that wins durably."""
    broker, _token = _broker(tmp_path, events=[])
    request = _request(call_id="racing-poll")
    broker.store.claim_call(
        run_id=request.run_id,
        call_id=request.call_id,
        grant=request.grant,
        arguments_digest=request.arguments_digest,
    )
    original_get_call = broker.store.get_call
    pending_read = threading.Event()
    release_stale_read = threading.Event()

    def pause_after_pending_read(run_id: str, call_id: str) -> ScriptCallRecord:
        record = original_get_call(run_id, call_id)
        if record.state is ScriptCallState.PENDING:
            pending_read.set()
            assert release_stale_read.wait(timeout=1.0)
        return record

    monkeypatch.setattr(broker.store, "get_call", pause_after_pending_read)
    with ThreadPoolExecutor(max_workers=1) as executor:
        polling = executor.submit(broker.get_call, request.run_id, request.call_id)
        assert pending_read.wait(timeout=1.0)
        published = broker.store.publish_call_result(
            run_id=request.run_id,
            call_id=request.call_id,
            state=ScriptCallState.COMPLETED,
            result=3,
        )
        release_stale_read.set()
        receipt = polling.result(timeout=1.0)

    assert published.state is ScriptCallState.COMPLETED
    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == 3
    assert original_get_call(request.run_id, request.call_id) == published


@pytest.mark.asyncio
async def test_script_broker_get_keeps_owned_execution_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling cannot orphan a pending claim while its retained task is alive."""
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        async def add(self, a: int, b: int) -> int:
            entered.set()
            await release.wait()
            return a + b

    _replace_calculator_toolkit(monkeypatch, BlockingToolkit)
    broker, token = _broker(tmp_path, events=[])
    submission = asyncio.create_task(_call_through_gateway(broker, _request(call_id="owned-call"), token))
    await entered.wait()

    assert broker.get_call("run-1", "owned-call").state is ScriptCallState.PENDING

    release.set()
    assert (await submission).state is ScriptCallState.COMPLETED


@pytest.mark.asyncio
async def test_script_broker_get_reports_retryable_preclaim_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling an in-flight acceptance cannot fabricate a durable pending receipt."""
    broker, token = _broker(tmp_path, events=[])
    original_require = broker.store.require_active_capability
    preparation_started = threading.Event()
    release_preparation = threading.Event()

    def blocking_require(run_id: str, capability: str) -> ScriptRunRecord:
        preparation_started.set()
        assert release_preparation.wait(timeout=1)
        return original_require(run_id, capability)

    monkeypatch.setattr(broker.store, "require_active_capability", blocking_require)
    acceptance = asyncio.create_task(
        broker.accept_authenticated(
            _request(call_id="preclaim-poll"),
            f"Bearer {token}",
        ),
    )
    assert await asyncio.to_thread(preparation_started.wait, 1)

    with pytest.raises(ScriptCallPreparationPendingError):
        await broker.get_authenticated(
            "run-1",
            "preclaim-poll",
            f"Bearer {token}",
        )

    release_preparation.set()
    assert (await acceptance).state is ScriptCallState.PENDING
    [execution] = broker._tasks.values()
    assert (await execution).state is ScriptCallState.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "mismatched_value"),
    [
        ("channel", "openai_compat"),
        ("agent_name", "other-agent"),
        ("requester_id", "@mallory:example.test"),
        ("room_id", "!other:example.test"),
        ("thread_id", "$other:example.test"),
        ("resolved_thread_id", "$other:example.test"),
        ("session_id", "other-session"),
        ("tenant_id", "other-tenant"),
        ("account_id", "other-account"),
        ("transport_agent_name", "other-agent"),
    ],
)
async def test_script_broker_rejects_durable_execution_identity_mismatch_before_dispatch(
    tmp_path: Path,
    field_name: str,
    mismatched_value: str,
) -> None:
    """Every durable dispatch identity field must match the rebuilt live context."""
    context = _context(tmp_path, hook_registry=_hook_registry([]))
    identity = serialize_tool_execution_identity(build_execution_identity_from_runtime_context(context))
    identity[field_name] = mismatched_value
    events: list[str] = []
    broker, token = _broker(tmp_path, events=events, execution_identity=identity)

    receipt = await _call_through_gateway(broker, _request(call_id=f"bad-{field_name}"), token)

    assert receipt.state is ScriptCallState.FAILED
    assert events == []


@pytest.mark.asyncio
async def test_script_broker_rejects_live_thread_when_durable_thread_is_none(tmp_path: Path) -> None:
    """A threadless durable authority cannot expand to an arbitrary live thread."""
    context = _context(tmp_path, hook_registry=_hook_registry([]))
    identity = serialize_tool_execution_identity(build_execution_identity_from_runtime_context(context))
    identity["thread_id"] = None
    identity["resolved_thread_id"] = None
    events: list[str] = []
    broker, token = _broker(
        tmp_path,
        events=events,
        execution_identity=identity,
        thread_root_event_id=None,
    )

    receipt = await _call_through_gateway(broker, _request(call_id="threadless"), token)

    assert receipt.state is ScriptCallState.FAILED
    assert events == []


@pytest.mark.asyncio
async def test_script_broker_rejects_durable_live_worker_mismatch_before_dispatch(tmp_path: Path) -> None:
    """A run may dispatch only through the same live worker authority that launched it."""
    events: list[str] = []
    broker, token = _broker(
        tmp_path,
        events=events,
        durable_worker_id="worker-a",
        live_worker_id="worker-b",
    )

    receipt = await _call_through_gateway(broker, _request(call_id="wrong-worker"), token)

    assert receipt.state is ScriptCallState.FAILED
    assert events == []


@pytest.mark.asyncio
async def test_script_broker_rejects_durable_worker_key_mismatch_before_dispatch(tmp_path: Path) -> None:
    """A run cannot substitute another requester-scoped worker key at dispatch."""
    events: list[str] = []
    broker, token = _broker(
        tmp_path,
        events=events,
        durable_worker_key="v1:default:user_agent:mallory:watcher",
    )

    receipt = await _call_through_gateway(broker, _request(call_id="wrong-worker-key"), token)

    assert receipt.state is ScriptCallState.FAILED
    assert isinstance(receipt.error, dict)
    assert "worker key" in str(receipt.error.get("message", "")).lower()
    assert events == []


@pytest.mark.asyncio
async def test_script_broker_rejects_live_private_scope_mismatch_before_dispatch(tmp_path: Path) -> None:
    """Resolver-provided private routing must match the current config-derived target."""
    events: list[str] = []
    broker, token = _broker(
        tmp_path,
        events=events,
        live_private_agent_names=frozenset({"watcher"}),
    )

    receipt = await _call_through_gateway(broker, _request(call_id="wrong-private-scope"), token)

    assert receipt.state is ScriptCallState.FAILED
    assert events == []


@pytest.mark.asyncio
async def test_script_broker_rejects_durable_live_local_execution_mismatch(tmp_path: Path) -> None:
    """The live execution mode must match the durable unsafe-local authority bit."""
    events: list[str] = []
    broker, token = _broker(
        tmp_path,
        events=events,
        durable_local_unsafe=True,
        live_local_unsafe=False,
    )

    receipt = await _call_through_gateway(broker, _request(call_id="wrong-local-mode"), token)

    assert receipt.state is ScriptCallState.FAILED
    assert events == []


@pytest.mark.asyncio
async def test_script_broker_serializes_distinct_calls_within_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first release permits only one active tool body per durable run."""
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = asyncio.Event()

    class SerialToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        async def add(self, a: int, b: int) -> int:
            if b == 2:
                first_entered.set()
                await release_first.wait()
            else:
                second_entered.set()
            return a + b

    _replace_calculator_toolkit(monkeypatch, SerialToolkit)
    broker, token = _broker(tmp_path, events=[])
    first = asyncio.create_task(_call_through_gateway(broker, _request(call_id="serial-1", b=2), token))
    await first_entered.wait()
    second = asyncio.create_task(_call_through_gateway(broker, _request(call_id="serial-2", b=3), token))
    await asyncio.sleep(0.02)

    assert not second_entered.is_set()

    release_first.set()
    first_receipt, second_receipt = await asyncio.gather(first, second)
    assert first_receipt.state is ScriptCallState.COMPLETED
    assert second_receipt.state is ScriptCallState.COMPLETED
    assert second_entered.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [float("nan"), float("inf"), float("-inf")])
async def test_script_broker_never_publishes_nonfinite_completed_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    result: float,
) -> None:
    """Non-finite tool output must become a readable terminal failure."""

    class NonFiniteToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.add])

        def add(self, a: int, b: int) -> float:
            del a, b
            return result

    _replace_calculator_toolkit(monkeypatch, NonFiniteToolkit)
    broker, token = _broker(tmp_path, events=[])

    receipt = await _call_through_gateway(broker, _request(call_id=f"nonfinite-{result!s}"), token)

    assert receipt.state is ScriptCallState.FAILED
    json.dumps(receipt.result, allow_nan=False)
    json.dumps(receipt.error, allow_nan=False)


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

    receipt = await _call_through_gateway(broker, _request(), token)

    assert receipt.state is ScriptCallState.FAILED
    assert receipt.error == {
        "kind": "capability_revoked",
        "message": "The requested tool is no longer available to this script run.",
        "retryable": False,
    }
    assert events == []
