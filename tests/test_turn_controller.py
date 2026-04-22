"""Targeted turn-controller regressions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import interactive
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.matrix.users import AgentMatrixUser
from mindroom.streaming import send_streaming_response
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
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.message_target import MessageTarget


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

    bot._conversation_resolver.fetch_thread_history = AsyncMock(return_value=[])
    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$ack:localhost")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
    )

    captured_target = None

    async def generate_response(
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: list[object],
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        user_id: str | None = None,  # noqa: ARG001
        media: object | None = None,  # noqa: ARG001
        attachment_ids: list[str] | None = None,  # noqa: ARG001
        model_prompt: str | None = None,  # noqa: ARG001
        system_enrichment_items: tuple[object, ...] = (),  # noqa: ARG001
        response_envelope: object | None = None,  # noqa: ARG001
        correlation_id: str | None = None,  # noqa: ARG001
        target: MessageTarget | None = None,
        matrix_run_metadata: dict[str, object] | None = None,  # noqa: ARG001
    ) -> str | None:
        nonlocal captured_target
        captured_target = target
        assert room_id == room.room_id
        assert prompt == "The user selected: Option 1"
        assert reply_to_event_id == selection.question_event_id
        assert thread_id == selection.thread_id
        assert thread_history == []
        assert existing_event_id == "$ack:localhost"
        assert existing_event_is_placeholder is True
        assert target is not None
        assert target.reply_to_event_id == selection.question_event_id

        async def response_stream() -> AsyncIterator[str]:
            yield "Processed selection"

        with patch(
            "mindroom.streaming.edit_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$edit:localhost")),
        ) as mock_edit:
            outcome = await send_streaming_response(
                client=bot.client,
                room_id=room_id,
                reply_to_event_id=reply_to_event_id,
                thread_id=thread_id,
                sender_domain="localhost",
                config=config,
                runtime_paths=runtime_paths_for(config),
                response_stream=response_stream(),
                existing_event_id=existing_event_id,
                adopt_existing_placeholder=existing_event_is_placeholder,
                target=target,
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
    )

    bot._delivery_gateway.send_text.assert_awaited_once()
    ack_request = bot._delivery_gateway.send_text.await_args.args[0]
    assert ack_request.target.resolved_thread_id == selection.thread_id
    assert ack_request.target.reply_to_event_id is None
    generate_response_mock.assert_awaited_once()
    assert captured_target is not None
    assert captured_target.resolved_thread_id == selection.thread_id


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

    mock_handle_text_response.assert_awaited_once()
    assert mock_handle_text_response.await_args.kwargs["resolved_thread_id"] == "$thread-root:localhost"
    mock_dispatch_text.assert_awaited_once()
