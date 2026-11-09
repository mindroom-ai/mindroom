"""Chat clients list the workspaces a room opens to them, and read and change their files as the room lets them.

Alice may post and use both agents, Bob may use both but not post, and Carol may use only the private ``notes``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp
import nio
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mindroom.api import main, workspace_files
from mindroom.api.client_sessions import client_session_store
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.workspace_files import operations, watch
from tests.access_schema_support import joined_members_response
from tests.api.test_plugin_room_agents import ALICE, BOB, CAROL, ROOM, _agents, _Room, _room, _turn_root
from tests.conftest import test_runtime_paths
from tests.test_plugin_client_stream import _serving

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from httpx import Response

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

BASE = f"/api/rooms/{ROOM}"
WORKSPACES = f"{BASE}/workspaces"
ORIGIN = "https://chat.example.org"


class _Client:
    """Each person's requests to the routes, each signed in with a fresh client session of theirs."""

    def __init__(self, room: _Room) -> None:
        room.app.include_router(workspace_files.router)
        self.room = room
        self.http = TestClient(room.app)

    def _headers(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {client_session_store(self.room.app).create(user_id).token}"}

    def _url(self, agent: str, route: str) -> str:
        return f"{BASE}/agents/{self.room.ids[agent]}/workspace/{route}"

    def workspaces(self, user_id: str) -> Response:
        return self.http.get(WORKSPACES, headers=self._headers(user_id))

    def get(self, user_id: str, agent: str, route: str, **params: str) -> Response:
        return self.http.get(self._url(agent, route), params=params, headers=self._headers(user_id))

    def put(self, user_id: str, agent: str, route: str, body: dict[str, object]) -> Response:
        return self.http.put(self._url(agent, route), json=body, headers=self._headers(user_id))

    def post(self, user_id: str, agent: str, route: str, body: dict[str, object]) -> Response:
        return self.http.post(self._url(agent, route), json=body, headers=self._headers(user_id))

    def delete(self, user_id: str, agent: str, route: str, **params: str) -> Response:
        return self.http.delete(self._url(agent, route), params=params, headers=self._headers(user_id))


@pytest.fixture
def client(tmp_path: Path) -> _Client:
    """Alice, Bob and Carol in a room with ``coder`` and ``notes``."""
    return _Client(_room(tmp_path, _agents()))


def _etag(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_every_route_needs_a_client_session(client: _Client) -> None:
    """Without a session the routes answer 401 before reading the room."""
    coder = client.room.ids["coder"]

    assert client.http.get(WORKSPACES).status_code == 401
    assert client.http.get(f"{BASE}/agents/{coder}/workspace/folder").status_code == 401
    assert (
        client.http.put(f"{BASE}/agents/{coder}/workspace/file", json={"path": "a", "content": ""}).status_code == 401
    )


def test_the_room_lists_the_workspaces_each_caller_may_open_and_what_they_may_do(client: _Client) -> None:
    """Writing needs power to post; the private agent's files only read; Carol sees only what she may use."""
    ids = client.room.ids
    coder = {"agent_user_id": ids["coder"], "display_name": "Coder", "private": False}
    notes = {"agent_user_id": ids["notes"], "display_name": "Notes", "private": True}

    assert client.workspaces(ALICE).json() == {
        "workspaces": [coder | {"files": "write"}, notes | {"files": "read"}],
    }
    assert client.workspaces(BOB).json() == {
        "workspaces": [coder | {"files": "read"}, notes | {"files": "read"}],
    }
    assert client.workspaces(CAROL).json() == {"workspaces": [notes | {"files": "read"}]}


def test_an_agent_whose_tools_reach_no_files_opens_no_workspace(tmp_path: Path) -> None:
    """Without a toolkit that takes paths or runs code, the agent is left out and its workspace answers 403."""
    client = _Client(_room(tmp_path, {"chat": AgentConfig(display_name="Chat", tools=[], access={"users": [ALICE]})}))

    assert client.workspaces(ALICE).json() == {"workspaces": []}
    assert client.get(ALICE, "chat", "folder").status_code == 403


def test_bob_reads_but_cannot_change_and_carol_cannot_open(client: _Client) -> None:
    """A caller who may not post reads (200) and is refused every change (403); one the agent refuses gets 403."""
    assert client.put(ALICE, "coder", "file", {"path": "plan.md", "content": "# Plan\n"}).status_code == 200

    assert client.get(BOB, "coder", "file", path="plan.md").json()["content"] == "# Plan\n"
    assert client.put(BOB, "coder", "file", {"path": "plan.md", "content": "# Bob\n"}).status_code == 403
    assert client.post(BOB, "coder", "folder", {"path": "docs"}).status_code == 403
    assert client.post(BOB, "coder", "move", {"path": "plan.md", "to": "old.md"}).status_code == 403
    assert client.delete(BOB, "coder", "entry", path="plan.md").status_code == 403
    assert client.get(CAROL, "coder", "file", path="plan.md").status_code == 403
    assert client.get(CAROL, "coder", "folder").status_code == 403
    assert client.get(ALICE, "coder", "file", path="plan.md").json()["content"] == "# Plan\n"


def test_a_save_before_the_agent_s_first_turn_makes_the_workspace_a_turn_would(tmp_path: Path) -> None:
    """The first save makes the private root this caller's turns resolve, then the file in it."""
    agents = {
        "diary": AgentConfig(
            display_name="Diary",
            tools=["file"],
            private=AgentPrivateConfig(per="user"),
            access={"users": [ALICE]},
        ),
    }
    client = _Client(_room(tmp_path, agents))
    root = _turn_root(client.room, "diary", ALICE)
    assert not root.exists()
    assert client.get(ALICE, "diary", "folder").json() == {
        "path": "",
        "entries": [],
        "truncated": False,
    }

    saved = client.put(ALICE, "diary", "file", {"path": "today/entry.md", "content": "Dear diary\n"})

    assert saved.status_code == 200
    assert saved.json()["etag"] == _etag(b"Dear diary\n")
    assert (root / "today" / "entry.md").read_text() == "Dear diary\n"
    assert _turn_root(client.room, "diary", ALICE) == root


def test_a_save_with_a_stale_etag_answers_409_with_the_current_one(client: _Client) -> None:
    """A save of a version someone replaced changes nothing and names the file's etag, null once it is gone."""
    first = client.put(ALICE, "coder", "file", {"path": "plan.md", "content": "one"}).json()["etag"]
    second = client.put(ALICE, "coder", "file", {"path": "plan.md", "content": "two", "etag": first})
    assert second.status_code == 200

    stale = client.put(ALICE, "coder", "file", {"path": "plan.md", "content": "three", "etag": first})

    assert stale.status_code == 409
    assert stale.json()["etag"] == _etag(b"two") == second.json()["etag"]
    assert client.get(ALICE, "coder", "file", path="plan.md").json()["content"] == "two"
    client.delete(ALICE, "coder", "entry", path="plan.md")
    gone = client.put(ALICE, "coder", "file", {"path": "plan.md", "content": "four", "etag": first})
    assert (gone.status_code, gone.json()["etag"]) == (409, None)


def test_files_travel_as_text_or_base64_both_ways(client: _Client) -> None:
    """Binary content saves and reads as base64, and content that is not base64 answers 400."""
    binary = bytes(range(256))
    encoded = base64.b64encode(binary).decode()

    saved = client.put(
        ALICE,
        "coder",
        "file",
        {"path": "data.bin", "content": encoded, "encoding": "base64"},
    )
    read = client.get(ALICE, "coder", "file", path="data.bin").json()

    assert saved.json()["size"] == 256
    assert (read["encoding"], read["content"], read["etag"]) == ("base64", encoded, _etag(binary))
    for not_base64 in ("@@", "é"):
        body = {"path": "x", "content": not_base64, "encoding": "base64"}
        assert client.put(ALICE, "coder", "file", body).status_code == 400


def test_a_save_over_the_limit_answers_413(client: _Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Content over the file size limit answers 413 before anything is written."""
    monkeypatch.setattr(operations, "_MAX_FILE_BYTES", 4)

    assert client.put(ALICE, "coder", "file", {"path": "big.txt", "content": "12345"}).status_code == 413
    assert client.put(ALICE, "coder", "file", {"path": "ok.txt", "content": "1234"}).status_code == 200


def test_folders_moves_and_deletes_change_the_workspace(client: _Client) -> None:
    """Making, moving and deleting answer 204, and a listing shows the result."""
    client.put(ALICE, "coder", "file", {"path": "src/main.py", "content": "print()\n"})

    assert client.post(ALICE, "coder", "folder", {"path": "docs/guide"}).status_code == 204
    assert client.post(ALICE, "coder", "folder", {"path": "docs/guide"}).status_code == 409
    assert client.post(ALICE, "coder", "move", {"path": "src", "to": "lib/src"}).status_code == 204
    assert client.delete(ALICE, "coder", "entry", path="docs").status_code == 204

    listing = client.get(ALICE, "coder", "folder", path="lib/src").json()
    assert [(entry["path"], entry["kind"], entry["size"]) for entry in listing["entries"]] == [
        ("lib/src/main.py", "file", 8),
    ]
    assert [entry["name"] for entry in client.get(ALICE, "coder", "folder").json()["entries"]] == [
        "lib",
    ]


def test_paths_that_leave_or_name_credentials_are_refused_through_the_routes(client: _Client) -> None:
    """``..`` answers 400, a credential 404 as an absent file, and Git's files and ``.config`` 403."""
    root = _turn_root(client.room, "coder", ALICE)
    (root / ".config" / "gh").mkdir(parents=True)
    (root / ".env").write_text("TOKEN=1\n")

    assert client.get(ALICE, "coder", "file", path="../config.yaml").status_code == 400
    assert client.get(ALICE, "coder", "file", path=".env").status_code == 404
    assert client.put(ALICE, "coder", "file", {"path": "repo/.git/config", "content": ""}).status_code == 403
    assert client.post(ALICE, "coder", "move", {"path": ".config", "to": "config"}).status_code == 403
    assert [entry["name"] for entry in client.get(ALICE, "coder", "folder").json()["entries"]] == [
        ".config",
    ]


def test_mindroom_serves_the_routes_to_chat_clients_on_other_origins(tmp_path: Path) -> None:
    """The API answers from the client origins the Computer allows, and nothing it answers is cached."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_COMPUTER_ALLOWED_ORIGINS": json.dumps([ORIGIN])},
    )
    main.initialize_api_app(main.app, paths)

    response = TestClient(main.app).get(WORKSPACES, headers={"Origin": ORIGIN})

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.headers["cache-control"] == "no-store"


def test_text_that_is_not_unicode_answers_400(client: _Client) -> None:
    """JSON may escape half of a surrogate pair, which no UTF-8 file can hold."""
    coder = client.room.ids["coder"]

    response = client.http.put(
        f"{BASE}/agents/{coder}/workspace/file",
        content='{"path": "half.txt", "content": "a\\ud800b"}',
        headers={**client._headers(ALICE), "Content-Type": "application/json"},
    )

    assert response.status_code == 400


def test_a_bridge_alias_saves_into_the_workspace_its_person_s_turns_use(tmp_path: Path) -> None:
    """A caller signed in as Alice's bridge alias makes and writes Alice's private workspace, not one of its own."""
    alias = "@telegram_alice:localhost"
    diary = AgentConfig(
        display_name="Diary",
        tools=["file"],
        private=AgentPrivateConfig(per="user"),
        access={"users": [ALICE]},
    )
    config = Config(agents={"diary": diary}, authorization={"aliases": {ALICE: [alias]}})
    room = _Room(config, test_runtime_paths(tmp_path), ("diary",))
    room.client.joined_members.return_value = joined_members_response(ROOM, alias, *room.ids.values())
    power_levels = {"type": "m.room.power_levels", "state_key": "", "sender": ALICE, "content": {"users": {alias: 50}}}
    room.client.room_get_state.return_value = nio.RoomGetStateResponse([power_levels], ROOM)
    client = _Client(room)

    saved = client.put(
        alias,
        "diary",
        "file",
        {"path": "entry.md", "content": "Dear diary\n"},
    )

    assert saved.status_code == 200
    assert (_turn_root(room, "diary", ALICE) / "entry.md").read_text() == "Dear diary\n"


def _stream_room(tmp_path: Path, agents: dict[str, AgentConfig] | None = None) -> _Room:
    """A room whose API admits streams from ``ORIGIN``, with ``coder`` and ``notes`` unless ``agents`` are given."""
    return _room(tmp_path, agents or _agents(), MINDROOM_COMPUTER_ALLOWED_ORIGINS=json.dumps([ORIGIN]))


@asynccontextmanager
async def _served(room: _Room) -> AsyncIterator[Callable[[str, str], Awaitable[aiohttp.ClientWebSocketResponse]]]:
    """Serve the room's API with the workspace routes; yield a call that opens one person's stream of an agent."""
    room.app.include_router(workspace_files.router)
    async with _serving(room.app) as port, aiohttp.ClientSession() as client:

        async def follow(user_id: str, agent: str) -> aiohttp.ClientWebSocketResponse:
            url = f"ws://127.0.0.1:{port}/api/rooms/{quote(ROOM, safe='')}/agents/{room.ids[agent]}/workspace/changes"
            token = client_session_store(room.app).create(user_id).token
            return await client.ws_connect(
                url,
                protocols=["mindroom-session", f"mindroom-session.{token}"],
                origin=ORIGIN,
            )

        yield follow


async def _ready(stream: aiohttp.ClientWebSocketResponse) -> None:
    """Wait for the stream to say it follows the workspace."""
    async with asyncio.timeout(10):
        assert await stream.receive_json() == {"type": "ready"}


async def _changed_until(stream: aiohttp.ClientWebSocketResponse, path: str) -> set[str]:
    """Collect the paths the stream reports until ``path`` shows, checking each batch is sorted."""
    seen: set[str] = set()
    async with asyncio.timeout(10):
        while path not in seen:
            message = await stream.receive_json()
            assert message["type"] == "changed"
            assert message["paths"] == sorted(message["paths"])
            seen.update(message["paths"])
    return seen


@pytest.mark.asyncio
async def test_the_change_stream_reports_what_changes_but_never_git_or_credentials(tmp_path: Path) -> None:
    """Bob, who may only read, follows the shared workspace and hears each changed path, sorted, in batches."""
    room = _stream_room(tmp_path)
    root = _turn_root(room, "coder", ALICE)
    (root / ".git").mkdir(parents=True)
    async with _served(room) as follow:
        stream = await follow(BOB, "coder")
        await _ready(stream)
        (root / ".git" / "index").write_text("x")
        (root / ".env").write_text("TOKEN=1\n")
        (root / "notes.md").write_text("# Notes\n")

        seen = await _changed_until(stream, "notes.md")

    assert not {path for path in seen if path.startswith((".git", ".env"))}


@pytest.mark.asyncio
async def test_the_change_stream_closes_once_the_workspace_is_no_longer_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Carol, whom the agent leaves out, is closed at once; Alice once a save takes the agent from her, Bob once it moves."""
    monkeypatch.setattr(workspace_files, "_RECHECK_SECONDS", 0.5)
    room = _stream_room(tmp_path)
    async with _served(room) as follow:
        carol = await follow(CAROL, "coder")
        assert ((await carol.receive()).type, carol.close_code) == (aiohttp.WSMsgType.CLOSE, 1008)

        alice = await follow(ALICE, "coder")
        bob = await follow(BOB, "coder")
        await _ready(alice)
        await _ready(bob)
        agents = _agents()
        agents["coder"].access = ResponderAccessConfig(users=[BOB])
        room.serve(Config(agents=agents))
        async with asyncio.timeout(10):
            message = await alice.receive()
        assert (message.type, alice.close_code) == (aiohttp.WSMsgType.CLOSE, 1008)

        # Bob keeps the agent, but once it is private his workspace is his own, not the one he follows.
        agents["coder"].private = AgentPrivateConfig(per="user")
        room.serve(Config(agents=agents))
        async with asyncio.timeout(10):
            message = await bob.receive()

    assert (message.type, bob.close_code) == (aiohttp.WSMsgType.CLOSE, 1008)


@pytest.mark.asyncio
async def test_the_change_stream_of_an_agent_whose_tools_reach_no_files_closes(tmp_path: Path) -> None:
    """An agent Alice may use but whose tools take no paths and run no code opens no workspace to follow."""
    chat = AgentConfig(display_name="Chat", tools=[], access={"users": [ALICE]})
    room = _stream_room(tmp_path, {"chat": chat})
    async with _served(room) as follow:
        stream = await follow(ALICE, "chat")
        # Before the first recheck: the stream never opens.
        async with asyncio.timeout(5):
            message = await stream.receive()

    assert (message.type, stream.close_code, message.extra) == (
        aiohttp.WSMsgType.CLOSE,
        1008,
        "This agent's tools reach no files.",
    )


@pytest.mark.asyncio
async def test_the_change_stream_closes_once_the_agent_s_tools_no_longer_reach_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alice may still use the agent, but once a config save takes its tools away there are no files to follow."""
    monkeypatch.setattr(workspace_files, "_RECHECK_SECONDS", 0.5)
    room = _stream_room(tmp_path)
    async with _served(room) as follow:
        stream = await follow(ALICE, "coder")
        await _ready(stream)
        agents = _agents()
        agents["coder"].tools = []
        room.serve(Config(agents=agents))
        async with asyncio.timeout(10):
            message = await stream.receive()

    assert (message.type, stream.close_code, message.extra) == (
        aiohttp.WSMsgType.CLOSE,
        1008,
        "This agent's tools reach no files.",
    )


@pytest.mark.asyncio
async def test_the_change_stream_closes_with_1013_while_access_cannot_be_told(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recheck that can't finish in time closes the stream for the client to try again."""
    monkeypatch.setattr(workspace_files, "_RECHECK_SECONDS", 0.5)
    room = _stream_room(tmp_path)
    root = _turn_root(room, "coder", ALICE)
    root.mkdir(parents=True)
    async with _served(room) as follow:
        stream = await follow(ALICE, "coder")
        # Admitted and following, so only a recheck reads the room from here on.
        await _ready(stream)
        stalled = asyncio.Event()

        async def stall(*_args: object, **_kwargs: object) -> None:
            await stalled.wait()

        room.client.joined_members.side_effect = stall
        async with asyncio.timeout(10):
            message = await stream.receive()
        stalled.set()

    assert (message.type, stream.close_code, message.extra) == (
        aiohttp.WSMsgType.CLOSE,
        1013,
        "Agent access is unavailable; try again.",
    )


@pytest.mark.asyncio
async def test_the_watcher_stops_once_the_client_leaves(tmp_path: Path) -> None:
    """A client that closes its stream releases the workspace's watcher at once, without waiting for a change."""
    room = _stream_room(tmp_path)
    root = _turn_root(room, "coder", ALICE)
    root.mkdir(parents=True)
    async with _served(room) as follow:
        stream = await follow(ALICE, "coder")
        await _ready(stream)
        assert list(watch._watches) == [root]

        await stream.close()
        async with asyncio.timeout(5):
            while watch._watches:  # noqa: ASYNC110 - the watcher table signals nothing
                await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_a_client_that_stops_reading_is_told_to_connect_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A send that can't finish within one recheck ends the stream, so a stalled client holds nothing for long."""
    monkeypatch.setattr(workspace_files, "_RECHECK_SECONDS", 0.05)
    stalled = asyncio.Event()

    class _StalledSocket:
        async def send_json(self, _message: object) -> None:
            await stalled.wait()

    async def next_changes() -> list[str]:
        return ["notes.md"]

    with pytest.raises(HTTPException) as ended:
        async with asyncio.timeout(5):
            await workspace_files._send_changes(_StalledSocket(), next_changes)  # type: ignore[arg-type]

    assert ended.value.status_code == 503
