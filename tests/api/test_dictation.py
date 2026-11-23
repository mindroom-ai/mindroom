"""Tests for the streaming composer dictation API (``/api/dictation/v1/*``)."""

from __future__ import annotations

import asyncio
import io
import json
import re
import sqlite3
import tempfile
import wave
from contextlib import closing
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import starlette.formparsers
from fastapi import HTTPException
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import (
    AgentReplyMembershipIndex,
    _agent_reply_membership_policy_signature,
    _AgentReplyMembershipSnapshot,
    _GrantRoomMembership,
)
from mindroom.api import main
from mindroom.api.app_state import app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.api.dictation import DictationLimiter, close_dictation_http_client
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.config.voice import VoiceConfig, VoiceSTTConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.matrix.identity import MatrixID
from mindroom.matrix_identifiers import agent_username_localpart
from mindroom.requester_identity import runtime_matrix_domain
from mindroom.voice import handler as voice_handler
from tests.api.conftest import publish_api_config
from tests.runtime_fakes import bind_test_embedded_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from starlette.types import Message

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

CHAT = "https://chat.example.org"
PREFIX = "/api/dictation/v1"
ADMIN = "@admin:localhost"
ALICE = "@alice:localhost"
TEAM_USER = "@teammate:localhost"
GUEST = "@guest:localhost"
OUTSIDER = "@outsider:localhost"
ROOM = "!room:localhost"
GRANT_ROOM_KEY = "grant"
GRANT_ROOM_ID = "!grant:localhost"
_FORM_FIELD = re.compile(rb'Content-Disposition: form-data; name="([^"]+)"\r\n\r\n([^\r]*)\r\n')


def _wav(seconds: float = 1.5, *, rate: int = 16_000, channels: int = 1, sample_width: int = 2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(rate)
        writer.writeframes(b"\x00" * (int(seconds * rate) * channels * sample_width))
    return buf.getvalue()


def _form_fields(request: httpx.Request) -> dict[str, str]:
    return {name.decode(): value.decode() for name, value in _FORM_FIELD.findall(request.read())}


def _refusal(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert isinstance(body["error"], str)
    assert body["error"]


@dataclass
class _Clock:
    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _seed_memberships(
    index: AgentReplyMembershipIndex,
    config: Config,
    rooms: list[tuple[str | None, str, set[str]]],
) -> None:
    rows = tuple(
        _GrantRoomMembership(
            room_key=room_key,
            room_id=room_id,
            observer_user_id="@mindroom_router:localhost",
            raw_joined_user_ids=frozenset(members),
            ready=True,
        )
        for room_key, room_id, members in rooms
    )
    index._publish(
        _AgentReplyMembershipSnapshot(
            policy_signature=_agent_reply_membership_policy_signature(config),
            rooms=rows,
            possible_joined_room_ids=frozenset(room_id for _, room_id, _ in rooms),
        ),
    )


@dataclass
class DictationHarness:
    """MindRoom API app wired with client sessions, CORS, controllable STT client, clock, and memberships."""

    client: httpx.AsyncClient
    runtime_paths: RuntimePaths
    storage_path: Path
    clock: _Clock
    limiter: DictationLimiter
    stt_client: httpx.AsyncClient
    memberships: AgentReplyMembershipIndex
    room_roster: dict[str, dict[str, str | None]] = field(default_factory=dict)
    stt_requests: list[httpx.Request] = field(default_factory=list)
    stt_responder: Callable[[httpx.Request], httpx.Response] = field(
        default_factory=lambda: lambda _req: httpx.Response(200, json={"text": "hello world"}),
    )
    stt_gate: asyncio.Event | None = None
    stt_arrived: asyncio.Condition = field(default_factory=asyncio.Condition)
    refresh_calls: int = 0
    on_refresh: Callable[[], None] | None = None

    def headers(self, user_id: str, *, origin: str | None = CHAT) -> dict[str, str]:
        """Create a client session for ``user_id`` and return bearer + origin headers."""
        token = client_session_store(main.app).create(user_id).token
        hdrs = {"Authorization": f"Bearer {token}"}
        if origin is not None:
            hdrs["Origin"] = origin
        return hdrs

    def publish(self, config: Config) -> None:
        """Publish ``config`` onto the live API app snapshot."""
        publish_api_config(main.app, config)

    async def status(self, user_id: str, *, room_id: str | None = None, origin: str | None = CHAT) -> httpx.Response:
        """Call ``GET /api/dictation/v1/status`` as ``user_id``."""
        params = {} if room_id is None else {"room_id": room_id}
        return await self.client.get(f"{PREFIX}/status", headers=self.headers(user_id, origin=origin), params=params)

    async def wait_for_stt_requests(self, count: int) -> None:
        """Wait until at least ``count`` STT requests have reached the mock transport."""
        async with self.stt_arrived:
            await self.stt_arrived.wait_for(lambda: len(self.stt_requests) >= count)

    async def transcribe(
        self,
        user_id: str,
        *,
        audio: bytes | None = None,
        prompt: str | None = None,
        language: str | None = None,
        room_id: str | None = None,
        origin: str | None = CHAT,
    ) -> httpx.Response:
        """Call ``POST /api/dictation/v1/transcribe`` as ``user_id``."""
        files = {} if audio is None else {"audio": ("segment.wav", audio, "audio/wav")}
        data: dict[str, str] = {}
        if prompt is not None:
            data["prompt"] = prompt
        if language is not None:
            data["language"] = language
        if room_id is not None:
            data["room_id"] = room_id
        return await self.client.post(
            f"{PREFIX}/transcribe",
            headers=self.headers(user_id, origin=origin),
            files=files or {"unused": ("empty.txt", b"", "text/plain")},
            data=data,
        )


def _base_config(
    *,
    enabled: bool = True,
    dictation: bool | None = None,
    stt: VoiceSTTConfig | None = None,
    router_current_room_members: bool = True,
) -> Config:
    return Config(
        administrators=[ADMIN],
        agents={
            "assistant": AgentConfig(
                display_name="Assistant",
                rooms=[GRANT_ROOM_KEY],
                access=ResponderAccessConfig(users=[ALICE], members_of_rooms=[GRANT_ROOM_KEY]),
            ),
        },
        teams={
            "ops": TeamConfig(
                display_name="Ops",
                role="Ops team",
                agents=["assistant"],
                access=ResponderAccessConfig(users=[TEAM_USER]),
            ),
        },
        router=RouterConfig(access=ResponderAccessConfig(current_room_members=router_current_room_members)),
        voice=VoiceConfig(
            enabled=enabled,
            dictation=dictation,
            stt=stt or VoiceSTTConfig(api_key="test-stt-key", model="gpt-transcribe"),
        ),
    )


async def _drive_post(
    path: str,
    headers: dict[str, str],
    chunks: list[bytes],
) -> tuple[int, dict[str, Any], int, dict[str, str]]:
    """Stream one POST chunk by chunk through ``main.app`` and return status, JSON, chunks read, and headers."""
    read = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal read
        read += 1
        return {"type": "http.request", "body": chunks[read - 1], "more_body": read < len(chunks)}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("mindroom.test", 80),
        "client": ("127.0.0.1", 50000),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers.items()],
    }
    async with asyncio.timeout(10):
        await main.app(scope, receive, send)
    [start, *body] = sent
    answer = json.loads(b"".join(message.get("body", b"") for message in body))
    return start["status"], answer, read, {name.decode(): value.decode() for name, value in start["headers"]}


@pytest_asyncio.fixture
async def api(tmp_path: Path) -> AsyncIterator[DictationHarness]:
    """Serve MindRoom's real API app wired for dictation tests."""
    storage_path = tmp_path / "storage"
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=storage_path,
        process_env={"MINDROOM_COMPUTER_ALLOWED_ORIGINS": f'["{CHAT}"]'},
    )
    main.initialize_api_app(main.app, paths)
    clock = _Clock()
    limiter = DictationLimiter(clock=clock)
    memberships = AgentReplyMembershipIndex()

    async def stt_handler(request: httpx.Request) -> httpx.Response:
        harness.stt_requests.append(request)
        async with harness.stt_arrived:
            harness.stt_arrived.notify_all()
        if harness.stt_gate is not None:
            await harness.stt_gate.wait()
        return harness.stt_responder(request)

    stt_client = httpx.AsyncClient(transport=httpx.MockTransport(stt_handler))

    async def room_members(room_id: str) -> dict[str, str | None]:
        if room_id not in harness.room_roster:
            raise HTTPException(503, "No running MindRoom bot is in this room.")
        return harness.room_roster[room_id]

    async def refresh_memberships() -> None:
        harness.refresh_calls += 1
        if harness.on_refresh is not None:
            harness.on_refresh()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://mindroom.test",
    ) as client:
        harness = DictationHarness(client, paths, storage_path, clock, limiter, stt_client, memberships)
        harness.publish(_base_config())
        bind_test_embedded_runtime(
            main.app,
            agent_reply_memberships=memberships,
            refresh_agent_reply_memberships_when_needed=refresh_memberships,
        )
        state = app_state(main.app)
        state.dictation_http_client = stt_client
        state.dictation_limiter = limiter
        state.room_members = room_members
        try:
            yield harness
        finally:
            state.room_members = None
            state.dictation_limiter = None
            await close_dictation_http_client(main.app)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), [("GET", "/status"), ("POST", "/transcribe")])
async def test_unauthenticated_requests_answer_401_even_when_disabled(
    api: DictationHarness,
    method: str,
    path: str,
) -> None:
    """Missing or invalid client sessions fail with 401 and Bearer challenge before checking feature state."""
    api.publish(_base_config(enabled=False, dictation=False))

    response = await api.client.request(method, PREFIX + path, headers={"Origin": CHAT})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.headers["access-control-allow-origin"] == CHAT
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_status_respects_dictation_override_and_stt_readiness(
    api: DictationHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dictation defaults to voice.enabled, respects explicit voice.dictation, and requires STT readiness."""
    api.publish(_base_config(enabled=False, dictation=None))
    _refusal(await api.status(ALICE), 404, "not-found")

    api.publish(_base_config(enabled=True, dictation=False))
    _refusal(await api.status(ALICE), 404, "not-found")

    api.publish(_base_config(enabled=False, dictation=True))
    enabled = await api.status(ALICE)
    assert (enabled.status_code, enabled.json()) == (
        200,
        {"enabled": True, "maxSegmentSeconds": 30, "maxSegmentBytes": 1_000_000},
    )

    monkeypatch.setattr(voice_handler, "get_api_key_for_service", lambda *_args: None)
    api.publish(_base_config(enabled=True, stt=VoiceSTTConfig(credentials_service="openai")))
    _refusal(await api.status(ALICE), 404, "not-found")
    _refusal(await api.transcribe(ALICE, audio=_wav()), 404, "not-found")


@pytest.mark.asyncio
async def test_cors_exposes_retry_after_and_blocks_disallowed_or_sibling_paths(api: DictationHarness) -> None:
    """Allowed client origins read WWW-Authenticate and Retry-After; disallowed origins and sibling paths do not."""
    preflight = await api.client.options(
        f"{PREFIX}/transcribe",
        headers={
            "Origin": CHAT,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    allowed = await api.status(ALICE)
    disallowed = await api.status(ALICE, origin="https://evil.example.org")
    sibling = await api.client.get("/api/dictationX/v1/status", headers={"Origin": CHAT})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == CHAT
    assert allowed.headers["access-control-allow-origin"] == CHAT
    assert set(allowed.headers["access-control-expose-headers"].split(", ")) == {"WWW-Authenticate", "Retry-After"}
    assert allowed.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in disallowed.headers
    assert "access-control-allow-origin" not in sibling.headers


@pytest.mark.asyncio
async def test_authorization_covers_admins_agents_teams_room_grants_bots_and_guest_rooms(
    api: DictationHarness,
) -> None:
    """Authorization admits admins, agent/team/grant users, and router-room guests while blocking bots and outsiders."""
    assert (await api.status(ADMIN)).status_code == 200
    assert (await api.status(ALICE)).status_code == 200
    assert (await api.status(TEAM_USER)).status_code == 200
    _refusal(await api.status(OUTSIDER), 403, "forbidden")

    # MindRoom's own bot accounts are always rejected (even an agent's own account), unread on /transcribe.
    bot_id = MatrixID.from_username(
        agent_username_localpart("assistant", api.runtime_paths),
        runtime_matrix_domain(api.runtime_paths),
    ).full_id
    _refusal(await api.status(bot_id), 403, "forbidden")
    bot_status, bot_body, bot_chunks, _ = await _drive_post(
        f"{PREFIX}/transcribe",
        {**api.headers(bot_id), "Content-Type": "multipart/form-data; boundary=xyz"},
        [b"x" * 1024],
    )
    assert (bot_status, bot_body["code"], bot_chunks) == (403, "forbidden", 0)

    # Room-backed grant user succeeds after one membership refresh on pending state.
    grant_user = "@grantmember:localhost"
    cfg = _base_config()
    api.publish(cfg)
    api.refresh_calls = 0
    api.on_refresh = lambda: _seed_memberships(api.memberships, cfg, [(GRANT_ROOM_KEY, GRANT_ROOM_ID, {grant_user})])
    assert (await api.status(grant_user)).status_code == 200
    assert api.refresh_calls == 1

    # Guest in a router room passes only when joined in room_members and allowed by router access.
    api.on_refresh = None
    api.room_roster[ROOM] = {GUEST: "Guest"}
    _seed_memberships(api.memberships, cfg, [(GRANT_ROOM_KEY, GRANT_ROOM_ID, {grant_user}), (None, ROOM, {GUEST})])
    _refusal(await api.status(GUEST), 403, "forbidden")
    _refusal(await api.status(GUEST, room_id="!unknown:localhost"), 403, "forbidden")
    assert (await api.status(GUEST, room_id=ROOM)).status_code == 200
    assert (await api.transcribe(GUEST, audio=_wav(), room_id=ROOM)).status_code == 200
    _refusal(await api.transcribe(OUTSIDER, audio=_wav(), room_id=ROOM), 403, "forbidden")

    # Disabling router current_room_members denies the guest even while joined.
    strict_cfg = _base_config(router_current_room_members=False)
    api.publish(strict_cfg)
    _seed_memberships(api.memberships, strict_cfg, [(None, ROOM, {GUEST})])
    _refusal(await api.status(GUEST, room_id=ROOM), 403, "forbidden")


@pytest.mark.asyncio
async def test_transcribe_forwards_fields_records_usage_never_touches_disk_and_never_logs_text(
    api: DictationHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid segments stay in memory, forward prompt/language, record voice_dictation usage, and omit text from logs."""
    private_text = "confidential dictated text"
    private_prompt = "prior composer words"
    api.stt_responder = lambda _req: httpx.Response(
        200,
        json={
            "text": f"  {private_text}  ",
            "usage": {
                "type": "tokens",
                "input_tokens": 14,
                "output_tokens": 4,
                "total_tokens": 18,
                "input_token_details": {"audio_tokens": 10},
            },
        },
    )

    def forbid_disk(*_args: object, **_kwargs: object) -> object:
        msg = "Dictation uploads must stay strictly in memory"
        raise AssertionError(msg)

    monkeypatch.setattr(tempfile, "TemporaryFile", forbid_disk)
    monkeypatch.setattr(tempfile, "SpooledTemporaryFile", forbid_disk)
    monkeypatch.setattr(starlette.formparsers, "MultiPartParser", forbid_disk)

    with capture_logs() as logs:
        response = await api.transcribe(ALICE, audio=_wav(1.5), prompt=f"  {private_prompt}  ", language="nl-NL")

    assert response.status_code == 200
    assert response.json() == {"text": private_text, "seconds": 1.5}
    assert not api.stt_client.is_closed
    [stt_req] = api.stt_requests
    assert _form_fields(stt_req) == {"model": "gpt-transcribe", "prompt": private_prompt, "language": "nl-NL"}
    assert private_text not in str(logs)
    assert private_prompt not in str(logs)
    [debug_log] = [entry for entry in logs if entry.get("event") == "dictation_segment_transcribed"]
    assert (debug_log["user_id"], debug_log["seconds"], debug_log["chars"]) == (ALICE, 1.5, len(private_text))
    assert isinstance(debug_log["latency_ms"], int)

    db_path = api.storage_path / "system" / "sessions" / "system.db"
    with closing(sqlite3.connect(db_path)) as conn:
        [(payload,)] = conn.execute("SELECT usage_data FROM system_sessions_usage").fetchall()
    stored = json.loads(payload)
    assert stored["kind"] == "voice_dictation"
    assert stored["metrics"]["total_tokens"] == 18


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"audio": None}, 400, "invalid"),
        ({"audio": b"not a wav"}, 400, "invalid"),
        ({"audio": _wav(1.0, rate=48_000)}, 400, "invalid"),
        ({"audio": _wav(1.0, channels=2)}, 400, "invalid"),
        ({"audio": _wav(0.0)}, 400, "invalid"),
        ({"audio": _wav(30.5)}, 413, "too-large"),
        ({"audio": b"R" * 1_000_001}, 413, "too-large"),
        ({"audio": _wav(), "prompt": "é" * 501}, 400, "invalid"),
        ({"audio": _wav(), "language": "en_US"}, 400, "invalid"),
        ({"audio": _wav(), "language": "ab-12345678-12345"}, 400, "invalid"),
    ],
    ids=[
        "missing-audio",
        "not-wav",
        "wrong-sample-rate",
        "stereo",
        "empty-wav",
        "over-30s",
        "over-1mb-audio",
        "prompt-over-1000-bytes",
        "bad-language-underscore",
        "language-over-16-chars",
    ],
)
async def test_transcribe_validates_fields_and_audio_bounds(
    api: DictationHarness,
    kwargs: dict[str, Any],
    status: int,
    code: str,
) -> None:
    """Invalid fields return 400 and oversized audio returns 413 without hitting STT."""
    _refusal(await api.transcribe(ALICE, **kwargs), status, code)
    assert api.stt_requests == []


@pytest.mark.asyncio
async def test_transcribe_rejects_wrong_content_type_and_oversized_body_before_or_while_streaming(
    api: DictationHarness,
) -> None:
    """Non-multipart requests return 400; oversized bodies return 413 via Content-Length or streaming cap."""
    url = f"{PREFIX}/transcribe"
    wrong_ct = await api.client.post(url, headers={**api.headers(ALICE), "Content-Type": "audio/wav"}, content=_wav())
    _refusal(wrong_ct, 400, "invalid")

    hdrs = {**api.headers(ALICE), "Content-Type": "multipart/form-data; boundary=xyz"}
    chunk = b"x" * 65_536
    declared = await _drive_post(url, {**hdrs, "Content-Length": "2000000"}, [chunk] * 31)
    streamed = await _drive_post(url, hdrs, [chunk] * 24)

    assert (declared[0], declared[1]["code"], declared[2]) == (413, "too-large", 0)
    assert (streamed[0], streamed[1]["code"]) == (413, "too-large")
    assert streamed[2] * len(chunk) <= 1_016_384 + len(chunk)
    assert api.stt_requests == []


@pytest.mark.asyncio
async def test_concurrency_limits_admit_before_reading_body_and_release_on_completion(
    api: DictationHarness,
) -> None:
    """Per-user (2) and instance-wide (4) concurrency caps return 429 unread and release slots in finally."""
    api.stt_gate = asyncio.Event()
    url = f"{PREFIX}/transcribe"
    form = {"Content-Type": "multipart/form-data; boundary=xyz"}
    tasks = [
        asyncio.create_task(api.transcribe(ALICE, audio=_wav())),
        asyncio.create_task(api.transcribe(ALICE, audio=_wav())),
    ]
    await api.wait_for_stt_requests(2)

    per_user = await _drive_post(url, {**api.headers(ALICE), **form}, [b"x" * 4096])
    assert (per_user[0], per_user[1]["code"], per_user[2]) == (429, "too-many-requests", 0)
    assert int(per_user[3]["retry-after"]) >= 1

    tasks.extend(
        [
            asyncio.create_task(api.transcribe(ADMIN, audio=_wav())),
            asyncio.create_task(api.transcribe(ADMIN, audio=_wav())),
        ],
    )
    await api.wait_for_stt_requests(4)

    global_cap = await _drive_post(url, {**api.headers(TEAM_USER), **form}, [b"x" * 4096])
    assert (global_cap[0], global_cap[1]["code"], global_cap[2]) == (429, "too-many-requests", 0)
    assert int(global_cap[3]["retry-after"]) >= 1

    api.stt_gate.set()
    results = await asyncio.gather(*tasks)
    assert all(res.status_code == 200 for res in results)
    assert (await api.transcribe(TEAM_USER, audio=_wav())).status_code == 200


@pytest.mark.asyncio
async def test_sliding_window_rate_limits_and_stale_user_pruning(api: DictationHarness) -> None:
    """Per-user 30 req/min and 180 s/min sliding windows enforce Retry-After and prune stale user keys."""
    # Audio-seconds cap: 6 x 30s = 180s passes, 7th fails with Retry-After, advancing 60s admits again.
    for _ in range(6):
        assert (await api.transcribe(ALICE, audio=_wav(30.0))).status_code == 200
        api.clock.advance(1.0)
    over_audio = await api.transcribe(ALICE, audio=_wav(1.0))
    _refusal(over_audio, 429, "too-many-requests")
    assert int(over_audio.headers["retry-after"]) == 54

    # Request-count cap: 30 x 1s passes for ADMIN, 31st fails with Retry-After.
    for _ in range(30):
        assert (await api.transcribe(ADMIN, audio=_wav(1.0))).status_code == 200
    over_reqs = await api.transcribe(ADMIN, audio=_wav(1.0))
    _refusal(over_reqs, 429, "too-many-requests")
    assert int(over_reqs.headers["retry-after"]) == 60

    # Advancing beyond the 60s window prunes stale user entries and restores capacity.
    api.clock.advance(61.0)
    assert (await api.transcribe(TEAM_USER, audio=_wav(1.0))).status_code == 200
    assert list(api.limiter._history) == [TEAM_USER]
    assert (await api.transcribe(ALICE, audio=_wav(1.0))).status_code == 200


@pytest.mark.asyncio
async def test_provider_failure_returns_503_without_logging_body_and_shutdown_closes_client(
    api: DictationHarness,
) -> None:
    """Provider errors return 503 unavailable without echoing response bodies, and shutdown closes the pooled client."""
    leaked = "sensitive-upstream-echo"
    api.stt_responder = lambda _req: httpx.Response(503, text=leaked)

    with capture_logs() as logs:
        response = await api.transcribe(ALICE, audio=_wav())

    _refusal(response, 503, "unavailable")
    assert response.json()["error"] == "Speech transcription is currently unavailable."
    assert leaked not in str(logs)

    pooled = app_state(main.app).dictation_http_client
    assert pooled is not None
    assert not pooled.is_closed
    await close_dictation_http_client(main.app)
    assert pooled.is_closed
    assert app_state(main.app).dictation_http_client is None
