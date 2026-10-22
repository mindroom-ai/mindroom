"""Tests for the MindRoom server state that lets MindRoom Chat find its server."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import bot as bot_module
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.matrix import client_room_admin
from mindroom.matrix.event_types import MINDROOM_SERVER_EVENT_TYPE
from mindroom.orchestration import server_state
from mindroom.orchestration.server_state import publish_server_state, server_state_content
from mindroom.worker_computer.sessions import ComputerError
from tests.conftest import make_matrix_client_mock
from tests.test_bot_ready_hook import _agent_bot

if TYPE_CHECKING:
    from pathlib import Path

ROOM = "!room:example.org"
AGENT = "@mindroom_helper:example.org"
CONFIG = Config(agents={"helper": {"display_name": "Helper"}})


def _paths(tmp_path: Path, **env: str) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)


def _client(current: nio.Response) -> MagicMock:
    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = AGENT
    client.room_get_state_event = AsyncMock(return_value=current)
    client.room_put_state = AsyncMock(return_value=nio.RoomPutStateResponse(event_id="$state", room_id=ROOM))
    return client


def _state(content: dict[str, object]) -> nio.RoomGetStateEventResponse:
    return nio.RoomGetStateEventResponse(content, MINDROOM_SERVER_EVENT_TYPE, AGENT, ROOM)


_NOT_FOUND = nio.RoomGetStateEventError("Event not found.", status_code="M_NOT_FOUND")


@pytest.mark.parametrize(
    ("env", "content"),
    [
        ({}, {"api_origin": "http://localhost:8765", "computer": False}),
        ({"MINDROOM_PORT": "9000"}, {"api_origin": "http://localhost:9000", "computer": False}),
        (
            {"MINDROOM_PUBLIC_URL": "https://Mind.Example.org/app"},
            {"api_origin": "https://mind.example.org", "computer": False},
        ),
        ({"MINDROOM_PORT": "tcp://10.0.0.1:8765"}, {}),
    ],
    ids=["local-default", "local-port", "public-url", "no-usable-origin"],
)
def test_content_names_the_origin_chat_binds_tokens_to(
    tmp_path: Path,
    env: dict[str, str],
    content: dict[str, object],
) -> None:
    """The published origin is the OpenID audience, so a Chat token bound to it verifies here."""
    assert server_state_content("helper", CONFIG, _paths(tmp_path, **env)) == content


@pytest.mark.parametrize(
    ("entity", "computers_on", "provider_error", "computer"),
    [
        ("helper", True, None, True),
        ("helper", False, None, False),
        ("helper", True, ComputerError(409, "Computer requires exactly one browser provider."), False),
        ("router", True, None, False),
    ],
    ids=["agent-with-computer", "computers-off", "agent-without-browser-worker", "not-an-agent"],
)
def test_content_says_whether_the_entity_has_a_worker_computer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entity: str,
    computers_on: bool,
    provider_error: ComputerError | None,
    computer: bool,
) -> None:
    """Chat shows the Computer button only for an agent the Computers API would serve."""
    provider = MagicMock(return_value="browser", side_effect=provider_error)
    monkeypatch.setattr(server_state, "computer_browser_provider", provider)
    env = {"MINDROOM_WORKER_COMPUTER_ENABLED": "true"} if computers_on else {}
    assert server_state_content(entity, CONFIG, _paths(tmp_path, **env))["computer"] is computer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current", "content", "written"),
    [
        (_NOT_FOUND, {"api_origin": "http://localhost:8765", "computer": False}, True),
        (
            _state({"api_origin": "http://localhost:9000", "computer": False}),
            {"api_origin": "http://localhost:8765", "computer": False},
            True,
        ),
        (
            _state({"api_origin": "http://localhost:8765", "computer": False}),
            {"api_origin": "http://localhost:8765", "computer": False},
            False,
        ),
        (_state({"api_origin": "http://localhost:8765", "computer": False}), {}, True),
        (_NOT_FOUND, {}, False),
    ],
    ids=["first-write", "changed", "unchanged", "cleared", "nothing-to-clear"],
)
async def test_publish_writes_only_what_the_room_does_not_hold(
    current: nio.Response,
    content: dict[str, object],
    written: bool,
) -> None:
    """Restarts do not add a state event to every room when nothing changed."""
    client = _client(current)
    await publish_server_state(client, ROOM, content)
    client.room_get_state_event.assert_awaited_once_with(ROOM, MINDROOM_SERVER_EVENT_TYPE, AGENT)
    if written:
        client.room_put_state.assert_awaited_once_with(ROOM, MINDROOM_SERVER_EVENT_TYPE, content, state_key=AGENT)
    else:
        client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failures_never_stop_room_setup() -> None:
    """A homeserver that refuses or fails the write only leaves Chat without the address."""
    content = {"api_origin": "http://localhost:8765", "computer": False}
    refused = _client(_NOT_FOUND)
    refused.room_put_state.return_value = nio.RoomPutStateError("Forbidden", status_code="M_FORBIDDEN")
    failing = _client(_NOT_FOUND)
    failing.room_get_state_event.side_effect = TimeoutError
    with capture_logs() as logs:
        await publish_server_state(refused, ROOM, content)
        await publish_server_state(failing, ROOM, content)
    assert [log["event"] for log in logs] == [
        "MindRoom server state was refused",
        "Publishing the MindRoom server state failed",
    ]


def test_managed_rooms_let_every_entity_publish_its_server() -> None:
    """Matrix accepts a state key starting with @ only from that user, so level 0 lets each entity write only its own."""
    client = MagicMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_router:example.org"
    power_levels = client_room_admin._create_room_initial_state(client, [], [], encrypted=False, creators=frozenset())[
        0
    ]
    assert power_levels["content"]["events"][MINDROOM_SERVER_EVENT_TYPE] == 0


@pytest.mark.asyncio
async def test_every_entity_publishes_its_server_in_each_joined_room(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agents publish too, not only the router, because Chat reads the server of the agents in the room."""
    bot = _agent_bot(tmp_path)
    bot.client = make_matrix_client_mock(user_id=bot.agent_user.user_id)
    publish = AsyncMock()
    monkeypatch.setattr(bot_module, "publish_server_state", publish)
    await bot._post_join_room_setup("!room:localhost")
    publish.assert_awaited_once_with(
        bot.client,
        "!room:localhost",
        server_state_content("code", bot.config, bot.runtime_paths),
    )
