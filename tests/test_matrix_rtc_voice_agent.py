"""Tests for the failure-safe LiveKit voice bridge teardown."""

from __future__ import annotations

import asyncio
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from livekit import rtc
from livekit.agents import APIConnectionError, APIStatusError, CloseReason

from mindroom.matrix_rtc.focus import SfuGrant
from mindroom.matrix_rtc.voice_agent import (
    RealtimeVoiceBridge,
    VoiceAgentOptions,
    _AuthorizedParticipantAudioInput,
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

    assert alice_publication.subscription_changes == [True]
    assert bob_publication.subscription_changes == [True]
    assert rogue_publication.subscription_changes == [False]
    assert set(audio_input._streams) == {"alice-mic", "bob-mic"}

    audio_input.set_participant_identities(frozenset({"@bob:example.org:BOBDEV"}))
    assert alice_publication.subscription_changes[-1] is False
    assert set(audio_input._streams) == {"bob-mic"}
    await audio_input.aclose()


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
