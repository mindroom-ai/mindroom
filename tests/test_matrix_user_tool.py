"""The `matrix_user` toolkit: a personal agent reads Matrix as its owner, and only as its owner."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import aiohttp
import pytest

from mindroom.agents import build_agent_toolkit
from mindroom.config.main import Config
from mindroom.config.personal_agents import PersonalAgentRoster, PersonalAgentRosterEntry, personal_agent_roster_path
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.matrix_user import MatrixUserTools
from mindroom.message_target import MessageTarget
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.runtime_context import detached_tool_scope, tool_runtime_context
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.agent_tool_helpers import toolkit_offers
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup
from tests.identity_helpers import persist_entity_accounts
from tests.matrix_user_helpers import SYNC_PATH, FakeHomeserver, joined_room, sync_answer

if TYPE_CHECKING:
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.constants import RuntimePaths

OWNER = "@alice:example.com"
STRANGER = "@bob:example.com"
AGENT = "mind_alice"
ROOM = "!room:example.com"
TOKEN = "as-user-secret-7f3a9c"  # noqa: S105
NOT_SET_UP = "matrix_user is not set up: the runtime has no MATRIX_USER_APPSERVICE_TOKEN."


def _runtime_paths(tmp_path: Path, **env: str) -> RuntimePaths:
    """The primary runtime's paths, its process environment holding ``env``."""
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "https://matrix.example.com", "MINDROOM_NAMESPACE": "", **env},
    )


def _config(paths: RuntimePaths) -> Config:
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


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """The primary runtime, with the token in its environment."""
    return _runtime_paths(tmp_path, MATRIX_USER_APPSERVICE_TOKEN=TOKEN)


@pytest.fixture
def config(paths: RuntimePaths) -> Config:
    """The config with Alice's personal agent."""
    return _config(paths)


@pytest.fixture
def homeserver() -> FakeHomeserver:
    """The homeserver, listing one room for Alice."""
    return FakeHomeserver({SYNC_PATH: sync_answer(join={ROOM: joined_room("Planning", unread=2)})})


def _turn(config: Config, paths: RuntimePaths, requester_id: str = OWNER) -> AbstractContextManager[None]:
    """Enter a chat turn with Alice's personal agent that ``requester_id`` started."""
    context = make_test_tool_runtime_context(
        agent_name=AGENT,
        target=MessageTarget.resolve(ROOM, None, "$event"),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    return tool_runtime_context(context)


def _toolkit(paths: RuntimePaths, agent_name: str = AGENT) -> MatrixUserTools:
    """The toolkit as built for ``agent_name``, without a config of its own, so a turn's config decides."""
    return MatrixUserTools(runtime_paths=paths, runtime_config=None, agent_name=agent_name)


@pytest.mark.asyncio
async def test_the_owner_reads_their_rooms(config: Config, paths: RuntimePaths, homeserver: FakeHomeserver) -> None:
    """The owner's turn reads the owner's rooms, as the owner, with the service's token, on a client closed after."""
    with _turn(config, paths), homeserver.serving_the_toolkit():
        result = await _toolkit(paths).matrix_user_read(action="rooms")

    assert json.loads(result) == {
        "status": "ok",
        "tool": "matrix_user",
        "action": "rooms",
        "rooms": [f"{ROOM} | Planning | unread 2, mentions 0"],
        "left_out": 0,
    }
    [request] = homeserver.requests
    assert request.query["user_id"] == [OWNER]
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert homeserver.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requester_id", "agent_name", "message"),
    [
        (STRANGER, AGENT, "Only the agent's owner may use matrix_user."),
        (OWNER, "mind_bob", "matrix_user acts only for the agent it was built for."),
    ],
    ids=["another-person", "another-agents-toolkit"],
)
async def test_a_refused_caller_gets_the_refusal_and_no_request_is_made(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    requester_id: str,
    agent_name: str,
    message: str,
) -> None:
    """Anyone but the owner, or a toolkit built for another agent, is refused before any request."""
    with _turn(config, paths, requester_id), homeserver.serving_the_toolkit():
        result = await _toolkit(paths, agent_name).matrix_user_read(action="rooms")

    assert json.loads(result) == {"status": "error", "tool": "matrix_user", "action": "rooms", "message": message}
    assert homeserver.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, NOT_SET_UP),
        (
            {"MATRIX_USER_APPSERVICE_TOKEN": TOKEN, "MATRIX_USER_APPSERVICE_TOKEN_FILE": "token"},
            "matrix_user is not set up: Set only one of MATRIX_USER_APPSERVICE_TOKEN or "
            "MATRIX_USER_APPSERVICE_TOKEN_FILE.",
        ),
    ],
    ids=["neither-variable", "both-variables"],
)
async def test_without_a_token_the_tool_says_it_is_not_set_up(
    tmp_path: Path,
    homeserver: FakeHomeserver,
    env: dict[str, str],
    message: str,
) -> None:
    """A runtime without one usable token says so, never showing a token, and asks nothing of the homeserver."""
    paths = _runtime_paths(tmp_path, **env)
    config = _config(paths)

    with _turn(config, paths), homeserver.serving_the_toolkit():
        result = await _toolkit(paths).matrix_user_read(action="rooms")

    assert json.loads(result) == {"status": "error", "tool": "matrix_user", "action": "rooms", "message": message}
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_the_token_never_appears_in_a_result_or_an_error(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A transport error whose text holds the URL and the request's credentials says only that Matrix was unreachable."""
    with _turn(config, paths), homeserver.serving_the_toolkit():
        found = await _toolkit(paths).matrix_user_read(action="rooms")
        homeserver.failure = lambda url, headers: aiohttp.ClientConnectionError(f"{url} {headers}")
        failed = await _toolkit(paths).matrix_user_read(action="rooms")

    url = homeserver.requests[-1].url
    assert json.loads(failed) == {
        "status": "error",
        "tool": "matrix_user",
        "action": "rooms",
        "message": "The Matrix homeserver could not be reached.",
    }
    for result in (found, failed):
        assert TOKEN not in result
        assert url not in result
    assert homeserver.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "room"}, "room needs room_id, a room ID such as !abc:example.com."),
        (
            {"action": "room", "room_id": "#planning:example.com"},
            "room needs room_id, a room ID such as !abc:example.com.",
        ),
        ({"action": "people"}, "people needs query: a name, or a full Matrix ID such as @alice:example.com."),
    ],
)
async def test_each_action_names_the_arguments_it_is_missing(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
    message: str,
) -> None:
    """An action without what it needs says what is missing and asks nothing of the homeserver."""
    with _turn(config, paths), homeserver.serving_the_toolkit():
        result = await _toolkit(paths).matrix_user_read(**arguments)

    assert json.loads(result) == {
        "status": "error",
        "tool": "matrix_user",
        "action": arguments["action"],
        "message": message,
    }
    assert homeserver.requests == []


def test_the_function_takes_no_user_to_act_as(paths: RuntimePaths) -> None:
    """The model can name a room, a query and a limit, never a person to act as."""
    function = toolkit_offers(_toolkit(paths), "matrix_user_read")

    assert list(function.parameters["properties"]) == ["action", "room_id", "query", "limit"]


@pytest.mark.asyncio
async def test_a_toolkit_built_for_the_agent_reads_the_token_from_the_primary_environment(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """The agent's own build gives the toolkit the primary's paths, whose environment holds the token, and its config.

    The owner's call through the MCP gateway has no chat turn, so the config the toolkit was built with decides.
    """
    registry = builtin_tool_registry()
    toolkit = build_agent_toolkit(
        "matrix_user",
        agent_tools=resolve_agent_tools(config, AGENT, registry),
        placement=tool_placement(paths, registry),
        runtime_paths=paths,
        execution_identity=None,
    )
    assert isinstance(toolkit, MatrixUserTools)
    identity = build_tool_execution_identity(
        channel="mcp",
        agent_name=AGENT,
        runtime_paths=paths,
        requester_id=OWNER,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )

    with detached_tool_scope(identity), homeserver.serving_the_toolkit():
        result = await toolkit.matrix_user_read(action="rooms")

    assert json.loads(result)["rooms"] == [f"{ROOM} | Planning | unread 2, mentions 0"]
    [request] = homeserver.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.query["user_id"] == [OWNER]
