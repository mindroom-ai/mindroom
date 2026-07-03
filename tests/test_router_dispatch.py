"""Router-specific dispatch behavior: welcomes, media routing, and dispatch parity."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.attachments import _attachment_id_for_event, register_local_attachment
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.users import AgentMatrixUser
from mindroom.teams import TeamResolution
from mindroom.thread_utils import AgentResponseDecision
from tests.bot_helpers import (
    AgentBotTestBase,
    _attachment_record_stub,
    _install_runtime_cache_support,
    _room_send_response,
    _runtime_bound_config,
    _set_turn_store_tracker,
    _wrap_extracted_collaborators,
    make_mock_agent_user,
)
from tests.conftest import (
    TEST_PASSWORD,
    dispatch_context_result,
    drain_coalescing,
    install_generate_response_mock,
    install_runtime_cache_support,
    install_send_response_mock,
    runtime_paths_for,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.message_target import MessageTarget


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_router_routes_image_messages_in_multi_agent_rooms(
        self,
        tmp_path: Path,
    ) -> None:
        """Router should call _handle_ai_routing for images in multi-responder rooms."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        bot._turn_controller._execute_router_relay = AsyncMock()

        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []
        mock_context.has_non_agent_mentions = False
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        bot._conversation_resolver.extract_message_context = AsyncMock(return_value=mock_context)

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@mindroom_calculator:localhost": None,
            "@user:localhost": None,
        }

        event = nio.RoomMessageImage.from_dict(
            {
                "event_id": "$img_route",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.image",
                    "body": "photo.jpg",
                    "url": "mxc://localhost/test_image",
                    "info": {"mimetype": "image/jpeg"},
                },
            },
        )

        with (
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.thread_requires_explicit_agent_targeting", return_value=False),
            patch("mindroom.turn_policy.responder_candidate_entities_for_room") as mock_get_available,
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.dispatch_handoff.extract_media_caption", return_value="[Attached image]"),
        ):
            mock_get_available.return_value = [
                entity_ids(config, runtime_paths_for(config))["general"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ]
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        bot._turn_controller._execute_router_relay.assert_called_once_with(
            room,
            event,
            [],
            "$img_route",
            message="[Attached image]",
            requester_user_id="@user:localhost",
            extra_content={"com.mindroom.original_sender": "@user:localhost"},
        )

    @pytest.mark.asyncio
    async def test_router_joined_room_startup_sends_welcome_after_join(
        self,
        tmp_path: Path,
    ) -> None:
        """Startup room joins should cache the room locally before sending a welcome."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        install_runtime_cache_support(bot)
        bot.rooms = ["!welcome:localhost"]
        bot.client = AsyncMock()
        bot.client.user_id = agent_user.user_id
        bot.client.rooms = {}
        bot.client.join = AsyncMock(return_value=nio.JoinResponse("!welcome:localhost"))
        bot.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id="!welcome:localhost",
                chunk=[],
                start="",
                end=None,
            ),
        )

        async def fake_send_response(*, target: MessageTarget, **_: object) -> str:
            assert target.room_id in bot.client.rooms
            return "$welcome"

        send_response = AsyncMock(side_effect=fake_send_response)
        install_send_response_mock(bot, send_response)
        with (
            patch(
                "mindroom.bot_room_lifecycle.generate_welcome_message_for_room",
                new=AsyncMock(return_value="Welcome"),
            ),
            patch("mindroom.bot_room_lifecycle.get_joined_rooms", new=AsyncMock(return_value=[])),
            patch("mindroom.bot.restore_scheduled_tasks", new=AsyncMock(return_value=0)),
            patch("mindroom.bot.config_confirmation.restore_pending_changes", new=AsyncMock(return_value=0)),
        ):
            await bot.join_configured_rooms()

        assert "!welcome:localhost" in bot.client.rooms
        bot.client.join.assert_awaited_once_with("!welcome:localhost")
        send_response.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_router_routes_file_messages_with_sender_metadata(
        self,
        tmp_path: Path,
    ) -> None:
        """Router should pass sender metadata when routing file messages."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )

        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        bot._turn_controller._execute_router_relay = AsyncMock()

        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []
        mock_context.has_non_agent_mentions = False
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        bot._conversation_resolver.extract_message_context = AsyncMock(return_value=mock_context)

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@mindroom_calculator:localhost": None,
            "@user:localhost": None,
        }

        event = nio.RoomMessageFile.from_dict(
            {
                "event_id": "$file_route",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.file",
                    "body": "report.pdf",
                    "url": "mxc://localhost/test_file",
                    "info": {"mimetype": "application/pdf"},
                },
            },
        )
        local_media_path = tmp_path / "incoming_media" / "file_route.pdf"
        local_media_path.parent.mkdir(parents=True, exist_ok=True)
        local_media_path.write_bytes(b"%PDF")
        attachment_record = register_local_attachment(
            tmp_path,
            local_media_path,
            kind="file",
            attachment_id=_attachment_id_for_event("$file_route"),
            filename="report.pdf",
            mime_type="application/pdf",
            room_id=room.room_id,
            thread_id=None,
            source_event_id="$file_route",
            sender="@user:localhost",
        )
        assert attachment_record is not None

        with (
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.thread_requires_explicit_agent_targeting", return_value=False),
            patch("mindroom.turn_policy.responder_candidate_entities_for_room") as mock_get_available,
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch(
                "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_file,
        ):
            mock_get_available.return_value = [
                entity_ids(config, runtime_paths_for(config))["general"],
                entity_ids(config, runtime_paths_for(config))["calculator"],
            ]
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        bot._turn_controller._execute_router_relay.assert_called_once()
        mock_register_file.assert_not_awaited()
        call_kwargs = bot._turn_controller._execute_router_relay.call_args.kwargs
        assert call_kwargs["message"] == "[Attached file]"
        assert call_kwargs["requester_user_id"] == "@user:localhost"
        assert call_kwargs["extra_content"] == {ORIGINAL_SENDER_KEY: "@user:localhost"}

    @pytest.mark.asyncio
    async def test_router_routing_registers_file_with_effective_thread_scope(
        self,
        tmp_path: Path,
    ) -> None:
        """Router should register routed file attachments using the outgoing thread scope."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )

        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="General",
                        rooms=["!test:localhost"],
                        thread_mode="room",
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_turn_store_tracker(bot, MagicMock())
        send_response = AsyncMock(return_value="$route")
        install_send_response_mock(bot, send_response)

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        event = nio.RoomMessageFile.from_dict(
            {
                "event_id": "$file_route",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.file",
                    "body": "report.pdf",
                    "url": "mxc://localhost/test_file",
                    "info": {"mimetype": "application/pdf"},
                },
            },
        )
        media_path = tmp_path / "incoming_media" / "file_route.pdf"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"%PDF")
        attachment_record = register_local_attachment(
            tmp_path,
            media_path,
            kind="file",
            attachment_id=_attachment_id_for_event("$file_route"),
            filename="report.pdf",
            mime_type="application/pdf",
            room_id=room.room_id,
            thread_id=None,
            source_event_id="$file_route",
            sender="@user:localhost",
        )
        assert attachment_record is not None

        with (
            patch(
                "mindroom.turn_controller.suggest_responder_for_message",
                new_callable=AsyncMock,
                return_value="general",
            ),
            patch(
                "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_file,
        ):
            await bot._turn_controller._execute_router_relay(
                room=room,
                event=event,
                thread_history=[],
                thread_id=None,
                message="[Attached file]",
                requester_user_id="@user:localhost",
                extra_content={ORIGINAL_SENDER_KEY: "@user:localhost"},
            )

        mock_register_file.assert_awaited_once()
        assert mock_register_file.await_args.kwargs["thread_id"] is None
        sent_extra_content = send_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == [attachment_record.attachment_id]

    @pytest.mark.asyncio
    async def test_router_routing_registers_image_with_effective_thread_scope(
        self,
        tmp_path: Path,
    ) -> None:
        """Router should register routed image attachments using outgoing thread scope."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )

        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(
                        display_name="General",
                        rooms=["!test:localhost"],
                        thread_mode="room",
                    ),
                },
            ),
            tmp_path,
        )
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()
        _set_turn_store_tracker(bot, MagicMock())
        send_response = AsyncMock(return_value="$route")
        install_send_response_mock(bot, send_response)

        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        event = nio.RoomMessageImage.from_dict(
            {
                "event_id": "$image_route",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.image",
                    "body": "photo.jpg",
                    "url": "mxc://localhost/test_image",
                    "info": {"mimetype": "image/jpeg"},
                },
            },
        )

        attachment_record = MagicMock()
        attachment_record.attachment_id = _attachment_id_for_event("$image_route")

        with (
            patch(
                "mindroom.turn_controller.suggest_responder_for_message",
                new_callable=AsyncMock,
                return_value="general",
            ),
            patch(
                "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register_image,
        ):
            await bot._turn_controller._execute_router_relay(
                room=room,
                event=event,
                thread_history=[],
                thread_id=None,
                message="[Attached image]",
                requester_user_id="@user:localhost",
                extra_content={ORIGINAL_SENDER_KEY: "@user:localhost"},
            )

        mock_register_image.assert_awaited_once()
        assert mock_register_image.await_args.kwargs["thread_id"] is None
        sent_extra_content = send_response.await_args.kwargs["extra_content"]
        assert sent_extra_content[ATTACHMENT_IDS_KEY] == [attachment_record.attachment_id]

    @pytest.mark.asyncio
    async def test_multi_agent_file_event_registers_attachment_once(self, tmp_path: Path) -> None:
        """A file event in a multi-responder room should register exactly one attachment."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "general": AgentConfig(display_name="General", rooms=["!test:localhost"]),
                    "calculator": AgentConfig(display_name="Calculator", rooms=["!test:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        router_bot = AgentBot(
            AgentMatrixUser(
                agent_name="router",
                user_id="@mindroom_router:localhost",
                display_name="Router",
                password=TEST_PASSWORD,
                access_token="mock_test_token",  # noqa: S106
            ),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )
        general_bot = AgentBot(
            AgentMatrixUser(
                agent_name="general",
                user_id="@mindroom_general:localhost",
                display_name="General",
                password=TEST_PASSWORD,
                access_token="mock_test_token",  # noqa: S106
            ),
            tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

        router_bot.client = AsyncMock()
        general_bot.client = AsyncMock()
        router_tracker = _set_turn_store_tracker(router_bot, MagicMock())
        router_tracker.has_responded.return_value = False
        general_tracker = _set_turn_store_tracker(general_bot, MagicMock())
        general_tracker.has_responded.return_value = False
        router_send_response = AsyncMock(return_value="$route")
        install_send_response_mock(router_bot, router_send_response)
        general_generate_response = AsyncMock()
        install_generate_response_mock(general_bot, general_generate_response)

        message_context = MessageContext(
            am_i_mentioned=False,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=[],
            has_non_agent_mentions=False,
        )
        router_bot._conversation_resolver.extract_message_context = AsyncMock(return_value=message_context)
        general_bot._conversation_resolver.extract_message_context = AsyncMock(return_value=message_context)

        router_room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_router:localhost")
        general_room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_general:localhost")
        room_users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@mindroom_calculator:localhost": None,
            "@user:localhost": None,
        }
        router_room.users = room_users
        general_room.users = room_users

        file_event = nio.RoomMessageFile.from_dict(
            {
                "event_id": "$file_once",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.file",
                    "body": "report.pdf",
                    "url": "mxc://localhost/file_once",
                    "info": {"mimetype": "application/pdf"},
                },
            },
        )

        media_path = tmp_path / "incoming_media" / "file_once.pdf"
        media_path.parent.mkdir(parents=True, exist_ok=True)
        media_path.write_bytes(b"%PDF")
        attachment_record = register_local_attachment(
            tmp_path,
            media_path,
            kind="file",
            attachment_id=_attachment_id_for_event("$file_once"),
            filename="report.pdf",
            mime_type="application/pdf",
            room_id=router_room.room_id,
            thread_id=None,
            source_event_id="$file_once",
            sender="@user:localhost",
        )
        assert attachment_record is not None

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=MagicMock,
                return_value=TeamResolution.none(),
            ),
            patch(
                "mindroom.turn_controller.suggest_responder_for_message",
                new_callable=AsyncMock,
                return_value="general",
            ),
            patch(
                "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ) as mock_register,
        ):
            await router_bot._on_media_message(router_room, file_event)
            await general_bot._on_media_message(general_room, file_event)
            await drain_coalescing(router_bot, general_bot)

        mock_register.assert_awaited_once()
        assert mock_register.await_args.kwargs["room_id"] == "!test:localhost"
        assert mock_register.await_args.kwargs["thread_id"] == "$file_once"
        general_generate_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_router_dispatch_parity_text_and_image_route_under_same_conditions(self, tmp_path: Path) -> None:
        """Router should route both text and image when the decision context is equivalent."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = _runtime_bound_config(
            Config(
                agents={
                    "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
                    "general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"]),
                },
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        bot._turn_controller._execute_router_relay = AsyncMock()
        bot._conversation_resolver.extract_message_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {
            "@mindroom_router:localhost": MagicMock(),
            "@mindroom_calculator:localhost": MagicMock(),
            "@mindroom_general:localhost": MagicMock(),
            "@user:localhost": MagicMock(),
        }

        text_event = self._make_handler_event("message", sender="@user:localhost", event_id="$route_text")
        text_event.body = "help me"
        text_event.source = {"content": {"body": "help me"}}

        image_event = self._make_handler_event("image", sender="@user:localhost", event_id="$route_img")
        image_event.body = "image.jpg"
        image_event.source = {"content": {"body": "image.jpg"}}

        with (
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.thread_requires_explicit_agent_targeting", return_value=False),
            patch(
                "mindroom.turn_policy.responder_candidate_entities_for_room",
                return_value=[
                    entity_ids(config, runtime_paths_for(config))["calculator"],
                    entity_ids(config, runtime_paths_for(config))["general"],
                ],
            ),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.dispatch_handoff.extract_media_caption", return_value="[Attached image]"),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_message(room, text_event)
            await drain_coalescing(bot)
            await bot._on_media_message(room, image_event)
            await drain_coalescing(bot)

        assert bot._turn_controller._execute_router_relay.await_count == 2
        first_call = bot._turn_controller._execute_router_relay.await_args_list[0].kwargs
        second_call = bot._turn_controller._execute_router_relay.await_args_list[1].kwargs
        assert first_call["requester_user_id"] == "@user:localhost"
        assert first_call["message"] is None
        assert second_call["requester_user_id"] == "@user:localhost"
        assert second_call["message"] == "[Attached image]"

    @pytest.mark.asyncio
    async def test_router_dispatch_parity_text_and_image_skip_under_same_conditions(self, tmp_path: Path) -> None:
        """Router should skip routing both text and image in single-agent-visible rooms."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = _runtime_bound_config(
            Config(
                agents={"calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"])},
                authorization={"default_room_access": True},
            ),
            tmp_path,
        )
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False
        bot._turn_controller._execute_router_relay = AsyncMock()
        bot._conversation_resolver.extract_message_context = AsyncMock(
            return_value=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
        )

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.users = {
            "@mindroom_router:localhost": MagicMock(),
            "@mindroom_calculator:localhost": MagicMock(),
            "@user:localhost": MagicMock(),
        }

        text_event = self._make_handler_event("message", sender="@user:localhost", event_id="$skip_text")
        image_event = self._make_handler_event("image", sender="@user:localhost", event_id="$skip_img")

        with (
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.thread_requires_explicit_agent_targeting", return_value=False),
            patch(
                "mindroom.turn_policy.responder_candidate_entities_for_room",
                return_value=[entity_ids(config, runtime_paths_for(config))["calculator"]],
            ),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.dispatch_handoff.extract_media_caption", return_value="[Attached image]"),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_message(room, text_event)
            await drain_coalescing(bot)
            await bot._on_media_message(room, image_event)
            await drain_coalescing(bot)

        bot._turn_controller._execute_router_relay.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_router_replies_with_guidance_when_only_router_is_mentioned(self, tmp_path: Path) -> None:
        """Mentioning only the router should explain that users must tag routable entities."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token="mock_test_token",  # noqa: S106
        )
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()
        bot.client.room_send.return_value = _room_send_response("$router_guidance")
        tracker = _set_turn_store_tracker(bot, MagicMock())
        tracker.has_responded.return_value = False

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!test:localhost"
        room.canonical_alias = None
        room.encrypted = False
        room.users = {
            "@mindroom_router:localhost": MagicMock(),
            "@mindroom_calculator:localhost": MagicMock(),
            "@mindroom_general:localhost": MagicMock(),
            "@user:localhost": MagicMock(),
        }
        bot.client.rooms = {room.room_id: room}

        event = self._make_handler_event("message", sender="@user:localhost", event_id="$router_only")
        event.body = "@mindroom_router:localhost help me"
        event.source = {
            "content": {
                "body": event.body,
                "m.mentions": {"user_ids": ["@mindroom_router:localhost"]},
            },
        }

        with (
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_message(room, event)
            await drain_coalescing(bot)

        bot.client.room_send.assert_awaited_once()
        content = bot.client.room_send.await_args.kwargs["content"]
        assert content["body"].startswith("🧭")
        assert "router is not a conversational AI agent" in content["body"]
        assert "mention a specific agent or team" in content["body"]
        assert "one human and one agent or team are already talking in a thread" in content["body"]
        assert "thread has multiple human users or multiple agent/team participants" in content["body"]
        assert "automatic routing can still choose an agent or team" in content["body"]

    @pytest.mark.asyncio
    async def test_agent_receives_images_from_thread_root_after_routing(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """After router routes an image, the selected agent should resolve it via attachments."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _install_runtime_cache_support(bot)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)
        generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, generate_response)

        # Simulate the routing mention event in a thread rooted at the image
        room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="@mindroom_calculator:localhost")

        bot._conversation_resolver.extract_dispatch_context = AsyncMock(
            return_value=dispatch_context_result(
                MessageContext(
                    am_i_mentioned=True,
                    is_thread=True,
                    thread_id="$img_root",
                    thread_history=ThreadHistoryResult([], is_full_history=True),
                    mentioned_agents=[mock_agent_user.matrix_id],
                    has_non_agent_mentions=False,
                ),
            ),
        )

        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$router_mention",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.text",
                    "body": "@calculator could you help with this?",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$img_root"},
                },
            },
        )

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch(
                "mindroom.turn_controller.interactive.handle_text_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch("mindroom.turn_policy.get_agents_in_thread", return_value=[]),
            patch("mindroom.turn_policy.responder_candidate_entities_for_room", return_value=[]),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
                new_callable=AsyncMock,
                return_value=["att_img_root"],
            ) as mock_resolve_attachment_ids,
            patch(
                "mindroom.inbound_turn_normalizer.resolve_scoped_attachments",
                return_value=[_attachment_record_stub("att_img_root")],
            ),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=MagicMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.decide_agent_response", return_value=AgentResponseDecision(True)),
        ):
            await bot._on_message(room, event)
            await drain_coalescing(bot)

        mock_resolve_attachment_ids.assert_awaited_once()
        generate_response.assert_awaited_once()
        call_kwargs = generate_response.call_args.kwargs
        # The root image is a thread-history attachment now, so it is pinned to
        # its history message instead of riding the current-turn media inputs.
        assert list(call_kwargs["media"].images) == []
        assert call_kwargs["attachment_ids"] == ["att_img_root"]
        assert call_kwargs["model_prompt"] is None
