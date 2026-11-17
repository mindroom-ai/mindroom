"""The `matrix_user` toolkit's tests: Alice's personal agent and its turn, and a fake homeserver at the client's session.

The homeserver answers canned JSON per request path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, unquote, urlsplit

from mindroom.config.main import Config
from mindroom.config.personal_agents import PersonalAgentRoster, PersonalAgentRosterEntry, personal_agent_roster_path
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.matrix_user import MatrixUserTools
from mindroom.matrix.user_masquerade import create_user_masquerade_client
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from contextlib import AbstractContextManager
    from pathlib import Path

    import nio

    from mindroom.constants import RuntimePaths

OWNER = "@alice:example.com"
# Alice's personal agent.
AGENT = "mind_alice"
# The room of the agent's turn.
ROOM = "!room:example.com"
TOKEN = "as-user-secret-7f3a9c"  # noqa: S105
SYNC_PATH = "/_matrix/client/v3/sync"
# The time of every room event the helpers make, plus its ``minute``.
NOON = int(datetime(2026, 11, 16, 12, 0, tzinfo=UTC).timestamp() * 1000)


def room_event(
    event_id: str,
    event_type: str,
    content: dict[str, object],
    *,
    sender: str = "@bob:example.com",
    minute: int = 0,
    **fields: object,
) -> dict[str, object]:
    """Return one room event, sent ``minute`` minutes after noon, with any other top-level ``fields``."""
    return {
        "event_id": event_id,
        "type": event_type,
        "sender": sender,
        "origin_server_ts": NOON + minute * 60_000,
        "content": content,
        **fields,
    }


def text_message(
    event_id: str,
    body: str,
    *,
    sender: str = "@bob:example.com",
    minute: int = 0,
    **fields: object,
) -> dict[str, object]:
    """Return a text message."""
    content = {"msgtype": "m.text", "body": body}
    return room_event(event_id, "m.room.message", content, sender=sender, minute=minute, **fields)


def edit_of(
    event_id: str,
    original: str,
    body: str,
    *,
    sender: str = "@bob:example.com",
    minute: int = 1,
    **fields: object,
) -> dict[str, object]:
    """Return an edit that replaces ``original``'s text with ``body``."""
    content = {
        "msgtype": "m.text",
        "body": f"* {body}",
        "m.new_content": {"msgtype": "m.text", "body": body},
        "m.relates_to": {"rel_type": "m.replace", "event_id": original},
    }
    return room_event(event_id, "m.room.message", content, sender=sender, minute=minute, **fields)


def reaction(event_id: str, target: str, key: str, *, sender: str = "@bob:example.com") -> dict[str, object]:
    """Return ``sender``'s reaction ``key`` to ``target``."""
    content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": target, "key": key}}
    return room_event(event_id, "m.reaction", content, sender=sender, minute=2)


def message_line(event_id: str, text: str, *, sender: str = "@bob:example.com", minute: int = 0) -> str:
    """Return the line the toolkit shows for a message the helpers made."""
    return f"{event_id} | 2026-11-16T12:{minute:02}Z | {sender} | {text}"


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
    # The bytes of an upload, whose body is a stream rather than JSON text.
    uploaded: bytes | None = None


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
    # What nio reads besides the body.
    content_type: str = "application/json"
    content_disposition: None = None

    def release(self) -> None:
        return None

    async def json(self) -> object:
        return json.loads(self.content.body)

    async def text(self) -> str:
        return self.content.body.decode()


@dataclass
class FakeHomeserver:
    """Stands in for a client's HTTP session and answers each request by its unquoted path.

    A path with a list of answers gives them in turn and then repeats the last; a path with an exception raises it, as
    a failed transport does; a path with none is a 404 `M_NOT_FOUND`. A path ending in `*` answers every path it
    starts that has no answer of its own, such as a send's, which ends in a new transaction ID.
    """

    answers: dict[str, Answer | list[Answer] | dict[str, object] | Exception] = field(default_factory=dict)
    # Raised for each request instead of an answer, as a failed transport raises it.
    failure: Exception | None = None
    requests: list[HomeserverRequest] = field(default_factory=list)
    closed: bool = False

    def serve(self, client: nio.AsyncClient) -> nio.AsyncClient:
        """Make ``client`` send its requests here, and return it."""
        client.client_session = self  # type: ignore[assignment]
        return client

    def requests_to(self, path: str) -> list[HomeserverRequest]:
        """Return the requests to ``path``, in order."""
        return [request for request in self.requests if request.path == path]

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
        uploaded = b"".join([chunk async for chunk in data]) if isinstance(data, AsyncIterable) else None
        query = parse_qs(split.query)
        self.requests.append(HomeserverRequest(method, url, path, query, body, dict(headers), uploaded))
        if self.failure is not None:
            raise self.failure
        answer = self._answer(path)
        content = answer.body if isinstance(answer.body, bytes) else json.dumps(answer.body).encode()
        return _Response(answer.status, answer.headers, _Content(content))

    def _answer(self, path: str) -> Answer:
        answer = self.answers.get(path)
        if answer is None:
            answer = next(
                (found for key, found in self.answers.items() if key.endswith("*") and path.startswith(key[:-1])),
                None,
            )
        if answer is None:
            return matrix_error(404, "M_NOT_FOUND", "Not found")
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, list):
            return answer.pop(0) if len(answer) > 1 else answer[0]
        return answer if isinstance(answer, Answer) else Answer(answer)

    async def close(self) -> None:
        """Close the session, as the client does when it is done."""
        self.closed = True


def owner_runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    """The primary runtime's paths, its process environment holding ``env``."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.com", "MINDROOM_NAMESPACE": "", **env},
    )


def owner_config(paths: RuntimePaths) -> Config:
    """Alice's personal agent, made from a template that gives it `matrix_user`, with its Matrix account recorded."""
    entry = PersonalAgentRosterEntry(
        template="mind",
        user_id=OWNER,
        username="alice",
        agent_name=AGENT,
        matrix_username=None,
        user_display_name="Alice",
        source_room_id="!lobby:example.com",
    )
    roster_path = personal_agent_roster_path(paths)
    roster_path.parent.mkdir(parents=True, exist_ok=True)
    roster_path.write_text(PersonalAgentRoster(entries=[entry]).model_dump_json(), encoding="utf-8")
    data = {
        "models": {"default": {"provider": "openai", "id": "m"}},
        "personal_agents": {
            "mind": {
                "agent": {"display_name": "{user_display_name}'s Mind", "model": "default", "tools": ["matrix_user"]},
            },
        },
    }
    config = Config.validate_with_runtime(data, paths)
    persist_entity_accounts(config, paths, usernames={AGENT: "mind_alice"})
    return config


def owner_turn(
    config: Config,
    paths: RuntimePaths,
    requester_id: str = OWNER,
    agent_name: str = AGENT,
    attachment_ids: tuple[str, ...] = (),
) -> AbstractContextManager[None]:
    """Enter a chat turn in ``ROOM`` with ``agent_name``, Alice's personal agent unless named, that ``requester_id`` started.

    The turn holds the attachments ``attachment_ids`` name.
    """
    context = make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(ROOM, None, "$event"),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        attachment_ids=attachment_ids,
    )
    return tool_runtime_context(context)


def owner_toolkit(config: Config, paths: RuntimePaths, agent_name: str = AGENT) -> MatrixUserTools:
    """The toolkit as built for ``agent_name``."""
    return MatrixUserTools(runtime_paths=paths, runtime_config=config, agent_name=agent_name)
