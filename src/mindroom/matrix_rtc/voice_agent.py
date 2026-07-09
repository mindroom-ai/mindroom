"""LiveKit media bridge running an OpenAI realtime voice agent in a call.

This is the media plane of a MatrixRTC call: it connects to the LiveKit SFU
with the credentials minted by the MatrixRTC Authorization Service, applies
per-participant frame-encryption keys, and drives a ``livekit-agents``
``AgentSession`` backed by an OpenAI speech-to-speech realtime model.

The heavy ``livekit`` / ``livekit-agents`` dependencies are optional (the
``matrix_calls`` extra), so all imports happen inside functions.
"""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from livekit import rtc
    from livekit.agents import AgentSession
    from livekit.agents.voice.events import ConversationItemAddedEvent, FunctionToolsExecutedEvent
    from livekit.agents.voice.io import AudioInput

    from mindroom.matrix_rtc.focus import SfuGrant

logger = get_logger(__name__)

#: Frame-crypto settings mirroring Element Call's ``MatrixKeyProvider``
#: (``keyringSize: 256`` fits the 0-255 key indices, ``ratchetWindowSize: 10``).
_KEY_RING_SIZE = 256
_RATCHET_WINDOW_SIZE = 10
_AUDIO_SAMPLE_RATE = 24_000
_AUDIO_CHANNELS = 1
_AUDIO_FRAME_SIZE_MS = 50


class _AudioFrameStream:
    """Convert LiveKit ``AudioFrameEvent`` items into mixer-ready frames."""

    def __init__(self, stream: rtc.AudioStream) -> None:
        self._stream = stream

    def __aiter__(self) -> _AudioFrameStream:
        return self

    async def __anext__(self) -> rtc.AudioFrame:
        return (await self._stream.__anext__()).frame

    async def aclose(self) -> None:
        """Close the underlying SDK audio stream."""
        await self._stream.aclose()


class _AuthorizedParticipantAudioInput:
    """Mix microphone audio only from identities in the Matrix call roster."""

    label = "matrix-rtc-authorized-participants"

    def __init__(self, room: rtc.Room, rtc_module: ModuleType, participant_identities: frozenset[str]) -> None:
        self._room = room
        self._rtc = rtc_module
        self._participant_identities = participant_identities
        self._mixer = rtc_module.AudioMixer(
            _AUDIO_SAMPLE_RATE,
            _AUDIO_CHANNELS,
            blocksize=_AUDIO_SAMPLE_RATE * _AUDIO_FRAME_SIZE_MS // 1000,
        )
        self._streams: dict[str, tuple[str, _AudioFrameStream]] = {}
        self._close_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        room.on("participant_connected", self._on_participant_connected)
        room.on("participant_disconnected", self._on_participant_disconnected)
        room.on("track_published", self._on_track_published)
        room.on("track_unpublished", self._on_track_unpublished)
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_unsubscribed", self._on_track_unsubscribed)
        for participant in room.remote_participants.values():
            self._sync_participant(participant)

    def __aiter__(self) -> _AuthorizedParticipantAudioInput:
        return self

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._mixer.__anext__()

    def on_attached(self) -> None:
        """Satisfy the LiveKit AgentSession audio-input interface."""

    def on_detached(self) -> None:
        """Satisfy the LiveKit AgentSession audio-input interface."""

    def set_participant_identities(self, participant_identities: frozenset[str]) -> None:
        """Apply a new authoritative roster and resubscribe immediately."""
        if self._closed:
            return
        self._participant_identities = participant_identities
        for publication_sid, (identity, _stream) in list(self._streams.items()):
            if identity not in participant_identities:
                self._remove_stream(publication_sid)
        for participant in self._room.remote_participants.values():
            self._sync_participant(participant)

    def _is_microphone(self, publication: rtc.RemoteTrackPublication) -> bool:
        return (
            publication.kind == self._rtc.TrackKind.KIND_AUDIO
            and publication.source == self._rtc.TrackSource.SOURCE_MICROPHONE
        )

    def _sync_participant(self, participant: rtc.RemoteParticipant) -> None:
        allowed = participant.identity in self._participant_identities
        for publication in participant.track_publications.values():
            if not self._is_microphone(publication):
                continue
            if not allowed:
                self._remove_stream(publication.sid)
            if publication.subscribed != allowed:
                publication.set_subscribed(allowed)
            if allowed and publication.track is not None:
                self._add_stream(publication.sid, participant.identity, publication.track)

    def _add_stream(self, publication_sid: str, participant_identity: str, track: rtc.RemoteTrack) -> None:
        if publication_sid in self._streams or participant_identity not in self._participant_identities:
            return
        stream = _AudioFrameStream(
            self._rtc.AudioStream(
                track,
                sample_rate=_AUDIO_SAMPLE_RATE,
                num_channels=_AUDIO_CHANNELS,
                frame_size_ms=_AUDIO_FRAME_SIZE_MS,
            ),
        )
        self._streams[publication_sid] = (participant_identity, stream)
        self._mixer.add_stream(stream)

    def _remove_stream(self, publication_sid: str) -> None:
        entry = self._streams.pop(publication_sid, None)
        if entry is None:
            return
        _identity, stream = entry
        self._mixer.remove_stream(stream)
        task = asyncio.create_task(stream.aclose())
        self._close_tasks.add(task)
        task.add_done_callback(self._observe_close_task)

    def _observe_close_task(self, task: asyncio.Task[None]) -> None:
        self._close_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("call_audio_stream_close_failed", error=str(task.exception()))

    def _on_participant_connected(self, participant: rtc.RemoteParticipant) -> None:
        self._sync_participant(participant)

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        for publication_sid, (identity, _stream) in list(self._streams.items()):
            if identity == participant.identity:
                self._remove_stream(publication_sid)

    def _on_track_published(
        self,
        _publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        self._sync_participant(participant)

    def _on_track_unpublished(
        self,
        publication: rtc.RemoteTrackPublication,
        _participant: rtc.RemoteParticipant,
    ) -> None:
        self._remove_stream(publication.sid)

    def _on_track_subscribed(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if participant.identity not in self._participant_identities or not self._is_microphone(publication):
            publication.set_subscribed(False)
            return
        self._add_stream(publication.sid, participant.identity, track)

    def _on_track_unsubscribed(
        self,
        _track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        _participant: rtc.RemoteParticipant,
    ) -> None:
        self._remove_stream(publication.sid)

    async def aclose(self) -> None:
        """Unregister room listeners and close every participant stream."""
        if self._closed:
            return
        self._closed = True
        self._room.off("participant_connected", self._on_participant_connected)
        self._room.off("participant_disconnected", self._on_participant_disconnected)
        self._room.off("track_published", self._on_track_published)
        self._room.off("track_unpublished", self._on_track_unpublished)
        self._room.off("track_subscribed", self._on_track_subscribed)
        self._room.off("track_unsubscribed", self._on_track_unsubscribed)
        for publication_sid in list(self._streams):
            self._remove_stream(publication_sid)
        await self._mixer.aclose()
        if self._close_tasks:
            await asyncio.gather(*self._close_tasks, return_exceptions=True)


def matrix_calls_dependencies_available() -> bool:
    """Whether the optional ``matrix_calls`` extra is installed."""
    # find_spec("livekit.rtc") raises (rather than returning None) when the
    # parent "livekit" package itself is missing.
    try:
        return (
            importlib.util.find_spec("livekit.rtc") is not None
            and importlib.util.find_spec("livekit.agents") is not None
            and importlib.util.find_spec("livekit.plugins.openai") is not None
        )
    except ModuleNotFoundError:
        return False


@dataclass(frozen=True)
class VoiceAgentOptions:
    """Everything the realtime voice agent needs to join and speak."""

    instructions: str
    model: str
    api_key: str
    voice: str | None = None
    greeting_instructions: str | None = None
    #: LiveKit function tools exposed to the realtime model.
    tools: tuple[Any, ...] = ()
    #: Called with (speaker, text) for every finalized conversation turn.
    on_conversation_turn: Callable[[str, str], None] | None = None
    #: Called with executed tool names after each tool round.
    on_tools_executed: Callable[[list[str]], None] | None = None


class RealtimeVoiceBridge:
    """One LiveKit connection with an OpenAI realtime agent on top."""

    def __init__(self, *, local_identity: str, e2ee_enabled: bool) -> None:
        self._local_identity = local_identity
        self._e2ee_enabled = e2ee_enabled
        self._room: Any = None
        self._session: Any = None
        self._audio_input: _AuthorizedParticipantAudioInput | None = None
        self._participant_identities: frozenset[str] = frozenset()

    def set_participant_identities(self, participant_identities: frozenset[str]) -> None:
        """Restrict SFU subscriptions and published output to the Matrix roster."""
        self._participant_identities = participant_identities
        if self._audio_input is not None:
            self._audio_input.set_participant_identities(participant_identities)
        self._apply_output_permissions()

    async def connect(self, grant: SfuGrant) -> None:
        """Connect to the SFU, enabling frame encryption when required."""
        from livekit import rtc  # noqa: PLC0415

        options = rtc.RoomOptions(auto_subscribe=False)
        if self._e2ee_enabled:
            options = rtc.RoomOptions(
                auto_subscribe=False,
                e2ee=rtc.E2EEOptions(
                    key_provider_options=rtc.KeyProviderOptions(
                        ratchet_window_size=_RATCHET_WINDOW_SIZE,
                        key_ring_size=_KEY_RING_SIZE,
                    ),
                ),
            )
        room = rtc.Room()
        await room.connect(grant.url, grant.jwt, options)
        self._room = room
        self._apply_output_permissions()
        logger.info("call_sfu_connected", url=grant.url, identity=self._local_identity)

    def _apply_output_permissions(self) -> None:
        """Allow only current Matrix call members to subscribe to our tracks."""
        if self._room is None:
            return
        from livekit import rtc  # noqa: PLC0415

        permissions = [
            rtc.ParticipantTrackPermission(participant_identity=identity, allow_all=True)
            for identity in sorted(self._participant_identities)
        ]
        self._room.local_participant.set_track_subscription_permissions(
            allow_all_participants=False,
            participant_permissions=permissions,
        )

    def set_frame_key(self, participant_identity: str, key: bytes, key_index: int) -> None:
        """Install a media frame key for one participant (or ourselves)."""
        if self._room is None or not self._e2ee_enabled:
            return
        self._room.e2ee_manager.key_provider.set_key(participant_identity, key, key_index)

    async def start_agent(self, options: VoiceAgentOptions) -> None:
        """Start the realtime agent session on the connected room."""
        from livekit import rtc  # noqa: PLC0415
        from livekit.agents import Agent, AgentSession, room_io  # noqa: PLC0415
        from livekit.plugins.openai import realtime  # noqa: PLC0415

        if self._room is None:
            msg = "connect() must succeed before start_agent()"
            raise RuntimeError(msg)
        if options.voice:
            model = realtime.RealtimeModel(model=options.model, api_key=options.api_key, voice=options.voice)
        else:
            model = realtime.RealtimeModel(model=options.model, api_key=options.api_key)
        session = AgentSession(llm=model)
        self._session = session
        audio_input = _AuthorizedParticipantAudioInput(self._room, rtc, self._participant_identities)
        self._audio_input = audio_input
        session.input.audio = cast("AudioInput", audio_input)
        self._register_session_listeners(session, options)
        agent = Agent(instructions=options.instructions, tools=list(options.tools))
        await session.start(
            agent,
            room=self._room,
            room_options=room_io.RoomOptions(
                audio_input=False,
                text_input=False,
                text_output=False,
                close_on_disconnect=False,
            ),
        )
        if options.greeting_instructions:
            session.generate_reply(instructions=options.greeting_instructions)

    def _register_session_listeners(self, session: AgentSession, options: VoiceAgentOptions) -> None:
        from livekit.agents.llm import ChatMessage  # noqa: PLC0415

        on_turn = options.on_conversation_turn
        if on_turn is not None:

            def _on_item_added(event: ConversationItemAddedEvent) -> None:
                item = event.item
                if not isinstance(item, ChatMessage):
                    return
                text = item.text_content
                if text:
                    on_turn(str(item.role), text)

            session.on("conversation_item_added", _on_item_added)

        on_tools = options.on_tools_executed
        if on_tools is not None:

            def _on_tools_executed(event: FunctionToolsExecutedEvent) -> None:
                on_tools([call.name for call in event.function_calls])

            session.on("function_tools_executed", _on_tools_executed)

    async def aclose(self) -> None:
        """Tear down the agent session and leave the SFU."""
        session = self._session
        self._session = None
        audio_input = self._audio_input
        self._audio_input = None
        room = self._room
        self._room = None
        try:
            if session is not None:
                await session.aclose()
        finally:
            try:
                if audio_input is not None:
                    await audio_input.aclose()
            finally:
                if room is not None:
                    await room.disconnect()
