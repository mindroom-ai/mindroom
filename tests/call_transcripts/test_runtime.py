"""The call transcript runtime: the service, the router it posts through, and the loop that checks its calls."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import nio
import pytest

from mindroom.call_transcripts.service import CallRefusedError, CallTranscriptService
from mindroom.call_transcripts.store import CallTranscriptStore
from mindroom.config.calls import CallsConfig, CallTranscriptsConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.orchestration.call_transcripts_runtime import CallTranscriptsRuntimeCoordinator
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths

ROOM = "!room:example.org"
ANN = "@ann:example.org"


def _config(*, enabled: bool) -> Config:
    return Config(calls=CallsConfig(transcripts=CallTranscriptsConfig(enabled=enabled)))


def _router(*, running: bool = True, synced: bool = True) -> MagicMock:
    """A router bot whose client is in ``ROOM`` and finds Ann joined to it."""
    client = MagicMock(spec=nio.AsyncClient)
    client.rooms = {ROOM: nio.MatrixRoom(ROOM, "@mindroom_router:example.org")}

    async def state(room_id: str) -> nio.RoomGetStateResponse:
        joined = {"type": "m.room.member", "state_key": ANN, "sender": ANN, "content": {"membership": "join"}}
        return nio.RoomGetStateResponse(events=[joined], room_id=room_id)

    client.room_get_state.side_effect = state
    return MagicMock(client=client, running=running, first_sync_complete=synced)


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
) -> CallTranscriptsRuntimeCoordinator:
    return CallTranscriptsRuntimeCoordinator(runtime_paths=paths, config_provider=config, sleep=sleeps)


def _bots(router: MagicMock) -> dict[str, AgentBot]:
    return {ROUTER_AGENT_NAME: router}


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

    coordinator.bind_if_ready(config, _bots(router))
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
async def test_binding_waits_for_a_running_router_and_for_call_transcripts_to_be_on(tmp_path: Path) -> None:
    """Nothing binds without a running router, or while off; a later bind replaces a stopped router."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)
    coordinator = _coordinator(paths, lambda: config, _Sleeps())
    service = coordinator.service()
    assert service is not None

    coordinator.bind_if_ready(config, {})
    coordinator.bind_if_ready(config, _bots(_router(running=False)))
    coordinator.bind_if_ready(_config(enabled=False), _bots(_router()))
    assert await _room_cannot_be_checked(service)

    coordinator.bind_if_ready(config, _bots(_router()))
    assert (await service.live(ROOM, ANN)).call is None
    coordinator.bind_if_ready(_config(enabled=False), _bots(_router()))
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
    coordinator.bind_if_ready(config, _bots(router))
    service = coordinator.service()
    assert service is not None
    call = await service.join(ROOM, ANN)
    async with asyncio.timeout(5):
        await sending.wait()
    [write] = service._settling.values()

    config = _config(enabled=False)
    assert await service._room.post_row(ROOM, call.id) is None
    coordinator.bind_if_ready(config, _bots(router))
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

    coordinator.bind_if_ready(config, _bots(_router()))
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
async def test_while_off_the_loop_gives_up_what_was_not_posted_and_reads_and_writes_nothing_in_matrix(
    tmp_path: Path,
) -> None:
    """Off from the start, the loop still runs: it ends calls and drops lines kept from before, with no Matrix call."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=False)
    sleeps = _Sleeps()
    coordinator = _coordinator(paths, lambda: config, sleeps)
    router = _router()
    coordinator.bind_if_ready(config, _bots(router))
    await sleeps.until(lambda: len(sleeps.delays) >= 2)
    assert not (paths.storage_root / "call_transcripts").exists()

    store = CallTranscriptStore(paths.storage_root)
    heard_at = time.time() - 30
    call, _ = store.ensure_live_call(ROOM, at=heard_at)
    store.record_presence(call.id, ANN, at=heard_at)
    store.add_speech(call.id, user_id=ANN, said_at=heard_at, seconds=2.0, text="Hello there")
    waited = len(sleeps.delays)
    await sleeps.until(lambda: len(sleeps.delays) >= waited + 2)
    await coordinator.stop()

    ended = store.call(call.id)
    assert ended is not None
    assert (ended.ended_at, ended.transcript_event_id) == (heard_at, None)
    assert ended.transcript_done_at is not None
    assert store.lines(call.id) == []
    assert router.client.mock_calls == []
    assert sleeps.delays[-2:] == [15.0, 15.0]


@pytest.mark.asyncio
async def test_off_and_back_on_between_two_passes_still_ends_calls_and_gives_up_what_was_not_posted(
    tmp_path: Path,
) -> None:
    """Turned off and on again within one wait, the next pass still holds the store to having been off."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)
    slept = asyncio.Event()
    woken = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        slept.set()
        await woken.wait()
        woken.clear()

    coordinator = CallTranscriptsRuntimeCoordinator(runtime_paths=paths, config_provider=lambda: config, sleep=sleep)
    store = CallTranscriptStore(paths.storage_root)
    heard_at = time.time() - 5
    call, _ = store.ensure_live_call(ROOM, at=heard_at)
    store.record_presence(call.id, ANN, at=heard_at)
    store.add_speech(call.id, user_id=ANN, said_at=heard_at, seconds=2.0, text="Hello there")
    coordinator.bind_if_ready(config, {})
    async with asyncio.timeout(5):
        await slept.wait()
    assert store.lines(call.id) != []

    config = _config(enabled=False)
    coordinator.bind_if_ready(config, {})
    config = _config(enabled=True)
    coordinator.bind_if_ready(config, {})
    slept.clear()
    woken.set()
    async with asyncio.timeout(5):
        await slept.wait()
    await coordinator.stop()

    ended = store.call(call.id)
    assert ended is not None
    assert (ended.ended_at, ended.transcript_event_id) == (heard_at, None)
    assert ended.transcript_done_at is not None
    assert store.lines(call.id) == []


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

    coordinator.bind_if_ready(config, _bots(_router()))
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
    coordinator.bind_if_ready(config, _bots(_router()))
    await sleeps.until(lambda: len(ticks) >= 1)

    await coordinator.stop()
    stopped_at = len(ticks)
    for _ in range(20):
        await asyncio.sleep(0)

    assert len(ticks) == stopped_at
    service = coordinator.service()
    assert service is not None
    assert await _room_cannot_be_checked(service)


# Wiring in the orchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize(("entities", "unbound"), [({ROUTER_AGENT_NAME, "general"}, True), ({"general"}, False)])
async def test_the_orchestrator_unbinds_call_transcripts_only_when_the_router_changes(
    tmp_path: Path,
    entities: set[str],
    *,
    unbound: bool,
) -> None:
    """A router replacement unbinds before the router stops; another entity's restart leaves calls be."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path), config=Config())

    with patch.object(orchestrator._call_transcripts_runtime, "unbind") as unbind:
        await orchestrator._entities_changing(entities)

    assert unbind.called is unbound


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


@pytest.mark.asyncio
async def test_an_off_pass_that_fails_is_tried_again_at_the_next_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turned off and on again, an off pass that raises still holds the store to having been off at the next pass."""
    paths = test_runtime_paths(tmp_path)
    config = _config(enabled=True)
    passes = 0
    slept = asyncio.Event()
    woken = asyncio.Event()

    async def sleep(_seconds: float) -> None:
        slept.set()
        await woken.wait()
        woken.clear()

    real_tick_while_off = CallTranscriptService.tick_while_off

    async def tick_while_off_failing_once(service: CallTranscriptService) -> None:
        nonlocal passes
        passes += 1
        if passes == 1:
            msg = "the store could not be read"
            raise OSError(msg)
        await real_tick_while_off(service)

    monkeypatch.setattr(CallTranscriptService, "tick_while_off", tick_while_off_failing_once)
    coordinator = CallTranscriptsRuntimeCoordinator(runtime_paths=paths, config_provider=lambda: config, sleep=sleep)
    store = CallTranscriptStore(paths.storage_root)
    heard_at = time.time() - 5
    call, _ = store.ensure_live_call(ROOM, at=heard_at)
    store.record_presence(call.id, ANN, at=heard_at)
    store.add_speech(call.id, user_id=ANN, said_at=heard_at, seconds=2.0, text="Hello there")
    coordinator.bind_if_ready(config, {})
    async with asyncio.timeout(5):
        await slept.wait()

    config = _config(enabled=False)
    coordinator.bind_if_ready(config, {})
    config = _config(enabled=True)
    coordinator.bind_if_ready(config, {})
    for _ in range(2):
        slept.clear()
        woken.set()
        async with asyncio.timeout(5):
            await slept.wait()
    await coordinator.stop()

    assert passes == 2
    ended = store.call(call.id)
    assert ended is not None
    assert ended.transcript_done_at is not None
    assert store.lines(call.id) == []
