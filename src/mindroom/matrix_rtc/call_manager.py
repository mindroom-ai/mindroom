"""Per-bot MatrixRTC call lifecycle: watch rooms, join and leave calls.

The manager consumes the bot's sync callbacks (custom state events and
decrypted to-device events), reconciles the room's call membership state,
and starts or stops one ``CallSession`` per room. Reconciliation always
re-reads the room state from the homeserver, so a bot that restarts
mid-call recovers as soon as any call event arrives.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
import nio

from mindroom.credentials_sync import get_secret_from_env
from mindroom.logging_config import get_logger
from mindroom.matrix_rtc.call_session import CallJoinError, CallSession, CallSessionDeps, required_device_id
from mindroom.matrix_rtc.events import (
    CALL_ENCRYPTION_KEYS_EVENT_TYPE,
    CALL_MEMBER_EVENT_TYPE,
    RTC_NOTIFICATION_EVENT_TYPE,
    CallMember,
    parse_membership_event,
)
from mindroom.matrix_rtc.focus import OpenIDToken, discover_livekit_service_url, request_sfu_grant
from mindroom.matrix_rtc.key_transport import ToDeviceFrameKeyTransport
from mindroom.matrix_rtc.transcript import CallTranscript
from mindroom.matrix_rtc.voice_agent import (
    RealtimeVoiceBridge,
    VoiceAgentOptions,
    matrix_calls_dependencies_available,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix_rtc.call_session import VoiceBridgeLike
    from mindroom.matrix_rtc.focus import SfuGrant

logger = get_logger(__name__)


def _default_bridge_factory(local_identity: str, e2ee_enabled: bool) -> RealtimeVoiceBridge:
    return RealtimeVoiceBridge(local_identity=local_identity, e2ee_enabled=e2ee_enabled)


_CALL_EVENT_TYPES = frozenset({CALL_MEMBER_EVENT_TYPE, RTC_NOTIFICATION_EVENT_TYPE})


_VOICE_STYLE_ADDENDUM = (
    "You are participating in a live group voice call. Everything you say is spoken "
    "aloud: keep responses short, conversational, and natural, and never use markdown, "
    "lists, or other written formatting."
)


def _build_call_instructions(agent_name: str, config: Config) -> str:
    """Compose realtime-agent instructions from the agent's authored configuration."""
    agent = config.agents[agent_name]
    parts = [f"You are {agent.display_name}, an AI assistant."]
    if agent.role:
        parts.append(f"Your role: {agent.role}")
    parts.extend(agent.instructions)
    parts.append(_VOICE_STYLE_ADDENDUM)
    return "\n".join(parts)


def maybe_build_call_manager(
    *,
    agent_name: str,
    config: Config,
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    homeserver_url: str,
    ssl_verify: bool,
) -> CallManager | None:
    """Build a call manager when this agent is configured for voice calls."""
    if not config.calls.enabled or agent_name not in config.calls.agents:
        return None
    if agent_name not in config.agents:
        return None
    if not matrix_calls_dependencies_available():
        logger.warning(
            "calls_enabled_but_dependencies_missing",
            agent=agent_name,
            hint="install mindroom with the [matrix_calls] extra",
        )
        return None
    return CallManager(
        agent_name=agent_name,
        config=config,
        client=client,
        runtime_paths=runtime_paths,
        homeserver_url=homeserver_url,
        ssl_verify=ssl_verify,
    )


class CallManager:
    """Watches call events for one agent bot and manages its call sessions."""

    def __init__(
        self,
        *,
        agent_name: str,
        config: Config,
        client: nio.AsyncClient,
        runtime_paths: RuntimePaths,
        homeserver_url: str,
        ssl_verify: bool,
        bridge_factory: Callable[[str, bool], VoiceBridgeLike] = _default_bridge_factory,
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._client = client
        self._runtime_paths = runtime_paths
        self._homeserver_url = homeserver_url
        self._ssl_verify = ssl_verify
        self._bridge_factory = bridge_factory
        self._key_transport = ToDeviceFrameKeyTransport(client)
        self._sessions: dict[str, CallSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._shutting_down = False

    async def on_room_event(self, room: nio.MatrixRoom, event: nio.UnknownEvent) -> None:
        """Sync callback for custom room events (call membership, ring)."""
        if event.type not in _CALL_EVENT_TYPES or self._shutting_down:
            return
        await self._reconcile(room)

    async def on_to_device_event(self, event: nio.ToDeviceEvent) -> None:
        """Sync callback for decrypted call frame-key to-device events."""
        if not isinstance(event, nio.UnknownToDeviceEvent) or event.type != CALL_ENCRYPTION_KEYS_EVENT_TYPE:
            return
        for session in self._sessions.values():
            received = self._key_transport.parse_incoming(event, room_id=session.room_id)
            if received is not None:
                session.on_key_received(received)
                return

    async def shutdown(self) -> None:
        """Leave every active call."""
        self._shutting_down = True
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await session.stop()

    async def _reconcile(self, room: nio.MatrixRoom) -> None:
        room_id = room.room_id
        lock = self._locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            if self._shutting_down:
                return
            members = await self._fetch_remote_members(room_id)
            if members is None:
                # Transient state-fetch failure: keep any active session alive
                # and wait for the next call event to reconcile again.
                return
            session = self._sessions.get(room_id)
            if session is None:
                if members:
                    await self._join(room, members)
                return
            if members:
                await session.on_members_changed(members)
            else:
                self._sessions.pop(room_id, None)
                await session.stop()

    async def _fetch_remote_members(self, room_id: str) -> list[CallMember] | None:
        """Current, unexpired call members in the room, excluding ourselves.

        Returns ``None`` when the room state could not be read, so callers can
        distinguish "the call is empty" from a transient homeserver error.
        """
        response = await self._client.room_get_state(room_id)
        if isinstance(response, nio.RoomGetStateError):
            logger.warning("call_state_fetch_failed", room_id=room_id, error=response.message)
            return None
        now_ms = int(time.time() * 1000)
        members = []
        for raw_event in response.events:
            member = parse_membership_event(raw_event)
            if member is None or member.is_expired(now_ms):
                continue
            if member.user_id == self._client.user_id:
                continue
            members.append(member)
        return members

    async def _join(self, room: nio.MatrixRoom, members: list[CallMember]) -> None:
        room_id = room.room_id
        api_key = get_secret_from_env("OPENAI_API_KEY", self._runtime_paths)
        if not api_key:
            logger.warning("call_join_skipped_no_openai_key", room_id=room_id, agent=self._agent_name)
            return
        service_url = await self._resolve_service_url()
        if service_url is None:
            logger.warning("call_join_skipped_no_livekit_service", room_id=room_id, agent=self._agent_name)
            return
        transcript = CallTranscript.start(
            agent_name=self._agent_name,
            config=self._config,
            storage_path=self._runtime_paths.storage_root,
            room_id=room_id,
            room_display_name=room.display_name or room_id,
        )
        options = VoiceAgentOptions(
            instructions=_build_call_instructions(self._agent_name, self._config),
            model=self._config.calls.model,
            api_key=api_key,
            voice=self._config.calls.voice,
            greeting_instructions="Briefly greet the participants and let them know you joined the call.",
            on_conversation_turn=transcript.record,
        )
        try:
            session = CallSession(
                room_id=room_id,
                e2ee_enabled=room.encrypted,
                deps=CallSessionDeps(
                    client=self._client,
                    bridge=self._bridge_factory(
                        f"{self._client.user_id}:{required_device_id(self._client)}",
                        room.encrypted,
                    ),
                    key_transport=self._key_transport,
                    fetch_grant=lambda: self._fetch_grant(room_id, service_url),
                    agent_options=options,
                    livekit_service_url=service_url,
                    on_stopped=lambda: transcript.finalize(
                        config=self._config,
                        runtime_paths=self._runtime_paths,
                        storage_path=self._runtime_paths.storage_root,
                    ),
                ),
            )
            await session.start(members)
        except (CallJoinError, httpx.HTTPError, ValueError) as error:
            logger.warning("call_join_failed", room_id=room_id, agent=self._agent_name, error=str(error))
            return
        if self._shutting_down:
            # shutdown() ran while the join was in flight and cannot see this
            # session yet; stop it instead of leaking a live SFU connection.
            await session.stop()
            return
        self._sessions[room_id] = session
        logger.info("call_session_started", room_id=room_id, agent=self._agent_name)

    async def _resolve_service_url(self) -> str | None:
        if self._config.calls.livekit_service_url:
            return self._config.calls.livekit_service_url
        discovered = await discover_livekit_service_url(self._homeserver_url, ssl_verify=self._ssl_verify)
        if discovered:
            return discovered
        return None

    async def _fetch_grant(self, room_id: str, service_url: str) -> SfuGrant:
        client = self._client
        response = await client.get_openid_token(client.user_id)
        if isinstance(response, nio.responses.GetOpenIDTokenError):
            msg = f"OpenID token request failed: {response.message}"
            raise CallJoinError(msg)
        openid_token = OpenIDToken(
            access_token=response.access_token,
            expires_in=response.expires_in,
            matrix_server_name=response.matrix_server_name,
            token_type=response.token_type,
        )
        return await request_sfu_grant(
            service_url,
            room_id=room_id,
            device_id=required_device_id(client),
            openid_token=openid_token,
            ssl_verify=self._ssl_verify,
        )
