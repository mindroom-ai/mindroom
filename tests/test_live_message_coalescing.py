"""Tests for live debounce-based message coalescing."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom.bot import AgentBot
from mindroom.coalescing import (
    COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
    CoalescedBatch,
    CoalescingGate,
    GatePhase,
    PendingEvent,
    PreparedTextEvent,
    build_batch_dispatch_event,
    build_coalesced_batch,
    is_coalescing_exempt_source_kind,
)
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
    ORIGINAL_SENDER_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_handoff import DispatchIngressMetadata, DispatchPayloadMetadata, build_dispatch_handoff
from mindroom.handled_turns import HandledTurnState
from mindroom.hooks import MessageEnvelope
from mindroom.inbound_turn_normalizer import (
    BatchMediaAttachmentRequest,
    BatchMediaAttachmentResult,
    DispatchPayload,
    DispatchPayloadWithAttachmentsRequest,
)
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.turn_policy import DispatchPlan, PreparedDispatch
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_generate_response_mock,
    install_send_response_mock,
    make_matrix_client_mock,
    replace_turn_controller_deps,
    runtime_paths_for,
    test_runtime_paths,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


def _make_config(
    tmp_path: Path,
    *,
    debounce_ms: int = 10,
    upload_grace_ms: int = 0,
) -> Config:
    """Build a config with configurable live coalescing timings."""
    return bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="TestAgent", rooms=["!room:localhost"])},
            teams={},
            models={"default": ModelConfig(provider="test", id="test-model")},
            defaults=DefaultsConfig(
                coalescing={
                    "debounce_ms": debounce_ms,
                    "upload_grace_ms": upload_grace_ms,
                },
            ),
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        test_runtime_paths(tmp_path),
    )


def _make_bot(
    tmp_path: Path,
    *,
    debounce_ms: int = 10,
    upload_grace_ms: int = 0,
    agent_name: str = "test_agent",
) -> AgentBot:
    """Create a bot instance wired to a temporary runtime root."""
    config = _make_config(tmp_path, debounce_ms=debounce_ms, upload_grace_ms=upload_grace_ms)
    agent_user = AgentMatrixUser(
        agent_name=agent_name,
        password=TEST_PASSWORD,
        display_name="TestAgent",
        user_id=f"@mindroom_{agent_name}:localhost",
    )
    bot = AgentBot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!room:localhost"])
    bot.client = make_matrix_client_mock(user_id=agent_user.user_id)
    wrap_extracted_collaborators(bot)
    replace_turn_controller_deps(
        bot,
        turn_policy=bot._turn_policy,
        delivery_gateway=bot._delivery_gateway,
        response_runner=bot._response_runner,
        resolver=bot._conversation_resolver,
        normalizer=bot._inbound_turn_normalizer,
        state_writer=bot._conversation_state_writer,
    )
    return bot


def test_coalescing_config_rejects_removed_enabled_flag(tmp_path: Path) -> None:
    """Reject the removed defaults.coalescing.enabled toggle."""
    with pytest.raises(ValidationError, match="enabled"):
        Config.validate_with_runtime(
            {
                "agents": {"test_agent": {"display_name": "TestAgent"}},
                "models": {"default": {"provider": "test", "id": "test-model"}},
                "defaults": {"coalescing": {"enabled": True}},
            },
            test_runtime_paths(tmp_path),
        )


def _respond_dispatch_plan(action: object | None = None) -> DispatchPlan:
    """Return a plan that continues into the response executor path."""
    return DispatchPlan(
        kind="respond",
        response_action=action or MagicMock(kind="individual"),
    )


def _handled_turn_source_event_ids(handled_turn: HandledTurnState | None) -> list[str]:
    """Return source event IDs from one handled-turn carrier for test assertions."""
    return list(handled_turn.source_event_ids) if handled_turn is not None else []


def _make_room(room_id: str = "!room:localhost") -> MagicMock:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = room_id
    room.canonical_alias = None
    return room


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


def _text_event(
    *,
    event_id: str,
    body: str,
    sender: str = "@user:localhost",
    server_timestamp: int = 1000,
    thread_id: str | None = None,
    source_kind: str | None = None,
    original_sender: str | None = None,
) -> nio.RoomMessageText:
    """Build a synthetic inbound text event for coalescing tests."""
    content: dict[str, object] = {
        "msgtype": "m.text",
        "body": body,
    }
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    if source_kind is not None:
        content["com.mindroom.source_kind"] = source_kind
    if original_sender is not None:
        content[ORIGINAL_SENDER_KEY] = original_sender
    return cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": "!room:localhost",
                "type": "m.room.message",
                "content": content,
            },
        ),
    )


def _reply_event(
    *,
    event_id: str,
    body: str,
    reply_to_event_id: str,
    sender: str = "@user:localhost",
    server_timestamp: int = 1000,
) -> nio.RoomMessageText:
    """Build a synthetic inbound plain reply event for coalescing tests."""
    return cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": "!room:localhost",
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": body,
                    "m.relates_to": {
                        "m.in_reply_to": {"event_id": reply_to_event_id},
                    },
                },
            },
        ),
    )


def _image_event(
    *,
    event_id: str,
    body: str = "photo.jpg",
    sender: str = "@user:localhost",
    server_timestamp: int = 1000,
    thread_id: str | None = None,
) -> nio.RoomMessageImage:
    """Build a synthetic inbound image event for coalescing tests."""
    content: dict[str, object] = {
        "msgtype": "m.image",
        "body": body,
        "filename": body,
        "url": "mxc://localhost/test-image",
        "info": {"mimetype": "image/jpeg"},
    }
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return cast(
        "nio.RoomMessageImage",
        nio.RoomMessageImage.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": "!room:localhost",
                "type": "m.room.message",
                "content": content,
            },
        ),
    )


def _file_event(
    *,
    event_id: str,
    body: str = "document.pdf",
    sender: str = "@user:localhost",
    server_timestamp: int = 1000,
    thread_id: str | None = None,
) -> nio.RoomMessageFile:
    """Build a synthetic inbound file event for coalescing tests."""
    content: dict[str, object] = {
        "msgtype": "m.file",
        "body": body,
        "filename": body,
        "url": "mxc://localhost/test-file",
        "info": {"mimetype": "application/pdf"},
    }
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    return cast(
        "nio.RoomMessageFile",
        nio.RoomMessageFile.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": "!room:localhost",
                "type": "m.room.message",
                "content": content,
            },
        ),
    )


def _prepared_dispatch(
    *,
    event_id: str,
    requester_user_id: str = "@user:localhost",
    body: str = "hello",
    thread_id: str | None = None,
    source_kind: str = "message",
    dispatch_policy_source_kind: str | None = None,
) -> PreparedDispatch:
    history: list[ResolvedVisibleMessage] = []
    context = MessageContext(
        am_i_mentioned=True,
        is_thread=thread_id is not None,
        thread_id=thread_id,
        thread_history=history,
        replay_guard_history=history,
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )
    target = MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id=thread_id,
        reply_to_event_id=event_id,
    )
    return PreparedDispatch(
        requester_user_id=requester_user_id,
        context=context,
        target=target,
        correlation_id=event_id,
        envelope=MessageEnvelope(
            source_event_id=event_id,
            room_id="!room:localhost",
            target=target,
            requester_id=requester_user_id,
            sender_id=requester_user_id,
            body=body,
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="test_agent",
            source_kind=source_kind,
            dispatch_policy_source_kind=dispatch_policy_source_kind,
        ),
    )


def _set_context_histories(dispatch: PreparedDispatch, history: Sequence[ResolvedVisibleMessage]) -> None:
    """Keep replay-snapshot and planning history aligned for tests that need both."""
    dispatch.context.thread_history = list(history)
    dispatch.context.replay_guard_history = list(history)


@pytest.mark.asyncio
async def test_single_message_dispatches_after_debounce_window(tmp_path: Path) -> None:
    """Dispatch one text message once the debounce window elapses."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")
    calls: list[tuple[str, list[str], list[object]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn), media_events or []))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        assert calls == []
        await asyncio.sleep(0.03)

    assert calls == [("hello", ["$m1"], [])]


@pytest.mark.asyncio
async def test_two_rapid_text_messages_dispatch_one_combined_turn(tmp_path: Path) -> None:
    """Coalesce two quick text messages into one combined prompt."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second", server_timestamp=1001)
    calls: list[tuple[str, str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.event_id, dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            second,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

    assert calls == [
        (
            "$m2",
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\nfirst\nsecond",
            ["$m1", "$m2"],
        ),
    ]


@pytest.mark.asyncio
async def test_two_rapid_text_messages_forward_prompt_map_to_dispatch(tmp_path: Path) -> None:
    """Coalesced dispatch should forward the per-source prompt map."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second", server_timestamp=1001)

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch:
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            second,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

    assert mock_dispatch.await_count == 1
    handled_turn = mock_dispatch.await_args.kwargs["handled_turn"]
    assert list(handled_turn.source_event_ids) == ["$m1", "$m2"]
    assert handled_turn.source_event_prompts == {
        "$m1": "first",
        "$m2": "second",
    }


@pytest.mark.asyncio
async def test_image_and_text_coalesce_into_single_dispatch(tmp_path: Path) -> None:
    """Coalesce image uploads and follow-up text into one dispatch."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    image_event = _image_event(event_id="$img1", server_timestamp=1000)
    text_event = _text_event(event_id="$m2", body="describe it", server_timestamp=1001)
    calls: list[tuple[str, list[str], int]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn), len(media_events or [])))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            image_event,
            room,
            source_kind="image",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            text_event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

    assert calls == [
        (
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\n[Attached image]\ndescribe it",
            ["$img1", "$m2"],
            1,
        ),
    ]


@pytest.mark.asyncio
async def test_text_first_image_during_debounce_dispatches_without_upload_grace_delay(tmp_path: Path) -> None:
    """Do not add upload-grace delay once media already joined during debounce."""
    bot = _make_bot(tmp_path, debounce_ms=20, upload_grace_ms=200)
    room = _make_room()
    text_event = _text_event(event_id="$m1", body="describe this", server_timestamp=1000)
    image_event = _image_event(event_id="$img1", server_timestamp=1001)
    calls: list[tuple[list[str], int]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        calls.append((_handled_turn_source_event_ids(handled_turn), len(media_events or [])))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            text_event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.005)
        await bot._turn_controller._enqueue_for_dispatch(
            image_event,
            room,
            source_kind="image",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: calls == [(["$m1", "$img1"], 1)], deadline_seconds=0.12)

    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_text_first_image_during_grace_dispatches_once(tmp_path: Path) -> None:
    """Hold a text-only batch briefly so a late image joins the first dispatch."""
    bot = _make_bot(tmp_path, debounce_ms=10, upload_grace_ms=40)
    room = _make_room()
    text_event = _text_event(event_id="$m1", body="describe this", server_timestamp=1000)
    image_event = _image_event(event_id="$img1", server_timestamp=1001)
    calls: list[tuple[str, list[str], int]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn), len(media_events or [])))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            text_event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        # Wait for debounce to fire (10ms) so gate enters upload grace
        await asyncio.sleep(0.02)
        assert calls == []

        await bot._turn_controller._enqueue_for_dispatch(
            image_event,
            room,
            source_kind="image",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 1)

    assert calls == [
        (
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\ndescribe this\n[Attached image]",
            ["$m1", "$img1"],
            1,
        ),
    ]


@pytest.mark.asyncio
async def test_text_first_multiple_images_during_grace_dispatch_once(tmp_path: Path) -> None:
    """Merge several uploads that arrive during upload grace into one batch."""
    bot = _make_bot(tmp_path, debounce_ms=10, upload_grace_ms=40)
    room = _make_room()
    text_event = _text_event(event_id="$m1", body="summarize these", server_timestamp=1000)
    first_image = _image_event(event_id="$img1", server_timestamp=1001)
    second_image = _image_event(event_id="$img2", server_timestamp=1002)
    calls: list[tuple[list[str], int]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = handled_turn
        calls.append((_handled_turn_source_event_ids(handled_turn), len(media_events or [])))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            text_event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        # Wait for debounce to fire (10ms) so gate enters upload grace
        await asyncio.sleep(0.02)
        assert calls == []

        await bot._turn_controller._enqueue_for_dispatch(
            first_image,
            room,
            source_kind="image",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.01)
        await bot._turn_controller._enqueue_for_dispatch(
            second_image,
            room,
            source_kind="image",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 1)

    assert calls == [(["$m1", "$img1", "$img2"], 2)]


@pytest.mark.asyncio
async def test_text_during_upload_grace_flushes_pending_batch_and_starts_new_turn(tmp_path: Path) -> None:
    """Plain text should not join an upload-grace batch meant only for late media."""
    bot = _make_bot(tmp_path, debounce_ms=40, upload_grace_ms=200)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first turn", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second turn", server_timestamp=1001)
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        # Wait for debounce to fire (40ms) so gate enters upload grace
        await asyncio.sleep(0.06)
        assert calls == []

        await bot._turn_controller._enqueue_for_dispatch(
            second,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )

        await _wait_for(lambda: len(calls) >= 1)
        assert calls == [("first turn", ["$m1"])]

        await _wait_for(lambda: len(calls) == 2)

    assert calls == [
        ("first turn", ["$m1"]),
        ("second turn", ["$m2"]),
    ]


@pytest.mark.asyncio
async def test_image_after_grace_expires_dispatches_as_second_batch(tmp_path: Path) -> None:
    """Uploads that arrive after grace expires should remain a later turn."""
    bot = _make_bot(tmp_path, debounce_ms=10, upload_grace_ms=40)
    room = _make_room()
    text_event = _text_event(event_id="$m1", body="first turn", server_timestamp=1000)
    image_event = _image_event(event_id="$img1", server_timestamp=1001)
    calls: list[tuple[list[str], int]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = handled_turn
        calls.append((_handled_turn_source_event_ids(handled_turn), len(media_events or [])))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            text_event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 1)

        await bot._turn_controller._enqueue_for_dispatch(
            image_event,
            room,
            source_kind="image",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 2)

    assert calls == [
        (["$m1"], 0),
        (["$img1"], 1),
    ]


@pytest.mark.asyncio
async def test_different_senders_dispatch_separately(tmp_path: Path) -> None:
    """Keep coalescing isolated per sending Matrix user."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    alice = _text_event(event_id="$m1", body="hi", sender="@alice:localhost")
    bob = _text_event(event_id="$m2", body="hello", sender="@bob:localhost")
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(_handled_turn_source_event_ids(handled_turn))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            alice,
            room,
            source_kind="message",
            requester_user_id="@alice:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            bob,
            room,
            source_kind="message",
            requester_user_id="@bob:localhost",
        )
        await asyncio.sleep(0.03)

    assert sorted(calls) == [["$m1"], ["$m2"]]


def test_build_coalesced_batch_keeps_normalized_voice_out_of_media_events() -> None:
    """Voice messages should enter coalescing as synthetic text, not raw media."""
    room = _make_room()
    voice_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice1",
        body="transcribed voice",
        source={"content": {"body": "transcribed voice", "com.mindroom.source_kind": "voice"}},
    )

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [PendingEvent(event=voice_event, room=room, source_kind="voice")],
    )

    assert batch.prompt == "transcribed voice"
    assert batch.source_event_ids == ["$voice1"]
    assert batch.media_events == []


def test_build_coalesced_batch_preserves_fifo_order_with_synthetic_events() -> None:
    """Preserve queue order even when Matrix timestamps disagree."""
    room = _make_room()
    real_event = _text_event(event_id="$real", body="real", server_timestamp=1_712_350_002_000)
    synthetic_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$synthetic",
        body="synthetic",
        source={"content": {"body": "synthetic", "com.mindroom.source_kind": "voice"}},
        server_timestamp=1_712_350_003_000,
    )

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [
            PendingEvent(event=synthetic_event, room=room, source_kind="voice", enqueue_time=50_000.0),
            PendingEvent(event=real_event, room=room, source_kind="message"),
        ],
    )

    assert batch.source_event_ids == ["$synthetic", "$real"]
    assert batch.prompt.endswith("synthetic\nreal")


def test_build_coalesced_batch_prefers_media_source_kind_over_text_primary() -> None:
    """Mixed batches should keep media source_kind even when text is the primary event."""
    room = _make_room()
    image_event = _image_event(event_id="$img1", server_timestamp=1000)
    text_event = _text_event(event_id="$m2", body="describe it", server_timestamp=1001)

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [
            PendingEvent(event=image_event, room=room, source_kind="image"),
            PendingEvent(event=text_event, room=room, source_kind="message"),
        ],
    )

    assert batch.primary_event is text_event
    assert batch.source_kind == "image"


def test_build_coalesced_batch_prefers_voice_source_kind_over_media_and_text() -> None:
    """Voice should win batch source_kind precedence even when a text event is primary."""
    room = _make_room()
    voice_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice1",
        body="voice prompt",
        source={"content": {"body": "voice prompt", "com.mindroom.source_kind": "voice"}},
    )
    image_event = _image_event(event_id="$img1", server_timestamp=1000)
    text_event = _text_event(event_id="$m2", body="follow-up", server_timestamp=1001)

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [
            PendingEvent(event=voice_event, room=room, source_kind="voice", enqueue_time=0.5),
            PendingEvent(event=image_event, room=room, source_kind="image"),
            PendingEvent(event=text_event, room=room, source_kind="message"),
        ],
    )

    assert batch.primary_event is text_event
    assert batch.source_kind == "voice"


@pytest.mark.asyncio
async def test_same_sender_different_threads_dispatch_separately(tmp_path: Path) -> None:
    """Keep coalescing isolated per thread for the same sender."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    thread_a = _text_event(event_id="$m1", body="a", thread_id="$thread-a")
    thread_b = _text_event(event_id="$m2", body="b", thread_id="$thread-b")
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(_handled_turn_source_event_ids(handled_turn))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            thread_a,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            thread_b,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

    assert sorted(calls) == [["$m1"], ["$m2"]]


@pytest.mark.asyncio
async def test_room_message_and_plain_reply_to_known_thread_do_not_coalesce_together(tmp_path: Path) -> None:
    """Inherited-thread plain replies must not batch with unrelated room-level messages."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    room_message = _text_event(event_id="$roommsg", body="room message", server_timestamp=1000)
    threaded_plain_reply = _reply_event(
        event_id="$reply",
        body="bridged follow-up",
        reply_to_event_id="$thread-seed",
        server_timestamp=1001,
    )
    bot._turn_controller.deps.resolver.deps.conversation_cache.get_thread_id_for_event = AsyncMock(
        side_effect=lambda _room_id, event_id: "$thread-root" if event_id == "$thread-seed" else None,
    )
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(_handled_turn_source_event_ids(handled_turn))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            room_message,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            threaded_plain_reply,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

    assert sorted(calls) == [["$reply"], ["$roommsg"]]


@pytest.mark.asyncio
async def test_command_mid_batch_flushes_pending_then_processes_command(tmp_path: Path) -> None:
    """Flush pending messages before dispatching a command event."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first = _text_event(event_id="$m1", body="tell me more", server_timestamp=1000)
    command = _text_event(event_id="$m2", body="!help", server_timestamp=1001)
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            command,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 2)

    assert calls == [
        ("tell me more", ["$m1"]),
        ("!help", ["$m2"]),
    ]


@pytest.mark.asyncio
async def test_command_flush_does_not_leave_stale_timer_for_next_message(tmp_path: Path) -> None:
    """Drop stale debounce timers after a command-triggered flush."""
    bot = _make_bot(tmp_path, debounce_ms=40)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    command = _text_event(event_id="$m2", body="!help", server_timestamp=1001)
    second = _text_event(event_id="$m3", body="second", server_timestamp=1002)
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.01)
        await bot._turn_controller._enqueue_for_dispatch(
            command,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.005)
        await bot._turn_controller._enqueue_for_dispatch(
            second,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

        assert calls == [
            ("first", ["$m1"]),
            ("!help", ["$m2"]),
        ]

        await asyncio.sleep(0.03)

    assert calls == [
        ("first", ["$m1"]),
        ("!help", ["$m2"]),
        ("second", ["$m3"]),
    ]


@pytest.mark.asyncio
async def test_command_during_upload_grace_flushes_immediately(tmp_path: Path) -> None:
    """Commands should bypass upload grace rather than waiting for its timer."""
    bot = _make_bot(tmp_path, debounce_ms=10, upload_grace_ms=200)
    room = _make_room()
    text_event = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    command_event = _text_event(event_id="$m2", body="!help", server_timestamp=1001)
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            text_event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        # Wait for debounce to fire (10ms) so gate enters upload grace
        await asyncio.sleep(0.02)
        assert calls == []

        await bot._turn_controller._enqueue_for_dispatch(
            command_event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 2)

    assert calls == [
        ("first", ["$m1"]),
        ("!help", ["$m2"]),
    ]


@pytest.mark.asyncio
async def test_already_queued_command_barrier_flushes_normal_without_debounce() -> None:
    """A queued command barrier should flush older normal work without waiting for debounce."""
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    command = _text_event(event_id="$cmd", body="!help", server_timestamp=1001)
    calls: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        calls.append(list(batch.source_event_ids))

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 5.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", None, "@user:localhost")

    await gate.enqueue(key, PendingEvent(event=first, room=room, source_kind="message"))
    await gate.enqueue(key, PendingEvent(event=command, room=room, source_kind="message"))

    await _wait_for(lambda: calls == [["$m1"], ["$cmd"]], deadline_seconds=0.2)

    assert gate.is_idle()


@pytest.mark.asyncio
async def test_messages_during_active_response_wait_and_batch_after_completion(tmp_path: Path) -> None:
    """Hold threaded follow-ups while the first-turn response is in flight, then batch them."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second", server_timestamp=1001, thread_id="$m1")
    third = _text_event(event_id="$m3", body="third", server_timestamp=1002, thread_id="$m1")
    entered_first_dispatch = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(_handled_turn_source_event_ids(handled_turn))
        if _handled_turn_source_event_ids(handled_turn) == ["$m1"]:
            entered_first_dispatch.set()
            await release_first_dispatch.wait()

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)
        await entered_first_dispatch.wait()

        await bot._turn_controller._enqueue_for_dispatch(
            second,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            third,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

        assert calls == [["$m1"]]

        release_first_dispatch.set()
        await asyncio.sleep(0.05)

    assert calls == [["$m1"], ["$m2", "$m3"]]


@pytest.mark.asyncio
async def test_in_flight_command_barrier_flushes_buffered_normal_without_debounce() -> None:
    """A command queued during active dispatch should wake and flush older buffered work promptly."""
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    before_command = _text_event(event_id="$m2", body="before command", server_timestamp=1001)
    command = _text_event(event_id="$cmd", body="!help", server_timestamp=1002)
    entered_first_dispatch = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    debounce_seconds = 0.0
    calls: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        calls.append(source_event_ids)
        if source_event_ids == ["$m1"]:
            entered_first_dispatch.set()
            await release_first_dispatch.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: debounce_seconds,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", None, "@user:localhost")

    await gate.enqueue(key, PendingEvent(event=first, room=room, source_kind="message"))
    await entered_first_dispatch.wait()
    debounce_seconds = 5.0
    await gate.enqueue(key, PendingEvent(event=before_command, room=room, source_kind="message"))
    await gate.enqueue(key, PendingEvent(event=command, room=room, source_kind="message"))
    await asyncio.sleep(0.05)

    assert calls == [["$m1"]]

    release_first_dispatch.set()
    await _wait_for(lambda: calls == [["$m1"], ["$m2"], ["$cmd"]], deadline_seconds=0.2)

    assert gate.is_idle()


@pytest.mark.asyncio
async def test_command_during_active_dispatch_preserves_fifo_order() -> None:
    """A command should not pull later in-flight-buffered messages ahead of itself."""
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    before_command = _text_event(event_id="$m2", body="before command", server_timestamp=1001)
    command = _text_event(event_id="$cmd", body="!help", server_timestamp=1002)
    after_command = _text_event(event_id="$m3", body="after command", server_timestamp=1003)
    entered_first_dispatch = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    calls: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        calls.append(source_event_ids)
        if source_event_ids == ["$m1"]:
            entered_first_dispatch.set()
            await release_first_dispatch.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", None, "@user:localhost")

    await gate.enqueue(key, PendingEvent(event=first, room=room, source_kind="message"))
    await entered_first_dispatch.wait()
    await gate.enqueue(key, PendingEvent(event=before_command, room=room, source_kind="message"))
    await gate.enqueue(key, PendingEvent(event=command, room=room, source_kind="message"))
    await gate.enqueue(key, PendingEvent(event=after_command, room=room, source_kind="message"))

    release_first_dispatch.set()
    await _wait_for(lambda: calls == [["$m1"], ["$m2"], ["$cmd"], ["$m3"]])

    assert gate.is_idle()


@pytest.mark.asyncio
async def test_enqueue_for_dispatch_returns_while_drain_dispatch_blocks(tmp_path: Path) -> None:
    """A blocked coalescing drain must not hold later Matrix ingress callbacks."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second", server_timestamp=1001, thread_id="$m1")
    entered_first_dispatch = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events
        source_event_ids = _handled_turn_source_event_ids(handled_turn)
        calls.append(source_event_ids)
        if source_event_ids == ["$m1"]:
            entered_first_dispatch.set()
            await release_first_dispatch.wait()

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await asyncio.wait_for(
            bot._turn_controller._enqueue_for_dispatch(
                first,
                room,
                source_kind="message",
                requester_user_id="@user:localhost",
            ),
            timeout=0.05,
        )
        await entered_first_dispatch.wait()

        await asyncio.wait_for(
            bot._turn_controller._enqueue_for_dispatch(
                second,
                room,
                source_kind="message",
                requester_user_id="@user:localhost",
            ),
            timeout=0.05,
        )

        retargeted_key = ("!room:localhost", "$m1", "@user:localhost")
        assert [
            queued.pending_event.event.event_id for queued in bot._coalescing_gate._gates[retargeted_key].queue
        ] == ["$m2"]

        release_first_dispatch.set()
        await _wait_for(lambda: calls == [["$m1"], ["$m2"]])

    assert bot._coalescing_gate.is_idle()


def test_automation_source_kinds_are_coalescing_exempt() -> None:
    """Dispatch automation source kinds as FIFO barriers."""
    scheduled = _text_event(event_id="$scheduled", body="scheduled", source_kind="scheduled")
    hook = _text_event(event_id="$hook", body="hook", source_kind="hook")
    hook_dispatch = _text_event(event_id="$hook_dispatch", body="hook dispatch", source_kind="hook_dispatch")

    assert is_coalescing_exempt_source_kind(scheduled, "scheduled") is True
    assert is_coalescing_exempt_source_kind(hook, "hook") is True
    assert is_coalescing_exempt_source_kind(hook_dispatch, "hook_dispatch") is True


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["hook", "hook_dispatch"])
async def test_coalescing_exempt_source_kinds_bypass_gate(tmp_path: Path, source_kind: str) -> None:
    """Bypass the gate only for hook-originated synthetic events."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    event = _text_event(event_id=f"${source_kind}", body=f"{source_kind} task", source_kind=source_kind)
    calls: list[str] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(dispatched_event.body)

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind=source_kind,
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 1)

    assert calls == [f"{source_kind} task"]


@pytest.mark.asyncio
async def test_pending_dispatch_policy_controls_prepared_bypass_without_erasing_modality() -> None:
    """The queue-owned policy should classify prepared events inside the gate."""
    room = _make_room()
    event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice_followup",
        body="voice follow-up",
        source={"content": {"body": "voice follow-up", "com.mindroom.source_kind": "voice"}},
        server_timestamp=1000,
        source_kind_override="voice",
    )
    calls: list[CoalescedBatch] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        calls.append(batch)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 60.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    await gate.enqueue(
        ("!room:localhost", "$thread", "@user:localhost"),
        PendingEvent(
            event=event,
            room=room,
            source_kind="voice",
            dispatch_policy_source_kind=COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
        ),
    )
    await _wait_for(lambda: len(calls) == 1)

    assert calls[0].source_kind == "voice"
    assert calls[0].dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP
    dispatch_event = build_batch_dispatch_event(calls[0])
    assert isinstance(dispatch_event, PreparedTextEvent)
    assert dispatch_event.source_kind_override == "voice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "spoofed_source_kind",
    [COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP, "hook", "hook_dispatch", "voice"],
)
async def test_untrusted_source_kind_content_does_not_bypass_or_promote(
    tmp_path: Path,
    spoofed_source_kind: str,
) -> None:
    """User-controlled source_kind content should not become trusted dispatch policy."""
    bot = _make_bot(tmp_path, debounce_ms=1000)
    room = _make_room()
    event = _text_event(
        event_id=f"$spoof_{spoofed_source_kind}",
        body="normal user message",
        source_kind=spoofed_source_kind,
    )
    calls: list[nio.RoomMessageText | PreparedTextEvent] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText | PreparedTextEvent,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        queued_notice_reservation: object | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn, queued_notice_reservation
        calls.append(dispatched_event)

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.01)
        assert calls == []

        await bot.prepare_for_sync_shutdown()

    assert len(calls) == 1
    assert isinstance(calls[0], nio.RoomMessageText)
    assert not isinstance(calls[0], PreparedTextEvent)
    assert calls[0].body == "normal user message"


@pytest.mark.asyncio
async def test_bypass_preserves_fifo_order_behind_existing_normal_work() -> None:
    """Hook-originated bypass events dispatch solo without jumping ahead of queued user work."""
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    hook = _text_event(event_id="$hook", body="hook", server_timestamp=1001, source_kind="hook")
    second = _text_event(event_id="$m2", body="second", server_timestamp=1002)
    calls: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        calls.append(list(batch.source_event_ids))

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.02,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    key = ("!room:localhost", None, "@user:localhost")

    await gate.enqueue(key, PendingEvent(event=first, room=room, source_kind="message"))
    await gate.enqueue(key, PendingEvent(event=hook, room=room, source_kind="hook"))
    await gate.enqueue(key, PendingEvent(event=second, room=room, source_kind="message"))

    await _wait_for(lambda: calls == [["$m1"], ["$hook"], ["$m2"]])

    assert gate.is_idle()


@pytest.mark.asyncio
async def test_overlapping_scheduled_checkins_coalesce(tmp_path: Path) -> None:
    """Scheduled turns should buffer behind an in-flight dispatch instead of bypassing the gate."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first = _text_event(
        event_id="$m1",
        body="first scheduled",
        sender="@mindroom_test_agent:localhost",
        server_timestamp=1000,
        thread_id="$thread_root",
        source_kind="scheduled",
        original_sender="@user:localhost",
    )
    second = _text_event(
        event_id="$m2",
        body="second scheduled",
        sender="@mindroom_test_agent:localhost",
        server_timestamp=1001,
        thread_id="$thread_root",
        source_kind="scheduled",
        original_sender="@user:localhost",
    )
    entered_first_dispatch = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(_handled_turn_source_event_ids(handled_turn))
        if _handled_turn_source_event_ids(handled_turn) == ["$m1"]:
            entered_first_dispatch.set()
            await release_first_dispatch.wait()

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="scheduled",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)
        await entered_first_dispatch.wait()

        await bot._turn_controller._enqueue_for_dispatch(
            second,
            room,
            source_kind="scheduled",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

        assert calls == [["$m1"]]

        release_first_dispatch.set()
        await _wait_for(lambda: calls == [["$m1"], ["$m2"]])

    assert calls == [["$m1"], ["$m2"]]


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_waits_for_active_flush_task(tmp_path: Path) -> None:
    """Wait for an active flush task before finishing sync shutdown."""
    bot = _make_bot(tmp_path)
    bot.client.next_batch = "s_test_token"
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")
    entered_dispatch = asyncio.Event()
    release_dispatch = asyncio.Event()
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(_handled_turn_source_event_ids(handled_turn))
        entered_dispatch.set()
        await release_dispatch.wait()

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)
        await entered_dispatch.wait()

        shutdown_task = asyncio.create_task(bot.prepare_for_sync_shutdown())
        await asyncio.sleep(0.01)
        assert shutdown_task.done() is False

        release_dispatch.set()
        await shutdown_task

    assert calls == [["$m1"]]
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_drains_pending_debounced_messages(tmp_path: Path) -> None:
    """Flush any queued debounced messages during sync shutdown."""
    bot = _make_bot(tmp_path, debounce_ms=1000)
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot.prepare_for_sync_shutdown()

    assert calls == [("hello", ["$m1"])]
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_drains_pending_upload_grace(tmp_path: Path) -> None:
    """Flush a text-only batch immediately when shutdown interrupts upload grace."""
    bot = _make_bot(tmp_path, debounce_ms=10, upload_grace_ms=200)
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        # Wait for debounce to fire (10ms) so gate enters upload grace
        await asyncio.sleep(0.02)
        assert calls == []

        await bot.prepare_for_sync_shutdown()

    assert calls == [("hello", ["$m1"])]
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_shutdown_during_in_flight_dispatch_does_not_start_grace(tmp_path: Path) -> None:
    """Shutdown during an in-flight dispatch should not trigger upload grace."""
    bot = _make_bot(tmp_path, debounce_ms=10, upload_grace_ms=200)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second", server_timestamp=1001)
    entered_dispatch = asyncio.Event()
    release_dispatch = asyncio.Event()
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))
        if _handled_turn_source_event_ids(handled_turn) == ["$m1"]:
            entered_dispatch.set()
            await release_dispatch.wait()

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)
        await entered_dispatch.wait()

        # Enqueue another message while first is in-flight
        await bot._turn_controller._enqueue_for_dispatch(
            second,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )

        # Start shutdown — should wait for in-flight, then flush remaining without grace
        shutdown_task = asyncio.create_task(bot.prepare_for_sync_shutdown())
        await asyncio.sleep(0.01)
        assert shutdown_task.done() is False

        release_dispatch.set()
        await shutdown_task

    assert calls == [("first", ["$m1"]), ("second", ["$m2"])]
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_first_turn_thread_resolution_retargets_in_flight_gate(tmp_path: Path) -> None:
    """Retarget the first-turn room gate so threaded follow-ups reuse the live gate."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first")
    followups = [
        _text_event(event_id="$m2", body="second", thread_id="$m1"),
        _text_event(event_id="$m3", body="third", thread_id="$m1"),
    ]
    entered_dispatch = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(_handled_turn_source_event_ids(handled_turn))
        if len(calls) == 1:
            entered_dispatch.set()
            await release_first_dispatch.wait()

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        first_task = asyncio.create_task(
            bot._turn_controller._enqueue_for_dispatch(
                first,
                room,
                source_kind="message",
                requester_user_id="@user:localhost",
            ),
        )
        await entered_dispatch.wait()

        retargeted_key = ("!room:localhost", "$m1", "@user:localhost")
        assert set(bot._coalescing_gate._gates) == {retargeted_key}
        assert bot._coalescing_gate._gates[retargeted_key].phase is GatePhase.IN_FLIGHT

        for followup in followups:
            await bot._turn_controller._enqueue_for_dispatch(
                followup,
                room,
                source_kind="message",
                requester_user_id="@user:localhost",
            )

        gate = bot._coalescing_gate._gates[retargeted_key]
        assert gate.phase is GatePhase.IN_FLIGHT
        assert [queued.pending_event.event.event_id for queued in gate.queue] == ["$m2", "$m3"]

        release_first_dispatch.set()
        await first_task
        await _wait_for(lambda: calls == [["$m1"], ["$m2", "$m3"]])

    assert calls == [["$m1"], ["$m2", "$m3"]]
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_retargeted_sleeping_timer_flushes_under_current_key() -> None:
    """A timer scheduled before retarget must still flush the gate after re-keying."""
    room = _make_room()
    dispatched_source_event_ids: list[list[str]] = []
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(
            side_effect=lambda batch: dispatched_source_event_ids.append(batch.source_event_ids),
        ),
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    old_key = ("!room:localhost", None, "@user:localhost")
    new_key = ("!room:localhost", "$m1", "@user:localhost")

    await gate.enqueue(
        old_key,
        PendingEvent(
            event=_text_event(event_id="$m1", body="first"),
            room=room,
            source_kind="message",
        ),
    )
    gate.retarget(old_key, new_key)

    await _wait_for(lambda: len(dispatched_source_event_ids) == 1)

    assert dispatched_source_event_ids == [["$m1"]]
    assert gate.is_idle()


@pytest.mark.asyncio
async def test_retarget_merges_existing_destination_gate_queue() -> None:
    """A pre-existing canonical gate should merge into the retargeted drain owner."""
    room = _make_room()
    dispatched_source_event_ids: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        dispatched_source_event_ids.append(list(batch.source_event_ids))

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 5.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    old_key = ("!room:localhost", None, "@user:localhost")
    new_key = ("!room:localhost", "$m1", "@user:localhost")

    await gate.enqueue(
        old_key,
        PendingEvent(
            event=_text_event(event_id="$m1", body="first"),
            room=room,
            source_kind="message",
        ),
    )
    await gate.enqueue(
        new_key,
        PendingEvent(
            event=_text_event(event_id="$m2", body="follow-up", thread_id="$m1"),
            room=room,
            source_kind="message",
        ),
    )

    gate.retarget(old_key, new_key)

    assert set(gate._gates) == {new_key}
    assert [queued.pending_event.event.event_id for queued in gate._gates[new_key].queue] == ["$m1", "$m2"]

    await gate.drain_all()

    assert dispatched_source_event_ids == [["$m1", "$m2"]]
    assert gate.is_idle()


@pytest.mark.asyncio
async def test_retarget_preserves_in_flight_destination_dispatch() -> None:
    """Retargeting into an active canonical gate must not cancel claimed destination work."""
    room = _make_room()
    destination_dispatch_started = asyncio.Event()
    release_destination_dispatch = asyncio.Event()
    dispatched_source_event_ids: list[list[str]] = []
    cancelled_source_event_ids: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        if source_event_ids == ["$m2"]:
            destination_dispatch_started.set()
            try:
                await release_destination_dispatch.wait()
            except asyncio.CancelledError:
                cancelled_source_event_ids.append(source_event_ids)
                raise
        dispatched_source_event_ids.append(source_event_ids)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    old_key = ("!room:localhost", None, "@user:localhost")
    new_key = ("!room:localhost", "$m1", "@user:localhost")

    await gate.enqueue(
        new_key,
        PendingEvent(
            event=_text_event(event_id="$m2", body="follow-up", thread_id="$m1"),
            room=room,
            source_kind="message",
        ),
    )
    await destination_dispatch_started.wait()
    await gate.enqueue(
        old_key,
        PendingEvent(
            event=_text_event(event_id="$m1", body="first"),
            room=room,
            source_kind="message",
        ),
    )

    gate.retarget(old_key, new_key)
    await asyncio.sleep(0)

    assert cancelled_source_event_ids == []
    assert set(gate._gates) == {new_key}
    release_destination_dispatch.set()
    await gate.drain_all()

    assert cancelled_source_event_ids == []
    assert dispatched_source_event_ids == [["$m2"], ["$m1"]]
    assert gate.is_idle()


@pytest.mark.asyncio
async def test_retarget_preserves_both_in_flight_dispatches() -> None:
    """Retargeting two active gates must not cancel either already-claimed batch."""
    room = _make_room()
    dispatch_started = {"$m1": asyncio.Event(), "$m2": asyncio.Event()}
    release_dispatch = {"$m1": asyncio.Event(), "$m2": asyncio.Event()}
    dispatched_source_event_ids: list[list[str]] = []
    cancelled_source_event_ids: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        event_id = source_event_ids[0]
        dispatch_started[event_id].set()
        try:
            await release_dispatch[event_id].wait()
        except asyncio.CancelledError:
            cancelled_source_event_ids.append(source_event_ids)
            raise
        dispatched_source_event_ids.append(source_event_ids)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    old_key = ("!room:localhost", None, "@user:localhost")
    new_key = ("!room:localhost", "$m1", "@user:localhost")

    await gate.enqueue(
        old_key,
        PendingEvent(
            event=_text_event(event_id="$m1", body="first"),
            room=room,
            source_kind="message",
        ),
    )
    await dispatch_started["$m1"].wait()
    await gate.enqueue(
        new_key,
        PendingEvent(
            event=_text_event(event_id="$m2", body="follow-up", thread_id="$m1"),
            room=room,
            source_kind="message",
        ),
    )
    await dispatch_started["$m2"].wait()

    gate.retarget(old_key, new_key)
    await asyncio.sleep(0)

    assert cancelled_source_event_ids == []
    assert set(gate._gates) == {new_key}
    release_dispatch["$m1"].set()
    release_dispatch["$m2"].set()
    await gate.drain_all()

    assert cancelled_source_event_ids == []
    assert sorted(dispatched_source_event_ids) == [["$m1"], ["$m2"]]
    assert gate.is_idle()


@pytest.mark.asyncio
async def test_retarget_tracks_retired_in_flight_source_until_dispatch_finishes() -> None:
    """Retired in-flight source drains must remain visible to shutdown and idle checks."""
    room = _make_room()
    dispatch_started = {"$m1": asyncio.Event(), "$m2": asyncio.Event()}
    release_dispatch = {"$m1": asyncio.Event(), "$m2": asyncio.Event()}
    dispatched_source_event_ids: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        event_id = source_event_ids[0]
        dispatch_started[event_id].set()
        await release_dispatch[event_id].wait()
        dispatched_source_event_ids.append(source_event_ids)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    old_key = ("!room:localhost", None, "@user:localhost")
    new_key = ("!room:localhost", "$m1", "@user:localhost")

    await gate.enqueue(
        old_key,
        PendingEvent(
            event=_text_event(event_id="$m1", body="first"),
            room=room,
            source_kind="message",
        ),
    )
    await dispatch_started["$m1"].wait()
    await gate.enqueue(
        new_key,
        PendingEvent(
            event=_text_event(event_id="$m2", body="follow-up", thread_id="$m1"),
            room=room,
            source_kind="message",
        ),
    )
    await dispatch_started["$m2"].wait()

    gate.retarget(old_key, new_key)
    release_dispatch["$m2"].set()
    await _wait_for(lambda: dispatched_source_event_ids == [["$m2"]])

    assert gate.is_idle() is False
    drain_task = asyncio.create_task(gate.drain_all())
    await asyncio.sleep(0.01)
    assert drain_task.done() is False

    release_dispatch["$m1"].set()
    await drain_task

    assert sorted(dispatched_source_event_ids) == [["$m1"], ["$m2"]]
    assert gate.is_idle()


@pytest.mark.asyncio
async def test_zero_debounce_immediate_flush_logs_pending_count_before_clearing() -> None:
    """Immediate-flush telemetry should report the batch size before _flush clears pending."""
    room = _make_room()
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    with patch("mindroom.coalescing.emit_elapsed_timing") as mock_emit:
        await gate.enqueue(
            ("!room:localhost", None, "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m1", body="first"),
                room=room,
                source_kind="message",
            ),
        )

    immediate_flush_calls = [
        call
        for call in mock_emit.call_args_list
        if call.args and call.args[0] == "coalescing_gate.enqueue" and call.kwargs.get("path") == "zero_debounce"
    ]
    assert len(immediate_flush_calls) == 1
    assert immediate_flush_calls[0].kwargs["pending_count"] == 1
    assert immediate_flush_calls[0].kwargs["flush_outcome"] == "scheduled_drain"


@pytest.mark.asyncio
async def test_zero_debounce_with_upload_grace_logs_scheduled_grace_outcome() -> None:
    """Zero debounce should not claim an immediate flush when upload grace delays dispatch."""
    room = _make_room()
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.1,
        is_shutting_down=lambda: False,
    )

    with patch("mindroom.coalescing.emit_elapsed_timing") as mock_emit:
        await gate.enqueue(
            ("!room:localhost", None, "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m1", body="first"),
                room=room,
                source_kind="message",
            ),
        )

    zero_debounce_calls = [
        call
        for call in mock_emit.call_args_list
        if call.args and call.args[0] == "coalescing_gate.enqueue" and call.kwargs.get("path") == "zero_debounce"
    ]
    assert len(zero_debounce_calls) == 1
    assert zero_debounce_calls[0].kwargs["flush_outcome"] == "scheduled_drain"


@pytest.mark.asyncio
async def test_enqueue_for_dispatch_timing_events_include_explicit_scope(tmp_path: Path) -> None:
    """Pre-dispatch handoff telemetry should carry the source-event timing scope explicitly."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")

    with (
        patch.object(bot._coalescing_gate, "enqueue", new=AsyncMock()),
        patch("mindroom.turn_controller.emit_elapsed_timing") as mock_emit,
    ):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
            coalescing_key=(room.room_id, None, "@user:localhost"),
        )

    handoff_calls = [
        call
        for call in mock_emit.call_args_list
        if call.args
        and isinstance(call.args[0], str)
        and call.args[0].startswith("ingress_handoff.enqueue_for_dispatch")
    ]
    assert handoff_calls
    assert all(call.kwargs["timing_scope"] == "$m1" for call in handoff_calls)


@pytest.mark.asyncio
async def test_matrix_ingress_logging_includes_receive_lag(tmp_path: Path) -> None:
    """Matrix callback logs should include receive lag when origin_server_ts is present."""
    bot = _make_bot(tmp_path)
    bot.logger = MagicMock()
    bot._turn_controller.handle_text_event = AsyncMock()
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello", server_timestamp=1000, thread_id="$thread")

    with patch("mindroom.bot.time.time", return_value=2.5):
        await bot._on_message(room, event)

    bot.logger.info.assert_any_call(
        "matrix_event_callback_started",
        callback="message",
        event_id="$m1",
        room_id="!room:localhost",
        agent_name="test_agent",
        receive_timestamp_ms=2500,
        origin_server_ts_ms=1000,
        matrix_event_receive_lag_ms=1500.0,
    )
    bot._turn_controller.handle_text_event.assert_awaited_once_with(room, event)


@pytest.mark.asyncio
async def test_matrix_ingress_logging_handles_missing_origin_timestamp(tmp_path: Path) -> None:
    """Matrix callback logs should tolerate events without origin_server_ts."""
    bot = _make_bot(tmp_path)
    bot.logger = MagicMock()
    bot._turn_controller.handle_text_event = AsyncMock()
    room = _make_room()
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = "$m1"
    event.sender = "@user:localhost"
    event.source = {"content": {"msgtype": "m.text", "body": "hello"}}

    with patch("mindroom.bot.time.time", return_value=2.5):
        await bot._on_message(room, event)

    log_call = next(call for call in bot.logger.info.call_args_list if call.args == ("matrix_event_callback_started",))
    assert log_call.kwargs == {
        "callback": "message",
        "event_id": "$m1",
        "room_id": "!room:localhost",
        "agent_name": "test_agent",
        "receive_timestamp_ms": 2500,
    }
    bot._turn_controller.handle_text_event.assert_awaited_once_with(room, event)


@pytest.mark.asyncio
async def test_handle_coalesced_batch_timing_events_include_dispatch_scope(tmp_path: Path) -> None:
    """Coalesced-batch telemetry emitted before dispatch should carry the batch event scope."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    batch = build_coalesced_batch(
        (room.room_id, None, "@user:localhost"),
        [
            PendingEvent(
                event=_text_event(event_id="$m1", body="hello"),
                room=room,
                source_kind="message",
            ),
        ],
    )

    with (
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()),
        patch("mindroom.turn_controller.emit_elapsed_timing") as mock_emit,
    ):
        await bot._turn_controller.handle_coalesced_batch(batch)

    batch_calls = [
        call
        for call in mock_emit.call_args_list
        if call.args and isinstance(call.args[0], str) and call.args[0].startswith("coalescing.handle_batch.")
    ]
    assert batch_calls
    assert all(call.kwargs["timing_scope"] == "$m1" for call in batch_calls)


@pytest.mark.asyncio
async def test_register_batch_media_attachments_emits_payload_timing_for_empty_batch(tmp_path: Path) -> None:
    """Batch media registration timing should report empty payload batches."""
    bot = _make_bot(tmp_path)

    with patch("mindroom.inbound_turn_normalizer.emit_elapsed_timing") as mock_emit:
        result = await bot._inbound_turn_normalizer.register_batch_media_attachments(
            BatchMediaAttachmentRequest(
                room_id="!room:localhost",
                thread_id="$thread",
                media_events=[],
            ),
        )

    assert result.attachment_ids == []
    assert result.fallback_images is None
    mock_emit.assert_called_once()
    assert mock_emit.call_args.args[0] == "response_payload.register_batch_media_attachments"
    assert isinstance(mock_emit.call_args.args[1], float)
    assert mock_emit.call_args.kwargs == {
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "outcome": "success",
        "media_event_count": 0,
        "image_event_count": 0,
        "file_or_video_event_count": 0,
        "attachment_count": 0,
        "fallback_image_count": 0,
    }


@pytest.mark.asyncio
async def test_register_batch_media_attachments_emits_payload_timing_on_failure(tmp_path: Path) -> None:
    """Batch media registration timing should still emit when attachment hydration fails."""
    bot = _make_bot(tmp_path)

    with (
        patch("mindroom.inbound_turn_normalizer.download_image", new=AsyncMock(return_value=None)),
        patch("mindroom.inbound_turn_normalizer.emit_elapsed_timing") as mock_emit,
        pytest.raises(RuntimeError, match="Failed to download image"),
    ):
        await bot._inbound_turn_normalizer.register_batch_media_attachments(
            BatchMediaAttachmentRequest(
                room_id="!room:localhost",
                thread_id="$thread",
                media_events=[_image_event(event_id="$img1")],
            ),
        )

    mock_emit.assert_called_once()
    assert mock_emit.call_args.args[0] == "response_payload.register_batch_media_attachments"
    assert isinstance(mock_emit.call_args.args[1], float)
    assert mock_emit.call_args.kwargs == {
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "outcome": "failed",
        "media_event_count": 1,
        "image_event_count": 1,
        "file_or_video_event_count": 0,
        "attachment_count": 0,
        "fallback_image_count": 0,
    }


@pytest.mark.asyncio
async def test_flush_logs_failed_outcome_when_dispatch_batch_raises() -> None:
    """Flush telemetry should not report success when dispatch_batch raises."""
    room = _make_room()

    async def failing_dispatch_batch(_batch: object) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    gate = CoalescingGate(
        dispatch_batch=failing_dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    with patch("mindroom.coalescing.emit_elapsed_timing") as mock_emit:
        await gate.enqueue(
            ("!room:localhost", None, "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m1", body="first"),
                room=room,
                source_kind="message",
            ),
        )
        await _wait_for(gate.is_idle)

    flush_calls = [call for call in mock_emit.call_args_list if call.args and call.args[0] == "coalescing_gate.flush"]
    assert len(flush_calls) == 1
    assert flush_calls[0].kwargs["outcome"] == "failed"


@pytest.mark.asyncio
async def test_coalescing_enqueue_logs_pending_count() -> None:
    """Coalescing enqueue diagnostics should identify the pending scope."""
    room = _make_room()
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 10.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    with patch("mindroom.coalescing.logger.info") as mock_info:
        await gate.enqueue(
            ("!room:localhost", "$thread", "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m1", body="first"),
                room=room,
                source_kind="message",
            ),
        )

    await gate.drain_all()
    enqueue_call = next(call for call in mock_info.call_args_list if call.args == ("coalescing_gate_message_enqueued",))
    assert enqueue_call.kwargs["pending_count"] == 1
    assert enqueue_call.kwargs["event_id"] == "$m1"
    assert enqueue_call.kwargs["room_id"] == "!room:localhost"
    assert enqueue_call.kwargs["thread_id"] == "$thread"


@pytest.mark.asyncio
async def test_slow_coalescing_flush_warns_with_correlation_metadata() -> None:
    """Slow flush diagnostics should carry event, room, and thread identifiers."""
    room = _make_room()
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )
    with (
        patch("mindroom.coalescing._COALESCING_FLUSH_WARNING_SECONDS", 0.0),
        patch("mindroom.coalescing.logger.warning") as mock_warning,
    ):
        await gate.enqueue(
            ("!room:localhost", "$thread", "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m1", body="first"),
                room=room,
                source_kind="message",
            ),
        )
        await _wait_for(gate.is_idle)

    mock_warning.assert_called_once()
    assert mock_warning.call_args.args == ("coalescing_gate_flush_slow",)
    assert mock_warning.call_args.kwargs["duration_ms"] >= 0.0
    assert mock_warning.call_args.kwargs["source_event_ids"] == ["$m1"]
    assert mock_warning.call_args.kwargs["room_id"] == "!room:localhost"
    assert mock_warning.call_args.kwargs["thread_id"] == "$thread"
    assert mock_warning.call_args.kwargs["outcome"] == "dispatched"


@pytest.mark.asyncio
async def test_timer_flush_logs_dispatch_failure_without_unhandled_task() -> None:
    """Timer callbacks should consume dispatch failures instead of leaking task exceptions."""
    room = _make_room()
    loop = asyncio.get_running_loop()
    loop_exceptions: list[dict[str, object]] = []
    previous_exception_handler = loop.get_exception_handler()

    async def failing_dispatch_batch(_batch: object) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    gate = CoalescingGate(
        dispatch_batch=failing_dispatch_batch,
        debounce_seconds=lambda: 0.01,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    def capture_loop_exception(_loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        loop_exceptions.append(context)

    loop.set_exception_handler(capture_loop_exception)
    try:
        with patch("mindroom.coalescing.logger.exception") as mock_exception:
            await gate.enqueue(
                ("!room:localhost", None, "@user:localhost"),
                PendingEvent(
                    event=_text_event(event_id="$m1", body="first"),
                    room=room,
                    source_kind="message",
                ),
            )
            await _wait_for(lambda: mock_exception.called)
    finally:
        loop.set_exception_handler(previous_exception_handler)

    assert loop_exceptions == []
    mock_exception.assert_called_once()
    assert mock_exception.call_args.args == ("Coalescing drain failed",)
    assert mock_exception.call_args.kwargs["exception_type"] == "RuntimeError"
    assert mock_exception.call_args.kwargs["error_message"] == "Coalesced dispatch failed."
    assert "pending_count" in mock_exception.call_args.kwargs
    assert "oldest_pending_age_ms" in mock_exception.call_args.kwargs
    assert gate.is_idle()


@pytest.mark.asyncio
async def test_failed_drain_does_not_poison_future_ingress() -> None:
    """A failed drain should log, clean up, and allow later events to dispatch."""
    room = _make_room()
    dispatched_source_event_ids: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        if source_event_ids == ["$m1"]:
            msg = "boom"
            raise RuntimeError(msg)
        dispatched_source_event_ids.append(source_event_ids)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    with patch("mindroom.coalescing.logger.exception") as mock_exception:
        await gate.enqueue(
            ("!room:localhost", None, "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m1", body="first"),
                room=room,
                source_kind="message",
            ),
        )
        await _wait_for(lambda: mock_exception.called)

    assert gate.is_idle()

    await gate.enqueue(
        ("!room:localhost", None, "@user:localhost"),
        PendingEvent(
            event=_text_event(event_id="$m2", body="second"),
            room=room,
            source_kind="message",
        ),
    )
    await _wait_for(lambda: dispatched_source_event_ids == [["$m2"]])

    assert gate.is_idle()


@pytest.mark.asyncio
async def test_failed_drain_dispatches_buffered_ingress_without_waiting_for_another_event() -> None:
    """Ingress buffered behind a failed dispatch should get its own follow-up drain."""
    room = _make_room()
    entered_first_dispatch = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    dispatched_source_event_ids: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        if source_event_ids == ["$m1"]:
            entered_first_dispatch.set()
            await release_first_dispatch.wait()
            msg = "boom"
            raise RuntimeError(msg)
        dispatched_source_event_ids.append(source_event_ids)

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    with patch("mindroom.coalescing.logger.exception") as mock_exception:
        await gate.enqueue(
            ("!room:localhost", None, "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m1", body="first"),
                room=room,
                source_kind="message",
            ),
        )
        await entered_first_dispatch.wait()
        await gate.enqueue(
            ("!room:localhost", None, "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m2", body="second"),
                room=room,
                source_kind="message",
            ),
        )

        release_first_dispatch.set()
        await _wait_for(lambda: mock_exception.called)
        await _wait_for(lambda: dispatched_source_event_ids == [["$m2"]])

    assert gate.is_idle()


@pytest.mark.asyncio
async def test_cancelled_drain_cleans_state_for_later_message() -> None:
    """A cancelled in-flight dispatch should not prevent a fresh later drain."""
    room = _make_room()
    entered_first_dispatch = asyncio.Event()
    never_release = asyncio.Event()
    dispatched_source_event_ids: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        dispatched_source_event_ids.append(source_event_ids)
        if source_event_ids == ["$m1"]:
            entered_first_dispatch.set()
            await never_release.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    await gate.enqueue(
        ("!room:localhost", None, "@user:localhost"),
        PendingEvent(
            event=_text_event(event_id="$m1", body="first"),
            room=room,
            source_kind="message",
        ),
    )
    await entered_first_dispatch.wait()

    drain_task = next(iter(gate._gates.values())).drain_task
    assert drain_task is not None
    drain_task.cancel()
    await asyncio.gather(drain_task, return_exceptions=True)

    assert gate.is_idle()

    await gate.enqueue(
        ("!room:localhost", None, "@user:localhost"),
        PendingEvent(
            event=_text_event(event_id="$m2", body="second"),
            room=room,
            source_kind="message",
        ),
    )
    await _wait_for(lambda: dispatched_source_event_ids == [["$m1"], ["$m2"]])

    assert gate.is_idle()


@pytest.mark.asyncio
async def test_cancelled_drain_dispatches_buffered_ingress_without_waiting_for_another_event() -> None:
    """Ingress buffered behind a cancelled dispatch should get its own follow-up drain."""
    room = _make_room()
    entered_first_dispatch = asyncio.Event()
    never_release = asyncio.Event()
    dispatched_source_event_ids: list[list[str]] = []

    async def dispatch_batch(batch: CoalescedBatch) -> None:
        source_event_ids = list(batch.source_event_ids)
        dispatched_source_event_ids.append(source_event_ids)
        if source_event_ids == ["$m1"]:
            entered_first_dispatch.set()
            await never_release.wait()

    gate = CoalescingGate(
        dispatch_batch=dispatch_batch,
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    await gate.enqueue(
        ("!room:localhost", None, "@user:localhost"),
        PendingEvent(
            event=_text_event(event_id="$m1", body="first"),
            room=room,
            source_kind="message",
        ),
    )
    await entered_first_dispatch.wait()
    await gate.enqueue(
        ("!room:localhost", None, "@user:localhost"),
        PendingEvent(
            event=_text_event(event_id="$m2", body="second"),
            room=room,
            source_kind="message",
        ),
    )

    drain_task = next(iter(gate._gates.values())).drain_task
    assert drain_task is not None
    drain_task.cancel()
    await asyncio.gather(drain_task, return_exceptions=True)
    await _wait_for(lambda: dispatched_source_event_ids == [["$m1"], ["$m2"]])

    assert gate.is_idle()


@pytest.mark.asyncio
async def test_coalescing_drain_logs_lifecycle_metadata() -> None:
    """Drain diagnostics should include enqueue, start, finish, count, and age fields."""
    room = _make_room()
    gate = CoalescingGate(
        dispatch_batch=AsyncMock(),
        debounce_seconds=lambda: 0.0,
        upload_grace_seconds=lambda: 0.0,
        is_shutting_down=lambda: False,
    )

    with patch("mindroom.coalescing.logger.debug") as mock_debug:
        await gate.enqueue(
            ("!room:localhost", None, "@user:localhost"),
            PendingEvent(
                event=_text_event(event_id="$m1", body="first"),
                room=room,
                source_kind="message",
            ),
        )
        await _wait_for(gate.is_idle)

    debug_events = [call.args[0] for call in mock_debug.call_args_list if call.args]
    assert "coalescing_gate_enqueue" in debug_events
    assert "coalescing_drain_start" in debug_events
    assert "coalescing_drain_finish" in debug_events
    for event_name in ("coalescing_gate_enqueue", "coalescing_drain_start", "coalescing_drain_finish"):
        event_call = next(call for call in mock_debug.call_args_list if call.args and call.args[0] == event_name)
        assert "pending_count" in event_call.kwargs
        assert "oldest_pending_age_ms" in event_call.kwargs
    enqueue_call = next(
        call for call in mock_debug.call_args_list if call.args and call.args[0] == "coalescing_gate_enqueue"
    )
    assert "duration_ms" in enqueue_call.kwargs


@pytest.mark.asyncio
async def test_cleanup_drains_pending_debounce_tasks(tmp_path: Path) -> None:
    """Drain pending debounce tasks when a bot is cleaned up."""
    bot = _make_bot(tmp_path, debounce_ms=1000)
    bot.client = AsyncMock()
    bot._emit_agent_lifecycle_event = AsyncMock()
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")

    with (
        patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
        patch("mindroom.bot.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock()),
    ):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        assert not bot._coalescing_gate.is_idle()

        await bot.cleanup()

    mock_dispatch.assert_awaited_once()
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_upload_grace_hard_cap_prevents_indefinite_extension(tmp_path: Path) -> None:
    """Media arrivals may extend grace, but never past the gate hard cap."""
    # Use generous grace (100ms) so images arrive well before the grace timer fires.
    # Hard cap = max(0.1, min(0.4, 2.0)) = 0.4s.
    bot = _make_bot(tmp_path, debounce_ms=10, upload_grace_ms=100)
    room = _make_room()
    text_event = _text_event(event_id="$m1", body="describe", server_timestamp=1000)
    image_events = [
        _image_event(event_id="$img1", server_timestamp=1001),
        _image_event(event_id="$img2", server_timestamp=1002),
        _image_event(event_id="$img3", server_timestamp=1003),
        _image_event(event_id="$img4", server_timestamp=1004),
    ]
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append(_handled_turn_source_event_ids(handled_turn))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        started_at = time.monotonic()
        await bot._turn_controller._enqueue_for_dispatch(
            text_event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        # Wait for debounce to fire (10ms) so gate enters upload grace
        await asyncio.sleep(0.02)
        assert calls == []

        for delay, image_event in zip((0.01, 0.01, 0.01, 0.01), image_events, strict=True):
            await asyncio.sleep(delay)
            await bot._turn_controller._enqueue_for_dispatch(
                image_event,
                room,
                source_kind="image",
                requester_user_id="@user:localhost",
            )
        await _wait_for(lambda: len(calls) == 1, deadline_seconds=0.5)

    assert calls == [["$m1", "$img1", "$img2", "$img3", "$img4"]]
    # Hard cap bounds total time: dispatch must complete well within 500ms.
    assert time.monotonic() - started_at < 0.5


@pytest.mark.asyncio
async def test_turn_store_marks_all_batch_event_ids(tmp_path: Path) -> None:
    """Mark every source event ID from a coalesced batch as responded."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second", server_timestamp=1001)
    dispatch = _prepared_dispatch(event_id="$m2")
    send_response = AsyncMock(return_value="$placeholder")
    generate_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, send_response)
    install_generate_response_mock(bot, generate_response)

    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(return_value=_respond_dispatch_plan())),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="combined")),
        ),
        patch.object(bot._conversation_resolver, "hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot._turn_controller, "_log_dispatch_latency"),
    ):
        await bot._turn_controller._enqueue_for_dispatch(
            first,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            second,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.05)

    assert bot._turn_store.is_handled("$m1")
    assert bot._turn_store.is_handled("$m2")
    turn_record = bot._turn_store.get_turn_record("$m1")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"
    assert turn_record.source_event_ids == ("$m1", "$m2")
    assert turn_record.anchor_event_id == "$m2"


@pytest.mark.asyncio
async def test_zero_debounce_dispatches_immediately(tmp_path: Path) -> None:
    """A zero debounce window should dispatch each message without delay."""
    bot = _make_bot(tmp_path, debounce_ms=0, upload_grace_ms=0)
    room = _make_room()
    event = _text_event(event_id="$m1", body="immediate")
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 1)

    assert calls == [("immediate", ["$m1"])]
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_multiple_commands_each_dispatch_independently(tmp_path: Path) -> None:
    """Each command should dispatch as its own solo batch even when sent rapidly."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first_cmd = _text_event(event_id="$c1", body="!help", server_timestamp=1000)
    second_cmd = _text_event(event_id="$c2", body="!schedule list", server_timestamp=1001)
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        handled_turn: HandledTurnState | None = None,
        **_metadata: object,
    ) -> None:
        _ = media_events, handled_turn
        calls.append((dispatched_event.body, _handled_turn_source_event_ids(handled_turn)))

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._turn_controller._enqueue_for_dispatch(
            first_cmd,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await bot._turn_controller._enqueue_for_dispatch(
            second_cmd,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await _wait_for(lambda: len(calls) == 2)

    assert calls == [
        ("!help", ["$c1"]),
        ("!schedule list", ["$c2"]),
    ]


@pytest.mark.asyncio
async def test_gate_entry_removed_after_dispatch_with_no_pending(tmp_path: Path) -> None:
    """A gate entry should be cleaned up once dispatch completes with no new pending."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")

    with patch.object(bot._turn_controller, "_dispatch_text_message", new=AsyncMock()):
        assert bot._coalescing_gate.is_idle()
        await bot._turn_controller._enqueue_for_dispatch(
            event,
            room,
            source_kind="message",
            requester_user_id="@user:localhost",
        )
        await asyncio.sleep(0.03)

    assert bot._coalescing_gate.is_idle()


# ---------------------------------------------------------------------------
# BLOCKER 1 regression: thread-history guard for backlog/replay scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backlog_replay_skips_older_message_when_newer_exists(tmp_path: Path) -> None:
    """Skip an older message during backlog replay when a newer unresponded message exists."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    older_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$m1",
        body="old",
        source={"content": {"msgtype": "m.text", "body": "old"}},
        server_timestamp=1000,
    )
    dispatch = _prepared_dispatch(event_id="$m1", body="old")

    # Thread history already contains a newer unresponded message from the same sender
    newer_msg = ResolvedVisibleMessage(
        sender="@user:localhost",
        body="newer",
        timestamp=2000,
        event_id="$m2",
        content={"body": "newer"},
        thread_id=None,
        latest_event_id="$m2",
    )
    _set_context_histories(dispatch, [newer_msg])

    action_mock = AsyncMock()
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
    ):
        await bot._turn_controller._dispatch_text_message(room, older_event, "@user:localhost")

    # Older message should be skipped — resolve_dispatch_action never called
    action_mock.assert_not_awaited()
    assert bot._turn_store.is_handled("$m1")


@pytest.mark.asyncio
async def test_media_dispatch_uses_replay_snapshot_instead_of_mutated_planning_history(tmp_path: Path) -> None:
    """Media-backed turns must use replay snapshot history instead of mutable planning history."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    image_event = _image_event(event_id="$img1", server_timestamp=1000)
    dispatch_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$img1",
        body="[Attached image]",
        source={"content": {"msgtype": "m.text", "body": "[Attached image]"}},
        server_timestamp=1000,
    )
    dispatch = _prepared_dispatch(event_id="$img1", body="[Attached image]")
    hydrated_msg = ResolvedVisibleMessage(
        sender="@user:localhost",
        body="hydrated newer message",
        timestamp=2000,
        event_id="$img2",
        content={"body": "hydrated newer message"},
        thread_id=None,
        latest_event_id="$img2",
    )

    action_mock = AsyncMock(return_value=DispatchPlan(kind="ignore"))
    dispatch.context.thread_history = [hydrated_msg]
    dispatch.context.replay_guard_history = []

    newer_mock = MagicMock(return_value=False)
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
        patch.object(bot._turn_controller, "_has_newer_unresponded_in_thread", new=newer_mock),
        patch.object(bot._turn_controller, "_log_dispatch_latency"),
    ):
        await bot._turn_controller._dispatch_text_message(
            room,
            dispatch_event,
            "@user:localhost",
            media_events=[image_event],
        )

    newer_mock.assert_called_once()
    assert list(newer_mock.call_args.args[2]) == []
    action_mock.assert_awaited_once()
    assert not bot._turn_store.is_handled("$img1")


@pytest.mark.asyncio
async def test_thread_history_guard_does_not_interfere_with_normal_dispatch(tmp_path: Path) -> None:
    """Normal live dispatch proceeds when no newer unresponded message exists."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$m1",
        body="hello",
        source={"content": {"msgtype": "m.text", "body": "hello"}},
        server_timestamp=2000,
    )
    dispatch = _prepared_dispatch(event_id="$m1", body="hello")
    dispatch.context.thread_history = []
    bot._send_response = AsyncMock(return_value="$placeholder")
    bot._generate_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, bot._send_response)
    install_generate_response_mock(bot, bot._generate_response)

    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(return_value=_respond_dispatch_plan())),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="hello")),
        ),
        patch.object(bot._conversation_resolver, "hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot._turn_controller, "_log_dispatch_latency"),
    ):
        await bot._turn_controller._dispatch_text_message(room, event, "@user:localhost")

    # Dispatch proceeded to completion
    assert bot._turn_store.is_handled("$m1")


# ---------------------------------------------------------------------------
# BLOCKER 2 regression: multi-event batch preserves merged metadata
# ---------------------------------------------------------------------------


def _mention_text_event(
    *,
    event_id: str,
    body: str,
    mentioned_user_ids: list[str],
    sender: str = "@user:localhost",
    server_timestamp: int = 1000,
) -> nio.RoomMessageText:
    """Build a text event with m.mentions metadata."""
    content: dict[str, object] = {
        "msgtype": "m.text",
        "body": body,
        "m.mentions": {"user_ids": mentioned_user_ids},
    }
    return cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": "!room:localhost",
                "type": "m.room.message",
                "content": content,
            },
        ),
    )


def test_batch_dispatch_event_merges_mentions_across_events() -> None:
    """A batch of '@agent first' + 'follow up' must preserve the mention."""
    room = _make_room()
    mention_event = _mention_text_event(
        event_id="$m1",
        body="@agent first",
        mentioned_user_ids=["@mindroom_test_agent:localhost"],
        server_timestamp=1000,
    )
    followup_event = _text_event(event_id="$m2", body="follow up", server_timestamp=1001)

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [
            PendingEvent(event=mention_event, room=room, source_kind="message"),
            PendingEvent(event=followup_event, room=room, source_kind="message"),
        ],
    )
    dispatch_event = build_batch_dispatch_event(batch)

    assert isinstance(dispatch_event, PreparedTextEvent)
    content = dispatch_event.source.get("content", {})
    mentions = content.get("m.mentions", {})
    assert "@mindroom_test_agent:localhost" in mentions.get("user_ids", [])


def test_batch_dispatch_event_preserves_voice_fallback_metadata() -> None:
    """A voice + text batch must preserve raw_audio_fallback in the synthetic source."""
    room = _make_room()
    voice_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice1",
        body="transcribed voice",
        source={
            "content": {
                "body": "transcribed voice",
                "com.mindroom.source_kind": "voice",
                VOICE_RAW_AUDIO_FALLBACK_KEY: True,
            },
        },
        server_timestamp=1000,
    )
    text_event = _text_event(event_id="$m2", body="and this too", server_timestamp=1001)

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [
            PendingEvent(event=voice_event, room=room, source_kind="voice"),
            PendingEvent(event=text_event, room=room, source_kind="message"),
        ],
    )
    dispatch_event = build_batch_dispatch_event(batch)

    assert isinstance(dispatch_event, PreparedTextEvent)
    content = dispatch_event.source.get("content", {})
    assert content.get(VOICE_RAW_AUDIO_FALLBACK_KEY) is True


def test_single_prepared_batch_dispatch_event_preserves_source_kind() -> None:
    """Single prepared events should carry active policy separately from source kind."""
    room = _make_room()
    event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$followup",
        body="stop if you see this",
        source={"content": {"body": "stop if you see this"}},
        server_timestamp=1000,
    )

    batch = build_coalesced_batch(
        ("!room:localhost", "$thread", "@user:localhost"),
        [
            PendingEvent(
                event=event,
                room=room,
                source_kind="message",
                dispatch_policy_source_kind=COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
            ),
        ],
    )
    handoff = build_dispatch_handoff(batch)
    dispatch_event = build_batch_dispatch_event(batch)

    assert handoff.ingress.source_kind == "message"
    assert handoff.ingress.dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP
    assert isinstance(dispatch_event, PreparedTextEvent)
    assert dispatch_event.source_kind_override is None


def test_single_text_batch_dispatch_event_preserves_bypass_source_kind() -> None:
    """Single text active follow-ups should expose policy without changing source kind."""
    room = _make_room()
    event = _text_event(event_id="$relay", body="@agent relay", server_timestamp=1000)

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [
            PendingEvent(
                event=event,
                room=room,
                source_kind="message",
                dispatch_policy_source_kind=COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
            ),
        ],
    )
    handoff = build_dispatch_handoff(batch)
    dispatch_event = build_batch_dispatch_event(batch)

    assert handoff.ingress.source_kind == "message"
    assert handoff.ingress.dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP
    assert isinstance(dispatch_event, nio.RoomMessageText)


def test_batch_dispatch_event_preserves_original_sender() -> None:
    """A relay batch must preserve original_sender metadata."""
    room = _make_room()
    relay_event = PreparedTextEvent(
        sender="@bridge:localhost",
        event_id="$relay1",
        body="relayed message",
        source={
            "content": {
                "body": "relayed message",
                ORIGINAL_SENDER_KEY: "@real_user:remote",
            },
        },
        server_timestamp=1000,
    )
    followup = _text_event(
        event_id="$m2",
        body="follow up",
        sender="@bridge:localhost",
        server_timestamp=1001,
    )

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@real_user:remote"),
        [
            PendingEvent(event=relay_event, room=room, source_kind="message"),
            PendingEvent(event=followup, room=room, source_kind="message"),
        ],
    )
    dispatch_event = build_batch_dispatch_event(batch)

    assert isinstance(dispatch_event, PreparedTextEvent)
    content = dispatch_event.source.get("content", {})
    assert content.get(ORIGINAL_SENDER_KEY) == "@real_user:remote"


def test_batch_dispatch_event_preserves_attachment_ids() -> None:
    """Attachment IDs from all events must flow through to the synthetic source."""
    room = _make_room()
    event_with_attachment = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$m1",
        body="see attached",
        source={
            "content": {
                "body": "see attached",
                ATTACHMENT_IDS_KEY: ["att-001"],
            },
        },
        server_timestamp=1000,
    )
    event_with_another = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$m2",
        body="another",
        source={
            "content": {
                "body": "another",
                ATTACHMENT_IDS_KEY: ["att-002"],
            },
        },
        server_timestamp=1001,
    )

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [
            PendingEvent(event=event_with_attachment, room=room, source_kind="message"),
            PendingEvent(event=event_with_another, room=room, source_kind="message"),
        ],
    )
    dispatch_event = build_batch_dispatch_event(batch)

    assert isinstance(dispatch_event, PreparedTextEvent)
    content = dispatch_event.source.get("content", {})
    raw_ids = content.get(ATTACHMENT_IDS_KEY, [])
    assert isinstance(raw_ids, list), "attachment IDs must be a list, not a comma-string"
    assert "att-001" in raw_ids
    assert "att-002" in raw_ids


# ---------------------------------------------------------------------------
# Thread-history guard: command exclusion (BLOCKER 1 from R2 review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_newer_command_does_not_suppress_older_message(tmp_path: Path) -> None:
    """A newer !command must not suppress an older legitimate message during backlog replay."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    older_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$m1",
        body="What is the project structure?",
        source={"content": {"msgtype": "m.text", "body": "What is the project structure?"}},
        server_timestamp=1000,
    )
    dispatch = _prepared_dispatch(event_id="$m1", body="What is the project structure?")

    # Thread history contains a newer !help command from the same sender
    newer_cmd = ResolvedVisibleMessage(
        sender="@user:localhost",
        body="!help",
        timestamp=2000,
        event_id="$m2",
        content={"body": "!help"},
        thread_id=None,
        latest_event_id="$m2",
    )
    _set_context_histories(dispatch, [newer_cmd])
    bot._send_response = AsyncMock(return_value="$placeholder")
    bot._generate_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, bot._send_response)
    install_generate_response_mock(bot, bot._generate_response)

    action_mock = AsyncMock(return_value=_respond_dispatch_plan())
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="What is the project structure?")),
        ),
        patch.object(bot._conversation_resolver, "hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot._turn_controller, "_log_dispatch_latency"),
    ):
        await bot._turn_controller._dispatch_text_message(room, older_event, "@user:localhost")

    # The older message must NOT be suppressed — dispatch action should be called
    action_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_newer_command_with_whitespace_does_not_suppress(tmp_path: Path) -> None:
    """A newer command with leading whitespace must not suppress older messages."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    older_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$m1",
        body="hello",
        source={"content": {"msgtype": "m.text", "body": "hello"}},
        server_timestamp=1000,
    )
    dispatch = _prepared_dispatch(event_id="$m1", body="hello")

    newer_cmd = ResolvedVisibleMessage(
        sender="@user:localhost",
        body="  !help  ",
        timestamp=2000,
        event_id="$m2",
        content={"body": "  !help  "},
        thread_id=None,
        latest_event_id="$m2",
    )
    _set_context_histories(dispatch, [newer_cmd])
    bot._send_response = AsyncMock(return_value="$placeholder")
    bot._generate_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, bot._send_response)
    install_generate_response_mock(bot, bot._generate_response)

    action_mock = AsyncMock(return_value=_respond_dispatch_plan())
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="hello")),
        ),
        patch.object(bot._conversation_resolver, "hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot._turn_controller, "_log_dispatch_latency"),
    ):
        await bot._turn_controller._dispatch_text_message(room, older_event, "@user:localhost")

    action_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Thread-history guard: synthetic/automation bypass (BLOCKER 2 from R2 review)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_event_not_suppressed(tmp_path: Path) -> None:
    """Synthetic scheduled events must never be suppressed by the thread-history guard."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    scheduled_event = PreparedTextEvent(
        sender="@mindroom_test_agent:localhost",
        event_id="$s1",
        body="scheduled task output",
        source={
            "content": {"msgtype": "m.text", "body": "scheduled task output", "com.mindroom.source_kind": "scheduled"},
        },
        server_timestamp=1000,
        is_synthetic=True,
        source_kind_override="scheduled",
    )
    dispatch = _prepared_dispatch(event_id="$s1", body="scheduled task output", source_kind="scheduled")

    # A newer unresponded message from the same sender exists
    newer_msg = ResolvedVisibleMessage(
        sender="@mindroom_test_agent:localhost",
        body="another scheduled output",
        timestamp=2000,
        event_id="$s2",
        content={"body": "another scheduled output"},
        thread_id=None,
        latest_event_id="$s2",
    )
    _set_context_histories(dispatch, [newer_msg])
    bot._send_response = AsyncMock(return_value="$placeholder")
    bot._generate_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, bot._send_response)
    install_generate_response_mock(bot, bot._generate_response)

    action_mock = AsyncMock(return_value=_respond_dispatch_plan())
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="scheduled task output")),
        ),
        patch.object(bot._conversation_resolver, "hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot._turn_controller, "_log_dispatch_latency"),
    ):
        await bot._turn_controller._dispatch_text_message(room, scheduled_event, "@mindroom_test_agent:localhost")

    action_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_hook_event_not_suppressed(tmp_path: Path) -> None:
    """Synthetic hook events must never be suppressed by the thread-history guard."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    hook_event = PreparedTextEvent(
        sender="@mindroom_test_agent:localhost",
        event_id="$h1",
        body="hook result",
        source={"content": {"msgtype": "m.text", "body": "hook result", "com.mindroom.source_kind": "hook"}},
        server_timestamp=1000,
        is_synthetic=True,
        source_kind_override="hook",
    )
    dispatch = _prepared_dispatch(event_id="$h1", body="hook result", source_kind="hook")

    newer_msg = ResolvedVisibleMessage(
        sender="@mindroom_test_agent:localhost",
        body="newer message",
        timestamp=2000,
        event_id="$h2",
        content={"body": "newer message"},
        thread_id=None,
        latest_event_id="$h2",
    )
    _set_context_histories(dispatch, [newer_msg])
    bot._send_response = AsyncMock(return_value="$placeholder")
    bot._generate_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, bot._send_response)
    install_generate_response_mock(bot, bot._generate_response)

    action_mock = AsyncMock(return_value=_respond_dispatch_plan())
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="hook result")),
        ),
        patch.object(bot._conversation_resolver, "hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot._turn_controller, "_log_dispatch_latency"),
    ):
        await bot._turn_controller._dispatch_text_message(room, hook_event, "@mindroom_test_agent:localhost")

    action_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_multiple_scheduled_fires_not_suppressed(tmp_path: Path) -> None:
    """Two scheduled fires from the same sender must both execute independently."""
    bot = _make_bot(tmp_path)
    room = _make_room()

    first_fire = PreparedTextEvent(
        sender="@mindroom_test_agent:localhost",
        event_id="$s1",
        body="scheduled fire 1",
        source={"content": {"msgtype": "m.text", "body": "scheduled fire 1", "com.mindroom.source_kind": "scheduled"}},
        server_timestamp=1000,
        is_synthetic=True,
        source_kind_override="scheduled",
    )
    dispatch = _prepared_dispatch(event_id="$s1", body="scheduled fire 1", source_kind="scheduled")

    # Second scheduled fire is newer and unresponded
    second_fire_msg = ResolvedVisibleMessage(
        sender="@mindroom_test_agent:localhost",
        body="scheduled fire 2",
        timestamp=2000,
        event_id="$s2",
        content={"body": "scheduled fire 2"},
        thread_id=None,
        latest_event_id="$s2",
    )
    _set_context_histories(dispatch, [second_fire_msg])
    bot._send_response = AsyncMock(return_value="$placeholder")
    bot._generate_response = AsyncMock(return_value="$response")
    install_send_response_mock(bot, bot._send_response)
    install_generate_response_mock(bot, bot._generate_response)

    action_mock = AsyncMock(return_value=_respond_dispatch_plan())
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=DispatchPayload(prompt="scheduled fire 1")),
        ),
        patch.object(bot._conversation_resolver, "hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot._turn_controller, "_log_dispatch_latency"),
    ):
        await bot._turn_controller._dispatch_text_message(room, first_fire, "@mindroom_test_agent:localhost")

    # First fire must NOT be suppressed
    action_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# R3 regression: user-originated synthetics must still be guarded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coalesced_user_batch_suppressed_by_thread_guard(tmp_path: Path) -> None:
    """Coalesced user batches (is_synthetic=True, source_kind='user') must be guarded."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    coalesced_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$m1",
        body="batched message",
        source={"content": {"msgtype": "m.text", "body": "batched message"}},
        server_timestamp=1000,
        is_synthetic=True,
        source_kind_override="user",
    )
    dispatch = _prepared_dispatch(event_id="$m1", body="batched message")

    newer_msg = ResolvedVisibleMessage(
        sender="@user:localhost",
        body="newer message",
        timestamp=2000,
        event_id="$m2",
        content={"body": "newer message"},
        thread_id=None,
        latest_event_id="$m2",
    )
    _set_context_histories(dispatch, [newer_msg])

    action_mock = AsyncMock()
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
    ):
        await bot._turn_controller._dispatch_text_message(room, coalesced_event, "@user:localhost")

    # Coalesced user batch MUST be suppressed — not an automation event
    action_mock.assert_not_awaited()
    assert bot._turn_store.is_handled("$m1")


@pytest.mark.asyncio
async def test_coalesced_media_batch_suppressed_by_replay_snapshot(tmp_path: Path) -> None:
    """Media-backed coalesced user batches must still be suppressed by newer-user replay snapshots."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    image_event = _image_event(event_id="$img1", server_timestamp=1000)
    coalesced_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$img1",
        body="[Attached image]",
        source={"content": {"msgtype": "m.text", "body": "[Attached image]"}},
        server_timestamp=1000,
        is_synthetic=True,
        source_kind_override="image",
    )
    dispatch = _prepared_dispatch(event_id="$img1", body="[Attached image]")

    newer_msg = ResolvedVisibleMessage(
        sender="@user:localhost",
        body="newer message",
        timestamp=2000,
        event_id="$img2",
        content={"body": "newer message"},
        thread_id=None,
        latest_event_id="$img2",
    )
    _set_context_histories(dispatch, [newer_msg])

    action_mock = AsyncMock()
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=action_mock),
    ):
        await bot._turn_controller._dispatch_text_message(
            room,
            coalesced_event,
            "@user:localhost",
            media_events=[image_event],
        )

    # Media-backed coalesced user batch MUST still be suppressed.
    action_mock.assert_not_awaited()
    assert bot._turn_store.is_handled("$img1")


@pytest.mark.asyncio
async def test_normal_text_command_still_dispatches_as_command(tmp_path: Path) -> None:
    """Non-voice !commands must still take the command execution path."""
    bot = _make_bot(tmp_path, agent_name="router")
    room = _make_room()
    command_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$c1",
        body="!schedule tomorrow at 9am turn off the lights",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "!schedule tomorrow at 9am turn off the lights",
            },
        },
        server_timestamp=1000,
    )
    dispatch = _prepared_dispatch(event_id="$c1", body="!schedule tomorrow at 9am turn off the lights")

    handle_cmd_mock = AsyncMock()
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_controller, "_execute_command", new=handle_cmd_mock),
    ):
        await bot._turn_controller._dispatch_text_message(room, command_event, "@user:localhost")

    handle_cmd_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_active_voice_follow_up_preserves_voice_command_policy(tmp_path: Path) -> None:
    """Voice active follow-ups should force response policy without becoming commands."""
    bot = _make_bot(tmp_path, agent_name="router")
    room = _make_room()
    voice_command_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice_command",
        body="!schedule tomorrow at 9am turn off the lights",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "!schedule tomorrow at 9am turn off the lights",
                "com.mindroom.source_kind": "voice",
            },
        },
        server_timestamp=1000,
        source_kind_override="voice",
    )
    dispatch = _prepared_dispatch(
        event_id="$voice_command",
        body="!schedule tomorrow at 9am turn off the lights",
        thread_id="$thread",
        source_kind="voice",
        dispatch_policy_source_kind=COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
    )

    plan_mock = AsyncMock(return_value=_respond_dispatch_plan())
    execute_command_mock = AsyncMock()
    execute_response_mock = AsyncMock()
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_policy, "plan_turn", new=plan_mock),
        patch.object(bot._turn_controller, "_execute_command", new=execute_command_mock),
        patch.object(bot._turn_controller, "_execute_response_action", new=execute_response_mock),
    ):
        await bot._turn_controller._dispatch_text_message(room, voice_command_event, "@user:localhost")

    execute_command_mock.assert_not_awaited()
    plan_mock.assert_awaited_once()
    execute_response_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# R3 regression: commands must not be suppressed during backlog replay
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_older_command_not_suppressed_during_replay(tmp_path: Path) -> None:
    """An older !help replayed while a newer normal message exists must still dispatch."""
    bot = _make_bot(tmp_path, agent_name="router")
    room = _make_room()
    cmd_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$c1",
        body="!help",
        source={"content": {"msgtype": "m.text", "body": "!help"}},
        server_timestamp=1000,
    )
    dispatch = _prepared_dispatch(event_id="$c1", body="!help")

    newer_msg = ResolvedVisibleMessage(
        sender="@user:localhost",
        body="some question",
        timestamp=2000,
        event_id="$c2",
        content={"body": "some question"},
        thread_id=None,
        latest_event_id="$c2",
    )
    _set_context_histories(dispatch, [newer_msg])

    handle_cmd_mock = AsyncMock()
    with (
        patch.object(bot._turn_controller, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot._turn_controller, "_execute_command", new=handle_cmd_mock),
    ):
        await bot._turn_controller._dispatch_text_message(room, cmd_event, "@user:localhost")

    # Command must have been dispatched, not suppressed
    handle_cmd_mock.assert_awaited_once()


# ---------------------------------------------------------------------------
# Batch formatted_body merging (BLOCKER 4 from R2 review)
# ---------------------------------------------------------------------------


def _formatted_body_event(
    *,
    event_id: str,
    body: str,
    formatted_body: str,
    mentioned_user_ids: list[str] | None = None,
    sender: str = "@user:localhost",
    server_timestamp: int = 1000,
) -> nio.RoomMessageText:
    """Build a text event with formatted_body (bridge-style pill mentions)."""
    content: dict[str, object] = {
        "msgtype": "m.text",
        "body": body,
        "format": "org.matrix.custom.html",
        "formatted_body": formatted_body,
    }
    if mentioned_user_ids is not None:
        content["m.mentions"] = {"user_ids": mentioned_user_ids}
    return cast(
        "nio.RoomMessageText",
        nio.RoomMessageText.from_dict(
            {
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": server_timestamp,
                "room_id": "!room:localhost",
                "type": "m.room.message",
                "content": content,
            },
        ),
    )


def test_batch_dispatch_event_preserves_formatted_body_mentions() -> None:
    """Bridge-style pill mentions in formatted_body must survive batch merging."""
    room = _make_room()
    pill_event = _formatted_body_event(
        event_id="$m1",
        body="@agent hello",
        formatted_body='<a href="https://matrix.to/#/@mindroom_test_agent:localhost">agent</a> hello',
        server_timestamp=1000,
    )
    followup = _text_event(event_id="$m2", body="follow up", server_timestamp=1001)

    batch = build_coalesced_batch(
        ("!room:localhost", None, "@user:localhost"),
        [
            PendingEvent(event=pill_event, room=room, source_kind="message"),
            PendingEvent(event=followup, room=room, source_kind="message"),
        ],
    )
    dispatch_event = build_batch_dispatch_event(batch)

    assert isinstance(dispatch_event, PreparedTextEvent)
    content = dispatch_event.source.get("content", {})
    formatted = content.get("formatted_body", "")
    assert "@mindroom_test_agent:localhost" in formatted
    assert content.get("format") == "org.matrix.custom.html"


async def _capture_gate_dispatches(
    bot: AgentBot,
    room: nio.MatrixRoom,
    enqueued: Sequence[tuple[nio.Event | PreparedTextEvent, str, str | None, dict[str, object]]],
) -> tuple[list[MessageEnvelope], list[list[object]], list[DispatchPayloadWithAttachmentsRequest]]:
    """Dispatch queued events through the real handoff path and capture final envelopes."""
    envelopes: list[MessageEnvelope] = []
    media_batches: list[list[object]] = []
    payload_requests: list[DispatchPayloadWithAttachmentsRequest] = []

    async def record_plan(*args: object, **kwargs: object) -> DispatchPlan:
        dispatch = cast("PreparedDispatch", args[2])
        envelopes.append(dispatch.envelope)
        media_batches.append(list(cast("list[object] | None", kwargs.get("media_events")) or []))
        return _respond_dispatch_plan()

    async def record_response(*args: object, **_kwargs: object) -> None:
        dispatch = cast("PreparedDispatch", args[2])
        build_payload = cast("Callable[[MessageContext], object]", args[4])
        await build_payload(dispatch.context)

    async def record_payload_request(request: DispatchPayloadWithAttachmentsRequest) -> DispatchPayload:
        payload_requests.append(request)
        return DispatchPayload(prompt=request.prompt, attachment_ids=list(request.current_attachment_ids))

    with (
        patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(side_effect=record_plan)),
        patch.object(bot._turn_controller, "_execute_response_action", new=AsyncMock(side_effect=record_response)),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(side_effect=record_payload_request),
        ),
        patch.object(
            bot._inbound_turn_normalizer,
            "register_batch_media_attachments",
            new=AsyncMock(return_value=BatchMediaAttachmentResult(attachment_ids=[])),
        ),
    ):
        for event, source_kind, dispatch_policy_source_kind, metadata in enqueued:
            await bot._turn_controller._enqueue_for_dispatch(
                cast("nio.RoomMessageText | PreparedTextEvent", event),
                room,
                source_kind=source_kind,
                dispatch_policy_source_kind=dispatch_policy_source_kind,
                hook_source=cast("str | None", metadata.get("hook_source")),
                message_received_depth=cast("int", metadata.get("message_received_depth", 0)),
                requester_user_id=cast("str", metadata.get("requester_user_id", "@user:localhost")),
            )
        await bot._coalescing_gate.drain_all()

    return envelopes, media_batches, payload_requests


@pytest.mark.asyncio
async def test_gate_final_envelope_preserves_active_voice_source_and_policy(tmp_path: Path) -> None:
    """Active voice follow-ups should keep voice source kind and active policy separate."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    voice_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice-active",
        body="!help",
        source={"content": {"msgtype": "m.text", "body": "!help", "com.mindroom.source_kind": "voice"}},
        server_timestamp=1000,
        source_kind_override="voice",
    )

    envelopes, media_batches, _payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [
            (
                voice_event,
                "voice",
                COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
                {},
            ),
        ],
    )

    assert [envelope.source_kind for envelope in envelopes] == ["voice"]
    assert envelopes[0].dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP
    assert media_batches == [[]]


@pytest.mark.asyncio
async def test_gate_final_envelope_preserves_non_active_voice_command_policy(tmp_path: Path) -> None:
    """Non-active voice transcripts that look like commands should still plan as voice turns."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    voice_event = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$voice-normal",
        body="!help",
        source={"content": {"msgtype": "m.text", "body": "!help", "com.mindroom.source_kind": "voice"}},
        server_timestamp=1000,
        source_kind_override="voice",
    )

    envelopes, _media_batches, _payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [(voice_event, "voice", None, {})],
    )

    assert [envelope.source_kind for envelope in envelopes] == ["voice"]
    assert envelopes[0].dispatch_policy_source_kind is None


@pytest.mark.asyncio
async def test_gate_final_envelope_preserves_active_text_source_and_policy(tmp_path: Path) -> None:
    """Active text follow-ups should stay message source kind with separate active policy."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    event = _text_event(event_id="$text-active", body="I have more context")

    envelopes, _media_batches, _payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [(event, "message", COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP, {})],
    )

    assert [envelope.source_kind for envelope in envelopes] == ["message"]
    assert envelopes[0].dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP


@pytest.mark.asyncio
async def test_gate_final_envelope_preserves_active_and_normal_media_sources(tmp_path: Path) -> None:
    """Media handoffs should keep modality while active policy stays separate."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    image_event = _image_event(event_id="$image-active", server_timestamp=1000)
    file_event = _file_event(event_id="$file-normal", server_timestamp=1001)

    envelopes, media_batches, _payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [
            (
                image_event,
                "image",
                COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP,
                {},
            ),
            (
                file_event,
                "media",
                None,
                {"requester_user_id": "@other:localhost"},
            ),
        ],
    )

    assert [envelope.source_kind for envelope in envelopes] == ["image", "media"]
    assert envelopes[0].dispatch_policy_source_kind == COALESCING_BYPASS_ACTIVE_THREAD_FOLLOW_UP
    assert envelopes[1].dispatch_policy_source_kind is None
    assert [[event.event_id for event in media_batch] for media_batch in media_batches] == [
        ["$image-active"],
        ["$file-normal"],
    ]


@pytest.mark.asyncio
async def test_gate_final_envelope_preserves_raw_trusted_relay_source_kind(tmp_path: Path) -> None:
    """Raw relay text should stay raw for hydration while handoff source metadata reaches the envelope."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    relay_event = _text_event(
        event_id="$relay",
        body="relayed",
        sender="@mindroom_test_agent:localhost",
        source_kind=None,
        original_sender="@external:example.org",
    )

    envelopes, _media_batches, _payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [(relay_event, "trusted_internal_relay", None, {"requester_user_id": "@external:example.org"})],
    )

    assert [envelope.source_kind for envelope in envelopes] == ["trusted_internal_relay"]
    assert envelopes[0].requester_id == "@external:example.org"


@pytest.mark.asyncio
async def test_gate_final_envelope_preserves_hook_metadata_with_original_sender(tmp_path: Path) -> None:
    """Hook dispatch should not become a trusted relay just because original sender is present."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    hook_event = _text_event(
        event_id="$hook-dispatch",
        body="@test_agent hook output",
        sender="@mindroom_test_agent:localhost",
        source_kind="hook_dispatch",
        original_sender="@requester:localhost",
    )

    envelopes, _media_batches, _payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [
            (
                hook_event,
                "hook_dispatch",
                None,
                {
                    "hook_source": "message_received",
                    "message_received_depth": 1,
                    "requester_user_id": "@requester:localhost",
                },
            ),
        ],
    )

    assert [envelope.source_kind for envelope in envelopes] == ["hook_dispatch"]
    assert envelopes[0].hook_source == "message_received"
    assert envelopes[0].message_received_depth == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_kind", "event_id"),
    [
        ("scheduled", "$scheduled"),
        ("hook", "$hook"),
        ("hook_dispatch", "$hook-dispatch"),
        ("trusted_internal_relay", "$relay"),
    ],
)
async def test_automation_and_relay_source_kinds_dispatch_solo_with_human_neighbor(
    tmp_path: Path,
    source_kind: str,
    event_id: str,
) -> None:
    """Automation and relay source kinds should be FIFO barriers with preserved final source kind."""
    bot = _make_bot(tmp_path, debounce_ms=50)
    room = _make_room()
    automated = _text_event(
        event_id=event_id,
        body=f"{source_kind} turn",
        sender="@mindroom_test_agent:localhost",
        source_kind=source_kind,
        original_sender="@requester:localhost",
    )
    human = _text_event(event_id="$human", body="human turn", sender="@requester:localhost", server_timestamp=1001)

    envelopes, _media_batches, _payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [
            (
                automated,
                source_kind,
                None,
                {
                    "requester_user_id": "@requester:localhost",
                    "hook_source": "message_received" if source_kind.startswith("hook") else None,
                    "message_received_depth": 1 if source_kind.startswith("hook") else 0,
                },
            ),
            (human, "message", None, {"requester_user_id": "@requester:localhost"}),
        ],
    )

    assert [envelope.source_kind for envelope in envelopes] == [source_kind, "message"]
    assert [envelope.source_event_id for envelope in envelopes] == [event_id, "$human"]


@pytest.mark.asyncio
async def test_coalesced_attachment_ids_reach_envelope_and_model_payload(tmp_path: Path) -> None:
    """Coalesced attachment IDs should reach both the final envelope and model payload request."""
    bot = _make_bot(tmp_path, debounce_ms=10)
    room = _make_room()
    first = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$att1",
        body="first attachment",
        source={"content": {"msgtype": "m.text", "body": "first attachment", ATTACHMENT_IDS_KEY: ["att-001"]}},
        server_timestamp=1000,
    )
    second = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$att2",
        body="second attachment",
        source={"content": {"msgtype": "m.text", "body": "second attachment", ATTACHMENT_IDS_KEY: ["att-002"]}},
        server_timestamp=1001,
    )

    envelopes, _media_batches, payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [
            (first, "message", None, {}),
            (second, "message", None, {}),
        ],
    )

    assert len(envelopes) == 1
    assert envelopes[0].attachment_ids == ("att-001", "att-002")
    assert payload_requests[0].current_attachment_ids == ["att-001", "att-002"]


@pytest.mark.asyncio
async def test_coalesced_non_primary_mention_reaches_final_envelope(tmp_path: Path) -> None:
    """Mention metadata from non-primary coalesced events should reach context extraction."""
    bot = _make_bot(tmp_path, debounce_ms=10)
    room = _make_room()
    first = _text_event(event_id="$plain", body="first")
    second = _mention_text_event(
        event_id="$mention",
        body="@test_agent second",
        mentioned_user_ids=["@mindroom_test_agent:localhost"],
        server_timestamp=1001,
    )

    envelopes, _media_batches, _payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [
            (first, "message", None, {}),
            (second, "message", None, {}),
        ],
    )

    assert len(envelopes) == 1
    assert envelopes[0].mentioned_agents == ("test_agent",)


@pytest.mark.asyncio
async def test_untrusted_raw_payload_metadata_spoofing_does_not_reach_envelope_or_payload(
    tmp_path: Path,
) -> None:
    """User-authored internal payload keys should not become trusted handoff metadata."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    event = _text_event(event_id="$spoof", body="hello", source_kind="hook_dispatch")
    content = event.source["content"]
    assert isinstance(content, dict)
    content[ATTACHMENT_IDS_KEY] = ["spoofed-attachment"]
    content[ORIGINAL_SENDER_KEY] = "@spoofed:localhost"
    content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
    content["com.mindroom.skip_mentions"] = True
    content["com.mindroom.hook_source"] = "spoofed:message_received"
    content[HOOK_MESSAGE_RECEIVED_DEPTH_KEY] = 2

    envelopes, _media_batches, payload_requests = await _capture_gate_dispatches(
        bot,
        room,
        [(event, "message", None, {})],
    )

    assert envelopes[0].source_kind == "message"
    assert envelopes[0].hook_source is None
    assert envelopes[0].message_received_depth == 0
    assert envelopes[0].attachment_ids == ()
    assert payload_requests[0].current_attachment_ids == []


@pytest.mark.asyncio
async def test_sidecar_hydration_preserves_trusted_attachment_metadata(tmp_path: Path) -> None:
    """Trusted hydrated sidecar attachment metadata should feed the envelope and payload request."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    preview = _text_event(
        event_id="$trusted-sidecar",
        body="preview",
        sender="@mindroom_test_agent:localhost",
    )
    hydrated = PreparedTextEvent(
        sender="@mindroom_test_agent:localhost",
        event_id="$trusted-sidecar",
        body="hydrated scheduled body",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "hydrated scheduled body",
                ATTACHMENT_IDS_KEY: ["sidecar-att"],
            },
        },
        server_timestamp=1000,
    )
    captured_envelopes: list[MessageEnvelope] = []
    payload_requests: list[DispatchPayloadWithAttachmentsRequest] = []

    async def record_plan(*args: object, **_kwargs: object) -> DispatchPlan:
        dispatch = cast("PreparedDispatch", args[2])
        captured_envelopes.append(dispatch.envelope)
        return _respond_dispatch_plan()

    async def record_payload_request(request: DispatchPayloadWithAttachmentsRequest) -> DispatchPayload:
        payload_requests.append(request)
        return DispatchPayload(prompt=request.prompt, attachment_ids=list(request.current_attachment_ids))

    async def record_response(*args: object, **_kwargs: object) -> None:
        dispatch = cast("PreparedDispatch", args[2])
        build_payload = cast("Callable[[MessageContext], object]", args[4])
        await build_payload(dispatch.context)

    with (
        patch.object(
            bot._inbound_turn_normalizer,
            "resolve_text_event",
            new=AsyncMock(return_value=hydrated),
        ),
        patch.object(
            bot._inbound_turn_normalizer,
            "build_dispatch_payload_with_attachments",
            new=AsyncMock(side_effect=record_payload_request),
        ),
        patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(side_effect=record_plan)),
        patch.object(bot._turn_controller, "_execute_response_action", new=AsyncMock(side_effect=record_response)),
    ):
        await bot._turn_controller._dispatch_text_message(
            room,
            preview,
            "@requester:localhost",
            ingress_metadata=DispatchIngressMetadata(source_kind="scheduled"),
            payload_metadata=DispatchPayloadMetadata(attachment_ids=None),
        )

    assert captured_envelopes[0].source_kind == "scheduled"
    assert captured_envelopes[0].attachment_ids == ("sidecar-att",)
    assert payload_requests[0].current_attachment_ids == ["sidecar-att"]


@pytest.mark.asyncio
async def test_sidecar_hydration_refreshes_prompt_and_mentions_before_dispatch(tmp_path: Path) -> None:
    """Hydrated sidecar metadata should replace preview prompt and feed final context extraction."""
    bot = _make_bot(tmp_path, debounce_ms=0)
    room = _make_room()
    preview = _text_event(event_id="$sidecar", body="preview")
    hydrated = PreparedTextEvent(
        sender="@user:localhost",
        event_id="$sidecar",
        body="@test_agent hydrated body",
        source={
            "content": {
                "msgtype": "m.text",
                "body": "@test_agent hydrated body",
                "m.mentions": {"user_ids": ["@mindroom_test_agent:localhost"]},
            },
        },
        server_timestamp=1000,
    )
    captured_envelopes: list[MessageEnvelope] = []
    captured_handled_turns: list[HandledTurnState] = []

    async def record_plan(*args: object, **_kwargs: object) -> DispatchPlan:
        dispatch = cast("PreparedDispatch", args[2])
        captured_envelopes.append(dispatch.envelope)
        return _respond_dispatch_plan()

    async def record_response(*_args: object, **kwargs: object) -> None:
        captured_handled_turns.append(cast("HandledTurnState", kwargs["handled_turn"]))

    handled_turn = HandledTurnState.create(["$sidecar"], source_event_prompts={"$sidecar": "preview"})
    with (
        patch.object(
            bot._inbound_turn_normalizer,
            "resolve_text_event",
            new=AsyncMock(return_value=hydrated),
        ),
        patch.object(bot._turn_policy, "plan_turn", new=AsyncMock(side_effect=record_plan)),
        patch.object(bot._turn_controller, "_execute_response_action", new=AsyncMock(side_effect=record_response)),
    ):
        await bot._turn_controller._dispatch_text_message(
            room,
            preview,
            "@user:localhost",
            handled_turn=handled_turn,
        )

    assert captured_envelopes[0].mentioned_agents == ("test_agent",)
    assert captured_handled_turns[0].source_event_prompts == {"$sidecar": "@test_agent hydrated body"}


@pytest.mark.asyncio
async def test_router_early_skip_keeps_sidecar_preview_for_hydration(tmp_path: Path) -> None:
    """Router early skip should not drop sidecar previews before hydration can recover metadata."""
    bot = _make_bot(tmp_path, agent_name="router")
    room = _make_room()
    sidecar_preview = cast("nio.RoomMessageText", _file_event(event_id="$sidecar-preview", body="preview"))
    content = sidecar_preview.source["content"]
    assert isinstance(content, dict)
    content["io.mindroom.long_text"] = {
        "version": 2,
        "encoding": "matrix_event_content_json",
        "original_event_size": 100_000,
        "preview_size": len(sidecar_preview.body),
        "is_complete_content": True,
    }

    should_skip = await bot._turn_controller._should_skip_router_before_shared_ingress_work(
        room,
        sidecar_preview,
        requester_user_id="@user:localhost",
        thread_id="$thread",
    )

    assert should_skip is False
