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

import aiohttp
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
    from mindroom.matrix_rtc.voice_agent import CallVoiceAgentOptions

logger = get_logger(__name__)

#: Refresh the membership state event this long before its expiry window ends.
_MEMBERSHIP_REFRESH_MARGIN_MS = 5 * 60 * 1000

#: Retry delay after a failed membership refresh.
_MEMBERSHIP_REFRESH_RETRY_MS = 60 * 1000

#: Backoff for members that could not receive a media frame key yet; after
#: the last delay, delivery waits for the next membership change instead of
#: polling the homeserver forever for a device that may never come online.
_KEY_DISTRIBUTION_RETRY_DELAYS_S = (1.0, 5.0, 30.0)

_MATRIX_NETWORK_ERRORS = (nio.exceptions.ProtocolError, OSError, aiohttp.ClientError)


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

    def set_participant_identities(self, participant_identities: frozenset[str]) -> None:
        """Restrict media input and output to the current Matrix call roster."""
        ...

    async def connect(self, grant: SfuGrant) -> None:
        """Connect to the SFU with the granted credentials."""
        ...

    def set_frame_key(self, participant_identity: str, key: bytes, key_index: int) -> None:
        """Install a media frame key for one participant."""
        ...

    async def start_agent(self, options: CallVoiceAgentOptions) -> None:
        """Start the configured voice agent on the connected room."""
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
    ) -> list[CallMember]:
        """Deliver our frame key and return the members that received it."""
        ...


@dataclass
class CallSessionDeps:
    """Injected collaborators for one call session."""

    client: nio.AsyncClient
    bridge: VoiceBridgeLike
    key_transport: _FrameKeyTransportLike
    fetch_grant: Callable[[], Coroutine[None, None, SfuGrant]]
    agent_options: CallVoiceAgentOptions
    livekit_service_url: str
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000)
    #: Awaited once after the session fully stopped (transcript finalization).
    on_stopped: Callable[[], Coroutine[None, None, None]] | None = None


@dataclass
class CallSession:
    """Drives one agent's participation in one room call."""

    room_id: str
    requester_id: str
    e2ee_enabled: bool
    deps: CallSessionDeps
    _key_manager: FrameKeyManager = field(init=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    _refresh_iteration: int = field(default=1, init=False)
    _created_ts: int | None = field(default=None, init=False)
    _stopped: bool = field(default=False, init=False)
    _stop_task: asyncio.Task[None] | None = field(default=None, init=False)
    _members: list[CallMember] = field(default_factory=list, init=False)
    _key_distribution_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _key_distribution_retry_scheduled: bool = field(default=False, init=False)
    _key_distribution_wakeup_scheduled: bool = field(default=False, init=False)
    _key_retry_attempt: int = field(default=0, init=False)

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
        self._members = members
        self._sync_bridge_participants()
        try:
            grant = await self.deps.fetch_grant()
            try:
                await self.deps.bridge.connect(grant)
            except Exception as error:
                # livekit raises SDK-native exception types; convert them so the
                # manager's join guard handles them as an ordinary failed join.
                msg = f"LiveKit SFU connect failed: {error}"
                raise CallJoinError(msg) from error
            await self._publish_membership(initial=True)
            if self.e2ee_enabled:
                await self._distribute_keys()
            self._spawn(self._membership_refresh_loop())
            try:
                await self.deps.bridge.start_agent(self.deps.agent_options)
            except Exception as error:
                if isinstance(error, CallJoinError):
                    raise
                msg = f"Voice agent start failed: {error}"
                raise CallJoinError(msg) from error
        except BaseException:
            await self.stop()
            raise
        logger.info("call_joined", room_id=self.room_id, identity=self.local_identity)

    async def on_members_changed(self, members: list[CallMember]) -> None:
        """React to remote membership changes (key rotation/sharing)."""
        if self._stopped:
            return
        self._members = members
        self._sync_bridge_participants()
        if not self.e2ee_enabled:
            return
        # A roster change is a fresh delivery opportunity: restart the backoff.
        self._key_retry_attempt = 0
        await self._distribute_keys()

    def on_key_received(self, received: ReceivedFrameKey) -> bool:
        """Install a current participant's frame key and report roster admission."""
        if self._stopped:
            return True
        member = next(
            (
                candidate
                for candidate in self._members
                if candidate.user_id == received.user_id and candidate.device_id == received.claimed_device_id
            ),
            None,
        )
        if member is None:
            return False
        participant_identity = f"{member.user_id}:{member.device_id}"
        inbound = self._key_manager.receive(
            received,
            participant_identity=participant_identity,
        )
        if inbound is None:
            return True
        self.deps.bridge.set_frame_key(inbound.participant_identity, inbound.key, inbound.key_index)
        logger.debug(
            "call_frame_key_installed",
            room_id=self.room_id,
            participant=inbound.participant_identity,
            key_index=inbound.key_index,
        )
        # Decrypting this Olm event proves that a usable bidirectional session
        # now exists with the sender. Wake outbound key distribution
        # immediately instead of waiting for the old bounded retry timer (or a
        # future membership change after that retry budget was exhausted).
        if self.e2ee_enabled and self._created_ts is not None:
            self._wake_key_distribution_after_inbound_key()
        return True

    def _wake_key_distribution_after_inbound_key(self) -> None:
        """Coalesce immediate outbound retries after authenticated key intake."""
        if self._stopped or self._key_distribution_wakeup_scheduled:
            return
        self._key_distribution_wakeup_scheduled = True
        self._spawn(self._redistribute_after_inbound_key())

    async def _redistribute_after_inbound_key(self) -> None:
        """Retry our key now that inbound Olm traffic established a session."""
        try:
            self._key_retry_attempt = 0
            await self._distribute_keys()
        except _MATRIX_NETWORK_ERRORS as error:
            logger.warning("call_key_distribution_error", room_id=self.room_id, error=str(error))
            self._schedule_key_distribution_retry()
        finally:
            self._key_distribution_wakeup_scheduled = False

    async def stop(self) -> None:
        """Leave the call through one cancellation-safe shared cleanup task."""
        if self._stop_task is None:
            self._stopped = True
            self._stop_task = asyncio.create_task(
                self._run_stop(),
                name=f"matrix_rtc_stop_{self.room_id}",
            )
        await asyncio.shield(self._stop_task)

    async def _run_stop(self) -> None:
        """Clear membership, cancel background work, and close media once."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        # Media teardown and transcript finalization must run even when the
        # homeserver is unreachable while leaving.
        try:
            await self._clear_membership()
        finally:
            try:
                await self.deps.bridge.aclose()
            finally:
                if self.deps.on_stopped is not None:
                    await self.deps.on_stopped()
        logger.info("call_left", room_id=self.room_id, identity=self.local_identity)

    async def _distribute_keys(self) -> None:
        async with self._key_distribution_lock:
            if self._stopped:
                return
            members = list(self._members)
            distribution = self._key_manager.update_memberships(members, self.deps.clock_ms())
            if distribution is None:
                return
            delivered: list[CallMember] = []
            if distribution.targets:
                self._key_manager.mark_exposed(distribution)
                delivered = await self.deps.key_transport.send_key(
                    room_id=self.room_id,
                    key_base64=distribution.key_base64,
                    key_index=distribution.key_index,
                    targets=list(distribution.targets),
                )
            self._key_manager.mark_distributed(distribution, tuple(delivered))
            if len(delivered) == len(distribution.targets):
                self._key_retry_attempt = 0
            else:
                self._schedule_key_distribution_retry()
            if distribution.apply_after_ms <= 0:
                self._apply_own_key(distribution.key, distribution.key_index)
            else:
                self._spawn(
                    self._apply_own_key_later(distribution.key, distribution.key_index, distribution.apply_after_ms),
                )

    def _schedule_key_distribution_retry(self) -> None:
        """Re-attempt undelivered recipients on a bounded backoff."""
        if self._key_distribution_retry_scheduled or self._stopped:
            return
        if self._key_retry_attempt >= len(_KEY_DISTRIBUTION_RETRY_DELAYS_S):
            logger.warning(
                "call_key_distribution_gave_up",
                room_id=self.room_id,
                hint="undelivered recipients will be retried on the next membership change",
            )
            return
        delay_s = _KEY_DISTRIBUTION_RETRY_DELAYS_S[self._key_retry_attempt]
        self._key_retry_attempt += 1
        self._key_distribution_retry_scheduled = True
        self._spawn(self._retry_key_distribution(delay_s))

    async def _retry_key_distribution(self, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
        finally:
            self._key_distribution_retry_scheduled = False
        if not self._stopped:
            try:
                await self._distribute_keys()
            except _MATRIX_NETWORK_ERRORS as error:
                logger.warning("call_key_distribution_error", room_id=self.room_id, error=str(error))
                self._schedule_key_distribution_retry()

    def _apply_own_key(self, key: bytes, key_index: int) -> None:
        self.deps.bridge.set_frame_key(self.local_identity, key, key_index)

    def _sync_bridge_participants(self) -> None:
        """Apply the authoritative Matrix call roster to the SFU bridge."""
        self.deps.bridge.set_participant_identities(
            frozenset(f"{member.user_id}:{member.device_id}" for member in self._members),
        )

    async def _apply_own_key_later(self, key: bytes, key_index: int, delay_ms: int) -> None:
        await asyncio.sleep(delay_ms / 1000)
        if not self._stopped and self._key_manager.is_current_outbound_key(key, key_index):
            self._apply_own_key(key, key_index)

    async def _publish_membership(self, *, initial: bool, expires_iteration: int | None = None) -> bool:
        """Publish or refresh our membership state event; returns success."""
        client = self.deps.client
        now = self.deps.clock_ms()
        if self._created_ts is None:
            self._created_ts = now
        device_id = required_device_id(client)
        content = build_membership_content(
            user_id=client.user_id,
            device_id=device_id,
            livekit_service_url=self.deps.livekit_service_url,
            expires_ms=DEFAULT_MEMBERSHIP_EXPIRES_MS
            * (self._refresh_iteration if expires_iteration is None else expires_iteration),
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
            return False
        return True

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
            try:
                next_iteration = self._refresh_iteration + 1
                published = await self._publish_membership(initial=False, expires_iteration=next_iteration)
            except _MATRIX_NETWORK_ERRORS as error:
                logger.warning("call_membership_refresh_error", room_id=self.room_id, error=str(error))
                published = False
            if not published:
                # Retry the SAME refresh window after a short delay instead of
                # skipping ahead a whole expiry period (which would let the
                # membership expire out of the roster).
                await asyncio.sleep(_MEMBERSHIP_REFRESH_RETRY_MS / 1000)
                continue
            self._refresh_iteration = next_iteration

    async def _clear_membership(self) -> None:
        client = self.deps.client
        try:
            response = await client.room_put_state(
                self.room_id,
                CALL_MEMBER_EVENT_TYPE,
                {},
                state_key=membership_state_key(client.user_id, required_device_id(client)),
            )
        except _MATRIX_NETWORK_ERRORS as error:
            logger.warning("call_membership_clear_failed", room_id=self.room_id, error=str(error))
            return
        if isinstance(response, nio.RoomPutStateError):
            logger.warning("call_membership_clear_failed", room_id=self.room_id, error=response.message)

    def _spawn(self, coro: Coroutine[None, None, None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _observe(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                logger.warning("call_session_task_failed", room_id=self.room_id, error=str(done.exception()))

        task.add_done_callback(_observe)
