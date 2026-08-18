"""Tests for direct background-script tool-call brokering."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from agno.tools import Toolkit
from agno.tools.function import FunctionCall

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
from mindroom.script_runs import broker as broker_module
from mindroom.script_runs.broker import (
    ScriptCallPreparationPendingError,
    ScriptCallReceipt,
    ScriptRuntimeWorkerAuthority,
    ScriptToolBroker,
    ScriptToolCallRequest,
)
from mindroom.script_runs.models import ScriptCallState, ScriptRunRecord, ScriptRunState, ScriptToolGrant
from mindroom.script_runs.store import ScriptRunStore, mint_script_capability
from mindroom.tool_approval import ToolApprovalDecision
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from mindroom.tool_system.worker_routing import WorkerScope, serialize_tool_execution_identity
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
        worker_id: str | None = None,
        private_agent_names: frozenset[str] | None = None,
        local_unsafe: bool = False,
    ) -> None:
        self.context = context
        self.approval_events = approval_events
        self.approval_decision = approval_decision or ToolApprovalDecision(approved=True)
        self.worker_id = worker_id
        self.private_agent_names = private_agent_names
        self.local_unsafe = local_unsafe
        self.approval_wait: asyncio.Event | None = None
        self.approval_started: asyncio.Event | None = None
        self.settled_approvals: list[tuple[BackgroundScriptToolOrigin, str]] = []

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
    durable_local_unsafe: bool | None = None,
    live_local_unsafe: bool | None = None,
    worker_scope: WorkerScope | None = None,
) -> tuple[ScriptToolBroker, str]:
    context = _context(
        tmp_path,
        hook_registry=_hook_registry(events),
        require_approval=require_approval,
        log_tool_calls=log_tool_calls,
        preapprove_script_tool=preapprove_script_tool,
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


async def _call_through_gateway(
    broker: ScriptToolBroker,
    request: ScriptToolCallRequest,
) -> ScriptCallReceipt:
    authorization = f"Bearer {request.token}"
    request_without_token = replace(request, token="")
    receipt = await broker.accept_authenticated(request_without_token, authorization)
    while receipt.state is ScriptCallState.PENDING:
        await asyncio.sleep(0)
        receipt = await broker.get_authenticated(request.run_id, request.call_id, authorization)
    return receipt


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

    receipt = await _call_through_gateway(broker, _request(token))

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
    broker, token = _broker(
        tmp_path,
        events=events,
        worker_scope=tool_worker_scope,
        durable_worker_key="v1:default:user_agent:@alice:example.test:watcher",
        durable_worker_id="script-process-worker",
        live_worker_id="script-process-worker",
    )

    receipt = await _call_through_gateway(broker, _request(token))

    assert receipt.state is ScriptCallState.COMPLETED
    assert receipt.result == '{"operation": "addition", "result": 3}'


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

    receipt = await _call_through_gateway(broker, _request(token, call_id="single-build"))

    assert receipt.state is ScriptCallState.COMPLETED
    assert builds == 1


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

    receipt = await _call_through_gateway(broker, _request(token, call_id="approval-call"))

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
    request = _request(token, call_id="cancelled-approval")
    accepted = await broker.accept_authenticated(replace(request, token=""), f"Bearer {token}")
    assert accepted.state is ScriptCallState.PENDING
    await resolver.approval_started.wait()
    broker.store.request_cancel(request.run_id, reason="run cancelled")

    await broker.cancel_run(request.run_id)

    receipt = broker.get_call(request.run_id, request.call_id)
    assert receipt.state is ScriptCallState.INDETERMINATE
    [(origin, reason)] = resolver.settled_approvals
    assert (origin.run_id, origin.call_id) == (request.run_id, request.call_id)
    assert reason == "Background script ownership was cancelled."


@pytest.mark.asyncio
async def test_orphaned_pending_receipt_settles_exact_approval(tmp_path: Path) -> None:
    """Restart orphan detection closes the approval paired with its indeterminate receipt."""
    broker, token = _broker(tmp_path, events=[])
    request = _request(token, call_id="orphaned-approval")
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
    monkeypatch.setattr(
        broker_module,
        "_script_allowed_toolkits",
        lambda _config, _agent_name: frozenset({"calculator"}),
    )
    broker, token = _broker(
        tmp_path,
        events=events,
        approval_decision=ToolApprovalDecision(approved=False, reason="Authored confirmation denied."),
        preapprove_script_tool=True,
    )

    receipt = await _call_through_gateway(broker, _request(token, call_id="authored-confirmation"))

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
    monkeypatch.setattr(
        broker_module,
        "_script_allowed_toolkits",
        lambda _config, _agent_name: frozenset({"calculator"}),
    )
    broker, token = _broker(
        tmp_path,
        events=events,
        approval_decision=ToolApprovalDecision(approved=False, reason="Cached result denied."),
        preapprove_script_tool=True,
    )

    receipt = await _call_through_gateway(broker, _request(token, call_id="cached-confirmation"))

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
    request = _request(token, call_id="stable-call")

    first = await _call_through_gateway(broker, request)
    second = await _call_through_gateway(broker, request)

    assert second == first
    assert events == ["tool:before_call", "tool:after_call"]


@pytest.mark.asyncio
async def test_script_broker_records_background_origin_and_durable_request_provenance(tmp_path: Path) -> None:
    """The ordinary audit log must correlate a script call without exposing its capability token."""
    broker, token = _broker(tmp_path, events=[], log_tool_calls=True)

    receipt = await _call_through_gateway(broker, _request(token, call_id="audited-call"))

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

    receipt = await _call_through_gateway(broker, _request(token, call_id="stream-call"))

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

    receipt = await _call_through_gateway(broker, _request(token, call_id=f"stream-bytes-{maximum_bytes}"))

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

    receipt = await _call_through_gateway(broker, _request(token, call_id="nonfinite-stream"))

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
    submission = asyncio.create_task(_call_through_gateway(broker, _request(token, call_id="blocking-connect")))

    await asyncio.sleep(0.01)

    assert time.monotonic() - started < 0.07
    assert (await submission).state is ScriptCallState.COMPLETED


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
    submission = asyncio.create_task(_call_through_gateway(broker, _request(token, call_id="blocking-close")))

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
    submission = asyncio.create_task(_call_through_gateway(broker, _request(token, call_id="cancelled-waiter")))
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
    request = _request(token, call_id="cancelled-run-call")
    accepted = await broker.accept_authenticated(replace(request, token=""), f"Bearer {token}")
    assert accepted.state is ScriptCallState.PENDING
    await entered.wait()
    broker.store.request_cancel(request.run_id)

    await broker.cancel_run(request.run_id)

    receipt = broker.get_call(request.run_id, request.call_id)
    assert receipt.state is ScriptCallState.INDETERMINATE
    assert broker._tasks == {}


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
    first_request = _request(token, call_id="first", b=2)
    second_request = _request(token, call_id="queued", b=3)
    await broker.accept_authenticated(replace(first_request, token=""), f"Bearer {token}")
    await first_entered.wait()
    await broker.accept_authenticated(replace(second_request, token=""), f"Bearer {token}")
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
    request = _request(token, call_id="accepted-while-starting")

    accepted = await broker.accept_authenticated(replace(request, token=""), f"Bearer {token}")
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
    request = _request(token, call_id="accepted-before-restart")
    broker.store.claim_call(
        run_id=request.run_id,
        call_id=request.call_id,
        grant=request.grant,
        arguments_digest=request.arguments_digest,
    )

    receipt = await _call_through_gateway(broker, request)

    assert receipt.state is ScriptCallState.INDETERMINATE
    assert receipt.error == {
        "kind": "indeterminate",
        "message": "The call was accepted, but its terminal result cannot be determined safely.",
        "retryable": False,
    }


def test_script_broker_get_marks_unowned_pending_claim_indeterminate(tmp_path: Path) -> None:
    """GET polling must resolve an accepted claim whose in-process owner disappeared."""
    broker, token = _broker(tmp_path, events=[])
    request = _request(token, call_id="orphaned-before-get")
    broker.store.claim_call(
        run_id=request.run_id,
        call_id=request.call_id,
        grant=request.grant,
        arguments_digest=request.arguments_digest,
    )

    receipt = broker.get_call(request.run_id, request.call_id)

    assert receipt.state is ScriptCallState.INDETERMINATE


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
    submission = asyncio.create_task(_call_through_gateway(broker, _request(token, call_id="owned-call")))
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
            replace(_request(token, call_id="preclaim-poll"), token=""),
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

    receipt = await _call_through_gateway(broker, _request(token, call_id=f"bad-{field_name}"))

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

    receipt = await _call_through_gateway(broker, _request(token, call_id="threadless"))

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

    receipt = await _call_through_gateway(broker, _request(token, call_id="wrong-worker"))

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

    receipt = await _call_through_gateway(broker, _request(token, call_id="wrong-worker-key"))

    assert receipt.state is ScriptCallState.FAILED
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

    receipt = await _call_through_gateway(broker, _request(token, call_id="wrong-private-scope"))

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

    receipt = await _call_through_gateway(broker, _request(token, call_id="wrong-local-mode"))

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
    first = asyncio.create_task(_call_through_gateway(broker, _request(token, call_id="serial-1", b=2)))
    await first_entered.wait()
    second = asyncio.create_task(_call_through_gateway(broker, _request(token, call_id="serial-2", b=3)))
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

    receipt = await _call_through_gateway(broker, _request(token, call_id=f"nonfinite-{result!s}"))

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

    receipt = await _call_through_gateway(broker, _request(token))

    assert receipt.state is ScriptCallState.FAILED
    assert receipt.error == {
        "kind": "capability_revoked",
        "message": "The requested tool is no longer available to this script run.",
        "retryable": False,
    }
    assert events == []
