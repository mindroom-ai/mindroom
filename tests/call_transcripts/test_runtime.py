"""The call transcript runtime: the service, the router it posts through, and the loop that checks its calls."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import nio
import pytest

from mindroom.call_transcripts.service import CallRefusedError, CallTranscriptService
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

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)

    async def until(self, condition: Callable[[], bool]) -> None:
        """Let the loop run until ``condition`` holds."""
        for _ in range(1_000):
            if condition():
                return
            await asyncio.sleep(0)
        msg = "the loop never got there"
        raise AssertionError(msg)


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
    """The service is made once it is on and kept, so live calls and their listeners stay with it."""
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
