"""Per-bot MatrixRTC call lifecycle: watch rooms, join and leave calls.

The manager consumes the bot's sync callbacks (custom state events and
decrypted to-device events), reconciles the room's call membership state,
and starts or stops one ``CallSession`` per room. Reconciliation always
re-reads the room state from the homeserver, both on call events and after
each sync-loop start, so a bot recovers calls already active at startup.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import aiohttp
import httpx
import nio

from mindroom.authorization import is_authorized_sender, is_sender_allowed_for_agent_reply
from mindroom.credentials_sync import get_api_key_for_provider
from mindroom.entity_resolution import configured_call_agent_name_for_room
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID
from mindroom.matrix_rtc.call_session import CallJoinError, CallSession, CallSessionDeps, required_device_id
from mindroom.matrix_rtc.call_tools import CallAgentTooling, build_call_tools
from mindroom.matrix_rtc.events import (
    CALL_ENCRYPTION_KEYS_EVENT_TYPE,
    CALL_MEMBER_EVENT_TYPE,
    RTC_NOTIFICATION_EVENT_TYPE,
    CallMember,
    ReceivedFrameKey,
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
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport

logger = get_logger(__name__)


def _default_bridge_factory(local_identity: str, e2ee_enabled: bool) -> RealtimeVoiceBridge:
    return RealtimeVoiceBridge(local_identity=local_identity, e2ee_enabled=e2ee_enabled)


_CALL_EVENT_TYPES = frozenset({CALL_MEMBER_EVENT_TYPE, RTC_NOTIFICATION_EVENT_TYPE})
_MAX_PENDING_KEYS_PER_ROOM = 64
_RECONCILE_RETRY_DELAYS_S = (1.0, 5.0, 30.0, 60.0)
_MATRIX_NETWORK_ERRORS = (nio.exceptions.ProtocolError, OSError, aiohttp.ClientError)
_CALL_NETWORK_ERRORS = (httpx.HTTPError, *_MATRIX_NETWORK_ERRORS)


_VOICE_STYLE_ADDENDUM = (
    "You are participating in a live group voice call. Everything you say is spoken "
    "aloud: keep responses short, conversational, and natural, and never use markdown, "
    "lists, or other written formatting."
)


def _build_call_instructions(agent_name: str, config: Config, chat_system_prompt: str | None) -> str:
    """Compose realtime-agent instructions, preferring the chat system prompt."""
    if chat_system_prompt:
        return f"{chat_system_prompt}\n\n{_VOICE_STYLE_ADDENDUM}"
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
    ssl_verify: bool,
    tool_support: ToolRuntimeSupport | None = None,
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
        ssl_verify=ssl_verify,
        tool_support=tool_support,
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
        ssl_verify: bool,
        bridge_factory: Callable[[str, bool], VoiceBridgeLike] = _default_bridge_factory,
        tool_support: ToolRuntimeSupport | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._client = client
        self._runtime_paths = runtime_paths
        self._ssl_verify = ssl_verify
        self._bridge_factory = bridge_factory
        self._tool_support = tool_support
        self._clock_ms = clock_ms
        self._key_transport = ToDeviceFrameKeyTransport(client)
        self._sessions: dict[str, CallSession] = {}
        self._pending_keys: dict[str, dict[tuple[str, str, int], ReceivedFrameKey]] = {}
        self._observed_rooms: dict[str, nio.MatrixRoom] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_attempts: dict[str, int] = {}
        self._expiry_handles: dict[str, asyncio.TimerHandle] = {}
        self._expiry_reconcile_tasks: set[asyncio.Task[None]] = set()
        self._shutting_down = False

    async def on_room_event(self, room: nio.MatrixRoom, event: nio.UnknownEvent) -> None:
        """Sync callback for custom room events (call membership, ring)."""
        if event.type not in _CALL_EVENT_TYPES or self._shutting_down or not self._is_configured_call_room(room):
            return
        self._observed_rooms[room.room_id] = room
        await self._reconcile(room)

    async def on_room_membership_event(self, room: nio.MatrixRoom, _event: nio.RoomMemberEvent) -> None:
        """Reconcile calls when a user's underlying room membership changes."""
        if self._shutting_down or not self._is_configured_call_room(room):
            return
        self._observed_rooms[room.room_id] = room
        await self._reconcile(room)

    async def on_to_device_event(self, event: nio.ToDeviceEvent) -> None:
        """Sync callback for decrypted call frame-key to-device events."""
        if (
            not isinstance(event, nio.UnknownToDeviceEvent)
            or event.type != CALL_ENCRYPTION_KEYS_EVENT_TYPE
            or self._shutting_down
        ):
            return
        parsed = self._key_transport.parse_incoming(event)
        if parsed is None:
            return
        room_id, received = parsed
        if not self._is_configured_call_room_id(room_id) or not self._is_authorized_call_member(
            received.user_id,
            room_id,
        ):
            return
        session = self._sessions.get(room_id)
        if session is not None and session.on_key_received(received):
            return
        members = await self._fetch_remote_members(room_id)
        if members is None or not self._received_key_matches_member(received, members):
            logger.warning(
                "call_frame_key_rejected_nonmember",
                room_id=room_id,
                user_id=received.user_id,
                device_id=received.claimed_device_id,
            )
            return
        self._queue_pending_key(room_id, received)
        if session is not None:
            room = self._observed_rooms.get(room_id) or self._client.rooms.get(room_id)
            if room is not None:
                await self._reconcile(room)

    async def reconcile_joined_rooms(self) -> None:
        """Reconcile configured calls after a successful Matrix sync response."""
        if self._shutting_down:
            return
        rooms = [room for room in self._client.rooms.values() if self._is_configured_call_room(room)]
        self._observed_rooms.update((room.room_id, room) for room in rooms)
        await asyncio.gather(*(self._reconcile(room) for room in rooms))

    async def shutdown(self) -> None:
        """Leave every active call."""
        self._shutting_down = True
        self._pending_keys.clear()
        background_tasks = [*self._retry_tasks.values(), *self._expiry_reconcile_tasks]
        self._retry_tasks.clear()
        self._expiry_reconcile_tasks.clear()
        self._retry_attempts.clear()
        for handle in self._expiry_handles.values():
            handle.cancel()
        self._expiry_handles.clear()
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        sessions = list(self._sessions.values())
        self._sessions.clear()
        for session in sessions:
            await self._stop_session(session, event="call_session_shutdown_failed")

    async def _reconcile(self, room: nio.MatrixRoom) -> None:
        if not self._is_configured_call_room(room):
            return
        self._observed_rooms[room.room_id] = room
        room_id = room.room_id
        lock = self._locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            if self._shutting_down:
                return
            members = await self._fetch_remote_members(room_id)
            if members is None:
                # Transient state-fetch failure: keep any active session alive
                # and retry even if no further call event arrives.
                self._schedule_reconcile_retry(room)
                return
            self._schedule_expiry_reconcile(room, members)
            await self._apply_reconciled_members(room, members)

    async def _apply_reconciled_members(self, room: nio.MatrixRoom, members: list[CallMember]) -> None:
        """Apply one authoritative room/call roster to the active session."""
        room_id = room.room_id
        session = self._sessions.get(room_id)
        if not self._members_are_authorized(members, room_id):
            self._clear_reconcile_retry(room_id)
            self._pending_keys.pop(room_id, None)
            if session is not None:
                self._sessions.pop(room_id, None)
                await self._stop_session(session)
            return
        if session is None:
            await self._join_if_populated(room, members)
            return
        if not members:
            self._clear_reconcile_retry(room_id)
            self._pending_keys.pop(room_id, None)
            self._sessions.pop(room_id, None)
            await self._stop_session(session)
            return
        await self._update_session_members(room, session, members)

    async def _join_if_populated(self, room: nio.MatrixRoom, members: list[CallMember]) -> None:
        """Join a populated call or finish a successful empty reconciliation."""
        if not members:
            self._clear_reconcile_retry(room.room_id)
            return
        if await self._join(room, members):
            self._clear_reconcile_retry(room.room_id)
        else:
            self._schedule_reconcile_retry(room)

    async def _update_session_members(
        self,
        room: nio.MatrixRoom,
        session: CallSession,
        members: list[CallMember],
    ) -> None:
        """Update one live session, retrying transient key-delivery failures."""
        try:
            await session.on_members_changed(members)
        except _MATRIX_NETWORK_ERRORS as error:
            logger.warning("call_membership_update_failed", room_id=room.room_id, error=str(error))
            self._schedule_reconcile_retry(room)
        else:
            self._replay_pending_keys(room.room_id, session)
            self._clear_reconcile_retry(room.room_id)

    def _is_configured_call_room(self, room: nio.MatrixRoom) -> bool:
        """Return whether this agent is configured to join calls in ``room``."""
        room_alias = room.canonical_alias
        room_aliases = (room_alias,) if isinstance(room_alias, str) and room_alias else ()
        try:
            configured_agent = configured_call_agent_name_for_room(
                self._config,
                room.room_id,
                self._runtime_paths,
                room_aliases=room_aliases,
            )
        except ValueError as error:
            logger.warning("call_room_ownership_ambiguous", room_id=room.room_id, error=str(error))
            return False
        return configured_agent == self._agent_name

    def _is_configured_call_room_id(self, room_id: str) -> bool:
        """Return whether this agent is configured to join calls in ``room_id``."""
        room = self._observed_rooms.get(room_id) or self._client.rooms.get(room_id)
        return room is not None and self._is_configured_call_room(room)

    @staticmethod
    def _received_key_matches_member(received: ReceivedFrameKey, members: list[CallMember]) -> bool:
        """Return whether a key sender/device is in the current call roster."""
        return any(
            member.user_id == received.user_id and member.device_id == received.claimed_device_id for member in members
        )

    def _queue_pending_key(self, room_id: str, received: ReceivedFrameKey) -> None:
        """Retain a bounded, deduplicated key set while a session is starting."""
        pending = self._pending_keys.setdefault(room_id, {})
        identity = (received.user_id, received.claimed_device_id, received.key_index)
        pending.pop(identity, None)
        if len(pending) >= _MAX_PENDING_KEYS_PER_ROOM:
            pending.pop(next(iter(pending)))
        pending[identity] = received

    def _replay_pending_keys(self, room_id: str, session: CallSession) -> None:
        """Replay validated keys after the session receives an authoritative roster."""
        for received in self._pending_keys.pop(room_id, {}).values():
            if session.on_key_received(received):
                continue
            logger.warning(
                "call_frame_key_rejected_nonmember",
                room_id=room_id,
                user_id=received.user_id,
                device_id=received.claimed_device_id,
            )

    def _is_authorized_call_member(self, user_id: str, room_id: str) -> bool:
        """Return whether a participant may hear and invoke this voice agent."""
        return is_authorized_sender(
            user_id,
            self._config,
            room_id,
            self._runtime_paths,
        ) and is_sender_allowed_for_agent_reply(
            user_id,
            self._agent_name,
            self._config,
            self._runtime_paths,
        )

    def _members_are_authorized(self, members: list[CallMember], room_id: str) -> bool:
        """Require every call participant to be eligible for this agent."""
        for member in members:
            if self._is_authorized_call_member(member.user_id, room_id):
                continue
            logger.warning(
                "call_join_skipped_unauthorized_member",
                room_id=room_id,
                agent=self._agent_name,
                user_id=member.user_id,
            )
            return False
        return True

    async def _fetch_remote_members(self, room_id: str) -> list[CallMember] | None:
        """Current, unexpired call members in the room, excluding ourselves.

        Returns ``None`` when the room state could not be read, so callers can
        distinguish "the call is empty" from a transient homeserver error.
        """
        try:
            response = await self._client.room_get_state(room_id)
        except _MATRIX_NETWORK_ERRORS as error:
            logger.warning("call_state_fetch_failed", room_id=room_id, error=str(error))
            return None
        if isinstance(response, nio.RoomGetStateError):
            logger.warning("call_state_fetch_failed", room_id=room_id, error=response.message)
            return None
        joined_user_ids = {
            raw_event["state_key"]
            for raw_event in response.events
            if raw_event.get("type") == "m.room.member"
            and isinstance(raw_event.get("state_key"), str)
            and isinstance(raw_event.get("content"), dict)
            and raw_event["content"].get("membership") == "join"
        }
        now_ms = self._clock_ms()
        members = []
        for raw_event in response.events:
            member = parse_membership_event(raw_event)
            if member is None or member.is_expired(now_ms):
                continue
            if member.user_id not in joined_user_ids:
                continue
            if member.user_id == self._client.user_id:
                continue
            members.append(member)
        return members

    async def _join(self, room: nio.MatrixRoom, members: list[CallMember]) -> bool:
        room_id = room.room_id
        api_key = get_api_key_for_provider("openai", self._runtime_paths)
        if not api_key:
            logger.warning("call_join_skipped_no_openai_key", room_id=room_id, agent=self._agent_name)
            return False
        try:
            service = await self._resolve_service(members)
        except (ValueError, *_CALL_NETWORK_ERRORS) as error:
            logger.warning("call_service_discovery_failed", room_id=room_id, agent=self._agent_name, error=str(error))
            return False
        if service is None:
            logger.warning("call_join_skipped_no_livekit_service", room_id=room_id, agent=self._agent_name)
            return False
        tooling = await self._build_tooling(room_id)
        transcript = CallTranscript.start(
            agent_name=self._agent_name,
            config=self._config,
            storage_path=self._runtime_paths.storage_root,
            room_id=room_id,
            room_display_name=room.display_name or room_id,
        )
        options = VoiceAgentOptions(
            instructions=_build_call_instructions(self._agent_name, self._config, tooling.instructions),
            model=self._config.calls.model,
            api_key=api_key,
            voice=self._config.calls.voice,
            greeting_instructions="Briefly greet the participants and let them know you joined the call.",
            tools=tuple(tooling.tools),
            on_conversation_turn=transcript.record,
            on_tools_executed=transcript.record_tool_use,
        )
        started = False
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
                    fetch_grant=lambda: self._fetch_grant(room_id, service),
                    agent_options=options,
                    livekit_service_url=service,
                    on_stopped=lambda: transcript.finalize(
                        config=self._config,
                        runtime_paths=self._runtime_paths,
                        storage_path=self._runtime_paths.storage_root,
                    ),
                ),
            )
            await session.start(members)
            started = True
        except (CallJoinError, ValueError, *_CALL_NETWORK_ERRORS) as error:
            logger.warning("call_join_failed", room_id=room_id, agent=self._agent_name, error=str(error))
            return False
        finally:
            if not started:
                self._pending_keys.pop(room_id, None)
        if self._shutting_down:
            # shutdown() ran while the join was in flight and cannot see this
            # session yet; stop it instead of leaking a live SFU connection.
            self._pending_keys.pop(room_id, None)
            await self._stop_session(session)
            return False
        self._sessions[room_id] = session
        self._replay_pending_keys(room_id, session)
        logger.info("call_session_started", room_id=room_id, agent=self._agent_name)
        return True

    def _schedule_reconcile_retry(self, room: nio.MatrixRoom) -> None:
        """Retry transient reconciliation failures with a bounded backoff."""
        room_id = room.room_id
        if self._shutting_down or room_id in self._retry_tasks:
            return
        attempt = self._retry_attempts.get(room_id, 0)
        delay_s = _RECONCILE_RETRY_DELAYS_S[min(attempt, len(_RECONCILE_RETRY_DELAYS_S) - 1)]
        self._retry_attempts[room_id] = attempt + 1
        task = asyncio.create_task(self._retry_reconcile(room, delay_s))
        self._retry_tasks[room_id] = task
        task.add_done_callback(lambda done: self._observe_background_task("call_reconcile_retry_failed", room_id, done))

    async def _retry_reconcile(self, room: nio.MatrixRoom, delay_s: float) -> None:
        await asyncio.sleep(delay_s)
        self._retry_tasks.pop(room.room_id, None)
        if not self._shutting_down:
            await self._reconcile(room)

    def _clear_reconcile_retry(self, room_id: str) -> None:
        self._retry_attempts.pop(room_id, None)
        task = self._retry_tasks.pop(room_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _schedule_expiry_reconcile(self, room: nio.MatrixRoom, members: list[CallMember]) -> None:
        """Re-read call state when the earliest current membership expires."""
        room_id = room.room_id
        current = self._expiry_handles.pop(room_id, None)
        if current is not None:
            current.cancel()
        if self._shutting_down or not members:
            return
        expires_at_ms = min(member.created_ts + member.expires_ms for member in members)
        delay_s = max(0, expires_at_ms - self._clock_ms()) / 1000
        self._expiry_handles[room_id] = asyncio.get_running_loop().call_later(
            delay_s,
            self._start_expiry_reconcile,
            room,
        )

    def _start_expiry_reconcile(self, room: nio.MatrixRoom) -> None:
        self._expiry_handles.pop(room.room_id, None)
        if self._shutting_down:
            return
        task = asyncio.create_task(self._reconcile(room))
        self._expiry_reconcile_tasks.add(task)
        task.add_done_callback(
            lambda done: self._observe_expiry_reconcile(room.room_id, done),
        )

    def _observe_expiry_reconcile(self, room_id: str, task: asyncio.Task[None]) -> None:
        self._expiry_reconcile_tasks.discard(task)
        self._observe_background_task("call_expiry_reconcile_failed", room_id, task)

    def _observe_background_task(self, event: str, room_id: str, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning(event, room_id=room_id, error=str(task.exception()))

    async def _stop_session(self, session: CallSession, *, event: str = "call_session_stop_failed") -> None:
        """Stop one call without letting teardown failures escape sync callbacks."""
        try:
            await session.stop()
        except Exception as error:
            logger.warning(event, room_id=session.room_id, error=str(error))

    async def _build_tooling(self, room_id: str) -> CallAgentTooling:
        """Build agent tools for the voice session's agent-scoped context."""
        if self._tool_support is None:
            return CallAgentTooling(tools=[], tool_names=())
        try:
            return await build_call_tools(
                agent_name=self._agent_name,
                config=self._config,
                runtime_paths=self._runtime_paths,
                tool_support=self._tool_support,
                room_id=room_id,
            )
        except Exception as error:
            logger.warning("call_tools_build_failed", agent=self._agent_name, room_id=room_id, error=str(error))
            return CallAgentTooling(tools=[], tool_names=())

    async def _resolve_service(self, members: list[CallMember]) -> str | None:
        """Accept only the locally configured or discovered authorization service."""
        oldest_member = min(members, key=lambda member: member.created_ts)
        advertised_url = oldest_member.livekit_service_url
        if advertised_url is None:
            return None
        advertised_focus = _normalized_service_url(advertised_url)
        if advertised_focus is None:
            return None
        local_server_name = MatrixID.parse(self._client.user_id).domain
        trusted_url = self._config.calls.livekit_service_url
        if trusted_url is None:
            trusted_url = await discover_livekit_service_url(
                local_server_name,
                ssl_verify=self._ssl_verify,
                allow_private_networks=True,
            )
        trusted_focus = _normalized_service_url(trusted_url) if trusted_url is not None else None
        if advertised_focus != trusted_focus:
            logger.warning(
                "call_focus_not_trusted",
                user_id=oldest_member.user_id,
                advertised_url=advertised_url,
            )
            return None
        return advertised_focus

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
            allow_private_networks=True,
        )


def _normalized_service_url(url: str) -> str | None:
    """Normalize insignificant URL spelling differences for focus comparison."""
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.host is None
        or parsed.userinfo
        or parsed.query
        or parsed.fragment
    ):
        return None
    return str(parsed).rstrip("/")
