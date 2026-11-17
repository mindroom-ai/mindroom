"""Tests for the Matrix client that acts as one person through an application service."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

import nio
import pytest

from mindroom.constants import AGENT_AUTHORED_KEY, RuntimePaths, resolve_runtime_paths
from mindroom.matrix.user_masquerade import create_user_masquerade_client, user_appservice_token

if TYPE_CHECKING:
    from pathlib import Path

APPSERVICE_TOKEN = "as-user-secret"  # noqa: S105
PERSON = "@alice:example.com"
AGENT = "@mind:example.com"
ROOM = "!room:example.com"


@dataclass(frozen=True)
class _Request:
    method: str
    url: str
    data: object
    headers: dict[str, str]


@dataclass
class _Response:
    body: dict[str, object]
    status: int = 200
    content_type: str = "application/json"
    content_disposition: None = None

    async def json(self) -> dict[str, object]:
        return self.body

    async def text(self) -> str:
        return json.dumps(self.body)


@dataclass
class _RecordingSession:
    """Stands in for the client's aiohttp session and answers every request with one body."""

    body: dict[str, object]
    requests: list[_Request] = field(default_factory=list)

    async def request(
        self,
        method: str,
        url: str,
        *,
        data: object = None,
        headers: dict[str, str],
        **_kwargs: object,
    ) -> _Response:
        self.requests.append(_Request(method, url, data, headers))
        return _Response(self.body)

    async def close(self) -> None:
        return None


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.com", **env},
    )


def _client(tmp_path: Path, body: dict[str, object], **env: str) -> tuple[nio.AsyncClient, _RecordingSession]:
    client = create_user_masquerade_client(
        _runtime_paths(tmp_path, **env),
        token=APPSERVICE_TOKEN,
        person_user_id=PERSON,
        agent_user_id=AGENT,
    )
    session = _RecordingSession(body)
    client.client_session = session  # type: ignore[assignment]
    return client, session


def _query(request: _Request) -> dict[str, list[str]]:
    return parse_qs(urlsplit(request.url).query)


def _sent_body(request: _Request) -> dict[str, object]:
    assert isinstance(request.data, str)
    return json.loads(request.data)


def test_the_client_is_the_person_over_http_only(tmp_path: Path) -> None:
    """The client holds no device or encryption store, and acts as the person with the service's token."""
    client = create_user_masquerade_client(
        _runtime_paths(tmp_path),
        token=APPSERVICE_TOKEN,
        person_user_id=PERSON,
        agent_user_id=AGENT,
    )

    assert client.user_id == PERSON
    assert client.access_token == APPSERVICE_TOKEN
    assert client.homeserver == "https://matrix.example.com"
    assert client.store_path is None
    assert client.config.encryption_enabled is False
    assert client.olm is None


@pytest.mark.asyncio
async def test_every_request_names_the_person(tmp_path: Path) -> None:
    """The service acts as the person only when each request names them."""
    client, session = _client(tmp_path, {"joined_rooms": [ROOM]})

    response = await client.joined_rooms()

    assert isinstance(response, nio.JoinedRoomsResponse)
    assert response.rooms == [ROOM]
    [request] = session.requests
    assert request.url == "https://matrix.example.com/_matrix/client/v3/joined_rooms?user_id=%40alice%3Aexample.com"


@pytest.mark.asyncio
async def test_a_path_with_a_query_keeps_it_and_gains_the_person(tmp_path: Path) -> None:
    """A request that already has parameters keeps each of them and names the person once."""
    client, session = _client(tmp_path, {"chunk": [], "start": "s1", "end": "s2"})

    await client.room_messages(ROOM, start="s1", limit=5)

    [request] = session.requests
    query = _query(request)
    assert query["from"] == ["s1"]
    assert query["limit"] == ["5"]
    assert query["dir"] == ["b"]
    assert query["user_id"] == [PERSON]
    assert request.url.count("user_id=") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "user_id=%40bob%3Aexample.com",
        "limit=5&user_id=%40bob%3Aexample.com",
        "user%5Fid=%40bob%3Aexample.com",
        "limit=5;user_id=%40bob%3Aexample.com",
        "user_id",
    ],
)
async def test_a_path_that_already_names_a_user_is_refused(tmp_path: Path, query: str) -> None:
    """A homeserver may act as the first user a request names, so a request naming anyone already is never sent."""
    client, session = _client(tmp_path, {"joined_rooms": []})

    with pytest.raises(ValueError, match="already names a user"):
        await client.send("GET", f"/_matrix/client/v3/joined_rooms?{query}")

    assert session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("character", ["\t", "\r", "\n"], ids=["tab", "carriage-return", "line-feed"])
async def test_a_user_hidden_behind_a_control_character_is_refused(tmp_path: Path, character: str) -> None:
    """The HTTP library deletes tabs and line breaks from a URL, so a key holding one still names a user."""
    client, session = _client(tmp_path, {"joined_rooms": []})

    with pytest.raises(ValueError, match="already names a user"):
        await client.send("GET", f"/_matrix/client/v3/joined_rooms?user{character}_id=%40bob%3Aexample.com")

    assert session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "@evil.example/_matrix/client/v3/joined_rooms",
        ".evil.example/_matrix/client/v3/joined_rooms",
    ],
    ids=["user-info", "host-suffix"],
)
async def test_a_path_that_leaves_the_homeserver_is_refused(tmp_path: Path, path: str) -> None:
    """The URL is the homeserver followed by the path, so a path not starting with `/` can name another host."""
    client, session = _client(tmp_path, {"joined_rooms": []})

    with pytest.raises(ValueError, match="must go to the homeserver") as raised:
        await client.send("GET", path)

    assert session.requests == []
    assert APPSERVICE_TOKEN not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["#top", "?limit=5#top", "#"])
async def test_a_path_with_a_fragment_is_refused(tmp_path: Path, suffix: str) -> None:
    """A parameter appended after a fragment never reaches the homeserver, so such a path is never sent."""
    client, session = _client(tmp_path, {"joined_rooms": []})

    with pytest.raises(ValueError, match="fragment"):
        await client.send("GET", f"/_matrix/client/v3/joined_rooms{suffix}")

    assert session.requests == []


@pytest.mark.asyncio
async def test_the_token_is_a_bearer_header_and_never_in_the_url(tmp_path: Path) -> None:
    """The service's token travels only in the Authorization header."""
    client, session = _client(tmp_path, {"joined_rooms": []})

    await client.joined_rooms()

    [request] = session.requests
    assert request.headers["Authorization"] == f"Bearer {APPSERVICE_TOKEN}"
    assert APPSERVICE_TOKEN not in request.url
    assert "access_token" not in request.url


@pytest.mark.asyncio
async def test_a_sent_message_is_stamped(tmp_path: Path) -> None:
    """A message the agent sends as the person carries the agent's marker."""
    client, session = _client(tmp_path, {"event_id": "$sent"})

    response = await client.room_send(ROOM, "m.room.message", {"msgtype": "m.text", "body": "hi"})

    assert isinstance(response, nio.RoomSendResponse)
    [request] = session.requests
    assert request.method == "PUT"
    assert _query(request)["user_id"] == [PERSON]
    assert _sent_body(request) == {"msgtype": "m.text", "body": "hi", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
async def test_an_edit_is_stamped_on_both_layers(tmp_path: Path) -> None:
    """An edit carries the marker on its fallback and on the replacement content clients render."""
    client, session = _client(tmp_path, {"event_id": "$edit"})

    await client.room_send(
        ROOM,
        "m.room.message",
        {
            "msgtype": "m.text",
            "body": "* hello",
            "m.new_content": {"msgtype": "m.text", "body": "hello"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        },
    )

    [request] = session.requests
    assert _sent_body(request) == {
        "msgtype": "m.text",
        "body": "* hello",
        "m.new_content": {"msgtype": "m.text", "body": "hello", AGENT_AUTHORED_KEY: AGENT},
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$original"},
        AGENT_AUTHORED_KEY: AGENT,
    }


@pytest.mark.asyncio
async def test_a_reaction_is_stamped(tmp_path: Path) -> None:
    """A reaction is an event the agent sends, so it carries the marker too."""
    client, session = _client(tmp_path, {"event_id": "$reaction"})
    relation = {"rel_type": "m.annotation", "event_id": "$target", "key": "👍"}

    await client.room_send(ROOM, "m.reaction", {"m.relates_to": relation})

    [request] = session.requests
    assert _sent_body(request) == {"m.relates_to": relation, AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
async def test_a_redaction_is_stamped(tmp_path: Path) -> None:
    """A redaction the agent makes as the person carries the marker."""
    client, session = _client(tmp_path, {"event_id": "$redaction"})

    response = await client.room_redact(ROOM, "$target", reason="sent by mistake")

    assert isinstance(response, nio.RoomRedactResponse)
    [request] = session.requests
    assert request.method == "PUT"
    assert "/rooms/%21room%3Aexample.com/redact/%24target/" in request.url
    assert _sent_body(request) == {"reason": "sent by mistake", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
async def test_a_post_send_without_a_transaction_id_is_stamped(tmp_path: Path) -> None:
    """Homeservers also accept a message posted without a transaction ID, and it carries the marker too."""
    client, session = _client(tmp_path, {"event_id": "$sent"})

    await client.send(
        "POST",
        "/_matrix/client/v3/rooms/%21room%3Aexample.com/send/m.room.message",
        json.dumps({"msgtype": "m.text", "body": "hi"}),
    )

    [request] = session.requests
    assert _sent_body(request) == {"msgtype": "m.text", "body": "hi", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["put", "post", "Put"])
async def test_a_lower_case_method_is_stamped(tmp_path: Path, method: str) -> None:
    """The HTTP layer sends a lower-case method as its upper-case form, so it is stamped the same."""
    client, session = _client(tmp_path, {"event_id": "$sent"})

    await client.send(
        method,
        "/_matrix/client/v3/rooms/%21room%3Aexample.com/send/m.room.message/txn1",
        json.dumps({"msgtype": "m.text", "body": "hi"}),
    )

    [request] = session.requests
    assert _sent_body(request) == {"msgtype": "m.text", "body": "hi", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
async def test_a_post_redact_without_a_transaction_id_is_stamped(tmp_path: Path) -> None:
    """Homeservers also accept a redaction posted without a transaction ID, and it carries the marker too."""
    client, session = _client(tmp_path, {"event_id": "$redaction"})

    await client.send(
        "POST",
        "/_matrix/client/v3/rooms/%21room%3Aexample.com/redact/%24target",
        json.dumps({"reason": "sent by mistake"}),
    )

    [request] = session.requests
    assert _sent_body(request) == {"reason": "sent by mistake", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/_matrix/client//rooms/%21room%3Aexample.com/send/m.room.message/txn1",
        "/_matrix/client/v3/rooms//send/m.room.message/txn1",
        "/_matrix/client/v3/rooms/%21room%3Aexample.com/send//txn1",
        "/_matrix/client/v3/rooms/%21room%3Aexample.com/send/m.room.message/",
        "/_matrix/client/v3/rooms/%21room%3Aexample.com/send/",
        "/_matrix/client/v3/rooms/%21room%3Aexample.com/redact//txn1",
    ],
)
async def test_an_event_path_with_an_empty_segment_is_stamped(tmp_path: Path, path: str) -> None:
    """A homeserver may route a path with an empty segment, so the event it creates carries the marker too."""
    client, session = _client(tmp_path, {"event_id": "$sent"})

    await client.send("PUT", path, json.dumps({"body": "hi"}))

    [request] = session.requests
    assert _sent_body(request) == {"body": "hi", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/_matrix/client/v3/rooms/R/%73end/m.room.message/txn1",
        "/%5Fmatrix/client/v3/rooms/R/send/m.room.message/txn1",
        "/_matrix/client/v3/rooms/R/x/../send/m.room.message/txn1",
        "/_matrix/client/v3/rooms/R/./send/m.room.message/txn1",
        "/_matrix/client/v3/rooms/R/se\tnd/m.room.message/txn1",
    ],
    ids=["encoded-letter", "encoded-underscore", "parent-segment", "current-segment", "tab"],
)
async def test_a_path_that_normalizes_into_a_send_route_is_stamped(tmp_path: Path, path: str) -> None:
    """The HTTP library normalizes the URL before requesting it, so a path that becomes a send route is stamped."""
    client, session = _client(tmp_path, {"event_id": "$sent"})

    await client.send("PUT", path, json.dumps({"body": "hi"}))

    [request] = session.requests
    assert _sent_body(request) == {"body": "hi", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
@pytest.mark.parametrize("homeserver", ["https://matrix.example.com/", "https://example.com/matrix"])
async def test_a_homeserver_url_with_a_path_still_stamps(tmp_path: Path, homeserver: str) -> None:
    """The requested path starts with the homeserver URL's own path, so the send route is matched at its end."""
    client, session = _client(tmp_path, {"event_id": "$sent"}, MATRIX_HOMESERVER=homeserver)

    await client.room_send(ROOM, "m.room.message", {"msgtype": "m.text", "body": "hi"})

    [request] = session.requests
    assert _sent_body(request) == {"msgtype": "m.text", "body": "hi", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
async def test_a_model_supplied_marker_is_overwritten(tmp_path: Path) -> None:
    """Content cannot claim another author: the marker always names the agent that sent it."""
    client, session = _client(tmp_path, {"event_id": "$sent"})

    await client.room_send(
        ROOM,
        "m.room.message",
        {
            "msgtype": "m.text",
            "body": "* hi",
            AGENT_AUTHORED_KEY: "@other:example.com",
            "m.new_content": {"msgtype": "m.text", "body": "hi", AGENT_AUTHORED_KEY: False},
        },
    )

    [request] = session.requests
    body = _sent_body(request)
    assert body[AGENT_AUTHORED_KEY] == AGENT
    assert body["m.new_content"] == {"msgtype": "m.text", "body": "hi", AGENT_AUTHORED_KEY: AGENT}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('["not", "an", "object"]', "must be a JSON object"),
        ("not json", "Expecting value"),
        (None, "must be a JSON object"),
    ],
)
async def test_a_send_body_that_is_not_a_json_object_is_refused(
    tmp_path: Path,
    body: str | None,
    message: str,
) -> None:
    """An event that cannot carry the marker is never sent."""
    client, session = _client(tmp_path, {"event_id": "$sent"})

    with pytest.raises(ValueError, match=message):
        await client.send("PUT", "/_matrix/client/v3/rooms/%21room%3Aexample.com/send/m.room.message/txn1", body)

    assert session.requests == []


@pytest.mark.asyncio
async def test_state_and_account_data_writes_are_not_stamped(tmp_path: Path) -> None:
    """Only events sent into a room carry the marker; state and account data pass unchanged, naming the person."""
    client, session = _client(tmp_path, {"event_id": "$state"})
    account_data = json.dumps({"@bob:example.com": [ROOM]})

    await client.room_put_state(ROOM, "m.room.topic", {"topic": "Plans"})
    await client.send(
        "PUT",
        "/_matrix/client/v3/user/%40alice%3Aexample.com/account_data/m.direct",
        account_data,
    )

    state, direct = session.requests
    assert "/rooms/%21room%3Aexample.com/state/m.room.topic" in state.url
    assert _sent_body(state) == {"topic": "Plans"}
    assert direct.data == account_data
    assert _query(state)["user_id"] == _query(direct)["user_id"] == [PERSON]


@pytest.mark.parametrize("source", ["variable", "file"])
def test_the_token_comes_from_the_variable_or_its_file(tmp_path: Path, source: str) -> None:
    """The token is read from MATRIX_USER_APPSERVICE_TOKEN or the file MATRIX_USER_APPSERVICE_TOKEN_FILE names."""
    if source == "variable":
        runtime_paths = _runtime_paths(tmp_path, MATRIX_USER_APPSERVICE_TOKEN=APPSERVICE_TOKEN)
    else:
        token_file = tmp_path / "user-appservice-token"
        token_file.write_text(f"{APPSERVICE_TOKEN}\n", encoding="utf-8")
        runtime_paths = _runtime_paths(tmp_path, MATRIX_USER_APPSERVICE_TOKEN_FILE=str(token_file))

    assert user_appservice_token(runtime_paths) == APPSERVICE_TOKEN


def test_no_token_is_none(tmp_path: Path) -> None:
    """Without either variable the feature is off, which is not an error."""
    assert user_appservice_token(_runtime_paths(tmp_path)) is None


def test_both_token_variables_are_refused(tmp_path: Path) -> None:
    """Two sources for one secret are a misconfiguration, and the error never shows the token."""
    token_file = tmp_path / "user-appservice-token"
    token_file.write_text(APPSERVICE_TOKEN, encoding="utf-8")
    runtime_paths = _runtime_paths(
        tmp_path,
        MATRIX_USER_APPSERVICE_TOKEN=APPSERVICE_TOKEN,
        MATRIX_USER_APPSERVICE_TOKEN_FILE=str(token_file),
    )

    with pytest.raises(ValueError, match="Set only one of") as raised:
        user_appservice_token(runtime_paths)

    assert APPSERVICE_TOKEN not in str(raised.value)


@pytest.mark.parametrize(("contents", "message"), [(None, "not readable"), ("\n", "empty file")])
def test_an_unreadable_or_empty_token_file_is_refused(tmp_path: Path, contents: str | None, message: str) -> None:
    """A token file that is missing or blank is a misconfiguration, not a missing variable."""
    token_file = tmp_path / "user-appservice-token"
    if contents is not None:
        token_file.write_text(contents, encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path, MATRIX_USER_APPSERVICE_TOKEN_FILE=str(token_file))

    with pytest.raises(ValueError, match=message):
        user_appservice_token(runtime_paths)
