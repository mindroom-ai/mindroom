"""The `matrix_user` toolkit: a personal agent uses Matrix as its owner, with no sign-in.

Every call acts as the owner of the agent making it, read from the call itself, never from an argument, through a
client that the application-service token in the primary runtime's environment authenticates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import aiohttp

from mindroom.config.main import Config  # noqa: TC001  # resolved by tool contract introspection
from mindroom.custom_tools.matrix_user_authority import MatrixUserActor, MatrixUserRefused, matrix_user_actor
from mindroom.custom_tools.matrix_user_reads import MatrixRequestError, read_people, read_room, read_rooms
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.matrix.user_masquerade import create_user_masquerade_client, user_appservice_token
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.constants import RuntimePaths

_NOT_SET_UP = "matrix_user is not set up: the runtime has no MATRIX_USER_APPSERVICE_TOKEN."
_NOT_THIS_AGENT = "matrix_user acts only for the agent it was built for."
_UNREACHABLE = "The Matrix homeserver could not be reached."
_ROOM_NEEDED = "room needs room_id, a room ID such as !abc:example.com."
_QUERY_NEEDED = "people needs query: a name, or a full Matrix ID such as @alice:example.com."


class MatrixUserTools(Toolkit):
    """Matrix as the personal agent's owner: their rooms, one room, and people."""

    def __init__(
        self,
        *,
        runtime_paths: RuntimePaths,
        runtime_config: Config | None = None,
        agent_name: str | None = None,
    ) -> None:
        # The primary's own paths: the token's variables are in no worker's or tool request's view of the environment.
        self._runtime_paths = runtime_paths
        self._runtime_config = runtime_config
        self._agent_name = agent_name
        super().__init__(name="matrix_user", tools=[self.matrix_user_read])

    @staticmethod
    def _payload(status: str, **fields: object) -> str:
        return custom_tool_payload("matrix_user", status, **fields)

    def _actor(self) -> MatrixUserActor:
        """Return whom this call acts as, raising ``MatrixUserRefused`` when it may act as nobody."""
        context = get_tool_runtime_context()
        config = context.current_config if context is not None else self._runtime_config
        if config is None:
            raise MatrixUserRefused(_NOT_THIS_AGENT)
        actor = matrix_user_actor(config, self._runtime_paths)
        if actor.agent_name != self._agent_name:
            raise MatrixUserRefused(_NOT_THIS_AGENT)
        return actor

    async def _as_owner(self, action: str, read: Callable[[nio.AsyncClient], Awaitable[dict[str, object]]]) -> str:
        """Return ``read``'s result, run with a client that acts as the call's owner, created for it and closed after.

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
            fields = await read(client)
        except MatrixRequestError as error:
            return self._payload("error", action=action, message=str(error))
        except (aiohttp.ClientError, TimeoutError):
            # The transport's own text names the URL it requested.
            return self._payload("error", action=action, message=_UNREACHABLE)
        finally:
            await client.close()
        return self._payload("ok", action=action, **fields)

    async def matrix_user_read(
        self,
        action: Literal["rooms", "room", "people"],
        room_id: str | None = None,
        query: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Read Matrix as your owner: their own rooms and DMs, including those you are not in.

        Actions:
        - rooms: Their rooms, unread first, then invitations, then the rest, one line each:
          `<room_id> | <name> | unread <n>, mentions <m>[, marked unread]` or `<room_id> | <name> | invited by <user>`.
          query keeps rooms whose name contains it. limit: default 50, at most 200.
        - room: room_id's name, topic, encryption, whether it is a DM, and up to limit (default 50) members.
        - people: query is a full Matrix ID for that profile, or a name to search the user directory
          (limit: default 10, at most 50).
        """
        if action == "rooms":
            return await self._as_owner(action, lambda client: read_rooms(client, query=query, limit=limit))
        if action == "room":
            if room_id is None or not room_id.startswith("!"):
                return self._payload("error", action=action, message=_ROOM_NEEDED)
            return await self._as_owner(action, lambda client: read_room(client, room_id, limit=limit))
        if action == "people":
            if query is None or not query.strip():
                return self._payload("error", action=action, message=_QUERY_NEEDED)
            return await self._as_owner(action, lambda client: read_people(client, query, limit=limit))
        return self._payload("error", action=action, message="action must be rooms, room, or people.")
