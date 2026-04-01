"""Tests for live debounce-based message coalescing."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, _DispatchPayload, _MessageContext, _PreparedDispatch
from mindroom.coalescing import GatePhase, PendingEvent, SyntheticTextEvent, build_coalesced_batch
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _make_config(
    tmp_path: Path,
    *,
    debounce_ms: int = 10,
    upload_grace_ms: int = 0,
    enabled: bool = True,
) -> Config:
    """Build a config with configurable live coalescing enabled or disabled."""
    return bind_runtime_paths(
        Config(
            agents={"test_agent": AgentConfig(display_name="TestAgent", rooms=["!room:localhost"])},
            teams={},
            models={"default": ModelConfig(provider="test", id="test-model")},
            defaults=DefaultsConfig(
                coalescing={
                    "enabled": enabled,
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
    enabled: bool = True,
) -> AgentBot:
    """Create a bot instance wired to a temporary runtime root."""
    config = _make_config(tmp_path, debounce_ms=debounce_ms, upload_grace_ms=upload_grace_ms, enabled=enabled)
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        password=TEST_PASSWORD,
        display_name="TestAgent",
        user_id="@mindroom_test_agent:localhost",
    )
    return AgentBot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!room:localhost"])


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


def _coalescing_phases(bot: AgentBot) -> tuple[GatePhase, ...]:
    """Return the active coalescing phases for deterministic assertions."""
    return bot._coalescing_gate.debug_phases()


def _text_event(
    *,
    event_id: str,
    body: str,
    sender: str = "@user:localhost",
    server_timestamp: int = 1000,
    thread_id: str | None = None,
    source_kind: str | None = None,
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


def _prepared_dispatch(
    *,
    event_id: str,
    requester_user_id: str = "@user:localhost",
    body: str = "hello",
    thread_id: str | None = None,
) -> _PreparedDispatch:
    context = _MessageContext(
        am_i_mentioned=True,
        is_thread=thread_id is not None,
        thread_id=thread_id,
        thread_history=[],
        mentioned_agents=[],
        has_non_agent_mentions=False,
    )
    return _PreparedDispatch(
        requester_user_id=requester_user_id,
        context=context,
        correlation_id=event_id,
        envelope=MessageEnvelope(
            source_event_id=event_id,
            room_id="!room:localhost",
            thread_id=thread_id,
            resolved_thread_id=thread_id,
            requester_id=requester_user_id,
            sender_id=requester_user_id,
            body=body,
            attachment_ids=(),
            mentioned_agents=(),
            agent_name="test_agent",
            source_kind="message",
        ),
    )


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
        source_event_ids: list[str] | None = None,
    ) -> None:
        calls.append((dispatched_event.body, source_event_ids or [], media_events or []))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(event, room, source_kind="message", requester_user_id="@user:localhost")
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
    calls: list[tuple[str, list[str]]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append((dispatched_event.body, source_event_ids or []))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(first, room, source_kind="message", requester_user_id="@user:localhost")
        await bot._enqueue_for_dispatch(second, room, source_kind="message", requester_user_id="@user:localhost")
        await asyncio.sleep(0.03)

    assert calls == [
        (
            "The user sent the following messages in quick succession. "
            "Treat them as one turn and respond once:\n\nfirst\nsecond",
            ["$m1", "$m2"],
        ),
    ]


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
        source_event_ids: list[str] | None = None,
    ) -> None:
        calls.append((dispatched_event.body, source_event_ids or [], len(media_events or [])))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(image_event, room, source_kind="image", requester_user_id="@user:localhost")
        await bot._enqueue_for_dispatch(text_event, room, source_kind="message", requester_user_id="@user:localhost")
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        calls.append((dispatched_event.body, source_event_ids or [], len(media_events or [])))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(text_event, room, source_kind="message", requester_user_id="@user:localhost")
        await _wait_for(lambda: _coalescing_phases(bot) == (GatePhase.GRACE,))
        assert calls == []

        await bot._enqueue_for_dispatch(image_event, room, source_kind="image", requester_user_id="@user:localhost")
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        calls.append((source_event_ids or [], len(media_events or [])))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(text_event, room, source_kind="message", requester_user_id="@user:localhost")
        await _wait_for(lambda: _coalescing_phases(bot) == (GatePhase.GRACE,))

        await bot._enqueue_for_dispatch(first_image, room, source_kind="image", requester_user_id="@user:localhost")
        await asyncio.sleep(0.01)
        await bot._enqueue_for_dispatch(second_image, room, source_kind="image", requester_user_id="@user:localhost")
        await _wait_for(lambda: len(calls) == 1)

    assert calls == [(["$m1", "$img1", "$img2"], 2)]


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
        source_event_ids: list[str] | None = None,
    ) -> None:
        calls.append((source_event_ids or [], len(media_events or [])))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(text_event, room, source_kind="message", requester_user_id="@user:localhost")
        await _wait_for(lambda: len(calls) == 1)

        await bot._enqueue_for_dispatch(image_event, room, source_kind="image", requester_user_id="@user:localhost")
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append(source_event_ids or [])

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(alice, room, source_kind="message", requester_user_id="@alice:localhost")
        await bot._enqueue_for_dispatch(bob, room, source_kind="message", requester_user_id="@bob:localhost")
        await asyncio.sleep(0.03)

    assert sorted(calls) == [["$m1"], ["$m2"]]


def test_build_coalesced_batch_keeps_normalized_voice_out_of_media_events() -> None:
    """Voice messages should enter coalescing as synthetic text, not raw media."""
    room = _make_room()
    voice_event = SyntheticTextEvent(
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append(source_event_ids or [])

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(thread_a, room, source_kind="message", requester_user_id="@user:localhost")
        await bot._enqueue_for_dispatch(thread_b, room, source_kind="message", requester_user_id="@user:localhost")
        await asyncio.sleep(0.03)

    assert sorted(calls) == [["$m1"], ["$m2"]]


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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append((dispatched_event.body, source_event_ids or []))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(first, room, source_kind="message", requester_user_id="@user:localhost")
        await bot._enqueue_for_dispatch(command, room, source_kind="message", requester_user_id="@user:localhost")

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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append((dispatched_event.body, source_event_ids or []))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(first, room, source_kind="message", requester_user_id="@user:localhost")
        await asyncio.sleep(0.01)
        await bot._enqueue_for_dispatch(command, room, source_kind="message", requester_user_id="@user:localhost")
        await asyncio.sleep(0.005)
        await bot._enqueue_for_dispatch(second, room, source_kind="message", requester_user_id="@user:localhost")
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append((dispatched_event.body, source_event_ids or []))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(text_event, room, source_kind="message", requester_user_id="@user:localhost")
        await _wait_for(lambda: _coalescing_phases(bot) == (GatePhase.GRACE,))

        await bot._enqueue_for_dispatch(command_event, room, source_kind="message", requester_user_id="@user:localhost")

    assert calls == [
        ("first", ["$m1"]),
        ("!help", ["$m2"]),
    ]


@pytest.mark.asyncio
async def test_messages_during_active_response_wait_and_batch_after_completion(tmp_path: Path) -> None:
    """Hold new messages while a response is in flight, then batch them."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second", server_timestamp=1001)
    third = _text_event(event_id="$m3", body="third", server_timestamp=1002)
    entered_first_dispatch = asyncio.Event()
    release_first_dispatch = asyncio.Event()
    calls: list[list[str]] = []

    async def record_dispatch(
        _room: nio.MatrixRoom,
        _dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append(source_event_ids or [])
        if source_event_ids == ["$m1"]:
            entered_first_dispatch.set()
            await release_first_dispatch.wait()

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(first, room, source_kind="message", requester_user_id="@user:localhost")
        await asyncio.sleep(0.03)
        await entered_first_dispatch.wait()

        await bot._enqueue_for_dispatch(second, room, source_kind="message", requester_user_id="@user:localhost")
        await bot._enqueue_for_dispatch(third, room, source_kind="message", requester_user_id="@user:localhost")
        await asyncio.sleep(0.03)

        assert calls == [["$m1"]]

        release_first_dispatch.set()
        await asyncio.sleep(0.05)

    assert calls == [["$m1"], ["$m2", "$m3"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["scheduled", "hook"])
async def test_coalescing_exempt_source_kinds_bypass_gate(tmp_path: Path, source_kind: str) -> None:
    """Bypass the gate for synthetic scheduled and hook-originated events."""
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events, source_event_ids
        calls.append(dispatched_event.body)

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(event, room, source_kind=source_kind, requester_user_id="@user:localhost")

    assert calls == [f"{source_kind} task"]


@pytest.mark.asyncio
async def test_prepare_for_sync_shutdown_waits_for_active_flush_task(tmp_path: Path) -> None:
    """Wait for an active flush task before finishing sync shutdown."""
    bot = _make_bot(tmp_path)
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append(source_event_ids or [])
        entered_dispatch.set()
        await release_dispatch.wait()

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(event, room, source_kind="message", requester_user_id="@user:localhost")
        await asyncio.sleep(0.03)
        await entered_dispatch.wait()

        assert _coalescing_phases(bot) == (GatePhase.IN_FLIGHT,)

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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append((dispatched_event.body, source_event_ids or []))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(event, room, source_kind="message", requester_user_id="@user:localhost")
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append((dispatched_event.body, source_event_ids or []))

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        await bot._enqueue_for_dispatch(event, room, source_kind="message", requester_user_id="@user:localhost")
        await _wait_for(lambda: _coalescing_phases(bot) == (GatePhase.GRACE,))

        await bot.prepare_for_sync_shutdown()

    assert calls == [("hello", ["$m1"])]
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_shutdown_during_flush_task_does_not_start_grace(tmp_path: Path) -> None:
    """Shutdown should dispatch immediately even if a queued flush starts late."""
    bot = _make_bot(tmp_path, debounce_ms=0, upload_grace_ms=200)
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")
    calls: list[tuple[str, list[str]]] = []
    entered_flush = asyncio.Event()
    release_flush = asyncio.Event()
    original_run_gate_flush = bot._coalescing_gate._run_gate_flush

    async def delayed_run_gate_flush(
        key: tuple[str, str | None, str],
        *,
        wake_epoch: int | None = None,
        bypass_grace: bool = False,
    ) -> None:
        entered_flush.set()
        await release_flush.wait()
        await original_run_gate_flush(
            key,
            wake_epoch=wake_epoch,
            bypass_grace=bypass_grace,
        )

    async def record_dispatch(
        _room: nio.MatrixRoom,
        dispatched_event: nio.RoomMessageText,
        _requester_user_id: str,
        *,
        media_events: list[object] | None = None,
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append((dispatched_event.body, source_event_ids or []))

    with (
        patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)),
        patch.object(bot._coalescing_gate, "_run_gate_flush", new=delayed_run_gate_flush),
    ):
        enqueue_task = asyncio.create_task(
            bot._enqueue_for_dispatch(event, room, source_kind="message", requester_user_id="@user:localhost"),
        )
        await entered_flush.wait()

        shutdown_task = asyncio.create_task(bot.prepare_for_sync_shutdown())
        await asyncio.sleep(0.01)
        assert shutdown_task.done() is False

        release_flush.set()
        await enqueue_task
        await shutdown_task

    assert calls == [("hello", ["$m1"])]
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_cleanup_drains_pending_debounce_tasks(tmp_path: Path) -> None:
    """Drain pending debounce tasks when a bot is cleaned up."""
    bot = _make_bot(tmp_path, debounce_ms=1000)
    bot.client = AsyncMock()
    bot._emit_agent_lifecycle_event = AsyncMock()
    room = _make_room()
    event = _text_event(event_id="$m1", body="hello")

    with (
        patch.object(bot, "_dispatch_text_message", new=AsyncMock()) as mock_dispatch,
        patch("mindroom.bot.get_joined_rooms", new=AsyncMock(return_value=[])),
        patch("mindroom.bot.wait_for_background_tasks", new=AsyncMock()),
    ):
        await bot._enqueue_for_dispatch(event, room, source_kind="message", requester_user_id="@user:localhost")
        assert _coalescing_phases(bot) == (GatePhase.DEBOUNCE,)

        await bot.cleanup()

    mock_dispatch.assert_awaited_once()
    assert bot._coalescing_gate.is_idle()


@pytest.mark.asyncio
async def test_upload_grace_hard_cap_prevents_indefinite_extension(tmp_path: Path) -> None:
    """Media arrivals may extend grace, but never past the gate hard cap."""
    bot = _make_bot(tmp_path, debounce_ms=10, upload_grace_ms=40)
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
        source_event_ids: list[str] | None = None,
    ) -> None:
        _ = media_events
        calls.append(source_event_ids or [])

    with patch.object(bot, "_dispatch_text_message", new=AsyncMock(side_effect=record_dispatch)):
        started_at = time.monotonic()
        await bot._enqueue_for_dispatch(text_event, room, source_kind="message", requester_user_id="@user:localhost")
        await _wait_for(lambda: _coalescing_phases(bot) == (GatePhase.GRACE,))

        for delay, image_event in zip((0.03, 0.04, 0.04, 0.04), image_events, strict=True):
            await asyncio.sleep(delay)
            await bot._enqueue_for_dispatch(image_event, room, source_kind="image", requester_user_id="@user:localhost")
            assert _coalescing_phases(bot) == (GatePhase.GRACE,)
        await _wait_for(lambda: len(calls) == 1, deadline_seconds=0.35)

    assert calls == [["$m1", "$img1", "$img2", "$img3", "$img4"]]
    assert time.monotonic() - started_at < 0.35


@pytest.mark.asyncio
async def test_response_tracker_marks_all_batch_event_ids(tmp_path: Path) -> None:
    """Mark every source event ID from a coalesced batch as responded."""
    bot = _make_bot(tmp_path)
    room = _make_room()
    first = _text_event(event_id="$m1", body="first", server_timestamp=1000)
    second = _text_event(event_id="$m2", body="second", server_timestamp=1001)
    dispatch = _prepared_dispatch(event_id="$m2")

    with (
        patch.object(bot, "_prepare_dispatch", new=AsyncMock(return_value=dispatch)),
        patch.object(bot, "_resolve_dispatch_action", new=AsyncMock(return_value=MagicMock(kind="individual"))),
        patch.object(
            bot,
            "_build_dispatch_payload_with_attachments",
            new=AsyncMock(return_value=_DispatchPayload(prompt="combined")),
        ),
        patch.object(bot, "_send_response", new=AsyncMock(return_value="$placeholder")),
        patch.object(bot, "_generate_response", new=AsyncMock(return_value="$response")),
        patch.object(bot, "_hydrate_dispatch_context", new=AsyncMock()),
        patch.object(bot, "_log_dispatch_latency"),
    ):
        await bot._enqueue_for_dispatch(first, room, source_kind="message", requester_user_id="@user:localhost")
        await bot._enqueue_for_dispatch(second, room, source_kind="message", requester_user_id="@user:localhost")
        await asyncio.sleep(0.05)

    assert bot.response_tracker.has_responded("$m1")
    assert bot.response_tracker.has_responded("$m2")
    assert bot.response_tracker.get_response_event_id("$m1") == "$response"
    assert bot.response_tracker.get_response_event_id("$m2") == "$response"
