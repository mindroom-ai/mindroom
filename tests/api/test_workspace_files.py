"""Chat clients list the workspaces a room opens to them, and read and change their files as the room lets them.

Alice may post and use both agents, Bob may use both but not post, and Carol may use only the private ``notes``.
"""

from __future__ import annotations

import base64
import hashlib
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from mindroom.api import main
from mindroom.api.client_sessions import client_session_store
from mindroom.api.workspace_files import router
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.workspace_files import operations
from tests.api.test_plugin_room_agents import ALICE, BOB, CAROL, ROOM, _agents, _Room, _room, _turn_root

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import Response

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

BASE = f"/api/workspaces/v1/rooms/{ROOM}"


class _Client:
    """Each person's requests to the routes, signed in with a client session of their own."""

    def __init__(self, room: _Room) -> None:
        room.app.include_router(router)
        self.room = room
        self.http = TestClient(room.app)
        self.tokens: dict[str, str] = {}

    def _headers(self, user_id: str) -> dict[str, str]:
        token = self.tokens.setdefault(user_id, client_session_store(self.room.app).create(user_id).token)
        return {"Authorization": f"Bearer {token}"}

    def get(self, user_id: str, route: str, **params: str) -> Response:
        return self.http.get(f"{BASE}{route}", params=params, headers=self._headers(user_id))

    def put(self, user_id: str, route: str, body: dict[str, object]) -> Response:
        return self.http.put(f"{BASE}{route}", json=body, headers=self._headers(user_id))

    def post(self, user_id: str, route: str, body: dict[str, object]) -> Response:
        return self.http.post(f"{BASE}{route}", json=body, headers=self._headers(user_id))

    def delete(self, user_id: str, route: str, **params: str) -> Response:
        return self.http.delete(f"{BASE}{route}", params=params, headers=self._headers(user_id))


@pytest.fixture
def client(tmp_path: Path) -> _Client:
    """Alice, Bob and Carol in a room with ``coder`` and ``notes``."""
    return _Client(_room(tmp_path, _agents()))


def _etag(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_every_route_needs_a_client_session(client: _Client) -> None:
    """Without a session the routes answer 401 before reading the room."""
    coder = client.room.ids["coder"]

    assert client.http.get(BASE).status_code == 401
    assert client.http.get(f"{BASE}/{coder}/folder").status_code == 401
    assert client.http.put(f"{BASE}/{coder}/file", json={"path": "a", "content": ""}).status_code == 401


def test_the_room_lists_the_workspaces_each_caller_may_open_and_what_they_may_do(client: _Client) -> None:
    """Writing needs power to post; the private agent's files only read; Carol sees only what she may use."""
    ids = client.room.ids
    coder = {"agent_user_id": ids["coder"], "display_name": "Coder", "private": False}
    notes = {"agent_user_id": ids["notes"], "display_name": "Notes", "private": True}

    assert client.get(ALICE, "").json() == {"workspaces": [coder | {"files": "write"}, notes | {"files": "read"}]}
    assert client.get(BOB, "").json() == {"workspaces": [coder | {"files": "read"}, notes | {"files": "read"}]}
    assert client.get(CAROL, "").json() == {"workspaces": [notes | {"files": "read"}]}


def test_an_agent_whose_tools_reach_no_files_opens_no_workspace(tmp_path: Path) -> None:
    """Without a toolkit that takes paths or runs code, the agent is left out and its workspace answers 403."""
    client = _Client(_room(tmp_path, {"chat": AgentConfig(display_name="Chat", tools=[], access={"users": [ALICE]})}))

    assert client.get(ALICE, "").json() == {"workspaces": []}
    assert client.get(ALICE, f"/{client.room.ids['chat']}/folder").status_code == 403


def test_bob_reads_but_cannot_change_and_carol_cannot_open(client: _Client) -> None:
    """A caller who may not post reads (200) and is refused every change (403); one the agent refuses gets 403."""
    coder = client.room.ids["coder"]
    assert client.put(ALICE, f"/{coder}/file", {"path": "plan.md", "content": "# Plan\n"}).status_code == 200

    assert client.get(BOB, f"/{coder}/file", path="plan.md").json()["content"] == "# Plan\n"
    assert client.put(BOB, f"/{coder}/file", {"path": "plan.md", "content": "# Bob\n"}).status_code == 403
    assert client.post(BOB, f"/{coder}/folder", {"path": "docs"}).status_code == 403
    assert client.post(BOB, f"/{coder}/move", {"path": "plan.md", "to": "old.md"}).status_code == 403
    assert client.delete(BOB, f"/{coder}/entry", path="plan.md").status_code == 403
    assert client.get(CAROL, f"/{coder}/file", path="plan.md").status_code == 403
    assert client.get(CAROL, f"/{coder}/folder").status_code == 403
    assert client.get(ALICE, f"/{coder}/file", path="plan.md").json()["content"] == "# Plan\n"


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
    diary = client.room.ids["diary"]
    root = _turn_root(client.room, "diary", ALICE)
    assert not root.exists()
    assert client.get(ALICE, f"/{diary}/folder").json() == {"path": "", "entries": [], "truncated": False}

    saved = client.put(ALICE, f"/{diary}/file", {"path": "today/entry.md", "content": "Dear diary\n"})

    assert saved.status_code == 200
    assert saved.json()["etag"] == _etag(b"Dear diary\n")
    assert (root / "today" / "entry.md").read_text() == "Dear diary\n"
    assert _turn_root(client.room, "diary", ALICE) == root


def test_a_save_with_a_stale_etag_answers_409_with_the_current_one(client: _Client) -> None:
    """A save of a version someone replaced changes nothing and names the file's etag, null once it is gone."""
    coder = client.room.ids["coder"]
    first = client.put(ALICE, f"/{coder}/file", {"path": "plan.md", "content": "one"}).json()["etag"]
    second = client.put(ALICE, f"/{coder}/file", {"path": "plan.md", "content": "two", "etag": first})
    assert second.status_code == 200

    stale = client.put(ALICE, f"/{coder}/file", {"path": "plan.md", "content": "three", "etag": first})

    assert stale.status_code == 409
    assert stale.json()["etag"] == _etag(b"two") == second.json()["etag"]
    assert client.get(ALICE, f"/{coder}/file", path="plan.md").json()["content"] == "two"
    client.delete(ALICE, f"/{coder}/entry", path="plan.md")
    gone = client.put(ALICE, f"/{coder}/file", {"path": "plan.md", "content": "four", "etag": first})
    assert (gone.status_code, gone.json()["etag"]) == (409, None)


def test_files_travel_as_text_or_base64_both_ways(client: _Client) -> None:
    """Binary content saves and reads as base64, and content that is not base64 answers 400."""
    coder = client.room.ids["coder"]
    binary = bytes(range(256))
    encoded = base64.b64encode(binary).decode()

    saved = client.put(ALICE, f"/{coder}/file", {"path": "data.bin", "content": encoded, "encoding": "base64"})
    read = client.get(ALICE, f"/{coder}/file", path="data.bin").json()

    assert saved.json()["size"] == 256
    assert (read["encoding"], read["content"], read["etag"]) == ("base64", encoded, _etag(binary))
    assert client.put(ALICE, f"/{coder}/file", {"path": "x", "content": "@@", "encoding": "base64"}).status_code == 400


def test_a_save_over_the_limit_answers_413(client: _Client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Content over the file size limit answers 413 before anything is written."""
    monkeypatch.setattr(operations, "MAX_FILE_BYTES", 4)
    coder = client.room.ids["coder"]

    assert client.put(ALICE, f"/{coder}/file", {"path": "big.txt", "content": "12345"}).status_code == 413
    assert client.put(ALICE, f"/{coder}/file", {"path": "ok.txt", "content": "1234"}).status_code == 200


def test_folders_moves_and_deletes_change_the_workspace(client: _Client) -> None:
    """Making, moving and deleting answer 204, and a listing shows the result."""
    coder = client.room.ids["coder"]
    client.put(ALICE, f"/{coder}/file", {"path": "src/main.py", "content": "print()\n"})

    assert client.post(ALICE, f"/{coder}/folder", {"path": "docs/guide"}).status_code == 204
    assert client.post(ALICE, f"/{coder}/folder", {"path": "docs/guide"}).status_code == 409
    assert client.post(ALICE, f"/{coder}/move", {"path": "src", "to": "lib/src"}).status_code == 204
    assert client.delete(ALICE, f"/{coder}/entry", path="docs").status_code == 204

    listing = client.get(ALICE, f"/{coder}/folder", path="lib/src").json()
    assert [(entry["path"], entry["kind"], entry["size"]) for entry in listing["entries"]] == [
        ("lib/src/main.py", "file", 8),
    ]
    assert [entry["name"] for entry in client.get(ALICE, f"/{coder}/folder").json()["entries"]] == ["lib"]


def test_paths_that_leave_or_name_credentials_are_refused_through_the_routes(client: _Client) -> None:
    """``..`` answers 400, a credential 404 as an absent file, and Git's files and ``.config`` 403."""
    coder = client.room.ids["coder"]
    root = _turn_root(client.room, "coder", ALICE)
    (root / ".config" / "gh").mkdir(parents=True)
    (root / ".env").write_text("TOKEN=1\n")

    assert client.get(ALICE, f"/{coder}/file", path="../config.yaml").status_code == 400
    assert client.get(ALICE, f"/{coder}/file", path=".env").status_code == 404
    assert client.put(ALICE, f"/{coder}/file", {"path": "repo/.git/config", "content": ""}).status_code == 403
    assert client.post(ALICE, f"/{coder}/move", {"path": ".config", "to": "config"}).status_code == 403
    assert [entry["name"] for entry in client.get(ALICE, f"/{coder}/folder").json()["entries"]] == [".config"]


def test_mindroom_serves_the_routes_to_chat_clients_on_other_origins(tmp_path: Path) -> None:
    """The API answers from the client origins the Computer allows, and nothing it answers is cached."""
    chat = "https://chat.example.org"
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_COMPUTER_ALLOWED_ORIGINS": f'["{chat}"]'},
    )
    main.initialize_api_app(main.app, paths)

    response = TestClient(main.app).get(BASE, headers={"Origin": chat})

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == chat
    assert response.headers["cache-control"] == "no-store"
