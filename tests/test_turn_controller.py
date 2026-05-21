"""Targeted turn-controller regressions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import interactive
from mindroom.bot import AgentBot
from mindroom.coalescing import CoalescingGate
from mindroom.coalescing_batch import CoalescedBatch, PendingEvent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    HOOK_DISPATCH_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.users import AgentMatrixUser
from mindroom.streaming import send_streaming_response
from mindroom.turn_controller import _VISIBLE_ROUTER_VOICE_ECHO_KEY
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    install_generate_response_mock,
    replace_turn_controller_deps,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)
from tests.identity_helpers import persist_entity_accounts

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


def _make_general_bot(tmp_path: Path) -> AgentBot:
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", rooms=["!test:localhost"])}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    persist_entity_accounts(config, runtime_paths_for(config))
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
    return bot


def _prepared_text_event(
    *,
    event_id: str,
    body: str,
    sender: str = "@user:localhost",
    source_kind: str | None = None,
    content_overrides: dict[str, object] | None = None,
) -> PreparedTextEvent:
    content: dict[str, object] = {"body": body, "msgtype": "m.text"}
    if source_kind is not None:
        content[SOURCE_KIND_KEY] = source_kind
    if content_overrides is not None:
        content.update(content_overrides)
    return PreparedTextEvent(
        sender=sender,
        event_id=event_id,
        body=body,
        source={
            "content": content,
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:localhost",
        },
        server_timestamp=1000000,
        source_kind_override=source_kind,
    )


@pytest.mark.asyncio
async def test_active_thread_voice_stays_normal_and_coalesces(tmp_path: Path) -> None:
    """Voice follow-ups during active responses should remain in one coalesced batch."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    bot._turn_controller.deps.response_runner.has_active_response_for_target = MagicMock(return_value=True)

    voice_events = [
        _prepared_text_event(event_id="$voice-1", body="first transcription", source_kind=VOICE_SOURCE_KIND),
        _prepared_text_event(event_id="$voice-2", body="second transcription", source_kind=VOICE_SOURCE_KIND),
    ]
    for event in voice_events:
        target = bot._turn_controller.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id="$thread:localhost",
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        envelope = bot._turn_controller.deps.resolver.build_ingress_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id="@user:localhost",
            target=target,
            source_kind=VOICE_SOURCE_KIND,
        )
        await bot._turn_controller._enqueue_prepared_text_for_dispatch(
            room=room,
            prepared_event=event,
            dispatch_event=event,
            envelope=envelope,
            coalescing_thread_id="$thread:localhost",
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$voice-1", "$voice-2"]]
    assert [pending.dispatch_policy_source_kind for pending in batches[0].pending_events] == [None, None]
    assert ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND not in {
        pending.dispatch_policy_source_kind for pending in batches[0].pending_events
    }
    assert "first transcription" in batches[0].prompt
    assert "second transcription" in batches[0].prompt


@pytest.mark.asyncio
async def test_visible_router_voice_echo_is_display_only(tmp_path: Path) -> None:
    """Router voice echoes should be marked handled without dispatching to agents."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    echo_event = _prepared_text_event(
        event_id="$voice-echo",
        body="🎤 transcribed audio",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        content_overrides={
            ORIGINAL_SENDER_KEY: "@user:localhost",
            _VISIBLE_ROUTER_VOICE_ECHO_KEY: True,
        },
    )

    with patch(
        "mindroom.turn_controller.interactive.handle_text_response",
        new_callable=AsyncMock,
        return_value=None,
    ) as mock_interactive:
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=echo_event,
            dispatch_event=echo_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await gate.drain_all()

    mock_interactive.assert_not_awaited()
    assert batches == []
    assert bot._turn_store.is_handled("$voice-echo")


@pytest.mark.asyncio
async def test_voice_coalescing_real_trusted_router_handoff_still_dispatches(tmp_path: Path) -> None:
    """Trusted router handoffs without the voice echo marker should still dispatch."""
    bot = _make_general_bot(tmp_path)
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    replace_turn_controller_deps(bot, coalescing_gate=gate)
    handoff_event = _prepared_text_event(
        event_id="$router-handoff",
        body="@general could you help with this?",
        sender="@mindroom_router:localhost",
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        content_overrides={ORIGINAL_SENDER_KEY: "@user:localhost"},
    )

    with patch(
        "mindroom.turn_controller.interactive.handle_text_response",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await bot._turn_controller._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=handoff_event,
            dispatch_event=handoff_event,
            requester_user_id="@user:localhost",
            dispatch_timing=None,
        )

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$router-handoff"]]
    assert batches[0].source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
    assert not bot._turn_store.is_handled("$router-handoff")


@pytest.mark.asyncio
async def test_voice_coalescing_command_hook_and_scheduled_bypasses_remain() -> None:
    """Command, hook, and scheduled source kinds should keep their solo bypass behavior."""
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
    batches: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        batches.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    pending_events = [
        PendingEvent(
            event=_prepared_text_event(event_id="$command", body="!help", source_kind=MESSAGE_SOURCE_KIND),
            room=room,
            source_kind=MESSAGE_SOURCE_KIND,
        ),
        PendingEvent(
            event=_prepared_text_event(event_id="$hook", body="hook dispatch", source_kind=HOOK_DISPATCH_SOURCE_KIND),
            room=room,
            source_kind=HOOK_DISPATCH_SOURCE_KIND,
        ),
        PendingEvent(
            event=_prepared_text_event(event_id="$scheduled", body="scheduled", source_kind=SCHEDULED_SOURCE_KIND),
            room=room,
            source_kind=SCHEDULED_SOURCE_KIND,
        ),
    ]

    key = ("!test:localhost", "$thread:localhost", "@user:localhost")
    for pending_event in pending_events:
        await gate.enqueue(key, pending_event)

    await gate.drain_all()

    assert [batch.source_event_ids for batch in batches] == [["$command"], ["$hook"], ["$scheduled"]]
    assert all("quick succession" not in batch.prompt for batch in batches)


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
