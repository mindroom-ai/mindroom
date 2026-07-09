"""One active MatrixRTC call for one agent bot.

Owns the full per-call lifecycle: membership state event publish/refresh,
SFU credential exchange, frame-key distribution, the media bridge, and
teardown. Collaborators are injected behind small protocols so the session
logic is testable without LiveKit or a homeserver.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import nio

from mindroom.logging_config import get_logger
from mindroom.matrix_rtc.events import (
    CALL_MEMBER_EVENT_TYPE,
    DEFAULT_MEMBERSHIP_EXPIRES_MS,
    CallMember,
    ReceivedFrameKey,
    build_membership_content,
    membership_state_key,
)
from mindroom.matrix_rtc.frame_keys import FrameKeyManager

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from mindroom.matrix_rtc.focus import SfuGrant
    from mindroom.matrix_rtc.voice_agent import VoiceAgentOptions

logger = get_logger(__name__)

#: Refresh the membership state event this long before its expiry window ends.
_MEMBERSHIP_REFRESH_MARGIN_MS = 5 * 60 * 1000

#: Retry delay after a failed membership refresh.
_MEMBERSHIP_REFRESH_RETRY_MS = 60 * 1000


class CallJoinError(RuntimeError):
    """Joining the call failed before the media bridge came up."""


def required_device_id(client: nio.AsyncClient) -> str:
    """The client's device ID, which a logged-in call participant must have."""
    device_id = client.device_id
    if not device_id:
        msg = "Matrix client has no device_id; cannot participate in a call"
        raise CallJoinError(msg)
    return device_id


class VoiceBridgeLike(Protocol):
    """Media-plane surface the session drives (see ``RealtimeVoiceBridge``)."""

    async def connect(self, grant: SfuGrant) -> None:
        """Connect to the SFU with the granted credentials."""
        ...

    def set_frame_key(self, participant_identity: str, key: bytes, key_index: int) -> None:
        """Install a media frame key for one participant."""
        ...

    async def start_agent(self, options: VoiceAgentOptions) -> None:
        """Start the realtime voice agent on the connected room."""
        ...

    async def aclose(self) -> None:
        """Tear down the agent and leave the SFU."""
        ...


class _FrameKeyTransportLike(Protocol):
    """Key distribution surface (see ``ToDeviceFrameKeyTransport``)."""

    async def send_key(
        self,
        *,
        room_id: str,
        key_base64: str,
        key_index: int,
        targets: list[CallMember],
    ) -> None:
        """Deliver our frame key to the target call members."""
        ...


@dataclass
class CallSessionDeps:
    """Injected collaborators for one call session."""

    client: nio.AsyncClient
    bridge: VoiceBridgeLike
    key_transport: _FrameKeyTransportLike
    fetch_grant: Callable[[], Coroutine[None, None, SfuGrant]]
    agent_options: VoiceAgentOptions
    livekit_service_url: str
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    #: Awaited once after the session fully stopped (transcript finalization).
    on_stopped: Callable[[], Coroutine[None, None, None]] | None = None


@dataclass
class CallSession:
    """Drives one agent's participation in one room call."""

    room_id: str
    e2ee_enabled: bool
    deps: CallSessionDeps
    _key_manager: FrameKeyManager = field(init=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _refresh_iteration: int = field(default=1, init=False)
    _created_ts: int | None = field(default=None, init=False)
    _stopped: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Initialize the frame-key manager from the client identity."""
        client = self.deps.client
        self._key_manager = FrameKeyManager(
            own_user_id=client.user_id,
            own_device_id=required_device_id(client),
        )

    @property
    def local_identity(self) -> str:
        """Our LiveKit participant identity (``user_id:device_id``)."""
        client = self.deps.client
        return f"{client.user_id}:{required_device_id(client)}"

    async def start(self, members: list[CallMember]) -> None:
        """Join the call: connect media, publish membership, distribute keys."""
        grant = await self.deps.fetch_grant()
        await self.deps.bridge.connect(grant)
        try:
            if self.e2ee_enabled:
                await self._distribute_keys(members)
            await self._publish_membership(initial=True)
            self._spawn(self._membership_refresh_loop())
            await self.deps.bridge.start_agent(self.deps.agent_options)
        except BaseException:
            await self.stop()
            raise
        logger.info("call_joined", room_id=self.room_id, identity=self.local_identity)

    async def on_members_changed(self, members: list[CallMember]) -> None:
        """React to remote membership changes (key rotation/sharing)."""
        if self._stopped or not self.e2ee_enabled:
            return
        await self._distribute_keys(members)

    def on_key_received(self, received: ReceivedFrameKey) -> None:
        """Install a remote participant's frame key on the media bridge."""
        if self._stopped:
            return
        inbound = self._key_manager.receive(received, self.deps.clock_ms())
        if inbound is None:
            return
        self.deps.bridge.set_frame_key(inbound.participant_identity, inbound.key, inbound.key_index)
        logger.debug(
            "call_frame_key_installed",
            room_id=self.room_id,
            participant=inbound.participant_identity,
            key_index=inbound.key_index,
        )

    async def stop(self) -> None:
        """Leave the call: clear membership, cancel tasks, close media."""
        if self._stopped:
            return
        self._stopped = True
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        await self._clear_membership()
        await self.deps.bridge.aclose()
        if self.deps.on_stopped is not None:
            await self.deps.on_stopped()
        logger.info("call_left", room_id=self.room_id, identity=self.local_identity)

    async def _distribute_keys(self, members: list[CallMember]) -> None:
        distribution = self._key_manager.update_memberships(members, self.deps.clock_ms())
        if distribution is None:
            return
        if distribution.targets:
            await self.deps.key_transport.send_key(
                room_id=self.room_id,
                key_base64=distribution.key_base64,
                key_index=distribution.key_index,
                targets=list(distribution.targets),
            )
        self._key_manager.mark_distributed(distribution)
        if distribution.apply_after_ms <= 0:
            self._apply_own_key(distribution.key, distribution.key_index)
        else:
            self._spawn(
                self._apply_own_key_later(distribution.key, distribution.key_index, distribution.apply_after_ms),
            )

    def _apply_own_key(self, key: bytes, key_index: int) -> None:
        self.deps.bridge.set_frame_key(self.local_identity, key, key_index)

    async def _apply_own_key_later(self, key: bytes, key_index: int, delay_ms: int) -> None:
        await asyncio.sleep(delay_ms / 1000)
        if not self._stopped:
            self._apply_own_key(key, key_index)

    async def _publish_membership(self, *, initial: bool) -> None:
        client = self.deps.client
        now = self.deps.clock_ms()
        if self._created_ts is None:
            self._created_ts = now
        device_id = required_device_id(client)
        content = build_membership_content(
            user_id=client.user_id,
            device_id=device_id,
            livekit_service_url=self.deps.livekit_service_url,
            expires_ms=DEFAULT_MEMBERSHIP_EXPIRES_MS * self._refresh_iteration,
            # Like matrix-js-sdk: the first event carries no created_ts (a
            # "join", timestamped by the server); refreshes repeat the
            # original timestamp so the expiry window stays anchored.
            created_ts=None if initial else self._created_ts,
        )
        response = await client.room_put_state(
            self.room_id,
            CALL_MEMBER_EVENT_TYPE,
            content,
            state_key=membership_state_key(client.user_id, device_id),
        )
        if isinstance(response, nio.RoomPutStateError):
            message = f"Failed to publish call membership in {self.room_id}: {response.message}"
            if initial:
                raise CallJoinError(message)
            logger.warning("call_membership_refresh_failed", room_id=self.room_id, error=response.message)

    async def _membership_refresh_loop(self) -> None:
        while not self._stopped:
            created_ts = self._created_ts if self._created_ts is not None else self.deps.clock_ms()
            target_ms = (
                created_ts + DEFAULT_MEMBERSHIP_EXPIRES_MS * self._refresh_iteration - _MEMBERSHIP_REFRESH_MARGIN_MS
            )
            delay_ms = max(0, target_ms - self.deps.clock_ms())
            await asyncio.sleep(delay_ms / 1000)
            if self._stopped:
                return
            self._refresh_iteration += 1
            try:
                await self._publish_membership(initial=False)
            except (nio.exceptions.ProtocolError, OSError) as error:
                logger.warning("call_membership_refresh_error", room_id=self.room_id, error=str(error))
                await asyncio.sleep(_MEMBERSHIP_REFRESH_RETRY_MS / 1000)

    async def _clear_membership(self) -> None:
        client = self.deps.client
        response = await client.room_put_state(
            self.room_id,
            CALL_MEMBER_EVENT_TYPE,
            {},
            state_key=membership_state_key(client.user_id, required_device_id(client)),
        )
        if isinstance(response, nio.RoomPutStateError):
            logger.warning("call_membership_clear_failed", room_id=self.room_id, error=response.message)

    def _spawn(self, coro: Coroutine[None, None, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
