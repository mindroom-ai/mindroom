"""Call lifecycle tests for CallManager and CallSession with a fake media plane."""

from __future__ import annotations

import asyncio
import base64
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import nio
import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.calls import CallsConfig
from mindroom.config.main import Config
from mindroom.matrix.state import MatrixState
from mindroom.matrix_rtc.call_manager import (
    _MAX_PENDING_KEYS_PER_ROOM,
    CallManager,
    _build_call_instructions,
    maybe_build_call_manager,
)
from mindroom.matrix_rtc.call_session import CallSession, CallSessionDeps
from mindroom.matrix_rtc.call_tools import CallAgentTooling
from mindroom.matrix_rtc.events import (
    CALL_ENCRYPTION_KEYS_EVENT_TYPE,
    CALL_MEMBER_EVENT_TYPE,
    DEFAULT_MEMBERSHIP_EXPIRES_MS,
    ReceivedFrameKey,
    build_key_to_device_content,
    build_membership_content,
    membership_state_key,
)
from mindroom.matrix_rtc.focus import SfuGrant
from mindroom.matrix_rtc.voice_agent import VoiceAgentOptions
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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
        self.participant_rosters: list[frozenset[str]] = []
        self.frame_keys: list[tuple[str, bytes, int]] = []
        self.agent_options: VoiceAgentOptions | None = None
        self.closed = False

    def set_participant_identities(self, participant_identities: frozenset[str]) -> None:
        """Record the authoritative media roster."""
        self.participant_rosters.append(participant_identities)

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

    async def send_key(
        self,
        *,
        room_id: str,
        key_base64: str,
        key_index: int,
        targets: list[CallMember],
    ) -> list[CallMember]:
        """Record one key distribution."""
        self.sent.append(
            {"room_id": room_id, "key_base64": key_base64, "key_index": key_index, "targets": targets},
        )
        return targets


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


def _remote_member_event(
    user: str = "@alice:example.org",
    device: str = "ALICEDEV",
    *,
    created_ts: int | None = None,
    expires_ms: int = 10_000_000,
    livekit_service_url: str = SERVICE_URL,
) -> dict:
    return {
        "type": CALL_MEMBER_EVENT_TYPE,
        "state_key": membership_state_key(user, device),
        "sender": user,
        # Manager expiry checks run against the wall clock, so the event must be fresh.
        "origin_server_ts": int(time.time() * 1000) if created_ts is None else created_ts,
        "content": build_membership_content(
            user_id=user,
            device_id=device,
            livekit_service_url=livekit_service_url,
            expires_ms=expires_ms,
            created_ts=created_ts,
        ),
    }


def _room_member_event(user: str = "@alice:example.org", membership: str = "join") -> dict:
    return {
        "type": "m.room.member",
        "state_key": user,
        "sender": user,
        "origin_server_ts": int(time.time() * 1000),
        "content": {"membership": membership},
    }


def _state_response(*call_events: dict) -> nio.RoomGetStateResponse:
    joined_users = {
        event["sender"] for event in call_events if event.get("type") == CALL_MEMBER_EVENT_TYPE and event.get("content")
    }
    events = [*call_events, *(_room_member_event(user) for user in sorted(joined_users))]
    return nio.RoomGetStateResponse(events, ROOM_ID)


def _config(*, enabled: bool = True) -> Config:
    return Config(
        agents={
            "helper": AgentConfig(
                display_name="Helper",
                role="Answer questions",
                instructions=["Be kind."],
                rooms=[ROOM_ID],
            ),
        },
        models={},
        authorization=AuthorizationConfig(global_users=["@alice:example.org"]),
        calls=CallsConfig(
            enabled=enabled,
            agents=["helper"],
            livekit_service_url=SERVICE_URL,
        ),
    )


def _manager(
    client: AsyncMock,
    bridge: FakeBridge,
    tmp_path: Path,
    config: Config | None = None,
    tool_support: object | None = None,
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
) -> CallManager:
    return CallManager(
        agent_name="helper",
        config=config or _config(),
        client=client,
        runtime_paths=test_runtime_paths(tmp_path),
        ssl_verify=True,
        bridge_factory=lambda _identity, _e2ee: bridge,
        tool_support=tool_support,  # type: ignore[arg-type]
        clock_ms=clock_ms,
    )


def _room(*, encrypted: bool = False, room_id: str = ROOM_ID) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id=room_id, own_user_id=BOT_USER)
    room.encrypted = encrypted
    return room


def _member_unknown_event() -> nio.UnknownEvent:
    return nio.UnknownEvent(
        {"event_id": "$e1", "sender": "@alice:example.org", "origin_server_ts": 1_000},
        CALL_MEMBER_EVENT_TYPE,
    )


def _frame_key_event(
    *,
    room_id: str = ROOM_ID,
    user_id: str = "@alice:example.org",
    device_id: str = "ALICEDEV",
) -> nio.UnknownToDeviceEvent:
    """Build one decrypted inbound Element Call frame key event."""
    key_base64 = base64.b64encode(b"A" * 16).decode("ascii")
    event = nio.UnknownToDeviceEvent.from_dict(
        {
            "type": CALL_ENCRYPTION_KEYS_EVENT_TYPE,
            "sender": user_id,
            "content": build_key_to_device_content(
                key_base64=key_base64,
                key_index=2,
                room_id=room_id,
                member_id=f"{user_id}:{device_id}",
                device_id=device_id,
                sent_ts=1_500,
            ),
        },
    )
    assert isinstance(event, nio.UnknownToDeviceEvent)
    return event


@pytest.fixture(autouse=True)
def _stub_join_externals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_manager.get_api_key_for_provider",
        lambda _provider, _paths: "sk-test",
    )

    async def fake_grant(*_args: object, **_kwargs: object) -> SfuGrant:
        return GRANT

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.request_sfu_grant", fake_grant)

    async def fake_tools(**_kwargs: object) -> CallAgentTooling:
        return CallAgentTooling(tools=[], tool_names=(), instructions="You are Helper.")

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.build_call_tools", fake_tools)


@pytest.mark.asyncio
async def test_manager_joins_call_when_remote_member_appears(tmp_path: Path) -> None:
    """Manager joins call when remote member appears."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant == GRANT
    assert bridge.participant_rosters == [frozenset({"@alice:example.org:ALICEDEV"})]
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
async def test_manager_requires_current_room_membership_for_call_roster(tmp_path: Path) -> None:
    """Stale call state from a former room member cannot activate the agent."""
    call_event = _remote_member_event()
    client = _client()
    client.room_get_state.return_value = nio.RoomGetStateResponse(
        [call_event, _room_member_event(membership="leave")],
        ROOM_ID,
    )
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_room_membership_event_removes_stale_call_participant(tmp_path: Path) -> None:
    """A room leave triggers reconciliation even when call state does not change."""
    call_event = _remote_member_event()
    client = _client()
    client.room_get_state.return_value = _state_response(call_event)
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    room = _room()
    await manager.on_room_event(room, _member_unknown_event())

    client.room_get_state.return_value = nio.RoomGetStateResponse(
        [call_event, _room_member_event(membership="leave")],
        ROOM_ID,
    )
    member_event = nio.RoomMemberEvent.from_dict(
        {
            "event_id": "$leave",
            "sender": "@alice:example.org",
            "state_key": "@alice:example.org",
            "type": "m.room.member",
            "origin_server_ts": int(time.time() * 1000),
            "content": {"membership": "leave"},
        },
    )
    assert isinstance(member_event, nio.RoomMemberEvent)

    await manager.on_room_membership_event(room, member_event)

    assert bridge.closed


@pytest.mark.asyncio
async def test_manager_ignores_unrelated_event_types(tmp_path: Path) -> None:
    """Manager ignores unrelated event types."""
    client = _client()
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    event = nio.UnknownEvent(
        {"event_id": "$e2", "sender": "@alice:example.org", "origin_server_ts": 1_000},
        "io.mindroom.tool_approval_response",
    )

    await manager.on_room_event(_room(), event)

    client.room_get_state.assert_not_awaited()
    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_manager_ignores_calls_outside_agent_rooms(tmp_path: Path) -> None:
    """Call events in dynamically joined rooms cannot activate this agent."""
    client = _client()
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(room_id="!other:example.org"), _member_unknown_event())

    client.room_get_state.assert_not_awaited()
    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_manager_rejects_unauthorized_call_members(tmp_path: Path) -> None:
    """A participant must pass normal room authorization before the agent joins."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    config = _config()
    config.authorization = AuthorizationConfig()
    manager = _manager(client, bridge, tmp_path, config)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_manager_rejects_members_denied_by_agent_reply_permissions(tmp_path: Path) -> None:
    """Per-agent reply permissions also gate whole-call admission."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    config = _config()
    config.authorization.agent_reply_permissions = {"helper": ["@other:example.org"]}
    manager = _manager(client, bridge, tmp_path, config)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_manager_leaves_call_when_room_call_empties(tmp_path: Path) -> None:
    """Manager leaves call when room call empties."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.connected_grant is not None

    empty_leave_event = {
        "type": CALL_MEMBER_EVENT_TYPE,
        "state_key": membership_state_key("@alice:example.org", "ALICEDEV"),
        "sender": "@alice:example.org",
        "origin_server_ts": 2_000,
        "content": {},
    }
    client.room_get_state.return_value = _state_response(empty_leave_event)
    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.closed
    # The bot cleared its own membership state event on leave.
    final_args, final_kwargs = client.room_put_state.await_args_list[-1]
    assert final_args[2] == {}
    assert final_kwargs["state_key"] == membership_state_key(BOT_USER, BOT_DEVICE)


@pytest.mark.asyncio
async def test_manager_leaves_when_a_denied_member_joins(tmp_path: Path) -> None:
    """An active agent leaves rather than sharing a call with a denied participant."""
    client = _client()
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    client.room_get_state.return_value = _state_response(_remote_member_event())
    await manager.on_room_event(_room(), _member_unknown_event())

    client.room_get_state.return_value = _state_response(
        _remote_member_event(),
        _remote_member_event(user="@mallory:example.org", device="MALLORYDEV"),
    )
    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.closed


@pytest.mark.asyncio
async def test_manager_reconciles_active_calls_after_sync(tmp_path: Path) -> None:
    """Initial full-state calls are discovered even without a timeline event."""
    client = _client()
    client.rooms = {ROOM_ID: _room()}
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.reconcile_joined_rooms()

    assert bridge.connected_grant is GRANT


@pytest.mark.asyncio
async def test_manager_skips_join_without_openai_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Manager skips join without openai key."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_provider", lambda _provider, _paths: None)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_manager_reads_openai_key_from_shared_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Voice calls use the same dashboard-backed key source as model loading."""
    requested_providers: list[str] = []

    def fake_api_key(provider: str, _paths: object) -> str:
        requested_providers.append(provider)
        return "sk-dashboard"

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_provider", fake_api_key)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert requested_providers == ["openai"]
    assert bridge.agent_options is not None
    assert bridge.agent_options.api_key == "sk-dashboard"


@pytest.mark.asyncio
async def test_manager_handles_missing_device_id_as_a_join_failure(tmp_path: Path) -> None:
    """A not-yet-initialized Matrix client must not crash the event callback."""
    client = _client()
    client.device_id = None
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant is None


@pytest.mark.asyncio
async def test_manager_shutdown_stops_sessions(tmp_path: Path) -> None:
    """Manager shutdown stops sessions."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    await manager.on_room_event(_room(), _member_unknown_event())

    await manager.shutdown()

    assert bridge.closed
    # Events after shutdown must not start new sessions.
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.frame_keys == []


@pytest.mark.asyncio
async def test_manager_shutdown_continues_after_a_session_stop_failure(tmp_path: Path) -> None:
    """One broken call teardown cannot leak another active call."""
    client = _client()
    first_bridge = FakeBridge()
    second_bridge = FakeBridge()

    async def failed_finalizer() -> None:
        msg = "finalizer failed"
        raise RuntimeError(msg)

    first = _plain_session(client, first_bridge, on_stopped=failed_finalizer)
    second = _plain_session(client, second_bridge)
    second.room_id = "!other:example.org"
    manager = _manager(client, FakeBridge(), tmp_path)
    manager._sessions = {first.room_id: first, second.room_id: second}

    await manager.shutdown()

    assert first_bridge.closed
    assert second_bridge.closed


@pytest.mark.asyncio
async def test_manager_reconcile_contains_session_stop_failure(tmp_path: Path) -> None:
    """A teardown failure on call end cannot escape the Matrix sync callback."""
    client = _client()
    client.room_get_state.return_value = nio.RoomGetStateResponse([], ROOM_ID)
    bridge = FakeBridge()

    async def failed_finalizer() -> None:
        message = "disk full"
        raise OSError(message)

    session = _plain_session(client, bridge, on_stopped=failed_finalizer)
    manager = _manager(client, FakeBridge(), tmp_path)
    manager._sessions[ROOM_ID] = session

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.closed
    assert manager._sessions == {}


def test_maybe_build_call_manager_respects_configuration(tmp_path: Path) -> None:
    """Maybe build call manager respects configuration."""
    client = _client()
    runtime_paths = test_runtime_paths(tmp_path)
    disabled = maybe_build_call_manager(
        agent_name="helper",
        config=_config(enabled=False),
        client=client,
        runtime_paths=runtime_paths,
        ssl_verify=True,
    )
    assert disabled is None
    not_listed = maybe_build_call_manager(
        agent_name="other",
        config=_config(),
        client=client,
        runtime_paths=runtime_paths,
        ssl_verify=True,
    )
    assert not_listed is None
    enabled = maybe_build_call_manager(
        agent_name="helper",
        config=_config(),
        client=client,
        runtime_paths=runtime_paths,
        ssl_verify=True,
    )
    assert isinstance(enabled, CallManager)


def test_maybe_build_call_manager_survives_missing_livekit_package(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing livekit package disables calls instead of crashing agent startup."""

    def raising_find_spec(_name: str) -> None:
        msg = "No module named 'livekit'"
        raise ModuleNotFoundError(msg)

    monkeypatch.setattr("importlib.util.find_spec", raising_find_spec)
    manager = maybe_build_call_manager(
        agent_name="helper",
        config=_config(),
        client=_client(),
        runtime_paths=test_runtime_paths(tmp_path),
        ssl_verify=True,
    )
    assert manager is None


def test_build_call_instructions_falls_back_to_config() -> None:
    """Without a chat system prompt the config-derived fallback is used."""
    text = _build_call_instructions("helper", _config(), None)
    assert "Helper" in text
    assert "Answer questions" in text
    assert "Be kind." in text
    assert "spoken" in text


def test_build_call_instructions_prefers_chat_system_prompt() -> None:
    """The agent chat prompt wins, with voice guidance appended."""
    text = _build_call_instructions("helper", _config(), "CHAT SYSTEM PROMPT")
    assert text.startswith("CHAT SYSTEM PROMPT")
    assert "spoken" in text
    assert "Answer questions" not in text


def _member(
    user: str,
    device: str,
    created_ts: int = 0,
    livekit_service_url: str | None = SERVICE_URL,
) -> CallMember:
    from mindroom.matrix_rtc.events import CallMember  # noqa: PLC0415

    return CallMember(
        user_id=user,
        device_id=device,
        created_ts=created_ts,
        expires_ms=10_000_000,
        membership_id=f"{user}:{device}",
        livekit_service_url=livekit_service_url,
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
async def test_session_publishes_membership_before_peer_receives_first_key() -> None:
    """A peer can admit the sender's first E2EE key from authoritative membership."""
    sender_client = _client()
    receiver_client = _client()
    receiver_client.user_id = "@alice:example.org"
    receiver_client.device_id = "ALICEDEV"
    receiver_bridge = FakeBridge()
    receiver_session = _session(receiver_client, receiver_bridge, FakeKeyTransport(), [1_000])
    sender_member = _member(BOT_USER, BOT_DEVICE)
    receiver_member = _member(receiver_client.user_id, receiver_client.device_id)
    sender_published = False

    async def record_sender_membership(
        _room_id: str,
        _event_type: str,
        content: dict,
        *,
        state_key: str,
    ) -> MagicMock:
        nonlocal sender_published
        assert state_key == membership_state_key(BOT_USER, BOT_DEVICE)
        if content:
            sender_published = True
        return MagicMock()

    sender_client.room_put_state.side_effect = record_sender_membership

    class PeerTransport(FakeKeyTransport):
        async def send_key(
            self,
            *,
            room_id: str,
            key_base64: str,
            key_index: int,
            targets: list[CallMember],
        ) -> list[CallMember]:
            assert sender_published
            receiver_session._members = [sender_member]
            admitted = receiver_session.on_key_received(
                ReceivedFrameKey(
                    user_id=BOT_USER,
                    claimed_device_id=BOT_DEVICE,
                    key_base64=key_base64,
                    key_index=key_index,
                ),
            )
            assert admitted
            return await super().send_key(
                room_id=room_id,
                key_base64=key_base64,
                key_index=key_index,
                targets=targets,
            )

    sender_session = _session(sender_client, FakeBridge(), PeerTransport(), [1_000])

    await sender_session.start([receiver_member])

    assert len(receiver_bridge.frame_keys) == 1
    participant_identity, received_key, key_index = receiver_bridge.frame_keys[0]
    assert participant_identity == f"{BOT_USER}:{BOT_DEVICE}"
    assert len(received_key) == 16
    assert key_index == 0
    await sender_session.stop()
    await receiver_session.stop()


@pytest.mark.asyncio
async def test_session_installs_inbound_keys_on_bridge() -> None:
    """Session derives the media identity from its trusted call roster."""
    client = _client()
    bridge = FakeBridge()
    transport = FakeKeyTransport()
    clock = [1_000]
    session = _session(client, bridge, transport, clock)
    await session.start([_member("@alice:example.org", "ALICEDEV")])
    bridge.frame_keys.clear()

    accepted = session.on_key_received(
        ReceivedFrameKey(
            user_id="@alice:example.org",
            claimed_device_id="ALICEDEV",
            key_base64="QUFBQUFBQUFBQUFBQUFBQQ==",
            key_index=2,
            sent_ts=1_500,
        ),
    )

    assert accepted is True
    assert bridge.frame_keys == [("@alice:example.org:ALICEDEV", b"A" * 16, 2)]
    await session.stop()


@pytest.mark.asyncio
async def test_session_rejects_inbound_key_from_device_outside_roster() -> None:
    """An authorized user cannot inject a key for a device outside the active call."""
    bridge = FakeBridge()
    session = _session(_client(), bridge, FakeKeyTransport(), [1_000])
    await session.start([_member("@alice:example.org", "ALICEDEV")])
    bridge.frame_keys.clear()

    accepted = session.on_key_received(
        ReceivedFrameKey(
            user_id="@alice:example.org",
            claimed_device_id="OTHERDEV",
            key_base64="QUFBQUFBQUFBQUFBQUFBQQ==",
            key_index=2,
            sent_ts=1_500,
        ),
    )

    assert accepted is False
    assert bridge.frame_keys == []
    await session.stop()


@pytest.mark.asyncio
async def test_unencrypted_session_keeps_group_media_roster_current() -> None:
    """Roster enforcement is independent of frame encryption and tracks every device."""
    bridge = FakeBridge()
    session = _plain_session(_client(), bridge)
    alice = _member("@alice:example.org", "ALICEDEV")
    bob = _member("@bob:example.org", "BOBDEV")

    await session.start([alice, bob])
    await session.on_members_changed([bob])

    assert bridge.participant_rosters == [
        frozenset({"@alice:example.org:ALICEDEV", "@bob:example.org:BOBDEV"}),
        frozenset({"@bob:example.org:BOBDEV"}),
    ]
    await session.stop()


@pytest.mark.asyncio
async def test_manager_passes_same_agent_tools_and_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The realtime session gets chat tools, prompt, and transcript hooks."""
    sentinel_tool = object()

    async def fake_build_call_tools(**_kwargs: object) -> CallAgentTooling:
        return CallAgentTooling(tools=[sentinel_tool], tool_names=("magic",), instructions="CHAT PROMPT")

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.build_call_tools", fake_build_call_tools)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path, tool_support=object())

    await manager.on_room_event(_room(), _member_unknown_event())

    options = bridge.agent_options
    assert options is not None
    assert options.tools == (sentinel_tool,)
    assert options.instructions.startswith("CHAT PROMPT")
    assert options.on_conversation_turn is not None
    assert options.on_tools_executed is not None


@pytest.mark.asyncio
async def test_manager_replays_a_key_received_before_startup_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A to-device key preceding full-state call discovery remains available."""

    async def send_key(_self: object, *, targets: list[CallMember], **_kwargs: object) -> list[CallMember]:
        return targets

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.ToDeviceFrameKeyTransport.send_key", send_key)
    client = _client()
    client.rooms = {ROOM_ID: _room(encrypted=True)}
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_to_device_event(_frame_key_event())
    await manager.reconcile_joined_rooms()

    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) in bridge.frame_keys


@pytest.mark.asyncio
async def test_manager_replays_a_key_received_before_active_roster_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A key racing ahead of a new member state event is replayed after reconciliation."""

    async def send_key(_self: object, *, targets: list[CallMember], **_kwargs: object) -> list[CallMember]:
        return targets

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.ToDeviceFrameKeyTransport.send_key", send_key)
    config = _config()
    config.authorization.global_users.append("@bob:example.org")
    client = _client()
    room = _room(encrypted=True)
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path, config)

    await manager.on_room_event(room, _member_unknown_event())

    client.room_get_state.return_value = _state_response(
        _remote_member_event(),
        _remote_member_event(user="@bob:example.org", device="BOBDEV"),
    )
    await manager.on_to_device_event(
        _frame_key_event(user_id="@bob:example.org", device_id="BOBDEV"),
    )

    assert ("@bob:example.org:BOBDEV", b"A" * 16, 2) in bridge.frame_keys


@pytest.mark.asyncio
async def test_manager_accepts_key_for_alias_only_configured_room(tmp_path: Path) -> None:
    """To-device admission uses the cached room alias just like room events."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper", rooms=["#voice:example.org"])},
        models={},
        authorization=AuthorizationConfig(global_users=["@alice:example.org"]),
        calls=CallsConfig(enabled=True, agents=["helper"], livekit_service_url=SERVICE_URL),
    )
    room = _room(encrypted=True)
    room.canonical_alias = "#voice:example.org"
    client = _client()
    client.rooms = {ROOM_ID: room}
    client.room_get_state.return_value = _state_response(_remote_member_event())
    manager = _manager(client, FakeBridge(), tmp_path, config)

    await manager.on_to_device_event(_frame_key_event())

    assert ROOM_ID in manager._pending_keys


@pytest.mark.asyncio
async def test_manager_rejects_pending_key_from_device_outside_roster(tmp_path: Path) -> None:
    """Pre-join key buffering requires an exact current user/device membership."""
    client = _client()
    client.rooms = {ROOM_ID: _room(encrypted=True)}
    client.room_get_state.return_value = _state_response(_remote_member_event(device="DIFFERENTDEV"))
    manager = _manager(client, FakeBridge(), tmp_path)

    await manager.on_to_device_event(_frame_key_event())

    assert manager._pending_keys == {}


def test_manager_bounds_and_deduplicates_pending_keys(tmp_path: Path) -> None:
    """A stalled join cannot accumulate an unbounded to-device key backlog."""
    manager = _manager(_client(), FakeBridge(), tmp_path)
    for index in range(_MAX_PENDING_KEYS_PER_ROOM + 1):
        manager._queue_pending_key(
            ROOM_ID,
            ReceivedFrameKey(
                user_id="@alice:example.org",
                claimed_device_id="ALICEDEV",
                key_base64="QUFBQUFBQUFBQUFBQUFBQQ==",
                key_index=index,
            ),
        )

    pending = manager._pending_keys[ROOM_ID]
    assert len(pending) == _MAX_PENDING_KEYS_PER_ROOM
    assert ("@alice:example.org", "ALICEDEV", 0) not in pending

    replacement = ReceivedFrameKey(
        user_id="@alice:example.org",
        claimed_device_id="ALICEDEV",
        key_base64="QkJCQkJCQkJCQkJCQkJCQg==",
        key_index=1,
    )
    manager._queue_pending_key(ROOM_ID, replacement)
    assert len(pending) == _MAX_PENDING_KEYS_PER_ROOM
    assert pending[("@alice:example.org", "ALICEDEV", 1)] is replacement


@pytest.mark.asyncio
async def test_manager_replays_a_key_received_while_starting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A key received after call membership publication is applied once the bridge is ready."""

    async def send_key(_self: object, *, targets: list[CallMember], **_kwargs: object) -> list[CallMember]:
        return targets

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.ToDeviceFrameKeyTransport.send_key", send_key)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    agent_starting = asyncio.Event()
    release_agent = asyncio.Event()

    async def blocked_start_agent(options: VoiceAgentOptions) -> None:
        bridge.agent_options = options
        agent_starting.set()
        await release_agent.wait()

    bridge.start_agent = blocked_start_agent  # type: ignore[method-assign]
    join_task = asyncio.create_task(manager.on_room_event(_room(encrypted=True), _member_unknown_event()))
    await asyncio.wait_for(agent_starting.wait(), timeout=1)

    await manager.on_to_device_event(_frame_key_event())
    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) not in bridge.frame_keys

    release_agent.set()
    await join_task

    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) in bridge.frame_keys


@pytest.mark.asyncio
async def test_manager_uses_oldest_membership_federated_https_focus(
    tmp_path: Path,
) -> None:
    """A federated call follows the sticky HTTPS focus advertised by its oldest member."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=CallsConfig(enabled=True, agents=["helper"]),
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, config)
    members = [
        _member(
            "@oldest:remote.example",
            "OLD",
            created_ts=1,
            livekit_service_url="https://rtc.remote.example/",
        ),
        _member("@newer:example.org", "NEW", created_ts=2),
    ]

    service = await manager._resolve_service(members)

    assert service is not None
    assert service.url == "https://rtc.remote.example"
    assert not service.allow_private_networks


@pytest.mark.asyncio
async def test_manager_rejects_same_server_focus_that_config_does_not_trust(
    tmp_path: Path,
) -> None:
    """A same-server participant cannot override the operator's configured focus."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=CallsConfig(enabled=True, agents=["helper"], livekit_service_url=SERVICE_URL),
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, config)
    member = _member(
        "@alice:example.org",
        "ALICEDEV",
        livekit_service_url="https://attacker.example",
    )

    assert await manager._resolve_service([member]) is None


@pytest.mark.asyncio
async def test_manager_recovers_inherited_focus_after_founder_leaves(tmp_path: Path) -> None:
    """A restarted bot follows the focus a remaining follower inherited from a departed founder."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=CallsConfig(enabled=True, agents=["helper"]),
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, config)
    follower = _member(
        "@follower:follower.example",
        "FOLLOWER",
        livekit_service_url="https://rtc.founder.example",
    )

    service = await manager._resolve_service([follower])

    assert service is not None
    assert service.url == "https://rtc.founder.example"
    assert not service.allow_private_networks


@pytest.mark.asyncio
async def test_manager_rejects_insecure_federated_focus(tmp_path: Path) -> None:
    """Federated membership cannot redirect an OpenID token over plaintext HTTP."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=CallsConfig(enabled=True, agents=["helper"]),
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, config)
    member = _member(
        "@alice:remote.example",
        "ALICEDEV",
        livekit_service_url="http://rtc.remote.example",
    )

    assert await manager._resolve_service([member]) is None


@pytest.mark.asyncio
async def test_transient_state_fetch_error_keeps_active_session(tmp_path: Path) -> None:
    """A homeserver error on state fetch must not tear down a live call."""
    client = _client()
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    client.room_get_state.return_value = _state_response(_remote_member_event())
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.connected_grant is GRANT

    client.room_get_state.return_value = nio.RoomGetStateError("503 upstream sad")
    await manager.on_room_event(_room(), _member_unknown_event())
    assert not bridge.closed

    # A genuinely empty call still ends the session.
    client.room_get_state.return_value = nio.RoomGetStateResponse([], ROOM_ID)
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", [nio.RoomGetStateError("503 upstream sad"), aiohttp.ClientError("offline")])
async def test_state_fetch_failure_retries_without_another_call_event(
    first_failure: object,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup reconciliation recovers after response and transport failures."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager._RECONCILE_RETRY_DELAYS_S", (0.0,))
    client = _client()
    client.room_get_state.side_effect = [first_failure, _state_response(_remote_member_event())]
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())
    for _ in range(20):
        if bridge.connected_grant is not None:
            break
        await asyncio.sleep(0)

    assert bridge.connected_grant is GRANT
    await manager.shutdown()


@pytest.mark.asyncio
async def test_call_member_expiry_reconciles_without_a_new_event(tmp_path: Path) -> None:
    """An expired membership cannot keep a media session alive indefinitely."""
    clock_values = iter((1_000, 1_000, 1_001))
    call_event = _remote_member_event(created_ts=1_000, expires_ms=1)
    client = _client()
    client.room_get_state.return_value = _state_response(call_event)
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path, clock_ms=lambda: next(clock_values))

    await manager.on_room_event(_room(), _member_unknown_event())
    for _ in range(20):
        if bridge.closed:
            break
        await asyncio.sleep(0.001)

    assert bridge.closed


@pytest.mark.asyncio
async def test_shutdown_during_join_stops_the_new_session(tmp_path: Path) -> None:
    """A join that completes while shutdown runs must not leak a live session."""
    client = _client()
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    client.room_get_state.return_value = _state_response(_remote_member_event())

    release = asyncio.Event()
    original_connect = bridge.connect

    async def blocking_connect(grant: SfuGrant) -> None:
        await release.wait()
        await original_connect(grant)

    bridge.connect = blocking_connect  # type: ignore[method-assign]

    join_task = asyncio.create_task(manager.on_room_event(_room(), _member_unknown_event()))
    for _ in range(20):
        await asyncio.sleep(0)
    shutdown_task = asyncio.create_task(manager.shutdown())
    for _ in range(5):
        await asyncio.sleep(0)
    release.set()
    await join_task
    await shutdown_task
    assert bridge.closed


@pytest.mark.asyncio
async def test_bridge_connect_failure_is_a_clean_join_failure(tmp_path: Path) -> None:
    """livekit-native connect errors become an ordinary failed join, not a crash."""
    client = _client()
    bridge = FakeBridge()

    async def exploding_connect(_grant: SfuGrant) -> None:
        bridge.connected_grant = GRANT
        msg = "sdk boom"
        raise RuntimeError(msg)

    bridge.connect = exploding_connect  # type: ignore[method-assign]
    manager = _manager(client, bridge, tmp_path)
    client.room_get_state.return_value = _state_response(_remote_member_event())
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.agent_options is None
    assert bridge.closed


def _plain_session(
    client: AsyncMock,
    bridge: FakeBridge,
    *,
    on_stopped: object = None,
) -> CallSession:
    async def fetch_grant() -> SfuGrant:
        return GRANT

    return CallSession(
        room_id=ROOM_ID,
        e2ee_enabled=False,
        deps=CallSessionDeps(
            client=client,
            bridge=bridge,
            key_transport=FakeKeyTransport(),
            fetch_grant=fetch_grant,
            agent_options=VoiceAgentOptions(instructions="x", model="m", api_key="k"),
            livekit_service_url=SERVICE_URL,
            on_stopped=on_stopped,  # type: ignore[arg-type]
        ),
    )


@pytest.mark.asyncio
async def test_stop_closes_bridge_and_finalizes_when_clear_membership_fails() -> None:
    """Transport failures while clearing membership must not skip media teardown."""
    client = _client()
    client.room_put_state.side_effect = aiohttp.ClientError("network down")
    bridge = FakeBridge()
    finalized: list[bool] = []

    async def on_stopped() -> None:
        finalized.append(True)

    session = _plain_session(client, bridge, on_stopped=on_stopped)
    await session.stop()
    assert bridge.closed
    assert finalized == [True]


@pytest.mark.asyncio
async def test_stop_still_tears_down_on_unexpected_clear_error() -> None:
    """Even unexpected errors propagate only after aclose and finalization ran."""
    client = _client()
    client.room_put_state.side_effect = RuntimeError("bug")
    bridge = FakeBridge()
    finalized: list[bool] = []

    async def on_stopped() -> None:
        finalized.append(True)

    session = _plain_session(client, bridge, on_stopped=on_stopped)
    with pytest.raises(RuntimeError, match="bug"):
        await session.stop()
    assert bridge.closed
    assert finalized == [True]


@pytest.mark.asyncio
async def test_stop_drains_cancelled_background_tasks() -> None:
    """Session shutdown waits until cancelled background work has unwound."""
    session = _plain_session(_client(), FakeBridge())
    cancelled = asyncio.Event()

    async def background() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    session._spawn(background())
    await asyncio.sleep(0)

    await session.stop()

    assert cancelled.is_set()
    assert session._tasks == set()


@pytest.mark.asyncio
async def test_membership_refresh_retries_the_same_window_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed refresh retries the same iteration instead of skipping a window."""
    client = _client()
    error = MagicMock(spec=nio.RoomPutStateError)
    error.message = "boom"
    client.room_put_state.side_effect = [error, MagicMock()]
    session = _plain_session(client, FakeBridge())
    session._created_ts = 0

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 4:
            session._stopped = True
        await real_sleep(0)

    monkeypatch.setattr("mindroom.matrix_rtc.call_session.asyncio.sleep", fake_sleep)
    await session._membership_refresh_loop()

    assert client.room_put_state.await_count == 2
    assert session._refresh_iteration == 2
    # The second sleep is the short retry delay, not a full refresh window.
    assert sleeps[1] == pytest.approx(60.0)
    assert [call.args[2]["expires"] for call in client.room_put_state.await_args_list] == [
        2 * DEFAULT_MEMBERSHIP_EXPIRES_MS,
        2 * DEFAULT_MEMBERSHIP_EXPIRES_MS,
    ]


@pytest.mark.asyncio
async def test_session_retries_members_that_did_not_receive_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A skipped to-device send remains eligible until delivery succeeds."""

    class RetryTransport(FakeKeyTransport):
        async def send_key(
            self,
            *,
            room_id: str,
            key_base64: str,
            key_index: int,
            targets: list[CallMember],
        ) -> list[CallMember]:
            await super().send_key(
                room_id=room_id,
                key_base64=key_base64,
                key_index=key_index,
                targets=targets,
            )
            return [] if len(self.sent) == 1 else targets

    real_sleep = asyncio.sleep

    async def immediate_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("mindroom.matrix_rtc.call_session.asyncio.sleep", immediate_sleep)
    bridge = FakeBridge()
    transport = RetryTransport()
    session = _session(_client(), bridge, transport, [1_000])
    alice = _member("@alice:example.org", "ALICEDEV")

    session._members = [alice]
    await session._distribute_keys()
    for _ in range(10):
        if len(transport.sent) == 2:
            break
        await real_sleep(0)

    assert len(transport.sent) == 2
    await session.stop()


@pytest.mark.asyncio
async def test_key_distribution_serializes_roster_change_after_inflight_send() -> None:
    """A leaver during key delivery is followed by a rotation for the latest roster."""

    class BlockingTransport(FakeKeyTransport):
        def __init__(self) -> None:
            super().__init__()
            self.sending = asyncio.Event()
            self.release = asyncio.Event()

        async def send_key(
            self,
            *,
            room_id: str,
            key_base64: str,
            key_index: int,
            targets: list[CallMember],
        ) -> list[CallMember]:
            await super().send_key(
                room_id=room_id,
                key_base64=key_base64,
                key_index=key_index,
                targets=targets,
            )
            if len(self.sent) == 1:
                self.sending.set()
                await self.release.wait()
            return targets

    transport = BlockingTransport()
    session = _session(_client(), FakeBridge(), transport, [1_000])
    alice = _member("@alice:example.org", "ALICEDEV")
    bob = _member("@bob:example.org", "BOBDEV")
    session._members = [alice, bob]
    initial = asyncio.create_task(session._distribute_keys())
    await asyncio.wait_for(transport.sending.wait(), timeout=1)

    changed = asyncio.create_task(session.on_members_changed([bob]))
    await asyncio.sleep(0)
    transport.release.set()
    await initial
    await changed

    assert [send["key_index"] for send in transport.sent] == [0, 1]
    assert transport.sent[1]["targets"] == [bob]
    assert session._key_manager.update_memberships([bob], now_ms=1_001) is None
    await session.stop()


def test_calls_config_rejects_unknown_agents() -> None:
    """Call configuration may reference only declared agents."""
    with pytest.raises(ValueError, match=r"calls\.agents references unknown agent"):
        Config(models={}, calls=CallsConfig(enabled=True, agents=["missing"]))


def test_calls_config_rejects_requester_private_agents() -> None:
    """Voice calls cannot safely materialize requester-private state."""
    with pytest.raises(ValueError, match=r"calls\.agents cannot reference requester-private agent"):
        Config(
            models={},
            agents={
                "private": AgentConfig(
                    display_name="Private",
                    private=AgentPrivateConfig(per="user_agent"),
                ),
            },
            calls=CallsConfig(enabled=True, agents=["private"]),
        )


def test_calls_config_rejects_agents_sharing_a_room() -> None:
    """Two call agents cannot both join the same configured room."""
    with pytest.raises(ValueError, match=r"calls\.agents configures multiple agents for room"):
        Config(
            models={},
            agents={
                "one": AgentConfig(display_name="One", rooms=["voice"]),
                "two": AgentConfig(display_name="Two", rooms=["voice"]),
            },
            calls=CallsConfig(enabled=True, agents=["one", "two"]),
        )


class UndeliverableKeyTransport(FakeKeyTransport):
    """A transport whose targets never receive the key."""

    async def send_key(self, **kwargs: object) -> list[CallMember]:
        """Record the attempt and deliver to nobody."""
        await super().send_key(**kwargs)  # type: ignore[arg-type]
        return []


@pytest.mark.asyncio
async def test_key_distribution_retry_backs_off_and_gives_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undeliverable frame keys retry on a bounded backoff, not a 1s poll forever."""
    client = _client()
    transport = UndeliverableKeyTransport()
    clock = [1_000]
    session = _session(client, FakeBridge(), transport, clock)

    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("mindroom.matrix_rtc.call_session.asyncio.sleep", fake_sleep)

    session._members = [_member("@alice:example.org", "ALICEDEV")]
    await session._distribute_keys()
    for _ in range(50):
        await real_sleep(0)

    # One initial attempt plus one per backoff delay, then it stops.
    assert len(transport.sent) == 4
    assert sleeps == [1.0, 5.0, 30.0]

    # A membership change restarts the budget.
    await session.on_members_changed(
        [_member("@alice:example.org", "ALICEDEV"), _member("@bob:example.org", "BOBDEV")],
    )
    for _ in range(50):
        await real_sleep(0)
    assert len(transport.sent) > 4


def test_calls_config_rejects_agents_sharing_a_resolved_room(tmp_path: Path) -> None:
    """Alias and room-ID spellings cannot activate two call agents in one room."""
    runtime_paths = test_runtime_paths(tmp_path)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_room("voice", ROOM_ID, "#voice:example.org", "Voice")
    state.save(runtime_paths=runtime_paths)

    with pytest.raises(ValueError, match=r"calls\.agents configures multiple agents for room"):
        Config.validate_with_runtime(
            {
                "agents": {
                    "one": {"display_name": "One", "rooms": ["voice"]},
                    "two": {"display_name": "Two", "rooms": [ROOM_ID]},
                },
                "calls": {"enabled": True, "agents": ["one", "two"]},
            },
            runtime_paths,
        )


def test_calls_config_rejects_equivalent_room_refs_before_matrix_state(tmp_path: Path) -> None:
    """Managed room keys and their full aliases cannot bypass call ownership validation."""
    with pytest.raises(ValueError, match=r"calls\.agents configures multiple agents for room"):
        Config.validate_with_runtime(
            {
                "agents": {
                    "one": {"display_name": "One", "rooms": ["voice"]},
                    "two": {"display_name": "Two", "rooms": ["#voice:example.org"]},
                },
                "calls": {"enabled": True, "agents": ["one", "two"]},
            },
            test_runtime_paths(tmp_path),
        )


def test_manager_fails_closed_when_live_room_resolves_multiple_call_agents(tmp_path: Path) -> None:
    """Unexpected live alias ambiguity keeps every configured call agent out."""
    config = Config(
        models={},
        agents={
            "helper": AgentConfig(display_name="Helper", rooms=[ROOM_ID]),
            "other": AgentConfig(display_name="Other", rooms=["#voice:example.org"]),
        },
        calls=CallsConfig(enabled=True, agents=["helper", "other"]),
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, config)
    room = _room()
    room.canonical_alias = "#voice:example.org"

    assert not manager._is_configured_call_room(room)
