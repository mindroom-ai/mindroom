"""Direct tests for the one-way ingress -> execution payload seam.

These exercise :class:`ResponsePayloadPreparer` (the execution-side, under-lock
payload-assembly step) on its own, plus the response runner's guarantee that the
step runs exactly once while the lifecycle lock is held.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.ai import _compose_current_turn_prompt
from mindroom.conversation_resolver import MessageContext
from mindroom.final_delivery import Completed, FinalDeliveryOutcome
from mindroom.hooks import EVENT_MESSAGE_ENRICH, HookRegistry, MessageEnrichContext, hook
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.reply_turn.enrichment import prepare_memory_and_model_context
from mindroom.reply_turn.locked_turn import LockedTurns
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_payload_preparation import (
    _STEER_CONTINUATION_LINE,
    DispatchPayloadInputs,
    ResponsePayloadPreparation,
    _steered_follow_up_prompt,
)
from mindroom.response_sources import Owned, ResponseSources
from mindroom.turn_policy import PreparedDispatch
from tests.bot_helpers import _hook_plugin
from tests.conftest import (
    unwrap_extracted_collaborator,
)
from tests.response_runner_helpers import _envelope, _target, attempt_in_place
from tests.test_queued_message_notify import _bot

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.dispatch_handoff import MediaDispatchEvent
    from mindroom.message_target import MessageTarget


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
    prompt_source_event_ids: tuple[str, ...] = (),
) -> ResponseRequest:
    return ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=("$event",),
            logical_source_event_ids=("$event",),
        ),
        thread_history=thread_history,
        prompt_source_event_ids=prompt_source_event_ids,
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


def _held(event_id: str, body: str, *, revision: str | None = None) -> ResolvedVisibleMessage:
    """Return one message as this responder's thread holds it, at ``revision`` when its own sender edited it."""
    return ResolvedVisibleMessage.from_message_data(
        {"sender": "@user:localhost", "body": body, "timestamp": 1, "event_id": event_id, "content": {"body": body}},
        thread_id="$thread",
        latest_event_id=revision or event_id,
    )


@pytest.mark.parametrize(
    ("held", "routed", "answered"),
    [
        # The sender corrected the message while the steward selected: the route was written from the old text.
        pytest.param([_held("$first", "the corrected text", revision="$edit")], ("$first",), "the corrected text"),
        # No edit of its sender's: the route's prompt stands. A replacement someone else sent reads the same, as the
        # thread holds the message as its sender wrote it (``test_a_replacement_from_another_sender_is_not_an_edit``).
        pytest.param([_held("$first", "the first text")], ("$first",), "raw prompt"),
        # A batch's prompt is its steward's, whatever its messages became since.
        pytest.param(
            [_held("$first", "the corrected text", revision="$edit"), _held("$second", "more")],
            ("$first", "$second"),
            "raw prompt",
        ),
        # Another message's edit is not the routed one's.
        pytest.param([_held("$other", "an edited neighbour", revision="$edit")], ("$first",), "raw prompt"),
        # An unrouted turn's prompt is its own.
        pytest.param([_held("$event", "the corrected text", revision="$edit")], (), "raw prompt"),
    ],
    ids=["edited", "unedited-or-replaced-by-another", "batch", "neighbour-edited", "unrouted"],
)
@pytest.mark.asyncio
async def test_a_lone_routed_message_is_answered_as_its_sender_last_edited_it(
    tmp_path: Path,
    held: list[ResolvedVisibleMessage],
    routed: tuple[str, ...],
    answered: str,
) -> None:
    """A route names one message its sender edited since: the responder answers the text its own thread holds."""
    bot = _bot(tmp_path)
    target = _target(thread_id="$thread")
    build = AsyncMock(return_value=DispatchPayload(prompt="built"))

    with patch.object(bot._inbound_turn_normalizer, "build_dispatch_payload_with_attachments", new=build):
        await bot._request_payload_preparer.prepare(
            _request(target, _preparation(target), thread_history=held, prompt_source_event_ids=routed),
        )

    assert build.await_args.args[0].prompt == answered


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

    delivered = FinalDeliveryOutcome(
        ending=Completed(),
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
        patch.object(LockedTurns, "_run_cancellable_response", new=attempt_in_place()),
        patch.object(coordinator, "_deliver_turn", new=AsyncMock(return_value=delivered)),
        patch("mindroom.response_runner.should_use_streaming", AsyncMock(return_value=False)),
    ):
        result = await coordinator.generate_response(
            _request(target, _preparation(target), thread_history=[]),
        )

    # The delivered answer's reply holds the source.
    assert result == Owned()
    assert len(prepare_calls) == 1
    assert lock_held_at_prepare == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("model_prompt", [None, "raw prompt\n\nThe attachment is a photo."], ids=["text", "attachment"])
@pytest.mark.parametrize("stopped_reply", [True, False], ids=["marked", "unmarked"])
async def test_carry_on_line_follows_only_marked_model_prompt_after_enrichment(
    tmp_path: Path,
    model_prompt: str | None,
    *,
    stopped_reply: bool,
) -> None:
    """A message that cut a reply short tells the model once, after its enrichment, only in the model's copy.

    The person's raw prompt, the envelope body, and the memory input stay as the person wrote them; an unmarked
    message (Queue, a Steer sent while nothing ran, an Auto message without a verdict) carries no line.
    """
    bot = _bot(tmp_path)

    @hook(EVENT_MESSAGE_ENRICH)
    async def add_note(ctx: MessageEnrichContext) -> None:
        ctx.add_metadata("note", "Enriched note.")

    bot.orchestrator.hook_registry_state.registry = HookRegistry.from_plugins([_hook_plugin("note", [add_note])])
    target = _target()
    preparation = _preparation(target)
    preparation = replace(preparation, dispatch=replace(preparation.dispatch, stopped_reply=stopped_reply))
    request = _request(target, preparation, thread_history=ThreadHistoryResult([], is_full_history=True))
    with patch.object(
        bot._inbound_turn_normalizer,
        "build_dispatch_payload_with_attachments",
        new=AsyncMock(return_value=DispatchPayload(prompt="raw prompt", model_prompt=model_prompt)),
    ):
        result = await bot._request_payload_preparer.prepare(request)

    assert result.model_prompt is not None
    assert result.prompt == preparation.prompt == "raw prompt"
    assert result.response_envelope.body == preparation.dispatch.envelope.body
    assert "Enriched note." in result.model_prompt
    assert ("The attachment is a photo." in result.model_prompt) is (model_prompt is not None)
    assert result.model_prompt.count(_STEER_CONTINUATION_LINE) == (1 if stopped_reply else 0)
    memory_prompt, _history, model_prompt_text, _model_history = prepare_memory_and_model_context(
        result.prompt,
        [],
        config=bot.config,
        runtime_paths=bot.runtime_paths,
        timezone="UTC",
        model_prompt=result.model_prompt,
    )
    assert memory_prompt == "raw prompt"
    # The model reads the person's message once, then the line after its enrichment.
    current = _compose_current_turn_prompt(raw_prompt=memory_prompt, model_prompt=model_prompt_text)
    assert current.count("raw prompt") == 1
    assert current.startswith("raw prompt\n\n")
    assert current.endswith(_STEER_CONTINUATION_LINE) is stopped_reply
    assert _steered_follow_up_prompt("Do X") == f"Do X\n\n{_STEER_CONTINUATION_LINE}"
