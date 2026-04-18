"""Targeted turn-controller regressions."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import interactive
from mindroom.bot import AgentBot
from mindroom.commands.parsing import command_parser
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.conversation_resolver import MessageContext
from mindroom.handled_turns import HandledTurnState
from mindroom.inbound_turn_normalizer import DispatchPayload
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.users import AgentMatrixUser
from mindroom.streaming import send_streaming_response
from mindroom.teams import TeamMode
from mindroom.tool_system.skills import _SkillCommandDispatch, _SkillCommandSpec
from mindroom.turn_policy import PreparedDispatch, ResponseAction
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
    source_event_id = "$selection:localhost"

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
        strip_transient_enrichment_after_run: bool = False,  # noqa: ARG001
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
            event_id, accumulated = await send_streaming_response(
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
        assert accumulated == "Processed selection"
        return event_id

    generate_response_mock = AsyncMock(side_effect=generate_response)
    install_generate_response_mock(bot, generate_response_mock)

    await bot._turn_controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id="@user:localhost",
        source_event_id=source_event_id,
    )

    bot._delivery_gateway.send_text.assert_awaited_once()
    ack_request = bot._delivery_gateway.send_text.await_args.args[0]
    assert ack_request.target.resolved_thread_id == selection.thread_id
    assert ack_request.target.reply_to_event_id is None
    assert ack_request.transaction_id is not None
    generate_response_mock.assert_awaited_once()
    assert captured_target is not None
    assert captured_target.resolved_thread_id == selection.thread_id
    question_turn = bot._turn_store.get_turn_record(selection.question_event_id)
    source_turn = bot._turn_store.get_turn_record(source_event_id)
    assert question_turn is not None
    assert source_turn is not None
    assert question_turn.response_transaction_id == ack_request.transaction_id
    assert source_turn.response_transaction_id == ack_request.transaction_id


@pytest.mark.asyncio
async def test_execute_router_relay_reserves_transaction_id_for_first_visible_reply(tmp_path: Path) -> None:
    """Router relay sends should reserve one stable tx-id before the first visible reply."""
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", rooms=["!test:localhost"])},
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"

    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password="test_password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.agent_user.user_id)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": "help me route this", "msgtype": "m.text"},
            "event_id": "$router-source:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "room_id": room.room_id,
        },
    )
    event.source = {
        "content": {"body": "help me route this", "msgtype": "m.text"},
        "event_id": "$router-source:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "room_id": room.room_id,
    }

    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$relay:localhost")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
        normalizer=bot._inbound_turn_normalizer,
    )

    with patch("mindroom.turn_controller.suggest_agent_for_message", new=AsyncMock(return_value="general")):
        await bot._turn_controller._execute_router_relay(
            room,
            event,
            thread_history=[],
            requester_user_id="@user:localhost",
        )

    bot._delivery_gateway.send_text.assert_awaited_once()
    request = bot._delivery_gateway.send_text.await_args.args[0]
    assert request.transaction_id is not None
    turn_record = bot._turn_store.get_turn_record(event.event_id)
    assert turn_record is not None
    assert turn_record.response_transaction_id == request.transaction_id


@pytest.mark.asyncio
async def test_execute_command_standard_reply_reserves_transaction_id(tmp_path: Path) -> None:
    """Command replies should reserve a stable tx-id before the first visible send."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", rooms=["!test:localhost"])}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General",
            password="test_password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.agent_user.user_id)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": "!help", "msgtype": "m.text"},
            "event_id": "$command:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "room_id": room.room_id,
        },
    )
    event.source = {
        "content": {"body": "!help", "msgtype": "m.text"},
        "event_id": "$command:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "room_id": room.room_id,
    }

    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$reply:localhost")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
        normalizer=bot._inbound_turn_normalizer,
    )

    async def fake_handle_command(*, context: object, room: object, event: object, **_kwargs: object) -> None:
        await context.send_response(
            room.room_id,
            event.event_id,
            "Command reply",
            None,
            reply_to_event=event,
            skip_mentions=True,
        )

    with patch("mindroom.turn_controller.handle_command", new=AsyncMock(side_effect=fake_handle_command)):
        await bot._turn_controller._execute_command(
            room,
            event,
            requester_user_id="@user:localhost",
            command=MagicMock(),
        )

    bot._delivery_gateway.send_text.assert_awaited_once()
    request = bot._delivery_gateway.send_text.await_args.args[0]
    assert request.transaction_id is not None


@pytest.mark.asyncio
async def test_execute_command_skill_reply_reserves_transaction_id(tmp_path: Path) -> None:
    """Skill-command replies should reserve a tx-id before delegating to the response runner."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", rooms=["!test:localhost"])}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General",
            password="test_password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.agent_user.user_id)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": "!skill demo", "msgtype": "m.text"},
            "event_id": "$skill:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "room_id": room.room_id,
        },
    )
    event.source = {
        "content": {"body": "!skill demo", "msgtype": "m.text"},
        "event_id": "$skill:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "room_id": room.room_id,
    }

    wrap_extracted_collaborators(bot, "_response_runner", "_delivery_gateway")
    bot._response_runner.send_skill_command_response = AsyncMock(return_value="$skill-reply:localhost")
    bot._delivery_gateway.send_text = AsyncMock()
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        response_runner=bot._response_runner,
        delivery_gateway=bot._delivery_gateway,
        normalizer=bot._inbound_turn_normalizer,
    )

    async def fake_handle_command(*, context: object, room: object, event: object, **_kwargs: object) -> None:
        await context.send_skill_command_response(
            room_id=room.room_id,
            reply_to_event_id=event.event_id,
            thread_id=None,
            thread_history=(),
            prompt="Run skill",
            agent_name="general",
            user_id=event.sender,
            reply_to_event=event,
        )

    with patch("mindroom.turn_controller.handle_command", new=AsyncMock(side_effect=fake_handle_command)):
        await bot._turn_controller._execute_command(
            room,
            event,
            requester_user_id="@user:localhost",
            command=MagicMock(),
        )

    assert bot._response_runner.send_skill_command_response.await_args is not None
    assert bot._response_runner.send_skill_command_response.await_args.kwargs["response_transaction_id"] is not None


@pytest.mark.asyncio
async def test_schedule_command_marks_startup_owned_turn_handled_before_side_effects(
    tmp_path: Path,
) -> None:
    """Mutating schedule commands should become non-replayable before the scheduler state changes."""
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", rooms=["!test:localhost"])},
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password="test_password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.agent_user.user_id)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": "!schedule tomorrow remind me", "msgtype": "m.text"},
            "event_id": "$schedule:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "room_id": room.room_id,
        },
    )
    event.source = {
        "content": {"body": "!schedule tomorrow remind me", "msgtype": "m.text"},
        "event_id": "$schedule:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "room_id": room.room_id,
    }
    command = command_parser.parse(event.body)
    assert command is not None

    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(side_effect=RuntimeError("crash after schedule mutation"))
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
        normalizer=bot._inbound_turn_normalizer,
    )
    assert bot._turn_store.claim_pending_inbound(room_id=room.room_id, event_source=event.source) is True

    async def fake_schedule_task(**_kwargs: object) -> tuple[str | None, str]:
        assert bot._turn_store.is_handled(event.event_id) is True
        assert bot._turn_store.has_pending_inbound(event.event_id) is False
        return ("task-1", "Scheduled")

    with (
        patch("mindroom.commands.handler.schedule_task", new=AsyncMock(side_effect=fake_schedule_task)),
        pytest.raises(RuntimeError, match="crash after schedule mutation"),
    ):
        await bot._turn_controller._execute_command(
            room,
            event,
            requester_user_id="@user:localhost",
            command=command,
        )

    assert bot._turn_store.has_pending_inbound(event.event_id) is False
    assert bot._turn_store.get_turn_record(event.event_id) is not None
    assert bot._turn_store.pending_inbound_replays() == []
    assert await bot.replay_pending_inbound_turns() == set()


@pytest.mark.asyncio
async def test_skill_tool_command_marks_startup_owned_turn_handled_before_tool_side_effects(
    tmp_path: Path,
) -> None:
    """Tool-dispatched skill commands should become non-replayable before the tool runs."""
    config = bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", rooms=["!test:localhost"], skills=["demo"])},
            router=RouterConfig(model="default"),
        ),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password="test_password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.agent_user.user_id)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": "!skill demo do it", "msgtype": "m.text"},
            "event_id": "$skill-tool:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "room_id": room.room_id,
        },
    )
    event.source = {
        "content": {"body": "!skill demo do it", "msgtype": "m.text"},
        "event_id": "$skill-tool:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "room_id": room.room_id,
    }
    command = command_parser.parse(event.body)
    assert command is not None

    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(side_effect=RuntimeError("crash after tool side effect"))
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
        normalizer=bot._inbound_turn_normalizer,
    )
    assert bot._turn_store.claim_pending_inbound(room_id=room.room_id, event_source=event.source) is True

    async def fake_run_skill_command_tool(**_kwargs: object) -> str:
        assert bot._turn_store.is_handled(event.event_id) is True
        assert bot._turn_store.has_pending_inbound(event.event_id) is False
        return "Tool finished"

    spec = _SkillCommandSpec(
        name="demo",
        description="Demo skill",
        source_path=tmp_path / "skills" / "demo" / "SKILL.md",
        user_invocable=True,
        disable_model_invocation=False,
        dispatch=_SkillCommandDispatch(tool_name="demo_tool"),
    )

    with (
        patch("mindroom.commands.handler._resolve_skill_command_agent", return_value=("general", None)),
        patch("mindroom.commands.handler.resolve_skill_command_spec", return_value=spec),
        patch(
            "mindroom.turn_controller._run_skill_command_tool",
            new=AsyncMock(side_effect=fake_run_skill_command_tool),
        ),
        pytest.raises(RuntimeError, match="crash after tool side effect"),
    ):
        await bot._turn_controller._execute_command(
            room,
            event,
            requester_user_id="@user:localhost",
            command=command,
        )

    assert bot._turn_store.has_pending_inbound(event.event_id) is False
    assert bot._turn_store.get_turn_record(event.event_id) is not None
    assert bot._turn_store.pending_inbound_replays() == []
    assert await bot.replay_pending_inbound_turns() == set()


@pytest.mark.asyncio
async def test_config_command_keeps_startup_owned_turn_replayable_until_confirmation_state_is_durable(
    tmp_path: Path,
) -> None:
    """Startup-owned config previews must stay replayable until their confirmation state is durably stored."""
    config = bind_runtime_paths(
        Config(router=RouterConfig(model="default")),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router",
            password="test_password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.agent_user.user_id)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": "!config set defaults.markdown false", "msgtype": "m.text"},
            "event_id": "$config-preview:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "room_id": room.room_id,
        },
    )
    event.source = {
        "content": {"body": "!config set defaults.markdown false", "msgtype": "m.text"},
        "event_id": "$config-preview:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "room_id": room.room_id,
    }
    command = command_parser.parse(event.body)
    assert command is not None

    wrap_extracted_collaborators(bot, "_delivery_gateway")
    bot._delivery_gateway.send_text = AsyncMock(return_value="$preview")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        delivery_gateway=bot._delivery_gateway,
        normalizer=bot._inbound_turn_normalizer,
    )
    assert bot._turn_store.claim_pending_inbound(room_id=room.room_id, event_source=event.source) is True

    change_info = {
        "config_path": "defaults.markdown",
        "old_value": True,
        "new_value": False,
    }

    with (
        patch(
            "mindroom.commands.handler.handle_config_command",
            new=AsyncMock(return_value=("preview", change_info)),
        ),
        patch("mindroom.commands.handler.config_confirmation.register_pending_change"),
        patch(
            "mindroom.commands.handler.config_confirmation.get_pending_change",
            return_value=MagicMock(),
        ),
        patch(
            "mindroom.commands.handler.config_confirmation.store_pending_change_in_matrix",
            new=AsyncMock(return_value=False),
        ),
        patch("mindroom.commands.handler.config_confirmation.add_confirmation_reactions", new=AsyncMock()),
    ):
        await bot._turn_controller._execute_command(
            room,
            event,
            requester_user_id="@user:localhost",
            command=command,
        )

    turn_record = bot._turn_store.get_turn_record(event.event_id)
    assert turn_record is not None
    assert turn_record.response_event_id == "$preview"
    assert turn_record.completed is False
    assert bot._turn_store.has_pending_inbound(event.event_id) is True
    assert bot._turn_store.is_handled(event.event_id) is False


@pytest.mark.asyncio
async def test_execute_response_action_records_pending_visible_response_event_before_completion(
    tmp_path: Path,
) -> None:
    """The first visible AI reply should be durably anchored before the turn reaches a terminal outcome."""
    config = bind_runtime_paths(
        Config(agents={"general": AgentConfig(display_name="General", rooms=["!test:localhost"])}),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General",
            password="test_password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    bot.client = AsyncMock()
    room = nio.MatrixRoom(room_id="!test:localhost", own_user_id=bot.agent_user.user_id)
    event = nio.RoomMessageText.from_dict(
        {
            "content": {"body": "hello", "msgtype": "m.text"},
            "event_id": "$visible-anchor-source:localhost",
            "sender": "@user:localhost",
            "origin_server_ts": 1000,
            "type": "m.room.message",
            "room_id": room.room_id,
        },
    )
    event.source = {
        "content": {"body": "hello", "msgtype": "m.text"},
        "event_id": "$visible-anchor-source:localhost",
        "sender": "@user:localhost",
        "origin_server_ts": 1000,
        "type": "m.room.message",
        "room_id": room.room_id,
    }

    wrap_extracted_collaborators(bot, "_response_runner")
    replace_turn_controller_deps(
        bot,
        resolver=bot._conversation_resolver,
        response_runner=bot._response_runner,
        delivery_gateway=bot._delivery_gateway,
        normalizer=bot._inbound_turn_normalizer,
        turn_store=bot._turn_store,
    )

    async def fake_generate_response(request: object) -> str | None:
        prepared_request = await request.prepare_after_lock(request)
        assert prepared_request.record_visible_response_event_id is not None
        prepared_request.record_visible_response_event_id("$visible-anchor:localhost")
        return None

    bot._response_runner.generate_response = AsyncMock(side_effect=fake_generate_response)
    replace_turn_controller_deps(bot, response_runner=bot._response_runner)

    dispatch = PreparedDispatch(
        requester_user_id="@user:localhost",
        context=MessageContext(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        ),
        target=bot._conversation_resolver.build_message_target(
            room_id=room.room_id,
            thread_id=None,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        ),
        correlation_id="corr-visible-anchor",
        envelope=bot._conversation_resolver.build_ingress_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id="@user:localhost",
        ),
    )

    async def payload_builder(_context: MessageContext) -> DispatchPayload:
        return DispatchPayload(prompt=event.body)

    await bot._turn_controller._execute_response_action(
        room,
        event,
        dispatch,
        ResponseAction(kind="individual"),
        payload_builder,
        processing_log="Processing",
        dispatch_started_at=0.0,
        handled_turn=HandledTurnState.from_source_event_id(event.event_id),
    )

    turn_record = bot._turn_store.get_turn_record(event.event_id)
    assert turn_record is not None
    assert turn_record.response_event_id == "$visible-anchor:localhost"
    assert turn_record.completed is False


@pytest.mark.asyncio
async def test_resolve_response_action_does_not_auto_form_team_from_stale_mentions_in_multi_human_thread(
    tmp_path: Path,
) -> None:
    """Multi-human threads still require a fresh mention before stale thread mentions can form a team."""
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(display_name="General", rooms=["!test:localhost"]),
                "research": AgentConfig(display_name="Research", rooms=["!test:localhost"]),
                "writer": AgentConfig(display_name="Writer", rooms=["!test:localhost"]),
            },
        ),
        test_runtime_paths(tmp_path),
    )
    config.memory.backend = "file"
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General",
            password="test_password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!test:localhost"],
    )
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:localhost"
    room.users = {
        "@mindroom_general:localhost": MagicMock(),
        "@mindroom_research:localhost": MagicMock(),
        "@mindroom_writer:localhost": MagicMock(),
        "@alice:localhost": MagicMock(),
        "@bob:localhost": MagicMock(),
    }

    context = MessageContext(
        am_i_mentioned=False,
        is_thread=True,
        thread_id="$thread:localhost",
        thread_history=[
            ResolvedVisibleMessage(
                sender="@alice:localhost",
                body="Can the team help?",
                timestamp=1000,
                event_id="$earlier-mention:localhost",
                content={
                    "body": "Can the team help?",
                    "msgtype": "m.text",
                    "m.mentions": {
                        "user_ids": [
                            "@mindroom_general:localhost",
                            "@mindroom_research:localhost",
                            "@mindroom_writer:localhost",
                        ],
                    },
                },
                thread_id="$thread:localhost",
                latest_event_id="$earlier-mention:localhost",
            ),
            ResolvedVisibleMessage(
                sender="@bob:localhost",
                body="I have the same question",
                timestamp=1001,
                event_id="$follow-up:localhost",
                content={"body": "I have the same question", "msgtype": "m.text"},
                thread_id="$thread:localhost",
                latest_event_id="$follow-up:localhost",
            ),
        ],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )

    with patch("mindroom.teams._select_team_mode", new=AsyncMock(return_value=TeamMode.COLLABORATE)):
        action = await bot._turn_policy.resolve_response_action(
            context,
            room,
            "@bob:localhost",
            "Any updates?",
            False,
        )

    assert action.kind == "skip"


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
