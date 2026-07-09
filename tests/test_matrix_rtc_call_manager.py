"""Call lifecycle tests for CallManager and CallSession with a fake media plane."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.calls import CallsConfig
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.matrix_rtc.call_manager import CallManager, _build_call_instructions, maybe_build_call_manager
from mindroom.matrix_rtc.call_session import CallSession, CallSessionDeps
from mindroom.matrix_rtc.events import (
    CALL_MEMBER_EVENT_TYPE,
    build_membership_content,
    membership_state_key,
)
from mindroom.matrix_rtc.focus import SfuGrant
from mindroom.matrix_rtc.voice_agent import VoiceAgentOptions

if TYPE_CHECKING:
    from mindroom.matrix_rtc.events import CallMember

BOT_USER = "@helper:example.org"
BOT_DEVICE = "BOTDEV"
ROOM_ID = "!room:example.org"
SERVICE_URL = "https://rtc.example.org"
GRANT = SfuGrant(url="wss://sfu.example.org", jwt="jwt-token")


class FakeBridge:
    """Records media-plane calls instead of touching LiveKit."""

    def __init__(self) -> None:
        self.connected_grant: SfuGrant | None = None
        self.frame_keys: list[tuple[str, bytes, int]] = []
        self.agent_options: VoiceAgentOptions | None = None
        self.closed = False

    async def connect(self, grant: SfuGrant) -> None:
        """Record the grant."""
        self.connected_grant = grant

    def set_frame_key(self, participant_identity: str, key: bytes, key_index: int) -> None:
        """Record the key."""
        self.frame_keys.append((participant_identity, key, key_index))

    async def start_agent(self, options: VoiceAgentOptions) -> None:
        """Record the agent options."""
        self.agent_options = options

    async def aclose(self) -> None:
        """Record the close."""
        self.closed = True


class FakeKeyTransport:
    """Records key sends instead of encrypting to-device messages."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_key(self, *, room_id: str, key_base64: str, key_index: int, targets: list[CallMember]) -> None:
        """Record one key distribution."""
        self.sent.append(
            {"room_id": room_id, "key_base64": key_base64, "key_index": key_index, "targets": targets},
        )


def _client() -> AsyncMock:
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = BOT_USER
    client.device_id = BOT_DEVICE
    client.get_openid_token.return_value = nio.responses.GetOpenIDTokenResponse(
        "opaque-token",
        3600,
        "example.org",
        "Bearer",
    )
    return client


def _remote_member_event(user: str = "@alice:example.org", device: str = "ALICEDEV") -> dict:
    return {
        "type": CALL_MEMBER_EVENT_TYPE,
        "state_key": membership_state_key(user, device),
        "sender": user,
        # Manager expiry checks run against the wall clock, so the event must be fresh.
        "origin_server_ts": int(time.time() * 1000),
        "content": build_membership_content(
            user_id=user,
            device_id=device,
            livekit_service_url=SERVICE_URL,
            expires_ms=10_000_000,
        ),
    }


def _config(*, enabled: bool = True) -> Config:
    return Config(
        agents={"helper": AgentConfig(display_name="Helper", role="Answer questions", instructions=["Be kind."])},
        models={},
        calls=CallsConfig(
            enabled=enabled,
            agents=["helper"],
            livekit_service_url=SERVICE_URL,
        ),
    )


def _manager(client: AsyncMock, bridge: FakeBridge, config: Config | None = None) -> CallManager:
    return CallManager(
        agent_name="helper",
        config=config or _config(),
        client=client,
        runtime_paths=MagicMock(spec=RuntimePaths),
        homeserver_url="https://matrix.example.org",
        ssl_verify=True,
        bridge_factory=lambda _identity, _e2ee: bridge,
    )


def _room(*, encrypted: bool = False) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id=ROOM_ID, own_user_id=BOT_USER)
    room.encrypted = encrypted
    return room


def _member_unknown_event() -> nio.UnknownEvent:
    return nio.UnknownEvent(
        {"event_id": "$e1", "sender": "@alice:example.org", "origin_server_ts": 1_000},
        CALL_MEMBER_EVENT_TYPE,
    )


@pytest.fixture(autouse=True)
def _stub_join_externals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_manager.get_secret_from_env",
        lambda _name, _paths: "sk-test",
    )

    async def fake_grant(*_args: object, **_kwargs: object) -> SfuGrant:
        return GRANT

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.request_sfu_grant", fake_grant)


@pytest.mark.asyncio
async def test_manager_joins_call_when_remote_member_appears() -> None:
    """Manager joins call when remote member appears."""
    client = _client()
    client.room_get_state.return_value = nio.RoomGetStateResponse([_remote_member_event()], ROOM_ID)
    bridge = FakeBridge()
    manager = _manager(client, bridge)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant == GRANT
    assert bridge.agent_options is not None
    assert bridge.agent_options.model == "gpt-realtime-2.1"
    assert "Helper" in bridge.agent_options.instructions
    put_state_calls = client.room_put_state.await_args_list
    assert put_state_calls, "expected the bot to publish its call membership"
    args, kwargs = put_state_calls[0]
    assert args[0] == ROOM_ID
    assert args[1] == CALL_MEMBER_EVENT_TYPE
    assert args[2]["device_id"] == BOT_DEVICE
    assert kwargs["state_key"] == membership_state_key(BOT_USER, BOT_DEVICE)


@pytest.mark.asyncio
async def test_manager_ignores_unrelated_event_types() -> None:
    """Manager ignores unrelated event types."""
    client = _client()
    bridge = FakeBridge()
    manager = _manager(client, bridge)
    event = nio.UnknownEvent(
        {"event_id": "$e2", "sender": "@alice:example.org", "origin_server_ts": 1_000},
        "io.mindroom.tool_approval_response",
    )

    await manager.on_room_event(_room(), event)

    client.room_get_state.assert_not_awaited()
    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_manager_leaves_call_when_room_call_empties() -> None:
    """Manager leaves call when room call empties."""
    client = _client()
    client.room_get_state.return_value = nio.RoomGetStateResponse([_remote_member_event()], ROOM_ID)
    bridge = FakeBridge()
    manager = _manager(client, bridge)
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.connected_grant is not None

    empty_leave_event = {
        "type": CALL_MEMBER_EVENT_TYPE,
        "state_key": membership_state_key("@alice:example.org", "ALICEDEV"),
        "sender": "@alice:example.org",
        "origin_server_ts": 2_000,
        "content": {},
    }
    client.room_get_state.return_value = nio.RoomGetStateResponse([empty_leave_event], ROOM_ID)
    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.closed
    # The bot cleared its own membership state event on leave.
    final_args, final_kwargs = client.room_put_state.await_args_list[-1]
    assert final_args[2] == {}
    assert final_kwargs["state_key"] == membership_state_key(BOT_USER, BOT_DEVICE)


@pytest.mark.asyncio
async def test_manager_skips_join_without_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Manager skips join without openai key."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_secret_from_env", lambda _n, _p: None)
    client = _client()
    client.room_get_state.return_value = nio.RoomGetStateResponse([_remote_member_event()], ROOM_ID)
    bridge = FakeBridge()
    manager = _manager(client, bridge)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_manager_shutdown_stops_sessions() -> None:
    """Manager shutdown stops sessions."""
    client = _client()
    client.room_get_state.return_value = nio.RoomGetStateResponse([_remote_member_event()], ROOM_ID)
    bridge = FakeBridge()
    manager = _manager(client, bridge)
    await manager.on_room_event(_room(), _member_unknown_event())

    await manager.shutdown()

    assert bridge.closed
    # Events after shutdown must not start new sessions.
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.frame_keys == []


def test_maybe_build_call_manager_respects_configuration() -> None:
    """Maybe build call manager respects configuration."""
    client = _client()
    runtime_paths = MagicMock(spec=RuntimePaths)
    disabled = maybe_build_call_manager(
        agent_name="helper",
        config=_config(enabled=False),
        client=client,
        runtime_paths=runtime_paths,
        homeserver_url="https://matrix.example.org",
        ssl_verify=True,
    )
    assert disabled is None
    not_listed = maybe_build_call_manager(
        agent_name="other",
        config=_config(),
        client=client,
        runtime_paths=runtime_paths,
        homeserver_url="https://matrix.example.org",
        ssl_verify=True,
    )
    assert not_listed is None
    enabled = maybe_build_call_manager(
        agent_name="helper",
        config=_config(),
        client=client,
        runtime_paths=runtime_paths,
        homeserver_url="https://matrix.example.org",
        ssl_verify=True,
    )
    assert isinstance(enabled, CallManager)


def test_build_call_instructions_includes_role_and_voice_guidance() -> None:
    """Build call instructions includes role and voice guidance."""
    text = _build_call_instructions("helper", _config())
    assert "Helper" in text
    assert "Answer questions" in text
    assert "Be kind." in text
    assert "speaking out loud" in text


def _member(user: str, device: str, created_ts: int = 0) -> CallMember:
    from mindroom.matrix_rtc.events import CallMember  # noqa: PLC0415

    return CallMember(
        user_id=user,
        device_id=device,
        created_ts=created_ts,
        expires_ms=10_000_000,
        membership_id=f"{user}:{device}",
    )


def _session(client: AsyncMock, bridge: FakeBridge, transport: FakeKeyTransport, clock: list[int]) -> CallSession:
    async def fetch_grant() -> SfuGrant:
        return GRANT

    return CallSession(
        room_id=ROOM_ID,
        e2ee_enabled=True,
        deps=CallSessionDeps(
            client=client,
            bridge=bridge,
            key_transport=transport,
            fetch_grant=fetch_grant,
            agent_options=VoiceAgentOptions(instructions="hi", model="gpt-realtime-2.1", api_key="sk-test"),
            livekit_service_url=SERVICE_URL,
            clock_ms=lambda: clock[0],
        ),
    )


@pytest.mark.asyncio
async def test_session_distributes_and_applies_first_key_on_start() -> None:
    """Session distributes and applies first key on start."""
    client = _client()
    bridge = FakeBridge()
    transport = FakeKeyTransport()
    clock = [1_000]
    session = _session(client, bridge, transport, clock)
    alice = _member("@alice:example.org", "ALICEDEV")

    await session.start([alice])

    assert transport.sent
    assert transport.sent[0]["key_index"] == 0
    assert transport.sent[0]["targets"] == [alice]
    own_identity = f"{BOT_USER}:{BOT_DEVICE}"
    assert bridge.frame_keys
    assert bridge.frame_keys[0][0] == own_identity
    assert bridge.frame_keys[0][2] == 0
    await session.stop()


@pytest.mark.asyncio
async def test_session_installs_inbound_keys_on_bridge() -> None:
    """Session installs inbound keys on bridge."""
    client = _client()
    bridge = FakeBridge()
    transport = FakeKeyTransport()
    clock = [1_000]
    session = _session(client, bridge, transport, clock)
    await session.start([_member("@alice:example.org", "ALICEDEV")])
    bridge.frame_keys.clear()

    from mindroom.matrix_rtc.events import ReceivedFrameKey  # noqa: PLC0415

    session.on_key_received(
        ReceivedFrameKey(
            user_id="@alice:example.org",
            claimed_device_id="ALICEDEV",
            member_id="@alice:example.org:ALICEDEV",
            key_base64="QUFBQUFBQUFBQUFBQUFBQQ==",
            key_index=2,
            sent_ts=1_500,
        ),
    )

    assert bridge.frame_keys == [("@alice:example.org:ALICEDEV", b"A" * 16, 2)]
    await session.stop()
