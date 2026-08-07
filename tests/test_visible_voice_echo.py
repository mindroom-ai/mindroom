"""Focused tests for cross-entity visible voice echo ordering."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, VISIBLE_ROUTER_VOICE_ECHO_KEY
from mindroom.dispatch_handoff import PreparedTextEvent
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_scope
from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.message_target import MessageTarget
from mindroom.visible_voice_echo import (
    VisibleVoiceEchoDeps,
    VisibleVoiceEchoLifecycle,
    VisibleVoiceEchoRequest,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.delivery_gateway import DeliveryGateway, EditTextRequest, SendTextRequest
    from mindroom.ingress_validation import IngressValidator
    from mindroom.runtime_protocols import OrchestratorRuntime
    from mindroom.turn_store import TurnStore

_ROOM_ID = "!voice:localhost"
_REQUESTER_ID = "@alice:localhost"


@dataclass
class _RecordingEchoGateway:
    """Controllable visible-echo delivery seam."""

    send_result: str | None = "$echo:localhost"
    send_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_send: asyncio.Event = field(default_factory=asyncio.Event)
    block_send: bool = False

    async def send_text(self, request: SendTextRequest) -> str | None:  # noqa: ARG002
        self.send_started.set()
        if self.block_send:
            await self.release_send.wait()
        return self.send_result

    async def edit_text(self, request: EditTextRequest) -> bool:  # noqa: ARG002
        return True


@dataclass
class _EchoTurnStore:
    """Minimal durable-echo state used by the lifecycle."""

    visible_event_ids: dict[str, str] = field(default_factory=dict)

    def visible_echo_for_source(self, source_event_id: str) -> str | None:
        return self.visible_event_ids.get(source_event_id)

    async def record_visible_echo(self, source_event_id: str, echo_event_id: str) -> None:
        self.visible_event_ids[source_event_id] = echo_event_id

    def finalized_visible_echo(self, source_event_id: str) -> None:  # noqa: ARG002
        return None

    async def record_finalized_visible_echo(
        self,
        source_event_id: str,  # noqa: ARG002
        echo_event_id: str,  # noqa: ARG002
        *,
        is_fallback: bool,  # noqa: ARG002
    ) -> None:
        return


@dataclass(frozen=True)
class _RouterIngress:
    """Resolve only the configured router Matrix identity."""

    router_user_id: str

    def managed_entity_name_for_sender(self, sender_id: str, *, include_router: bool = True) -> str | None:
        if include_router and sender_id == self.router_user_id:
            return ROUTER_AGENT_NAME
        return None


@dataclass(frozen=True)
class _RouterReadiness:
    """Expose whether the router runtime completed its first sync."""

    ready: bool

    def entity_first_sync_complete(self, entity_name: str) -> bool | None:
        assert entity_name == ROUTER_AGENT_NAME
        return self.ready


@dataclass(frozen=True)
class _EchoHarness:
    """Router and responder lifecycles sharing one process-global barrier registry."""

    config: Config
    router: VisibleVoiceEchoLifecycle
    responder: VisibleVoiceEchoLifecycle
    room: nio.MatrixRoom
    gateway: _RecordingEchoGateway


def _echo_harness(
    tmp_path: Path,
    *,
    voice_enabled: bool = False,
    router_ready: bool = True,
) -> _EchoHarness:
    config = bind_runtime_paths(
        Config(
            agents={"home": {"display_name": "Home"}},
            authorization={"default_room_access": True},
            voice={"enabled": voice_enabled, "visible_router_echo": True},
        ),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    runtime = BotRuntimeState(
        client=None,
        config=config,
        runtime_paths=runtime_paths,
        enable_streaming=True,
        orchestrator=cast("OrchestratorRuntime", _RouterReadiness(router_ready)),
    )
    router_user_id = entity_identity_registry(config, runtime_paths).current_id(ROUTER_AGENT_NAME).full_id
    ingress = _RouterIngress(router_user_id)
    gateway = _RecordingEchoGateway()
    turn_store = _EchoTurnStore()

    def lifecycle(agent_name: str) -> VisibleVoiceEchoLifecycle:
        return VisibleVoiceEchoLifecycle(
            VisibleVoiceEchoDeps(
                runtime=runtime,
                logger=get_logger(f"test.visible_voice_echo.{agent_name}"),
                agent_name=agent_name,
                delivery_gateway=cast("DeliveryGateway", gateway),
                turn_store=cast("TurnStore", turn_store),
                ingress=cast("IngressValidator", ingress),
            ),
        )

    room = nio.MatrixRoom(_ROOM_ID, _REQUESTER_ID)
    room.add_member(router_user_id, router_user_id, None)
    room.add_member(_REQUESTER_ID, _REQUESTER_ID, None)
    return _EchoHarness(
        config=config,
        router=lifecycle(ROUTER_AGENT_NAME),
        responder=lifecycle("home"),
        room=room,
        gateway=gateway,
    )


def _request(source_event_id: str) -> VisibleVoiceEchoRequest:
    return VisibleVoiceEchoRequest(
        source_event_id=source_event_id,
        target=MessageTarget.resolve(
            room_id=_ROOM_ID,
            thread_id=source_event_id,
            reply_to_event_id=source_event_id,
        ),
        requester_user_id=_REQUESTER_ID,
        raw_source={"content": {"body": "voice.ogg"}},
    )


def _normalized_event(source_event_id: str) -> PreparedTextEvent:
    return PreparedTextEvent(
        sender=_REQUESTER_ID,
        event_id=source_event_id,
        body="🎤 test transcript",
        source={"content": {"body": "🎤 test transcript"}},
    )


async def _await_responder(harness: _EchoHarness, source_event_id: str) -> bool:
    return await harness.responder.await_publication(
        room=harness.room,
        source_event_id=source_event_id,
        requester_user_id=_REQUESTER_ID,
    )


@pytest.mark.asyncio
async def test_responder_waits_for_claimed_echo_publication(tmp_path: Path) -> None:
    """Removing the claimed-barrier wait would let the responder answer first."""
    harness = _echo_harness(tmp_path)
    source_event_id = "$voice-order"
    handle = harness.router.start(_request(source_event_id))
    assert handle is not None
    harness.gateway.block_send = True

    responder_wait = asyncio.create_task(_await_responder(harness, source_event_id))
    finish = asyncio.create_task(harness.router.finish(handle, _normalized_event(source_event_id)))
    await asyncio.wait_for(harness.gateway.send_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not responder_wait.done()

    harness.gateway.release_send.set()
    await finish
    assert await asyncio.wait_for(responder_wait, timeout=1) is True


@pytest.mark.asyncio
async def test_recovery_adopts_untracked_marked_echo_before_sending(tmp_path: Path) -> None:
    """A hard crash after Matrix delivery must not create a second visible router echo."""
    harness = _echo_harness(tmp_path, voice_enabled=True)
    source_event_id = "$voice-recovery"
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = cast("_RouterIngress", harness.router.deps.ingress).router_user_id
    cast("BotRuntimeState", harness.router.deps.runtime).client = client

    with (
        turn_dispatch_recovery_scope(active=True),
        patch(
            "mindroom.visible_voice_echo.find_response_event_ids_via_room_messages",
            new_callable=AsyncMock,
            return_value=frozenset({"$orphaned-echo"}),
        ) as find_echo,
        patch.object(harness.gateway, "send_text", new_callable=AsyncMock) as send_text,
        patch.object(harness.gateway, "edit_text", new_callable=AsyncMock, return_value=True) as edit_text,
    ):
        handle = harness.router.start(_request(source_event_id))
        assert handle is not None
        await harness.router.finish(handle, _normalized_event(source_event_id))

    send_text.assert_not_awaited()
    edit_text.assert_awaited_once()
    assert edit_text.await_args.args[0].event_id == "$orphaned-echo"
    assert harness.router.deps.turn_store.visible_echo_for_source(source_event_id) == "$orphaned-echo"
    response_filter = find_echo.await_args.kwargs["response_source_filter"]
    assert response_filter({"content": {VISIBLE_ROUTER_VOICE_ECHO_KEY: True}})
    assert not response_filter({"content": {}})


@pytest.mark.asyncio
async def test_unclaimed_expected_echo_fails_closed_after_grace(tmp_path: Path) -> None:
    """Returning True after claim grace would reproduce answer-before-echo ordering."""
    harness = _echo_harness(tmp_path)

    assert await _await_responder(harness, "$voice-late-router") is False


@pytest.mark.asyncio
async def test_responder_skips_barrier_when_visible_echo_is_disabled(tmp_path: Path) -> None:
    """A disabled echo must not add ordering delay."""
    harness = _echo_harness(tmp_path)
    harness.config.voice.visible_router_echo = False

    assert await _await_responder(harness, "$voice-disabled") is True


@pytest.mark.asyncio
async def test_responder_skips_barrier_when_router_is_absent(tmp_path: Path) -> None:
    """A room without the router must not wait for an impossible claim."""
    harness = _echo_harness(tmp_path)
    router_user_id = next(
        user_id
        for user_id in harness.room.users
        if harness.responder.deps.ingress.managed_entity_name_for_sender(user_id) == ROUTER_AGENT_NAME
    )
    harness.room.remove_member(router_user_id)

    assert await _await_responder(harness, "$voice-no-router") is True


@pytest.mark.asyncio
async def test_responder_skips_barrier_when_router_runtime_is_not_ready(tmp_path: Path) -> None:
    """Room membership alone must not make an inactive router block voice turns."""
    harness = _echo_harness(tmp_path, router_ready=False)

    assert await _await_responder(harness, "$voice-router-down") is True


@pytest.mark.asyncio
async def test_abandoned_router_lifecycle_releases_responder(tmp_path: Path) -> None:
    """A router turn dying before settle must release its responder waiter."""
    harness = _echo_harness(tmp_path)
    source_event_id = "$voice-abandoned"
    handle = harness.router.start(_request(source_event_id))
    assert handle is not None
    responder_wait = asyncio.create_task(_await_responder(harness, source_event_id))
    await asyncio.sleep(0)

    harness.router.abandon_unsettled(handle)

    assert await asyncio.wait_for(responder_wait, timeout=1) is False


@pytest.mark.asyncio
async def test_router_canonical_turn_does_not_wait_on_its_own_echo(tmp_path: Path) -> None:
    """Gating the router on a failed best-effort echo would suppress canonical dispatch."""
    harness = _echo_harness(tmp_path)
    source_event_id = "$voice-router"
    harness.gateway.send_result = None
    handle = harness.router.start(_request(source_event_id))
    assert handle is not None
    await harness.router.finish(handle, _normalized_event(source_event_id))

    assert (
        await harness.router.await_publication(
            room=harness.room,
            source_event_id=source_event_id,
            requester_user_id=_REQUESTER_ID,
        )
        is True
    )


@pytest.mark.asyncio
async def test_inflight_barrier_survives_registry_capacity_pressure(tmp_path: Path) -> None:
    """Evicting an unsettled generation would strand its existing responder waiter."""
    harness = _echo_harness(tmp_path)
    source_event_id = "$voice-oldest"
    handle = harness.router.start(_request(source_event_id))
    assert handle is not None
    responder_wait = asyncio.create_task(_await_responder(harness, source_event_id))
    await asyncio.sleep(0)

    for index in range(200):
        assert harness.router.start(_request(f"$voice-new-{index}")) is not None

    await harness.router.finish(handle, _normalized_event(source_event_id))
    assert await asyncio.wait_for(responder_wait, timeout=1) is True


@pytest.mark.asyncio
async def test_published_barrier_survives_inflight_capacity_pressure(tmp_path: Path) -> None:
    """In-flight generations must not consume the bounded terminal-history budget."""
    harness = _echo_harness(tmp_path)
    source_event_id = "$voice-published"
    handle = harness.router.start(_request(source_event_id))
    assert handle is not None
    await harness.router.finish(handle, _normalized_event(source_event_id))

    for index in range(200):
        assert harness.router.start(_request(f"$voice-inflight-{index}")) is not None

    assert await _await_responder(harness, source_event_id) is True


@pytest.mark.asyncio
async def test_evicted_published_barrier_recovers_from_durable_echo(tmp_path: Path) -> None:
    """Evicting terminal history must not make a late responder abandon a published echo."""
    harness = _echo_harness(tmp_path)
    source_event_id = "$voice-published-oldest"
    handle = harness.router.start(_request(source_event_id))
    assert handle is not None
    await harness.router.finish(handle, _normalized_event(source_event_id))

    for index in range(200):
        newer_event_id = f"$voice-settled-{index}"
        newer_handle = harness.router.start(_request(newer_event_id))
        assert newer_handle is not None
        await harness.router.finish(newer_handle, _normalized_event(newer_event_id))

    assert await _await_responder(harness, source_event_id) is True


@pytest.mark.asyncio
async def test_failed_echo_generation_can_be_retried(tmp_path: Path) -> None:
    """Reusing a failed terminal barrier would poison every redelivery of that event."""
    harness = _echo_harness(tmp_path)
    source_event_id = "$voice-retry"
    harness.gateway.send_result = None
    failed_handle = harness.router.start(_request(source_event_id))
    assert failed_handle is not None
    await harness.router.finish(failed_handle, _normalized_event(source_event_id))
    assert await _await_responder(harness, source_event_id) is False

    harness.gateway.send_result = "$retry-echo"
    retry_handle = harness.router.start(_request(source_event_id))
    assert retry_handle is not None
    await harness.router.finish(retry_handle, _normalized_event(source_event_id))

    assert await _await_responder(harness, source_event_id) is True


@pytest.mark.asyncio
async def test_claimed_echo_stays_ordered_after_config_disable(tmp_path: Path) -> None:
    """A live config toggle must not bypass an already-started router echo."""
    harness = _echo_harness(tmp_path)
    source_event_id = "$voice-config-toggle"
    handle = harness.router.start(_request(source_event_id))
    assert handle is not None
    harness.config.voice.visible_router_echo = False
    harness.gateway.block_send = True

    responder_wait = asyncio.create_task(_await_responder(harness, source_event_id))
    finish = asyncio.create_task(harness.router.finish(handle, _normalized_event(source_event_id)))
    await asyncio.wait_for(harness.gateway.send_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not responder_wait.done()

    harness.gateway.release_send.set()
    await finish
    assert await asyncio.wait_for(responder_wait, timeout=1) is True
