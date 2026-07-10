"""Tests for the failure-safe LiveKit voice bridge teardown."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from livekit import rtc
from livekit.agents import APIConnectionError, APIStatusError, CloseReason, llm
from livekit.agents.voice import io as agents_io
from structlog.testing import capture_logs

from mindroom.matrix_rtc.call_tools import CallAgentResponse
from mindroom.matrix_rtc.focus import SfuGrant
from mindroom.matrix_rtc.voice_agent import (
    CascadedVoiceAgentOptions,
    CascadedVoiceBridge,
    RealtimeVoiceBridge,
    SpeechServiceOptions,
    VoiceAgentOptions,
    _AudioFrameStream,
    _AuthorizedParticipantAudioInput,
    _build_mindroom_llm,
)

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.asyncio
async def test_bridge_connect_disables_automatic_sfu_subscriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SDK cannot select or subscribe an unverified participant by default."""

    class FakeRoom:
        def __init__(self) -> None:
            self.local_participant = MagicMock()
            self.options = None

        async def connect(self, _url: str, _jwt: str, options: object) -> None:
            self.options = options

    room = FakeRoom()
    monkeypatch.setattr("livekit.rtc.Room", lambda: room)
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)

    await bridge.connect(SfuGrant(url="wss://sfu.example.org", jwt="jwt"))

    assert room.options is not None
    assert room.options.auto_subscribe is False
    assert room.options.connect_timeout is not None


@pytest.mark.asyncio
async def test_bridge_e2ee_uses_matrix_compatible_hkdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Raw MatrixRTC keys derive the same AES key as Element Call's Web Crypto path."""

    class FakeRoom:
        def __init__(self) -> None:
            self.local_participant = MagicMock()
            self.options = None

        async def connect(self, _url: str, _jwt: str, options: object) -> None:
            self.options = options

    room = FakeRoom()
    monkeypatch.setattr("livekit.rtc.Room", lambda: room)
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=True)

    await bridge.connect(SfuGrant(url="wss://sfu.example.org", jwt="jwt"))

    assert room.options is not None
    provider = room.options.e2ee.key_provider_options
    assert provider.key_derivation_function == rtc.KeyDerivationFunction.HKDF
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=provider.ratchet_salt,
        info=bytes(128),
    ).derive(bytes(range(16)))
    # Captured from the Web Crypto HKDF derivation used by LiveKit JS.
    assert derived.hex() == "4086b4641064e1ae8b63d4eb83ad3e7e"


@pytest.mark.asyncio
async def test_aclose_settles_cancelled_connect_before_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling the join cannot orphan a native connection still completing."""

    class FakeRoom:
        def __init__(self) -> None:
            self.local_participant = MagicMock()
            self.connect_started = asyncio.Event()
            self.release_connect = asyncio.Event()
            self.disconnected = False

        async def connect(self, _url: str, _jwt: str, _options: object) -> None:
            self.connect_started.set()
            await self.release_connect.wait()

        async def disconnect(self) -> None:
            self.disconnected = True

    room = FakeRoom()
    monkeypatch.setattr("livekit.rtc.Room", lambda: room)
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    connect_waiter = asyncio.create_task(bridge.connect(SfuGrant(url="wss://sfu.example.org", jwt="jwt")))
    await room.connect_started.wait()

    connect_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connect_waiter
    close_waiter = asyncio.create_task(bridge.aclose())
    await asyncio.sleep(0)

    assert not close_waiter.done()
    room.release_connect.set()
    await close_waiter
    assert room.disconnected


@pytest.mark.asyncio
async def test_aclose_cancels_stalled_connect_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled native connect cannot block call teardown forever."""

    class FakeRoom:
        def __init__(self) -> None:
            self.local_participant = MagicMock()
            self.connect_started = asyncio.Event()
            self.connect_cancelled = False
            self.disconnected = False

        async def connect(self, _url: str, _jwt: str, _options: object) -> None:
            self.connect_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.connect_cancelled = True
                raise

        async def disconnect(self) -> None:
            self.disconnected = True

    monkeypatch.setattr("mindroom.matrix_rtc.voice_agent._SFU_CONNECT_TIMEOUT_S", 0.01)
    room = FakeRoom()
    monkeypatch.setattr("livekit.rtc.Room", lambda: room)
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    connect_waiter = asyncio.create_task(bridge.connect(SfuGrant(url="wss://sfu.example.org", jwt="jwt")))
    await room.connect_started.wait()

    connect_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connect_waiter
    await asyncio.wait_for(bridge.aclose(), timeout=0.5)

    assert room.connect_cancelled
    assert room.disconnected


@pytest.mark.asyncio
async def test_agent_session_uses_group_safe_room_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """A linked participant leaving cannot close the group session or inject text."""

    class FakeSession:
        def __init__(self) -> None:
            self.input = SimpleNamespace(audio=None)
            self.room_options = None
            self.handlers: dict[str, object] = {}

        async def start(self, _agent: object, *, room: object, room_options: object) -> None:
            assert room is bridge._room
            self.room_options = room_options

        def generate_reply(self, **_kwargs: object) -> None:
            return

        def on(self, event: str, callback: object) -> None:
            self.handlers[event] = callback

    fake_session = FakeSession()
    fake_audio_input = MagicMock()
    monkeypatch.setattr("livekit.agents.AgentSession", lambda **_kwargs: fake_session)
    monkeypatch.setattr("livekit.agents.Agent", lambda **_kwargs: object())
    monkeypatch.setattr("livekit.plugins.openai.realtime.RealtimeModel", lambda **_kwargs: object())
    monkeypatch.setattr(
        "mindroom.matrix_rtc.voice_agent._AuthorizedParticipantAudioInput",
        lambda *_args: fake_audio_input,
    )
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    bridge._room = MagicMock()

    await bridge.start_agent(VoiceAgentOptions(instructions="Be concise.", model="gpt-realtime-2.1", api_key="sk"))

    assert fake_session.input.audio is fake_audio_input
    assert fake_session.room_options.audio_input is False
    assert fake_session.room_options.text_input is False
    assert fake_session.room_options.text_output is False
    assert fake_session.room_options.close_on_disconnect is False


@pytest.mark.asyncio
async def test_cascaded_session_wires_stt_normal_agent_and_tts(  # noqa: C901, PLR0915
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cascaded bridge gives LiveKit speech legs and the MindRoom LLM adapter."""
    built: dict[str, object] = {}
    turns: list[tuple[str, str]] = []
    tool_uses: list[list[str]] = []
    finalized: list[tuple[str | None, str, bool]] = []
    responder_inputs: list[str] = []

    class FakeSpeechModel:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeSession:
        def __init__(self, **kwargs: object) -> None:
            built["session_kwargs"] = kwargs
            self.input = SimpleNamespace(audio=None)
            self.handlers: dict[str, object] = {}
            self.greetings: list[str] = []

        async def start(self, agent: object, *, room: object, room_options: object) -> None:
            built["agent"] = agent
            built["room"] = room
            built["room_options"] = room_options

        def on(self, event: str, callback: object) -> None:
            self.handlers[event] = callback

        def say(self, text: str) -> None:
            self.greetings.append(text)

        async def aclose(self) -> None:
            built["session_closed"] = True

    async def respond(transcript: str) -> CallAgentResponse:
        responder_inputs.append(transcript)
        return CallAgentResponse(text=f"heard {transcript}", tool_names=("weather",), turn_id="turn-1")

    async def finalize_spoken_response(token: str | None, text: str, interrupted: bool) -> None:
        finalized.append((token, text, interrupted))

    fake_session = FakeSession()
    stt = FakeSpeechModel()
    tts = FakeSpeechModel()
    monkeypatch.setattr(
        "livekit.agents.AgentSession",
        lambda **kwargs: (built.update(session_kwargs=kwargs), fake_session)[1],
    )
    monkeypatch.setattr("livekit.agents.Agent", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("livekit.agents.inference.VAD", lambda **kwargs: ("vad", kwargs))
    monkeypatch.setattr(
        "livekit.plugins.openai.STT",
        lambda **kwargs: (setattr(stt, "kwargs", kwargs), stt)[1],
    )
    monkeypatch.setattr(
        "livekit.plugins.openai.TTS",
        lambda **kwargs: (setattr(tts, "kwargs", kwargs), tts)[1],
    )
    fake_audio_input = MagicMock()
    fake_audio_input.aclose = AsyncMock()
    monkeypatch.setattr(
        "mindroom.matrix_rtc.voice_agent._AuthorizedParticipantAudioInput",
        lambda *_args: fake_audio_input,
    )
    bridge = CascadedVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    bridge._room = MagicMock()
    bridge._room.disconnect = AsyncMock()
    options = CascadedVoiceAgentOptions(
        stt=SpeechServiceOptions(
            provider="openai_compatible",
            model="whisper-large-v3",
            api_key="local-stt",
            base_url="http://127.0.0.1:9000/v1",
            extra_kwargs={"language": "en"},
        ),
        tts=SpeechServiceOptions(
            provider="openai",
            model="tts-1",
            api_key="tts-key",
            extra_kwargs={"voice": "ash"},
        ),
        respond=respond,
        finalize_spoken_response=finalize_spoken_response,
        greeting_text="Hello.",
        on_conversation_turn=lambda role, text: turns.append((role, text)),
        on_tools_executed=tool_uses.append,
    )

    await bridge.start_agent(options)

    session_kwargs = cast("dict[str, object]", built["session_kwargs"])
    assert session_kwargs["stt"] is stt
    assert stt.kwargs == {
        "language": "en",
        "model": "whisper-large-v3",
        "api_key": "local-stt",
        "base_url": "http://127.0.0.1:9000/v1",
    }
    assert session_kwargs["tts"] is tts
    assert tts.kwargs == {"voice": "ash", "model": "tts-1", "api_key": "tts-key"}
    assert session_kwargs["vad"] == ("vad", {"model": "silero"})
    assert session_kwargs["turn_handling"] == {
        "turn_detection": "vad",
        "interruption": {"enabled": True, "mode": "vad"},
        "preemptive_generation": {"enabled": False},
    }
    assert fake_session.input.audio is fake_audio_input
    assert fake_session.greetings == ["Hello."]

    user_message = llm.ChatContext.empty().add_message(role="user", content="hello")
    callback = cast("Callable[[object], None]", fake_session.handlers["conversation_item_added"])
    callback(SimpleNamespace(item=user_message))
    assert turns == [("user", "hello")]

    chat_context = llm.ChatContext.empty()
    chat_context.add_message(role="user", content="status part one")
    chat_context.add_message(role="user", content="status part two")
    stream = session_kwargs["llm"].chat(chat_ctx=chat_context)  # type: ignore[union-attr]
    assert stream._conn_options.max_retry == 0
    chunks = [chunk async for chunk in stream]
    assert responder_inputs == ["status part one\nstatus part two"]
    assert [chunk.delta.content for chunk in chunks] == ["heard status part one\nstatus part two"]
    assert chunks[0].delta.extra == {"mindroom_call_turn_id": "turn-1"}
    assert tool_uses == [["weather"]]

    assistant_message = llm.ChatContext.empty().add_message(
        role="assistant",
        content="heard sta",
        interrupted=True,
        extra={"mindroom_call_turn_id": "turn-1"},
    )
    callback(SimpleNamespace(item=assistant_message))
    await asyncio.sleep(0)
    assert finalized == [("turn-1", "heard sta", True)]

    class UnforwardedSpeech:
        def __init__(self) -> None:
            self.chat_items: list[object] = []

        def add_done_callback(self, done: Callable[[object], None]) -> None:
            done(self)

    speech_callback = cast("Callable[[object], None]", fake_session.handlers["speech_created"])
    speech_callback(SimpleNamespace(source="generate_reply", speech_handle=UnforwardedSpeech()))
    await asyncio.sleep(0)
    assert finalized[-1] == (None, "", True)

    await bridge.aclose()
    assert stt.closed
    assert tts.closed
    assert built["session_closed"] is True
    fake_audio_input.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cascaded_start_closes_stt_when_tts_construction_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial speech-provider construction cannot leak the first client."""

    class FakeSTT:
        closed = False

        async def aclose(self) -> None:
            self.closed = True

    stt = FakeSTT()
    monkeypatch.setattr("livekit.plugins.openai.STT", lambda **_kwargs: stt)

    def fail_tts(**_kwargs: object) -> None:
        msg = "tts construction failed"
        raise RuntimeError(msg)

    monkeypatch.setattr("livekit.plugins.openai.TTS", fail_tts)
    bridge = CascadedVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    bridge._room = MagicMock()
    options = CascadedVoiceAgentOptions(
        stt=SpeechServiceOptions(provider="openai", model="gpt-4o-transcribe", api_key="stt-key"),
        tts=SpeechServiceOptions(provider="openai", model="tts-1", api_key="tts-key"),
        respond=AsyncMock(),
    )

    with pytest.raises(RuntimeError, match="tts construction failed"):
        await bridge.start_agent(options)

    assert stt.closed


@pytest.mark.asyncio
async def test_cascaded_agent_failure_propagates_to_session_lifecycle() -> None:
    """Retryable SDK errors cannot duplicate a normal-agent turn or its tools."""
    attempts = 0

    async def fail(_transcript: str) -> CallAgentResponse:
        nonlocal attempts
        attempts += 1
        message = "agent unavailable"
        raise APIConnectionError(message, retryable=True)

    context = llm.ChatContext.empty()
    context.add_message(role="user", content="hello")
    stream = _build_mindroom_llm(fail, None).chat(chat_ctx=context)

    with pytest.raises(APIConnectionError, match="agent unavailable"):
        _ = [chunk async for chunk in stream]

    assert attempts == 1


def test_voice_agent_import_keeps_livekit_and_openai_lazy() -> None:
    """Importing the optional bridge does not import either provider SDK."""
    probe = (
        "import json, sys; import mindroom.matrix_rtc.voice_agent; "
        "print(json.dumps(sorted(name for name in sys.modules "
        "if name == 'livekit' or name.startswith('livekit.') or name == 'openai' or name.startswith('openai.'))))"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == []


@pytest.mark.parametrize(
    ("api_error", "retryable"),
    [
        (APIConnectionError("offline", retryable=True), True),
        (APIStatusError("bad key", status_code=401), False),
    ],
)
def test_terminal_session_close_reports_api_retryability(api_error: object, retryable: bool) -> None:
    """Terminal SDK errors preserve public API retryability for the manager."""
    callbacks: list[bool] = []
    handlers: dict[str, object] = {}
    session = SimpleNamespace(on=lambda event, callback: handlers.__setitem__(event, callback))
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    bridge._session = session
    bridge._register_termination_listener(
        session,  # type: ignore[arg-type]
        VoiceAgentOptions(
            instructions="Be concise.",
            model="gpt-realtime-2.1",
            api_key="sk",
            on_session_terminated=callbacks.append,
        ),
    )

    handler = cast("Callable[[object], None]", handlers["close"])
    handler(
        SimpleNamespace(
            reason=CloseReason.ERROR,
            error=SimpleNamespace(error=api_error),
        ),
    )

    assert callbacks == [retryable]


@pytest.mark.asyncio
async def test_explicit_bridge_close_does_not_report_termination() -> None:
    """Manager-requested shutdown cannot feed back as an unexpected close."""
    callbacks: list[bool] = []
    handlers: dict[str, object] = {}

    class FakeSession:
        def on(self, event: str, callback: object) -> None:
            handlers[event] = callback

        async def aclose(self) -> None:
            handler = cast("Callable[[object], None]", handlers["close"])
            handler(SimpleNamespace(reason=CloseReason.USER_INITIATED, error=None))

    session = FakeSession()
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    bridge._session = session
    room = MagicMock()
    room.disconnect = AsyncMock()
    bridge._room = room
    bridge._register_termination_listener(
        session,  # type: ignore[arg-type]
        VoiceAgentOptions(
            instructions="Be concise.",
            model="gpt-realtime-2.1",
            api_key="sk",
            on_session_terminated=callbacks.append,
        ),
    )

    await bridge.aclose()

    assert callbacks == []
    room.disconnect.assert_awaited_once()


def test_bridge_limits_output_subscriptions_to_matrix_roster() -> None:
    """Unrostered SFU participants cannot subscribe to the agent's audio."""
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    room = MagicMock()
    bridge._room = room

    bridge.set_participant_identities(
        frozenset({"@bob:example.org:BOBDEV", "@alice:example.org:ALICEDEV"}),
    )

    kwargs = room.local_participant.set_track_subscription_permissions.call_args.kwargs
    assert kwargs["allow_all_participants"] is False
    assert [permission.participant_identity for permission in kwargs["participant_permissions"]] == [
        "@alice:example.org:ALICEDEV",
        "@bob:example.org:BOBDEV",
    ]


def test_bridge_logs_sorted_output_permission_roster() -> None:
    """Output permission diagnostics preserve the authoritative sorted roster."""
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    room = MagicMock()
    bridge._room = room

    with capture_logs() as logs:
        bridge.set_participant_identities(
            frozenset({"@bob:example.org:BOBDEV", "@alice:example.org:ALICEDEV"}),
        )

    assert logs == [
        {
            "event": "call_output_permissions_applied",
            "log_level": "info",
            "participants": ["@alice:example.org:ALICEDEV", "@bob:example.org:BOBDEV"],
        },
    ]


@pytest.mark.asyncio
async def test_audio_frame_stream_logs_first_frame_once_with_participant_identity() -> None:
    """Only the first decoded frame emits the participant-scoped diagnostic."""

    class FakeAudioStream:
        def __init__(self) -> None:
            self.frames = iter(("first", "second"))

        async def __anext__(self) -> object:
            return SimpleNamespace(frame=next(self.frames))

    stream = _AudioFrameStream(
        cast("rtc.AudioStream", FakeAudioStream()),
        "@alice:example.org:ALICEDEV",
    )

    with capture_logs() as logs:
        assert await stream.__anext__() == "first"
        assert await stream.__anext__() == "second"

    assert logs == [
        {
            "event": "call_audio_first_frame",
            "log_level": "info",
            "participant": "@alice:example.org:ALICEDEV",
        },
    ]


@pytest.mark.asyncio
async def test_authorized_audio_input_logs_added_stream_identity_and_sid() -> None:
    """Stream-add diagnostics identify both Matrix participant and LiveKit publication."""

    class FakeAudioStream:
        async def aclose(self) -> None:
            return

    class FakeMixer:
        def add_stream(self, _stream: object) -> None:
            return

        def remove_stream(self, _stream: object) -> None:
            return

        async def aclose(self) -> None:
            return

    fake_rtc = cast(
        "ModuleType",
        SimpleNamespace(
            AudioMixer=lambda *_args, **_kwargs: FakeMixer(),
            AudioStream=lambda *_args, **_kwargs: FakeAudioStream(),
            TrackKind=SimpleNamespace(KIND_AUDIO=1),
            TrackSource=SimpleNamespace(SOURCE_MICROPHONE=2),
        ),
    )
    room = MagicMock()
    room.remote_participants = {}
    identity = "@alice:example.org:ALICEDEV"
    audio_input = _AuthorizedParticipantAudioInput(room, fake_rtc, frozenset({identity}))

    with capture_logs() as logs:
        audio_input._add_stream("alice-mic", identity, cast("rtc.RemoteTrack", object()))

    assert logs == [
        {
            "event": "call_audio_stream_added",
            "log_level": "info",
            "participant": identity,
            "publication_sid": "alice-mic",
        },
    ]
    await audio_input.aclose()


@pytest.mark.asyncio
async def test_authorized_audio_input_mixes_all_and_only_rostered_microphones() -> None:
    """Group audio subscribes every rostered microphone and rejects an SFU-only identity."""

    class FakePublication:
        kind = 1
        source = 2

        def __init__(self, sid: str, *, subscribed: bool = False) -> None:
            self.sid = sid
            self.subscribed = subscribed
            self.track = object()
            self.subscription_changes: list[bool] = []

        def set_subscribed(self, subscribed: bool) -> None:
            self.subscribed = subscribed
            self.subscription_changes.append(subscribed)

    class FakeAudioStream:
        async def aclose(self) -> None:
            return

    class FakeMixer:
        def __init__(self) -> None:
            self.streams: set[object] = set()

        def add_stream(self, stream: object) -> None:
            self.streams.add(stream)

        def remove_stream(self, stream: object) -> None:
            self.streams.discard(stream)

        async def aclose(self) -> None:
            return

    mixer = FakeMixer()
    rtc = cast(
        "ModuleType",
        SimpleNamespace(
            AudioMixer=lambda *_args, **_kwargs: mixer,
            AudioStream=lambda *_args, **_kwargs: FakeAudioStream(),
            TrackKind=SimpleNamespace(KIND_AUDIO=1),
            TrackSource=SimpleNamespace(SOURCE_MICROPHONE=2),
        ),
    )
    alice_publication = FakePublication("alice-mic")
    bob_publication = FakePublication("bob-mic")
    rogue_publication = FakePublication("rogue-mic", subscribed=True)
    participants = {
        "alice": SimpleNamespace(
            identity="@alice:example.org:ALICEDEV",
            track_publications={alice_publication.sid: alice_publication},
        ),
        "bob": SimpleNamespace(
            identity="@bob:example.org:BOBDEV",
            track_publications={bob_publication.sid: bob_publication},
        ),
        "rogue": SimpleNamespace(
            identity="@rogue:example.org:ROGUEDEV",
            track_publications={rogue_publication.sid: rogue_publication},
        ),
    }
    room = MagicMock()
    room.remote_participants = participants
    audio_input = _AuthorizedParticipantAudioInput(
        room,
        rtc,
        frozenset({"@alice:example.org:ALICEDEV", "@bob:example.org:BOBDEV"}),
    )

    assert audio_input.label == "authorized MatrixRTC participants"
    assert audio_input.source is None
    assert alice_publication.subscription_changes == [True]
    assert bob_publication.subscription_changes == [True]
    assert rogue_publication.subscription_changes == [False]
    assert set(audio_input._streams) == {"alice-mic", "bob-mic"}

    audio_input.set_participant_identities(frozenset({"@bob:example.org:BOBDEV"}))
    assert alice_publication.subscription_changes[-1] is False
    assert set(audio_input._streams) == {"bob-mic"}
    await audio_input.aclose()


@pytest.mark.asyncio
async def test_authorized_audio_input_satisfies_agent_session_audio_interface() -> None:
    """The mixer input must expose every public AudioInput attribute AgentSession reads.

    AgentSession.start walks ``input.audio.source`` recursively to log the IO
    chain; a missing attribute aborts the realtime agent start and the bot
    leaves the call immediately after joining.
    """

    class FakeMixer:
        def add_stream(self, _stream: object) -> None:
            return

        async def aclose(self) -> None:
            return

    fake_rtc = cast(
        "ModuleType",
        SimpleNamespace(
            AudioMixer=lambda *_args, **_kwargs: FakeMixer(),
            TrackKind=SimpleNamespace(KIND_AUDIO=1),
            TrackSource=SimpleNamespace(SOURCE_MICROPHONE=2),
        ),
    )
    room = MagicMock()
    room.remote_participants = {}
    audio_input = _AuthorizedParticipantAudioInput(room, fake_rtc, frozenset())

    assert audio_input.source is None
    assert audio_input.label
    audio_input.on_attached()
    audio_input.on_detached()
    required = {name for name in dir(agents_io.AudioInput) if not name.startswith("_")}
    implemented = {name for name in dir(type(audio_input)) if not name.startswith("_")}
    assert required <= implemented
    await audio_input.aclose()


@pytest.mark.asyncio
async def test_start_agent_logs_detailed_media_snapshot_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent startup emits one INFO snapshot with local, remote, and roster fields."""

    class FakeSession:
        def __init__(self) -> None:
            self.input = SimpleNamespace(audio=None)

        async def start(self, _agent: object, *, room: object, room_options: object) -> None:
            assert room is bridge._room
            assert room_options is not None

    fake_session = FakeSession()
    monkeypatch.setattr("livekit.agents.AgentSession", lambda **_kwargs: fake_session)
    monkeypatch.setattr("livekit.agents.Agent", lambda **_kwargs: object())
    monkeypatch.setattr("livekit.plugins.openai.realtime.RealtimeModel", lambda **_kwargs: object())
    monkeypatch.setattr(
        "mindroom.matrix_rtc.voice_agent._AuthorizedParticipantAudioInput",
        lambda *_args: MagicMock(),
    )
    alice_identity = "@alice:example.org:ALICEDEV"
    bob_identity = "@bob:example.org:BOBDEV"
    room = SimpleNamespace(
        local_participant=SimpleNamespace(
            track_publications={"local": SimpleNamespace(sid="bot-audio")},
        ),
        remote_participants={
            "alice": SimpleNamespace(
                identity=alice_identity,
                track_publications={
                    "mic": SimpleNamespace(sid="alice-mic", subscribed=True, muted=False),
                },
            ),
        },
    )
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    bridge._room = room
    bridge._participant_identities = frozenset({bob_identity, alice_identity})

    with capture_logs() as logs:
        await bridge.start_agent(
            VoiceAgentOptions(instructions="Be concise.", model="gpt-realtime-2.1", api_key="sk"),
        )

    assert logs == [
        {
            "event": "call_media_snapshot",
            "local_published_tracks": ["bot-audio"],
            "log_level": "info",
            "remote_participants": {
                alice_identity: [{"sid": "alice-mic", "subscribed": True, "muted": False}],
            },
            "roster": [alice_identity, bob_identity],
        },
    ]


@pytest.mark.asyncio
async def test_aclose_disconnects_room_when_session_close_fails() -> None:
    """A failing realtime session close must not leave the SFU connection open."""
    bridge = RealtimeVoiceBridge(local_identity="@bot:example.org:BOTDEV", e2ee_enabled=False)
    session = MagicMock()
    session.aclose = AsyncMock(side_effect=RuntimeError("session close failed"))
    room = MagicMock()
    room.disconnect = AsyncMock()
    bridge._session = session
    bridge._room = room

    with pytest.raises(RuntimeError, match="session close failed"):
        await bridge.aclose()

    room.disconnect.assert_awaited_once()
