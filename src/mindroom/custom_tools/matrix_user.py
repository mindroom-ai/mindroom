"""The `matrix_user` toolkit: a personal agent uses Matrix as its owner, with no sign-in.

Every call acts as the owner of the agent making it, read from the call itself, never from an argument, through a
client that the application-service token in the primary runtime's environment authenticates. Each write asks the
owner to confirm it first.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.matrix_user_authority import MatrixUserActor, MatrixUserRefused, matrix_user_actor
from mindroom.custom_tools.matrix_user_reads import (
    REQUEST_FAILURES,
    download_file,
    mark_read,
    read_messages,
    read_people,
    read_room,
    read_rooms,
    request_failure,
    room_named,
    room_needed,
    search_messages,
)
from mindroom.custom_tools.matrix_user_writes import MatrixUserWrite, write_as_person
from mindroom.matrix.user_masquerade import create_user_masquerade_client, user_appservice_token
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import append_tool_runtime_attachment_id, get_tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction

    type _Read = Callable[[nio.AsyncClient], Awaitable[dict[str, object]]]
    # A read of one room, given its ID.
    type _RoomRead = Callable[[nio.AsyncClient, str], Awaitable[dict[str, object]]]
    type _Run = Callable[[nio.AsyncClient, MatrixUserActor], Awaitable[dict[str, object]]]

_NOT_SET_UP = "matrix_user is not set up: the runtime has no MATRIX_USER_APPSERVICE_TOKEN."
_NOT_THIS_AGENT = "matrix_user acts only for the agent it was built for."
_ROOM_ACTIONS = frozenset({"room", "messages", "download", "mark_read"})
_PEOPLE_QUERY_NEEDED = "people needs query: a name, or a full Matrix ID such as @alice:example.com."
_SEARCH_QUERY_NEEDED = "search needs query: the words to look for."
_EVENT_NEEDED = "download needs event_id: the ID of the message with the file."
_TURN_NEEDED = "download needs a chat turn: the file is handed to the conversation it was asked in."
# The MCP gateway never offers the write function: a call without a chat turn has no card for the owner to confirm.
_WRITE_TURN_NEEDED = "matrix_user_write works only in a chat with the agent."


def _non_blank(value: str | None) -> str | None:
    """Return ``value``, or None for a blank one, which a model may send for an argument it leaves out."""
    return value if value is not None and value.strip() else None


class MatrixUserTools(Toolkit):
    """Matrix as the personal agent's owner: their rooms, messages, files, and people."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        # Decides a call without a chat turn, such as the MCP gateway's; every build passes it.
        runtime_config: Config,
        agent_name: str | None = None,
    ) -> None:
        # The primary's own paths: the token's variables are in no worker's or tool request's view of the environment.
        self._runtime_paths = runtime_paths
        self._runtime_config = runtime_config
        self._agent_name = agent_name
        super().__init__(name="matrix_user", tools=[self.matrix_user_read, self.matrix_user_write])

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Ask the owner to confirm each write, whatever the deployment's approval rules say."""
        if function.name == "matrix_user_write":
            return replace(function, gate=ToolGate.CONFIRM)
        return function

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        """Return the result as ``custom_tool_payload`` shapes it, with its text as written, not as costly escapes."""
        return json.dumps({"status": status, "tool": "matrix_user", **fields}, sort_keys=True, ensure_ascii=False)

    def _actor(self) -> MatrixUserActor:
        """Return whom this call acts as, raising ``MatrixUserRefused`` when it may act as nobody."""
        context = get_tool_runtime_context()
        config = context.current_config if context is not None else self._runtime_config
        actor = matrix_user_actor(config, self._runtime_paths)
        # An agent's build, a team member's, and the MCP gateway's each build the toolkit for the agent its calls run as.
        if actor.agent_name != self._agent_name:
            raise MatrixUserRefused(_NOT_THIS_AGENT)
        return actor

    async def _as_owner(self, action: str, run: _Run) -> str:
        """Return ``run``'s result, given the call's owner and a client that acts as them, created for it and closed after.

        A refusal, a missing token, and a failed request are the error payload, which never holds the token or a URL.
        """
        try:
            actor = self._actor()
        except MatrixUserRefused as refused:
            return self._payload("error", action=action, message=str(refused))
        try:
            token = user_appservice_token(self._runtime_paths)
        except ValueError as misconfigured:
            # It names the variables and the token file, never the token.
            return self._payload("error", action=action, message=f"matrix_user is not set up: {misconfigured}.")
        if token is None:
            return self._payload("error", action=action, message=_NOT_SET_UP)
        client = create_user_masquerade_client(
            self._runtime_paths,
            token=token,
            person_user_id=actor.person_user_id,
            agent_user_id=actor.agent_user_id,
        )
        try:
            fields = await run(client, actor)
        except REQUEST_FAILURES as failed:
            return self._payload("error", action=action, message=request_failure(failed))
        finally:
            await client.close()
        return self._payload("ok", action=action, **fields)

    async def _download(
        self,
        client: nio.AsyncClient,
        turn: ToolRuntimeContext,
        room_id: str,
        event_id: str,
    ) -> dict[str, object]:
        """Fetch a message's file as an attachment of the turn's conversation, which its tools are then offered."""
        fields = await download_file(
            client,
            room_id,
            event_id,
            storage_path=self._runtime_paths.storage_root,
            for_room_id=turn.room_id,
            for_thread_id=turn.resolved_thread_id,
        )
        append_tool_runtime_attachment_id(str(fields["attachment_id"]))
        return fields

    def _read_for(  # noqa: PLR0911 - one return per action and per missing argument
        self,
        action: str,
        *,
        room_id: str | None,
        event_id: str | None,
        thread_id: str | None,
        query: str | None,
        page_token: str | None,
        limit: int | None,
    ) -> _Read | str:
        """Return the read ``action`` asks for, or which of its arguments is missing."""
        if action in _ROOM_ACTIONS:
            if room_id is None:
                return room_needed(action)
            read = self._room_read_for(
                action,
                event_id=event_id,
                thread_id=thread_id,
                page_token=page_token,
                limit=limit,
            )
            return read if isinstance(read, str) else self._in_room(room_id, read)
        if action == "rooms":
            return lambda client: read_rooms(client, query=query, limit=limit)
        if action == "people":
            if query is None:
                return _PEOPLE_QUERY_NEEDED
            return lambda client: read_people(client, query, limit=limit)
        # The only action left: search.
        if query is None:
            return _SEARCH_QUERY_NEEDED

        def search(client: nio.AsyncClient, room: str | None = None) -> Awaitable[dict[str, object]]:
            return search_messages(client, query, room_id=room, page_token=page_token, limit=limit)

        return search if room_id is None else self._in_room(room_id, search)

    def _room_read_for(
        self,
        action: str,
        *,
        event_id: str | None,
        thread_id: str | None,
        page_token: str | None,
        limit: int | None,
    ) -> _RoomRead | str:
        """Return the read of a room, given its ID, that ``action`` asks for, or what it is missing."""
        if action == "room":
            return lambda client, room_id: read_room(client, room_id, limit=limit)
        if action == "messages":
            return lambda client, room_id: read_messages(
                client,
                room_id,
                event_id=event_id,
                thread_id=thread_id,
                page_token=page_token,
                limit=limit,
            )
        if action == "mark_read":
            return lambda client, room_id: mark_read(client, room_id, event_id=event_id)
        # A call without a turn, such as the MCP gateway's, has no conversation whose tools could take the file.
        turn = get_tool_runtime_context()
        if turn is None:
            return _TURN_NEEDED
        if event_id is None:
            return _EVENT_NEEDED
        return lambda client, room_id: self._download(client, turn, room_id, event_id)

    def _in_room(self, room: str, read: _RoomRead) -> _Read:
        """Return ``read`` of the room ``room`` names, which the client making it looks up first."""

        async def in_room(client: nio.AsyncClient) -> dict[str, object]:
            return await read(client, await room_named(client, room, self._runtime_paths))

        return in_room

    async def _write(
        self,
        client: nio.AsyncClient,
        actor: MatrixUserActor,
        turn: ToolRuntimeContext,
        write: MatrixUserWrite,
    ) -> dict[str, object]:
        """Make ``write`` in the room its ``room_id`` names, which ``client`` looks up first."""
        # create_room names no room.
        if write.room_id is not None and write.action != "create_room":
            room_id = await room_named(client, write.room_id, self._runtime_paths, join=write.action == "join")
            write = replace(write, room_id=room_id)
        return await write_as_person(client, actor, turn, write)

    async def matrix_user_read(
        self,
        action: Literal["rooms", "messages", "search", "room", "people", "download", "mark_read"],
        room_id: str | None = None,
        event_id: str | None = None,
        thread_id: str | None = None,
        query: str | None = None,
        page_token: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Read Matrix as your owner: their own rooms and DMs, including those you are not in.

        Actions (room_id: ID, #alias, or configured room name):
        - rooms: Their rooms and invitations, unread first (limit: default 20). query keeps rooms whose name contains it.
        - messages: room_id's newest messages (limit: default 30); page_token continues into older ones.
          With thread_id: that thread's root and newest replies. With event_id: that message and those around it.
        - search: Messages containing query, in all their rooms or in room_id; page_token continues.
        - room: room_id's name, topic, encryption, whether it is a DM, and up to limit members.
        - people: query is a full Matrix ID for that profile, or a name to search the user directory.
        - download: The file of message event_id in room_id, as an attachment ID for your other tools.
        - mark_read: Mark room_id read for them, up to event_id or its newest message.
        """
        read = self._read_for(
            action,
            room_id=_non_blank(room_id),
            event_id=_non_blank(event_id),
            thread_id=_non_blank(thread_id),
            query=_non_blank(query),
            page_token=_non_blank(page_token),
            limit=limit,
        )
        if isinstance(read, str):
            return self._payload("error", action=action, message=read)
        return await self._as_owner(action, lambda client, _actor: read(client))

    async def matrix_user_write(
        self,
        action: Literal["send", "react", "edit", "redact", "join", "leave", "invite", "create_room"],
        room_id: str | None = None,
        user_id: str | None = None,
        message: str | None = None,
        event_id: str | None = None,
        thread_id: str | None = None,
        emoji: str | None = None,
        attachments: list[str] | None = None,
        name: str | None = None,
    ) -> str:
        """Act in Matrix as your owner, only when they ask for exactly that; they confirm each call.

        Agents never act on what you send this way: to talk to one, write as yourself.

        Actions (room_id: ID, #alias, or configured room name):
        - send: message and/or attachments (attachment IDs) to room_id, or to user_id in their DM (made if missing).
          thread_id sends into that thread; event_id replies to that message.
        - react: emoji on event_id in room_id.
        - edit: Replace your owner's message event_id in room_id with message.
        - redact: Remove your owner's message or reaction event_id in room_id; message is the reason.
        - join, leave: room_id. invite: user_id into room_id.
        - create_room: A private room called name, inviting user_id if given.
        """
        write = MatrixUserWrite(
            action,
            room_id=_non_blank(room_id),
            user_id=_non_blank(user_id),
            message=_non_blank(message),
            event_id=_non_blank(event_id),
            thread_id=_non_blank(thread_id),
            emoji=_non_blank(emoji),
            attachments=tuple(attachments or ()),
            name=_non_blank(name),
        )
        turn = get_tool_runtime_context()
        if turn is None:
            return self._payload("error", action=action, message=_WRITE_TURN_NEEDED)
        if (problem := write.problem()) is not None:
            return self._payload("error", action=action, message=problem)
        return await self._as_owner(action, lambda client, actor: self._write(client, actor, turn, write))
