"""Tests for the message:cancelled hook emission and workloop retry."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    ResponseHookService,
)
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.hooks import (
    EVENT_MESSAGE_AFTER_RESPONSE,
    EVENT_MESSAGE_BEFORE_RESPONSE,
    EVENT_MESSAGE_CANCELLED,
    AfterResponseContext,
    BeforeResponseContext,
    CancelledResponseContext,
    HookRegistry,
    MessageEnvelope,
    hook,
)
from mindroom.hooks.context import CancelledResponseInfo, HookContextSupport
from mindroom.hooks.execution import emit
from mindroom.hooks.registry import HookRegistryState
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(display_name="Code", rooms=["!room:localhost"]),
            },
        ),
        runtime_paths,
    )


def _plugin(name: str, callbacks: list[object]) -> object:
    return type(
        "PluginStub",
        (),
        {
            "name": name,
            "discovered_hooks": tuple(callbacks),
            "entry_config": PluginEntryConfig(path=f"./plugins/{name}"),
            "plugin_order": 0,
        },
    )()


def _envelope(*, agent_name: str = "code", body: str = "hello") -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$event",
        room_id="!room:localhost",
        target=MessageTarget.resolve("!room:localhost", None, "$event"),
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body=body,
        attachment_ids=(),
        mentioned_agents=(),
        agent_name=agent_name,
        source_kind="message",
    )


def _response_hook_service(tmp_path: Path, registry: HookRegistry) -> tuple[Config, ResponseHookService]:
    config = _config(tmp_path)
    rp = runtime_paths_for(config)
    hook_context = HookContextSupport(
        runtime=type("RT", (), {"client": None, "orchestrator": None, "config": config, "runtime_started_at": 0.0})(),
        logger=get_logger("tests"),
        runtime_paths=rp,
        agent_name="code",
        hook_registry_state=HookRegistryState(registry),
        hook_send_message=AsyncMock(),
    )
    return config, ResponseHookService(hook_context=hook_context)


@pytest.mark.asyncio
async def test_cancelled_hook_fires_on_emit(tmp_path: Path) -> None:
    """message:cancelled hook should fire when emitted."""
    seen: list[CancelledResponseInfo] = []

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        seen.append(ctx.info)

    registry = HookRegistry.from_plugins([_plugin("test-cancel", [on_cancelled])])
    config = _config(tmp_path)
    context = CancelledResponseContext(
        event_name=EVENT_MESSAGE_CANCELLED,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests").bind(event_name=EVENT_MESSAGE_CANCELLED),
        correlation_id="corr-cancel",
        info=CancelledResponseInfo(
            envelope=_envelope(),
            visible_response_event_id="$visible",
            response_kind="ai",
        ),
    )

    await emit(registry, EVENT_MESSAGE_CANCELLED, context)

    assert len(seen) == 1
    assert seen[0].visible_response_event_id == "$visible"
    assert seen[0].response_kind == "ai"
    assert seen[0].envelope.agent_name == "code"


@pytest.mark.asyncio
async def test_after_response_does_not_fire_on_cancelled_path(tmp_path: Path) -> None:
    """message:after_response hooks should NOT fire when only message:cancelled is emitted."""
    after_seen: list[str] = []
    cancelled_seen: list[str] = []

    @hook(EVENT_MESSAGE_AFTER_RESPONSE)
    async def on_after(ctx: AfterResponseContext) -> None:
        del ctx
        after_seen.append("after")

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        del ctx
        cancelled_seen.append("cancelled")

    registry = HookRegistry.from_plugins([_plugin("test-exclusive", [on_after, on_cancelled])])
    config = _config(tmp_path)

    cancel_ctx = CancelledResponseContext(
        event_name=EVENT_MESSAGE_CANCELLED,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests").bind(event_name=EVENT_MESSAGE_CANCELLED),
        correlation_id="corr-cancel",
        info=CancelledResponseInfo(
            envelope=_envelope(),
        ),
    )

    await emit(registry, EVENT_MESSAGE_CANCELLED, cancel_ctx)

    assert cancelled_seen == ["cancelled"]
    assert after_seen == [], "after_response must not fire when only cancelled is emitted"


@pytest.mark.asyncio
async def test_cancelled_context_preserves_envelope_fields(tmp_path: Path) -> None:
    """CancelledResponseContext should carry the original envelope and response metadata."""
    captured: list[CancelledResponseContext] = []

    @hook(EVENT_MESSAGE_CANCELLED)
    async def capture(ctx: CancelledResponseContext) -> None:
        captured.append(ctx)

    registry = HookRegistry.from_plugins([_plugin("test-envelope", [capture])])
    config = _config(tmp_path)
    envelope = _envelope(agent_name="research", body="do something")
    context = CancelledResponseContext(
        event_name=EVENT_MESSAGE_CANCELLED,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests").bind(event_name=EVENT_MESSAGE_CANCELLED),
        correlation_id="corr-fields",
        info=CancelledResponseInfo(
            envelope=envelope,
            visible_response_event_id="$partial_msg",
            response_kind="team",
        ),
    )

    await emit(registry, EVENT_MESSAGE_CANCELLED, context)

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.info.envelope.agent_name == "research"
    assert ctx.info.envelope.body == "do something"
    assert ctx.info.visible_response_event_id == "$partial_msg"
    assert ctx.info.response_kind == "team"
    assert ctx.correlation_id == "corr-fields"


@pytest.mark.asyncio
async def test_cancelled_hook_respects_agent_and_room_scope(tmp_path: Path) -> None:
    """Scoped message:cancelled hooks should match the cancelled envelope agent and room."""
    seen: list[str] = []

    @hook(EVENT_MESSAGE_CANCELLED, name="wrong-agent", agents=["research"])
    async def wrong_agent(ctx: CancelledResponseContext) -> None:
        del ctx
        seen.append("wrong-agent")

    @hook(EVENT_MESSAGE_CANCELLED, name="wrong-room", rooms=["!elsewhere:localhost"])
    async def wrong_room(ctx: CancelledResponseContext) -> None:
        del ctx
        seen.append("wrong-room")

    @hook(EVENT_MESSAGE_CANCELLED, name="matched", agents=["code"], rooms=["!room:localhost"])
    async def matched(ctx: CancelledResponseContext) -> None:
        del ctx
        seen.append("matched")

    registry = HookRegistry.from_plugins([_plugin("test-scoped-cancelled", [wrong_agent, wrong_room, matched])])
    config = _config(tmp_path)
    context = CancelledResponseContext(
        event_name=EVENT_MESSAGE_CANCELLED,
        plugin_name="",
        settings={},
        config=config,
        runtime_paths=runtime_paths_for(config),
        logger=get_logger("tests").bind(event_name=EVENT_MESSAGE_CANCELLED),
        correlation_id="corr-scoped-cancel",
        info=CancelledResponseInfo(
            envelope=_envelope(),
        ),
    )

    await emit(registry, EVENT_MESSAGE_CANCELLED, context)

    assert seen == ["matched"]


@pytest.mark.asyncio
async def test_response_hook_service_emit_cancelled(tmp_path: Path) -> None:
    """ResponseHookService.emit_cancelled_response should emit via the registry."""
    seen: list[CancelledResponseInfo] = []

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        seen.append(ctx.info)

    registry = HookRegistry.from_plugins([_plugin("test-service", [on_cancelled])])
    config = _config(tmp_path)
    rp = runtime_paths_for(config)

    hook_context = HookContextSupport(
        runtime=type("RT", (), {"client": None, "orchestrator": None, "config": config, "runtime_started_at": 0.0})(),
        logger=get_logger("tests"),
        runtime_paths=rp,
        agent_name="code",
        hook_registry_state=HookRegistryState(registry),
        hook_send_message=AsyncMock(),
    )
    service = ResponseHookService(hook_context=hook_context)

    await service.emit_cancelled_response(
        correlation_id="corr-svc",
        envelope=_envelope(),
        visible_response_event_id="$vis",
        response_kind="ai",
    )

    assert len(seen) == 1
    assert seen[0].visible_response_event_id == "$vis"


@pytest.mark.asyncio
async def test_response_hook_service_skips_when_no_hooks(tmp_path: Path) -> None:
    """emit_cancelled_response should be a no-op when no hooks are registered."""
    registry = HookRegistry.from_plugins([])
    config = _config(tmp_path)
    rp = runtime_paths_for(config)

    hook_context = HookContextSupport(
        runtime=type("RT", (), {"client": None, "orchestrator": None, "config": config, "runtime_started_at": 0.0})(),
        logger=get_logger("tests"),
        runtime_paths=rp,
        agent_name="code",
        hook_registry_state=HookRegistryState(registry),
        hook_send_message=AsyncMock(),
    )
    service = ResponseHookService(hook_context=hook_context)

    # Should not raise
    await service.emit_cancelled_response(
        correlation_id="corr-noop",
        envelope=_envelope(),
    )


@pytest.mark.asyncio
async def test_suppressed_final_delivery_emits_cancelled_hook(
    tmp_path: Path,
) -> None:
    """Hook-suppressed final delivery should still emit message:cancelled cleanup."""
    after_seen: list[str] = []
    cancelled_seen: list[CancelledResponseInfo] = []

    @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
    async def suppress_response(ctx: BeforeResponseContext) -> None:
        ctx.draft.suppress = True

    @hook(EVENT_MESSAGE_AFTER_RESPONSE)
    async def on_after(ctx: AfterResponseContext) -> None:
        del ctx
        after_seen.append("after")

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info)

    registry = HookRegistry.from_plugins(
        [_plugin("test-suppressed-cancelled", [suppress_response, on_after, on_cancelled])],
    )
    config, response_hooks = _response_hook_service(tmp_path, registry)
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=response_hooks.hook_context.runtime,
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=MagicMock(),
            response_hooks=response_hooks,
        ),
    )

    result = await gateway.deliver_final(
        FinalDeliveryRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$event"),
            existing_event_id=None,
            response_text="suppressed",
            response_kind="ai",
            response_envelope=_envelope(),
            correlation_id="corr-suppressed-final",
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert result.suppressed is True
    assert after_seen == []
    assert len(cancelled_seen) == 1
    assert cancelled_seen[0].envelope.agent_name == "code"
    assert cancelled_seen[0].visible_response_event_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_event_id", "expected_delivery_kind", "tracked_event_id"),
    [
        ("final", "$response", "sent", None),
        ("streamed", "$stream", "sent", "$stream"),
    ],
)
async def test_late_after_response_cancellation_preserves_delivery_result(
    tmp_path: Path,
    mode: str,
    expected_event_id: str,
    expected_delivery_kind: str,
    tracked_event_id: str | None,
) -> None:
    """Late cancellation during after_response must not downgrade a visible delivery to cancelled."""
    after_started = asyncio.Event()
    cancelled_seen: list[CancelledResponseInfo] = []

    @hook(EVENT_MESSAGE_AFTER_RESPONSE)
    async def slow_after_response(ctx: AfterResponseContext) -> None:
        del ctx
        after_started.set()
        await asyncio.Event().wait()

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info)

    registry = HookRegistry.from_plugins(
        [_plugin("test-late-after-cancel", [slow_after_response, on_cancelled])],
    )
    config, response_hooks = _response_hook_service(tmp_path, registry)
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=response_hooks.hook_context.runtime,
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=MagicMock(),
            response_hooks=response_hooks,
        ),
    )

    parsed = MagicMock()
    parsed.formatted_text = "visible response"
    parsed.option_map = None
    parsed.options_list = None

    delivery_result = None

    async def deliver_response() -> None:
        nonlocal delivery_result
        if mode == "final":
            delivery_result = await gateway.deliver_final(
                FinalDeliveryRequest(
                    target=MessageTarget.resolve("!room:localhost", None, "$event"),
                    existing_event_id=None,
                    response_text="visible response",
                    response_kind="ai",
                    response_envelope=_envelope(),
                    correlation_id="corr-late-final",
                    tool_trace=None,
                    extra_content=None,
                ),
            )
            return

        delivery_result = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=MessageTarget.resolve("!room:localhost", None, "$event"),
                stream_transport_outcome=StreamTransportOutcome(
                    last_physical_stream_event_id="$stream",
                    terminal_operation="send",
                    terminal_result="succeeded",
                    terminal_status="completed",
                    rendered_body="visible response",
                    visible_body_state="visible_body",
                ),
                initial_delivery_kind="sent",
                response_kind="ai",
                response_envelope=_envelope(),
                correlation_id="corr-late-streamed",
                tool_trace=None,
                extra_content=None,
            ),
        )

    with patch("mindroom.delivery_gateway.interactive.parse_and_format_interactive", return_value=parsed):
        if mode == "final":
            with patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$response")):
                task = asyncio.create_task(deliver_response())
                await asyncio.wait_for(after_started.wait(), timeout=1)
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        else:
            task = asyncio.create_task(deliver_response())
            await asyncio.wait_for(after_started.wait(), timeout=1)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    if delivery_result is None:
        await response_hooks.emit_cancelled_response(
            correlation_id=f"corr-coordinator-{mode}",
            envelope=_envelope(),
            visible_response_event_id=tracked_event_id,
            response_kind="ai",
        )

    assert delivery_result is not None
    assert delivery_result.event_id == expected_event_id
    assert delivery_result.delivery_kind == expected_delivery_kind
    assert delivery_result.response_text == "visible response"
    assert cancelled_seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_event_id", "expected_state", "expected_visible_event_id"),
    [
        (None, "error_without_visible_response", None),
        ("$existing", "kept_prior_visible_response_after_error", "$existing"),
    ],
)
async def test_deliver_final_delivery_failure_emits_cancelled_hook(
    tmp_path: Path,
    existing_event_id: str | None,
    expected_state: str,
    expected_visible_event_id: str | None,
) -> None:
    """Ordinary final send/edit failures must still emit exactly one cancelled hook."""
    cancelled_seen: list[CancelledResponseInfo] = []

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info)

    registry = HookRegistry.from_plugins([_plugin("test-delivery-failure", [on_cancelled])])
    config, response_hooks = _response_hook_service(tmp_path, registry)
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=response_hooks.hook_context.runtime,
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=MagicMock(),
            response_hooks=response_hooks,
        ),
    )

    parsed = MagicMock()
    parsed.formatted_text = "visible response"
    parsed.option_map = None
    parsed.options_list = None

    with (
        patch("mindroom.delivery_gateway.interactive.parse_and_format_interactive", return_value=parsed),
        patch.object(DeliveryGateway, "edit_text", new=AsyncMock(return_value=False)),
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value=None)),
    ):
        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!room:localhost", None, "$event"),
                existing_event_id=existing_event_id,
                existing_event_is_placeholder=False,
                response_text="visible response",
                response_kind="ai",
                response_envelope=_envelope(),
                correlation_id="corr-delivery-failure",
                tool_trace=None,
                extra_content=None,
            ),
        )

    assert outcome.state == expected_state
    assert len(cancelled_seen) == 1
    assert cancelled_seen[0].visible_response_event_id == expected_visible_event_id
    assert cancelled_seen[0].failure_reason == "delivery_failed"


@pytest.mark.asyncio
async def test_suppressed_placeholder_cleanup_failure_returns_typed_outcome_after_cleanup_attempt(
    tmp_path: Path,
) -> None:
    """Suppressed placeholder cleanup failure must not emit hooks before cleanup succeeds."""
    cancelled_seen: list[CancelledResponseInfo] = []

    @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
    async def suppress_response(ctx: BeforeResponseContext) -> None:
        ctx.draft.suppress = True

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info)

    registry = HookRegistry.from_plugins(
        [_plugin("test-suppression-cleanup-failure", [suppress_response, on_cancelled])],
    )
    config, response_hooks = _response_hook_service(tmp_path, registry)

    async def redact_message_event(*, room_id: str, event_id: str, reason: str) -> bool:
        del room_id, event_id, reason
        assert cancelled_seen == []
        return False

    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=response_hooks.hook_context.runtime,
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(side_effect=redact_message_event),
            sender_domain="localhost",
            resolver=MagicMock(),
            response_hooks=response_hooks,
        ),
    )

    outcome = await gateway.deliver_final(
        FinalDeliveryRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$event"),
            existing_event_id="$placeholder",
            existing_event_is_placeholder=True,
            response_text="suppressed",
            response_kind="ai",
            response_envelope=_envelope(),
            correlation_id="corr-suppressed-cleanup-fail",
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert outcome.state == "suppression_cleanup_failed"
    assert outcome.visible_response_event_id == "$placeholder"
    assert len(cancelled_seen) == 1
    assert cancelled_seen[0].visible_response_event_id == "$placeholder"


@pytest.mark.asyncio
async def test_suppressed_placeholder_cleanup_exception_returns_typed_outcome_after_cleanup_attempt(
    tmp_path: Path,
) -> None:
    """Redaction exceptions should still canonicalize to suppression_cleanup_failed."""
    cancelled_seen: list[CancelledResponseInfo] = []

    @hook(EVENT_MESSAGE_BEFORE_RESPONSE)
    async def suppress_response(ctx: BeforeResponseContext) -> None:
        ctx.draft.suppress = True

    @hook(EVENT_MESSAGE_CANCELLED)
    async def on_cancelled(ctx: CancelledResponseContext) -> None:
        cancelled_seen.append(ctx.info)

    registry = HookRegistry.from_plugins(
        [_plugin("test-suppression-cleanup-exception", [suppress_response, on_cancelled])],
    )
    config, response_hooks = _response_hook_service(tmp_path, registry)

    async def redact_message_event(*, room_id: str, event_id: str, reason: str) -> bool:
        del room_id, event_id, reason
        assert cancelled_seen == []
        message = "redaction transport failed"
        raise RuntimeError(message)

    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=response_hooks.hook_context.runtime,
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(side_effect=redact_message_event),
            sender_domain="localhost",
            resolver=MagicMock(),
            response_hooks=response_hooks,
        ),
    )

    outcome = await gateway.deliver_final(
        FinalDeliveryRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$event"),
            existing_event_id="$placeholder",
            existing_event_is_placeholder=True,
            response_text="suppressed",
            response_kind="ai",
            response_envelope=_envelope(),
            correlation_id="corr-suppressed-cleanup-exception",
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert outcome.state == "suppression_cleanup_failed"
    assert outcome.visible_response_event_id == "$placeholder"
    assert len(cancelled_seen) == 1
    assert cancelled_seen[0].visible_response_event_id == "$placeholder"
