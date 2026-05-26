"""Targeted turn-controller regressions."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import interactive
from mindroom.bot import AgentBot
from mindroom.coalescing import CoalescingGate, IngressAdmissionClosedError, IngressOrderReservation, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescingKey, PendingEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY
from mindroom.dispatch_handoff import PendingDispatchMetadata, PreparedTextEvent
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from mindroom.streaming import send_streaming_response
from mindroom.turn_controller import _PromptIngressReservationOwner
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    install_generate_response_mock,
    replace_turn_controller_deps,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.hooks import MessageEnvelope


async def _wait_for(condition: Callable[[], bool], *, deadline_seconds: float = 0.5) -> None:
    """Poll until a test condition becomes true."""
    ready = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _mark_ready() -> None:
        if condition():
            ready.set()
            return
        loop.call_later(0.001, _mark_ready)

    _mark_ready()
    try:
        async with asyncio.timeout(deadline_seconds):
            await ready.wait()
    except TimeoutError as exc:
        msg = "Timed out waiting for async test condition"
        raise AssertionError(msg) from exc


def _text_event(event_id: str, body: str, origin_server_ts: int) -> nio.RoomMessageText:
    """Build one plain Matrix text event."""
    return nio.RoomMessageText.from_dict(
        {
            "content": {"body": body, "msgtype": "m.text"},
            "event_id": event_id,
            "sender": "@user:localhost",
            "origin_server_ts": origin_server_ts,
            "room_id": "!room:localhost",
            "type": "m.room.message",
        },
    )


def _pending(event: nio.RoomMessageText) -> PendingEvent:
    """Wrap one Matrix event as pending user ingress."""
    return PendingEvent(
        event=event,
        room=nio.MatrixRoom("!room:localhost", "@mindroom:localhost"),
        source_kind=MESSAGE_SOURCE_KIND,
    )


@pytest.mark.asyncio
async def test_late_admit_rejection_closes_completed_ready_task_metadata_once() -> None:
    """Owner cleanup should close completed ready-task metadata once after late admit rejection."""
    close_count = 0

    def close_metadata() -> None:
        nonlocal close_count
        close_count += 1

    pending_event = _pending(_text_event("$late:localhost", "late", 1000))
    pending_event.dispatch_metadata = (
        PendingDispatchMetadata(
            kind="test",
            payload=object(),
            close=close_metadata,
            requires_solo_batch=False,
        ),
    )

    async def ready() -> ReadyPendingEvent:
        return ReadyPendingEvent(pending_event=pending_event)

    class RejectingGate:
        async def admit(self, *_args: object, **_kwargs: object) -> None:
            msg = "closed"
            raise IngressAdmissionClosedError(msg)

        def release_order_reservation(self, reservation: IngressOrderReservation) -> None:
            reservation.released = True
            reservation.settled.set()

    reservation = IngressOrderReservation(
        room_id="!room:localhost",
        requester_user_id="@user:localhost",
        received_order=1,
        receipt_time=1.0,
    )
    owner = _PromptIngressReservationOwner(gate=RejectingGate(), reservation=reservation)
    ready_task = asyncio.create_task(ready())
    await ready_task

    with pytest.raises(IngressAdmissionClosedError):
        await owner.admit(
            CoalescingKey("!room:localhost", "$thread:localhost", "@user:localhost"),
            ready_task=ready_task,
            source_event_id="$late:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        )

    await owner.release()

    assert close_count == 1


@pytest.mark.asyncio
async def test_owner_cancel_ready_task_closes_ready_result_returned_during_cancellation() -> None:
    """Owner cancellation should close metadata even when a task returns a ready result while cancelling."""
    close_count = 0
    cancelled = asyncio.Event()

    def close_metadata() -> None:
        nonlocal close_count
        close_count += 1

    pending_event = _pending(_text_event("$late:localhost", "late", 1000))
    pending_event.dispatch_metadata = (
        PendingDispatchMetadata(
            kind="test",
            payload=object(),
            close=close_metadata,
            requires_solo_batch=False,
        ),
    )

    async def ready() -> ReadyPendingEvent:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            return ReadyPendingEvent(pending_event=pending_event)

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    reservation = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")
    owner = _PromptIngressReservationOwner(gate=gate, reservation=reservation)
    owner.ready_task = asyncio.create_task(ready())
    await asyncio.sleep(0)

    await owner.cancel_ready_task()

    assert cancelled.is_set()
    assert close_count == 1
    await owner.cancel_ready_task()
    assert close_count == 1


@pytest.mark.asyncio
async def test_owner_release_settles_reservation_when_cancelled_during_ready_task_cleanup() -> None:
    """Owner release must not orphan its ready task when callback cancellation interrupts cleanup."""

    async def never_ready() -> ReadyPendingEvent | None:
        await asyncio.Event().wait()
        return None

    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    reservation = gate.reserve_order(room_id="!room:localhost", requester_user_id="@user:localhost")
    owner = _PromptIngressReservationOwner(gate=gate, reservation=reservation)
    ready_task = asyncio.create_task(never_ready())
    owner.ready_task = ready_task

    try:
        release_task = asyncio.create_task(owner.release())
        await asyncio.sleep(0)
        release_task.cancel()
        with suppress(asyncio.CancelledError):
            await release_task

        assert reservation.released
        assert reservation.settled.is_set()
        assert ready_task.done()
        assert ready_task.cancelled()
        assert owner.ready_task is None
    finally:
        if not ready_task.done():
            ready_task.cancel()
        await asyncio.gather(ready_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_handle_interactive_selection_threaded_streaming_keeps_reply_target(
    tmp_path: Path,
) -> None:
    """Threaded interactive selections should stream edits without thread-fallback assertions."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()

    room = MagicMock()
    room.room_id = "!test:localhost"
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        selection_key="1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )

    bot._conversation_resolver.fetch_thread_history = AsyncMock(
        return_value=thread_history_result([], is_full_history=True),
    )
    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$ack:localhost")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
    )

    captured_envelope: MessageEnvelope | None = None
    captured_metadata: dict[str, object] | None = None

    async def generate_response(
        prompt: str,
        thread_history: list[object],
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        user_id: str | None = None,  # noqa: ARG001
        media: object | None = None,  # noqa: ARG001
        attachment_ids: list[str] | None = None,  # noqa: ARG001
        model_prompt: str | None = None,  # noqa: ARG001
        system_enrichment_items: tuple[object, ...] = (),  # noqa: ARG001
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,  # noqa: ARG001
        matrix_run_metadata: dict[str, object] | None = None,
    ) -> str | None:
        nonlocal captured_envelope, captured_metadata
        assert response_envelope is not None
        captured_envelope = response_envelope
        captured_metadata = matrix_run_metadata
        assert prompt == "The user selected: Option 1"
        assert response_envelope.target.room_id == room.room_id
        assert response_envelope.target.reply_to_event_id == selection.question_event_id
        assert response_envelope.target.resolved_thread_id == selection.thread_id
        assert thread_history == []
        assert existing_event_id == "$ack:localhost"
        assert existing_event_is_placeholder is True

        async def response_stream() -> AsyncIterator[str]:
            yield "Processed selection"

        with patch(
            "mindroom.streaming.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit:localhost")),
        ) as mock_edit:
            outcome = await send_streaming_response(
                client=bot.client,
                target=response_envelope.target,
                config=config,
                runtime_paths=runtime_paths_for(config),
                response_stream=response_stream(),
                existing_event_id=existing_event_id,
                adopt_existing_placeholder=existing_event_is_placeholder,
            )

        mock_edit.assert_awaited()
        assert outcome.rendered_body == "Processed selection"
        return outcome.last_physical_stream_event_id

    generate_response_mock = AsyncMock(side_effect=generate_response)
    install_generate_response_mock(bot, generate_response_mock)

    await bot._turn_controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id="@user:localhost",
        source_event_id="$selection:localhost",
    )

    bot._delivery_gateway.send_text.assert_awaited_once()
    ack_request = bot._delivery_gateway.send_text.await_args.args[0]
    assert ack_request.target.resolved_thread_id == selection.thread_id
    assert ack_request.target.reply_to_event_id is None
    generate_response_mock.assert_awaited_once()
    assert captured_envelope is not None
    assert captured_envelope.source_event_id == "$selection:localhost"
    assert captured_envelope.target.resolved_thread_id == selection.thread_id
    assert captured_metadata is not None
    assert captured_metadata[MATRIX_SOURCE_EVENT_IDS_METADATA_KEY] == ["$selection:localhost"]


@pytest.mark.asyncio
async def test_handle_interactive_selection_does_not_mark_handled_when_runner_returns_none(
    tmp_path: Path,
) -> None:
    """A retryable terminal outcome must not mark the source turn handled."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()

    room = MagicMock()
    room.room_id = "!test:localhost"
    selection = interactive.InteractiveSelection(
        question_event_id="$question:localhost",
        selection_key="1",
        selected_value="Option 1",
        thread_id="$thread-root:localhost",
    )

    bot._conversation_resolver.fetch_thread_history = AsyncMock(
        return_value=thread_history_result([], is_full_history=True),
    )
    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$ack:localhost")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
    )
    bot._turn_controller.deps.turn_store.record_turn = MagicMock()
    generate_response_mock = AsyncMock(return_value=None)
    install_generate_response_mock(bot, generate_response_mock)

    await bot._turn_controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id="@user:localhost",
        source_event_id="$selection:localhost",
    )

    generate_response_mock.assert_awaited_once()
    bot._turn_controller.deps.turn_store.record_turn.assert_not_called()


@pytest.mark.asyncio
async def test_on_message_passes_resolved_thread_id_to_interactive_text_response(
    tmp_path: Path,
) -> None:
    """Plain numeric replies should use the canonical coalescing thread id for interactive matching."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()

    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    message_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "1",
                "msgtype": "m.text",
                "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-reply:localhost"}},
            },
            "event_id": "$selection:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
    )
    message_event.source = {
        "content": {
            "body": "1",
            "msgtype": "m.text",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$plain-reply:localhost"}},
        },
        "event_id": "$selection:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000000,
        "type": "m.room.message",
        "room_id": "!test:localhost",
    }

    wrap_extracted_collaborators(bot, "_delivery_gateway", "_turn_policy")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
        turn_policy=bot._turn_policy,
    )

    with (
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch.object(bot._turn_policy, "can_reply_to_sender", return_value=True),
        patch.object(
            bot._conversation_resolver,
            "coalescing_thread_id",
            new_callable=AsyncMock,
            return_value="$thread-root:localhost",
        ),
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_handle_text_response,
        patch.object(bot._turn_controller, "_dispatch_text_message", new_callable=AsyncMock) as mock_dispatch_text,
    ):
        await bot._on_message(room, message_event)
        await _wait_for(lambda: mock_dispatch_text.await_count == 1)

    mock_handle_text_response.assert_awaited_once()
    assert mock_handle_text_response.await_args.kwargs["resolved_thread_id"] == "$thread-root:localhost"
    mock_dispatch_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_sidecar_preview_passes_resolved_thread_id_to_interactive_text_response(
    tmp_path: Path,
) -> None:
    """Sidecar previews should use the same interactive matching thread id as text messages."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General")}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="test_password",  # noqa: S106
    )
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()

    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    sidecar_event = nio.RoomMessageFile.from_dict(
        {
            "content": {
                "body": "1 [Message continues in attached file]",
                "msgtype": "m.file",
                "info": {"mimetype": "application/json"},
                "io.mindroom.long_text": {
                    "version": 2,
                    "encoding": "matrix_event_content_json",
                },
                "url": "mxc://server/sidecar-selection",
            },
            "event_id": "$sidecar-selection:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
    )
    sidecar_event.source = {
        "content": {
            "body": "1 [Message continues in attached file]",
            "msgtype": "m.file",
            "info": {"mimetype": "application/json"},
            "io.mindroom.long_text": {
                "version": 2,
                "encoding": "matrix_event_content_json",
            },
            "url": "mxc://server/sidecar-selection",
        },
        "event_id": "$sidecar-selection:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000000,
        "type": "m.room.message",
        "room_id": "!test:localhost",
    }
    prepared_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$sidecar-selection:localhost",
        body="1",
        source={
            "content": {
                "body": "1",
                "msgtype": "m.text",
            },
            "event_id": "$sidecar-selection:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
        server_timestamp=1000000,
    )

    wrap_extracted_collaborators(bot, "_turn_policy", "_inbound_turn_normalizer")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        turn_policy=bot._turn_policy,
        normalizer=bot._inbound_turn_normalizer,
    )

    with (
        patch("mindroom.turn_controller.is_authorized_sender", return_value=True),
        patch.object(bot._turn_policy, "can_reply_to_sender", return_value=True),
        patch.object(
            bot._conversation_resolver,
            "coalescing_thread_id",
            new_callable=AsyncMock,
            return_value="$thread-root:localhost",
        ),
        patch.object(
            bot._inbound_turn_normalizer,
            "prepare_file_sidecar_text_event",
            new_callable=AsyncMock,
            return_value=prepared_event,
        ),
        patch(
            "mindroom.turn_controller.interactive.handle_text_response",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_handle_text_response,
        patch.object(
            bot._turn_controller,
            "_enqueue_prepared_text_for_dispatch",
            new_callable=AsyncMock,
        ) as mock_enqueue,
    ):
        await bot._turn_controller._handle_media_message_inner(room, sidecar_event)

    mock_handle_text_response.assert_awaited_once()
    assert mock_handle_text_response.await_args.kwargs["resolved_thread_id"] == "$thread-root:localhost"
    mock_enqueue.assert_awaited_once()
    assert mock_enqueue.await_args.kwargs["prepared_event"] is prepared_event
    assert mock_enqueue.await_args.kwargs["dispatch_event"] is prepared_event
