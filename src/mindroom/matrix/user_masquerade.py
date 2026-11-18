"""A Matrix client that acts as one person through an application service.

An application service may act as any user in its namespace by naming that user in each request's `user_id`
parameter, so this client needs no login, device, or encryption keys of its own.
Every message event, redaction, and state event it sends, which are the `send` and `redact` requests and a `PUT` of
`state`, is marked as the agent's, so MindRoom never treats a message or state event as the person's own, whatever
endpoint carried it. A homeserver may store a redaction with its reason only, and MindRoom does not read the marker on
one.
A membership change or a new room, which have endpoints of their own, goes out as the person's own.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, quote

import nio
from yarl import URL

from mindroom.constants import AGENT_AUTHORED_KEY, RuntimePaths, runtime_matrix_homeserver, secret_from_env_or_file
from mindroom.matrix.client_session import MindRoomAsyncClient, matrix_client_config, maybe_ssl_context
from mindroom.runtime_env_policy import MATRIX_USER_APPSERVICE_TOKEN_ENV, MATRIX_USER_APPSERVICE_TOKEN_FILE_ENV

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from nio.client.async_client import AsyncDataT

__all__ = ["create_user_masquerade_client", "user_appservice_token"]

# The requests that send a message event, a redaction, or a state event: `send/<type>` and `redact/<event>`, each with
# or without a transaction ID, and `state/<type>`, with or without a state key.
# Homeservers route a path with an empty segment too.
# Matched at the end of the requested path, which begins with the homeserver URL's own path, if it has one.
_EVENT_CREATING_PATH = re.compile(r"/_matrix/client/[^/]*/rooms/[^/]*/(?:send|redact|state)/[^/]*(?:/[^/]*)?\Z")


def user_appservice_token(runtime_paths: RuntimePaths) -> str | None:
    """Return the configured token for acting as people, or None when neither variable is set."""
    return secret_from_env_or_file(
        runtime_paths,
        MATRIX_USER_APPSERVICE_TOKEN_ENV,
        MATRIX_USER_APPSERVICE_TOKEN_FILE_ENV,
    )


def _named_user_count(query: str) -> int:
    """Return how many `user_id` parameters a query has, splitting on `;` too, as some servers do."""
    return sum(name == "user_id" for name, _ in parse_qsl(query.replace(";", "&"), keep_blank_values=True))


def _stamped_event_body(data: object, agent_user_id: str) -> str:
    """Return an event body marked as the agent's, on the replacement content of an edit too."""
    content = json.loads(data) if isinstance(data, str | bytes) else None
    if not isinstance(content, dict):
        msg = "A Matrix event body must be a JSON object"
        raise ValueError(msg)  # noqa: TRY004 - the body is a value the caller serialized wrongly
    content[AGENT_AUTHORED_KEY] = agent_user_id
    new_content = content.get("m.new_content")
    if isinstance(new_content, dict):
        new_content[AGENT_AUTHORED_KEY] = agent_user_id
    return nio.Api.to_json(content)


class _UserMasqueradeClient(MindRoomAsyncClient):
    """Name the person in each request; mark each message event, redaction, and state event it sends as the agent's."""

    def __init__(
        self,
        homeserver: str,
        runtime_paths: RuntimePaths,
        *,
        person_user_id: str,
        agent_user_id: str,
    ) -> None:
        super().__init__(
            homeserver,
            person_user_id,
            store_path=None,
            config=replace(matrix_client_config(), encryption_enabled=False),
            ssl=maybe_ssl_context(homeserver, runtime_paths),
        )
        self.user_id = person_user_id
        self._person_query = f"user_id={quote(person_user_id, safe='')}"
        self._agent_user_id = agent_user_id

    async def send(
        self,
        method: str,
        path: str,
        data: None | str | AsyncDataT = None,
        headers: dict[str, str] | None = None,
        trace_context: object = None,
        timeout: float | None = None,  # noqa: ASYNC109 - nio's request timeout parameter
    ) -> ClientResponse:
        """Send one request as the person; refuse one that leaves the homeserver, names a user, or is unmarkable."""
        person_path = f"{path}{'&' if '?' in path else '?'}{self._person_query}"
        # The parent requests `homeserver + path` as yarl parses it, which deletes tabs and line breaks, decodes
        # unreserved characters, and resolves dot segments, so every check reads that URL, never the path string.
        url = URL(self.homeserver + person_path)
        homeserver = URL(self.homeserver)
        if (url.scheme, url.raw_authority) != (homeserver.scheme, homeserver.raw_authority):
            # A path not starting with `/` extends the authority: another host, a port, or user info.
            msg = "A Matrix request must go to the homeserver"
            raise ValueError(msg)
        if url.raw_fragment:
            # The person's parameter follows any `#` in the path, so it would never reach the homeserver.
            msg = "A Matrix request path must not have a fragment"
            raise ValueError(msg)
        if _named_user_count(url.raw_query_string) != 1:
            # The one allowed is the person's: a homeserver may act as the first user a request names.
            msg = "The Matrix request already names a user"
            raise ValueError(msg)
        if method.upper() != "GET" and _EVENT_CREATING_PATH.search(url.raw_path):
            data = _stamped_event_body(data, self._agent_user_id)
        return await super().send(method, person_path, data, headers, trace_context, timeout)


def create_user_masquerade_client(
    runtime_paths: RuntimePaths,
    *,
    token: str,
    person_user_id: str,
    agent_user_id: str,
) -> nio.AsyncClient:
    """Return an HTTP-only client that acts as ``person_user_id`` and marks what it sends as ``agent_user_id``'s.

    What it marks is each message event, redaction, and state event it sends.
    """
    client = _UserMasqueradeClient(
        runtime_matrix_homeserver(runtime_paths),
        runtime_paths,
        person_user_id=person_user_id,
        agent_user_id=agent_user_id,
    )
    client.access_token = token
    return client
