"""Tests for the failure-safe LiveKit voice bridge teardown."""

from __future__ import annotations

import asyncio
from types import ModuleType, SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.matrix_rtc.focus import SfuGrant
from mindroom.matrix_rtc.voice_agent import (
    RealtimeVoiceBridge,
    VoiceAgentOptions,
    _AuthorizedParticipantAudioInput,
)


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
async def test_agent_session_uses_group_safe_room_options(monkeypatch: pytest.MonkeyPatch) -> None:
    """A linked participant leaving cannot close the group session or inject text."""

    class FakeSession:
        def __init__(self) -> None:
            self.input = SimpleNamespace(audio=None)
            self.room_options = None

        async def start(self, _agent: object, *, room: object, room_options: object) -> None:
            assert room is bridge._room
            self.room_options = room_options

        def generate_reply(self, **_kwargs: object) -> None:
            return

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
