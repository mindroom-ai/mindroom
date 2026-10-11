"""Real TCP handshakes cover the normal runtime's pinned WebSocket transport."""

import asyncio
import json
import socket
from pathlib import Path
from typing import Literal

import aiohttp
import pytest
import uvicorn

from mindroom.api import computers
from tests.computer_helpers import ComputerPeer, computer_app


async def _assert_denial(port: int, path: str, protocols: str, status: int, *, allowed_origin: bool = True) -> None:
    """Inspect real HTTP refusal framing, content and credential redaction."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    request_origin = "https://chat.example.org" if allowed_origin else "https://evil.example.org"
    request = (
        f"GET {path}/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
        f"Origin: {request_origin}\r\nSec-WebSocket-Protocol: {protocols}\r\n\r\n"
    )
    try:
        writer.write(request.encode())
        await writer.drain()
        async with asyncio.timeout(5):
            wire = await reader.read()
    finally:
        writer.close()
        await writer.wait_closed()
    head, body = wire.split(b"\r\n\r\n", 1)
    lines = head.decode().split("\r\n")
    assert lines[0].split()[1] == str(status)
    lengths = [line.split(":", 1)[1].strip() for line in lines[1:] if line.lower().startswith("content-length:")]
    assert lengths == [str(len(body))]
    types = [line for line in lines[1:] if line.lower().startswith("content-type:")]
    assert len(types) == 1
    assert "text/plain" in types[0]
    assert "cache-control: no-store" in head.decode().lower()
    assert "detail" in json.loads(body)
    assert "mindroom-ticket." not in body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend",
    [
        "websockets-sansio",
        pytest.param(
            "websockets",
            marks=[
                pytest.mark.filterwarnings(
                    r"ignore:websockets\.legacy is deprecated.*:DeprecationWarning:websockets\.legacy",
                ),
                pytest.mark.filterwarnings(
                    r"ignore:websockets\.server\.WebSocketServerProtocol is deprecated:DeprecationWarning:uvicorn\.protocols\.websockets\.websockets_impl",
                ),
                pytest.mark.filterwarnings(
                    r"ignore:remove second argument of ws_handler:DeprecationWarning:websockets\.legacy\.server",
                ),
            ],
        ),
    ],
)
async def test_computer_ticket_handshake_and_denial_framing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: Literal["websockets-sansio", "websockets"],
) -> None:
    """Standard comma-separated offers authenticate; denials remain parseable HTTP."""
    peer = ComputerPeer()
    app = computer_app(peer, tmp_path)
    monkeypatch.setattr(computers, "_resolve_worker", lambda *_args, **_kwargs: peer.handle)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(app, ws=backend, log_level="error"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        async with aiohttp.ClientSession() as client:
            async with client.post(
                origin + "/api/computers/sessions",
                json={
                    "openid_token": {
                        "access_token": "openid-secret",
                        "token_type": "Bearer",
                        "matrix_server_name": "example.org",
                        "expires_in": 60,
                    },
                    "room_id": "!room:example.org",
                    "agent_user_id": "@agent:example.org",
                },
            ) as response:
                assert response.status == 200
                session = await response.json()
            path = "/api/computers/sessions/" + session["session_id"]
            headers = {"Authorization": "Bearer " + session["session_token"]}

            async def ticket() -> str:
                async with client.post(origin + path + "/stream-ticket", headers=headers) as response:
                    assert response.status == 200
                    return "mindroom-ticket." + (await response.json())["ticket"]

            # A denial before any valid offer also catches duplicate transport framing.
            await _assert_denial(port, path, "binary", 401)
            first = await ticket()
            await _assert_denial(port, path, f"binary, {first}", 403, allowed_origin=False)
            await _assert_denial(port, path, f"binary, {first}, {first}", 401)
            await _assert_denial(port, path, first, 401)
            async with client.ws_connect(
                origin + path + "/stream",
                protocols=["binary", first],
                origin="https://chat.example.org",
            ) as websocket:
                assert websocket.protocol == "binary"
                assert await websocket.receive_bytes() == b"screen"
            await _assert_denial(port, path, f"binary, {first}", 401)
            expired = await ticket()
            peer.now += 31
            await _assert_denial(port, path, f"binary, {expired}", 401)
    finally:
        server.should_exit = True
        await serving
        listener.close()
