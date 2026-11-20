"""Run Agno's Zep toolkits on the zep-cloud 4 SDK, which addresses users and threads by Zep-generated UUIDs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agno.tools.zep import ZepAsyncTools, ZepTools
from agno.utils.log import log_debug, log_error
from zep_cloud import AddMessage, NotFoundError
from zep_cloud.client import AsyncZep, Zep
from zep_cloud.core.api_error import ApiError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from zep_cloud import Edge, Message, Node, User

# A user's metadata maps each configured session ID to the thread created for it, since v4 threads take no chosen ID.
_THREAD_KEY_PREFIX = "mindroom_zep_thread:"
# zep-cloud 3's graph.search returned 10 results unless told otherwise, and Agno's sync search sets no limit.
_V3_SEARCH_LIMIT = 10
_SEARCH_SCOPES = ("edges", "nodes")


@dataclass(frozen=True)
class _MessageList:
    messages: list[Message]


@dataclass(frozen=True)
class _SearchResults:
    edges: list[Edge] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)


def _add_messages(messages: Sequence[Message]) -> list[AddMessage]:
    # Agno builds response Messages carrying v3's role_type, which the v4 add endpoint does not take.
    return [
        AddMessage(role=message.role, content=message.content)
        if message.name is None
        else AddMessage(role=message.role, content=message.content, name=message.name)
        for message in messages
    ]


def _scope_error(scope: str) -> ValueError:
    return ValueError(f"Zep search scope {scope!r} is not supported; use one of {', '.join(_SEARCH_SCOPES)}.")


@dataclass(frozen=True)
class _Thread:
    """The v3 thread calls Agno's ZepTools makes, sent to the one v4 thread the toolkit resolved."""

    client: Zep
    thread_uuid: str

    def add_messages(self, *, thread_id: str, messages: Sequence[Message], ignore_roles: list[str] | None) -> object:
        del thread_id
        if ignore_roles is None:
            return self.client.thread.add_messages(self.thread_uuid, messages=_add_messages(messages))
        return self.client.thread.add_messages(
            self.thread_uuid,
            messages=_add_messages(messages),
            ignore_roles=ignore_roles,
        )

    def get_user_context(self, *, thread_id: str, mode: str) -> object:
        del thread_id, mode
        return self.client.thread.get_context(self.thread_uuid)

    def get(self, *, thread_id: str) -> _MessageList:
        del thread_id
        newest_first = self.client.thread.list_messages(self.thread_uuid, order="desc").items or []
        return _MessageList(messages=newest_first[::-1])


@dataclass(frozen=True)
class _Graph:
    """The v3 graph search Agno's ZepTools makes, run as v4's per-scope search of the toolkit's user graph."""

    client: Zep
    user_uuid: str

    def search(self, *, query: str, user_id: str, scope: str, limit: int | None = None) -> _SearchResults:
        del user_id
        page_size = _V3_SEARCH_LIMIT if limit is None else limit
        if scope == "edges":
            edges = self.client.graph.search_edges(self.user_uuid, query=query, page_size=page_size).items
            return _SearchResults(edges=edges or [])
        if scope == "nodes":
            nodes = self.client.graph.search_nodes(self.user_uuid, query=query, page_size=page_size).items
            return _SearchResults(nodes=nodes or [])
        raise _scope_error(scope)


@dataclass(frozen=True)
class _Client:
    thread: _Thread
    graph: _Graph


def _resolve_user(client: Zep, user_id: str | None) -> User:
    if user_id is None:
        return client.user.create()
    # The search matches user IDs, emails, and names containing the text.
    for user in client.user.list(search=user_id):
        if user.user_id == user_id:
            return user
    return client.user.create(user_id=user_id)


def _resolve_thread(client: Zep, user: User, session_id: str | None) -> str:
    user_uuid = _uuid(user.uuid_)
    if session_id is None:
        return _uuid(client.thread.create(user_uuid=user_uuid).uuid_)
    key = _THREAD_KEY_PREFIX + session_id
    known = (user.metadata or {}).get(key)
    if isinstance(known, str):
        try:
            client.thread.get(known)
        except NotFoundError:
            log_debug(f"Zep thread {known} for session {session_id} no longer exists; creating another")
        else:
            return known
    try:
        # A thread created on zep-cloud 3 with this session ID as its thread_id.
        thread_uuid = _uuid(client.thread.lookup(thread_id=session_id).uuid_)
    except ApiError:
        thread_uuid = _uuid(client.thread.create(user_uuid=user_uuid).uuid_)
    client.user.update(user_uuid, metadata={key: thread_uuid})
    return thread_uuid


@dataclass(frozen=True)
class _AsyncThread:
    """The v3 thread calls Agno's ZepAsyncTools makes, sent to the one v4 thread the toolkit resolved."""

    client: AsyncZep
    thread_uuid: str

    async def add_messages(
        self,
        *,
        thread_id: str,
        messages: Sequence[Message],
        ignore_roles: list[str] | None,
    ) -> object:
        del thread_id
        if ignore_roles is None:
            return await self.client.thread.add_messages(self.thread_uuid, messages=_add_messages(messages))
        return await self.client.thread.add_messages(
            self.thread_uuid,
            messages=_add_messages(messages),
            ignore_roles=ignore_roles,
        )

    async def get_user_context(self, *, thread_id: str, mode: str) -> object:
        del thread_id, mode
        return await self.client.thread.get_context(self.thread_uuid)

    async def get(self, *, thread_id: str) -> _MessageList:
        del thread_id
        newest_first = (await self.client.thread.list_messages(self.thread_uuid, order="desc")).items or []
        return _MessageList(messages=newest_first[::-1])


@dataclass(frozen=True)
class _AsyncGraph:
    """The v3 graph search Agno's ZepAsyncTools makes, run as v4's per-scope search of the toolkit's user graph."""

    client: AsyncZep
    user_uuid: str

    async def search(self, *, query: str, user_id: str, scope: str, limit: int | None = None) -> _SearchResults:
        del user_id
        page_size = _V3_SEARCH_LIMIT if limit is None else limit
        if scope == "edges":
            edges = await self.client.graph.search_edges(self.user_uuid, query=query, page_size=page_size)
            return _SearchResults(edges=edges.items or [])
        if scope == "nodes":
            nodes = await self.client.graph.search_nodes(self.user_uuid, query=query, page_size=page_size)
            return _SearchResults(nodes=nodes.items or [])
        raise _scope_error(scope)


@dataclass(frozen=True)
class _AsyncClient:
    thread: _AsyncThread
    graph: _AsyncGraph


async def _resolve_user_async(client: AsyncZep, user_id: str | None) -> User:
    if user_id is None:
        return await client.user.create()
    async for user in await client.user.list(search=user_id):
        if user.user_id == user_id:
            return user
    return await client.user.create(user_id=user_id)


async def _resolve_thread_async(client: AsyncZep, user: User, session_id: str | None) -> str:
    user_uuid = _uuid(user.uuid_)
    if session_id is None:
        return _uuid((await client.thread.create(user_uuid=user_uuid)).uuid_)
    key = _THREAD_KEY_PREFIX + session_id
    known = (user.metadata or {}).get(key)
    if isinstance(known, str):
        try:
            await client.thread.get(known)
        except NotFoundError:
            log_debug(f"Zep thread {known} for session {session_id} no longer exists; creating another")
        else:
            return known
    try:
        # A thread created on zep-cloud 3 with this session ID as its thread_id.
        thread_uuid = _uuid((await client.thread.lookup(thread_id=session_id)).uuid_)
    except ApiError:
        thread_uuid = _uuid((await client.thread.create(user_uuid=user_uuid)).uuid_)
    await client.user.update(user_uuid, metadata={key: thread_uuid})
    return thread_uuid


def _uuid(value: str | None) -> str:
    if value is None:
        msg = "Zep returned a user or thread without a UUID"
        raise ValueError(msg)
    return value


# AGNO_COMPAT: ZepTools and ZepAsyncTools call the zep-cloud 3 SDK, which zep-cloud 4 replaced.
# Reason: Agno 3.2.1 creates users with user.add, which v4 removed, and addresses users, threads, and graph searches
# by developer-chosen IDs, while v4 addresses each by a UUID Zep generates and rejects a chosen thread_id; it also
# reads messages with thread.get, context with thread.get_user_context, and searches with graph.search, which v4
# replaced with thread.list_messages, thread.get_context, and graph.search_edges or graph.search_nodes. On v4 every
# toolkit fails to initialize, so every function returns "Error: Zep client/session not initialized."
# Upstream issue: Tracking gap; upstream tracking has not been verified (a search of Agno's issues and PRs titled
# "zep" found only PRs #2853 and #3436, both from before zep-cloud 4).
# Upstream PR: None identified.
# Remove when: Agno's Zep toolkits use the zep-cloud 4 API, keep a configured user_id and session_id on the same Zep
# user and thread across toolkit builds, and keep the function names, parameters, and result text the model sees.
# Coverage: tests/test_zep_tool.py::test_zep_tool_creates_a_configured_user_and_session_then_uses_them,
#   tests/test_zep_tool.py::test_zep_tool_keeps_a_configured_session_on_the_same_thread_across_builds,
#   tests/test_zep_tool.py::test_zep_tool_continues_a_zep_cloud_3_thread_and_replaces_a_deleted_one,
#   tests/test_zep_tool.py::test_zep_tool_without_ids_creates_a_fresh_user_and_thread,
#   tests/test_zep_tool.py::test_zep_async_tool_initializes_on_first_use_and_reaches_the_same_thread
class MindRoomZepTools(ZepTools):
    """ZepTools on zep-cloud 4: the configured user is matched by its user_id and the session kept in its metadata."""

    def initialize(self) -> bool:
        """Resolve the Zep user and thread, and give the toolkit's functions a client speaking v3's calls."""
        if self._initialized:
            return True
        try:
            client = Zep(api_key=self._api_key)
            user = _resolve_user(client, self.user_id_provided)
            thread_uuid = _resolve_thread(client, user, self.session_id_provided)
            user_uuid = _uuid(user.uuid_)
            self.user_id = self.user_id_provided or user_uuid
            self.session_id = self.session_id_provided or thread_uuid
            log_debug(f"Using Zep thread {thread_uuid} of user {user_uuid} for session {self.session_id}")
            self.zep_client = _Client(  # ty: ignore[invalid-assignment]
                thread=_Thread(client, thread_uuid),
                graph=_Graph(client, user_uuid),
            )
        except Exception as e:
            log_error(f"Failed to initialize ZepTools: {e}")
            self.zep_client = None
            self._initialized = False
            return False
        self._initialized = True
        return True


class MindRoomZepAsyncTools(ZepAsyncTools):
    """ZepAsyncTools on zep-cloud 4, resolving the user and thread like ``MindRoomZepTools``."""

    async def initialize(self) -> bool:
        """Resolve the Zep user and thread, and give the toolkit's functions a client speaking v3's calls."""
        if self._initialized:
            return True
        try:
            client = AsyncZep(api_key=self._api_key)
            user = await _resolve_user_async(client, self.user_id_provided)
            thread_uuid = await _resolve_thread_async(client, user, self.session_id_provided)
            user_uuid = _uuid(user.uuid_)
            self.user_id = self.user_id_provided or user_uuid
            self.session_id = self.session_id_provided or thread_uuid
            log_debug(f"Using Zep thread {thread_uuid} of user {user_uuid} for session {self.session_id}")
            self.zep_client = _AsyncClient(  # ty: ignore[invalid-assignment]
                thread=_AsyncThread(client, thread_uuid),
                graph=_AsyncGraph(client, user_uuid),
            )
        except Exception as e:
            log_error(f"Failed to initialize ZepAsyncTools: {e}")
            self.zep_client = None
            self._initialized = False
            return False
        self._initialized = True
        return True
