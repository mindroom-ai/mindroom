"""Chat clients reach the terminals of the agents they may use in a room, and type in them only while they may."""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
import nio
import pytest
import uvicorn

from mindroom.api import agent_terminals
from mindroom.api.client_sessions import client_session_store
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from tests.api.test_plugin_room_agents import ALICE, BOB, ROOM, _Room
from tests.terminal.conftest import primary_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("enforce_turn_authorization")]

_ORIGIN = "https://chat.example.org"


def _agents(*users: str) -> dict[str, AgentConfig]:
    """Two agents with a shell, which ``users`` may use."""
    access = ResponderAccessConfig(users=list(users))
    return {
        "coder": AgentConfig(display_name="Coder", tools=["shell"], access=access),
        "other": AgentConfig(display_name="Other", tools=["shell"], access=access),
    }


def _paths(tmp_path: Path, runner: str) -> RuntimePaths:
    """A primary whose shared runner mounts the agents' storage and runs their shell commands, with the client origin."""
    (tmp_path / "config.yaml").write_text("router:\n  model: default\n", encoding="utf-8")
    return primary_paths(
        tmp_path,
        runner,
        MATRIX_HOMESERVER="http://localhost:8008",
        MINDROOM_NAMESPACE="",
        MINDROOM_COMPUTER_ALLOWED_ORIGINS=json.dumps([_ORIGIN]),
        MINDROOM_STATIC_RUNNER_AGENT_STORAGE="true",
    )


@asynccontextmanager
async def _served(room: _Room) -> AsyncIterator[tuple[aiohttp.ClientSession, str]]:
    """Serve the room's API with the terminal routes; yield a client and the base URL of the room's routes."""
    room.app.include_router(agent_terminals.router)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(room.app, ws="websockets-sansio", log_level="critical"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if serving.done():
                    await serving
                await asyncio.sleep(0.01)
        async with aiohttp.ClientSession() as client:
            yield client, f"http://127.0.0.1:{listener.getsockname()[1]}/api/rooms/{quote(ROOM, safe='')}"
    finally:
        server.should_exit = True
        await serving


def _token(room: _Room, user_id: str) -> str:
    return client_session_store(room.app).create(user_id).token


def _auth(room: _Room, user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(room, user_id)}"}


async def _answer(response: aiohttp.ClientResponse) -> tuple[int, Any]:
    async with response:
        return response.status, await response.json() if response.content_type == "application/json" else None


async def _stream(
    client: aiohttp.ClientSession,
    room: _Room,
    url: str,
    user_id: str,
) -> aiohttp.ClientWebSocketResponse:
    token = _token(room, user_id)
    return await client.ws_connect(url, protocols=["mindroom-session", f"mindroom-session.{token}"], origin=_ORIGIN)


async def _read_until(stream: aiohttp.ClientWebSocketResponse, marker: bytes) -> bytes:
    seen = b""
    async with asyncio.timeout(10):
        while marker not in seen:
            message = await stream.receive()
            assert message.type == aiohttp.WSMsgType.BINARY, message
            seen += message.data
    return seen


async def test_the_routes_need_a_client_session(tmp_path: Path, runner: str) -> None:
    """Without a session nothing answers but 401."""
    room = _Room(Config(agents=_agents(ALICE)), _paths(tmp_path, runner), ("coder",))
    async with _served(room) as (client, base):
        assert (await _answer(await client.get(f"{base}/terminals")))[0] == 401
        assert (await _answer(await client.post(f"{base}/agents/{room.ids['coder']}/terminals")))[0] == 401


async def test_people_who_may_post_open_close_and_list_terminals_and_others_only_watch(
    tmp_path: Path,
    runner: str,
) -> None:
    """Bob, whom the power levels keep from posting, sees Alice's terminal but may neither open nor close one."""
    room = _Room(Config(agents=_agents(ALICE, BOB)), _paths(tmp_path, runner), ("coder", "other"))
    coder = f"agents/{room.ids['coder']}/terminals"
    async with _served(room) as (client, base):
        opened_status, opened = await _answer(
            await client.post(f"{base}/{coder}", json={"cols": 100, "rows": 30}, headers=_auth(room, ALICE)),
        )
        assert opened_status == 201
        assert (opened["agent_user_id"], opened["cols"], opened["rows"], opened["alive"], opened["writable"]) == (
            room.ids["coder"],
            100,
            30,
            True,
            True,
        )
        other_status, other = await _answer(
            await client.post(f"{base}/agents/{room.ids['other']}/terminals", headers=_auth(room, ALICE)),
        )
        assert other_status == 201

        _, alice_sees = await _answer(await client.get(f"{base}/terminals", headers=_auth(room, ALICE)))
        _, bob_sees = await _answer(await client.get(f"{base}/terminals", headers=_auth(room, BOB)))
        assert [(t["id"], t["writable"]) for t in alice_sees["terminals"]] == [
            (opened["id"], True),
            (other["id"], True),
        ]
        assert [(t["id"], t["writable"]) for t in bob_sees["terminals"]] == [
            (opened["id"], False),
            (other["id"], False),
        ]

        for refused in (
            client.post(f"{base}/{coder}", headers=_auth(room, BOB)),
            client.delete(f"{base}/{coder}/{opened['id']}", headers=_auth(room, BOB)),
        ):
            assert (await _answer(await refused))[0] == 403
        # Another workspace's terminal is no terminal of this one.
        assert (await _answer(await client.delete(f"{base}/{coder}/{other['id']}", headers=_auth(room, ALICE))))[
            0
        ] == 404
        assert (await _answer(await client.delete(f"{base}/{coder}/{opened['id']}", headers=_auth(room, ALICE))))[
            0
        ] == 204
        _, after = await _answer(await client.get(f"{base}/terminals", headers=_auth(room, ALICE)))
        assert [t["id"] for t in after["terminals"]] == [other["id"]]


async def test_a_stream_relays_the_terminal_and_follows_what_its_viewer_may_do(
    tmp_path: Path,
    runner: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bob's keys never reach the shell; Alice's stream turns read-only, then closes, as she loses what she may do.

    It turns read-only once she can't post, and closes once a config save takes the agent from her.
    """
    monkeypatch.setattr(agent_terminals, "_RECHECK_SECONDS", 0.05)
    room = _Room(Config(agents=_agents(ALICE, BOB)), _paths(tmp_path, runner), ("coder",))
    coder = f"agents/{room.ids['coder']}/terminals"
    async with _served(room) as (client, base):
        _, opened = await _answer(await client.post(f"{base}/{coder}", headers=_auth(room, ALICE)))
        url = f"{base}/{coder}/{opened['id']}"
        alice = await _stream(client, room, url, ALICE)
        bob = await _stream(client, room, url, BOB)
        assert await alice.receive_json() == {"type": "writable", "writable": True}
        assert await bob.receive_json() == {"type": "writable", "writable": False}

        await bob.send_json({"type": "input", "data": "echo from-$((2*3))-bob\r"})
        await alice.send_json({"type": "input", "data": "echo from-$((2*3))-alice\r"})
        assert b"from-6-bob" not in await _read_until(alice, b"from-6-alice")

        power_levels = {"users": {ALICE: 0}, "events_default": 10}
        room.client.room_get_state.return_value = nio.RoomGetStateResponse(
            [
                {"type": "m.room.create", "state_key": "", "sender": ALICE, "content": {"room_version": "10"}},
                {"type": "m.room.power_levels", "state_key": "", "sender": ALICE, "content": power_levels},
            ],
            ROOM,
        )
        async with asyncio.timeout(10):
            while (message := await alice.receive()).type == aiohttp.WSMsgType.BINARY:
                pass
        assert json.loads(message.data) == {"type": "writable", "writable": False}

        room.serve(Config(agents=_agents(BOB)))
        async with asyncio.timeout(10):
            while (message := await alice.receive()).type == aiohttp.WSMsgType.BINARY:
                pass
        assert (message.type, alice.close_code) == (aiohttp.WSMsgType.CLOSE, 1008)
        await bob.close()


async def test_a_stream_of_a_terminal_the_workspace_does_not_have_closes(tmp_path: Path, runner: str) -> None:
    """An ID that names no terminal of the workspace closes the stream as refused, saying nothing of other terminals."""
    room = _Room(Config(agents=_agents(ALICE)), _paths(tmp_path, runner), ("coder",))
    async with _served(room) as (client, base):
        stream = await _stream(client, room, f"{base}/agents/{room.ids['coder']}/terminals/nope", ALICE)
        assert (await stream.receive()).type == aiohttp.WSMsgType.CLOSE
        assert stream.close_code == 1008
