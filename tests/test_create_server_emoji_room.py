"""The server emoji room script creates a hidden, local room whose emoji pack everyone may extend."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from scripts.utilities.create_server_emoji_room import create_server_emoji_room, main

SERVER_POWER_LEVELS = {
    "events": {"m.room.name": 50, "m.room.power_levels": 100},
    "events_default": 0,
    "users": {"@op:example.com": 100},
}


class MatrixAPI:
    """Answer only the Matrix endpoints this script uses and keep what it sent."""

    def __init__(self) -> None:
        self.created: dict[str, Any] = {}
        self.server_levels: dict[str, Any] = SERVER_POWER_LEVELS
        self.power_levels: dict[str, Any] = {}
        self.authorization: str | None = None
        self.alias_taken = False
        self.redirect = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Implement room creation, power levels, and the operator's identity."""
        path = request.url.path
        self.authorization = request.headers.get("Authorization")
        if request.method == "POST" and path.endswith("/createRoom"):
            if self.alias_taken:
                return httpx.Response(400, json={"errcode": "M_ROOM_IN_USE", "error": "Room alias already taken"})
            self.created = json.loads(request.content)
            return httpx.Response(200, json={"room_id": "!new:example.com"})
        if path.endswith("/rooms/!new:example.com/state/m.room.power_levels"):
            if request.method == "GET":
                return httpx.Response(200, json=self.server_levels)
            if request.method == "PUT":
                self.power_levels = json.loads(request.content)
                return httpx.Response(200, json={"event_id": "$levels"})
        if request.method == "GET" and path.endswith("/account/whoami"):
            if self.redirect:
                return httpx.Response(308, headers={"Location": "https://matrix.example.com/"}, text="<html></html>")
            return httpx.Response(200, json={"user_id": "@op:example.com"})
        message = f"Unexpected Matrix request: {request.method} {path}"
        raise AssertionError(message)

    def client(self) -> httpx.Client:
        """Use a real HTTP client with only its network transport replaced."""
        return httpx.Client(base_url="https://matrix.example.com", transport=httpx.MockTransport(self.handle))


def server_levels_with_emoji_level(level: int) -> dict[str, Any]:
    """Return the power levels the fake server holds, plus the level for adding emoji and for posting."""
    return {
        **SERVER_POWER_LEVELS,
        "events": {**SERVER_POWER_LEVELS["events"], "im.ponies.room_emotes": level},
        "events_default": 50,
    }


def test_creates_a_hidden_local_room_with_an_empty_emoticon_pack() -> None:
    """The room type hides it from room lists, and a pack at state key "" is ready for the first emoji."""
    api = MatrixAPI()
    with api.client() as client:
        create_server_emoji_room(client, name="Example", alias="emoji", moderators_only=False)
    assert api.created == {
        "name": "Example",
        "room_alias_name": "emoji",
        "preset": "public_chat",
        "visibility": "private",
        "creation_content": {"type": "io.mindroom.emoji", "m.federate": False},
        "initial_state": [
            {
                "type": "im.ponies.room_emotes",
                "state_key": "",
                "content": {"pack": {"display_name": "Example", "usage": ["emoticon"]}, "images": {}},
            },
        ],
    }


def test_lets_everyone_add_emoji_but_only_moderators_post_and_keeps_the_other_power_levels() -> None:
    """Only the emoji and message levels change, so members cannot post and names and settings stay moderator-only."""
    api = MatrixAPI()
    with api.client() as client:
        create_server_emoji_room(client, name="Example", alias="emoji", moderators_only=False)
    assert api.power_levels == server_levels_with_emoji_level(0)


def test_raises_explicit_message_levels_below_50() -> None:
    """A server whose defaults let members send messages by type still ends up with a room members cannot post in."""
    api = MatrixAPI()
    api.server_levels = {
        **SERVER_POWER_LEVELS,
        "events": {**SERVER_POWER_LEVELS["events"], "m.room.message": 0, "m.room.encrypted": 10},
    }
    with api.client() as client:
        create_server_emoji_room(client, name="Example", alias="emoji", moderators_only=False)
    assert api.power_levels["events"]["m.room.message"] == 50
    assert api.power_levels["events"]["m.room.encrypted"] == 50
    assert api.power_levels["events"]["m.room.name"] == 50
    assert api.power_levels["events"]["im.ponies.room_emotes"] == 0


def test_keeps_a_message_level_that_is_already_higher_than_50() -> None:
    """A server that already limits posting to admins is not loosened to moderators."""
    api = MatrixAPI()
    api.server_levels = {**SERVER_POWER_LEVELS, "events_default": 100}
    with api.client() as client:
        create_server_emoji_room(client, name="Example", alias="emoji", moderators_only=False)
    assert api.power_levels["events_default"] == 100


def test_moderators_only_requires_power_level_50() -> None:
    """Moderators-only keeps adding, editing, and removing emoji to power level 50."""
    api = MatrixAPI()
    with api.client() as client:
        create_server_emoji_room(client, name="Example", alias="emoji", moderators_only=True)
    assert api.power_levels == server_levels_with_emoji_level(50)


def test_returns_the_full_alias_on_the_operator_server() -> None:
    """The alias belongs to the server of the operator's account, not to the URL the script talked to."""
    api = MatrixAPI()
    with api.client() as client:
        result = create_server_emoji_room(client, name="Example", alias="emoji", moderators_only=False)
    assert result == ("!new:example.com", "#emoji:example.com")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> MatrixAPI:
    """Point `main` at the fake homeserver and give it an operator's token."""
    fake = MatrixAPI()
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(fake.handle), **kwargs),
    )
    monkeypatch.setenv("MATRIX_ACCESS_TOKEN", "operator-token")
    return fake


def test_main_prints_the_room_id_and_the_well_known_key(api: MatrixAPI, capsys: pytest.CaptureFixture[str]) -> None:
    """The operator gets the room ID and the key to serve; every flag and the token reach Matrix."""
    homeserver_and_name = ["--homeserver", "https://matrix.example.com/", "--name", "Example"]
    assert main([*homeserver_and_name, "--alias", "stickers", "--moderators-only"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "Room ID: !new:example.com",
        'Add to /.well-known/matrix/client: {"io.mindroom.emoji": {"room": "#stickers:example.com"}}',
    ]
    assert api.created["room_alias_name"] == "stickers"
    assert api.power_levels == server_levels_with_emoji_level(50)
    assert api.authorization == "Bearer operator-token"


def test_main_reports_why_matrix_refused_and_leaves_the_power_levels_alone(
    api: MatrixAPI,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A taken alias is reported with Matrix's own error, so the operator knows to pick another."""
    api.alias_taken = True
    assert main(["--homeserver", "https://matrix.example.com", "--name", "Example"]) == 1
    assert "M_ROOM_IN_USE" in capsys.readouterr().err
    assert api.power_levels == {}


def test_main_reports_a_redirect_instead_of_reading_it_as_json(
    api: MatrixAPI,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A homeserver URL that redirects (for example http to https) is named in the error, and nothing is created."""
    api.redirect = True
    assert main(["--homeserver", "https://matrix.example.com", "--name", "Example"]) == 1
    error = capsys.readouterr().err
    assert "308" in error
    assert "/account/whoami" in error
    assert api.created == {}
