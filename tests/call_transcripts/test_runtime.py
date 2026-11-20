"""The call transcript runtime: the service, the router it posts through, and the loop that checks its calls."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import nio
import pytest

from mindroom.call_transcripts.service import (
    CallRefusedError,
    CallTranscript,
    CallTranscriptService,
    TranscriptMessage,
    _owed_row_edit,
)
from mindroom.call_transcripts.store import CallTranscriptStore
from mindroom.config.calls import CallsConfig, CallTranscriptsConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.orchestration.call_transcripts_runtime import CallTranscriptsRuntimeCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.call_transcripts.test_service import FakeAgentCall
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths

ROOM = "!room:example.org"
OTHER_ROOM = "!other:example.org"
ANN = "@ann:example.org"


def _config(*, enabled: bool) -> Config:
    return Config(calls=CallsConfig(transcripts=CallTranscriptsConfig(enabled=enabled)))


def _router(
    *,
    running: bool = True,
    synced: bool = True,
    user_id: str = "@mindroom_router:example.org",
) -> MagicMock:
    """A bot, the owner account's unless named, answering no calls, whose client is in ``ROOM`` and finds Ann joined."""
    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = user_id
    client.rooms = {ROOM: nio.MatrixRoom(ROOM, client.user_id)}

    async def state(room_id: str) -> nio.RoomGetStateResponse:
        joined = {"type": "m.room.member", "state_key": ANN, "sender": ANN, "content": {"membership": "join"}}
        return nio.RoomGetStateResponse(events=[joined], room_id=room_id)

    client.room_get_state.side_effect = state
    return MagicMock(client=client, running=running, first_sync_complete=synced, call_manager=None)


@dataclass
class _FakeCallManager:
    """An agent's call manager: the rooms it answers calls in, and its call in each room where it is in one."""

    rooms: set[str]
    calls: dict[str, FakeAgentCall] = field(default_factory=dict)

    def watches(self, room_id: str) -> bool:
        """Return whether the agent answers calls in the room."""
        return room_id in self.rooms

    def live_call(self, room_id: str) -> FakeAgentCall | None:
        """Return the agent's call in the room, None while it is in none there."""
        return self.calls.get(room_id)


def _calls_agent(manager: _FakeCallManager) -> MagicMock:
    """An agent bot answering calls, whose client is in ``ROOM`` and finds Ann joined to it."""
    bot = _router()
    bot.call_manager = manager
    return bot


class _Sleeps:
    """The loop's waits: recorded, each one only a turn of the event loop."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._waited = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self._waited.set()
        await asyncio.sleep(0)

    async def until(self, condition: Callable[[], bool]) -> None:
        """Let the loop run, its store work in threads included, until ``condition`` holds; fail after 5 seconds."""
        async with asyncio.timeout(5):
            while not condition():
                self._waited.clear()
                await self._waited.wait()


def _coordinator(
    paths: RuntimePaths,
    config: Callable[[], Config],
    sleeps: _Sleeps,
    bots: Mapping[str, AgentBot] | None = None,
) -> CallTranscriptsRuntimeCoordinator:
    return CallTranscriptsRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=config,
        bots=lambda: bots or {},
        sleep=sleeps,
    )


def _bots(router: MagicMock) -> dict[str, AgentBot]:
    return {ROUTER_AGENT_NAME: router}


def _bind(coordinator: CallTranscriptsRuntimeCoordinator, config: Config, bots: Mapping[str, AgentBot]) -> None:
    """Make ``bots`` the runtime's live bots and bind, as the orchestrator does once they run."""
    coordinator.bots = lambda: bots
    coordinator.bind_if_ready(config, bots)


async def _room_cannot_be_checked(service: CallTranscriptService) -> bool:
    """Return whether the service answers that the room cannot be checked, as it does without a ready router."""
    try:
        await service.live(ROOM, ANN)
    except CallRefusedError as refused:
        return refused.code == "unavailable"
    return False


# The service and the router


def test_there_is_no_service_while_call_transcripts_are_off(tmp_path: Path) -> None:
    """While off, the routes find no service, and no call database is made."""
    paths = test_runtime_paths(tmp_path)
    coordinator = _coordinator(paths, lambda: _config(enabled=False), _Sleeps())

    assert coordinator.service() is None
    assert not (paths.storage_root / "call_transcripts").exists()


def test_while_on_the_routes_always_reach_the_same_service(tmp_path: Path) -> None:
    """The service is made once it is on and kept, so what it holds in memory about live calls stays with it."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)
    coordinator = _coordinator(paths, lambda: config, _Sleeps())

    service = coordinator.service()

    assert isinstance(service, CallTranscriptService)
    assert coordinator.service() is service
    config = _config(enabled=False)
    assert coordinator.service() is None
    config = _config(enabled=True)
    assert coordinator.service() is service


@pytest.mark.asyncio
async def test_the_service_reads_rooms_through_the_router_only_while_it_is_bound_and_synced(tmp_path: Path) -> None:
    """Before binding, after unbinding, and before the router's first sync, the room cannot be checked."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)
    coordinator = _coordinator(paths, lambda: config, _Sleeps())
    service = coordinator.service()
    assert service is not None
    router = _router(synced=False)

    assert await _room_cannot_be_checked(service)

    _bind(coordinator, config, _bots(router))
    assert await _room_cannot_be_checked(service)

    router.first_sync_complete = True
    live = await service.live(ROOM, ANN)
    assert live.call is None

    router.running = False
    assert await _room_cannot_be_checked(service)

    router.running = True
    coordinator.unbind()
    assert await _room_cannot_be_checked(service)
    await coordinator.stop()


@pytest.mark.asyncio
async def test_a_calls_thread_is_titled_again_by_the_bound_router_only(tmp_path: Path) -> None:
    """The router titles the thread now, whatever its count, as after its own replies; unbound, nobody does."""
    config = _config(enabled=True)
    coordinator = _coordinator(test_runtime_paths(tmp_path), lambda: config, _Sleeps())
    service = coordinator.service()
    assert service is not None
    router = _router()
    transcript = CallTranscript(call_id="call1", room_id=ROOM, duration_seconds=60, turns=(), names={})
    message = TranscriptMessage(
        room_id=ROOM,
        call_id="call1",
        row_event_id="$row",
        row_sender=None,
        body="We planned the launch.",
        transcript=transcript,
    )

    service._room.title_thread(message, "$notes")
    _bind(coordinator, config, _bots(router))
    service._room.title_thread(message, "$notes")
    coordinator.unbind()
    service._room.title_thread(message, "$notes")
    await coordinator.stop()

    router.post_response_effects.retitle_thread.assert_called_once_with(
        ROOM,
        "$row",
        event_id="$notes",
        body="We planned the launch.",
    )


@pytest.mark.asyncio
async def test_a_room_without_the_owner_account_is_read_and_posted_to_by_its_agent(tmp_path: Path) -> None:
    """With one person, one agent and no owner account in the room, the agent posts the row and titles its thread."""
    config = _config(enabled=True)
    coordinator = _coordinator(test_runtime_paths(tmp_path), lambda: config, _Sleeps())
    service = coordinator.service()
    assert service is not None
    agent = _router(user_id="@mindroom_code:example.org")
    elsewhere = _router()
    elsewhere.client.rooms = {}
    agent.client.room_send.return_value = nio.RoomSendResponse("$row", ROOM)
    _bind(coordinator, config, {ROUTER_AGENT_NAME: elsewhere, "code": agent})

    assert service._room.poster_joined(ROOM) is True
    assert (await service.live(ROOM, ANN)).call is None
    assert await service._room.post_row(ROOM, "call1") == ("$row", "@mindroom_code:example.org")
    service._room.retitle(ROOM, "$row", "$notes", "We planned the launch.")
    assert service._room.poster_joined("!nobody:example.org") is False
    await coordinator.stop()

    agent.client.room_send.assert_awaited_once()
    agent.post_response_effects.retitle_thread.assert_called_once()
    elsewhere.client.room_send.assert_not_awaited()
    elsewhere.client.room_get_state.assert_not_awaited()


async def _writes_made(service: CallTranscriptService) -> None:
    """Wait until the service has made every Matrix write it started."""
    async with asyncio.timeout(5):
        while service._settling:
            await asyncio.gather(*service._settling.values())


def _replaced_event_ids(bot: MagicMock) -> list[str]:
    """Return the event each edit the bot sent replaces."""
    contents = [call.kwargs["content"] for call in bot.client.room_send.await_args_list]
    return [content["m.relates_to"]["event_id"] for content in contents if "m.new_content" in content]


@pytest.mark.asyncio
async def test_a_calls_row_is_edited_by_the_account_that_posted_it_or_by_none(tmp_path: Path) -> None:
    """Matrix ignores an edit from another account, so a row's edits never go out through the room's next poster."""
    config = _config(enabled=True)
    coordinator = _coordinator(test_runtime_paths(tmp_path), lambda: config, _Sleeps())
    service = coordinator.service()
    assert service is not None
    code = _router(user_id="@mindroom_code:example.org")
    code.client.room_send.return_value = nio.RoomSendResponse("$row", ROOM)
    bots: dict[str, AgentBot] = {"code": code}
    _bind(coordinator, config, bots)
    call = await service.join(ROOM, ANN)
    await _writes_made(service)
    assert code.client.room_send.await_count == 1

    # An agent with a lower Matrix ID joins, and posts for the room from now on.
    newcomer = _router(user_id="@mindroom_alpha:example.org")
    newcomer.client.room_send.return_value = nio.RoomSendResponse("$other", ROOM)
    bots["alpha"] = newcomer
    await service.leave(call.id, ANN)
    await _writes_made(service)

    assert _replaced_event_ids(newcomer) == []
    assert _replaced_event_ids(code) == ["$row"]

    # With the row's account stopped, its next edit waits rather than going out through the newcomer.
    code.running = False
    stored = await asyncio.to_thread(service._store.call, call.id)
    assert stored is not None
    assert stored.row_sender == "@mindroom_code:example.org"
    linked = replace(_owed_row_edit(replace(stored, row_edit=None)), transcript_event_id="$transcript")
    assert await service._room.edit_row(linked) is None
    assert _replaced_event_ids(newcomer) == []
    await coordinator.stop()


@pytest.mark.asyncio
async def test_a_bot_that_never_syncs_holds_back_only_the_rooms_no_synced_bot_is_in(tmp_path: Path) -> None:
    """A room whose steward and poster synced is read and posted to; a room without them is unknown, not empty."""
    config = _config(enabled=True)
    coordinator = _coordinator(test_runtime_paths(tmp_path), lambda: config, _Sleeps())
    service = coordinator.service()
    assert service is not None
    agent = _router(user_id="@mindroom_code:example.org")
    stuck = _router(synced=False)
    stuck.client.rooms = {}
    agent.client.room_send.return_value = nio.RoomSendResponse("$row", ROOM)
    _bind(coordinator, config, {ROUTER_AGENT_NAME: stuck, "code": agent})

    assert service._room.poster_joined(ROOM) is True
    assert (await service.live(ROOM, ANN)).call is None
    assert await service._room.post_row(ROOM, "call1") == ("$row", "@mindroom_code:example.org")
    assert service._room.poster_joined("!nobody:example.org") is None
    stuck.first_sync_complete = True
    assert service._room.poster_joined("!nobody:example.org") is False
    await coordinator.stop()


@pytest.mark.asyncio
async def test_binding_waits_for_a_running_bot_and_for_call_transcripts_to_be_on(tmp_path: Path) -> None:
    """Nothing binds without a running bot, or while off; a later bind replaces a stopped bot."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)
    coordinator = _coordinator(paths, lambda: config, _Sleeps())
    service = coordinator.service()
    assert service is not None

    _bind(coordinator, config, {})
    _bind(coordinator, config, _bots(_router(running=False)))
    _bind(coordinator, _config(enabled=False), _bots(_router()))
    assert await _room_cannot_be_checked(service)

    _bind(coordinator, config, _bots(_router()))
    assert (await service.live(ROOM, ANN)).call is None
    _bind(coordinator, _config(enabled=False), _bots(_router()))
    assert await _room_cannot_be_checked(service)
    await coordinator.stop()


@pytest.mark.asyncio
async def test_turning_call_transcripts_off_stops_the_writes_under_way_and_sends_no_more(tmp_path: Path) -> None:
    """Off, no write is sent from then on, and one under way is cancelled once the runtime rebinds, before any pass."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)

    async def no_next_pass(_seconds: float) -> None:
        await asyncio.Event().wait()

    coordinator = CallTranscriptsRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bots=dict,
        sleep=no_next_pass,
    )
    router = _router()
    sending = asyncio.Event()

    async def room_send(*_args: object, **_kwargs: object) -> nio.RoomSendResponse:
        """Never answer the first send; answer the others at once."""
        if not sending.is_set():
            sending.set()
            await asyncio.Event().wait()
        return nio.RoomSendResponse("$sent", ROOM)

    router.client.room_send.side_effect = room_send
    _bind(coordinator, config, _bots(router))
    service = coordinator.service()
    assert service is not None
    call = await service.join(ROOM, ANN)
    async with asyncio.timeout(5):
        await sending.wait()
    [write] = service._settling.values()

    config = _config(enabled=False)
    assert await service._room.post_row(ROOM, call.id) is None
    _bind(coordinator, config, _bots(router))
    await asyncio.wait([write], timeout=5)

    assert write.cancelled()
    assert router.client.room_send.await_count == 1
    await coordinator.stop()


# The loop


@pytest.mark.asyncio
async def test_the_loop_ticks_while_on_idles_while_off_and_reads_the_config_each_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each pass reads the settings: while off it waits 15 s without a tick; while on it waits what the tick asks."""
    paths = test_runtime_paths(tmp_path)
    ticks: list[float] = []

    async def tick(_service: CallTranscriptService) -> float:
        ticks.append(len(ticks))
        return 4.0

    monkeypatch.setattr(CallTranscriptService, "tick", tick)
    config = _config(enabled=True)
    sleeps = _Sleeps()
    coordinator = _coordinator(paths, lambda: config, sleeps)

    _bind(coordinator, config, _bots(_router()))
    await sleeps.until(lambda: len(ticks) >= 2)
    assert sleeps.delays[:2] == [4.0, 4.0]

    config = _config(enabled=False)
    ticked = len(ticks)
    waited = len(sleeps.delays)
    await sleeps.until(lambda: len(sleeps.delays) >= waited + 3)
    assert len(ticks) <= ticked + 1
    assert sleeps.delays[-2:] == [15.0, 15.0]

    config = _config(enabled=True)
    ticked = len(ticks)
    await sleeps.until(lambda: len(ticks) >= ticked + 2)
    await coordinator.stop()


@pytest.mark.asyncio
async def test_while_off_the_loop_holds_the_store_to_a_day_and_reads_and_writes_nothing_in_matrix(
    tmp_path: Path,
) -> None:
    """Off from the start, the loop still runs: a call not heard from for a day ends and its lines go, others stay."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=False)
    sleeps = _Sleeps()
    coordinator = _coordinator(paths, lambda: config, sleeps)
    router = _router()
    _bind(coordinator, config, _bots(router))
    await sleeps.until(lambda: len(sleeps.delays) >= 2)
    assert not (paths.storage_root / "call_transcripts").exists()

    store = CallTranscriptStore(paths.storage_root)
    calls: dict[str, tuple[str, float]] = {}
    for room_id, heard_at in ((ROOM, time.time() - 86_430), (OTHER_ROOM, time.time() - 30)):
        call, _ = store.ensure_live_call(room_id, at=heard_at)
        store.record_presence(call.id, ANN, at=heard_at)
        store.add_speech(call.id, user_id=ANN, said_at=heard_at, seconds=2.0, text="Hello there")
        calls[room_id] = (call.id, heard_at)
    waited = len(sleeps.delays)
    await sleeps.until(lambda: len(sleeps.delays) >= waited + 2)
    await coordinator.stop()

    lapsed_id, lapsed_at = calls[ROOM]
    lapsed = store.call(lapsed_id)
    assert lapsed is not None
    assert (lapsed.ended_at, lapsed.transcript_event_id) == (lapsed_at, None)
    assert lapsed.transcript_done_at is not None
    assert store.lines(lapsed_id) == []
    recent_id, _ = calls[OTHER_ROOM]
    recent = store.call(recent_id)
    assert recent is not None
    assert recent.ended_at is None
    assert len(store.lines(recent_id)) == 1
    assert router.client.mock_calls == []
    assert sleeps.delays[-2:] == [15.0, 15.0]


@pytest.mark.asyncio
async def test_off_and_back_on_between_two_passes_ends_nothing(tmp_path: Path) -> None:
    """Turned off and on again within one wait, the next pass goes on as if it had stayed on: every call keeps its lines."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)
    slept = asyncio.Event()
    woken = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        slept.set()
        await woken.wait()
        woken.clear()

    coordinator = CallTranscriptsRuntimeCoordinator(
        runtime_paths=paths,
        config_provider=lambda: config,
        bots=dict,
        sleep=sleep,
    )
    store = CallTranscriptStore(paths.storage_root)

    def start_call(room_id: str) -> str:
        heard_at = time.time() - 5
        call, _ = store.ensure_live_call(room_id, at=heard_at)
        store.record_presence(call.id, ANN, at=heard_at)
        store.add_speech(call.id, user_id=ANN, said_at=heard_at, seconds=2.0, text="Hello there")
        return call.id

    before = start_call(ROOM)
    _bind(coordinator, config, {})
    async with asyncio.timeout(5):
        await slept.wait()

    config = _config(enabled=False)
    _bind(coordinator, config, {})
    config = _config(enabled=True)
    _bind(coordinator, config, {})
    after = start_call(OTHER_ROOM)
    slept.clear()
    woken.set()
    async with asyncio.timeout(5):
        await slept.wait()
    await coordinator.stop()

    for call_id in (before, after):
        stored = store.call(call_id)
        assert stored is not None
        assert stored.ended_at is None
        assert len(store.lines(call_id)) == 1


@pytest.mark.asyncio
async def test_a_failing_pass_never_stops_the_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass that raises is logged; the loop waits 15 s and goes on."""
    paths = test_runtime_paths(tmp_path)
    ticks: list[int] = []

    async def tick(_service: CallTranscriptService) -> float:
        ticks.append(1)
        if len(ticks) == 1:
            msg = "A bug in a pass."
            raise RuntimeError(msg)
        return 2.0

    monkeypatch.setattr(CallTranscriptService, "tick", tick)
    config = _config(enabled=True)
    sleeps = _Sleeps()
    coordinator = _coordinator(paths, lambda: config, sleeps)

    _bind(coordinator, config, _bots(_router()))
    await sleeps.until(lambda: len(ticks) >= 3)

    assert sleeps.delays[:2] == [15.0, 2.0]
    await coordinator.stop()


@pytest.mark.asyncio
async def test_stopping_stops_the_service_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop cancels the transcript messages the service is posting, once the loop that starts them has stopped."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)
    coordinator = _coordinator(paths, lambda: config, _Sleeps())
    service = coordinator.service()
    assert service is not None
    stopped: list[CallTranscriptService] = []

    async def stop(stopping: CallTranscriptService) -> None:
        stopped.append(stopping)

    monkeypatch.setattr(CallTranscriptService, "stop", stop)

    await coordinator.stop()

    assert stopped == [service]


@pytest.mark.asyncio
async def test_stopping_ends_the_loop_and_unbinds_the_router(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a stop no pass runs and the room cannot be checked."""
    paths = test_runtime_paths(tmp_path)
    ticks: list[int] = []

    async def tick(_service: CallTranscriptService) -> float:
        ticks.append(1)
        return 1.0

    monkeypatch.setattr(CallTranscriptService, "tick", tick)
    config = _config(enabled=True)
    sleeps = _Sleeps()
    coordinator = _coordinator(paths, lambda: config, sleeps)
    _bind(coordinator, config, _bots(_router()))
    await sleeps.until(lambda: len(ticks) >= 1)

    await coordinator.stop()
    stopped_at = len(ticks)
    for _ in range(20):
        await asyncio.sleep(0)

    assert len(ticks) == stopped_at
    service = coordinator.service()
    assert service is not None
    assert await _room_cannot_be_checked(service)


# Agent calls


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_a_rooms_agent_call_is_read_through_the_agent_answering_calls_there(
    tmp_path: Path,
    *,
    enabled: bool,
) -> None:
    """The agent in a call in the room reads it, without the router and whether call transcripts are on or off."""
    paths = test_runtime_paths(tmp_path)
    call = FakeAgentCall(spoken=((ANN, "Hello", 1_001.0),))
    bots: dict[str, AgentBot] = {
        ROUTER_AGENT_NAME: _router(running=False),
        "idle": _calls_agent(_FakeCallManager({ROOM, OTHER_ROOM})),
        "helper": _calls_agent(_FakeCallManager({ROOM}, {ROOM: call})),
    }
    coordinator = _coordinator(paths, lambda: _config(enabled=enabled), _Sleeps(), bots)

    in_call = coordinator.agent_call_room(ROOM)
    idle = coordinator.agent_call_room(OTHER_ROOM)

    assert in_call is not None
    assert in_call.call is call
    live = await in_call.live(ANN)
    assert live.call is not None
    assert (live.call.id, [line.text for line in live.lines]) == ("agentcall", ["Hello"])
    assert idle is not None
    assert idle.call is None
    assert coordinator.agent_call_room("!nobody:example.org") is None


@pytest.mark.asyncio
async def test_an_agent_call_cannot_be_read_while_its_agent_is_not_ready(tmp_path: Path) -> None:
    """Until the agent runs and has synced, nobody can be told whether they are in its room."""
    paths = test_runtime_paths(tmp_path)
    agent = _calls_agent(_FakeCallManager({ROOM}))
    coordinator = _coordinator(paths, lambda: _config(enabled=False), _Sleeps(), {"helper": agent})
    agent.first_sync_complete = False

    agent_room = coordinator.agent_call_room(ROOM)

    assert agent_room is not None
    with pytest.raises(CallRefusedError) as refused:
        await agent_room.live(ANN)
    assert refused.value.code == "unavailable"


@pytest.mark.asyncio
async def test_nobody_is_a_member_of_a_room_the_calls_agent_is_not_in(tmp_path: Path) -> None:
    """A room missing from the agent's own rooms is answered at once, without asking the homeserver for its state."""
    agent = _calls_agent(_FakeCallManager({ROOM, OTHER_ROOM}))
    paths = test_runtime_paths(tmp_path)
    coordinator = _coordinator(paths, lambda: _config(enabled=False), _Sleeps(), {"helper": agent})

    left = coordinator.agent_call_room(OTHER_ROOM)

    assert left is not None
    assert await left.room.is_member(OTHER_ROOM, ANN) is False
    assert await left.room.display_names(OTHER_ROOM, [ANN]) is None
    agent.client.room_get_state.assert_not_awaited()
    assert await left.room.is_member(ROOM, ANN) is True


# Wiring in the orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize("entities", [{ROUTER_AGENT_NAME, "general"}, {"general"}])
async def test_the_orchestrator_unbinds_call_transcripts_when_any_entity_changes(
    tmp_path: Path,
    entities: set[str],
) -> None:
    """Any bot may be a room's steward or poster, so calls stop reading and posting before any of them stops."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path), config=Config())

    with patch.object(orchestrator._call_transcripts_runtime, "unbind") as unbind:
        await orchestrator._entities_changing(entities)

    unbind.assert_called_once_with()


def test_the_orchestrator_binds_call_transcripts_with_the_other_router_runtimes(tmp_path: Path) -> None:
    """Call transcripts bind to the router wherever trigger delivery does, once the runtime runs."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path), config=Config())
    orchestrator.running = True

    with patch.object(orchestrator._call_transcripts_runtime, "bind_if_ready") as bind:
        orchestrator._bind_trigger_runtimes()

    bind.assert_called_once_with(orchestrator.config, orchestrator.agent_bots)


@pytest.mark.asyncio
async def test_the_orchestrator_stops_call_transcripts_on_shutdown(tmp_path: Path) -> None:
    """Shutdown stops the loop and unbinds the router."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path), config=Config())

    with patch.object(orchestrator._call_transcripts_runtime, "stop") as stop:
        await orchestrator.stop()

    stop.assert_awaited_once()


def test_the_api_reaches_the_running_service_through_what_the_runtime_lends_it(tmp_path: Path) -> None:
    """The dashboard API gets the service while call transcripts are on, and nothing while they are off."""
    paths = test_runtime_paths(tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=paths, config=_config(enabled=False))
    lent = orchestrator.embedded_runtime()

    assert lent.call_transcripts() is None
    orchestrator.config = _config(enabled=True)
    service = lent.call_transcripts()
    assert isinstance(service, CallTranscriptService)
    assert lent.call_transcripts() is service


def test_the_api_finds_agent_calls_among_the_runtimes_current_bots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dashboard API reads the bots the runtime runs at each request, so a restarted agent's calls are found."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path), config=Config())
    lent = orchestrator.embedded_runtime()

    assert lent.agent_call_room(ROOM) is None
    monkeypatch.setattr(orchestrator.fleet, "bots", {"helper": _calls_agent(_FakeCallManager({ROOM}))})
    agent_room = lent.agent_call_room(ROOM)
    assert agent_room is not None
    assert agent_room.call is None
