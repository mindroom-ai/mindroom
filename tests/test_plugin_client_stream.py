"""Plugin websocket routes sign their caller in with a client session offered as a subprotocol, through client_stream."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from contextlib import asynccontextmanager
from types import ModuleType
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from mindroom import plugin_http
from mindroom.api import client_sessions
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import ClientSessionStore, client_session_store
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.plugin_http import ClientUser
from tests.conftest import bind_runtime_paths, publish_test_plugins
from tests.test_plugin_routes import _app, _write_plugin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator
    from pathlib import Path

    from starlette.testclient import WebSocketTestSession

ROOM = "!room:example.org"
ALICE = "@alice:example.org"
CLIENT_ORIGIN = "https://chat.example.org"
NO_SESSION = "Missing, invalid or expired client session."
REFUSED = "The route refused this stream; try again later."

# The routes reach the test through this module, which the autouse plugin-state restore removes after each test.
PROBE_MODULE = "mindroom_test_probe_client_stream"
ROUTES = f"""
import asyncio
import sys

from fastapi import APIRouter, Depends, WebSocket

from mindroom.plugin_http import ClientUser, client_stream, require_client_user

router = APIRouter()
_probe = sys.modules[{PROBE_MODULE!r}]
"""
ROUTES += """


@router.websocket("/rooms/{room_id}/echo")
async def echo(websocket: WebSocket, room_id: str) -> None:
    async with client_stream(websocket, room_id) as user:
        async for text in websocket.iter_text():
            await websocket.send_text(f"{user.user_id}: {text}")


@router.websocket("/echo")
async def echo_without_room(websocket: WebSocket) -> None:
    async with client_stream(websocket) as user:
        async for text in websocket.iter_text():
            await websocket.send_text(f"{user.user_id}: {text}")


@router.websocket("/waits")
async def waits(websocket: WebSocket) -> None:
    # Waits on something other than the websocket, as a stream pushing server events does.
    async with client_stream(websocket) as user:
        await websocket.send_text(user.user_id)
        await asyncio.Event().wait()
    _probe.after_block.append(user.user_id)


@router.websocket("/times-out")
async def times_out(websocket: WebSocket) -> None:
    try:
        async with client_stream(websocket):
            async with asyncio.timeout(0.01):
                await asyncio.Event().wait()
    except TimeoutError:
        await websocket.send_text("timed out")


@router.websocket("/rooms/{room_id}/admits")
async def admits(websocket: WebSocket, room_id: str) -> None:
    async with client_stream(websocket, room_id, admit=_probe.admit) as user:
        await websocket.send_text(user.user_id)
        await asyncio.Event().wait()


@router.get("/whoami")
async def whoami(user: ClientUser = Depends(require_client_user)) -> dict:
    return {"user_id": user.user_id}
"""


class _Membership:
    """A stand-in for the running bots' room membership reader, which a test changes as the stream runs."""

    def __init__(self) -> None:
        self.members = {ROOM: frozenset({ALICE})}
        self.error: HTTPException | None = None
        self.hang = False
        self.calls = 0
        # Runs while a lookup is answered, as a sign-out that lands during a slow Matrix call.
        self.during: Callable[[], None] | None = None

    async def __call__(self, room_id: str) -> frozenset[str]:
        self.calls += 1
        if self.during is not None:
            self.during()
        if self.error is not None:
            raise self.error
        if self.hang:
            await asyncio.Event().wait()
        return self.members.get(room_id, frozenset())


class _Clock:
    """Wall-clock time shifted by ``offset`` seconds, so a test can bring a session's expiry close."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    """API app serving the demo plugin's websocket routes and client sessions, with the client origin allowed."""
    probe = ModuleType(PROBE_MODULE)
    probe.after_block = []
    probe.admit = None
    sys.modules[PROBE_MODULE] = probe
    _write_plugin(tmp_path, ROUTES)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_COMPUTER_ALLOWED_ORIGINS": json.dumps([CLIENT_ORIGIN]),
        },
    )
    config = bind_runtime_paths(Config(plugins=["./plugins/demo"]), runtime_paths)
    publish_test_plugins(config, runtime_paths)
    app = _app(config, runtime_paths)
    app.include_router(client_sessions.router)
    state = ensure_app_state(app)
    state.client_sessions = ClientSessionStore(clock=_Clock())
    state.room_members = _Membership()
    return app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """Test client of ``app``."""
    with TestClient(app) as test_client:
        yield test_client


def _membership(app: FastAPI) -> _Membership:
    membership = ensure_app_state(app).room_members
    assert isinstance(membership, _Membership)
    return membership


def _echo_path(room_id: str = ROOM) -> str:
    return f"/api/plugins/demo/rooms/{quote(room_id, safe='')}/echo"


def _admits_path(room_id: str = ROOM) -> str:
    return f"/api/plugins/demo/rooms/{quote(room_id, safe='')}/admits"


def _admit_with(check: Callable[[ClientUser], object], *, asynchronous: bool = False) -> None:
    """Make ``check``, wrapped as a coroutine function when ``asynchronous``, the admission check at ``_admits_path``."""

    async def async_check(user: ClientUser) -> object:
        return check(user)

    sys.modules[PROBE_MODULE].admit = async_check if asynchronous else check


def _offers(token: str) -> list[str]:
    return ["mindroom-session", f"mindroom-session.{token}"]


def _connect(client: TestClient, token: str, path: str | None = None) -> WebSocketTestSession:
    """Open ``path``, the echo route in the room by default, as a browser does from the allowed origin."""
    return client.websocket_connect(path or _echo_path(), _offers(token), headers={"origin": CLIENT_ORIGIN})


def _revoke(client: TestClient, token: str) -> None:
    """Sign out as a client does, so the revocation runs on the app's event loop."""
    assert client.delete("/api/client-sessions", headers={"Authorization": f"Bearer {token}"}).status_code == 204


@asynccontextmanager
async def _serving(app: FastAPI) -> AsyncIterator[int]:
    """Serve ``app`` on a free local port with the websocket transport `mindroom run` uses, and yield the port."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, ws="websockets-sansio", log_level="critical"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        await serving


def _denial(client: TestClient, path: str, protocols: list[str], headers: dict[str, str]) -> WebSocketDenialResponse:
    with pytest.raises(WebSocketDenialResponse) as denied, client.websocket_connect(path, protocols, headers=headers):
        pass
    return denied.value


def test_an_allowed_handshake_is_accepted_with_the_session_protocol_and_never_echoes_the_token(
    app: FastAPI,
    client: TestClient,
) -> None:
    """The route gets the session's user, and the token is in no header of the accepting answer."""
    token = client_session_store(app).create(ALICE).token

    with _connect(client, token) as websocket:
        websocket.send_text("hi")
        assert websocket.receive_text() == f"{ALICE}: hi"
        assert websocket.accepted_subprotocol == "mindroom-session"
        assert all(token not in value for _, value in websocket.extra_headers)


@pytest.mark.parametrize(
    ("offers", "origin", "room_id", "status", "detail"),
    [
        (lambda _token: ["mindroom-session"], CLIENT_ORIGIN, ROOM, 401, NO_SESSION),
        (lambda token: [*_offers(token), f"mindroom-session.{token}x"], CLIENT_ORIGIN, ROOM, 401, NO_SESSION),
        (lambda token: [f"mindroom-session.{token}"], CLIENT_ORIGIN, ROOM, 401, NO_SESSION),
        (lambda _token: _offers("unknown"), CLIENT_ORIGIN, ROOM, 401, NO_SESSION),
        (lambda _token: ["mindroom-session", "mindroom-session."], CLIENT_ORIGIN, ROOM, 401, NO_SESSION),
        (_offers, None, ROOM, 403, "Origin is not allowed."),
        (_offers, "https://evil.example.org", ROOM, 403, "Origin is not allowed."),
        (_offers, CLIENT_ORIGIN, "!other:example.org", 403, "Not a member of this room."),
    ],
)
def test_a_handshake_is_refused_before_accept(
    app: FastAPI,
    client: TestClient,
    offers: Callable[[str], list[str]],
    origin: str | None,
    room_id: str,
    status: int,
    detail: str,
) -> None:
    """A wrong offer, an unknown token, a missing or unlisted origin, or a non-member gets an HTTP answer."""
    token = client_session_store(app).create(ALICE).token
    headers = {"origin": origin} if origin is not None else {}

    denial = _denial(client, _echo_path(room_id), offers(token), headers)

    assert denial.status_code == status
    assert denial.json()["detail"] == detail
    assert token not in denial.text


def test_an_expired_or_revoked_session_is_refused_with_401(app: FastAPI, client: TestClient) -> None:
    """Only a live session signs a websocket in, and an Authorization header never does."""
    store = client_session_store(app)
    revoked = store.create(ALICE).token
    store.revoke(revoked)
    expired = store.create(ALICE).token
    assert isinstance(store.clock, _Clock)
    store.clock.offset = 1800
    live = store.create(ALICE).token
    # Expires the earlier session only after the later one was created, so the handshake itself finds it expired.
    store.clock.offset = 3600
    headers = {"origin": CLIENT_ORIGIN}

    for token in (revoked, expired):
        assert _denial(client, _echo_path(), _offers(token), headers).status_code == 401
    denial = _denial(client, _echo_path(), [], {**headers, "Authorization": f"Bearer {live}"})
    assert denial.status_code == 401


@pytest.mark.parametrize("during", ["membership", "admission"])
@pytest.mark.parametrize("ending", ["revoked", "expired"])
def test_a_session_that_ends_during_the_handshakes_checks_is_refused_with_401(
    app: FastAPI,
    client: TestClient,
    ending: str,
    during: str,
) -> None:
    """The handshake never accepts a session that ended while the room's membership was read or the route's check ran."""
    store = client_session_store(app)
    token = store.create(ALICE).token
    assert isinstance(store.clock, _Clock)
    clock = store.clock

    def end() -> None:
        if ending == "revoked":
            store.revoke(token)
        else:
            clock.offset = 3600

    if during == "membership":
        _membership(app).during = end
    else:

        async def check(_user: ClientUser) -> bool:
            end()
            return True

        _admit_with(check)

    denial = _denial(client, _admits_path(), _offers(token), {"origin": CLIENT_ORIGIN})

    assert (denial.status_code, denial.json()) == (401, {"detail": NO_SESSION})


@pytest.mark.parametrize(
    ("unavailable", "detail"),
    [
        ("unbound", "Room membership is unavailable."),
        ("failing", "Room membership lookup failed; try again."),
    ],
)
def test_unavailable_membership_is_refused_with_503(
    app: FastAPI,
    client: TestClient,
    unavailable: str,
    detail: str,
) -> None:
    """Without a membership answer the stream is refused rather than let through."""
    token = client_session_store(app).create(ALICE).token
    if unavailable == "unbound":
        ensure_app_state(app).room_members = None
    else:
        _membership(app).error = HTTPException(503, detail)

    denial = _denial(client, _echo_path(), _offers(token), {"origin": CLIENT_ORIGIN})

    assert (denial.status_code, denial.json()) == (503, {"detail": detail})


def _answering(answer: bool, asked: list[ClientUser]) -> Callable[[ClientUser], bool]:
    """Return an admission check that records each user it is asked about and answers ``answer``."""

    def check(user: ClientUser) -> bool:
        asked.append(user)
        return answer

    return check


@pytest.mark.parametrize("asynchronous", [False, True])
def test_an_admitting_check_is_asked_about_the_signed_in_user_and_the_stream_opens(
    app: FastAPI,
    client: TestClient,
    asynchronous: bool,
) -> None:
    """A route's check, sync or async, is asked once about the session's user, and the stream then runs as without one."""
    asked: list[ClientUser] = []
    _admit_with(_answering(True, asked), asynchronous=asynchronous)
    token = client_session_store(app).create(ALICE).token

    with _connect(client, token, _admits_path()) as websocket:
        assert websocket.receive_text() == ALICE
        assert websocket.accepted_subprotocol == "mindroom-session"
        _revoke(client, token)
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == 4401
    assert asked == [ClientUser(ALICE)]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_refusing_check_refuses_the_handshake_with_503(
    app: FastAPI,
    client: TestClient,
    asynchronous: bool,
) -> None:
    """The route's refusal answers the handshake before accept, so the client never sees the stream open."""
    asked: list[ClientUser] = []
    _admit_with(_answering(False, asked), asynchronous=asynchronous)
    token = client_session_store(app).create(ALICE).token

    denial = _denial(client, _admits_path(), _offers(token), {"origin": CLIENT_ORIGIN})

    assert (denial.status_code, denial.json()) == (503, {"detail": REFUSED})
    assert asked == [ClientUser(ALICE)]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_check_may_refuse_with_its_own_http_answer(app: FastAPI, client: TestClient, asynchronous: bool) -> None:
    """A check that raises `HTTPException` refuses the handshake with that answer instead of 503."""

    def check(_user: ClientUser) -> bool:
        raise HTTPException(429, "Too many streams.")

    _admit_with(check, asynchronous=asynchronous)
    token = client_session_store(app).create(ALICE).token

    denial = _denial(client, _admits_path(), _offers(token), {"origin": CLIENT_ORIGIN})

    assert (denial.status_code, denial.json()) == (429, {"detail": "Too many streams."})


def test_a_check_is_asked_only_once_the_handshakes_own_checks_pass(app: FastAPI, client: TestClient) -> None:
    """A caller from another origin, without a live session, or outside the room never reaches the route's check."""
    asked: list[ClientUser] = []
    _admit_with(_answering(True, asked))
    token = client_session_store(app).create(ALICE).token

    for path, protocols, origin, status in (
        (_admits_path(), _offers(token), "https://evil.example.org", 403),
        (_admits_path(), ["mindroom-session"], CLIENT_ORIGIN, 401),
        (_admits_path("!other:example.org"), _offers(token), CLIENT_ORIGIN, 403),
    ):
        assert _denial(client, path, protocols, {"origin": origin}).status_code == status

    assert asked == []


def test_http_routes_still_sign_in_with_the_authorization_header_only(app: FastAPI, client: TestClient) -> None:
    """A request offering the websocket subprotocols is not signed in by them."""
    token = client_session_store(app).create(ALICE).token

    offered = client.get("/api/plugins/demo/whoami", headers={"Sec-WebSocket-Protocol": ", ".join(_offers(token))})
    bearer = client.get("/api/plugins/demo/whoami", headers={"Authorization": f"Bearer {token}"})

    assert offered.status_code == 401
    assert (bearer.status_code, bearer.json()) == (200, {"user_id": ALICE})


def test_revoking_the_session_closes_the_stream_at_once_with_4401(app: FastAPI, client: TestClient) -> None:
    """`DELETE /api/client-sessions` wakes the stream, long before its expiry or a membership recheck."""
    token = client_session_store(app).create(ALICE).token

    with _connect(client, token) as websocket:
        websocket.send_text("hi")
        assert websocket.receive_text() == f"{ALICE}: hi"
        started = time.monotonic()
        _revoke(client, token)
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == 4401
    assert time.monotonic() - started < 5


def test_an_expiring_session_closes_the_stream_with_4401(app: FastAPI, client: TestClient) -> None:
    """The stream ends when its session's hour is up."""
    store = client_session_store(app)
    token = store.create(ALICE).token
    assert isinstance(store.clock, _Clock)
    store.clock.offset = 3600 - 1

    with _connect(client, token, "/api/plugins/demo/waits") as ws:
        assert ws.receive_text() == ALICE
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()

    assert (closed.value.code, closed.value.reason) == (4401, "Client session ended; sign in again.")


def test_a_closed_stream_cancels_a_block_waiting_on_something_else(app: FastAPI, client: TestClient) -> None:
    """A route waiting on anything but the websocket is cancelled there, and runs on after its block."""
    token = client_session_store(app).create(ALICE).token

    with _connect(client, token, "/api/plugins/demo/waits") as ws:
        assert ws.receive_text() == ALICE
        _revoke(client, token)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()

    assert closed.value.code == 4401
    assert sys.modules[PROBE_MODULE].after_block == [ALICE]


def test_leaving_the_room_closes_the_stream_with_1008(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A periodic recheck that finds the user gone from the room ends the stream."""
    monkeypatch.setattr(plugin_http, "_MEMBERSHIP_RECHECK_SECONDS", 0.05)
    token = client_session_store(app).create(ALICE).token
    membership = _membership(app)

    with _connect(client, token) as websocket:
        websocket.send_text("hi")
        assert websocket.receive_text() == f"{ALICE}: hi"
        membership.members = {}
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert (closed.value.code, closed.value.reason) == (1008, "Not a member of this room.")


@pytest.mark.parametrize("failure", ["error", "unbound"])
def test_a_failed_recheck_closes_the_stream_with_1013(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A recheck that fails or finds membership unavailable ends the stream with try-again-later."""
    monkeypatch.setattr(plugin_http, "_MEMBERSHIP_RECHECK_SECONDS", 0.05)
    token = client_session_store(app).create(ALICE).token
    membership = _membership(app)

    with _connect(client, token) as websocket:
        websocket.send_text("hi")
        assert websocket.receive_text() == f"{ALICE}: hi"
        if failure == "error":
            membership.error = HTTPException(503, "Room membership lookup failed; try again.")
        else:
            ensure_app_state(app).room_members = None
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == 1013


def test_a_stream_without_a_room_never_rechecks_membership(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a stream given a room reads membership, and one woken for an expiry the wall clock then moved stays open."""
    monkeypatch.setattr(plugin_http, "_MEMBERSHIP_RECHECK_SECONDS", 0.01)
    store = client_session_store(app)
    token = store.create(ALICE).token
    assert isinstance(store.clock, _Clock)
    store.clock.offset = 3600 - 1

    with _connect(client, token, "/api/plugins/demo/echo") as ws:
        store.clock.offset = 0
        time.sleep(1.5)
        ws.send_text("still here")
        assert ws.receive_text() == f"{ALICE}: still here"
        _revoke(client, token)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()

    assert closed.value.code == 4401
    assert _membership(app).calls == 0


def test_revoking_the_session_during_a_membership_recheck_closes_the_stream_at_once(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recheck that is still waiting for its answer does not hold the revoked session's stream open."""
    monkeypatch.setattr(plugin_http, "_MEMBERSHIP_RECHECK_SECONDS", 0.01)
    token = client_session_store(app).create(ALICE).token
    membership = _membership(app)

    with _connect(client, token) as websocket:
        websocket.send_text("hi")
        assert websocket.receive_text() == f"{ALICE}: hi"
        membership.hang = True
        calls = membership.calls
        while membership.calls == calls:
            time.sleep(0.01)
        started = time.monotonic()
        _revoke(client, token)
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()

    assert closed.value.code == 4401
    assert time.monotonic() - started < 5


def test_a_blocks_own_timeout_reaches_the_route(app: FastAPI, client: TestClient) -> None:
    """A `TimeoutError` the block raises is the route's to handle, not taken for the stream's end."""
    token = client_session_store(app).create(ALICE).token

    with _connect(client, token, "/api/plugins/demo/times-out") as ws:
        assert ws.receive_text() == "timed out"


def test_a_stream_that_ends_stops_its_membership_rechecks(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the route's block ends, nothing keeps reading the room's membership for it."""
    monkeypatch.setattr(plugin_http, "_MEMBERSHIP_RECHECK_SECONDS", 0.01)
    token = client_session_store(app).create(ALICE).token
    membership = _membership(app)

    with _connect(client, token) as websocket:
        websocket.send_text("hi")
        assert websocket.receive_text() == f"{ALICE}: hi"
        while membership.calls < 2:
            time.sleep(0.01)
    calls = membership.calls
    time.sleep(0.1)

    assert membership.calls == calls


async def _handshake(port: int, path: str, origin: str, protocols: str) -> tuple[list[str], bytes]:
    """Send one raw websocket handshake and return the answer's status and header lines, and its body."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            (
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
                f"Origin: {origin}\r\nSec-WebSocket-Protocol: {protocols}\r\n\r\n"
            ).encode(),
        )
        await writer.drain()
        async with asyncio.timeout(5):
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().removesuffix("\r\n\r\n").split("\r\n")
            body = b"" if lines[0].split()[1] == "101" else await reader.read()
    finally:
        writer.close()
    return lines, body


def _assert_well_formed_denial(lines: list[str], body: bytes, status: int) -> None:
    """A denial carries one Content-Length matching its body and one Content-Type, so clients can read its status."""
    assert lines[0].split()[1] == str(status)
    lengths = [line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("content-length:")]
    assert lengths == [str(len(body))]
    assert len([line for line in lines[1:] if line.lower().startswith("content-type:")]) == 1
    assert "detail" in json.loads(body)


@pytest.mark.asyncio
async def test_the_embedded_servers_transport_accepts_and_refuses_well_formed(app: FastAPI) -> None:
    """Under the websocket transport `mindroom run` uses, the handshake and every refusal are parseable HTTP."""
    token = client_session_store(app).create(ALICE).token
    _admit_with(lambda _user: False)
    async with _serving(app) as port:
        offers = ", ".join(_offers(token))
        lines, _ = await _handshake(port, _echo_path(), CLIENT_ORIGIN, offers)
        assert lines[0].split()[1] == "101"
        assert "sec-websocket-protocol: mindroom-session" in [line.lower() for line in lines]
        assert all(token not in line for line in lines)

        for path, origin, protocols, status in (
            (_echo_path(), CLIENT_ORIGIN, "mindroom-session", 401),
            (_echo_path(), "https://evil.example.org", offers, 403),
            (_echo_path("!other:example.org"), CLIENT_ORIGIN, offers, 403),
            (_admits_path(), CLIENT_ORIGIN, offers, 503),
            ("/api/plugins/nope/ws", CLIENT_ORIGIN, offers, 404),
        ):
            lines, body = await _handshake(port, path, origin, protocols)
            _assert_well_formed_denial(lines, body, status)
            assert token.encode() not in body


@pytest.mark.asyncio
async def test_a_failing_check_refuses_with_500_on_the_embedded_servers_transport(app: FastAPI) -> None:
    """A check that fails answers the handshake as any route that raises does, and never accepts."""

    async def check(_user: ClientUser) -> bool:
        msg = "check failed"
        raise RuntimeError(msg)

    _admit_with(check)
    token = client_session_store(app).create(ALICE).token
    async with _serving(app) as port:
        lines, _ = await _handshake(port, _admits_path(), CLIENT_ORIGIN, ", ".join(_offers(token)))

    assert lines[0].split()[1] == "500"


@pytest.mark.asyncio
async def test_a_stream_whose_client_left_ends_cleanly_on_the_embedded_servers_transport(app: FastAPI) -> None:
    """A stream that never read its client's goodbye still ends cleanly at revocation, and runs on after its block."""
    store = client_session_store(app)
    token = store.create(ALICE).token
    async with _serving(app) as port, aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"http://127.0.0.1:{port}/api/plugins/demo/waits",
            protocols=_offers(token),
            origin=CLIENT_ORIGIN,
        ) as websocket:
            assert websocket.protocol == "mindroom-session"
            assert await websocket.receive_str() == ALICE
        store.revoke(token)
        async with asyncio.timeout(10):
            while sys.modules[PROBE_MODULE].after_block != [ALICE]:  # noqa: ASYNC110 - the plugin route signals no event
                await asyncio.sleep(0.01)
