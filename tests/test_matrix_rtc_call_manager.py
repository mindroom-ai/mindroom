"""Call lifecycle tests for CallManager and CallSession with a fake media plane."""

from __future__ import annotations

import asyncio
import base64
import time
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import httpx
import nio
import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.calls import CallsConfig, CascadedCallProfile, RealtimeCallProfile
from mindroom.config.main import Config
from mindroom.config.memory import MemoryConfig
from mindroom.config.models import ModelConfig
from mindroom.config.voice import SpeechServiceConfig
from mindroom.matrix.state import MatrixState
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from mindroom.matrix_rtc.call_manager import (
    _MAX_PENDING_KEYS_PER_ROOM,
    _PENDING_KEY_TTL_MS,
    CallManager,
    _build_call_instructions,
    maybe_build_call_manager,
)
from mindroom.matrix_rtc.call_session import CallSession, CallSessionDeps
from mindroom.matrix_rtc.call_tools import CallAgentResponse, CallAgentTooling
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
from mindroom.matrix_rtc.voice_agent import (
    CallVoiceAgentOptions,
    CascadedVoiceAgentOptions,
    CascadedVoiceBridge,
    RealtimeVoiceBridge,
    VoiceAgentOptions,
)
from mindroom.model_defaults import LOCAL_OPENAI_API_KEY_DEFAULT
from mindroom.model_loading import get_model_instance
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, build_tool_execution_identity
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agno.models.openai.chat import OpenAIChat

    from mindroom.constants import RuntimePaths
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
        self.agent_options: CallVoiceAgentOptions | None = None
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

    async def start_agent(self, options: CallVoiceAgentOptions) -> None:
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


class RecoveringKeyTransport(FakeKeyTransport):
    """Starts unable to send and becomes usable after inbound Olm traffic."""

    def __init__(self) -> None:
        super().__init__()
        self.available = False
        self.delivered = asyncio.Event()

    async def send_key(
        self,
        *,
        room_id: str,
        key_base64: str,
        key_index: int,
        targets: list[CallMember],
    ) -> list[CallMember]:
        """Deliver only after the test models an established Olm session."""
        await super().send_key(
            room_id=room_id,
            key_base64=key_base64,
            key_index=key_index,
            targets=targets,
        )
        if not self.available:
            return []
        self.delivered.set()
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


def _realtime_calls(
    *,
    enabled: bool = True,
    credentials_service: str = "openai",
    livekit_service_url: str | None = SERVICE_URL,
) -> CallsConfig:
    return CallsConfig(
        enabled=enabled,
        profiles={
            "realtime": RealtimeCallProfile(
                backend="realtime",
                model="gpt-realtime-2.1",
                credentials_service=credentials_service,
                voice="marin",
            ),
        },
        agents={"helper": "realtime"},
        livekit_service_url=livekit_service_url,
    )


def _config(*, enabled: bool = True, credentials_service: str = "openai") -> Config:
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
        calls=_realtime_calls(enabled=enabled, credentials_service=credentials_service),
    )


def _call_execution_identity(
    *,
    runtime_paths: RuntimePaths,
    requester_id: str,
    room_id: str = ROOM_ID,
    agent_name: str = "helper",
    session_id: str | None = None,
) -> ToolExecutionIdentity:
    return build_tool_execution_identity(
        channel="matrix",
        agent_name=agent_name,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        room_id=room_id,
        thread_id=None,
        resolved_thread_id=None,
        session_id=session_id or room_id,
    )


def _call_execution_identity_from_tool_kwargs(kwargs: dict[str, object]) -> ToolExecutionIdentity:
    return _call_execution_identity(
        runtime_paths=cast("RuntimePaths", kwargs["runtime_paths"]),
        requester_id=cast("str", kwargs["requester_id"]),
        room_id=cast("str", kwargs["room_id"]),
        agent_name=cast("str", kwargs["agent_name"]),
        session_id=cast("str | None", kwargs["session_id"]),
    )


def _cascaded_config(*, local: bool = False, call_model: str | None = None) -> Config:
    config = _config()
    if call_model is not None:
        config.models[call_model] = ModelConfig(provider="openai", id="fast-call-model")
    if local:
        config.models["default"] = ModelConfig(
            provider="openai",
            id="local-chat-model",
            extra_kwargs={
                "api_key": LOCAL_OPENAI_API_KEY_DEFAULT,
                "base_url": "http://127.0.0.1:9292/v1",
            },
        )
        config.memory = MemoryConfig(backend="none")
    config.calls = CallsConfig(
        enabled=True,
        profiles={
            "cascaded": CascadedCallProfile(
                backend="cascaded",
                model=call_model,
                stt=SpeechServiceConfig(
                    provider="openai_compatible" if local else "openai",
                    model="whisper-large-v3" if local else "gpt-4o-transcribe",
                    api_key=None if local else "stt-key",
                    host="http://127.0.0.1:9000" if local else None,
                    extra_kwargs={"language": "en"},
                ),
                tts=SpeechServiceConfig(
                    provider="openai_compatible",
                    model="tts-1",
                    api_key=None if local else "tts-key",
                    host="http://127.0.0.1:9001",
                    extra_kwargs={"voice": "ash"},
                ),
            ),
        },
        agents={"helper": "cascaded"},
        livekit_service_url=SERVICE_URL,
    )
    return config


def _manager(
    client: AsyncMock,
    bridge: FakeBridge,
    tmp_path: Path,
    config: Config | None = None,
    tool_support: object = object(),
    clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    invited_rooms_by_agent: dict[str, set[str]] | None = None,
) -> CallManager:
    return CallManager(
        agent_name="helper",
        config=config or _config(),
        client=client,
        runtime_paths=test_runtime_paths(tmp_path),
        ssl_verify=True,
        bridge_factory=lambda _identity, _e2ee: bridge,
        tool_support=tool_support,  # type: ignore[arg-type]
        get_invited_rooms_by_agent=lambda: invited_rooms_by_agent or {},
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
) -> AuthenticatedToDeviceEvent:
    """Build one decrypted inbound Element Call frame key event."""
    key_base64 = base64.b64encode(b"A" * 16).decode("ascii")
    source = {
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
    }
    return AuthenticatedToDeviceEvent(
        source=source,
        sender=user_id,
        type=CALL_ENCRYPTION_KEYS_EVENT_TYPE,
        authenticated_device_id=device_id,
    )


@pytest.fixture(autouse=True)
def _stub_join_externals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_manager.get_api_key_for_service",
        lambda _service, _paths: "sk-test",
    )

    async def fake_grant(*_args: object, **_kwargs: object) -> SfuGrant:
        return GRANT

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.request_sfu_grant", fake_grant)

    async def fake_tools(**kwargs: object) -> CallAgentTooling:
        return CallAgentTooling(
            tools=(),
            instructions="You are Helper.",
            execution_identity=_call_execution_identity_from_tool_kwargs(kwargs),
        )

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
async def test_manager_joins_call_in_authorized_ad_hoc_invited_room(tmp_path: Path) -> None:
    """An accepted invite is sufficient room ownership for a calls-enabled agent."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    config = _config()
    config.agents["helper"].rooms = []
    manager = _manager(
        client,
        bridge,
        tmp_path,
        config,
        invited_rooms_by_agent={"helper": {ROOM_ID}},
    )

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant == GRANT


@pytest.mark.parametrize("invited_room", [False, True])
@pytest.mark.asyncio
async def test_manager_joins_requester_private_agent_in_owned_rooms(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    invited_room: bool,
) -> None:
    """Private call agents use verified caller state in configured and accepted-invite rooms."""
    seen_requesters: list[str] = []

    async def fake_tools(**kwargs: object) -> CallAgentTooling:
        requester_id = cast("str", kwargs["requester_id"])
        seen_requesters.append(requester_id)
        return CallAgentTooling(
            tools=(),
            instructions="Private helper",
            execution_identity=_call_execution_identity_from_tool_kwargs(kwargs),
        )

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.build_call_tools", fake_tools)
    config = Config(
        agents={
            "helper": AgentConfig(
                display_name="Helper",
                rooms=[] if invited_room else [ROOM_ID],
                private=AgentPrivateConfig(per="user_agent"),
            ),
        },
        models={},
        authorization=AuthorizationConfig(global_users=["@alice:example.org"]),
        calls=_realtime_calls(),
    )
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(
        client,
        bridge,
        tmp_path,
        config,
        invited_rooms_by_agent={"helper": {ROOM_ID}} if invited_room else None,
    )

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant == GRANT
    assert seen_requesters == ["@alice:example.org"]
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_stops_call_when_agent_is_kicked_from_ephemeral_room(tmp_path: Path) -> None:
    """Own membership removal tears down media without waiting for another call event."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    room = _room()
    await manager.on_room_event(room, _member_unknown_event())

    await manager.on_sync_room_membership(joined_room_ids=set(), left_room_ids={ROOM_ID})

    assert bridge.closed is True
    assert manager._sessions == {}
    assert manager._logical_calls == {}
    assert ROOM_ID in manager._departed_rooms
    assert ROOM_ID not in manager._observed_rooms
    assert ROOM_ID not in manager._locks


@pytest.mark.asyncio
async def test_manager_ignores_own_departure_from_unmanaged_room(tmp_path: Path) -> None:
    """Ordinary room churn cannot create call-manager departure or lock state."""
    client = _client()
    manager = _manager(client, FakeBridge(), tmp_path)
    room = _room(room_id="!text:example.org")
    event = nio.RoomMemberEvent.from_dict(
        {
            "event_id": "$leave",
            "sender": "@alice:example.org",
            "origin_server_ts": int(time.time() * 1000),
            "type": "m.room.member",
            "state_key": BOT_USER,
            "content": {"membership": "leave"},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)

    await manager.on_room_membership_event(room, event)

    assert manager._departed_rooms == set()
    assert dict(manager._locks) == {}


@pytest.mark.asyncio
async def test_manager_does_not_retain_departed_ad_hoc_room(tmp_path: Path) -> None:
    """Forgotten invite ownership permits teardown without retaining the room ID."""
    client = _client()
    config = _config()
    config.agents["helper"].rooms = []
    invited_rooms = {ROOM_ID}
    manager = _manager(
        client,
        FakeBridge(),
        tmp_path,
        config,
        invited_rooms_by_agent={"helper": invited_rooms},
    )
    room = _room()
    manager._observed_rooms[ROOM_ID] = room
    invited_rooms.clear()
    event = nio.RoomMemberEvent.from_dict(
        {
            "event_id": "$leave",
            "sender": "@alice:example.org",
            "origin_server_ts": int(time.time() * 1000),
            "type": "m.room.member",
            "state_key": BOT_USER,
            "content": {"membership": "leave"},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)

    await manager.on_room_membership_event(room, event)

    assert manager._observed_rooms == {}
    assert manager._departed_rooms == set()
    assert dict(manager._locks) == {}


@pytest.mark.asyncio
async def test_manager_ignores_frame_keys_after_departing_configured_room(tmp_path: Path) -> None:
    """Late to-device keys cannot repopulate state after the bot leaves."""
    client = _client()
    client.rooms = {ROOM_ID: _room(encrypted=True)}
    manager = _manager(client, FakeBridge(), tmp_path)
    event = nio.RoomMemberEvent.from_dict(
        {
            "event_id": "$leave",
            "sender": "@alice:example.org",
            "origin_server_ts": int(time.time() * 1000),
            "type": "m.room.member",
            "state_key": BOT_USER,
            "content": {"membership": "leave"},
        },
    )
    assert isinstance(event, nio.RoomMemberEvent)
    await manager.on_room_membership_event(client.rooms[ROOM_ID], event)

    await manager.on_to_device_event(_frame_key_event())

    assert manager._pending_keys == {}
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_clears_departure_guard_on_own_rejoin(tmp_path: Path) -> None:
    """A forwarded own join makes a configured call room eligible again."""
    client = _client()
    manager = _manager(client, FakeBridge(), tmp_path)
    manager._departed_rooms.add(ROOM_ID)

    await manager.on_sync_room_membership(joined_room_ids={ROOM_ID}, left_room_ids=set())

    assert ROOM_ID not in manager._departed_rooms


@pytest.mark.asyncio
async def test_manager_selects_cascaded_backend_with_independent_speech_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cascaded calls retain separate STT/TTS credentials, endpoints, and options."""

    async def respond(
        _transcript: str,
        _on_tools_executed: Callable[[list[str]], None] | None,
    ) -> CallAgentResponse:
        return CallAgentResponse("answer")

    close_responder = AsyncMock()

    async def fake_tools(**kwargs: object) -> CallAgentTooling:
        assert kwargs["enable_responder"] is True
        assert kwargs["active_model_name"] == "call_fast"
        assert str(kwargs["session_id"]).startswith(f"{ROOM_ID}:call:")
        return CallAgentTooling(
            tools=(),
            instructions="",
            execution_identity=_call_execution_identity_from_tool_kwargs(kwargs),
            responder=respond,
            close=close_responder,
        )

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.build_call_tools", fake_tools)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path, _cascaded_config(call_model="call_fast"))

    await manager.on_room_event(_room(), _member_unknown_event())

    options = bridge.agent_options
    assert isinstance(options, CascadedVoiceAgentOptions)
    assert (options.stt.model, options.stt.api_key, options.stt.base_url) == (
        "gpt-4o-transcribe",
        "stt-key",
        "https://api.openai.com/v1",
    )
    assert options.stt.extra_kwargs == {"language": "en"}
    assert (options.tts.model, options.tts.api_key, options.tts.base_url) == (
        "tts-1",
        "tts-key",
        "http://127.0.0.1:9001/v1",
    )
    assert options.tts.extra_kwargs == {"voice": "ash"}
    assert options.close_responder is close_responder
    await manager.shutdown()


@pytest.mark.asyncio
async def test_cascaded_agent_start_failure_tears_down_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cascaded pipeline startup failure uses the existing safe teardown path."""

    async def respond(
        _transcript: str,
        _on_tools_executed: Callable[[list[str]], None] | None,
    ) -> CallAgentResponse:
        return CallAgentResponse("answer")

    async def fake_tools(**kwargs: object) -> CallAgentTooling:
        return CallAgentTooling(
            tools=(),
            instructions="",
            execution_identity=_call_execution_identity_from_tool_kwargs(kwargs),
            responder=respond,
        )

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.build_call_tools", fake_tools)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()

    async def fail_start(options: CallVoiceAgentOptions) -> None:
        bridge.agent_options = options
        msg = "local TTS unavailable"
        raise RuntimeError(msg)

    bridge.start_agent = fail_start  # type: ignore[method-assign]
    manager = _manager(client, bridge, tmp_path, _cascaded_config(local=True))

    await manager.on_room_event(_room(), _member_unknown_event())

    assert isinstance(bridge.agent_options, CascadedVoiceAgentOptions)
    assert bridge.closed
    assert ROOM_ID in manager._retry_tasks
    await manager.shutdown()


def test_fully_local_speech_services_never_resolve_cloud_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Explicit localhost speech endpoints need no cloud key lookup."""

    def reject_cloud_lookup(*_args: object) -> str:
        msg = "cloud credential lookup is forbidden"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_service", reject_cloud_lookup)
    manager = _manager(_client(), FakeBridge(), tmp_path, _cascaded_config(local=True))

    backend = manager._resolve_voice_backend(ROOM_ID)
    model = cast("OpenAIChat", get_model_instance(manager._config, manager._runtime_paths))

    assert backend is not None
    assert backend.stt is not None
    assert backend.tts is not None
    assert backend.stt.base_url == "http://127.0.0.1:9000/v1"
    assert backend.tts.base_url == "http://127.0.0.1:9001/v1"
    assert backend.stt.api_key == LOCAL_OPENAI_API_KEY_DEFAULT
    assert backend.tts.api_key == LOCAL_OPENAI_API_KEY_DEFAULT
    assert model.id == "local-chat-model"
    assert model.api_key == LOCAL_OPENAI_API_KEY_DEFAULT
    assert model.base_url == "http://127.0.0.1:9292/v1"
    assert manager._config.memory.backend == "none"


def test_openai_speech_with_custom_host_uses_named_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """OpenAI speech resolves only its explicitly selected credential."""
    service_lookup = MagicMock(return_value="openai-call-key")
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_service", service_lookup)
    manager = _manager(_client(), FakeBridge(), tmp_path, _cascaded_config())

    service = manager._resolve_speech_service(
        SpeechServiceConfig(
            provider="openai",
            model="gpt-4o-transcribe",
            credentials_service="openai-realtime",
            host="https://proxy.example.test",
        ),
        component="stt",
        room_id=ROOM_ID,
    )

    assert service is not None
    assert service.api_key == "openai-call-key"
    assert service.base_url == "https://proxy.example.test/v1"
    service_lookup.assert_called_once_with("openai-realtime", manager._runtime_paths)


def test_each_speech_service_selects_its_own_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Each cascaded speech leg may select its own named credential."""
    lookup = MagicMock(return_value="speech-key")
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_service", lookup)
    manager = _manager(_client(), FakeBridge(), tmp_path, _cascaded_config())

    service = manager._resolve_speech_service(
        SpeechServiceConfig(
            provider="openai",
            model="gpt-4o-mini-tts",
            credentials_service="speech-override",
        ),
        component="tts",
        room_id=ROOM_ID,
    )

    assert service is not None
    assert service.api_key == "speech-key"
    lookup.assert_called_once_with("speech-override", manager._runtime_paths)


def test_missing_speech_credential_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A missing named speech credential cannot silently use another service."""
    service_lookup = MagicMock(return_value=None)
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_service", service_lookup)
    manager = _manager(_client(), FakeBridge(), tmp_path, _cascaded_config())

    service = manager._resolve_speech_service(
        SpeechServiceConfig(
            provider="openai",
            model="gpt-4o-mini-tts",
            credentials_service="openai-realtime",
        ),
        component="tts",
        room_id=ROOM_ID,
        warn_if_unavailable=False,
    )

    assert service is None
    service_lookup.assert_called_once_with("openai-realtime", manager._runtime_paths)


def test_openai_cloud_speech_uses_explicit_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A process-wide local OpenAI base URL cannot redirect cloud speech."""
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9292/v1")
    manager = _manager(_client(), FakeBridge(), tmp_path, _cascaded_config())

    service = manager._resolve_speech_service(
        SpeechServiceConfig(provider="openai", model="gpt-4o-transcribe", api_key="cloud-key"),
        component="stt",
        room_id=ROOM_ID,
    )

    assert service is not None
    assert service.base_url == "https://api.openai.com/v1"


def test_call_manager_uses_assigned_realtime_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An agent resolves one complete explicitly assigned profile."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=CallsConfig(
            enabled=True,
            profiles={
                "voice": RealtimeCallProfile(
                    backend="realtime",
                    model="gpt-realtime-global",
                    credentials_service="voice-default",
                    voice="cedar",
                ),
            },
            agents={"helper": "voice"},
        ),
    )
    key_lookup = MagicMock(return_value="sk-global")
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_service", key_lookup)

    manager = CallManager(
        agent_name="helper",
        config=config,
        client=_client(),
        runtime_paths=test_runtime_paths(tmp_path),
        ssl_verify=True,
        tool_support=object(),  # type: ignore[arg-type]
        get_invited_rooms_by_agent=dict,
    )

    backend = manager._resolve_voice_backend(ROOM_ID)

    assert manager._call_config.backend == "realtime"
    assert manager._call_config.model == "gpt-realtime-global"
    assert manager._call_config.credentials_service == "voice-default"
    assert manager._call_config.voice == "cedar"
    assert backend is not None
    assert backend.realtime_api_key == "sk-global"
    key_lookup.assert_called_once_with("voice-default", manager._runtime_paths)


def test_default_bridge_factory_uses_assigned_profile_backend(tmp_path: Path) -> None:
    """Each assigned profile selects its matching media bridge."""
    stt = SpeechServiceConfig(
        provider="openai_compatible",
        model="stt",
        host="http://127.0.0.1:9000",
    )
    tts = SpeechServiceConfig(
        provider="openai_compatible",
        model="tts",
        host="http://127.0.0.1:9001",
    )
    config = Config(
        agents={
            "helper": AgentConfig(display_name="Helper"),
            "other": AgentConfig(display_name="Other"),
        },
        models={},
        calls=CallsConfig(
            enabled=True,
            profiles={
                "realtime": RealtimeCallProfile(
                    backend="realtime",
                    model="gpt-realtime-custom",
                    credentials_service="openai",
                    voice="marin",
                ),
                "cascaded": CascadedCallProfile(backend="cascaded", stt=stt, tts=tts),
            },
            agents={
                "helper": "realtime",
                "other": "cascaded",
            },
        ),
    )
    realtime_manager = CallManager(
        agent_name="helper",
        config=config,
        client=_client(),
        runtime_paths=test_runtime_paths(tmp_path),
        ssl_verify=True,
        tool_support=object(),  # type: ignore[arg-type]
        get_invited_rooms_by_agent=dict,
    )
    cascaded_manager = CallManager(
        agent_name="other",
        config=config,
        client=_client(),
        runtime_paths=test_runtime_paths(tmp_path),
        ssl_verify=True,
        tool_support=object(),  # type: ignore[arg-type]
        get_invited_rooms_by_agent=dict,
    )

    realtime_bridge = realtime_manager._bridge_factory("@helper:example.org:BOTDEV", False)
    cascaded_bridge = cascaded_manager._bridge_factory("@other:example.org:BOTDEV", False)

    assert isinstance(realtime_bridge, RealtimeVoiceBridge)
    assert isinstance(cascaded_bridge, CascadedVoiceBridge)
    assert realtime_manager._call_config.backend == "realtime"
    assert realtime_manager._call_config.model == "gpt-realtime-custom"
    assert realtime_manager._call_config.voice == "marin"
    assert cascaded_manager._call_config.backend == "cascaded"
    assert cascaded_manager._call_config.stt == stt
    assert cascaded_manager._call_config.tts == tts


def test_call_profiles_round_trip_without_inheritance() -> None:
    """Serialization preserves profile assignments without merge semantics."""
    config = Config(
        agents={
            "one": AgentConfig(display_name="One"),
            "two": AgentConfig(display_name="Two"),
        },
        calls=CallsConfig(
            enabled=True,
            profiles={
                "voice": RealtimeCallProfile(
                    backend="realtime",
                    model="gpt-realtime",
                    credentials_service="openai",
                    voice="marin",
                ),
            },
            agents={"one": "voice", "two": "voice"},
        ),
    )

    dumped = config.model_dump()
    restored = Config.model_validate(dumped)

    assert dumped["calls"]["agents"] == {"one": "voice", "two": "voice"}
    assert restored.calls.resolve_agent_config("one").voice == "marin"


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
    assert manager._retry_tasks == {}
    assert manager._retry_attempts == {}


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
async def test_manager_leaves_when_second_authorized_user_joins(tmp_path: Path) -> None:
    """Mixed speakers cannot share one requester identity for tool calls."""
    config = _config()
    config.authorization.global_users.append("@bob:example.org")
    client = _client()
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path, config)
    client.room_get_state.return_value = _state_response(_remote_member_event())
    await manager.on_room_event(_room(), _member_unknown_event())

    client.room_get_state.return_value = _state_response(
        _remote_member_event(),
        _remote_member_event(user="@bob:example.org", device="BOBDEV"),
    )
    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.closed
    assert manager._sessions == {}


@pytest.mark.asyncio
async def test_manager_restarts_when_sole_requester_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A direct caller replacement cannot inherit the previous requester's tools."""
    requesters: list[str] = []

    async def fake_tools(**kwargs: object) -> CallAgentTooling:
        requesters.append(str(kwargs["requester_id"]))
        return CallAgentTooling(
            tools=(),
            instructions="You are Helper.",
            execution_identity=_call_execution_identity_from_tool_kwargs(kwargs),
        )

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.build_call_tools", fake_tools)
    config = _config()
    config.authorization.global_users.append("@bob:example.org")
    client = _client()
    alice_bridge = FakeBridge()
    bob_bridge = FakeBridge()
    bridges = iter((alice_bridge, bob_bridge))
    manager = CallManager(
        agent_name="helper",
        config=config,
        client=client,
        runtime_paths=test_runtime_paths(tmp_path),
        ssl_verify=True,
        bridge_factory=lambda _identity, _e2ee: next(bridges),
        tool_support=object(),  # type: ignore[arg-type]
        get_invited_rooms_by_agent=dict,
    )
    client.room_get_state.return_value = _state_response(_remote_member_event())
    await manager.on_room_event(_room(), _member_unknown_event())

    client.room_get_state.return_value = _state_response(
        _remote_member_event(user="@bob:example.org", device="BOBDEV"),
    )
    await manager.on_room_event(_room(), _member_unknown_event())

    assert requesters == ["@alice:example.org", "@bob:example.org"]
    assert alice_bridge.closed
    assert bob_bridge.connected_grant is GRANT
    assert not bob_bridge.closed
    assert manager._sessions[ROOM_ID].requester_id == "@bob:example.org"
    await manager.shutdown()


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
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_service", lambda _service, _paths: None)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant is None
    assert manager._retry_tasks == {}
    assert manager._retry_attempts == {}


@pytest.mark.asyncio
async def test_manager_reads_key_from_configured_credentials_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Voice calls use their selected dashboard-backed credential service."""
    requested_services: list[str] = []

    def fake_api_key(service: str, _paths: object) -> str:
        requested_services.append(service)
        return "sk-dashboard"

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_service", fake_api_key)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert requested_services == ["openai"]
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
    assert manager._retry_tasks == {}
    assert manager._retry_attempts == {}


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
        tool_support=object(),
        get_invited_rooms_by_agent=dict,
    )
    assert disabled is None
    not_listed = maybe_build_call_manager(
        agent_name="other",
        config=_config(),
        client=client,
        runtime_paths=runtime_paths,
        ssl_verify=True,
        tool_support=object(),
        get_invited_rooms_by_agent=dict,
    )
    assert not_listed is None
    enabled = maybe_build_call_manager(
        agent_name="helper",
        config=_config(),
        client=client,
        runtime_paths=runtime_paths,
        ssl_verify=True,
        tool_support=object(),
        get_invited_rooms_by_agent=dict,
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
        tool_support=object(),
        get_invited_rooms_by_agent=dict,
    )
    assert manager is None


def test_voice_backend_availability_requires_runtime_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Presence readiness is false when the realtime backend cannot authenticate."""
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_manager.get_api_key_for_service",
        lambda _service, _paths: None,
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, _config(credentials_service="openai-realtime"))

    with patch("mindroom.matrix_rtc.call_manager.logger.warning") as warning:
        assert manager.voice_backend_available is False
        warning.assert_not_called()

        assert manager._resolve_voice_backend(ROOM_ID) is None
        warning.assert_called_once_with(
            "call_join_skipped_no_openai_key",
            room_id=ROOM_ID,
            agent="helper",
            credentials_service="openai-realtime",
        )


def test_realtime_backend_uses_configured_credential_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Realtime calls use the strictly configured credential service."""
    selected_services: list[str] = []

    def lookup(service: str, _paths: object) -> str:
        selected_services.append(service)
        return "sk-realtime"

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.get_api_key_for_service", lookup)
    config = _config(credentials_service="openai-realtime")
    manager = _manager(_client(), FakeBridge(), tmp_path, config)

    backend = manager._resolve_voice_backend(ROOM_ID)

    assert backend is not None
    assert backend.realtime_api_key == "sk-realtime"
    assert selected_services == ["openai-realtime"]


def test_realtime_backend_defaults_to_openai_credential_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing installs keep using the shared OpenAI credential by default."""
    monkeypatch.setattr(
        "mindroom.matrix_rtc.call_manager.get_api_key_for_service",
        lambda service, _paths: "sk-shared" if service == "openai" else None,
    )
    manager = _manager(_client(), FakeBridge(), tmp_path)

    backend = manager._resolve_voice_backend(ROOM_ID)

    assert backend is not None
    assert backend.realtime_api_key == "sk-shared"


def test_build_call_instructions_appends_voice_guidance() -> None:
    """The exact chat prompt is retained, with voice guidance appended."""
    text = _build_call_instructions("CHAT SYSTEM PROMPT")
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
        livekit_service_url=livekit_service_url,
    )


def _session(client: AsyncMock, bridge: FakeBridge, transport: FakeKeyTransport, clock: list[int]) -> CallSession:
    async def fetch_grant() -> SfuGrant:
        return GRANT

    return CallSession(
        room_id=ROOM_ID,
        requester_id="@alice:example.org",
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
async def test_session_reports_when_callers_encryption_key_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller the agent cannot decrypt receives a directional diagnosis."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_session._E2EE_READY_TIMEOUT_S", 0)
    notices: list[str] = []

    async def on_failure(message: str) -> None:
        notices.append(message)

    client = _client()
    bridge = FakeBridge()
    session = _session(client, bridge, FakeKeyTransport(), [1_000])
    session.deps.on_failure = on_failure
    await session.start([_member("@alice:example.org", "ALICEDEV")])
    for _ in range(20):
        if notices:
            break
        await asyncio.sleep(0)

    assert len(notices) == 1
    assert "cannot decrypt or hear your microphone audio" in notices[0]
    assert "ALICEDEV" in notices[0]
    assert "one-time-key" in notices[0]
    await session.stop()


@pytest.mark.asyncio
async def test_session_reports_only_device_missing_inbound_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One device's key cannot hide another active device's missing key."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_session._E2EE_READY_TIMEOUT_S", 0)
    notices: list[str] = []

    async def on_failure(message: str) -> None:
        notices.append(message)

    alice_phone = _member("@alice:example.org", "ALICEPHONE")
    alice_tablet = _member("@alice:example.org", "ALICETABLET")
    session = _session(_client(), FakeBridge(), FakeKeyTransport(), [1_000])
    session.deps.on_failure = on_failure
    await session.start([alice_phone, alice_tablet])
    assert session.on_key_received(
        ReceivedFrameKey(
            user_id=alice_phone.user_id,
            claimed_device_id=alice_phone.device_id,
            key_base64="QUFBQUFBQUFBQUFBQUFBQQ==",
            key_index=2,
            received_at_ms=1_500,
        ),
    )
    for _ in range(20):
        if notices:
            break
        await asyncio.sleep(0)

    assert len(notices) == 1
    assert "ALICETABLET" in notices[0]
    assert "ALICEPHONE" not in notices[0]
    await session.stop()


@pytest.mark.asyncio
async def test_rejoining_device_must_send_a_new_inbound_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A departed device cannot reuse its old key-readiness state on rejoin."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_session._E2EE_READY_TIMEOUT_S", 0)
    notices: list[str] = []

    async def on_failure(message: str) -> None:
        notices.append(message)

    alice = _member("@alice:example.org", "ALICEDEV")
    session = _session(_client(), FakeBridge(), FakeKeyTransport(), [1_000])
    session.deps.on_failure = on_failure
    await session.start([alice])
    assert session.on_key_received(
        ReceivedFrameKey(
            user_id=alice.user_id,
            claimed_device_id=alice.device_id,
            key_base64="QUFBQUFBQUFBQUFBQUFBQQ==",
            key_index=2,
            received_at_ms=1_500,
        ),
    )

    await session.on_members_changed([])
    assert session._devices_with_received_key == set()
    await session.on_members_changed([alice])
    for _ in range(20):
        if notices:
            break
        await asyncio.sleep(0)

    assert len(notices) == 1
    assert "ALICEDEV" in notices[0]
    await session.stop()


@pytest.mark.asyncio
async def test_rejoining_device_gets_a_fresh_inbound_key_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout from an earlier roster cannot report a newly rejoined device."""
    timeout_s = 15.0
    monkeypatch.setattr("mindroom.matrix_rtc.call_session._E2EE_READY_TIMEOUT_S", timeout_s)
    real_sleep = asyncio.sleep
    timeout_waiters: list[asyncio.Future[None]] = []

    async def controlled_sleep(delay: float) -> None:
        if delay != timeout_s:
            await real_sleep(delay)
            return
        waiter = asyncio.get_running_loop().create_future()
        timeout_waiters.append(waiter)
        await waiter

    monkeypatch.setattr("mindroom.matrix_rtc.call_session.asyncio.sleep", controlled_sleep)
    notices: list[str] = []

    async def on_failure(message: str) -> None:
        notices.append(message)

    alice = _member("@alice:example.org", "ALICEDEV")
    session = _session(_client(), FakeBridge(), FakeKeyTransport(), [1_000])
    session.deps.on_failure = on_failure
    await session.start([alice])
    while len(timeout_waiters) < 1:
        await real_sleep(0)
    assert session.on_key_received(
        ReceivedFrameKey(
            user_id=alice.user_id,
            claimed_device_id=alice.device_id,
            key_base64="QUFBQUFBQUFBQUFBQUFBQQ==",
            key_index=2,
            received_at_ms=1_500,
        ),
    )

    await session.on_members_changed([])
    await session.on_members_changed([alice])
    while len(timeout_waiters) < 2:
        await real_sleep(0)

    timeout_waiters[0].set_result(None)
    await real_sleep(0)
    assert notices == []

    timeout_waiters[1].set_result(None)
    for _ in range(20):
        if notices:
            break
        await real_sleep(0)
    assert len(notices) == 1
    assert "ALICEDEV" in notices[0]
    await session.stop()


@pytest.mark.asyncio
async def test_session_reports_when_agents_encryption_key_cannot_be_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller who cannot decrypt the agent receives a directional diagnosis."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_session._KEY_DISTRIBUTION_RETRY_DELAYS_S", (0.0,))
    monkeypatch.setattr("mindroom.matrix_rtc.call_session._E2EE_READY_TIMEOUT_S", 60.0)
    notices: list[str] = []

    async def on_failure(message: str) -> None:
        notices.append(message)

    transport = RecoveringKeyTransport()
    session = _session(_client(), FakeBridge(), transport, [1_000])
    session.deps.on_failure = on_failure
    await session.start([_member("@alice:example.org", "ALICEDEV")])
    for _ in range(40):
        if notices:
            break
        await asyncio.sleep(0)

    assert len(notices) == 1
    assert "you will not hear its audio" in notices[0]
    assert "ALICEDEV" in notices[0]
    assert "one-time-key" in notices[0]
    await session.stop()


@pytest.mark.asyncio
async def test_session_reports_only_device_with_undelivered_outbound_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The outbound failure notice excludes devices that received the key."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_session._KEY_DISTRIBUTION_RETRY_DELAYS_S", (0.0,))
    monkeypatch.setattr("mindroom.matrix_rtc.call_session._E2EE_READY_TIMEOUT_S", 60.0)
    notices: list[str] = []

    async def on_failure(message: str) -> None:
        notices.append(message)

    class PhoneOnlyKeyTransport(FakeKeyTransport):
        async def send_key(self, **kwargs: object) -> list[CallMember]:
            await super().send_key(**kwargs)  # type: ignore[arg-type]
            targets = cast("list[CallMember]", kwargs["targets"])
            return [target for target in targets if target.device_id == "ALICEPHONE"]

    alice_phone = _member("@alice:example.org", "ALICEPHONE")
    alice_tablet = _member("@alice:example.org", "ALICETABLET")
    session = _session(_client(), FakeBridge(), PhoneOnlyKeyTransport(), [1_000])
    session.deps.on_failure = on_failure
    await session.start([alice_phone, alice_tablet])
    for _ in range(40):
        if notices:
            break
        await asyncio.sleep(0)

    assert len(notices) == 1
    assert "ALICETABLET" in notices[0]
    assert "ALICEPHONE" not in notices[0]
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
                    received_at_ms=1_000,
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
            received_at_ms=1_500,
        ),
    )

    assert accepted is True
    assert bridge.frame_keys == [("@alice:example.org:ALICEDEV", b"A" * 16, 2)]
    await session.stop()


@pytest.mark.asyncio
async def test_inbound_key_immediately_retries_undelivered_outbound_key() -> None:
    """Inbound Olm traffic wakes outbound sharing after its retry budget ended."""
    transport = RecoveringKeyTransport()
    session = _session(_client(), FakeBridge(), transport, [1_000])
    alice = _member("@alice:example.org", "ALICEDEV")
    # Model a call whose bounded retry budget was already exhausted before the
    # peer's first Olm-encrypted key established the bidirectional session.
    session._key_retry_attempt = 3
    await session.start([alice])
    assert len(transport.sent) == 1

    transport.available = True
    accepted = session.on_key_received(
        ReceivedFrameKey(
            user_id=alice.user_id,
            claimed_device_id=alice.device_id,
            key_base64="QUFBQUFBQUFBQUFBQUFBQQ==",
            key_index=2,
            received_at_ms=1_500,
        ),
    )
    await asyncio.wait_for(transport.delivered.wait(), timeout=1)

    assert accepted is True
    assert len(transport.sent) == 2
    assert transport.sent[1]["targets"] == [alice]
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
            received_at_ms=1_500,
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
    tool_kwargs: dict[str, object] = {}

    async def fake_build_call_tools(**kwargs: object) -> CallAgentTooling:
        tool_kwargs.update(kwargs)
        return CallAgentTooling(
            tools=(sentinel_tool,),
            instructions="CHAT PROMPT",
            execution_identity=_call_execution_identity_from_tool_kwargs(kwargs),
        )

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
    assert tool_kwargs["requester_id"] == "@alice:example.org"


@pytest.mark.asyncio
async def test_manager_skips_call_when_same_agent_materialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A broken same-agent surface cannot become a generic or tool-less voice bot."""

    async def fail_tools(**_kwargs: object) -> CallAgentTooling:
        msg = "prompt failed"
        raise ValueError(msg)

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.build_call_tools", fail_tools)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert bridge.connected_grant is None
    assert bridge.agent_options is None
    assert manager._sessions == {}
    assert manager._retry_tasks == {}
    assert manager._retry_attempts == {}


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
async def test_manager_replays_key_after_transient_admission_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A one-shot to-device key survives a transient roster-fetch failure."""

    async def send_key(_self: object, *, targets: list[CallMember], **_kwargs: object) -> list[CallMember]:
        return targets

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.ToDeviceFrameKeyTransport.send_key", send_key)
    client = _client()
    client.rooms = {ROOM_ID: _room(encrypted=True)}
    message = "offline"
    client.room_get_state.side_effect = [
        aiohttp.ClientError(message),
        _state_response(_remote_member_event()),
    ]
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_to_device_event(_frame_key_event())
    assert ROOM_ID in manager._pending_keys
    await manager.reconcile_joined_rooms()

    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) in bridge.frame_keys
    assert manager._pending_keys == {}
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_ignores_plaintext_custom_key_event(tmp_path: Path) -> None:
    """Only custom events carrying authenticated Olm provenance reach key intake."""
    authenticated = _frame_key_event()
    raw = nio.UnknownToDeviceEvent.from_dict(authenticated.source)
    assert isinstance(raw, nio.UnknownToDeviceEvent)
    client = _client()
    client.rooms = {ROOM_ID: _room(encrypted=True)}
    manager = _manager(client, FakeBridge(), tmp_path)

    await manager.on_to_device_event(raw)

    assert manager._pending_keys == {}
    client.room_get_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_manager_replays_a_key_received_before_active_roster_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unmatched key survives stale federation state until its device appears."""

    async def send_key(_self: object, *, targets: list[CallMember], **_kwargs: object) -> list[CallMember]:
        return targets

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.ToDeviceFrameKeyTransport.send_key", send_key)
    client = _client()
    room = _room(encrypted=True)
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(room, _member_unknown_event())

    await manager.on_to_device_event(
        _frame_key_event(user_id="@alice:example.org", device_id="ALICESECOND"),
    )

    assert ROOM_ID in manager._pending_keys
    assert ("@alice:example.org:ALICESECOND", b"A" * 16, 2) not in bridge.frame_keys

    client.room_get_state.return_value = _state_response(
        _remote_member_event(),
        _remote_member_event(user="@alice:example.org", device="ALICESECOND"),
    )
    await manager.on_room_event(room, _member_unknown_event())

    assert ("@alice:example.org:ALICESECOND", b"A" * 16, 2) in bridge.frame_keys
    assert manager._pending_keys == {}


@pytest.mark.asyncio
async def test_manager_accepts_key_for_alias_only_configured_room(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """To-device admission uses the cached room alias just like room events."""

    async def send_key(_self: object, *, targets: list[CallMember], **_kwargs: object) -> list[CallMember]:
        return targets

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.ToDeviceFrameKeyTransport.send_key", send_key)
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper", rooms=["#voice:example.org"])},
        models={},
        authorization=AuthorizationConfig(global_users=["@alice:example.org"]),
        calls=_realtime_calls(),
    )
    room = _room(encrypted=True)
    room.canonical_alias = "#voice:example.org"
    client = _client()
    client.rooms = {ROOM_ID: room}
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path, config)

    await manager.on_to_device_event(_frame_key_event())

    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) in bridge.frame_keys
    assert manager._pending_keys == {}
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_expires_pending_key_from_device_outside_roster(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An unmatched key stays bounded briefly, then expires without a retry loop."""

    async def send_key(_self: object, *, targets: list[CallMember], **_kwargs: object) -> list[CallMember]:
        return targets

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.ToDeviceFrameKeyTransport.send_key", send_key)
    client = _client()
    client.rooms = {ROOM_ID: _room(encrypted=True)}
    client.room_get_state.return_value = _state_response(_remote_member_event(device="DIFFERENTDEV"))
    bridge = FakeBridge()
    clock = [1_000]
    manager = _manager(client, bridge, tmp_path, clock_ms=lambda: clock[0])

    await manager.on_to_device_event(_frame_key_event())

    assert ROOM_ID in manager._pending_keys
    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) not in bridge.frame_keys
    assert manager._retry_tasks == {}

    clock[0] += _PENDING_KEY_TTL_MS + 1
    await manager.on_room_event(_room(encrypted=True), _member_unknown_event())

    assert manager._pending_keys == {}
    await manager.shutdown()


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
                received_at_ms=index,
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
        received_at_ms=999,
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

    key_task = asyncio.create_task(manager.on_to_device_event(_frame_key_event()))
    for _ in range(20):
        if ROOM_ID in manager._pending_keys:
            break
        await asyncio.sleep(0)
    assert ROOM_ID in manager._pending_keys
    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) not in bridge.frame_keys

    release_agent.set()
    await asyncio.gather(join_task, key_task)

    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) in bridge.frame_keys
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_key_admission_uses_one_authoritative_state_fetch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Key admission reuses normal reconciliation instead of fetching state twice."""

    async def send_key(_self: object, *, targets: list[CallMember], **_kwargs: object) -> list[CallMember]:
        return targets

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.ToDeviceFrameKeyTransport.send_key", send_key)
    client = _client()
    room = _room(encrypted=True)
    client.rooms = {ROOM_ID: room}
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    client.room_get_state.return_value = _state_response(_remote_member_event())

    await manager.on_to_device_event(_frame_key_event())

    client.room_get_state.assert_awaited_once_with(ROOM_ID)
    assert ("@alice:example.org:ALICEDEV", b"A" * 16, 2) in bridge.frame_keys
    assert manager._pending_keys == {}
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_rejects_participant_selected_remote_focus(
    tmp_path: Path,
) -> None:
    """A server-hosted agent never dials a participant-selected remote focus."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=_realtime_calls(),
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

    assert await manager._resolve_service(members) is None


@pytest.mark.asyncio
async def test_manager_rejects_unconfigured_same_server_focus(
    tmp_path: Path,
) -> None:
    """A local participant cannot redirect the server-hosted agent to another focus."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=_realtime_calls(),
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, config)
    member = _member(
        "@alice:example.org",
        "ALICEDEV",
        livekit_service_url="https://rtc.founder.example",
    )

    assert await manager._resolve_service([member]) is None


@pytest.mark.asyncio
async def test_manager_does_not_poll_untrusted_focus(tmp_path: Path) -> None:
    """An untrusted advertisement waits for a new room event instead of polling forever."""
    client = _client()
    client.room_get_state.return_value = _state_response(
        _remote_member_event(livekit_service_url="https://rtc.attacker.example"),
    )
    manager = _manager(client, FakeBridge(), tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert manager._sessions == {}
    assert manager._retry_tasks == {}
    assert manager._retry_attempts == {}


@pytest.mark.asyncio
async def test_manager_retries_transient_focus_discovery_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A temporary well-known outage retains the bounded reconciliation retry."""

    async def fail_discovery(*_args: object, **_kwargs: object) -> None:
        message = "offline"
        raise httpx.ConnectError(message)

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.discover_livekit_service_url", fail_discovery)
    config = _config()
    config.calls.livekit_service_url = None
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    manager = _manager(client, FakeBridge(), tmp_path, config)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert ROOM_ID in manager._retry_tasks
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_does_not_retry_invalid_sfu_grant(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Malformed authorization-service output waits for new state instead of polling."""

    async def invalid_grant(*_args: object, **_kwargs: object) -> SfuGrant:
        message = "invalid grant"
        raise ValueError(message)

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.request_sfu_grant", invalid_grant)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    manager = _manager(client, FakeBridge(), tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())

    assert manager._sessions == {}
    assert manager._retry_tasks == {}
    assert manager._retry_attempts == {}


@pytest.mark.asyncio
async def test_manager_recovers_inherited_focus_after_founder_leaves(tmp_path: Path) -> None:
    """A remote follower may inherit and advertise the trusted local focus."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=_realtime_calls(),
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, config)
    follower = _member(
        "@follower:follower.example",
        "FOLLOWER",
        livekit_service_url=SERVICE_URL,
    )

    service = await manager._resolve_service([follower])

    assert service == SERVICE_URL


@pytest.mark.asyncio
async def test_active_session_keeps_pinned_focus_after_founder_leaves(tmp_path: Path) -> None:
    """A follower's advertisement cannot tear down an already trusted session."""
    now_ms = int(time.time() * 1000)
    founder = _remote_member_event(device="FOUNDER", created_ts=now_ms)
    follower = _remote_member_event(device="FOLLOWER", created_ts=now_ms + 1)
    client = _client()
    client.room_get_state.return_value = _state_response(founder, follower)
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    room = _room()

    await manager.on_room_event(room, _member_unknown_event())
    client.room_get_state.return_value = _state_response(
        _remote_member_event(
            device="FOLLOWER",
            created_ts=now_ms + 1,
            livekit_service_url="https://rtc.remote.example",
        ),
    )
    await manager.on_room_event(room, _member_unknown_event())

    assert ROOM_ID in manager._sessions
    assert not bridge.closed
    assert bridge.participant_rosters[-1] == frozenset({"@alice:example.org:FOLLOWER"})
    await manager.shutdown()


@pytest.mark.asyncio
async def test_manager_fetches_grant_with_local_network_trust(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The operator-selected authorization service may resolve to local infrastructure."""
    seen: dict[str, object] = {}

    async def capture_grant(service_url: str, **kwargs: object) -> SfuGrant:
        seen["service_url"] = service_url
        seen.update(kwargs)
        return GRANT

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.request_sfu_grant", capture_grant)
    manager = _manager(_client(), FakeBridge(), tmp_path)

    assert await manager._fetch_grant(ROOM_ID, SERVICE_URL) == GRANT
    assert seen["service_url"] == SERVICE_URL
    assert seen["allow_private_networks"] is True


@pytest.mark.asyncio
async def test_manager_rejects_insecure_remote_focus(tmp_path: Path) -> None:
    """A remote plaintext focus cannot receive the bot's OpenID token."""
    config = Config(
        agents={"helper": AgentConfig(display_name="Helper")},
        models={},
        calls=_realtime_calls(livekit_service_url=None),
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
async def test_shutdown_drains_retry_during_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A retry remains tracked while its homeserver reconciliation is in flight."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager._RECONCILE_RETRY_DELAYS_S", (0.0,))
    entered_retry = asyncio.Event()
    retry_cancelled = asyncio.Event()
    calls = 0

    async def fetch_state(_room_id: str) -> nio.RoomGetStateResponse | nio.RoomGetStateError:
        nonlocal calls
        calls += 1
        if calls == 1:
            return nio.RoomGetStateError("503 upstream sad")
        entered_retry.set()
        try:
            await asyncio.Event().wait()
        finally:
            retry_cancelled.set()
        return _state_response()

    client = _client()
    client.room_get_state.side_effect = fetch_state
    manager = _manager(client, FakeBridge(), tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())
    await entered_retry.wait()
    assert ROOM_ID not in manager._retry_tasks
    assert manager._background_tasks

    await manager.shutdown()

    assert retry_cancelled.is_set()
    assert manager._retry_tasks == {}
    assert manager._background_tasks == set()


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
    assert ROOM_ID in manager._retry_tasks
    await manager.shutdown()


@pytest.mark.asyncio
async def test_call_events_cannot_bypass_pending_join_backoff(tmp_path: Path) -> None:
    """Sync echoes and membership refreshes must not bypass join backoff."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    manager = _manager(client, FakeBridge(), tmp_path)
    join = AsyncMock(return_value="retry")
    manager._join = join  # type: ignore[method-assign]

    await manager.on_room_event(_room(), _member_unknown_event())
    assert join.await_count == 1
    assert ROOM_ID in manager._retry_tasks

    retry_task = manager._retry_tasks.pop(ROOM_ID)
    retry_task.cancel()
    await asyncio.gather(retry_task, return_exceptions=True)
    await manager.on_room_event(_room(), _member_unknown_event())

    assert join.await_count == 1
    await manager.shutdown()


@pytest.mark.asyncio
async def test_cascaded_retries_reuse_logical_call_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Media reconnects retain chat history until the remote call empties."""
    monkeypatch.setattr("mindroom.matrix_rtc.call_manager._RECONCILE_RETRY_DELAYS_S", (0.0,))
    session_ids: list[str] = []

    async def fake_tools(**kwargs: object) -> CallAgentTooling:
        session_ids.append(cast("str", kwargs["session_id"]))
        return CallAgentTooling(
            tools=(),
            instructions="",
            execution_identity=_call_execution_identity_from_tool_kwargs(kwargs),
        )

    monkeypatch.setattr("mindroom.matrix_rtc.call_manager.build_call_tools", fake_tools)
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    manager = _manager(client, FakeBridge(), tmp_path, _cascaded_config())
    results = iter(("retry", "joined", "joined"))

    async def fake_join(room: nio.MatrixRoom, members: list[CallMember]) -> str:
        await manager._build_tooling(room.room_id, requester_id=members[0].user_id, cascaded=True)
        return next(results)

    manager._join = fake_join  # type: ignore[method-assign]

    await manager.on_room_event(_room(), _member_unknown_event())
    for _ in range(20):
        if len(session_ids) == 2:
            break
        await asyncio.sleep(0)

    assert len(session_ids) == 2
    assert session_ids[0] == session_ids[1]

    client.room_get_state.return_value = _state_response()
    await manager.on_room_event(_room(), _member_unknown_event())
    client.room_get_state.return_value = _state_response(_remote_member_event())
    await manager.on_room_event(_room(), _member_unknown_event())

    assert session_ids[2] != session_ids[0]
    await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable", [True, False])
async def test_terminal_voice_close_stops_session_and_retries_only_when_allowed(
    tmp_path: Path,
    retryable: bool,
) -> None:
    """A terminal voice close cannot leave live Matrix membership behind."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.agent_options is not None
    assert bridge.agent_options.on_session_terminated is not None

    bridge.agent_options.on_session_terminated(retryable)
    await asyncio.gather(*list(manager._background_tasks))

    assert bridge.closed
    assert manager._sessions == {}
    assert (ROOM_ID in manager._retry_tasks) is retryable
    await manager.shutdown()


@pytest.mark.asyncio
async def test_voice_runtime_error_is_posted_as_actionable_room_notice(tmp_path: Path) -> None:
    """A connected-but-broken voice provider cannot fail as silent audio."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    client.room_send.return_value = nio.RoomSendResponse("$notice", ROOM_ID)
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.agent_options is not None
    assert bridge.agent_options.on_session_error is not None

    notice = (
        "Voice call error: OpenAI Realtime rejected the configured credential. "
        "Update it, restart MindRoom, and rejoin the call."
    )
    bridge.agent_options.on_session_error(notice)
    await asyncio.gather(*list(manager._background_tasks))

    client.room_send.assert_awaited_once_with(
        ROOM_ID,
        message_type="m.room.message",
        content={
            "msgtype": "m.notice",
            "body": notice,
            "chat.mindroom.call_failure": {"version": 1},
        },
        ignore_unverified_devices=True,
    )
    await manager.shutdown()


@pytest.mark.asyncio
async def test_nonretryable_terminal_close_stays_quarantined_until_next_call(tmp_path: Path) -> None:
    """The bot's own membership clear echo cannot reopen a terminal call."""
    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = FakeBridge()
    manager = _manager(client, bridge, tmp_path)
    await manager.on_room_event(_room(), _member_unknown_event())
    assert bridge.agent_options is not None
    assert bridge.agent_options.on_session_terminated is not None

    bridge.agent_options.on_session_terminated(False)
    await asyncio.gather(*list(manager._background_tasks))
    await manager.on_room_event(_room(), _member_unknown_event())

    assert manager._sessions == {}
    assert manager._logical_calls[ROOM_ID].join_blocked

    client.room_get_state.return_value = _state_response()
    await manager.on_room_event(_room(), _member_unknown_event())
    client.room_get_state.return_value = _state_response(_remote_member_event())
    await manager.on_room_event(_room(), _member_unknown_event())

    assert ROOM_ID in manager._sessions
    await manager.shutdown()


@pytest.mark.asyncio
async def test_terminal_close_during_agent_start_cannot_leave_ghost_session(tmp_path: Path) -> None:
    """A close callback racing with join registration still removes the new session."""

    class ClosingBridge(FakeBridge):
        async def start_agent(self, options: CallVoiceAgentOptions) -> None:
            await super().start_agent(options)
            assert options.on_session_terminated is not None
            options.on_session_terminated(True)

    client = _client()
    client.room_get_state.return_value = _state_response(_remote_member_event())
    bridge = ClosingBridge()
    manager = _manager(client, bridge, tmp_path)

    await manager.on_room_event(_room(), _member_unknown_event())
    await asyncio.gather(*list(manager._background_tasks))

    assert bridge.closed
    assert manager._sessions == {}
    assert ROOM_ID in manager._retry_tasks
    await manager.shutdown()


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
        requester_id="@alice:example.org",
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
async def test_grant_failure_closes_bridge_and_finalizes() -> None:
    """A pre-connect grant failure still runs the full session cleanup."""
    client = _client()
    bridge = FakeBridge()
    finalized: list[bool] = []

    async def fetch_grant() -> SfuGrant:
        message = "grant unavailable"
        raise aiohttp.ClientError(message)

    async def on_stopped() -> None:
        finalized.append(True)

    session = _plain_session(client, bridge, on_stopped=on_stopped)
    session.deps.fetch_grant = fetch_grant

    with pytest.raises(aiohttp.ClientError, match="grant unavailable"):
        await session.start([_member("@alice:example.org", "ALICEDEV")])

    assert bridge.closed
    assert finalized == [True]


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
async def test_stop_cleanup_survives_caller_cancellation() -> None:
    """Cancelling one stop waiter cannot strand partially closed call state."""
    client = _client()
    bridge = FakeBridge()
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    finalized: list[bool] = []

    async def blocking_close() -> None:
        close_started.set()
        await release_close.wait()
        bridge.closed = True

    async def on_stopped() -> None:
        finalized.append(True)

    bridge.aclose = blocking_close  # type: ignore[method-assign]
    session = _plain_session(client, bridge, on_stopped=on_stopped)
    first_waiter = asyncio.create_task(session.stop())
    await close_started.wait()

    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first_waiter

    release_close.set()
    await session.stop()

    assert bridge.closed
    assert finalized == [True]
    assert session._stop_task is not None
    assert session._stop_task.done()


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
async def test_key_distribution_retry_survives_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient homeserver failure cannot terminate the bounded retry chain."""

    class FlakyTransport(FakeKeyTransport):
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
                return []
            if len(self.sent) == 2:
                message = "offline"
                raise aiohttp.ClientError(message)
            return targets

    real_sleep = asyncio.sleep

    async def immediate_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("mindroom.matrix_rtc.call_session.asyncio.sleep", immediate_sleep)
    transport = FlakyTransport()
    session = _session(_client(), FakeBridge(), transport, [1_000])
    session._members = [_member("@alice:example.org", "ALICEDEV")]

    await session._distribute_keys()
    for _ in range(20):
        if len(transport.sent) == 3:
            break
        await real_sleep(0)

    assert len(transport.sent) == 3
    await session.stop()


@pytest.mark.asyncio
async def test_partial_key_send_failure_still_rotates_after_exposed_member_leaves() -> None:
    """A transport error cannot hide an earlier recipient from leave rotation."""

    class PartialFailureTransport(FakeKeyTransport):
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
                message = "failed after first recipient"
                raise aiohttp.ClientError(message)
            return targets

    transport = PartialFailureTransport()
    session = _session(_client(), FakeBridge(), transport, [1_000])
    alice = _member("@alice:example.org", "ALICEDEV")
    bob = _member("@bob:example.org", "BOBDEV")
    session._members = [alice, bob]

    with pytest.raises(aiohttp.ClientError):
        await session._distribute_keys()
    await session.on_members_changed([bob])

    assert [send["key_index"] for send in transport.sent] == [0, 1]
    assert transport.sent[1]["targets"] == [bob]
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


@pytest.mark.asyncio
async def test_rapid_roster_changes_preserve_key_activation_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending key stays delayed and is skipped if another rotation supersedes it."""
    release_delays = asyncio.Event()
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def controlled_sleep(seconds: float) -> None:
        delays.append(seconds)
        await release_delays.wait()

    monkeypatch.setattr("mindroom.matrix_rtc.call_session.asyncio.sleep", controlled_sleep)
    bridge = FakeBridge()
    clock = [0]
    session = _session(_client(), bridge, FakeKeyTransport(), clock)
    alice = _member("@alice:example.org", "ALICEDEV")
    bob = _member("@bob:example.org", "BOBDEV")
    charlie = _member("@charlie:example.org", "CHARLIEDEV")

    session._members = [alice]
    await session._distribute_keys()
    clock[0] = 10_001
    session._members = [alice, bob]
    await session._distribute_keys()
    await real_sleep(0)
    clock[0] = 10_501
    session._members = [alice, bob, charlie]
    await session._distribute_keys()
    await real_sleep(0)
    clock[0] = 10_502
    session._members = [alice, charlie]
    await session._distribute_keys()
    await real_sleep(0)

    assert [key_index for _, _, key_index in bridge.frame_keys] == [0]
    assert delays == [1.0, 0.5, 1.0]
    release_delays.set()
    for _ in range(5):
        await real_sleep(0)

    assert [key_index for _, _, key_index in bridge.frame_keys] == [0, 2]
    await session.stop()


def test_calls_config_rejects_unknown_agents() -> None:
    """Call configuration may reference only declared agents."""
    with pytest.raises(ValueError, match=r"calls\.agents references unknown agent"):
        Config(
            models={},
            calls=CallsConfig(
                enabled=True,
                profiles={
                    "voice": RealtimeCallProfile(
                        backend="realtime",
                        model="gpt-realtime",
                        credentials_service="openai",
                        voice="marin",
                    ),
                },
                agents={"missing": "voice"},
            ),
        )


def test_calls_config_rejects_unknown_profiles() -> None:
    """Every calls-enabled agent must select a defined profile."""
    with pytest.raises(ValueError, match=r"calls\.agents references unknown profile.*missing"):
        CallsConfig(agents={"helper": "missing"})


def test_calls_config_validates_credentials_service() -> None:
    """Call credential bindings use safe normalized service names."""
    assert (
        RealtimeCallProfile(
            backend="realtime",
            model="gpt-realtime",
            credentials_service=" openai-realtime ",
            voice="marin",
        ).credentials_service
        == "openai-realtime"
    )
    with pytest.raises(ValueError, match="Service name can only include"):
        RealtimeCallProfile(
            backend="realtime",
            model="gpt-realtime",
            credentials_service="../openai",
            voice="marin",
        )


def test_speech_config_validates_credentials_service() -> None:
    """Speech credential bindings use safe normalized service names."""
    assert (
        SpeechServiceConfig(model="gpt-4o-mini-tts", credentials_service=" openai-speech ").credentials_service
        == "openai-speech"
    )
    with pytest.raises(ValueError, match="Service name can only include"):
        SpeechServiceConfig(model="gpt-4o-mini-tts", credentials_service="../openai")


@pytest.mark.parametrize("private_scope", ["user", "user_agent"])
def test_calls_config_accepts_requester_private_agents(private_scope: Literal["user", "user_agent"]) -> None:
    """Both requester-private partition modes may opt into voice calls."""
    config = Config(
        models={},
        agents={
            "private": AgentConfig(
                display_name="Private",
                private=AgentPrivateConfig(per=private_scope),
            ),
        },
        calls=CallsConfig(
            enabled=True,
            profiles={
                "voice": RealtimeCallProfile(
                    backend="realtime",
                    model="gpt-realtime",
                    credentials_service="openai",
                    voice="marin",
                ),
            },
            agents={"private": "voice"},
        ),
    )

    assert set(config.calls.agents) == {"private"}


def test_calls_config_rejects_agents_sharing_a_room() -> None:
    """Two call agents cannot both join the same configured room."""
    with pytest.raises(ValueError, match=r"calls\.agents configures multiple agents for room"):
        Config(
            models={},
            agents={
                "one": AgentConfig(display_name="One", rooms=["voice"]),
                "two": AgentConfig(display_name="Two", rooms=["voice"]),
            },
            calls=CallsConfig(
                enabled=True,
                profiles={
                    "voice": RealtimeCallProfile(
                        backend="realtime",
                        model="gpt-realtime",
                        credentials_service="openai",
                        voice="marin",
                    ),
                },
                agents={"one": "voice", "two": "voice"},
            ),
        )


def test_cascaded_calls_require_both_speech_services() -> None:
    """The discriminated cascaded profile requires both speech legs."""
    stt = SpeechServiceConfig(model="gpt-4o-transcribe", credentials_service="openai")
    tts = SpeechServiceConfig(model="tts-1", credentials_service="openai")

    config = CallsConfig(
        profiles={"voice": CascadedCallProfile(backend="cascaded", stt=stt, tts=tts)},
        agents={"helper": "voice"},
    )

    resolved = config.resolve_agent_config("helper")
    assert resolved.backend == "cascaded"
    assert resolved.stt == stt
    assert resolved.tts == tts

    with pytest.raises(ValueError, match=r"profiles\.voice\.cascaded\.tts|Field required"):
        CallsConfig(
            profiles={"voice": {"backend": "cascaded", "stt": stt}},  # type: ignore[dict-item]
            agents={"helper": "voice"},
        )
    with pytest.raises(ValueError, match=r"profiles\.voice\.cascaded\.stt|Field required"):
        CallsConfig(
            profiles={"voice": {"backend": "cascaded", "tts": tts}},  # type: ignore[dict-item]
            agents={"helper": "voice"},
        )


def test_cascaded_calls_accept_optional_model_override() -> None:
    """A cascaded profile can select a configured model for its agent turns."""
    stt = SpeechServiceConfig(model="gpt-4o-transcribe", credentials_service="openai")
    tts = SpeechServiceConfig(model="tts-1", credentials_service="openai")
    config = Config(
        models={"call_fast": ModelConfig(provider="anthropic", id="claude-haiku-4-5")},
        agents={"helper": AgentConfig(display_name="Helper")},
        calls=CallsConfig(
            profiles={
                "voice": CascadedCallProfile(
                    backend="cascaded",
                    model="call_fast",
                    stt=stt,
                    tts=tts,
                ),
            },
            agents={"helper": "voice"},
        ),
    )

    resolved = config.calls.resolve_agent_config("helper")
    assert isinstance(resolved, CascadedCallProfile)
    assert resolved.model == "call_fast"


def test_calls_config_rejects_unknown_cascaded_model() -> None:
    """Call model aliases must exist in the top-level model catalog."""
    stt = SpeechServiceConfig(model="gpt-4o-transcribe", credentials_service="openai")
    tts = SpeechServiceConfig(model="tts-1", credentials_service="openai")

    with pytest.raises(ValueError, match=r"voice -> missing"):
        Config(
            models={},
            agents={"helper": AgentConfig(display_name="Helper")},
            calls=CallsConfig(
                profiles={
                    "voice": CascadedCallProfile(
                        backend="cascaded",
                        model="missing",
                        stt=stt,
                        tts=tts,
                    ),
                },
                agents={"helper": "voice"},
            ),
        )


def test_openai_compatible_speech_config_requires_endpoint() -> None:
    """A compatible provider cannot silently fall through to OpenAI cloud."""
    with pytest.raises(ValueError, match="require host"):
        SpeechServiceConfig(provider="openai_compatible", model="whisper-large-v3")


@pytest.mark.parametrize("host", ["localhost:9000", "ftp://stt.example.test"])
def test_speech_config_rejects_unsafe_endpoint(host: str) -> None:
    """Non-HTTP endpoints cannot turn a local config into a cloud call."""
    with pytest.raises(ValueError, match=r"HTTP\(S\) URL"):
        SpeechServiceConfig(provider="openai_compatible", model="whisper-large-v3", host=host)


def test_speech_config_normalizes_blank_optional_fields() -> None:
    """Blank form values use the same fallback behavior as omitted fields."""
    service = SpeechServiceConfig(
        provider="openai",
        model="gpt-4o-transcribe",
        credentials_service="openai",
        host=" ",
        api_key="",
    )

    assert service.host is None
    assert service.api_key is None


def test_compatible_speech_blank_key_uses_local_placeholder(tmp_path: Path) -> None:
    """A blank local key cannot suppress the non-secret compatibility placeholder."""
    manager = _manager(_client(), FakeBridge(), tmp_path, _cascaded_config(local=True))

    service = manager._resolve_speech_service(
        SpeechServiceConfig(
            provider="openai_compatible",
            model="whisper-large-v3",
            host="http://127.0.0.1:9000",
            api_key=" ",
        ),
        component="stt",
        room_id=ROOM_ID,
    )

    assert service is not None
    assert service.api_key == LOCAL_OPENAI_API_KEY_DEFAULT


def test_speech_config_rejects_connection_fields_in_extra_kwargs() -> None:
    """Typed connection fields cannot be ambiguously overridden by provider options."""
    with pytest.raises(ValueError, match="must not redefine: api_key, base_url, client"):
        SpeechServiceConfig(
            model="gpt-4o-transcribe",
            extra_kwargs={"api_key": "wrong", "base_url": "https://wrong.example", "client": "wrong"},
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
                "calls": {
                    "enabled": True,
                    "profiles": {
                        "voice": {
                            "backend": "realtime",
                            "model": "gpt-realtime",
                            "credentials_service": "openai",
                            "voice": "marin",
                        },
                    },
                    "agents": {"one": "voice", "two": "voice"},
                },
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
                "calls": {
                    "enabled": True,
                    "profiles": {
                        "voice": {
                            "backend": "realtime",
                            "model": "gpt-realtime",
                            "credentials_service": "openai",
                            "voice": "marin",
                        },
                    },
                    "agents": {"one": "voice", "two": "voice"},
                },
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
        calls=CallsConfig(
            enabled=True,
            profiles={
                "voice": RealtimeCallProfile(
                    backend="realtime",
                    model="gpt-realtime",
                    credentials_service="openai",
                    voice="marin",
                ),
            },
            agents={"helper": "voice", "other": "voice"},
        ),
    )
    manager = _manager(_client(), FakeBridge(), tmp_path, config)
    room = _room()
    room.canonical_alias = "#voice:example.org"

    assert not manager._is_configured_call_room(room)
