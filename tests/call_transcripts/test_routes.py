"""The call transcript routes through MindRoom's real API app, with client sessions, its CORS policy and a live service."""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx
import pytest
import pytest_asyncio
import starlette.formparsers

from mindroom.api import main
from mindroom.api.client_sessions import client_session_store
from mindroom.call_transcripts import routes
from mindroom.constants import resolve_runtime_paths
from tests.call_transcripts.test_service import ANN, BOB, CAROL, OUTSIDER, ROOM, World, _wav
from tests.runtime_fakes import bind_test_embedded_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from starlette.types import Message

CHAT = "https://chat.example.org"
PREFIX = "/api/call-transcripts/v1"


@dataclass
class Api:
    """MindRoom's API app lent one call transcript service, and an HTTP client that calls it in this event loop."""

    world: World
    client: httpx.AsyncClient
    enabled: bool = True

    def headers(self, user_id: str) -> dict[str, str]:
        """Sign ``user_id`` in with a client session, from MindRoom Chat's origin."""
        token = client_session_store(main.app).create(user_id).token
        return {"Authorization": f"Bearer {token}", "Origin": CHAT}

    async def join(self, user_id: str, room_id: str = ROOM) -> httpx.Response:
        """Join or check in to the room's call."""
        return await self.client.post(f"{PREFIX}/rooms/{quote(room_id)}/call", headers=self.headers(user_id))

    async def pause(self, call_id: str, user_id: str, *, paused: bool) -> httpx.Response:
        """Pause or resume transcription in the call."""
        return await self.client.post(
            f"{PREFIX}/calls/{call_id}/pause",
            headers=self.headers(user_id),
            json={"paused": paused},
        )

    async def live(self, user_id: str, *, after: str | None = None) -> httpx.Response:
        """Read the room's live call, as MindRoom Chat polls it."""
        params = {} if after is None else {"after": after}
        return await self.client.get(
            f"{PREFIX}/rooms/{quote(ROOM)}/call",
            headers=self.headers(user_id),
            params=params,
        )

    async def speech(
        self,
        call_id: str,
        user_id: str,
        *,
        audio: bytes | None = None,
        started_at: str | None = "1970-01-01T00:16:40.000Z",
    ) -> httpx.Response:
        """Upload a stretch of speech as MindRoom Chat does."""
        data = {} if started_at is None else {"startedAt": started_at}
        return await self.client.post(
            f"{PREFIX}/calls/{call_id}/speech",
            headers=self.headers(user_id),
            files={"audio": ("speech.wav", _wav() if audio is None else audio, "audio/wav")},
            data=data,
        )


@pytest_asyncio.fixture
async def api(tmp_path: Path) -> AsyncIterator[Api]:
    """Serve MindRoom's API app, with Chat's origin allowed, lent a service over a real store and fake room side."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_COMPUTER_ALLOWED_ORIGINS": f'["{CHAT}"]'},
    )
    main.initialize_api_app(main.app, paths)
    world = World(tmp_path)
    world.clock.advance(5)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app),
        base_url="http://mindroom.test",
    ) as client:
        served = Api(world, client)
        bind_test_embedded_runtime(main.app, call_transcripts=lambda: served.world.service if served.enabled else None)
        yield served


def _refusal(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code
    assert isinstance(body["error"], str)
    assert body["error"]


# Every route


ROUTES = [
    ("POST", f"/rooms/{quote(ROOM)}/call"),
    ("POST", "/calls/call1/leave"),
    ("POST", "/calls/call1/pause"),
    ("POST", "/calls/call1/speech"),
    ("GET", f"/rooms/{quote(ROOM)}/call"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_answers_404_while_call_transcripts_are_off(api: Api, method: str, path: str) -> None:
    """Off, the routes look absent to everyone, signed in or not, and MindRoom Chat can still read that answer."""
    api.enabled = False
    preflight = await api.client.options(
        PREFIX + path,
        headers={
            "Origin": CHAT,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "authorization",
        },
    )
    signed_in = await api.client.request(method, PREFIX + path, headers=api.headers(ANN))
    anonymous = await api.client.request(method, PREFIX + path, headers={"Origin": CHAT})

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == CHAT
    for response in (signed_in, anonymous):
        _refusal(response, 404, "not-found")
        assert response.headers["access-control-allow-origin"] == CHAT
        assert response.headers["cache-control"] == "no-store"
        assert "access-control-allow-credentials" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_every_route_needs_a_client_session(api: Api, method: str, path: str) -> None:
    """Without a session, or with one that ran out, the answer is 401 and nothing is done."""
    store = client_session_store(main.app)
    expired = store.create(ANN).token
    store.clock = lambda: time.time() + 3601
    try:
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": f"Bearer {expired}"}):
            response = await api.client.request(method, PREFIX + path, headers={"Origin": CHAT, **headers})
            assert response.status_code == 401
            assert response.json() == {"detail": "Missing, invalid or expired client session."}
            assert response.headers["www-authenticate"] == "Bearer"
    finally:
        store.clock = time.time
    assert api.world.store.live_call(ROOM) is None


@pytest.mark.asyncio
async def test_retry_after_is_readable_from_chats_origin_on_these_routes_only(api: Api) -> None:
    """Chat reads Retry-After here; plugin routes keep exposing only WWW-Authenticate."""
    calls = await api.join(ANN)
    plugin = await api.client.get("/api/plugins/x/y", headers={"Origin": CHAT})
    elsewhere = await api.client.get("/api/call-transcriptsX/v1", headers={"Origin": CHAT})

    exposed = {name.strip().lower() for name in calls.headers["access-control-expose-headers"].split(",")}
    assert exposed == {"www-authenticate", "retry-after"}
    assert plugin.headers["access-control-expose-headers"].lower() == "www-authenticate"
    assert "access-control-allow-origin" not in elsewhere.headers


# Joining, leaving and Pause


@pytest.mark.asyncio
async def test_join_answers_the_live_call_and_is_also_the_heartbeat(api: Api) -> None:
    """The first join starts the call; later ones from anyone answer the same call, with times in ISO 8601."""
    first = await api.join(ANN)
    await api.world.settled()
    api.world.clock.advance(10)
    again = await api.join(ANN)
    other = await api.join(BOB)

    assert first.status_code == 200
    call = first.json()["call"]
    assert call == {
        "id": call["id"],
        "roomId": ROOM,
        "rowEventId": None,
        "startedAt": "1970-01-01T00:16:45.000Z",
        "endedAt": None,
        "members": [ANN],
        "pausedBy": None,
    }
    assert (again.json()["call"]["id"], again.json()["call"]["rowEventId"]) == (call["id"], "$row-1")
    assert other.json()["call"]["members"] == [ANN, BOB]
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["access-control-allow-origin"] == CHAT


@pytest.mark.asyncio
async def test_refusals_answer_with_their_status_sentence_and_code(api: Api) -> None:
    """Outsiders get 404, members who may not post 403, and a room that cannot be checked 503."""
    _refusal(await api.join(OUTSIDER), 404, "not-found")
    api.world.room.posters.discard(CAROL)
    _refusal(await api.join(CAROL), 403, "forbidden")
    api.world.room.readable = False
    _refusal(await api.join(ANN), 503, "unavailable")
    assert api.world.store.live_call(ROOM) is None


@pytest.mark.asyncio
async def test_call_routes_take_the_room_from_the_call_and_404_outsiders(api: Api) -> None:
    """Someone outside the call's room cannot leave, pause, speak in or even find the call; nor can anyone an unknown one."""
    call_id = (await api.join(ANN)).json()["call"]["id"]

    for user_id, target in ((OUTSIDER, call_id), (ANN, "unknown")):
        headers = api.headers(user_id)
        _refusal(await api.client.post(f"{PREFIX}/calls/{target}/leave", headers=headers), 404, "not-found")
        _refusal(await api.pause(target, user_id, paused=True), 404, "not-found")
        _refusal(await api.speech(target, user_id), 404, "not-found")
    stored = api.world.store.call(call_id)
    assert stored is not None
    assert stored.paused_by is None
    assert api.world.store.lines(call_id) == []


@pytest.mark.asyncio
async def test_pause_toggles_for_everyone_and_refuses_speech_meanwhile(api: Api) -> None:
    """Pausing shows who paused and refuses speech with 409; anyone in the call resumes it."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    await api.join(BOB)
    paused = await api.pause(call_id, ANN, paused=True)
    refused = await api.speech(call_id, BOB)
    resumed = await api.pause(call_id, BOB, paused=False)

    assert paused.json()["call"]["pausedBy"] == ANN
    _refusal(refused, 409, "paused")
    assert resumed.json()["call"]["pausedBy"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"{}", b'{"paused": "yes"}', b'{"paused": 1}', b"not json", b"[true]"])
async def test_pause_needs_paused_true_or_false(api: Api, body: bytes) -> None:
    """Anything but a JSON object with a boolean ``paused`` is invalid, and pauses nothing."""
    call_id = (await api.join(ANN)).json()["call"]["id"]

    response = await api.client.post(
        f"{PREFIX}/calls/{call_id}/pause",
        headers={**api.headers(ANN), "Content-Type": "application/json"},
        content=body,
    )

    _refusal(response, 400, "invalid")
    stored = api.world.store.call(call_id)
    assert stored is not None
    assert stored.paused_by is None


@pytest.mark.asyncio
async def test_leaving_answers_an_empty_object_and_ends_the_call_for_the_last_one_out(api: Api) -> None:
    """The last app out ends the call."""
    call_id = (await api.join(ANN)).json()["call"]["id"]

    response = await api.client.post(f"{PREFIX}/calls/{call_id}/leave", headers=api.headers(ANN))

    assert (response.status_code, response.json()) == (200, {})
    ended = api.world.store.call(call_id)
    assert ended is not None
    assert ended.ended_at is not None


# Speech


@pytest.mark.asyncio
async def test_speech_becomes_a_line(api: Api) -> None:
    """A WAV stretch with when it began becomes a line of what was said, under the session's own user."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)
    api.world.transcriber.text = "We ship on Friday."

    response = await api.speech(call_id, ANN, started_at="1970-01-01T01:16:47.500+01:00")

    assert response.status_code == 200
    line = response.json()["line"]
    assert line == {
        "id": line["id"],
        "callId": call_id,
        "userId": ANN,
        "saidAt": "1970-01-01T00:16:47.500Z",
        "seconds": 2.0,
        "kind": "speech",
        "text": "We ship on Friday.",
    }
    assert api.world.transcriber.requests == [(call_id, ANN, 2.0)]


@pytest.mark.asyncio
async def test_speech_that_fails_to_transcribe_answers_a_gap(api: Api) -> None:
    """A stretch speech-to-text could not hear is a gap with its reason and no text."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)
    api.world.transcriber.text = None

    line = (await api.speech(call_id, ANN)).json()["line"]

    assert (line["kind"], line["reason"]) == ("gap", "not-transcribed")
    assert "text" not in line


@pytest.mark.asyncio
async def test_speech_in_which_no_words_were_heard_answers_no_line(api: Api) -> None:
    """A stretch speech-to-text heard no words in answers a null line and leaves nothing in the call."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)
    api.world.transcriber.text = ""

    response = await api.speech(call_id, ANN)

    assert response.status_code == 200
    assert response.json() == {"line": None}
    assert api.world.store.lines(call_id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("started_at", "audio"),
    [
        (None, None),
        ("yesterday", None),
        ("1970-01-01T00:16:47", None),
        ("1970-01-01T00:16:47Z", b"not a wav"),
    ],
    ids=["no-start", "not-a-time", "no-time-zone", "not-wav"],
)
async def test_speech_needs_a_start_with_a_time_zone_and_a_wav(
    api: Api,
    started_at: str | None,
    audio: bytes | None,
) -> None:
    """A stretch without a start, with one that names no time zone, or that is not a WAV is invalid."""
    call_id = (await api.join(ANN)).json()["call"]["id"]

    _refusal(await api.speech(call_id, ANN, audio=audio, started_at=started_at), 400, "invalid")
    assert api.world.transcriber.requests == []


@pytest.mark.asyncio
async def test_speech_must_be_multipart_with_its_audio(api: Api) -> None:
    """Speech sent as anything but multipart form data with an ``audio`` part is invalid."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    url = f"{PREFIX}/calls/{call_id}/speech"

    raw = await api.client.post(url, headers={**api.headers(ANN), "Content-Type": "audio/wav"}, content=_wav())
    no_audio = await api.client.post(url, headers=api.headers(ANN), files={"sound": ("s.wav", _wav(), "audio/wav")})
    broken = await api.client.post(
        url,
        headers={**api.headers(ANN), "Content-Type": "multipart/form-data; boundary=xyz"},
        content=b"--xyz\r\nnonsense",
    )

    for response in (raw, no_audio, broken):
        _refusal(response, 400, "invalid")
    assert api.world.transcriber.requests == []


@pytest.mark.asyncio
async def test_speech_longer_than_30_seconds_is_too_large(api: Api) -> None:
    """A WAV whose header says more than 30 seconds is refused with 413, untranscribed."""
    call_id = (await api.join(ANN)).json()["call"]["id"]

    _refusal(await api.speech(call_id, ANN, audio=_wav(31.0)), 413, "too-large")
    assert api.world.transcriber.requests == []


@pytest.mark.asyncio
async def test_too_many_stretches_say_when_to_try_again(api: Api) -> None:
    """A second stretch while the first is still at speech-to-text is refused with 429 and Retry-After."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)
    api.world.transcriber.gate = asyncio.Event()
    first = asyncio.create_task(api.speech(call_id, ANN))
    await api.world.transcriber.started.wait()

    second = await api.speech(call_id, ANN)
    api.world.transcriber.gate.set()

    _refusal(second, 429, "too-many-requests")
    assert second.headers["retry-after"] == "3"
    assert (await first).status_code == 200


async def _drive(
    path: str,
    headers: dict[str, str],
    chunks: list[bytes],
    *,
    reading: asyncio.Event | None = None,
) -> tuple[int, dict[str, Any], int, dict[str, str]]:
    """Send one POST through the API app body chunk by chunk; return its status, JSON answer, chunks read, headers.

    With ``reading``, set it once the body is first read, and never end the body: an upload still being sent.
    """
    read = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal read
        if reading is not None:
            reading.set()
        if read == len(chunks):
            await asyncio.Event().wait()
        read += 1
        return {
            "type": "http.request",
            "body": chunks[read - 1],
            "more_body": reading is not None or read < len(chunks),
        }

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


@pytest.mark.asyncio
async def test_speech_too_large_is_refused_before_its_body_is_read(api: Api) -> None:
    """A declared length over the limit is refused unread; an undeclared one stops being read once it is over."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    path = f"{PREFIX}/calls/{call_id}/speech"
    headers = {**api.headers(ANN), "Content-Type": "multipart/form-data; boundary=xyz"}
    chunk = b"x" * 65_536

    declared = await _drive(path, {**headers, "Content-Length": "2000000"}, [chunk] * 31)
    undeclared = await _drive(path, headers, [chunk] * 48)

    assert declared[:2] == (413, {"error": declared[1]["error"], "code": "too-large"})
    assert declared[2] == 0
    assert undeclared[:2] == (413, {"error": undeclared[1]["error"], "code": "too-large"})
    assert undeclared[2] * len(chunk) <= routes._MAX_SPEECH_BODY_BYTES + len(chunk)
    assert api.world.transcriber.requests == []


@pytest.mark.asyncio
async def test_speech_from_outside_the_call_or_with_a_stretch_in_flight_is_refused_before_its_body_is_read(
    api: Api,
) -> None:
    """Who sends a stretch is checked before its audio is read: 404 outside the call's room, 429 with one in flight."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)
    path = f"{PREFIX}/calls/{call_id}/speech"
    form = {"Content-Type": "multipart/form-data; boundary=xyz"}
    chunks = [b"x" * 65_536] * 4

    outsider = await _drive(path, {**api.headers(OUTSIDER), **form}, chunks)
    unknown = await _drive(f"{PREFIX}/calls/unknown/speech", {**api.headers(ANN), **form}, chunks)
    api.world.transcriber.gate = asyncio.Event()
    first = asyncio.create_task(api.speech(call_id, ANN))
    await api.world.transcriber.started.wait()
    in_flight = await _drive(path, {**api.headers(ANN), **form}, chunks)
    api.world.transcriber.gate.set()

    assert (outsider[0], outsider[1]["code"], outsider[2]) == (404, "not-found", 0)
    assert (unknown[0], unknown[1]["code"], unknown[2]) == (404, "not-found", 0)
    assert (in_flight[0], in_flight[1]["code"], in_flight[2]) == (429, "too-many-requests", 0)
    assert (await first).status_code == 200
    assert len(api.world.transcriber.requests) == 1


@pytest.mark.asyncio
async def test_a_second_upload_at_once_or_one_from_a_member_not_in_the_call_is_refused_before_its_body_is_read(
    api: Api,
) -> None:
    """One upload at a time per person, from someone in the call: a second is 429 and a member not in the call 403."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)
    path = f"{PREFIX}/calls/{call_id}/speech"
    form = {"Content-Type": "multipart/form-data; boundary=xyz"}
    chunks = [b"x" * 65_536] * 4
    reading = asyncio.Event()
    first = asyncio.create_task(_drive(path, {**api.headers(ANN), **form}, chunks[:1], reading=reading))
    await reading.wait()

    second = await _drive(path, {**api.headers(ANN), **form}, chunks)
    member = await _drive(path, {**api.headers(CAROL), **form}, chunks)
    first.cancel()
    await asyncio.gather(first, return_exceptions=True)

    assert (second[0], second[1]["code"], second[2], second[3]["retry-after"]) == (429, "too-many-requests", 0, "3")
    assert (member[0], member[1]["code"], member[2]) == (403, "forbidden", 0)
    assert (await api.speech(call_id, ANN)).status_code == 200
    assert len(api.world.transcriber.requests) == 1


@pytest.mark.asyncio
async def test_speech_is_never_written_to_disk(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    """The audio goes from the request to speech-to-text in memory: no temporary file, spooled or not, is made."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)

    def no_files(*_args: object, **_kwargs: object) -> None:
        msg = "speech must not be written to a file"
        raise AssertionError(msg)

    for name in ("SpooledTemporaryFile", "TemporaryFile", "NamedTemporaryFile", "mkstemp", "mkdtemp"):
        monkeypatch.setattr(tempfile, name, no_files)
    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", no_files)

    response = await api.speech(call_id, ANN)

    assert response.status_code == 200, response.text
    assert response.json()["line"]["kind"] == "speech"


# The live call


@pytest.mark.asyncio
async def test_the_live_call_has_its_lines_and_names(api: Api) -> None:
    """A member reads the call, its lines and everyone's names; with no call, nothing."""
    empty = await api.live(ANN)
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)
    line = (await api.speech(call_id, ANN)).json()["line"]

    live = await api.live(BOB)
    outsider = await api.live(OUTSIDER)

    assert empty.json() == {"call": None, "lines": [], "names": {}}
    assert live.headers["cache-control"] == "no-store"
    body = live.json()
    assert body["call"]["id"] == call_id
    assert body["lines"] == [line]
    assert body["names"] == {ANN: "Ann"}
    _refusal(outsider, 404, "not-found")


@pytest.mark.asyncio
async def test_polling_after_a_line_answers_only_the_lines_added_since(api: Api) -> None:
    """``after`` names the last line the app has; the answer has the call and only what was added after it."""
    call_id = (await api.join(ANN)).json()["call"]["id"]
    api.world.clock.advance(10)
    first = (await api.speech(call_id, ANN)).json()["line"]

    nothing_new = (await api.live(BOB, after=first["id"])).json()
    second = (await api.speech(call_id, ANN)).json()["line"]
    await api.pause(call_id, ANN, paused=True)
    polled = (await api.live(BOB, after=first["id"])).json()

    assert (nothing_new["call"]["id"], nothing_new["lines"]) == (call_id, [])
    assert polled["lines"] == [second]
    assert polled["call"]["pausedBy"] == ANN
    assert polled["names"] == {ANN: "Ann"}
