"""Tests for the Zep toolkit against the zep-cloud 4 SDK, faked at its HTTP transport."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from zep_cloud.client import AsyncZep, Zep

from mindroom.tools import agno_compat_zep
from mindroom.tools.agno_compat_zep import MindRoomZepAsyncTools
from mindroom.tools.zep import zep_tools

_PREFIX = "/api/v4/"


class _ZepServer:
    """Answer the v4 endpoints the toolkit uses from in-memory users, threads, messages, and graph results."""

    def __init__(self) -> None:
        self.users: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._next = 0

    def _uuid(self, kind: str) -> str:
        self._next += 1
        return f"{kind}-uuid-{self._next}"

    def add_user(self, user_id: str | None, metadata: dict[str, Any] | None = None) -> str:
        uuid = self._uuid("user")
        self.users[uuid] = {"uuid": uuid, "user_id": user_id, "metadata": metadata or {}}
        return uuid

    def add_thread(self, user_uuid: str, thread_id: str | None = None) -> str:
        uuid = self._uuid("thread")
        self.threads[uuid] = {"uuid": uuid, "user_uuid": user_uuid, "thread_id": thread_id}
        self.messages[uuid] = []
        return uuid

    def calls_named(self, *names: str) -> list[tuple[str, dict[str, Any]]]:
        return [(name, body) for name, path, body in self.calls if name in names or path in names]

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(_PREFIX)
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)
        name = f"{request.method} {path}"
        self.calls.append((name, path, {**params, **body}))
        parts = path.split("/")
        handler = {"users": self._users, "threads": self._threads, "graphs": self._graphs}[parts[0]]
        return handler(request.method, parts[1:], body, params)

    def _users(self, method: str, parts: list[str], body: dict[str, Any], _params: dict[str, str]) -> httpx.Response:
        if parts == ["list"]:
            found = [user for user in self.users.values() if body["search"] in (user["user_id"] or "")]
            return httpx.Response(200, json={"items": found})
        if not parts:
            return httpx.Response(201, json=self.users[self.add_user(body.get("user_id"))])
        assert method == "PATCH"
        self.users[parts[0]]["metadata"].update(body["metadata"])
        return httpx.Response(200, json=self.users[parts[0]])

    def _threads(self, method: str, parts: list[str], body: dict[str, Any], params: dict[str, str]) -> httpx.Response:
        if not parts:
            return httpx.Response(201, json=self.threads[self.add_thread(body["user_uuid"])])
        if parts == ["lookup"]:
            match = [thread for thread in self.threads.values() if thread["thread_id"] == body["thread_id"]]
            return httpx.Response(200, json=match[0]) if match else _not_found()
        thread_uuid, *rest = parts
        if thread_uuid not in self.threads:
            return _not_found()
        if rest == ["messages"]:
            return self._messages(method, thread_uuid, body, params)
        if rest == ["context"]:
            context = f"<FACTS>\n- {len(self.messages[thread_uuid])} messages\n</FACTS>"
            return httpx.Response(200, json={"context": context})
        return httpx.Response(200, json=self.threads[thread_uuid])

    def _messages(self, method: str, thread_uuid: str, body: dict[str, Any], params: dict[str, str]) -> httpx.Response:
        if method == "POST":
            stored = [
                {**message, "uuid": self._uuid("message"), "thread_uuid": thread_uuid} for message in body["messages"]
            ]
            self.messages[thread_uuid].extend(stored)
            return httpx.Response(202, json={"messages": stored})
        ordered = self.messages[thread_uuid][:: -1 if params.get("order") == "desc" else 1]
        return httpx.Response(200, json={"items": ordered})

    def _graphs(self, _method: str, parts: list[str], _body: dict[str, Any], params: dict[str, str]) -> httpx.Response:
        if parts[1:] == ["search", "edges"]:
            edges = [
                {"uuid": "e1", "fact": "Alice prefers window seats"},
                {"uuid": "e2", "fact": "Alice lives in Delft"},
            ]
            return httpx.Response(200, json={"items": edges[: int(params["page_size"])]})
        node = {"uuid": "n1", "name": "Alice", "summary": "A frequent flyer", "episode_uuids": []}
        return httpx.Response(200, json={"items": [{**node, "episode_uuids_truncated": False}]})


def _not_found() -> httpx.Response:
    return httpx.Response(404, json={"error": {"code": "not_found", "message": "thread not found"}})


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _ZepServer:
    """Route the toolkit's real zep-cloud 4 clients to an in-memory Zep server."""
    fake = _ZepServer()

    def sync_client(**kwargs: Any) -> Zep:  # noqa: ANN401
        return Zep(**kwargs, httpx_client=httpx.Client(transport=httpx.MockTransport(fake.handle)))

    def async_client(**kwargs: Any) -> AsyncZep:  # noqa: ANN401
        return AsyncZep(**kwargs, httpx_client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)))

    monkeypatch.setattr(agno_compat_zep, "Zep", sync_client)
    monkeypatch.setattr(agno_compat_zep, "AsyncZep", async_client)
    return fake


def _toolkit(**kwargs: object) -> Any:  # noqa: ANN401
    return zep_tools()(api_key="zep-test-key", **kwargs)


def test_zep_tool_creates_a_configured_user_and_session_then_uses_them(server: _ZepServer) -> None:
    """A new user_id and session_id become a v4 user and thread, and each function reaches them."""
    toolkit = _toolkit(user_id="alice", session_id="trip-planning", ignore_assistant_messages=True)

    [user_uuid] = server.users
    [thread_uuid] = server.threads
    assert server.users[user_uuid]["user_id"] == "alice"
    assert server.threads[thread_uuid]["user_uuid"] == user_uuid
    # v4 threads take no chosen ID, so the user's metadata remembers which thread the session is.
    assert server.users[user_uuid]["metadata"] == {"mindroom_zep_thread:trip-planning": thread_uuid}
    assert [name for name, _, _ in server.calls] == [
        "POST users/list",
        "POST users",
        "POST threads/lookup",
        "POST threads",
        f"PATCH users/{user_uuid}",
    ]

    added = toolkit.add_zep_message(role="user", content="I prefer window seats.")
    context = toolkit.get_zep_memory()
    messages = toolkit.get_zep_memory(memory_type="messages")
    facts = toolkit.search_zep_memory(query="seats")
    nodes = toolkit.search_zep_memory(query="Alice", search_scope="nodes")
    unsupported = toolkit.search_zep_memory(query="Alice", search_scope="episodes")

    assert added == "Message from 'user' added successfully to session trip-planning."
    [(_, sent)] = server.calls_named(f"POST threads/{thread_uuid}/messages")
    assert sent == {"messages": [{"role": "user", "content": "I prefer window seats."}], "ignore_roles": ["assistant"]}
    assert context == "<FACTS>\n- 1 messages\n</FACTS>"
    assert messages.startswith("[Message(")
    assert "content='I prefer window seats.'" in messages
    assert facts == "Found 2 facts:\n- Alice prefers window seats\n- Alice lives in Delft"
    assert nodes == "Found 1 nodes:\n- Alice: A frequent flyer"
    assert (
        unsupported == "Error searching graph: Zep search scope 'episodes' is not supported; use one of edges, nodes."
    )
    # Graph search runs on the user's graph, returning v3's default of 10 results.
    [(_, edge_search)] = server.calls_named(f"graphs/{user_uuid}/search/edges")
    assert edge_search == {"page_size": "10", "query": "seats"}


def test_zep_tool_keeps_a_configured_session_on_the_same_thread_across_builds(server: _ZepServer) -> None:
    """A second build with the same user_id and session_id finds the user and its thread, creating neither."""
    _toolkit(user_id="alice", session_id="trip-planning").add_zep_message(role="user", content="Window seats.")
    server.calls.clear()

    toolkit = _toolkit(user_id="alice", session_id="trip-planning")
    messages = toolkit.get_zep_memory(memory_type="messages")

    [user_uuid] = server.users
    [thread_uuid] = server.threads
    assert [name for name, _, _ in server.calls] == [
        "POST users/list",
        f"GET threads/{thread_uuid}",
        f"GET threads/{thread_uuid}/messages",
    ]
    assert "content='Window seats.'" in messages
    assert server.users[user_uuid]["metadata"] == {"mindroom_zep_thread:trip-planning": thread_uuid}


def test_zep_tool_continues_a_zep_cloud_3_thread_and_replaces_a_deleted_one(server: _ZepServer) -> None:
    """A session created on zep-cloud 3 keeps its thread; a remembered thread deleted since is replaced."""
    user_uuid = server.add_user("alice", {"mindroom_zep_thread:gone": "thread-uuid-deleted"})
    legacy_uuid = server.add_thread(user_uuid, thread_id="trip-planning")

    _toolkit(user_id="alice", session_id="trip-planning")
    _toolkit(user_id="alice", session_id="gone")

    [replacement_uuid] = set(server.threads) - {legacy_uuid}
    assert server.users[user_uuid]["metadata"] == {
        "mindroom_zep_thread:trip-planning": legacy_uuid,
        "mindroom_zep_thread:gone": replacement_uuid,
    }
    assert [name for name, _, _ in server.calls if name == "POST users"] == []


def test_zep_tool_without_ids_creates_a_fresh_user_and_thread(server: _ZepServer) -> None:
    """Without a user_id or session_id the toolkit makes a new Zep user and thread and remembers neither."""
    toolkit = _toolkit()

    [user_uuid] = server.users
    [thread_uuid] = server.threads
    assert server.users[user_uuid] == {"uuid": user_uuid, "user_id": None, "metadata": {}}
    assert (toolkit.user_id, toolkit.session_id) == (user_uuid, thread_uuid)
    assert toolkit.add_zep_message(role="assistant", content="Hi") == (
        f"Message from 'assistant' added successfully to session {thread_uuid}."
    )
    [(_, sent)] = server.calls_named(f"POST threads/{thread_uuid}/messages")
    assert sent == {"messages": [{"role": "assistant", "content": "Hi"}]}


def test_zep_tool_reports_an_uninitialized_client_when_zep_rejects_the_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed setup leaves the functions answering with Agno's not-initialized text."""

    def rejecting(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": "unauthenticated", "message": "bad key"}})

    monkeypatch.setattr(
        agno_compat_zep,
        "Zep",
        lambda **kwargs: Zep(**kwargs, httpx_client=httpx.Client(transport=httpx.MockTransport(rejecting))),
    )

    toolkit = _toolkit(user_id="alice", session_id="trip-planning")

    assert toolkit.zep_client is None
    assert toolkit.add_zep_message(role="user", content="Hi") == "Error: Zep client/session not initialized."


@pytest.mark.asyncio
async def test_zep_async_tool_initializes_on_first_use_and_reaches_the_same_thread(server: _ZepServer) -> None:
    """The async toolkit resolves the user and thread on first use, and a second toolkit finds them again."""
    toolkit = MindRoomZepAsyncTools(api_key="zep-test-key", user_id="alice", session_id="trip-planning")
    assert server.calls == []

    added = await toolkit.add_zep_message(role="user", content="I prefer window seats.")
    context = await toolkit.get_zep_memory()
    facts = await toolkit.search_zep_memory(query="seats", limit=1)
    nodes = await toolkit.search_zep_memory(query="Alice", scope="nodes")

    [user_uuid] = server.users
    [thread_uuid] = server.threads
    assert server.users[user_uuid]["metadata"] == {"mindroom_zep_thread:trip-planning": thread_uuid}
    assert added == "Message from 'user' added successfully to session trip-planning."
    assert context == "<FACTS>\n- 1 messages\n</FACTS>"
    assert facts == "Found 1 facts:\n- Alice prefers window seats"
    assert nodes == "Found 1 nodes:\n- Alice: A frequent flyer"
    [(_, node_search)] = server.calls_named(f"graphs/{user_uuid}/search/nodes")
    assert node_search == {"page_size": "5", "query": "Alice"}

    again = MindRoomZepAsyncTools(api_key="zep-test-key", user_id="alice", session_id="trip-planning")
    messages = await again.get_zep_memory(memory_type="messages")

    assert len(server.threads) == 1
    assert "content='I prefer window seats.'" in messages
