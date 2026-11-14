"""Thread snooze coordinator tests: room state restore, live tag events, and Matrix delivery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.hooks.sender import build_hook_message_sender
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.orchestration.thread_snooze_runtime import ThreadSnoozeRuntimeCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.thread_snooze import Snooze, SnoozeUnavailableError
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE, ThreadTagsError, ThreadTagsPermissionError
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:localhost"
ROOT = "$root"
PERSON = "@bas:localhost"
UNTIL = datetime(2026, 11, 14, 8, tzinfo=UTC)
_MODULE = "mindroom.orchestration.thread_snooze_runtime"


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(agents={"code": AgentConfig(display_name="Code")}),
        runtime_paths=test_runtime_paths(tmp_path),
    )


def _coordinator(config: Config, bots: dict[str, Any]) -> ThreadSnoozeRuntimeCoordinator:
    return ThreadSnoozeRuntimeCoordinator(
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
        bots=lambda: bots,
    )


def _bot(*room_ids: str) -> MagicMock:
    bot = MagicMock()
    bot.client = MagicMock()
    bot.client.rooms = {room_id: MagicMock() for room_id in room_ids}
    bot.hook_context.send_message = AsyncMock(return_value="$wake")
    return bot


def _snooze_source(
    *,
    root: str = ROOT,
    sender: str = PERSON,
    set_by: str = PERSON,
    content: dict[str, object] | None = None,
) -> dict[str, object]:
    if content is None:
        content = {
            "set_by": set_by,
            "set_at": "2026-11-13T08:00:00+00:00",
            "note": "check CI",
            "data": {"until": UNTIL.isoformat()},
        }
    return {
        "type": THREAD_TAGS_EVENT_TYPE,
        "state_key": f'["{root}","snoozed"]',
        "sender": sender,
        "event_id": f"$tag-{root}",
        "origin_server_ts": 1,
        "content": content,
    }


def _tracked(coordinator: ThreadSnoozeRuntimeCoordinator) -> dict[tuple[str, str], Snooze]:
    return {key: entry.snooze for key, entry in coordinator._runtime._snoozes.items()}


@pytest.mark.asyncio
async def test_restore_indexes_the_rooms_snoozes_and_reads_state_once(tmp_path: Path) -> None:
    """Every bot that joins a room restores it, but the room's state is read only once per process."""
    state = nio.RoomGetStateResponse(
        events=[
            _snooze_source(),
            _snooze_source(root="$woken", content={}),
            {"type": "m.room.name", "state_key": "", "sender": PERSON, "content": {"name": "Room"}},
        ],
        room_id=ROOM,
    )
    router, code = _bot(ROOM), _bot(ROOM)
    for bot in (router, code):
        bot.client.room_get_state = AsyncMock(return_value=state)
    coordinator = _coordinator(_config(tmp_path), {"router": router, "code": code})

    await asyncio.gather(coordinator.restore_room(router, ROOM), coordinator.restore_room(code, ROOM))
    await coordinator.restore_room(code, ROOM)

    assert router.client.room_get_state.await_count + code.client.room_get_state.await_count == 1
    assert _tracked(coordinator) == {
        (ROOM, ROOT): Snooze(room_id=ROOM, thread_root_id=ROOT, until=UNTIL, note="check CI", mention=PERSON),
    }


@pytest.mark.asyncio
async def test_restore_reads_the_room_again_after_a_failed_read(tmp_path: Path) -> None:
    """A room whose state could not be read is not marked restored, so the next join reads it."""
    bot = _bot(ROOM)
    bot.client.room_get_state = AsyncMock(
        side_effect=[
            nio.RoomGetStateError("boom"),
            RuntimeError("transport"),
            nio.RoomGetStateResponse(events=[_snooze_source()], room_id=ROOM),
        ],
    )
    coordinator = _coordinator(_config(tmp_path), {"router": bot})

    await coordinator.restore_room(bot, ROOM)
    await coordinator.restore_room(bot, ROOM)
    assert _tracked(coordinator) == {}
    await coordinator.restore_room(bot, ROOM)

    assert set(_tracked(coordinator)) == {(ROOM, ROOT)}


@pytest.mark.asyncio
async def test_live_event_from_a_person_tracks_and_a_removal_forgets(tmp_path: Path) -> None:
    """A live snooze tag event from sync indexes the snooze, and its removal drops it."""
    coordinator = _coordinator(_config(tmp_path), {})

    coordinator.observe_state_event(ROOM, _snooze_source())
    assert _tracked(coordinator)[ROOM, ROOT].mention == PERSON

    coordinator.observe_state_event(ROOM, _snooze_source(content={}))
    assert _tracked(coordinator) == {}

    coordinator.observe_state_event(ROOM, {"type": "m.room.topic", "state_key": "", "content": {"topic": "x"}})
    assert _tracked(coordinator) == {}


def test_tool_snooze_mentions_a_person_and_not_a_bot(tmp_path: Path) -> None:
    """A snooze set for a person mentions them on wake; one set for an agent mentions nobody."""
    config = _config(tmp_path)
    code_id = entity_ids(config, runtime_paths_for(config))["code"].full_id
    coordinator = _coordinator(config, {})

    coordinator.snoozed(ROOM, ROOT, until=UNTIL, note="later", set_by=PERSON)
    assert _tracked(coordinator)[ROOM, ROOT] == Snooze(
        room_id=ROOM,
        thread_root_id=ROOT,
        until=UNTIL,
        note="later",
        mention=PERSON,
    )

    coordinator.snoozed(ROOM, ROOT, until=UNTIL, note=None, set_by=code_id)
    assert _tracked(coordinator)[ROOM, ROOT].mention is None

    coordinator.unsnoozed(ROOM, ROOT)
    assert _tracked(coordinator) == {}


@pytest.mark.asyncio
async def test_current_until_and_is_resolved_read_the_tags_through_a_joined_bot(tmp_path: Path) -> None:
    """Room state is read through the router first, skipping bots not joined to the room."""
    router, code = _bot(), _bot(ROOM)
    coordinator = _coordinator(_config(tmp_path), {"code": code, "router": router})
    contents = {
        "snoozed": _snooze_source()["content"],
        "resolved": None,
    }

    async def read(client: object, room_id: str, thread_root_id: str, tag: str) -> dict[str, Any] | None:
        assert (client, room_id, thread_root_id) == (code.client, ROOM, ROOT)
        return contents[tag]

    with patch(f"{_MODULE}.get_thread_tag_content", new=read):
        assert await coordinator.current_until(ROOM, ROOT) == UNTIL
        assert await coordinator.is_resolved(ROOM, ROOT) is False
        contents["snoozed"] = {"set_by": PERSON, "set_at": "2026-11-13T08:00:00+00:00", "data": {}}
        contents["resolved"] = {"set_by": PERSON, "set_at": "2026-11-13T08:00:00+00:00"}
        assert await coordinator.current_until(ROOM, ROOT) is None
        assert await coordinator.is_resolved(ROOM, ROOT) is True
        contents["snoozed"] = None
        assert await coordinator.current_until(ROOM, ROOT) is None


@pytest.mark.asyncio
async def test_reads_without_a_joined_bot_are_unavailable(tmp_path: Path) -> None:
    """With no bot in the room the runtime retries later instead of treating the snooze as gone."""
    coordinator = _coordinator(_config(tmp_path), {"router": _bot()})

    with pytest.raises(SnoozeUnavailableError):
        await coordinator.current_until(ROOM, ROOT)
    with pytest.raises(SnoozeUnavailableError):
        await coordinator.is_resolved(ROOM, ROOT)


@pytest.mark.asyncio
async def test_a_non_matrix_exception_from_a_bot_call_is_unavailable(tmp_path: Path) -> None:
    """Any failure becomes a per-entry retry, so one broken call cannot stop every other wake."""
    coordinator = _coordinator(_config(tmp_path), {"router": _bot(ROOM)})

    with (
        patch(f"{_MODULE}.get_thread_tag_content", new=AsyncMock(side_effect=ValueError("bad"))),
        pytest.raises(SnoozeUnavailableError),
    ):
        await coordinator.current_until(ROOM, ROOT)
    with (
        patch(f"{_MODULE}.remove_thread_tag", new=AsyncMock(side_effect=KeyError("bad"))),
        pytest.raises(SnoozeUnavailableError),
    ):
        await coordinator.clear(ROOM, ROOT)


@pytest.mark.asyncio
async def test_clear_tries_the_router_first_then_another_joined_bot(tmp_path: Path) -> None:
    """A router without the power level to write tags hands the removal to the next joined bot."""
    router, code, absent = _bot(ROOM), _bot(ROOM), _bot()
    coordinator = _coordinator(_config(tmp_path), {"absent": absent, "code": code, "router": router})
    calls: list[object] = []

    async def remove(client: object, room_id: str, thread_root_id: str, tag: str) -> None:
        assert (room_id, thread_root_id, tag) == (ROOM, ROOT, "snoozed")
        calls.append(client)
        if client is router.client:
            msg = "power level"
            raise ThreadTagsPermissionError(msg)

    with patch(f"{_MODULE}.remove_thread_tag", new=remove):
        assert await coordinator.clear(ROOM, ROOT) is True

    assert calls == [router.client, code.client]


@pytest.mark.asyncio
async def test_clear_is_false_when_no_bot_may_write(tmp_path: Path) -> None:
    """Clear reports False when every joined bot lacks the power level, or no bot is in the room."""
    router, code = _bot(ROOM), _bot(ROOM)
    coordinator = _coordinator(_config(tmp_path), {"code": code, "router": router})

    with patch(f"{_MODULE}.remove_thread_tag", new=AsyncMock(side_effect=ThreadTagsPermissionError("no"))) as remove:
        assert await coordinator.clear(ROOM, ROOT) is False
        assert remove.await_count == 2
        assert await coordinator.clear("!elsewhere:localhost", ROOT) is False
        assert remove.await_count == 2


@pytest.mark.asyncio
async def test_clear_raises_unavailable_on_other_tag_errors(tmp_path: Path) -> None:
    """A tag write that failed for another reason is retried later, not handed to the next bot."""
    router, code = _bot(ROOM), _bot(ROOM)
    coordinator = _coordinator(_config(tmp_path), {"code": code, "router": router})

    with (
        patch(f"{_MODULE}.remove_thread_tag", new=AsyncMock(side_effect=ThreadTagsError("down"))) as remove,
        pytest.raises(SnoozeUnavailableError),
    ):
        await coordinator.clear(ROOM, ROOT)
    assert remove.await_count == 1


@pytest.mark.asyncio
async def test_send_ended_mentions_only_the_snoozer_and_wakes_no_agent(tmp_path: Path) -> None:
    """The wake message goes into the thread, mentions only the snoozer, and triggers no agent."""
    router, code = _bot(ROOM), _bot(ROOM)
    coordinator = _coordinator(_config(tmp_path), {"code": code, "router": router})
    snooze = Snooze(room_id=ROOM, thread_root_id=ROOT, until=UNTIL, note="note", mention=PERSON)
    extra_content = {"io.mindroom.thread_snooze": {"event": "ended", "until": "2026-11-14T08:00:00+00:00"}}

    assert await coordinator.send_ended(snooze) is True
    router.hook_context.send_message.assert_awaited_once_with(
        ROOM,
        "⏰ Snooze ended: note",
        ROOT,
        "thread_snooze",
        extra_content,
        trigger_dispatch=False,
        mentions=[PERSON],
        wake_mentioned=False,
    )
    code.hook_context.send_message.assert_not_awaited()

    router.hook_context.send_message.reset_mock()
    assert await coordinator.send_ended(Snooze(ROOM, ROOT, UNTIL, None, None)) is True
    router.hook_context.send_message.assert_awaited_once_with(
        ROOM,
        "⏰ Snooze ended",
        ROOT,
        "thread_snooze",
        extra_content,
        trigger_dispatch=False,
        mentions=[],
        wake_mentioned=False,
    )


@pytest.mark.asyncio
async def test_send_ended_is_false_when_the_message_is_not_delivered(tmp_path: Path) -> None:
    """An undelivered, failed, or bot-less wake message reports False so the runtime retries it."""
    router = _bot(ROOM)
    coordinator = _coordinator(_config(tmp_path), {"router": router})
    snooze = Snooze(room_id=ROOM, thread_root_id=ROOT, until=UNTIL, note=None, mention=None)

    router.hook_context.send_message = AsyncMock(return_value=None)
    assert await coordinator.send_ended(snooze) is False
    router.hook_context.send_message = AsyncMock(side_effect=RuntimeError("transport"))
    assert await coordinator.send_ended(snooze) is False
    router.client.rooms = {}
    assert await coordinator.send_ended(snooze) is False


@pytest.mark.asyncio
async def test_wake_message_is_an_ordinary_text_message(tmp_path: Path) -> None:
    """Push rules and MindRoom Chat's Activity count the wake message, so it is m.text in the thread."""
    config = _config(tmp_path)
    runtime_paths = runtime_paths_for(config)
    sent: list[dict[str, Any]] = []

    async def send_message_result(
        _client: object,
        _room_id: str,
        content: dict[str, Any],
        **_kwargs: object,
    ) -> DeliveredMatrixEvent:
        sent.append(content)
        return DeliveredMatrixEvent(event_id="$wake", content_sent=content)

    conversation_reader = make_conversation_reader_mock()
    conversation_reader.latest_thread_event_id = AsyncMock(return_value="$latest")
    router = _bot(ROOM)
    router.hook_context.send_message = build_hook_message_sender(
        MagicMock(),
        config,
        runtime_paths,
        conversation_reader=conversation_reader,
    )
    coordinator = _coordinator(config, {"router": router})
    with patch("mindroom.matrix.client_delivery.send_message_result", new=send_message_result):
        assert await coordinator.send_ended(Snooze(ROOM, ROOT, UNTIL, "check CI", PERSON)) is True

    [content] = sent
    assert content["msgtype"] == "m.text"
    assert content["body"] == "⏰ Snooze ended: check CI"
    assert content["m.mentions"] == {"user_ids": [PERSON]}
    assert content["m.relates_to"]["rel_type"] == "m.thread"
    assert content["m.relates_to"]["event_id"] == ROOT
    assert content["io.mindroom.wake_mentioned"] is False


@pytest.mark.asyncio
async def test_sync_starts_the_wake_loop_and_stop_ends_it(tmp_path: Path) -> None:
    """The coordinator follows the runtime service lifecycle."""
    coordinator = _coordinator(_config(tmp_path), {})

    await coordinator.sync(_config(tmp_path))
    assert coordinator._loop.running
    await coordinator.stop()
    assert not coordinator._loop.running


def _tag_event(source: dict[str, object], *, decrypted: bool = False) -> nio.Event:
    event = nio.Event.parse_event(source)
    event.decrypted = decrypted
    return event


def test_thread_tag_state_event_parses_as_unknown_event() -> None:
    """Nio has no class for thread tag state events, so the bot listens for UnknownEvent."""
    event = nio.Event.parse_event(_snooze_source())

    assert isinstance(event, nio.UnknownEvent)
    assert event.type == THREAD_TAGS_EVENT_TYPE
    assert "state_key" in event.source


def test_decrypted_thread_tag_event_is_ignored() -> None:
    """Only clear state events count: a decrypted look-alike or a non-state event is not a snooze."""
    bot = MagicMock()
    room = nio.MatrixRoom(ROOM, "@mindroom_router:localhost")
    observe = bot.orchestrator.thread_snooze.observe_state_event

    AgentBot._on_thread_tag_state(bot, room, _tag_event(_snooze_source(), decrypted=True))
    timeline_source = {key: value for key, value in _snooze_source().items() if key != "state_key"}
    AgentBot._on_thread_tag_state(bot, room, _tag_event(timeline_source))
    AgentBot._on_thread_tag_state(bot, room, _tag_event({**_snooze_source(), "type": "com.example.other"}))
    observe.assert_not_called()

    AgentBot._on_thread_tag_state(bot, room, _tag_event(_snooze_source()))
    observe.assert_called_once_with(ROOM, _snooze_source())


def test_thread_tag_callback_never_raises_into_nio() -> None:
    """A failure while indexing a live tag event is logged, never raised into nio's callback loop."""
    bot = MagicMock()
    bot.orchestrator.thread_snooze.observe_state_event.side_effect = RuntimeError("boom")

    AgentBot._on_thread_tag_state(bot, nio.MatrixRoom(ROOM, "@mindroom_router:localhost"), _tag_event(_snooze_source()))

    bot.logger.exception.assert_called_once()


@pytest.mark.asyncio
async def test_joining_a_room_restores_its_schedules_and_snoozes() -> None:
    """A room a bot joins hands both its scheduled tasks and its snoozes to the runtime."""
    bot = MagicMock()
    bot.orchestrator.scheduled_tasks.restore_room = AsyncMock()
    bot.orchestrator.thread_snooze.restore_room = AsyncMock()

    await AgentBot._restore_joined_room(bot, ROOM)

    bot.orchestrator.scheduled_tasks.restore_room.assert_awaited_once_with(bot, ROOM)
    bot.orchestrator.thread_snooze.restore_room.assert_awaited_once_with(bot, ROOM)


def test_orchestrator_runs_the_coordinator_as_a_service(tmp_path: Path) -> None:
    """The orchestrator exposes the coordinator and starts and stops it with its other services."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path), config=Config())

    assert isinstance(orchestrator.thread_snooze, ThreadSnoozeRuntimeCoordinator)
    assert orchestrator.thread_snooze in orchestrator._services
