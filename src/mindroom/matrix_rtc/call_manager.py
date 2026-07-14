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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

import aiohttp
import httpx
import nio

from mindroom.authorization import is_authorized_sender, is_sender_allowed_for_agent_reply
from mindroom.config.voice import normalize_speech_base_url
from mindroom.credentials_sync import get_api_key_for_provider, get_api_key_for_service
from mindroom.entity_resolution import configured_call_agent_name_for_room
from mindroom.logging_config import get_logger
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
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
    CascadedVoiceAgentOptions,
    CascadedVoiceBridge,
    RealtimeVoiceBridge,
    SpeechServiceOptions,
    VoiceAgentOptions,
    matrix_calls_dependencies_available,
)
from mindroom.model_defaults import LOCAL_OPENAI_API_KEY_DEFAULT
from mindroom.session_ids import create_session_id

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from collections.abc import Set as AbstractSet

    from mindroom.config.calls import CallAgentConfig
    from mindroom.config.main import Config
    from mindroom.config.voice import SpeechServiceConfig
    from mindroom.constants import RuntimePaths
    from mindroom.matrix_rtc.call_session import VoiceBridgeLike
    from mindroom.matrix_rtc.focus import SfuGrant
    from mindroom.matrix_rtc.voice_agent import CallVoiceAgentOptions
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport

logger = get_logger(__name__)


def _default_bridge_factory(local_identity: str, e2ee_enabled: bool) -> RealtimeVoiceBridge:
    return RealtimeVoiceBridge(local_identity=local_identity, e2ee_enabled=e2ee_enabled)


def _default_cascaded_bridge_factory(local_identity: str, e2ee_enabled: bool) -> CascadedVoiceBridge:
    return CascadedVoiceBridge(local_identity=local_identity, e2ee_enabled=e2ee_enabled)


_CALL_EVENT_TYPES = frozenset({CALL_MEMBER_EVENT_TYPE, RTC_NOTIFICATION_EVENT_TYPE})
_MAX_PENDING_KEYS_PER_ROOM = 64
_PENDING_KEY_TTL_MS = 120_000
_RECONCILE_RETRY_DELAYS_S = (1.0, 5.0, 30.0, 60.0)
_OPENAI_SPEECH_BASE_URL = "https://api.openai.com/v1"
_MATRIX_NETWORK_ERRORS = (nio.exceptions.ProtocolError, OSError, aiohttp.ClientError)
_CALL_NETWORK_ERRORS = (httpx.HTTPError, *_MATRIX_NETWORK_ERRORS)


_VOICE_STYLE_ADDENDUM = (
    "You are participating in a live voice call. Everything you say is spoken "
    "aloud: keep responses short, conversational, and natural, and never use markdown, "
    "lists, or other written formatting."
)


_JoinResult = Literal["joined", "retry", "skip"]


@dataclass(frozen=True)
class _ResolvedVoiceBackend:
    """Credentials and endpoints resolved for one configured call backend."""

    realtime_api_key: str | None = None
    stt: SpeechServiceOptions | None = None
    tts: SpeechServiceOptions | None = None


@dataclass
class _LogicalCallState:
    """State that survives media-session reconnects for one logical call."""

    requester_id: str
    cascaded_session_id: str | None
    join_blocked: bool = False


def _build_call_instructions(chat_system_prompt: str) -> str:
    """Append voice-specific delivery guidance to the chat system prompt."""
    return f"{chat_system_prompt}\n\n{_VOICE_STYLE_ADDENDUM}"


def maybe_build_call_manager(
    *,
    agent_name: str,
    config: Config,
    client: nio.AsyncClient,
    runtime_paths: RuntimePaths,
    ssl_verify: bool,
    tool_support: ToolRuntimeSupport,
    get_invited_rooms_by_agent: Callable[[], Mapping[str, AbstractSet[str]]],
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
        get_invited_rooms_by_agent=get_invited_rooms_by_agent,
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
        bridge_factory: Callable[[str, bool], VoiceBridgeLike] | None = None,
        tool_support: ToolRuntimeSupport,
        get_invited_rooms_by_agent: Callable[[], Mapping[str, AbstractSet[str]]],
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._agent_name = agent_name
        self._config = config
        self._call_config: CallAgentConfig = config.calls.agents[agent_name]
        self._client = client
        self._runtime_paths = runtime_paths
        self._ssl_verify = ssl_verify
        self._bridge_factory = bridge_factory or (
            _default_cascaded_bridge_factory if self._call_config.backend == "cascaded" else _default_bridge_factory
        )
        self._tool_support = tool_support
        self._get_invited_rooms_by_agent = get_invited_rooms_by_agent
        self._clock_ms = clock_ms
        self._key_transport = ToDeviceFrameKeyTransport(client)
        self._sessions: dict[str, CallSession] = {}
        self._pending_keys: dict[str, dict[tuple[str, str, int], ReceivedFrameKey]] = {}
        self._observed_rooms: dict[str, nio.MatrixRoom] = {}
        self._departed_rooms: set[str] = set()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_attempts: dict[str, int] = {}
        self._logical_calls: dict[str, _LogicalCallState] = {}
        self._expiry_handles: dict[str, asyncio.TimerHandle] = {}
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._shutting_down = False

    async def on_room_event(self, room: nio.MatrixRoom, event: nio.UnknownEvent) -> None:
        """Sync callback for custom room events (call membership, ring)."""
        if event.type not in _CALL_EVENT_TYPES or self._shutting_down or not self._is_configured_call_room(room):
            return
        self._observed_rooms[room.room_id] = room
        await self._reconcile(room)

    async def on_room_membership_event(self, room: nio.MatrixRoom, event: nio.RoomMemberEvent) -> None:
        """Reconcile calls when a user's underlying room membership changes."""
        if self._shutting_down:
            return
        if event.state_key == self._client.user_id and event.membership in {"leave", "ban"}:
            if self._is_configured_call_room(room):
                self._departed_rooms.add(room.room_id)
            if self._has_tracked_call_state(room.room_id):
                await self._handle_own_room_leave(room.room_id)
            return
        if event.state_key == self._client.user_id and event.membership == "join":
            self._departed_rooms.discard(room.room_id)
        if not self._is_configured_call_room(room):
            return
        self._observed_rooms[room.room_id] = room
        await self._reconcile(room)

    async def on_sync_room_membership(
        self,
        *,
        joined_room_ids: AbstractSet[str],
        left_room_ids: AbstractSet[str],
    ) -> None:
        """Apply the bot's authoritative joined/left room sections from sync."""
        if self._shutting_down:
            return
        self._departed_rooms.difference_update(joined_room_ids)
        for room_id in left_room_ids:
            room = self._observed_rooms.get(room_id) or self._client.rooms.get(room_id)
            if room is not None and self._is_configured_call_room(room):
                self._departed_rooms.add(room_id)
            if self._has_tracked_call_state(room_id):
                await self._handle_own_room_leave(room_id)

    async def _handle_own_room_leave(self, room_id: str) -> None:
        """Tear down call state immediately when the bot no longer belongs to the room."""
        lock = self._locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            self._observed_rooms.pop(room_id, None)
            self._pending_keys.pop(room_id, None)
            self._clear_logical_call(room_id)
            expiry = self._expiry_handles.pop(room_id, None)
            if expiry is not None:
                expiry.cancel()
            session = self._sessions.pop(room_id, None)
            if session is not None:
                await self._stop_session(session)

    async def on_to_device_event(self, event: nio.ToDeviceEvent) -> None:
        """Sync callback for decrypted call frame-key to-device events."""
        if (
            not isinstance(event, AuthenticatedToDeviceEvent)
            or event.type != CALL_ENCRYPTION_KEYS_EVENT_TYPE
            or self._shutting_down
        ):
            return
        received_at_ms = self._clock_ms()
        parsed = self._key_transport.parse_incoming(event, received_at_ms=received_at_ms)
        if parsed is None:
            logger.warning(
                "call_frame_key_rejected",
                sender=event.sender,
                authenticated_device_id=event.authenticated_device_id,
                reason="invalid_matrixrtc_payload",
            )
            return
        room_id, received = parsed
        if room_id in self._departed_rooms:
            logger.warning(
                "call_frame_key_rejected",
                room_id=room_id,
                sender=received.user_id,
                device_id=received.claimed_device_id,
                reason="departed_call_room",
            )
            return
        if not self._is_configured_call_room_id(room_id):
            logger.warning(
                "call_frame_key_rejected",
                room_id=room_id,
                sender=received.user_id,
                device_id=received.claimed_device_id,
                reason="unknown_call_room",
            )
            return
        if not self._is_authorized_call_member(received.user_id, room_id):
            logger.warning(
                "call_frame_key_rejected",
                room_id=room_id,
                sender=received.user_id,
                device_id=received.claimed_device_id,
                reason="unauthorized_call_member",
            )
            return
        logger.info(
            "call_frame_key_received",
            room_id=room_id,
            sender=received.user_id,
            device_id=received.claimed_device_id,
            key_index=received.key_index,
        )
        session = self._sessions.get(room_id)
        if session is not None and session.on_key_received(received):
            return
        self._queue_pending_key(room_id, received)
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
        background_tasks = [*self._retry_tasks.values(), *self._background_tasks]
        self._retry_tasks.clear()
        self._background_tasks.clear()
        self._retry_attempts.clear()
        self._logical_calls.clear()
        self._observed_rooms.clear()
        self._departed_rooms.clear()
        self._locks.clear()
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

    async def _reconcile(self, room: nio.MatrixRoom, *, retrying: bool = False) -> None:
        if room.room_id in self._departed_rooms or not self._is_configured_call_room(room):
            return
        self._observed_rooms[room.room_id] = room
        room_id = room.room_id
        lock = self._locks.setdefault(room_id, asyncio.Lock())
        async with lock:
            if self._shutting_down or room_id in self._departed_rooms:
                return
            members = await self._fetch_remote_members(room_id)
            if members is None:
                # Transient state-fetch failure: keep any active session alive
                # and retry even if no further call event arrives.
                self._schedule_reconcile_retry(room)
                return
            self._schedule_expiry_reconcile(room, members)
            await self._apply_reconciled_members(room, members, retrying=retrying)

    async def _apply_reconciled_members(
        self,
        room: nio.MatrixRoom,
        members: list[CallMember],
        *,
        retrying: bool = False,
    ) -> None:
        """Apply one authoritative room/call roster to the active session."""
        room_id = room.room_id
        session = self._sessions.get(room_id)
        if not self._members_are_authorized(members, room_id):
            self._clear_logical_call(room_id)
            self._pending_keys.pop(room_id, None)
            if session is not None:
                self._sessions.pop(room_id, None)
                await self._stop_session(session)
            return
        if not members:
            self._clear_logical_call(room_id)
            self._pending_keys.pop(room_id, None)
            if session is not None:
                self._sessions.pop(room_id, None)
                await self._stop_session(session)
            return
        logical_call = self._logical_calls.get(room_id)
        requester_id = members[0].user_id
        if logical_call is None or logical_call.requester_id != requester_id:
            self._clear_logical_call(room_id)
            logical_call = self._start_logical_call(room_id, requester_id)
        if session is None:
            await self._join_if_populated(room, members, retrying=retrying)
            return
        if session.requester_id != requester_id:
            self._sessions.pop(room_id, None)
            await self._stop_session(session)
            await self._join_if_populated(room, members, retrying=retrying)
            return
        await self._update_session_members(room, session, members)

    async def _join_if_populated(
        self,
        room: nio.MatrixRoom,
        members: list[CallMember],
        *,
        retrying: bool = False,
    ) -> None:
        """Join a populated call or finish a successful empty reconciliation."""
        if not members:
            self._clear_logical_call(room.room_id)
            return
        logical_call = self._logical_calls[room.room_id]
        if logical_call.join_blocked or (room.room_id in self._retry_attempts and not retrying):
            return
        result = await self._join(room, members)
        if result == "joined":
            self._clear_reconcile_retry(room.room_id)
        elif result == "retry":
            self._schedule_reconcile_retry(room)
        else:
            self._clear_reconcile_retry(room.room_id)
            self._pending_keys.pop(room.room_id, None)
            logical_call.join_blocked = True

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
                invited_rooms_by_agent=self._get_invited_rooms_by_agent(),
            )
        except ValueError as error:
            logger.warning("call_room_ownership_ambiguous", room_id=room.room_id, error=str(error))
            return False
        return configured_agent == self._agent_name

    def _is_configured_call_room_id(self, room_id: str) -> bool:
        """Return whether this agent is configured to join calls in ``room_id``."""
        room = self._observed_rooms.get(room_id) or self._client.rooms.get(room_id)
        return room is not None and self._is_configured_call_room(room)

    def _has_tracked_call_state(self, room_id: str) -> bool:
        """Return whether this manager has call state to tear down for ``room_id``."""
        return any(
            room_id in state
            for state in (
                self._observed_rooms,
                self._sessions,
                self._pending_keys,
                self._retry_tasks,
                self._retry_attempts,
                self._logical_calls,
                self._expiry_handles,
            )
        )

    def _queue_pending_key(self, room_id: str, received: ReceivedFrameKey) -> None:
        """Retain a bounded, deduplicated key set while a session is starting."""
        pending = self._pending_keys.setdefault(room_id, {})
        cutoff = received.received_at_ms - _PENDING_KEY_TTL_MS
        for identity, queued in list(pending.items()):
            if queued.received_at_ms < cutoff:
                pending.pop(identity)
        identity = (received.user_id, received.claimed_device_id, received.key_index)
        pending.pop(identity, None)
        if len(pending) >= _MAX_PENDING_KEYS_PER_ROOM:
            pending.pop(next(iter(pending)))
        pending[identity] = received

    def _replay_pending_keys(self, room_id: str, session: CallSession) -> None:
        """Replay bounded keys after the session receives an authoritative roster."""
        pending = self._pending_keys.get(room_id, {})
        if not pending:
            return
        retained: dict[tuple[str, str, int], ReceivedFrameKey] = {}
        cutoff = self._clock_ms() - _PENDING_KEY_TTL_MS
        for identity, received in pending.items():
            if received.received_at_ms < cutoff or session.on_key_received(received):
                continue
            retained[identity] = received
            logger.warning(
                "call_frame_key_waiting_for_membership",
                room_id=room_id,
                user_id=received.user_id,
                device_id=received.claimed_device_id,
            )
        if retained:
            self._pending_keys[room_id] = retained
        else:
            self._pending_keys.pop(room_id, None)

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
        """Require one authorized human identity across all call devices."""
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
        user_ids = {member.user_id for member in members}
        if len(user_ids) > 1:
            logger.warning(
                "call_join_skipped_multiple_users",
                room_id=room_id,
                agent=self._agent_name,
                user_ids=sorted(user_ids),
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

    async def _join(self, room: nio.MatrixRoom, members: list[CallMember]) -> _JoinResult:  # noqa: PLR0911
        room_id = room.room_id
        backend = self._resolve_voice_backend(room_id)
        if backend is None:
            return "skip"
        try:
            service = await self._resolve_service(members)
        except _CALL_NETWORK_ERRORS as error:
            logger.warning("call_service_discovery_failed", room_id=room_id, agent=self._agent_name, error=str(error))
            return "retry"
        except ValueError as error:
            logger.warning("call_service_discovery_failed", room_id=room_id, agent=self._agent_name, error=str(error))
            return "skip"
        if service is None:
            logger.warning("call_join_skipped_no_livekit_service", room_id=room_id, agent=self._agent_name)
            return "skip"
        try:
            tooling = await self._build_tooling(
                room_id,
                requester_id=members[0].user_id,
                cascaded=self._call_config.backend == "cascaded",
            )
        except Exception as error:
            logger.warning(
                "call_join_skipped_agent_materialization_failed",
                room_id=room_id,
                agent=self._agent_name,
                error=str(error),
            )
            return "skip"
        try:
            device_id = required_device_id(self._client)
        except CallJoinError as error:
            logger.warning(
                "call_join_skipped_invalid_client",
                room_id=room_id,
                agent=self._agent_name,
                error=str(error),
            )
            return "skip"
        transcript = CallTranscript.start(
            agent_name=self._agent_name,
            config=self._config,
            storage_path=self._runtime_paths.storage_root,
            room_id=room_id,
            room_display_name=room.display_name or room_id,
        )
        bridge = self._bridge_factory(
            f"{self._client.user_id}:{device_id}",
            room.encrypted,
        )
        options = self._build_voice_agent_options(
            tooling=tooling,
            transcript=transcript,
            room=room,
            bridge=bridge,
            backend=backend,
        )
        try:
            session = CallSession(
                room_id=room_id,
                requester_id=members[0].user_id,
                e2ee_enabled=room.encrypted,
                deps=CallSessionDeps(
                    client=self._client,
                    bridge=bridge,
                    key_transport=self._key_transport,
                    fetch_grant=lambda: self._fetch_grant(room_id, service),
                    agent_options=options,
                    livekit_service_url=service,
                    on_stopped=lambda: transcript.finalize(
                        config=self._config,
                        runtime_paths=self._runtime_paths,
                        storage_path=self._runtime_paths.storage_root,
                    ),
                    on_failure=lambda message: self._send_call_failure_notice(room_id, message),
                ),
            )
            await session.start(members)
        except ValueError as error:
            logger.warning("call_join_rejected", room_id=room_id, agent=self._agent_name, error=str(error))
            return "skip"
        except (CallJoinError, *_CALL_NETWORK_ERRORS) as error:
            logger.warning("call_join_failed", room_id=room_id, agent=self._agent_name, error=str(error))
            return "retry"
        if self._shutting_down:
            # shutdown() ran while the join was in flight and cannot see this
            # session yet; stop it instead of leaking a live SFU connection.
            self._pending_keys.pop(room_id, None)
            await self._stop_session(session)
            return "skip"
        self._sessions[room_id] = session
        self._replay_pending_keys(room_id, session)
        logger.info("call_session_started", room_id=room_id, agent=self._agent_name)
        return "joined"

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
        task = self._retry_tasks.pop(room.room_id, None)
        if task is None:
            return
        self._background_tasks.add(task)
        try:
            if not self._shutting_down:
                await self._reconcile(room, retrying=True)
        finally:
            self._background_tasks.discard(task)

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
        self._track_background_task(task, event="call_expiry_reconcile_failed", room_id=room.room_id)

    def _track_background_task(self, task: asyncio.Task[None], *, event: str, room_id: str) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(lambda done: self._observe_tracked_task(event, room_id, done))

    def _observe_tracked_task(self, event: str, room_id: str, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        self._observe_background_task(event, room_id, task)

    def _schedule_session_termination(
        self,
        room: nio.MatrixRoom,
        bridge: VoiceBridgeLike,
        *,
        retryable: bool,
    ) -> None:
        """Schedule cleanup after an unexpected terminal voice-session close."""
        if self._shutting_down:
            return
        task = asyncio.create_task(self._handle_session_termination(room, bridge, retryable=retryable))
        self._track_background_task(task, event="call_session_termination_failed", room_id=room.room_id)

    def _schedule_call_failure_notice(self, room_id: str, message: str) -> None:
        """Publish an asynchronous diagnostic without blocking SDK callbacks."""
        if self._shutting_down:
            return
        task = asyncio.create_task(self._send_call_failure_notice(room_id, message))
        self._track_background_task(task, event="call_failure_notice_failed", room_id=room_id)

    async def _send_call_failure_notice(self, room_id: str, message: str) -> None:
        """Post a cross-client Matrix notice explaining a silent call failure."""
        try:
            response = await self._client.room_send(
                room_id,
                message_type="m.room.message",
                content={
                    "msgtype": "m.notice",
                    "body": message,
                    "chat.mindroom.call_failure": {"version": 1},
                },
                ignore_unverified_devices=True,
            )
        except _MATRIX_NETWORK_ERRORS as error:
            logger.warning("call_failure_notice_send_failed", room_id=room_id, error=str(error))
            return
        if isinstance(response, nio.RoomSendError):
            logger.warning("call_failure_notice_send_failed", room_id=room_id, error=response.message)
            return
        logger.info("call_failure_notice_sent", room_id=room_id)

    async def _handle_session_termination(
        self,
        room: nio.MatrixRoom,
        bridge: VoiceBridgeLike,
        *,
        retryable: bool,
    ) -> None:
        lock = self._locks.setdefault(room.room_id, asyncio.Lock())
        async with lock:
            session = self._sessions.get(room.room_id)
            if session is None or session.deps.bridge is not bridge:
                return
            self._clear_reconcile_retry(room.room_id)
            logical_call = self._logical_calls.get(room.room_id)
            if logical_call is not None:
                logical_call.join_blocked = True
            await self._stop_session(session, event="call_failed_session_stop_failed")
            if self._sessions.get(room.room_id) is session:
                self._sessions.pop(room.room_id, None)
            if retryable and not self._shutting_down:
                if logical_call is not None:
                    logical_call.join_blocked = False
                self._schedule_reconcile_retry(room)

    def _observe_background_task(self, event: str, room_id: str, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.warning(event, room_id=room_id, error=str(task.exception()))

    async def _stop_session(self, session: CallSession, *, event: str = "call_session_stop_failed") -> None:
        """Stop one call without letting teardown failures escape sync callbacks."""
        try:
            await session.stop()
        except Exception as error:
            logger.warning(event, room_id=session.room_id, error=str(error))

    async def _build_tooling(
        self,
        room_id: str,
        *,
        requester_id: str,
        cascaded: bool,
    ) -> CallAgentTooling:
        """Build agent tools with the sole caller as the Matrix requester."""
        logical_call = self._logical_calls[room_id]
        session_id = logical_call.cascaded_session_id if cascaded else None
        return await build_call_tools(
            agent_name=self._agent_name,
            config=self._config,
            runtime_paths=self._runtime_paths,
            tool_support=self._tool_support,
            room_id=room_id,
            requester_id=requester_id,
            session_id=session_id,
            enable_responder=cascaded,
            voice_instructions=_VOICE_STYLE_ADDENDUM if cascaded else None,
        )

    def _start_logical_call(self, room_id: str, requester_id: str) -> _LogicalCallState:
        """Create the state shared by every media attempt for one caller presence."""
        session_id = None
        if self._call_config.backend == "cascaded":
            session_id = f"{create_session_id(room_id, None)}:call:{uuid4().hex}"
        logical_call = _LogicalCallState(requester_id=requester_id, cascaded_session_id=session_id)
        self._logical_calls[room_id] = logical_call
        return logical_call

    def _clear_logical_call(self, room_id: str) -> None:
        """Forget retry and conversation state after the remote call ends."""
        self._logical_calls.pop(room_id, None)
        self._clear_reconcile_retry(room_id)

    @property
    def voice_backend_available(self) -> bool:
        """Return whether the configured voice backend has its runtime credentials."""
        return self._resolve_voice_backend(room_id=None, warn_if_unavailable=False) is not None

    def _resolve_voice_backend(
        self,
        room_id: str | None,
        *,
        warn_if_unavailable: bool = True,
    ) -> _ResolvedVoiceBackend | None:
        """Resolve backend-specific credentials without affecting call lifecycle."""
        if self._call_config.backend == "realtime":
            api_key = get_api_key_for_service(
                self._call_config.credentials_service,
                self._runtime_paths,
            )
            if not api_key:
                if warn_if_unavailable:
                    logger.warning(
                        "call_join_skipped_no_openai_key",
                        room_id=room_id,
                        agent=self._agent_name,
                        credentials_service=self._call_config.credentials_service,
                    )
                return None
            return _ResolvedVoiceBackend(realtime_api_key=api_key)

        stt_config = self._call_config.stt
        tts_config = self._call_config.tts
        if stt_config is None or tts_config is None:
            msg = "Cascaded call configuration requires STT and TTS"
            raise ValueError(msg)
        stt = self._resolve_speech_service(
            stt_config,
            component="stt",
            room_id=room_id,
            warn_if_unavailable=warn_if_unavailable,
        )
        tts = self._resolve_speech_service(
            tts_config,
            component="tts",
            room_id=room_id,
            warn_if_unavailable=warn_if_unavailable,
        )
        if stt is None or tts is None:
            return None
        return _ResolvedVoiceBackend(stt=stt, tts=tts)

    def _build_voice_agent_options(
        self,
        *,
        tooling: CallAgentTooling,
        transcript: CallTranscript,
        room: nio.MatrixRoom,
        bridge: VoiceBridgeLike,
        backend: _ResolvedVoiceBackend,
    ) -> CallVoiceAgentOptions:
        """Build selected-backend options with shared transcript hooks."""

        def on_session_terminated(retryable: bool) -> None:
            self._schedule_session_termination(room, bridge, retryable=retryable)

        def on_session_error(message: str) -> None:
            self._schedule_call_failure_notice(room.room_id, message)

        if self._call_config.backend == "realtime":
            if backend.realtime_api_key is None:
                msg = "Realtime call API key was not resolved"
                raise RuntimeError(msg)
            return VoiceAgentOptions(
                instructions=_build_call_instructions(tooling.instructions),
                model=self._call_config.model,
                api_key=backend.realtime_api_key,
                voice=self._call_config.voice,
                greeting_instructions="Briefly greet the caller and let them know you joined the call.",
                tools=tooling.tools,
                on_conversation_turn=transcript.record,
                on_tools_executed=transcript.record_tool_use,
                on_session_terminated=on_session_terminated,
                on_session_error=on_session_error,
            )
        if backend.stt is None or backend.tts is None or tooling.responder is None:
            msg = "Cascaded call agent was not fully materialized"
            raise RuntimeError(msg)
        return CascadedVoiceAgentOptions(
            stt=backend.stt,
            tts=backend.tts,
            respond=tooling.responder,
            finalize_spoken_response=tooling.finalize_spoken_response,
            greeting_text="Hello, I joined the call.",
            on_conversation_turn=transcript.record,
            on_tools_executed=transcript.record_tool_use,
            on_session_terminated=on_session_terminated,
            on_session_error=on_session_error,
        )

    def _resolve_speech_service(
        self,
        service: SpeechServiceConfig,
        *,
        component: str,
        room_id: str | None,
        warn_if_unavailable: bool = True,
    ) -> SpeechServiceOptions | None:
        """Resolve one independently credentialed cloud or local speech service."""
        base_url = normalize_speech_base_url(service.host)
        if base_url is None and service.provider == "openai":
            base_url = _OPENAI_SPEECH_BASE_URL
        api_key = service.api_key
        if api_key is None and service.provider == "openai_compatible":
            api_key = LOCAL_OPENAI_API_KEY_DEFAULT
        if api_key is None:
            api_key = get_api_key_for_provider(service.provider, self._runtime_paths)
        if not api_key and warn_if_unavailable:
            logger.warning(
                "call_join_skipped_no_speech_key",
                room_id=room_id,
                agent=self._agent_name,
                component=component,
                provider=service.provider,
            )
        if not api_key:
            return None
        return SpeechServiceOptions(
            provider=service.provider,
            model=service.model,
            api_key=api_key,
            base_url=base_url,
            extra_kwargs=dict(service.extra_kwargs),
        )

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
            if not advertised_focus.startswith("https://"):
                return None
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
