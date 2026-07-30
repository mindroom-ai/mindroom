"""Media ingress and dispatch payload assembly: images, audio, files, and attachment merging."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from agno.media import Image

from mindroom.attachments import _attachment_id_for_event, register_local_attachment
from mindroom.bot import AgentBot
from mindroom.coalescing import CoalescingGate, LaneSlot, ReadyPendingEvent
from mindroom.coalescing_batch import CoalescedBatch, CoalescingKey, PendingEvent
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    SOURCE_KIND_KEY,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_source import (
    VOICE_SOURCE_KIND,
)
from mindroom.history.types import HistoryScope
from mindroom.hooks import (
    EnrichmentItem,
)
from mindroom.inbound_turn_normalizer import DispatchPayload, DispatchPayloadWithAttachmentsRequest
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.message_target import MessageTarget
from mindroom.teams import TeamResolution
from mindroom.thread_utils import AgentResponseDecision
from mindroom.turn_controller import _IngressAdmissionOutcome
from mindroom.turn_policy import PreparedDispatch
from tests.bot_helpers import (
    AgentBotTestBase,
    _agent_response_handled_turn,
    _assert_ready_voice_text_fallback,
    _attachment_record_stub,
    _hook_envelope,
    _make_matrix_client_mock,
    _MediaKind,
    _payload_media_for_kind,
    _register_payload_image_attachment,
    _register_payload_media_attachment,
    _replace_turn_policy_deps,
    _room_file_event,
    _room_image_event,
    _set_turn_store_tracker,
    _visible_message,
    _wrap_extracted_collaborators,
    make_mock_agent_user,
)
from tests.conftest import (
    dispatch_context_result,
    drain_coalescing,
    install_generate_response_mock,
    replace_turn_controller_deps,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.matrix.users import AgentMatrixUser


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Mock agent user for testing."""
    return make_mock_agent_user()


class TestAgentBot(AgentBotTestBase):
    """Bot behavior tests moved verbatim from tests/test_multi_agent_bot.py."""

    @pytest.mark.asyncio
    async def test_agent_bot_on_image_message_forwards_image_to_generate_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image messages should reach response generation with an images payload."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

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
        generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, generate_response)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = _room_image_event(sender="@user:localhost", event_id="$img_event", body="photo.jpg")
        event.source = {"content": {"body": "photo.jpg"}}  # no filename → body is filename

        image = MagicMock()
        image.content = b"image-bytes"
        image.mime_type = "image/jpeg"
        attachment_id = _attachment_id_for_event("$img_event")
        attachment_record = MagicMock()
        attachment_record.attachment_id = attachment_id

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=MagicMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.decide_agent_response", return_value=AgentResponseDecision(True)),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=image),
            patch(
                "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_scoped_attachments",
                return_value=[_attachment_record_stub(attachment_id)],
            ),
            patch(
                "mindroom.inbound_turn_normalizer.attachment_records_to_media",
                return_value=([], [image], [], []),
            ),
        ):
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        generate_response.assert_awaited_once()
        generate_kwargs = generate_response.await_args.kwargs
        response_target = generate_kwargs["response_envelope"].target
        assert response_target.room_id == "!test:localhost"
        assert "Attachments sent with the current message" not in generate_kwargs["prompt"]
        assert generate_kwargs["model_prompt"] is not None
        assert "Attachments sent with the current message" in generate_kwargs["model_prompt"]
        assert attachment_id in generate_kwargs["model_prompt"]
        assert response_target.reply_to_event_id == "$img_event"
        assert response_target.resolved_thread_id == "$img_event"
        assert generate_kwargs["thread_history"] == []
        assert response_target.source_thread_id is None
        assert generate_kwargs["user_id"] == "@user:localhost"
        media = generate_kwargs["media"]
        assert list(media.images) == [image]
        assert list(media.audio) == []
        assert list(media.files) == []
        assert list(media.videos) == []
        assert generate_kwargs["attachment_ids"] == [attachment_id]
        expected_handled_turn = _agent_response_handled_turn(
            agent_name=mock_agent_user.agent_name,
            room_id=room.room_id,
            event_id="$img_event",
            response_event_id="$response",
            requester_id="@user:localhost",
            correlation_id="$img_event",
            source_event_prompts={"$img_event": "[Attached image]"},
        )
        expected_handled_turn = replace(
            expected_handled_turn,
            conversation_target=MessageTarget.resolve(
                room_id=room.room_id,
                thread_id=None,
                reply_to_event_id="$img_event",
            ).with_thread_root("$img_event"),
        )
        tracker.record_handled_turn.assert_called_once_with(
            expected_handled_turn,
        )

    @pytest.mark.asyncio
    async def test_media_dispatch_appends_live_event_before_enqueue(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image/file media dispatch should update the live cache before enqueueing dispatch."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock()
        room.room_id = "!test:localhost"
        event = self._make_handler_event("image", sender="@user:localhost", event_id="$img_event")
        prechecked_event = SimpleNamespace(event=event, requester_user_id="@user:localhost")
        bot._conversation_cache.append_live_event = AsyncMock()
        bot._conversation_resolver.coalescing_thread_id = AsyncMock(return_value=None)
        bot._turn_controller._precheck_dispatch_event = MagicMock(return_value=prechecked_event)
        bot._turn_controller._dispatch_special_media_as_text = AsyncMock(return_value=_IngressAdmissionOutcome.IGNORED)
        bot._turn_controller._enqueue_for_dispatch = AsyncMock()

        await bot._turn_controller._handle_media_message_inner(room, event)

        bot._conversation_cache.append_live_event.assert_awaited_once()
        append_args = bot._conversation_cache.append_live_event.await_args
        assert append_args.args == ("!test:localhost", event)
        assert append_args.kwargs["event_info"].is_edit is False
        bot._conversation_resolver.coalescing_thread_id.assert_awaited_once_with(room, event)
        bot._turn_controller._enqueue_for_dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_audio_dispatch_resolves_thread_key_before_admit_and_defers_stt(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Audio dispatch should reserve receive order, then admit under a resolved key."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = SimpleNamespace(room_id="!test:localhost")
        event = self._make_handler_event("voice", sender="@user:localhost", event_id="$voice_event")
        call_order: list[str] = []
        admitted_ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None
        release_stt = asyncio.Event()

        async def record_append(*_args: object, **_kwargs: object) -> None:
            call_order.append("append")

        async def record_thread_id(_room: object, _event: object) -> str:
            call_order.append("coalescing_thread")
            return "$thread_root"

        original_enter_lane = bot._coalescing_gate.enter_lane

        def record_enter_lane(
            *,
            room_id: str,
            sender_id: str,
            receipt_time: float | None = None,
        ) -> LaneSlot:
            call_order.append("reserve")
            return original_enter_lane(room_id=room_id, sender_id=sender_id, receipt_time=receipt_time)

        def record_submit(
            slot: LaneSlot,
            *,
            key: CoalescingKey,
            source_event_id: str | None,
            source_kind: str,
            ready_task: asyncio.Task[ReadyPendingEvent | None] | None = None,
            **_ignored: object,
        ) -> None:
            assert ready_task is not None
            nonlocal admitted_ready_task
            call_order.append("admit")
            assert key == CoalescingKey("!test:localhost", "$thread_root", "@user:localhost")
            assert source_event_id == "$voice_event"
            assert source_kind == VOICE_SOURCE_KIND
            assert slot.released is False
            admitted_ready_task = ready_task

        async def record_voice_normalization(*_args: object, **_kwargs: object) -> None:
            call_order.append("normalize")
            await release_stt.wait()

        bot._conversation_cache.append_live_event = AsyncMock(side_effect=record_append)
        bot._conversation_resolver.coalescing_thread_id = AsyncMock(side_effect=record_thread_id)
        bot._turn_controller._precheck_dispatch_event = MagicMock(
            return_value=SimpleNamespace(event=event, requester_user_id="@user:localhost"),
        )
        bot._turn_controller._dispatch_special_media_as_text = AsyncMock(return_value=_IngressAdmissionOutcome.IGNORED)
        bot._turn_controller._enqueue_for_dispatch = AsyncMock()
        bot._coalescing_gate.enter_lane = MagicMock(side_effect=record_enter_lane)
        bot._coalescing_gate.submit_lane_slot = mock_submit = MagicMock(side_effect=record_submit)

        with patch(
            "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_voice_event",
            new=AsyncMock(side_effect=record_voice_normalization),
        ):
            await bot._turn_controller._handle_media_message_inner(room, event)
            mock_submit.assert_called_once()
            assert call_order == ["reserve", "append", "coalescing_thread", "admit"]
            assert admitted_ready_task is not None
            release_stt.set()
            ready_event = await admitted_ready_task
        admitted_slot = mock_submit.call_args.args[0]
        bot._coalescing_gate.release_lane_slot(admitted_slot)
        await asyncio.wait_for(admitted_slot.settled.wait(), timeout=1.0)
        _assert_ready_voice_text_fallback(ready_event)
        assert call_order == ["reserve", "append", "coalescing_thread", "admit", "normalize"]
        bot._conversation_cache.append_live_event.assert_awaited_once()
        bot._conversation_resolver.coalescing_thread_id.assert_awaited_once_with(
            room,
            event,
        )
        bot._turn_controller._dispatch_special_media_as_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_audio_dispatch_releases_receive_order_when_target_resolution_is_cancelled(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Cancelled pre-admission audio resolution must not leave gate work behind."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock()
        room.room_id = "!test:localhost"
        event = self._make_handler_event("voice", sender="@user:localhost", event_id="$voice_event")
        prechecked_event = SimpleNamespace(event=event, requester_user_id="@user:localhost")

        bot._turn_controller._precheck_dispatch_event = MagicMock(return_value=prechecked_event)
        bot._turn_controller._dispatch_special_media_as_text = AsyncMock(return_value=_IngressAdmissionOutcome.IGNORED)
        bot._turn_controller._resolve_ready_voice_target = AsyncMock(side_effect=asyncio.CancelledError)

        with pytest.raises(asyncio.CancelledError):
            await bot._turn_controller._handle_media_message_inner(room, event)

        await asyncio.wait_for(bot._coalescing_gate.drain_all(), timeout=1.0)
        assert bot._coalescing_gate.lanes.all_settled()

    @pytest.mark.asyncio
    async def test_text_reserves_receive_order_before_thread_lookup(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An earlier text message must not be overtaken by a later voice message."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock()
        room.room_id = "!test:localhost"
        text_event = self._make_handler_event("message", sender="@user:localhost", event_id="$typed")
        text_event.body = "typed first"
        text_event.source = {
            "event_id": "$typed",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "room_id": room.room_id,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "typed first"},
        }
        voice_event = self._make_handler_event("voice", sender="@user:localhost", event_id="$voice")
        release_text_lookup = asyncio.Event()
        dispatches: list[list[str]] = []

        async def coalescing_thread_id(_room: nio.MatrixRoom, event: nio.Event) -> str | None:
            if event.event_id == "$typed":
                await release_text_lookup.wait()
            return "$thread-root"

        async def dispatch_batch(batch: CoalescedBatch) -> None:
            dispatches.append(list(batch.source_event_ids))

        bot._coalescing_gate = CoalescingGate(
            dispatch_batch=dispatch_batch,
            debounce_seconds=lambda: 0.01,
            is_shutting_down=lambda: False,
        )
        replace_turn_controller_deps(bot, coalescing_gate=bot._coalescing_gate)
        bot._turn_controller.deps.resolver.coalescing_thread_id = AsyncMock(side_effect=coalescing_thread_id)
        bot._turn_controller._resolve_ready_voice_target = AsyncMock(
            return_value=(
                bot._turn_controller.deps.resolver.build_message_target(
                    room_id=room.room_id,
                    thread_id="$thread-root",
                    reply_to_event_id=voice_event.event_id,
                    event_source=voice_event.source,
                ),
                CoalescingKey(room.room_id, "$thread-root", "@user:localhost"),
            ),
        )
        bot._turn_controller._ready_voice_event = AsyncMock(
            return_value=ReadyPendingEvent(
                pending_event=PendingEvent(
                    event=PreparedTextEvent(
                        sender="@user:localhost",
                        event_id="$voice",
                        body="voice second",
                        source={
                            "content": {
                                "body": "voice second",
                                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread-root"},
                                SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
                            },
                        },
                        source_kind_override=VOICE_SOURCE_KIND,
                    ),
                    room=room,
                    source_kind=VOICE_SOURCE_KIND,
                ),
            ),
        )

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.resolve_text_event",
                new=AsyncMock(
                    return_value=PreparedTextEvent(
                        sender="@user:localhost",
                        event_id="$typed",
                        body="typed first",
                        source=text_event.source,
                        server_timestamp=1234567890,
                    ),
                ),
            ),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
        ):
            text_task = asyncio.create_task(bot._turn_controller.handle_text_event(room, text_event))
            await asyncio.sleep(0)
            await bot._turn_controller.handle_media_event(room, voice_event)
            await asyncio.sleep(0.03)

            assert dispatches == []

            release_text_lookup.set()
            await text_task
            await bot._coalescing_gate.drain_all()

        assert dispatches == [["$typed", "$voice"]]

    @pytest.mark.asyncio
    async def test_media_reserves_receive_order_before_thread_lookup(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An earlier non-audio media event must reserve before thread lookup can block."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock()
        room.room_id = "!test:localhost"
        image_event = self._make_handler_event("image", sender="@user:localhost", event_id="$image")
        text_event = self._make_handler_event("message", sender="@user:localhost", event_id="$typed")
        text_event.body = "typed second"
        text_event.source = {
            "event_id": "$typed",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "room_id": room.room_id,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "typed second"},
        }
        release_media_lookup = asyncio.Event()
        dispatches: list[list[str]] = []

        async def coalescing_thread_id(_room: nio.MatrixRoom, event: nio.Event) -> str | None:
            if event.event_id == "$image":
                await release_media_lookup.wait()
            return "$thread-root"

        async def dispatch_batch(batch: CoalescedBatch) -> None:
            dispatches.append(list(batch.source_event_ids))

        bot._coalescing_gate = CoalescingGate(
            dispatch_batch=dispatch_batch,
            debounce_seconds=lambda: 0.01,
            is_shutting_down=lambda: False,
        )
        replace_turn_controller_deps(bot, coalescing_gate=bot._coalescing_gate)
        bot._turn_controller.deps.resolver.coalescing_thread_id = AsyncMock(side_effect=coalescing_thread_id)

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.resolve_text_event",
                new=AsyncMock(
                    return_value=PreparedTextEvent(
                        sender="@user:localhost",
                        event_id="$typed",
                        body="typed second",
                        source=text_event.source,
                        server_timestamp=1234567891,
                    ),
                ),
            ),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
        ):
            media_task = asyncio.create_task(bot._turn_controller.handle_media_event(room, image_event))
            await asyncio.sleep(0)
            await bot._turn_controller.handle_text_event(room, text_event)
            await asyncio.sleep(0.03)

            assert dispatches == []

            release_media_lookup.set()
            await media_task
            await bot._coalescing_gate.drain_all()

        assert dispatches == [["$image", "$typed"]]

    @pytest.mark.asyncio
    async def test_file_sidecar_preview_reserves_receive_order_before_preview_normalization(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """An earlier file sidecar text preview must reserve before preview normalization can block."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        room = MagicMock()
        room.room_id = "!test:localhost"
        sidecar_event = self._make_handler_event("file", sender="@user:localhost", event_id="$sidecar")
        sidecar_event.source["content"]["io.mindroom.long_text"] = {
            "version": 2,
            "encoding": "matrix_event_content_json",
        }
        sidecar_event.source["content"]["info"] = {"mimetype": "application/json"}
        text_event = self._make_handler_event("message", sender="@user:localhost", event_id="$typed")
        text_event.body = "typed second"
        text_event.source = {
            "event_id": "$typed",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "room_id": room.room_id,
            "type": "m.room.message",
            "content": {"msgtype": "m.text", "body": "typed second"},
        }
        prepared_sidecar = PreparedTextEvent(
            sender="@user:localhost",
            event_id="$sidecar",
            body="sidecar first",
            source={
                "event_id": "$sidecar",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "room_id": room.room_id,
                "type": "m.room.message",
                "content": {"msgtype": "m.text", "body": "sidecar first"},
            },
            server_timestamp=1234567890,
        )
        release_preview_normalization = asyncio.Event()
        dispatches: list[list[str]] = []

        async def prepare_file_sidecar_text_event(_event: nio.RoomMessageFile) -> PreparedTextEvent:
            await release_preview_normalization.wait()
            return prepared_sidecar

        async def dispatch_batch(batch: CoalescedBatch) -> None:
            dispatches.append(list(batch.source_event_ids))

        bot._coalescing_gate = CoalescingGate(
            dispatch_batch=dispatch_batch,
            debounce_seconds=lambda: 0.01,
            is_shutting_down=lambda: False,
        )
        replace_turn_controller_deps(bot, coalescing_gate=bot._coalescing_gate)
        bot._turn_controller.deps.resolver.coalescing_thread_id = AsyncMock(return_value="$thread-root")

        with (
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.prepare_file_sidecar_text_event",
                new=AsyncMock(side_effect=prepare_file_sidecar_text_event),
            ),
            patch(
                "mindroom.inbound_turn_normalizer.InboundTurnNormalizer.resolve_text_event",
                new=AsyncMock(
                    return_value=PreparedTextEvent(
                        sender="@user:localhost",
                        event_id="$typed",
                        body="typed second",
                        source=text_event.source,
                        server_timestamp=1234567891,
                    ),
                ),
            ),
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
        ):
            sidecar_task = asyncio.create_task(bot._turn_controller.handle_media_event(room, sidecar_event))
            await asyncio.sleep(0)
            await bot._turn_controller.handle_text_event(room, text_event)
            await asyncio.sleep(0.03)

            assert dispatches == []

            release_preview_normalization.set()
            await sidecar_task
            await bot._coalescing_gate.drain_all()

        assert dispatches == [["$sidecar", "$typed"]]

    @pytest.mark.asyncio
    async def test_media_message_merges_thread_history_attachment_ids(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Media turns should include attachment IDs already referenced in thread history."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        _wrap_extracted_collaborators(bot)
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

        history_attachment_id = "att_prev_image"
        current_attachment_id = _attachment_id_for_event("$img_event_history")

        routed_history = ThreadHistoryResult(
            [
                _visible_message(
                    sender="@user:localhost",
                    event_id="$routed_prev",
                    content={ATTACHMENT_IDS_KEY: [history_attachment_id]},
                ),
            ],
            is_full_history=True,
        )
        bot._conversation_resolver.extract_dispatch_context = AsyncMock(
            return_value=dispatch_context_result(
                MessageContext(
                    am_i_mentioned=False,
                    is_thread=True,
                    thread_id="$thread_root",
                    thread_history=routed_history,
                    mentioned_agents=[],
                    has_non_agent_mentions=False,
                ),
            ),
        )
        generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, generate_response)
        _replace_turn_policy_deps(bot, resolver=bot._conversation_resolver)
        _set_turn_store_tracker(bot, tracker)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = _room_image_event(sender="@user:localhost", event_id="$img_event_history", body="photo.png")
        event.source = {
            "content": {
                "body": "photo.png",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        }

        image = MagicMock()
        image.content = b"\x89PNG\r\n\x1a\npayload"
        image.mime_type = "image/png"
        attachment_record = MagicMock()
        attachment_record.attachment_id = current_attachment_id

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=MagicMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.decide_agent_response", return_value=AgentResponseDecision(True)),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=image),
            patch(
                "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_scoped_attachments",
                return_value=[
                    _attachment_record_stub(current_attachment_id),
                    _attachment_record_stub(history_attachment_id),
                ],
            ) as mock_resolve_media,
            patch(
                "mindroom.inbound_turn_normalizer.attachment_records_to_media",
                return_value=([], [image], [], []),
            ) as mock_records_to_media,
        ):
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        assert mock_resolve_media.call_args_list == [
            call(
                tmp_path,
                [current_attachment_id, history_attachment_id],
                room_id="!test:localhost",
                thread_id="$thread_root",
            ),
        ]
        # Only current-turn records convert to inline media; history media is
        # pinned to its thread-history message instead.
        converted_records = mock_records_to_media.call_args.args[0]
        assert [record.attachment_id for record in converted_records] == [current_attachment_id]

        generate_response.assert_awaited_once()
        generate_kwargs = generate_response.await_args.kwargs
        assert generate_kwargs["attachment_ids"] == [current_attachment_id, history_attachment_id]
        assert current_attachment_id not in generate_kwargs["prompt"]
        assert history_attachment_id not in generate_kwargs["prompt"]
        assert generate_kwargs["model_prompt"] is not None
        model_prompt = generate_kwargs["model_prompt"]
        assert model_prompt.startswith("Attachments sent with the current message")
        assert current_attachment_id in model_prompt
        assert history_attachment_id not in model_prompt
        tracker.record_handled_turn.assert_called_once_with(
            _agent_response_handled_turn(
                agent_name=mock_agent_user.agent_name,
                room_id=room.room_id,
                event_id="$img_event_history",
                response_event_id="$response",
                thread_id="$thread_root",
                requester_id="@user:localhost",
                correlation_id="$img_event_history",
                source_event_prompts={"$img_event_history": "[Attached image]"},
            ),
        )

    @pytest.mark.parametrize("kind", ["audio", "image", "file", "video"])
    @pytest.mark.asyncio
    async def test_dispatch_payload_media_is_current_turn_only(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
        kind: _MediaKind,
    ) -> None:
        """Inline media carries only current-turn attachments while IDs stay thread/history-scoped."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        current_attachment_id = f"att_current_{kind}"
        thread_attachment_id = f"att_thread_{kind}"
        history_attachment_id = f"att_history_{kind}"
        same_content = b"same media bytes"
        current_path = _register_payload_media_attachment(
            tmp_path,
            kind=kind,
            attachment_id=current_attachment_id,
            filename=f"current-{kind}.bin",
            content=same_content,
        )
        _register_payload_media_attachment(
            tmp_path,
            kind=kind,
            attachment_id=thread_attachment_id,
            filename=f"thread-{kind}.bin",
            content=same_content,
        )
        _register_payload_media_attachment(
            tmp_path,
            kind=kind,
            attachment_id=history_attachment_id,
            filename=f"history-{kind}.bin",
            content=same_content,
        )
        thread_history = [
            _visible_message(
                sender="@user:localhost",
                event_id="$history",
                content={ATTACHMENT_IDS_KEY: [history_attachment_id]},
            ),
        ]

        with patch(
            "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
            new_callable=AsyncMock,
            return_value=[thread_attachment_id],
        ):
            payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id="!test:localhost",
                    prompt="describe this",
                    current_attachment_ids=[current_attachment_id],
                    thread_id="$thread",
                    media_thread_id="$thread",
                    thread_history=thread_history,
                ),
            )

        inline_media = _payload_media_for_kind(payload, kind)
        assert len(inline_media) == 1
        assert inline_media[0].id == current_attachment_id
        assert inline_media[0].filepath == current_path
        assert payload.attachment_ids == [current_attachment_id, thread_attachment_id, history_attachment_id]

    @pytest.mark.asyncio
    async def test_dispatch_payload_keeps_history_media_off_current_turn(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Thread and history media stay pinned to their messages, not the current turn."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        current_attachment_id = "att_current_image"
        thread_attachment_id = "att_thread_image"
        history_attachment_id = "att_history_image"
        current_path = _register_payload_image_attachment(
            tmp_path,
            attachment_id=current_attachment_id,
            filename="current.png",
        )
        _register_payload_image_attachment(
            tmp_path,
            attachment_id=thread_attachment_id,
            filename="thread.png",
        )
        _register_payload_image_attachment(
            tmp_path,
            attachment_id=history_attachment_id,
            filename="history.png",
        )
        thread_history = [
            _visible_message(
                sender="@user:localhost",
                event_id="$history",
                content={ATTACHMENT_IDS_KEY: [history_attachment_id]},
            ),
        ]

        with patch(
            "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
            new_callable=AsyncMock,
            return_value=[thread_attachment_id],
        ):
            payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id="!test:localhost",
                    prompt="describe these",
                    current_attachment_ids=[current_attachment_id],
                    thread_id="$thread",
                    media_thread_id="$thread",
                    thread_history=thread_history,
                ),
            )

        inline_image_paths = [image.filepath for image in payload.media.images]
        assert inline_image_paths == [current_path]
        assert payload.attachment_ids == [current_attachment_id, thread_attachment_id, history_attachment_id]

    @pytest.mark.asyncio
    async def test_dispatch_payload_inline_media_empty_when_no_attachments(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Payloads without attachments should have empty inline media and no tool-visible IDs."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()

        payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
            DispatchPayloadWithAttachmentsRequest(
                room_id="!test:localhost",
                prompt="check in",
                current_attachment_ids=[],
                thread_id=None,
                media_thread_id=None,
                thread_history=[],
            ),
        )

        assert payload.media.images == ()
        assert payload.media.audio == ()
        assert payload.media.files == ()
        assert payload.media.videos == ()
        assert payload.attachment_ids is None

    @pytest.mark.asyncio
    async def test_dispatch_payload_fallback_images_preserved(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Fallback images should still populate inline media when no current IDs resolve."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        fallback_image = Image(content=b"\x89PNG\r\n\x1a\nfallback", mime_type="image/png")

        payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
            DispatchPayloadWithAttachmentsRequest(
                room_id="!test:localhost",
                prompt="describe fallback",
                current_attachment_ids=[],
                thread_id=None,
                media_thread_id=None,
                thread_history=[],
                fallback_images=[fallback_image],
            ),
        )

        assert list(payload.media.images) == [fallback_image]
        assert payload.media.audio == ()
        assert payload.media.files == ()
        assert payload.media.videos == ()
        assert payload.attachment_ids is None

    @pytest.mark.asyncio
    async def test_build_dispatch_payload_merges_fallback_images_with_registered_attachments(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Fallback image bytes should be appended instead of discarded when some registrations succeed."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        stored_image = Image(content=b"\x89PNG\r\n\x1a\nstored", mime_type="image/png")
        fallback_image = Image(content=b"\x89PNG\r\n\x1a\nfallback", mime_type="image/png")

        with (
            patch(
                "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_scoped_attachments",
                return_value=[_attachment_record_stub("att_image")],
            ),
            patch(
                "mindroom.inbound_turn_normalizer.attachment_records_to_media",
                return_value=([], [stored_image], [], []),
            ),
        ):
            payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id="!test:localhost",
                    prompt="describe this",
                    current_attachment_ids=["att_image"],
                    thread_id=None,
                    media_thread_id=None,
                    thread_history=[],
                    fallback_images=[fallback_image],
                ),
            )

        assert payload.attachment_ids == ["att_image"]
        assert payload.prompt == "describe this"
        assert payload.model_prompt is not None
        assert "Attachments sent with the current message" in payload.model_prompt
        assert "att_image" in payload.model_prompt
        assert list(payload.media.images) == [stored_image, fallback_image]

    @pytest.mark.asyncio
    async def test_build_dispatch_payload_with_attachments_keeps_raw_prompt_clean(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Attachment IDs should be isolated to model_prompt instead of mutating the raw user prompt."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        stored_image = Image(content=b"\x89PNG\r\n\x1a\nstored", mime_type="image/png")

        with (
            patch(
                "mindroom.inbound_turn_normalizer.resolve_thread_attachment_ids",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "mindroom.inbound_turn_normalizer.resolve_scoped_attachments",
                return_value=[_attachment_record_stub("att_image")],
            ),
            patch(
                "mindroom.inbound_turn_normalizer.attachment_records_to_media",
                return_value=([], [stored_image], [], []),
            ),
        ):
            payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id="!test:localhost",
                    prompt="describe this",
                    current_attachment_ids=["att_image"],
                    thread_id=None,
                    media_thread_id=None,
                    thread_history=[],
                ),
            )

        assert payload.prompt == "describe this"
        assert payload.model_prompt is not None
        assert "Attachments sent with the current message" in payload.model_prompt
        assert "att_image" in payload.model_prompt

    @pytest.mark.asyncio
    async def test_voice_transcript_payload_adds_hidden_audio_guidance(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Successful voice transcripts should explain that raw audio is optional."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        current_attachment_id = "att_current_audio"
        current_path = _register_payload_media_attachment(
            tmp_path,
            kind="audio",
            attachment_id=current_attachment_id,
            filename="voice.ogg",
            content=b"audio bytes",
            thread_id=None,
        )

        payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
            DispatchPayloadWithAttachmentsRequest(
                room_id="!test:localhost",
                prompt="🎤 Please summarize the standup.",
                current_attachment_ids=[current_attachment_id],
                thread_id=None,
                media_thread_id=None,
                thread_history=[],
                raw_audio_fallback=False,
                voice_transcript=True,
            ),
        )

        assert payload.prompt == "🎤 Please summarize the standup."
        assert payload.model_prompt is not None
        assert "MindRoom already transcribed the current voice message." in payload.model_prompt
        assert current_attachment_id in payload.model_prompt
        assert "Only inspect or re-transcribe" in payload.model_prompt
        assert [audio.id for audio in payload.media.audio] == [current_attachment_id]
        assert payload.media.audio[0].filepath == current_path

    @pytest.mark.asyncio
    async def test_raw_voice_fallback_payload_does_not_claim_audio_was_transcribed(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Raw voice fallback should leave audio transcription up to the agent."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = _make_matrix_client_mock()
        current_attachment_id = "att_raw_audio"
        _register_payload_media_attachment(
            tmp_path,
            kind="audio",
            attachment_id=current_attachment_id,
            filename="voice.ogg",
            content=b"audio bytes",
            thread_id=None,
        )

        payload = await bot._inbound_turn_normalizer.build_dispatch_payload_with_attachments(
            DispatchPayloadWithAttachmentsRequest(
                room_id="!test:localhost",
                prompt="🎤 [Attached voice message]",
                current_attachment_ids=[current_attachment_id],
                thread_id=None,
                media_thread_id=None,
                thread_history=[],
                raw_audio_fallback=True,
                voice_transcript=False,
            ),
        )

        assert payload.model_prompt is not None
        assert "Attachments sent with the current message" in payload.model_prompt
        assert "MindRoom already transcribed the current voice message." not in payload.model_prompt

    @pytest.mark.asyncio
    async def test_message_enrichment_appends_to_existing_model_prompt(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Message enrichment should extend an existing model prompt rather than replacing it."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=False,
                is_thread=False,
                thread_id=None,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
                requires_model_history_refresh=False,
            ),
            target=(dispatch_target := MessageTarget.resolve("!test:localhost", None, "$event")),
            correlation_id="corr-1",
            envelope=_hook_envelope(body="hello", source_event_id="$event", target=dispatch_target),
        )
        registry_stub = MagicMock()
        registry_stub.has_hooks.return_value = True
        bot._ingress_hook_runner.hook_context.hook_registry_state.registry = registry_stub

        with patch(
            "mindroom.turn_policy.emit_collect",
            new=AsyncMock(
                return_value=[EnrichmentItem(key="extra", text="hook enrichment", cache_policy="volatile")],
            ),
        ):
            prepared = await bot._ingress_hook_runner.apply_message_enrichment(
                dispatch,
                DispatchPayload(
                    prompt="hello",
                    model_prompt="Available attachment IDs: att_1. Use tool calls to inspect or process them.",
                    attachment_ids=["att_1"],
                ),
                target_entity_name=mock_agent_user.agent_name,
                target_member_names=None,
            )

        assert prepared.payload.prompt == "hello"
        assert prepared.payload.model_prompt is not None
        assert prepared.payload.model_prompt.startswith("Available attachment IDs: att_1")
        assert "hook enrichment" in prepared.payload.model_prompt

    @pytest.mark.asyncio
    async def test_agent_bot_on_image_message_leaves_event_retryable_when_terminal_error_cannot_be_sent(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Image download failure should not mark the event responded without a visible terminal error."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

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
        generate_response = AsyncMock()
        install_generate_response_mock(bot, generate_response)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = _room_image_event(sender="@user:localhost", event_id="$img_event_fail", body="please analyze")
        event.source = {"content": {"body": "please analyze"}}

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=MagicMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.decide_agent_response", return_value=AgentResponseDecision(True)),
            patch("mindroom.inbound_turn_normalizer.download_image", new_callable=AsyncMock, return_value=None),
        ):
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        generate_response.assert_not_called()
        tracker.record_handled_turn.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_bot_on_file_message_forwards_local_path_to_generate_response(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """File messages should reach response generation with a local media path in prompt."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

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
        generate_response = AsyncMock(return_value="$response")
        install_generate_response_mock(bot, generate_response)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = _room_file_event(sender="@user:localhost", event_id="$file_event", body="report.pdf")
        event.url = "mxc://localhost/report"
        event.source = {"content": {"body": "report.pdf", "msgtype": "m.file"}}

        local_media_path = tmp_path / "incoming_media" / "file.pdf"
        local_media_path.parent.mkdir(parents=True, exist_ok=True)
        local_media_path.write_bytes(b"pdf")
        attachment_record = register_local_attachment(
            tmp_path,
            local_media_path,
            kind="file",
            attachment_id=_attachment_id_for_event("$file_event"),
            filename="report.pdf",
            mime_type="application/pdf",
            room_id=room.room_id,
            thread_id="$file_event",
            source_event_id="$file_event",
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
            patch("mindroom.turn_policy.decide_agent_response", return_value=AgentResponseDecision(True)),
            patch(
                "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
                new_callable=AsyncMock,
                return_value=attachment_record,
            ),
        ):
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        generate_response.assert_awaited_once()
        generate_kwargs = generate_response.await_args.kwargs
        attachment_id = _attachment_id_for_event("$file_event")
        response_target = generate_kwargs["response_envelope"].target
        assert response_target.room_id == "!test:localhost"
        assert response_target.reply_to_event_id == "$file_event"
        assert response_target.resolved_thread_id == "$file_event"
        assert generate_kwargs["thread_history"] == []
        assert response_target.source_thread_id is None
        assert generate_kwargs["user_id"] == "@user:localhost"
        assert generate_kwargs["attachment_ids"] == [attachment_id]
        assert "Attachments sent with the current message" not in generate_kwargs["prompt"]
        assert generate_kwargs["model_prompt"] is not None
        assert "Attachments sent with the current message" in generate_kwargs["model_prompt"]
        assert attachment_id in generate_kwargs["model_prompt"]
        media = generate_kwargs["media"]
        assert len(media.files) == 1
        assert str(media.files[0].filepath) == str(local_media_path)
        assert list(media.videos) == []
        tracker.record_handled_turn.assert_called_once_with(
            replace(
                _agent_response_handled_turn(
                    agent_name=mock_agent_user.agent_name,
                    room_id=room.room_id,
                    event_id="$file_event",
                    response_event_id="$response",
                    requester_id="@user:localhost",
                    correlation_id="$file_event",
                    source_event_prompts={"$file_event": "[Attached file]"},
                ),
                response_owner=mock_agent_user.agent_name,
                history_scope=HistoryScope(kind="agent", scope_id=mock_agent_user.agent_name),
                conversation_target=MessageTarget.resolve(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id="$file_event",
                ).with_thread_root("$file_event"),
            ),
        )

    @pytest.mark.asyncio
    async def test_agent_bot_on_file_message_leaves_event_retryable_when_terminal_error_cannot_be_sent(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """File persistence failure should not mark the event responded without a visible terminal error."""
        config = self._config_for_storage(tmp_path)
        bot = AgentBot(mock_agent_user, tmp_path, config=config, runtime_paths=runtime_paths_for(config))
        bot.client = AsyncMock()

        tracker = MagicMock()
        tracker.has_responded.return_value = False
        _set_turn_store_tracker(bot, tracker)

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
        generate_response = AsyncMock()
        install_generate_response_mock(bot, generate_response)

        room = MagicMock()
        room.room_id = "!test:localhost"

        event = _room_file_event(sender="@user:localhost", event_id="$file_event_fail", body="report.pdf")
        event.url = "mxc://localhost/report"
        event.source = {"content": {"body": "report.pdf", "msgtype": "m.file"}}

        with (
            patch("mindroom.bot.is_authorized_sender", return_value=True),
            patch("mindroom.ingress_validation.is_authorized_sender", return_value=True),
            patch("mindroom.text_ingress_dispatch.is_dm_room", new_callable=AsyncMock, return_value=False),
            patch(
                "mindroom.turn_policy.decide_team_formation",
                new_callable=MagicMock,
                return_value=TeamResolution.none(),
            ),
            patch("mindroom.turn_policy.decide_agent_response", return_value=AgentResponseDecision(True)),
            patch(
                "mindroom.inbound_turn_normalizer.register_matrix_media_attachment",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            await bot._on_media_message(room, event)
            await drain_coalescing(bot)

        generate_response.assert_not_called()
        tracker.record_handled_turn.assert_not_called()
