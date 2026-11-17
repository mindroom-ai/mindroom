"""The `matrix_user` toolkit: a personal agent reads Matrix as its owner, and only as its owner."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from structlog.testing import capture_logs

from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.connection_agents import resolve_connection_agent, resolve_connection_user
from mindroom.attachments.storage import load_attachment
from mindroom.config.main import Config
from mindroom.config.personal_agents import PersonalAgentRoster, PersonalAgentRosterEntry, personal_agent_roster_path
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.matrix_user import MatrixUserTools
from mindroom.mcp_gateway import tools as gateway
from mindroom.message_target import MessageTarget
from mindroom.teams.members import resolve_exact_team_members
from mindroom.tool_system.runtime_context import (
    current_execution_identity,
    get_tool_runtime_context,
    member_tool_scope,
    tool_runtime_context,
)
from tests.agent_tool_helpers import toolkit_offers
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup
from tests.identity_helpers import persist_entity_accounts
from tests.matrix_user_helpers import SYNC_PATH, Answer, FakeHomeserver, joined_room, room_event, sync_answer

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


def _turn(
    config: Config,
    paths: RuntimePaths,
    requester_id: str = OWNER,
    agent_name: str = AGENT,
) -> AbstractContextManager[None]:
    """Enter a chat turn with ``agent_name``, Alice's personal agent unless named, that ``requester_id`` started."""
    context = make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(ROOM, None, "$event"),
        requester_id=requester_id,
        client=MagicMock(),
        config=config,
        runtime_paths=paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )
    return tool_runtime_context(context)


def _toolkit(config: Config, paths: RuntimePaths, agent_name: str = AGENT) -> MatrixUserTools:
    """The toolkit as built for ``agent_name``."""
    return MatrixUserTools(runtime_paths=paths, runtime_config=config, agent_name=agent_name)


@pytest.mark.asyncio
async def test_the_owner_reads_their_rooms(config: Config, paths: RuntimePaths, homeserver: FakeHomeserver) -> None:
    """The owner's turn reads the owner's rooms, as the owner, with the service's token, on a client closed after."""
    with _turn(config, paths), homeserver.serving_the_toolkit():
        result = await _toolkit(config, paths).matrix_user_read(action="rooms")

    assert json.loads(result) == {
        "status": "ok",
        "tool": "matrix_user",
        "action": "rooms",
        "rooms": [f"{ROOM} | Planning | unread 2"],
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
        result = await _toolkit(config, paths, agent_name).matrix_user_read(action="rooms")

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
        result = await _toolkit(config, paths).matrix_user_read(action="rooms")

    assert json.loads(result) == {"status": "error", "tool": "matrix_user", "action": "rooms", "message": message}
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_the_token_never_appears_in_a_result_or_an_error(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A refused connection says only that Matrix was unreachable, and no result or log line holds the token."""
    refused = aiohttp.ClientConnectorError(
        ConnectionKey("matrix.example.com", 443, True, True, None, None, None),
        ConnectionRefusedError(111, "Connection refused"),
    )
    caplog.set_level(logging.DEBUG)

    with _turn(config, paths), homeserver.serving_the_toolkit(), capture_logs() as logs:
        found = await _toolkit(config, paths).matrix_user_read(action="rooms")
        homeserver.failure = refused
        failed = await _toolkit(config, paths).matrix_user_read(action="rooms")

    assert json.loads(failed) == {
        "status": "error",
        "tool": "matrix_user",
        "action": "rooms",
        "message": "The Matrix homeserver could not be reached.",
    }
    assert str(refused) not in failed
    # The client logs the failure with its cause; that line too must not hold the token.
    assert [entry["error_type"] for entry in logs if entry["event"] == "matrix_request_transport_failed"] == [
        "ClientConnectorError",
    ]
    for text in (found, failed, caplog.text, *(str(entry) for entry in logs)):
        assert TOKEN not in text
    assert homeserver.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "timed_out",
    [TimeoutError(), aiohttp.ConnectionTimeoutError("Connection timeout to host https://matrix.example.com")],
    ids=["total", "connect"],
)
async def test_a_timeout_says_the_homeserver_took_too_long(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    timed_out: Exception,
) -> None:
    """A homeserver that does not answer in time is reported as slow, not as unreachable."""
    homeserver.failure = timed_out

    with _turn(config, paths), homeserver.serving_the_toolkit():
        result = await _toolkit(config, paths).matrix_user_read(action="rooms")

    assert json.loads(result) == {
        "status": "error",
        "tool": "matrix_user",
        "action": "rooms",
        "message": "The homeserver took too long to answer.",
    }
    assert homeserver.closed


@pytest.mark.asyncio
async def test_results_keep_non_ascii_text_as_is(config: Config, paths: RuntimePaths) -> None:
    """Names reach the model as written, not as escapes that cost several tokens each."""
    homeserver = FakeHomeserver({SYNC_PATH: sync_answer(join={ROOM: joined_room("Café ☕")})})

    with _turn(config, paths), homeserver.serving_the_toolkit():
        result = await _toolkit(config, paths).matrix_user_read(action="rooms")

    assert f"{ROOM} | Café ☕" in result
    assert json.loads(result)["rooms"] == [f"{ROOM} | Café ☕"]


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
        ({"action": "messages"}, "messages needs room_id, a room ID such as !abc:example.com."),
        ({"action": "search"}, "search needs query: the words to look for."),
        ({"action": "search", "query": " "}, "search needs query: the words to look for."),
        ({"action": "download", "room_id": ROOM}, "download needs event_id: the ID of the message with the file."),
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
        result = await _toolkit(config, paths).matrix_user_read(**arguments)

    assert json.loads(result) == {
        "status": "error",
        "tool": "matrix_user",
        "action": arguments["action"],
        "message": message,
    }
    assert homeserver.requests == []


def test_the_function_takes_no_user_to_act_as(config: Config, paths: RuntimePaths) -> None:
    """The model can name a room, an event, a thread, a query, a page and a limit, never a person to act as."""
    function = toolkit_offers(_toolkit(config, paths), "matrix_user_read")

    assert list(function.parameters["properties"]) == [
        "action",
        "room_id",
        "event_id",
        "thread_id",
        "query",
        "page_token",
        "limit",
    ]


@pytest.mark.asyncio
async def test_a_downloaded_file_is_an_attachment_of_the_turn(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A file fetched from any of the owner's rooms is kept for the turn's conversation and offered to its tools."""
    files = "!files:example.com"
    content = {
        "msgtype": "m.file",
        "body": "notes.txt",
        "url": "mxc://example.com/abc",
        "info": {"mimetype": "text/plain"},
    }
    homeserver.answers.update(
        {
            f"/_matrix/client/v3/rooms/{files}/event/$file": room_event("$file", "m.room.message", content),
            "/_matrix/client/v1/media/download/example.com/abc": Answer(b"Remember the milk"),
        },
    )

    with _turn(config, paths), homeserver.serving_the_toolkit():
        result = await _toolkit(config, paths).matrix_user_read(action="download", room_id=files, event_id="$file")
        context = get_tool_runtime_context()

    fields = json.loads(result)
    assert fields == {
        "status": "ok",
        "tool": "matrix_user",
        "action": "download",
        "attachment_id": fields["attachment_id"],
        "name": "notes.txt",
        "mime_type": "text/plain",
        "size_bytes": 17,
    }
    assert context is not None
    assert context.runtime_attachment_ids == [fields["attachment_id"]]
    record = load_attachment(paths.storage_root, fields["attachment_id"])
    assert record is not None
    assert (record.room_id, record.thread_id) == (ROOM, context.resolved_thread_id)
    assert homeserver.closed


@pytest.mark.asyncio
async def test_download_without_a_turn_is_refused_before_any_request(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A call without a chat turn, such as the MCP gateway's, has no conversation to hand a file to, so fetches none."""
    owner = resolve_connection_user(ApiSnapshot(generation=1, runtime_paths=paths, runtime_config=config), OWNER)

    with homeserver.serving_the_toolkit():
        result = await gateway.invoke_tool(
            resolve_connection_agent(owner, AGENT),
            toolkit="matrix_user",
            function="matrix_user_read",
            arguments={"action": "download", "room_id": ROOM, "event_id": "$file"},
        )

    assert "error" not in result
    assert json.loads(result["result"]) == {
        "status": "error",
        "tool": "matrix_user",
        "action": "download",
        "message": "download needs a chat turn: the file is handed to the conversation it was asked in.",
    }
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_the_owners_mcp_gateway_call_reads_as_the_owner(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """The MCP gateway builds the toolkit for the agent its call runs as, and the owner's call reads as the owner.

    The build gives it the primary's paths, whose environment holds the token, and its config, which decides, as the
    call has no chat turn.
    """
    owner = resolve_connection_user(ApiSnapshot(generation=1, runtime_paths=paths, runtime_config=config), OWNER)

    with homeserver.serving_the_toolkit():
        result = await gateway.invoke_tool(
            resolve_connection_agent(owner, AGENT),
            toolkit="matrix_user",
            function="matrix_user_read",
            arguments={"action": "rooms"},
        )

    assert "error" not in result
    assert json.loads(result["result"])["rooms"] == [f"{ROOM} | Planning | unread 2"]
    [request] = homeserver.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.query["user_id"] == [OWNER]


@pytest.mark.asyncio
async def test_a_team_runs_its_members_toolkit_as_that_member(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A team builds its member's toolkit for the member and runs the member's calls as the member.

    The team's turn answers through another account; without the member's own name the call would be refused.
    """
    with _turn(config, paths, agent_name="team"), homeserver.serving_the_toolkit():
        members = resolve_exact_team_members(
            [AGENT],
            config=config,
            runtime_paths=paths,
            execution_identity=current_execution_identity(),
            refresh_scheduler=None,
            active_model_names={AGENT: "default"},
        )
        build = members.build(AGENT, "default")
        toolkit = next(toolkit for toolkit in build.spec.toolkits if toolkit.name == "matrix_user")
        read = next(function for function in toolkit.functions if function.name == "matrix_user_read")
        with member_tool_scope(AGENT, "default"):
            result = await read.entrypoint(action="rooms")

    assert json.loads(result)["rooms"] == [f"{ROOM} | Planning | unread 2"]
    [request] = homeserver.requests
    assert request.query["user_id"] == [OWNER]
