"""Direct tests for the one-way ingress -> execution payload seam.

These exercise :class:`ResponsePayloadPreparer` (the execution-side, under-lock
payload-assembly step) on its own, plus the response runner's guarantee that the
step runs exactly once while the lifecycle lock is held.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.conversation_resolver import MessageContext
from mindroom.final_delivery import FinalDeliveryOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparation
from mindroom.response_runner import ResponseRequest
from mindroom.turn_policy import PreparedDispatch
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    from mindroom.dispatch_handoff import MediaDispatchEvent
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", rooms=["!room:localhost"])},
            teams={},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        test_runtime_paths(tmp_path),
    )


def _bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = AgentBot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!room:localhost"])
    bot.client = AsyncMock(spec=nio.AsyncClient)
    install_runtime_cache_support(bot)
    wrap_extracted_collaborators(bot)
    return bot


def _target(thread_id: str | None = None) -> MessageTarget:
    return MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id=thread_id,
        reply_to_event_id="$event",
        room_mode=thread_id is None,
    )


def _envelope(target: MessageTarget) -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$event",
        room_id="!room:localhost",
        target=target,
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        source_kind="message",
        origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="message"),
    )


def _preparation(
    target: MessageTarget,
    *,
    prompt: str = "raw prompt",
    action_kind: str = "individual",
    message_attachment_ids: tuple[str, ...] = (),
    media_events: tuple[MediaDispatchEvent, ...] = (),
) -> ResponsePayloadPreparation:
    return ResponsePayloadPreparation(
        dispatch=PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=target.resolved_thread_id is not None,
                thread_id=target.resolved_thread_id,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
            target=target,
            correlation_id="$event",
            envelope=_envelope(target),
        ),
        prompt=prompt,
        action_kind=action_kind,
        payload_inputs=DispatchPayloadInputs(
            message_attachment_ids=message_attachment_ids,
            trusted_attachment_ids=(),
            media_events=media_events,
        ),
        target_member_names=None,
        dispatch_started_at=1.0,
        context_ready_monotonic=2.0,
    )


def _request(
    target: MessageTarget,
    preparation: ResponsePayloadPreparation,
    *,
    thread_history: Sequence[ResolvedVisibleMessage],
) -> ResponseRequest:
    return ResponseRequest(
        thread_history=thread_history,
        prompt=preparation.prompt,
        user_id="@user:localhost",
        response_envelope=_envelope(target),
        payload_preparation=preparation,
    )


@pytest.mark.asyncio
async def test_prepare_builds_request_from_inputs_and_refreshed_history(tmp_path: Path) -> None:
    """The preparer builds the final payload from the refreshed history and clears itself."""
    bot = _bot(tmp_path)
    preparer = bot._request_payload_preparer
    target = _target()
    refreshed_history = ThreadHistoryResult([], is_full_history=True)
    seen: list[object] = []

    async def fake_build(request: object) -> DispatchPayload:
        seen.append(request)
        return DispatchPayload(prompt="built prompt", model_prompt="model", attachment_ids=["att-1"])

    with patch.object(
        bot._inbound_turn_normalizer,
        "build_dispatch_payload_with_attachments",
        new=AsyncMock(side_effect=fake_build),
    ):
        result = await preparer.prepare(
            _request(target, _preparation(target, message_attachment_ids=("att-1",)), thread_history=refreshed_history),
        )

    assert result.prompt == "built prompt"
    assert result.model_prompt == "model"
    assert result.attachment_ids == ("att-1",)
    assert result.payload_preparation is None
    assert result.requires_model_history_refresh is False
    assert result.thread_history is refreshed_history
    # The under-lock build consumes the refreshed history and the raw prompt.
    build_request = seen[0]
    assert build_request.thread_history is refreshed_history
    assert build_request.prompt == "raw prompt"


@pytest.mark.asyncio
async def test_prepare_logs_startup_latency_fields(tmp_path: Path) -> None:
    """The preparer emits one startup-latency log including thread diagnostics."""
    bot = _bot(tmp_path)
    preparer = bot._request_payload_preparer
    preparer.logger = MagicMock()
    target = _target()
    refreshed_history = ThreadHistoryResult(
        [],
        is_full_history=True,
        diagnostics={"cache_read_ms": 11.0, "resolution_ms": 33.0},
    )

    with patch.object(
        bot._inbound_turn_normalizer,
        "build_dispatch_payload_with_attachments",
        new=AsyncMock(return_value=DispatchPayload(prompt="built")),
    ):
        await preparer.prepare(
            _request(target, _preparation(target, action_kind="team"), thread_history=refreshed_history),
        )

    latency_logs = [
        call for call in preparer.logger.info.call_args_list if call.args and call.args[0] == "Response startup latency"
    ]
    assert len(latency_logs) == 1
    kwargs = latency_logs[0].kwargs
    assert kwargs["action_kind"] == "team"
    assert kwargs["cache_read_ms"] == 11.0
    assert kwargs["resolution_ms"] == 33.0
    assert kwargs["context_hydration_ms"] == 1000.0  # (2.0 - 1.0) seconds in ms
    assert kwargs["startup_total_ms"] == kwargs["context_hydration_ms"] + kwargs["payload_hydration_ms"]


@pytest.mark.asyncio
async def test_prepare_emits_payload_builder_timing_success(tmp_path: Path) -> None:
    """A successful build emits one ``response_payload.builder`` timing with success outcome."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread")

    with (
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="built")),
        ),
        patch("mindroom.response_payload_preparation.emit_elapsed_timing") as mock_emit,
    ):
        await bot._request_payload_preparer.prepare(
            _request(target, _preparation(target), thread_history=ThreadHistoryResult([], is_full_history=True)),
        )

    builder_calls = [
        call for call in mock_emit.call_args_list if call.args and call.args[0] == "response_payload.builder"
    ]
    assert len(builder_calls) == 1
    assert builder_calls[0].kwargs == {
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "outcome": "success",
    }


@pytest.mark.asyncio
async def test_prepare_emits_payload_builder_timing_on_failure(tmp_path: Path) -> None:
    """A failing build still emits one ``response_payload.builder`` timing with failed outcome."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread")

    with (
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("mindroom.response_payload_preparation.emit_elapsed_timing") as mock_emit,
        pytest.raises(RuntimeError, match="boom"),
    ):
        await bot._request_payload_preparer.prepare(
            _request(target, _preparation(target), thread_history=ThreadHistoryResult([], is_full_history=True)),
        )

    builder_calls = [
        call for call in mock_emit.call_args_list if call.args and call.args[0] == "response_payload.builder"
    ]
    assert len(builder_calls) == 1
    assert builder_calls[0].kwargs["outcome"] == "failed"


@pytest.mark.asyncio
async def test_prepare_registers_batch_media_when_media_events_present(tmp_path: Path) -> None:
    """Media events trigger batch-media registration before the payload build."""
    bot = _bot(tmp_path)
    target = _target()
    media_result = MagicMock(attachment_ids=["media-att"], fallback_images=None)
    register_mock = AsyncMock(return_value=media_result)
    build_mock = AsyncMock(return_value=DispatchPayload(prompt="built"))

    with (
        patch.object(bot._inbound_turn_normalizer, "register_batch_media_attachments", new=register_mock),
        patch.object(bot._inbound_turn_normalizer, "build_dispatch_payload_with_attachments", new=build_mock),
    ):
        await bot._request_payload_preparer.prepare(
            _request(
                target,
                _preparation(target, media_events=(MagicMock(),)),
                thread_history=ThreadHistoryResult([], is_full_history=True),
            ),
        )

    register_mock.assert_awaited_once()
    build_request = build_mock.await_args.args[0]
    assert "media-att" in build_request.current_attachment_ids


@pytest.mark.asyncio
async def test_generate_response_invokes_preparer_exactly_once_under_lock(tmp_path: Path) -> None:
    """The runner calls the preparer once, after acquiring the lifecycle lock."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    target = _target()
    prepare_calls: list[ResponseRequest] = []
    lock_held_at_prepare: list[bool] = []

    real_prepare = bot._request_payload_preparer.prepare

    async def spy_prepare(request: ResponseRequest) -> ResponseRequest:
        prepare_calls.append(request)
        lock_held_at_prepare.append(coordinator.has_active_response_for_target(target))
        return await real_prepare(request)

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = cast("Callable[[object | None], Awaitable[object]]", kwargs["response_function"])
        await response_function(None)
        return "$response"

    async def fake_process_and_respond(_request: ResponseRequest, **_kwargs: object) -> FinalDeliveryOutcome:
        return FinalDeliveryOutcome(
            terminal_status="completed",
            event_id="$response",
            is_visible_response=True,
            final_visible_body="ok",
            delivery_kind="sent",
        )

    with (
        patch.object(bot._request_payload_preparer, "prepare", new=AsyncMock(side_effect=spy_prepare)),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="built")),
        ),
        patch.object(coordinator, "run_cancellable_response", new=AsyncMock(side_effect=fake_run_cancellable_response)),
        patch.object(coordinator, "process_and_respond", new=AsyncMock(side_effect=fake_process_and_respond)),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
    ):
        result = await coordinator.generate_response(
            _request(target, _preparation(target), thread_history=[]),
        )

    assert result == "$response"
    assert len(prepare_calls) == 1
    assert lock_held_at_prepare == [True]
