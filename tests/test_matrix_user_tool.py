"""The `matrix_user` toolkit: a personal agent uses Matrix as its owner, and only as its owner."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from structlog.testing import capture_logs

from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.connection_agents import AgentToolContext, resolve_connection_agent, resolve_connection_user
from mindroom.attachments.storage import load_attachment
from mindroom.mcp_gateway import tools as gateway
from mindroom.teams.members import resolve_exact_team_members
from mindroom.tool_system.agno_toolkits import adapt_toolkit
from mindroom.tool_system.runtime_context import (
    current_execution_identity,
    get_tool_runtime_context,
    member_tool_scope,
)
from mindroom.tool_system.tool_function import ToolGate
from tests.agent_tool_helpers import toolkit_offers
from tests.matrix_user_helpers import (
    AGENT,
    OWNER,
    ROOM,
    ROOM_NEEDED,
    SYNC_PATH,
    TOKEN,
    Answer,
    FakeHomeserver,
    error_result,
    joined_room,
    ok_result,
    owner_config,
    owner_runtime_paths,
    owner_toolkit,
    owner_turn,
    room_event,
    search_answer,
    sync_answer,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.custom_tools.matrix_user import MatrixUserTools

STRANGER = "@bob:example.com"
NOT_SET_UP = "matrix_user is not set up: the runtime has no MATRIX_USER_APPSERVICE_TOKEN."
TURN_NEEDED = "download needs a chat turn: the file is handed to the conversation it was asked in."
# An alias, and the room the homeserver's directory says it names.
ALIAS = "#general:example.com"
ALIASED_ROOM = "!general:example.com"
DIRECTORY_PATH = f"/_matrix/client/v3/directory/room/{ALIAS}"


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """The primary runtime, with the token in its environment."""
    return owner_runtime_paths(tmp_path, MATRIX_USER_APPSERVICE_TOKEN=TOKEN)


@pytest.fixture
def config(paths: RuntimePaths) -> Config:
    """The config with Alice's personal agent."""
    return owner_config(paths)


@pytest.fixture
def gateway_agent(config: Config, paths: RuntimePaths) -> AgentToolContext:
    """Alice's personal agent as the MCP gateway resolves it for her call."""
    owner = resolve_connection_user(ApiSnapshot(generation=1, runtime_paths=paths, runtime_config=config), OWNER)
    return resolve_connection_agent(owner, AGENT)


@pytest.fixture
def homeserver() -> FakeHomeserver:
    """The homeserver, listing one room for Alice."""
    return FakeHomeserver({SYNC_PATH: sync_answer(join={ROOM: joined_room("Planning", unread=2)})})


@pytest.mark.asyncio
async def test_the_owner_reads_their_rooms(config: Config, paths: RuntimePaths, homeserver: FakeHomeserver) -> None:
    """The owner's turn reads the owner's rooms, as the owner, with the service's token, on a client closed after."""
    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(action="rooms")

    assert json.loads(result) == ok_result("rooms", rooms=[f"{ROOM} | Planning | unread 2"], left_out=0)
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
    with owner_turn(config, paths, requester_id), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths, agent_name).matrix_user_read(action="rooms")

    assert json.loads(result) == error_result("rooms", message)
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
    paths = owner_runtime_paths(tmp_path, **env)
    config = owner_config(paths)

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(action="rooms")

    assert json.loads(result) == error_result("rooms", message)
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

    with owner_turn(config, paths), homeserver.serving_the_toolkit(), capture_logs() as logs:
        found = await owner_toolkit(config, paths).matrix_user_read(action="rooms")
        homeserver.answers[SYNC_PATH] = refused
        failed = await owner_toolkit(config, paths).matrix_user_read(action="rooms")

    assert json.loads(failed) == error_result("rooms", "The Matrix homeserver could not be reached.")
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
    homeserver.answers[SYNC_PATH] = timed_out

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(action="rooms")

    assert json.loads(result) == error_result("rooms", "The homeserver took too long to answer.")
    assert homeserver.closed


@pytest.mark.asyncio
async def test_results_keep_non_ascii_text_as_is(config: Config, paths: RuntimePaths) -> None:
    """Names reach the model as written, not as escapes that cost several tokens each."""
    homeserver = FakeHomeserver({SYNC_PATH: sync_answer(join={ROOM: joined_room("Café ☕")})})

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(action="rooms")

    assert f"{ROOM} | Café ☕" in result
    assert json.loads(result)["rooms"] == [f"{ROOM} | Café ☕"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "room"}, f"room {ROOM_NEEDED}"),
        ({"action": "room", "room_id": " "}, f"room {ROOM_NEEDED}"),
        ({"action": "people"}, "people needs query: a name, or a full Matrix ID such as @alice:example.com."),
        ({"action": "messages"}, f"messages {ROOM_NEEDED}"),
        ({"action": "search"}, "search needs query: the words to look for."),
        ({"action": "search", "query": " "}, "search needs query: the words to look for."),
        ({"action": "download", "room_id": ROOM}, "download needs event_id: the ID of the message with the file."),
        ({"action": "download"}, f"download {ROOM_NEEDED}"),
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
    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(**arguments)

    assert json.loads(result) == error_result(arguments["action"], message)
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_a_missing_argument_is_named_before_the_caller_or_the_token_is_checked(
    tmp_path: Path,
    homeserver: FakeHomeserver,
) -> None:
    """A call that lacks an argument says so first, whoever makes it and whether or not the runtime has a token."""
    paths = owner_runtime_paths(tmp_path)
    config = owner_config(paths)

    with owner_turn(config, paths, STRANGER), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(action="room")

    assert json.loads(result) == error_result("room", f"room {ROOM_NEEDED}")
    assert homeserver.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "blank"),
    [
        ({"action": "messages", "room_id": ROOM}, {"thread_id": ""}),
        ({"action": "messages", "room_id": ROOM}, {"event_id": " "}),
        ({"action": "messages", "room_id": ROOM}, {"page_token": " "}),
        ({"action": "search", "query": "budget"}, {"room_id": ""}),
    ],
    ids=["thread_id", "event_id", "page_token", "room_id"],
)
async def test_a_blank_read_argument_is_left_out(
    config: Config,
    paths: RuntimePaths,
    arguments: dict[str, str],
    blank: dict[str, str],
) -> None:
    """A blank argument, as a model may send for one it means to leave out, asks the homeserver what leaving it out does."""
    homeserver = FakeHomeserver(
        {
            f"/_matrix/client/v3/rooms/{ROOM}/messages": {"chunk": []},
            "/_matrix/client/v3/search": search_answer(),
        },
    )
    toolkit = owner_toolkit(config, paths)

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        without = await toolkit.matrix_user_read(**arguments)
        with_blank = await toolkit.matrix_user_read(**arguments, **blank)

    assert json.loads(without)["status"] == "ok"
    assert with_blank == without
    first, second = homeserver.requests
    assert (second.method, second.url, second.body) == (first.method, first.url, first.body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "first_request"),
    [("messages", "messages"), ("room", "joined_members"), ("mark_read", "messages")],
)
async def test_a_room_named_by_alias_is_resolved_for_reads(
    config: Config,
    paths: RuntimePaths,
    action: str,
    first_request: str,
) -> None:
    """The owner's client looks the alias up, and the read then asks about the room it names."""
    room_path = f"/_matrix/client/v3/rooms/{ALIASED_ROOM}"
    homeserver = FakeHomeserver(
        {
            DIRECTORY_PATH: {"room_id": ALIASED_ROOM, "servers": ["example.com"]},
            f"{room_path}/messages": {"chunk": [room_event("$newest", "m.room.message", {"body": "Hi"})]},
            f"{room_path}/joined_members": {"joined": {}},
            f"{room_path}/read_markers": {},
            f"/_matrix/client/v3/user/{OWNER}/rooms/{ALIASED_ROOM}/account_data/m.marked_unread": {},
        },
    )

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(action=action, room_id=ALIAS)

    assert json.loads(result)["status"] == "ok"
    lookup, first, *_ = homeserver.requests
    assert (lookup.method, lookup.path, lookup.query["user_id"]) == ("GET", DIRECTORY_PATH, [OWNER])
    assert first.path == f"{room_path}/{first_request}"
    assert all(ALIAS not in request.path for request in homeserver.requests[1:])


@pytest.mark.asyncio
async def test_a_search_in_a_room_named_by_alias_searches_the_room_it_names(
    config: Config,
    paths: RuntimePaths,
) -> None:
    """The owner's client looks the alias up first, and the search is then of the room it names."""
    search_path = "/_matrix/client/v3/search"
    homeserver = FakeHomeserver(
        {
            DIRECTORY_PATH: {"room_id": ALIASED_ROOM, "servers": ["example.com"]},
            search_path: search_answer(),
        },
    )

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(action="search", query="budget", room_id=ALIAS)

    assert json.loads(result)["status"] == "ok"
    lookup, search = homeserver.requests
    assert (lookup.method, lookup.path) == ("GET", DIRECTORY_PATH)
    assert (search.method, search.path) == ("POST", search_path)
    assert search.body["search_categories"]["room_events"]["filter"]["rooms"] == [ALIASED_ROOM]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "requested"),
    [
        ({"action": "rooms"}, [SYNC_PATH]),
        ({"action": "people", "query": STRANGER}, [f"/_matrix/client/v3/profile/{STRANGER}"]),
    ],
    ids=["rooms", "people"],
)
async def test_an_action_that_takes_no_room_looks_none_up(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
    requested: list[str],
) -> None:
    """`rooms` and `people` take no room, so a `room_id` sent with one, even one of no room, is not looked up."""
    homeserver.answers[f"/_matrix/client/v3/profile/{STRANGER}"] = {"displayname": "Bob"}

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(**arguments, room_id="#gone:example.com")

    assert json.loads(result)["status"] == "ok"
    assert homeserver.paths() == requested


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda toolkit, room: toolkit.matrix_user_read(action="messages", room_id=room), id="read"),
        pytest.param(
            lambda toolkit, room: toolkit.matrix_user_write(action="send", room_id=room, message="Hi"),
            id="write",
        ),
    ],
)
@pytest.mark.parametrize(
    ("room_id", "message", "requested"),
    [
        ("planning", "No room is known as planning.", []),
        (
            "#gone:example.com",
            "Matrix answered M_NOT_FOUND: Not found",
            ["/_matrix/client/v3/directory/room/#gone:example.com"],
        ),
    ],
    ids=["no-such-name", "unknown-alias"],
)
async def test_an_unknown_room_reference_is_refused_before_anything_else(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    call: Callable[[MatrixUserTools, str], Awaitable[str]],
    room_id: str,
    message: str,
    requested: list[str],
) -> None:
    """A name MindRoom does not know, or an alias the homeserver cannot resolve, stops the call before its action."""
    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = json.loads(await call(owner_toolkit(config, paths), room_id))

    assert (result["status"], result["message"]) == ("error", message)
    assert homeserver.paths() == requested


def test_the_read_function_takes_no_user_to_act_as(config: Config, paths: RuntimePaths) -> None:
    """The model can name a room, an event, a thread, a query, a page and a limit, never a person to act as."""
    function = toolkit_offers(owner_toolkit(config, paths), "matrix_user_read")

    assert list(function.parameters["properties"]) == [
        "action",
        "room_id",
        "event_id",
        "thread_id",
        "query",
        "page_token",
        "limit",
    ]


def test_the_write_function_asks_for_confirmation_and_the_read_function_does_not(
    config: Config,
    paths: RuntimePaths,
) -> None:
    """Each write as the owner shows the owner a card first, whatever the deployment's approval rules; reads do not."""
    functions = adapt_toolkit(owner_toolkit(config, paths)).functions

    assert {function.name: function.gate for function in functions} == {
        "matrix_user_read": None,
        "matrix_user_write": ToolGate.CONFIRM,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "send", "message": "Hi"},
        {"action": "invite", "room_id": ROOM},
        {"action": "create_room", "name": "Trip"},
    ],
    ids=["send", "invite", "create_room"],
)
async def test_the_write_function_takes_no_user_to_act_as(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
) -> None:
    """``user_id`` is only whom to message or invite: naming the owner, as if to act as them, is an error."""
    function = toolkit_offers(owner_toolkit(config, paths), "matrix_user_write")

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_write(user_id=OWNER, **arguments)

    assert list(function.parameters["properties"]) == [
        "action",
        "room_id",
        "user_id",
        "message",
        "event_id",
        "thread_id",
        "emoji",
        "attachment",
        "name",
    ]
    assert json.loads(result) == error_result(
        arguments["action"],
        "user_id is whom to message or invite, never your owner.",
    )
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_the_gateway_offers_the_read_function_only(
    gateway_agent: AgentToolContext,
    homeserver: FakeHomeserver,
) -> None:
    """A write asks the owner on a card, which an MCP client cannot show, so the gateway neither lists nor runs it."""
    with homeserver.serving_the_toolkit():
        found = await gateway.search_tools(gateway_agent, toolkit="matrix_user")
        refused = await gateway.invoke_tool(
            gateway_agent,
            toolkit="matrix_user",
            function="matrix_user_write",
            arguments={"action": "leave", "room_id": ROOM},
        )

    assert "error" not in found
    assert [result["function"] for result in found["results"]] == ["matrix_user_read"]
    assert refused == {
        "error": {
            "code": "approval_required",
            "message": "This tool requires approval and cannot run through this gateway.",
        },
    }
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_a_write_without_a_chat_turn_is_refused(
    config: Config,
    paths: RuntimePaths,
    homeserver: FakeHomeserver,
) -> None:
    """A write belongs to a conversation with the owner, which a call without a chat turn does not have."""
    with homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_write(action="leave", room_id=ROOM)

    assert json.loads(result) == error_result("leave", "matrix_user_write works only in a chat with the agent.")
    assert homeserver.requests == []


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

    with owner_turn(config, paths), homeserver.serving_the_toolkit():
        result = await owner_toolkit(config, paths).matrix_user_read(action="download", room_id=files, event_id="$file")
        context = get_tool_runtime_context()

    fields = json.loads(result)
    assert fields == ok_result(
        "download",
        attachment_id=fields["attachment_id"],
        name="notes.txt",
        mime_type="text/plain",
        size_bytes=17,
    )
    assert context is not None
    assert context.runtime_attachment_ids == [fields["attachment_id"]]
    record = load_attachment(paths.storage_root, fields["attachment_id"])
    assert record is not None
    assert (record.room_id, record.thread_id) == (ROOM, context.resolved_thread_id)
    assert homeserver.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"room_id": ROOM, "event_id": "$file"}, TURN_NEEDED),
        ({"room_id": ROOM}, TURN_NEEDED),
        ({"event_id": "$file"}, f"download {ROOM_NEEDED}"),
    ],
    ids=["nothing-missing", "no-event", "no-room"],
)
async def test_download_without_a_turn_is_refused_before_any_request(
    gateway_agent: AgentToolContext,
    homeserver: FakeHomeserver,
    arguments: dict[str, str],
    message: str,
) -> None:
    """A call without a chat turn, such as the MCP gateway's, has no conversation to hand a file to, so fetches none.

    It says so before it names a missing event, and only after a missing room.
    """
    with homeserver.serving_the_toolkit():
        result = await gateway.invoke_tool(
            gateway_agent,
            toolkit="matrix_user",
            function="matrix_user_read",
            arguments={"action": "download", **arguments},
        )

    assert "error" not in result
    assert json.loads(result["result"]) == error_result("download", message)
    assert homeserver.requests == []


@pytest.mark.asyncio
async def test_the_owners_mcp_gateway_call_reads_as_the_owner(
    gateway_agent: AgentToolContext,
    homeserver: FakeHomeserver,
) -> None:
    """The MCP gateway builds the toolkit for the agent its call runs as, and the owner's call reads as the owner.

    The build gives it the primary's paths, whose environment holds the token, and its config, which decides, as the
    call has no chat turn.
    """
    with homeserver.serving_the_toolkit():
        result = await gateway.invoke_tool(
            gateway_agent,
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
    with owner_turn(config, paths, agent_name="team"), homeserver.serving_the_toolkit():
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
