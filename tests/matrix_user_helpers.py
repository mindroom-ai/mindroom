"""A fake homeserver for the `matrix_user` toolkit's tests: canned JSON per request path, at the client's HTTP session."""

from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from mindroom.matrix.user_masquerade import create_user_masquerade_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    import nio

SYNC_PATH = "/_matrix/client/v3/sync"


def sync_answer(
    join: dict[str, object] | None = None,
    invite: dict[str, object] | None = None,
    direct: dict[str, list[str]] | None = None,
) -> dict[str, object]:
    """Return a `/sync` body with these joined and invited rooms and, when given, the person's `m.direct`."""
    body: dict[str, object] = {"next_batch": "s1", "rooms": {"join": join or {}, "invite": invite or {}}}
    if direct is not None:
        body["account_data"] = {"events": [{"type": "m.direct", "content": direct}]}
    return body


def state_event(
    event_type: str,
    content: dict[str, object],
    *,
    state_key: str = "",
    sender: str = "@x:example.com",
) -> dict[str, object]:
    """Return one state event."""
    return {"type": event_type, "state_key": state_key, "sender": sender, "content": content}


def joined_room(
    name: str | None = None,
    *,
    unread: int = 0,
    mentions: int = 0,
    marked_unread: bool = False,
    timeline: tuple[dict[str, object], ...] = (),
    heroes: tuple[str, ...] = (),
) -> dict[str, object]:
    """Return a joined room of a `/sync` body, its summary naming ``heroes`` when given."""
    room: dict[str, object] = {
        "state": {"events": [state_event("m.room.name", {"name": name})] if name is not None else []},
        "timeline": {"events": list(timeline), "limited": True},
        "unread_notifications": {"notification_count": unread, "highlight_count": mentions},
    }
    if marked_unread:
        room["account_data"] = {"events": [{"type": "m.marked_unread", "content": {"unread": True}}]}
    if heroes:
        room["summary"] = {"m.heroes": list(heroes), "m.joined_member_count": len(heroes) + 1}
    return room


def invited_room(person: str, inviter: str, name: str | None = None, *, is_direct: bool = False) -> dict[str, object]:
    """Return an invited room of a `/sync` body, whose stripped state holds ``person``'s invitation from ``inviter``."""
    events = [] if name is None else [state_event("m.room.name", {"name": name})]
    membership: dict[str, object] = {"membership": "invite", **({"is_direct": True} if is_direct else {})}
    events.append(state_event("m.room.member", membership, state_key=person, sender=inviter))
    return {"invite_state": {"events": events}}


@dataclass(frozen=True)
class Answer:
    """One answer of the fake homeserver."""

    body: object
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)


def matrix_error(status: int, errcode: str, error: str, **fields: object) -> Answer:
    """Return a Matrix error answer."""
    return Answer({"errcode": errcode, "error": error, **fields}, status=status)


@dataclass(frozen=True)
class HomeserverRequest:
    """One request the fake homeserver received, with its path unquoted and its JSON body parsed."""

    method: str
    url: str
    path: str
    query: dict[str, list[str]]
    body: object
    headers: dict[str, str]


@dataclass
class _Content:
    body: bytes

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        for start in range(0, len(self.body), size):
            yield self.body[start : start + size]


@dataclass
class _Response:
    status: int
    headers: dict[str, str]
    content: _Content

    def release(self) -> None:
        return None


@dataclass
class FakeHomeserver:
    """Stands in for a client's HTTP session and answers each request by its unquoted path.

    A path with a list of answers gives them in turn and then repeats the last; a path with none is a 404 `M_NOT_FOUND`.
    """

    answers: dict[str, Answer | list[Answer] | dict[str, object]] = field(default_factory=dict)
    # Raised for each request instead of an answer, as a failed transport raises it.
    failure: Exception | None = None
    requests: list[HomeserverRequest] = field(default_factory=list)
    closed: bool = False

    def serve(self, client: nio.AsyncClient) -> nio.AsyncClient:
        """Make ``client`` send its requests here, and return it."""
        client.client_session = self  # type: ignore[assignment]
        return client

    @contextmanager
    def serving_the_toolkit(self) -> Iterator[None]:
        """Make each client the `matrix_user` toolkit creates send its requests here."""

        def create(*args: Any, **kwargs: Any) -> nio.AsyncClient:  # noqa: ANN401
            return self.serve(create_user_masquerade_client(*args, **kwargs))

        with patch("mindroom.custom_tools.matrix_user.create_user_masquerade_client", create):
            yield

    def paths(self) -> list[str]:
        """Return the path of each request, in order."""
        return [request.path for request in self.requests]

    async def request(
        self,
        method: str,
        url: str,
        *,
        data: object = None,
        headers: dict[str, str],
        **_kwargs: object,
    ) -> _Response:
        """Record the request and answer it, or raise the transport failure."""
        split = urlsplit(url)
        path = unquote(split.path)
        body = json.loads(data) if isinstance(data, str) else None
        self.requests.append(HomeserverRequest(method, url, path, parse_qs(split.query), body, dict(headers)))
        if self.failure is not None:
            raise self.failure
        answer = self._answer(path)
        content = answer.body if isinstance(answer.body, bytes) else json.dumps(answer.body).encode()
        return _Response(answer.status, answer.headers, _Content(content))

    def _answer(self, path: str) -> Answer:
        answer = self.answers.get(path)
        if answer is None:
            return matrix_error(404, "M_NOT_FOUND", "Not found")
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer if isinstance(answer, Answer) else Answer(answer)

    async def close(self) -> None:
        """Close the session, as the client does when it is done."""
        self.closed = True
