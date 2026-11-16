"""A Matrix client that acts as one person through an application service.

An application service may act as any user in its namespace by naming that user in each request's `user_id`
parameter, so this client needs no login, device, or encryption keys of its own.
Every event it sends is marked as the agent's, so MindRoom never treats it as the person's own message.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING
from urllib.parse import quote, urlsplit

import nio

from mindroom.constants import (
    AGENT_AUTHORED_KEY,
    RuntimePaths,
    read_secret_file,
    runtime_env_path,
    runtime_matrix_homeserver,
)
from mindroom.matrix.client_session import MindRoomAsyncClient, matrix_client_config, maybe_ssl_context
from mindroom.runtime_env_policy import MATRIX_USER_APPSERVICE_TOKEN_ENV, MATRIX_USER_APPSERVICE_TOKEN_FILE_ENV

if TYPE_CHECKING:
    from aiohttp import ClientResponse
    from nio.client.async_client import AsyncDataT

__all__ = ["create_user_masquerade_client", "user_appservice_token"]

# The requests that create a room event: `send/<type>/<txn>` and `redact/<event>/<txn>`.
_EVENT_CREATING_PATH = re.compile(r"/_matrix/client/[^/]+/rooms/[^/]+/(?:send|redact)/[^/]+/[^/]+")


def user_appservice_token(runtime_paths: RuntimePaths) -> str | None:
    """Return the configured token for acting as people, or None when neither variable is set."""
    token = (runtime_paths.env_value(MATRIX_USER_APPSERVICE_TOKEN_ENV) or "").strip()
    file_path = runtime_env_path(runtime_paths, MATRIX_USER_APPSERVICE_TOKEN_FILE_ENV)
    if token and file_path is not None:
        msg = f"Set only one of {MATRIX_USER_APPSERVICE_TOKEN_ENV} or {MATRIX_USER_APPSERVICE_TOKEN_FILE_ENV}"
        raise ValueError(msg)
    if file_path is None:
        return token or None
    try:
        file_token = read_secret_file(file_path)
    except OSError as exc:
        msg = f"{MATRIX_USER_APPSERVICE_TOKEN_FILE_ENV} is not readable: {file_path}"
        raise ValueError(msg) from exc
    if not file_token:
        msg = f"{MATRIX_USER_APPSERVICE_TOKEN_FILE_ENV} points to an empty file: {file_path}"
        raise ValueError(msg)
    return file_token


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
    """Name the person in every request and mark every event it sends as the agent's."""

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
        """Send one request as the person; nothing is sent when an event body cannot be marked."""
        if method == "PUT" and _EVENT_CREATING_PATH.fullmatch(urlsplit(path).path):
            data = _stamped_event_body(data, self._agent_user_id)
        person_path = f"{path}{'&' if '?' in path else '?'}{self._person_query}"
        return await super().send(method, person_path, data, headers, trace_context, timeout)


def create_user_masquerade_client(
    runtime_paths: RuntimePaths,
    *,
    token: str,
    person_user_id: str,
    agent_user_id: str,
) -> nio.AsyncClient:
    """Return an HTTP-only client that acts as ``person_user_id`` and marks its events as ``agent_user_id``'s."""
    client = _UserMasqueradeClient(
        runtime_matrix_homeserver(runtime_paths),
        runtime_paths,
        person_user_id=person_user_id,
        agent_user_id=agent_user_id,
    )
    client.access_token = token
    return client
