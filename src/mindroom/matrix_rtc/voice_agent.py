"""LiveKit media bridges for realtime and cascaded MindRoom voice calls.

This is the media plane of a MatrixRTC call: it connects to the LiveKit SFU
with the credentials minted by the MatrixRTC Authorization Service, applies
per-participant frame-encryption keys, and drives a ``livekit-agents``
``AgentSession`` backed by either OpenAI realtime speech-to-speech or separate
STT, normal MindRoom agent, and TTS components.

The heavy ``livekit`` / ``livekit-agents`` dependencies are optional (the
``matrix_calls`` extra), so all imports happen inside functions.
"""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import ModuleType

    from livekit import rtc
    from livekit.agents import Agent as LiveKitAgent
    from livekit.agents import AgentSession, APIConnectOptions, NotGivenOr
    from livekit.agents.llm import LLM, ChatContext, LLMStream, Tool, ToolChoice
    from livekit.agents.voice.events import (
        CloseEvent,
        ConversationItemAddedEvent,
        ErrorEvent,
        FunctionToolsExecutedEvent,
        SpeechCreatedEvent,
    )
    from livekit.agents.voice.io import AudioInput
    from livekit.agents.voice.speech_handle import SpeechHandle

    from mindroom.matrix_rtc.call_tools import CallAgentResponse
    from mindroom.matrix_rtc.focus import SfuGrant

logger = get_logger(__name__)

#: Frame-crypto settings mirroring Element Call's ``MatrixKeyProvider``
#: (``keyringSize: 256`` fits the 0-255 key indices, ``ratchetWindowSize: 10``).
_KEY_RING_SIZE = 256
_RATCHET_WINDOW_SIZE = 10
_SFU_CONNECT_TIMEOUT_S = 10.0
_SFU_CONNECT_CANCEL_TIMEOUT_S = 1.0
_AUDIO_SAMPLE_RATE = 24_000
_AUDIO_CHANNELS = 1
_AUDIO_FRAME_SIZE_MS = 50
_CALL_TURN_CORRELATION_KEY = "mindroom_call_turn_id"


class _AudioFrameStream:
    """Convert LiveKit ``AudioFrameEvent`` items into mixer-ready frames."""

    def __init__(self, stream: rtc.AudioStream, participant_identity: str) -> None:
        self._stream = stream
        self._participant_identity = participant_identity
        self._received_first_frame = False

    def __aiter__(self) -> _AudioFrameStream:
        return self

    async def __anext__(self) -> rtc.AudioFrame:
        frame = (await self._stream.__anext__()).frame
        if not self._received_first_frame:
            self._received_first_frame = True
            logger.info("call_audio_first_frame", participant=self._participant_identity)
        return frame

    async def aclose(self) -> None:
        """Close the underlying SDK audio stream."""
        await self._stream.aclose()


class _AuthorizedParticipantAudioInput:
    """Mix microphone audio only from identities in the Matrix call roster."""

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

    @property
    def label(self) -> str:
        """Describe this leaf in LiveKit's input-chain diagnostics."""
        return "authorized MatrixRTC participants"

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._mixer.__anext__()

    @property
    def source(self) -> AudioInput | None:
        """Satisfy the LiveKit AgentSession audio-input interface (terminal input, no upstream)."""
        return None

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
            participant_identity,
        )
        self._streams[publication_sid] = (participant_identity, stream)
        self._mixer.add_stream(stream)
        logger.info("call_audio_stream_added", participant=participant_identity, publication_sid=publication_sid)

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
    #: Called after an unexpected terminal SDK close; bool means retryable.
    on_session_terminated: Callable[[bool], None] | None = None
    #: Called with a safe, actionable user-facing description of a runtime failure.
    on_session_error: Callable[[str], None] | None = None


@dataclass(frozen=True)
class SpeechServiceOptions:
    """Resolved connection settings for one cascaded speech component."""

    provider: str
    model: str
    api_key: str
    base_url: str | None = None
    extra_kwargs: dict[str, Any] | None = None


@dataclass(frozen=True)
class CascadedVoiceAgentOptions:
    """Everything the cascaded voice agent needs to join and speak."""

    stt: SpeechServiceOptions
    tts: SpeechServiceOptions
    respond: Callable[[str, Callable[[list[str]], None] | None], Awaitable[CallAgentResponse]]
    finalize_spoken_response: Callable[[str | None, str, bool], Awaitable[None] | None] | None = None
    greeting_text: str | None = None
    on_conversation_turn: Callable[[str, str], None] | None = None
    on_tools_executed: Callable[[list[str]], None] | None = None
    on_session_terminated: Callable[[bool], None] | None = None
    on_session_error: Callable[[str], None] | None = None


type CallVoiceAgentOptions = VoiceAgentOptions | CascadedVoiceAgentOptions


class RealtimeVoiceBridge:
    """One LiveKit connection with an OpenAI realtime agent on top."""

    def __init__(self, *, local_identity: str, e2ee_enabled: bool) -> None:
        self._local_identity = local_identity
        self._e2ee_enabled = e2ee_enabled
        self._room: Any = None
        self._connect_task: asyncio.Task[None] | None = None
        self._session: Any = None
        self._owned_speech_resource_closers: tuple[Callable[[], Awaitable[None]], ...] = ()
        self._session_event_tasks: set[asyncio.Future[None]] = set()
        self._reported_error_notices: set[str] = set()
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

        options = rtc.RoomOptions(auto_subscribe=False, connect_timeout=_SFU_CONNECT_TIMEOUT_S)
        if self._e2ee_enabled:
            options = rtc.RoomOptions(
                auto_subscribe=False,
                connect_timeout=_SFU_CONNECT_TIMEOUT_S,
                e2ee=rtc.E2EEOptions(
                    key_provider_options=rtc.KeyProviderOptions(
                        ratchet_window_size=_RATCHET_WINDOW_SIZE,
                        key_ring_size=_KEY_RING_SIZE,
                        key_derivation_function=rtc.KeyDerivationFunction.HKDF,
                    ),
                ),
            )
        room = rtc.Room()
        self._room = room
        connect_task = asyncio.create_task(
            room.connect(grant.url, grant.jwt, options),
            name="matrix_rtc_livekit_connect",
        )
        self._connect_task = connect_task
        try:
            await asyncio.shield(connect_task)
        finally:
            if connect_task.done():
                self._connect_task = None
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
        logger.info("call_output_permissions_applied", participants=sorted(self._participant_identities))

    def set_frame_key(self, participant_identity: str, key: bytes, key_index: int) -> None:
        """Install a media frame key for one participant (or ourselves)."""
        if self._room is None or not self._e2ee_enabled:
            return
        self._room.e2ee_manager.key_provider.set_key(participant_identity, key, key_index)

    async def start_agent(self, options: CallVoiceAgentOptions) -> None:
        """Start the realtime agent session on the connected room."""
        from livekit import rtc  # noqa: PLC0415
        from livekit.agents import Agent, AgentSession, room_io  # noqa: PLC0415
        from livekit.plugins.openai import realtime  # noqa: PLC0415

        if self._room is None:
            msg = "connect() must succeed before start_agent()"
            raise RuntimeError(msg)
        if not isinstance(options, VoiceAgentOptions):
            msg = "RealtimeVoiceBridge requires realtime agent options"
            raise TypeError(msg)
        if options.voice:
            model = realtime.RealtimeModel(model=options.model, api_key=options.api_key, voice=options.voice)
        else:
            model = realtime.RealtimeModel(model=options.model, api_key=options.api_key)
        self._owned_speech_resource_closers += (model.aclose,)
        session = AgentSession(llm=model)
        agent = Agent(instructions=options.instructions, tools=list(options.tools))
        await self._start_session(session, agent, options, rtc_module=rtc, room_io_module=room_io)
        if options.greeting_instructions:
            session.generate_reply(instructions=options.greeting_instructions)

    async def _start_session(
        self,
        session: AgentSession,
        agent: LiveKitAgent,
        options: CallVoiceAgentOptions,
        *,
        rtc_module: ModuleType,
        room_io_module: ModuleType,
    ) -> None:
        """Attach authorized audio and start one configured LiveKit session."""
        if self._room is None:
            msg = "connect() must succeed before start_agent()"
            raise RuntimeError(msg)
        self._session = session
        audio_input = _AuthorizedParticipantAudioInput(self._room, rtc_module, self._participant_identities)
        self._audio_input = audio_input
        session.input.audio = cast("AudioInput", audio_input)
        self._register_session_listeners(session, options)
        await session.start(
            agent,
            room=self._room,
            room_options=room_io_module.RoomOptions(
                audio_input=False,
                text_input=False,
                text_output=False,
                close_on_disconnect=False,
            ),
        )
        self._log_media_snapshot()
        if isinstance(options, CascadedVoiceAgentOptions):
            private_sync_close = await _attach_private_transcript_synchronizer(session)
            self._owned_speech_resource_closers += (private_sync_close,)

    def _log_media_snapshot(self) -> None:
        """Log local publications and remote subscription state for call diagnostics."""
        if self._room is None:
            return
        local_tracks = [
            str(publication.sid) for publication in self._room.local_participant.track_publications.values()
        ]
        remotes = {
            participant.identity: [
                {"sid": str(publication.sid), "subscribed": publication.subscribed, "muted": publication.muted}
                for publication in participant.track_publications.values()
            ]
            for participant in self._room.remote_participants.values()
        }
        logger.info(
            "call_media_snapshot",
            local_published_tracks=local_tracks,
            remote_participants=remotes,
            roster=sorted(self._participant_identities),
        )

    def _register_session_listeners(self, session: AgentSession, options: CallVoiceAgentOptions) -> None:
        self._register_conversation_listener(session, options)
        if isinstance(options, CascadedVoiceAgentOptions) and options.finalize_spoken_response is not None:
            self._register_unforwarded_speech_listener(session, options.finalize_spoken_response)

        on_tools = options.on_tools_executed
        if on_tools is not None:

            def _on_tools_executed(event: FunctionToolsExecutedEvent) -> None:
                on_tools([call.name for call in event.function_calls])

            session.on("function_tools_executed", _on_tools_executed)

        self._register_termination_listener(session, options)
        self._register_error_listener(session, options)

    def _register_error_listener(self, session: AgentSession, options: CallVoiceAgentOptions) -> None:
        """Turn provider/runtime failures into safe, actionable call notices."""
        on_error = options.on_session_error
        if on_error is None:
            return

        def _on_error(event: ErrorEvent) -> None:
            if self._session is not session:
                return
            notice = _describe_voice_error(event)
            if notice in self._reported_error_notices:
                return
            self._reported_error_notices.add(notice)
            on_error(notice)

        session.on("error", _on_error)

    def _register_conversation_listener(self, session: AgentSession, options: CallVoiceAgentOptions) -> None:
        """Record transcript items and reconcile cascaded assistant playout."""
        from livekit.agents.llm import ChatMessage  # noqa: PLC0415

        on_turn = options.on_conversation_turn
        finalize_spoken_response = (
            options.finalize_spoken_response if isinstance(options, CascadedVoiceAgentOptions) else None
        )
        if on_turn is not None or finalize_spoken_response is not None:

            def _on_item_added(event: ConversationItemAddedEvent) -> None:
                item = event.item
                if not isinstance(item, ChatMessage):
                    return
                text = item.text_content
                if text:
                    if on_turn is not None:
                        on_turn(str(item.role), text)
                    if item.role == "assistant" and finalize_spoken_response is not None:
                        correlation_id = item.extra.get(_CALL_TURN_CORRELATION_KEY)
                        self._schedule_session_event(
                            finalize_spoken_response(
                                correlation_id if isinstance(correlation_id, str) else None,
                                text,
                                item.interrupted,
                            ),
                        )

            session.on("conversation_item_added", _on_item_added)

    def _register_unforwarded_speech_listener(
        self,
        session: AgentSession,
        finalize_spoken_response: Callable[[str | None, str, bool], Awaitable[None] | None],
    ) -> None:
        """Reconcile generated responses that produced no assistant transcript."""
        from livekit.agents.llm import ChatMessage  # noqa: PLC0415

        def _on_speech_created(event: SpeechCreatedEvent) -> None:
            if event.source != "generate_reply":
                return

            def _on_speech_done(handle: SpeechHandle) -> None:
                forwarded = any(
                    isinstance(item, ChatMessage)
                    and item.role == "assistant"
                    and isinstance(item.extra.get(_CALL_TURN_CORRELATION_KEY), str)
                    for item in handle.chat_items
                )
                if not forwarded:
                    self._schedule_session_event(finalize_spoken_response(None, "", True))

            event.speech_handle.add_done_callback(_on_speech_done)

        session.on("speech_created", _on_speech_created)

    def _schedule_session_event(self, operation: Awaitable[None] | None) -> None:
        """Track async session-event work so call teardown waits for persistence."""
        if operation is None:
            return
        task = asyncio.ensure_future(operation)
        self._session_event_tasks.add(task)
        task.add_done_callback(self._observe_session_event_task)

    def _observe_session_event_task(self, task: asyncio.Future[None]) -> None:
        self._session_event_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("call_session_event_failed", error=str(task.exception()))

    def _register_termination_listener(self, session: AgentSession, options: CallVoiceAgentOptions) -> None:
        from livekit.agents import APIError, CloseReason  # noqa: PLC0415

        on_terminated = options.on_session_terminated
        if on_terminated is not None:

            def _on_close(event: CloseEvent) -> None:
                if self._session is not session:
                    return
                retryable = event.reason == CloseReason.ERROR
                if retryable and event.error is not None and isinstance(event.error.error, APIError):
                    retryable = event.error.error.retryable
                on_terminated(retryable)

            session.on("close", _on_close)

    async def aclose(self) -> None:
        """Tear down the agent session and leave the SFU."""
        session = self._session
        self._session = None
        speech_resource_closers = self._owned_speech_resource_closers
        self._owned_speech_resource_closers = ()
        audio_input = self._audio_input
        self._audio_input = None
        connect_task = self._connect_task
        self._connect_task = None
        room = self._room
        self._room = None
        try:
            if connect_task is not None:
                await self._settle_connect_task(connect_task)
            if session is not None:
                await session.aclose()
        finally:
            try:
                if self._session_event_tasks:
                    await asyncio.gather(*self._session_event_tasks, return_exceptions=True)
            finally:
                try:
                    await _close_speech_resources(speech_resource_closers)
                finally:
                    try:
                        if audio_input is not None:
                            await audio_input.aclose()
                    finally:
                        if room is not None:
                            await room.disconnect()

    async def _settle_connect_task(self, connect_task: asyncio.Task[None]) -> None:
        """Wait for or cancel an in-flight native SFU connection deterministically."""
        done, pending = await asyncio.wait({connect_task}, timeout=_SFU_CONNECT_TIMEOUT_S)
        if pending:
            logger.warning("call_sfu_connect_teardown_timeout", identity=self._local_identity)
            connect_task.cancel()
            cancelled, pending = await asyncio.wait(pending, timeout=_SFU_CONNECT_CANCEL_TIMEOUT_S)
            done.update(cancelled)
        if pending:
            logger.error("call_sfu_connect_cancel_timeout", identity=self._local_identity)
            for task in pending:
                task.add_done_callback(_consume_task_result)
        if done:
            await asyncio.gather(*done, return_exceptions=True)


class CascadedVoiceBridge(RealtimeVoiceBridge):
    """Existing MatrixRTC media bridge with a cascaded speech session on top."""

    async def start_agent(self, options: CallVoiceAgentOptions) -> None:
        """Start STT -> normal MindRoom agent -> TTS on the connected room."""
        from livekit import rtc  # noqa: PLC0415
        from livekit.agents import Agent, AgentSession, TurnHandlingOptions, inference, room_io  # noqa: PLC0415
        from livekit.plugins import openai  # noqa: PLC0415
        from openai import AsyncOpenAI  # noqa: PLC0415

        if self._room is None:
            msg = "connect() must succeed before start_agent()"
            raise RuntimeError(msg)
        if not isinstance(options, CascadedVoiceAgentOptions):
            msg = "CascadedVoiceBridge requires cascaded agent options"
            raise TypeError(msg)

        stt_client = AsyncOpenAI(
            api_key=options.stt.api_key,
            base_url=options.stt.base_url,
            max_retries=0,
        )
        stt_kwargs = _speech_component_kwargs(options.stt)
        del stt_kwargs["api_key"]
        stt_kwargs.pop("base_url", None)
        try:
            stt = openai.STT(client=stt_client, **stt_kwargs)
            tts = openai.TTS(**_speech_component_kwargs(options.tts))
        except BaseException:
            await stt_client.close()
            raise
        self._owned_speech_resource_closers = (stt_client.close, tts.aclose)
        mindroom_llm = _build_mindroom_llm(options.respond, options.on_tools_executed)
        session = AgentSession(
            stt=stt,
            vad=inference.VAD(model="silero"),
            llm=mindroom_llm,
            tts=tts,
            turn_handling=TurnHandlingOptions(
                turn_detection="vad",
                endpointing={"min_delay": 1.0},
                interruption={"enabled": True, "mode": "vad"},
                preemptive_generation={"enabled": False},
            ),
        )
        agent = Agent(instructions="MindRoom owns this call's model, prompt, history, and tools.")
        await self._start_session(session, agent, options, rtc_module=rtc, room_io_module=room_io)
        if options.greeting_text:
            session.say(options.greeting_text)


def _describe_voice_error(event: ErrorEvent) -> str:
    """Describe a LiveKit speech failure without leaking provider payloads or keys."""
    wrapper = event.error
    underlying = getattr(wrapper, "error", wrapper)
    status_code = getattr(underlying, "status_code", None)
    error_text = _exception_chain_text(underlying)
    error_type = getattr(wrapper, "type", "voice_error")
    component = {
        "realtime_model_error": "OpenAI Realtime voice model",
        "stt_error": "speech-to-text service",
        "tts_error": "text-to-speech service",
        "llm_error": "language model",
    }.get(error_type, "voice runtime")

    if status_code in {401, 403} or any(
        marker in error_text for marker in ("invalid_api_key", "incorrect api key", "unauthorized")
    ):
        return (
            f"Voice call error: the {component} rejected MindRoom's configured credential, so the agent "
            "cannot reliably hear or speak in this call. Update the credential used by the MindRoom runtime, "
            "restart the MindRoom service so it reloads that credential, then leave and rejoin the call. "
            "Retrying with the same credential will not fix the call."
        )
    if any(
        marker in error_text
        for marker in ("certificate_verify_failed", "certificate verify failed", "sslcertverificationerror")
    ):
        return (
            f"Voice call error: MindRoom could not establish a trusted TLS connection to the {component}, so "
            "the agent cannot hear or speak. Ensure the MindRoom service has an up-to-date system CA bundle; "
            "for a custom Python runtime, configure SSL_CERT_FILE to point at that bundle. Restart MindRoom "
            "after correcting the service environment, then leave and rejoin the call."
        )
    if status_code == 429 or "rate limit" in error_text or "rate_limit" in error_text:
        return (
            f"Voice call error: the {component} is rate-limiting this MindRoom instance, so the agent may not "
            "produce a response. Check the provider account's quota/billing and request limits, then retry the "
            "call after the limit clears."
        )
    if status_code is not None and status_code >= 500:
        return (
            f"Voice call error: the {component} returned a server-side failure (HTTP {status_code}), so the "
            "agent may not hear or speak. This is usually temporary; retry after the provider recovers. If it "
            "continues, inspect the MindRoom service logs for the corresponding provider error."
        )
    return (
        f"Voice call error: the {component} failed, so the agent may not hear or speak in this call. Leave and "
        "rejoin once. If the problem repeats, inspect the MindRoom service logs for the underlying provider "
        "error and verify that the configured model, endpoint, credential, quota, and network access are valid."
    )


def _exception_chain_text(error: object) -> str:
    """Collect exception causes for classification without exposing them to users."""
    parts: list[str] = []
    current: object | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(parts) < 8:
        seen.add(id(current))
        parts.append(f"{type(current).__name__}: {current}".lower())
        current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
    return " | ".join(parts)


def _speech_component_kwargs(options: SpeechServiceOptions) -> dict[str, Any]:
    """Build one LiveKit OpenAI-plugin component constructor payload."""
    kwargs = dict(options.extra_kwargs or {})
    kwargs.update(model=options.model, api_key=options.api_key)
    if options.base_url is not None:
        kwargs["base_url"] = options.base_url
    return kwargs


def _build_mindroom_llm(
    respond: Callable[[str, Callable[[list[str]], None] | None], Awaitable[CallAgentResponse]],
    on_tools_executed: Callable[[list[str]], None] | None,
) -> LLM:
    """Adapt finalized LiveKit transcripts to the normal MindRoom agent path."""
    from livekit.agents import NOT_GIVEN, llm  # noqa: PLC0415
    from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS  # noqa: PLC0415

    class _MindRoomLLMStream(llm.LLMStream):
        async def _run(self) -> None:
            transcript = _latest_user_transcript(self._chat_ctx)
            if not transcript:
                logger.warning("cascaded_voice_turn_skipped_no_transcript")
                return
            result = await respond(transcript, on_tools_executed)
            if result.text:
                self._event_ch.send_nowait(
                    llm.ChatChunk(
                        id=f"mindroom-{id(self)}",
                        delta=llm.ChoiceDelta(
                            role="assistant",
                            content=result.text,
                            extra=(
                                {_CALL_TURN_CORRELATION_KEY: result.turn_id} if result.turn_id is not None else None
                            ),
                        ),
                    ),
                )

    class _MindRoomLLM(llm.LLM):
        @property
        def model(self) -> str:
            return "configured-agent-model"

        @property
        def provider(self) -> str:
            return "mindroom"

        def chat(
            self,
            *,
            chat_ctx: ChatContext,
            tools: list[Tool] | None = None,
            conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
            parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
            tool_choice: NotGivenOr[ToolChoice] = NOT_GIVEN,
            extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
        ) -> LLMStream:
            del parallel_tool_calls, tool_choice, extra_kwargs
            return _MindRoomLLMStream(
                self,
                chat_ctx=chat_ctx,
                tools=tools or [],
                conn_options=replace(conn_options, max_retry=0),
            )

    return _MindRoomLLM()


async def _attach_private_transcript_synchronizer(
    session: AgentSession,
) -> Callable[[], Awaitable[None]]:
    """Sync assistant playout text without publishing transcripts to the SFU."""
    from livekit.agents.voice.transcription import TranscriptSynchronizer  # noqa: PLC0415

    audio_output = session.output.audio
    if audio_output is None:
        msg = "Cascaded voice session has no audio output to synchronize"
        raise RuntimeError(msg)
    synchronizer = TranscriptSynchronizer(
        next_in_chain_audio=audio_output,
        next_in_chain_text=None,
    )
    try:
        session.output.audio = synchronizer.audio_output
        session.output.transcription = synchronizer.text_output
    except BaseException:
        await synchronizer.aclose()
        raise
    return synchronizer.aclose


def _latest_user_transcript(chat_ctx: ChatContext) -> str:
    """Return only the finalized user turn that triggered this generation."""
    from livekit.agents.llm import ChatMessage  # noqa: PLC0415

    for item in reversed(chat_ctx.items):
        if not isinstance(item, ChatMessage):
            continue
        if item.role == "user":
            return item.text_content or ""
        if item.role == "assistant":
            return ""
    return ""


async def _close_speech_resources(closers: tuple[Callable[[], Awaitable[None]], ...]) -> None:
    """Close caller-owned LiveKit speech providers without blocking room teardown."""
    if not closers:
        return
    results = await asyncio.gather(*(close() for close in closers), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            logger.warning("call_speech_model_close_failed", error=str(result))


def _consume_task_result(task: asyncio.Task[None]) -> None:
    """Retrieve a late connect result after bounded teardown stopped waiting."""
    if not task.cancelled():
        task.exception()
