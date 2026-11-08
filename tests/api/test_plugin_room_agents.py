"""Plugin routes learn which of a room's agents their caller may use there, and the workspace each gives them.

They ask the worker Computer's question through the same check, and each workspace's root is the one a turn of that
agent for that caller resolves.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
from fastapi import FastAPI, HTTPException, Request

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api import config_lifecycle
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.orchestration.plugin_http_runtime import bind_room_members
from mindroom.plugin_http import AgentWorkspace, RoomReader, require_room_reader
from mindroom.room_agent_access import _WRITERS
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.conftest import bind_runtime_paths, publish_test_plugins, test_runtime_paths
from tests.identity_helpers import entity_ids
from tests.runtime_fakes import bind_test_embedded_runtime
from tests.test_generated_agents import _load, _personal, _with_template

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from pathlib import Path

    from mindroom.constants import RuntimePaths

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

ROOM = "!room:localhost"
ALICE = "@alice:localhost"
BOB = "@bob:localhost"
CAROL = "@carol:localhost"
# Bob's power level keeps him from posting; Alice and Carol may post.
POWER_LEVELS = {"users": {ALICE: 100, CAROL: 10}, "events_default": 10}
READ_ONLY_FILE = {"file": {"enable_save_file": False, "enable_replace_file_chunk": False}}


def _agents() -> dict[str, AgentConfig]:
    """A shared agent with files and a shell that Carol may not use, and a private one whose files only read."""
    return {
        "coder": AgentConfig(
            display_name="Coder",
            tools=["file", "shell"],
            access=ResponderAccessConfig(users=[ALICE, BOB]),
        ),
        "notes": AgentConfig(
            display_name="Notes",
            tools=[READ_ONLY_FILE],
            private=AgentPrivateConfig(per="user"),
            access=ResponderAccessConfig(users=[ALICE, BOB, CAROL]),
        ),
    }


class _Room:
    """A running router, Alice, Bob, Carol, and the ``joined`` agents in ``ROOM``, which the API serves ``config`` for."""

    def __init__(self, config: Config, paths: RuntimePaths, joined: tuple[str, ...]) -> None:
        self.paths = paths
        self.config = config
        self.ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(config, paths).items()}
        members = [ALICE, BOB, CAROL, self.ids[ROUTER_AGENT_NAME], *(self.ids[name] for name in joined)]
        self.client = SimpleNamespace(
            user_id=self.ids[ROUTER_AGENT_NAME],
            rooms={ROOM: nio.MatrixRoom(ROOM, self.ids[ROUTER_AGENT_NAME])},
            joined_members=AsyncMock(
                return_value=nio.JoinedMembersResponse(
                    members=[nio.RoomMember(user_id, None, None) for user_id in members],
                    room_id=ROOM,
                ),
            ),
            joined_rooms=AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[ROOM])),
            room_get_state=AsyncMock(
                return_value=nio.RoomGetStateResponse(
                    [
                        {"type": "m.room.create", "state_key": "", "sender": ALICE, "content": {"room_version": "10"}},
                        {"type": "m.room.power_levels", "state_key": "", "sender": ALICE, "content": POWER_LEVELS},
                    ],
                    ROOM,
                ),
            ),
        )
        self.router = SimpleNamespace(client=self.client, running=True, config=self.config)
        self.memberships = AgentReplyMembershipIndex()
        self.app = FastAPI()
        state = ensure_app_state(self.app)
        state.api_state = config_lifecycle.ApiState(
            threading.Lock(),
            config_lifecycle.ApiSnapshot(1, self.paths, runtime_config=self.config),
        )
        bind_test_embedded_runtime(
            self.app,
            agent_reply_memberships=self.memberships,
            agent_bot={ROUTER_AGENT_NAME: self.router}.get,
        )
        # Another running bot in the room answers room reads while the router is down.
        bots = {ROUTER_AGENT_NAME: self.router, "other": SimpleNamespace(client=self.client, running=True)}
        bind_room_members(self.app, bots)  # type: ignore[arg-type]

    def serve(self, config: Config) -> None:
        """Serve ``config`` from the API, as a config save does."""
        state = config_lifecycle.require_api_state(self.app)
        state.snapshot = config_lifecycle.ApiSnapshot(state.snapshot.generation + 1, self.paths, runtime_config=config)

    async def agents(self, user_id: str) -> list[AgentWorkspace]:
        return await (await self._reader(user_id)).agents()

    async def agent(self, user_id: str, agent_user_id: str) -> AgentWorkspace:
        return await (await self._reader(user_id)).agent(agent_user_id)

    async def _reader(self, user_id: str) -> RoomReader:
        token = client_session_store(self.app).create(user_id).token
        request = Request(
            {"type": "http", "app": self.app, "headers": [(b"authorization", f"Bearer {token}".encode())]},
        )
        return await require_room_reader(request, ROOM)


def _room(tmp_path: Path, agents: dict[str, AgentConfig], *, joined: tuple[str, ...] | None = None) -> _Room:
    """A room with ``agents``, all of them joined unless ``joined`` names some."""
    return _Room(Config(agents=agents), test_runtime_paths(tmp_path), joined or tuple(agents))


async def _status(read: Awaitable[object]) -> tuple[int, object]:
    """The status and detail a read answers: 200 and None when it returns, else its HTTP error's."""
    try:
        async with asyncio.timeout(5):
            await read
    except HTTPException as error:
        return error.status_code, error.detail
    return 200, None


def _turn_root(room: _Room, agent_name: str, requester_id: str) -> Path:
    """The workspace a turn of ``agent_name`` for ``requester_id`` in a thread of ``ROOM`` resolves."""
    identity = build_tool_execution_identity(
        channel="matrix",
        agent_name=agent_name,
        runtime_paths=room.paths,
        requester_id=requester_id,
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session",
    )
    return resolve_agent_runtime(agent_name, room.config, room.paths, identity).workspace.root


def _access(workspaces: list[AgentWorkspace]) -> list[tuple[str, str, str]]:
    return [(workspace.name, workspace.files, workspace.terminal) for workspace in workspaces]


@pytest.mark.asyncio
async def test_agents_lists_those_the_caller_may_use_in_config_order_with_what_they_may_do(tmp_path: Path) -> None:
    """Writing and typing need power to post; files that only read never write; the roots are the turns' own."""
    room = _room(tmp_path, _agents())

    alice = await room.agents(ALICE)
    bob = await room.agents(BOB)
    carol = await room.agents(CAROL)

    assert _access(alice) == [("coder", "write", "type"), ("notes", "read", "none")]
    assert _access(bob) == [("coder", "read", "watch"), ("notes", "read", "none")]
    assert _access(carol) == [("notes", "read", "none")]
    coder = alice[0]
    assert (coder.user_id, coder.display_name, coder.private) == (room.ids["coder"], "Coder", False)
    assert coder.config is room.config
    assert coder.root == bob[0].root == _turn_root(room, "coder", ALICE)
    assert not coder.root.exists()
    assert (alice[1].private, alice[1].root, bob[1].root, carol[0].root) == (
        True,
        _turn_root(room, "notes", ALICE),
        _turn_root(room, "notes", BOB),
        _turn_root(room, "notes", CAROL),
    )
    assert len({alice[1].root, bob[1].root, carol[0].root}) == 3


@pytest.mark.asyncio
async def test_an_agent_not_joined_or_refusing_the_caller_is_left_out(tmp_path: Path) -> None:
    """Only joined agents whose access rules admit the caller are listed; the others answer 403 by name."""
    room = _room(tmp_path, _agents(), joined=("coder",))

    assert _access(await room.agents(CAROL)) == []
    assert await _status(room.agent(CAROL, room.ids["coder"])) == (403, "Requester cannot use this agent.")
    assert await _status(room.agent(ALICE, room.ids["notes"])) == (
        403,
        "Requester and agent must both be joined to this room.",
    )
    assert (await room.agent(ALICE, room.ids["coder"])).files == "write"


@pytest.mark.asyncio
async def test_agent_refuses_a_user_that_is_no_current_agent(tmp_path: Path) -> None:
    """The router, a team, a person, and an unknown user are no agent anyone opens."""
    team = TeamConfig(display_name="Crew", role="Coordinate", agents=["coder"], mode="coordinate")
    room = _Room(Config(agents=_agents(), teams={"crew": team}), test_runtime_paths(tmp_path), ("coder", "crew"))

    for user_id in (room.ids[ROUTER_AGENT_NAME], room.ids["crew"], BOB, "@ghost:localhost"):
        assert await _status(room.agent(ALICE, user_id)) == (403, "The selected user is not a current agent.")


@pytest.mark.parametrize(
    ("tools", "files", "terminal"),
    [
        ([{"file": {"enable_save_file": False}}], "write", "none"),
        ([{"file": {"all": True, **READ_ONLY_FILE["file"]}}], "write", "none"),
        ([{"file": {"exclude_tools": ["save_file", "replace_file_chunk"]}}], "read", "none"),
        ([{"file": {"enable_delete_file": True, **READ_ONLY_FILE["file"]}}], "read", "none"),
        ([{"file": {"include_tools": ["delete_file"]}}], "none", "none"),
        (["coding"], "write", "none"),
        ([{"coding": {"include_tools": ["read_file", "grep"]}}], "read", "none"),
        (["python"], "write", "none"),
        ([{"python": {"include_tools": ["read_file", "list_files"]}}], "read", "none"),
        (["script"], "write", "none"),
        ([{"shell": {"enable_run_shell_command": False}}], "none", "none"),
        (["matrix_message"], "read", "none"),
        (["calculator"], "none", "none"),
    ],
)
@pytest.mark.asyncio
async def test_files_follow_what_each_toolkit_offers_by_its_own_settings(
    tmp_path: Path,
    tools: list[object],
    files: str,
    terminal: str,
) -> None:
    """Toolkits that take local paths give read; those that edit files or run code give write; a shell gives one."""
    room = _room(tmp_path, {"helper": AgentConfig(display_name="Helper", tools=tools, access={"users": [ALICE]})})

    assert _access(await room.agents(ALICE)) == [("helper", files, terminal)]


PLUGIN_TOOLS = """
from mindroom.plugin_api import ToolCategory, ToolFileAccess, Toolkit, register_tool_with_metadata, tool


class NotesTools(Toolkit):
    name = "notes"

    @tool
    def read_note(self, path: str) -> str:
        \"\"\"Read a note.

        Args:
            path: The note's path.
        \"\"\"
        return path


@register_tool_with_metadata(
    name="notes",
    file_access=ToolFileAccess.{file_access},
    executes_code={executes_code},
    display_name="Notes",
    description="Notes in the workspace",
    category=ToolCategory.PRODUCTIVITY,
)
def notes_tools() -> type[NotesTools]:
    return NotesTools
"""


@pytest.mark.parametrize(
    ("file_access", "executes_code", "tool", "files"),
    [
        ("AGENT", False, "notes", "read"),
        ("NONE", True, "notes", "write"),
        ("NONE", True, {"notes": {"include_tools": []}}, "none"),
        ("NONE", True, {"notes": {"include_tools": ["read_note"]}}, "write"),
    ],
)
@pytest.mark.asyncio
async def test_a_plugin_toolkit_that_names_no_functions_counts_by_its_declarations(
    tmp_path: Path,
    file_access: str,
    executes_code: bool,
    tool: object,
    files: str,
) -> None:
    """A plugin toolkit that declares no function names counts as offering what its build offers, unless filtered off."""
    plugin = tmp_path / "plugins" / "notes"
    plugin.mkdir(parents=True)
    (plugin / "mindroom.plugin.json").write_text('{"name": "notes", "tools_module": "tools.py"}')
    (plugin / "tools.py").write_text(PLUGIN_TOOLS.format(file_access=file_access, executes_code=executes_code))
    paths = test_runtime_paths(tmp_path)
    helper = AgentConfig(display_name="Helper", tools=[tool], access={"users": [ALICE]})
    config = bind_runtime_paths(Config(plugins=["./plugins/notes"], agents={"helper": helper}), paths)
    publish_test_plugins(config, paths)
    room = _Room(config, paths, ("helper",))

    assert _access(await room.agents(ALICE)) == [("helper", files, "none")]


def test_each_named_writer_is_a_function_its_toolkit_declares() -> None:
    """A renamed writing or code-running function would otherwise turn ``write`` into ``read`` without notice."""
    tools = builtin_tool_registry()

    assert {
        name: functions <= set(tools.entries[name].function_names) for name, functions in _WRITERS.items()
    } == dict.fromkeys(_WRITERS, True)


@pytest.mark.asyncio
async def test_a_setting_saved_on_the_dashboard_counts_like_an_authored_one(tmp_path: Path) -> None:
    """A stored filter, even one saved as legacy comma-separated text, takes writing functions away as the build does."""
    room = _room(tmp_path, {"helper": AgentConfig(display_name="Helper", tools=["file"], access={"users": [ALICE]})})
    assert _access(await room.agents(ALICE)) == [("helper", "write", "none")]

    get_runtime_credentials_manager(room.paths).save_credentials(
        "file",
        {"exclude_tools": "save_file, replace_file_chunk"},
    )

    assert _access(await room.agents(ALICE)) == [("helper", "read", "none")]


@pytest.mark.asyncio
async def test_pending_membership_policy_and_unprepared_identities_answer_503(tmp_path: Path) -> None:
    """A room-backed grant Matrix cannot confirm, or an agent without its account, answers 503, never a guess."""
    room = _room(
        tmp_path,
        {"helper": AgentConfig(display_name="Helper", tools=["file"], access={"current_room_members": True})},
    )
    room.client.joined_rooms.return_value = nio.JoinedRoomsError("unavailable")

    pending = (503, "Responder membership policy is unresolved.")
    assert await _status(room.agents(ALICE)) == pending
    assert await _status(room.agent(ALICE, room.ids["helper"])) == pending

    room.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[ROOM])
    assert _access(await room.agents(ALICE)) == [("helper", "write", "none")]

    unprepared = Config(agents={**room.config.agents, "newcomer": AgentConfig(display_name="Newcomer")})
    room.serve(unprepared)
    room.router.config = unprepared
    assert await _status(room.agents(ALICE)) == (503, "Agent identities are not ready.")


@pytest.mark.asyncio
async def test_a_changed_membership_policy_waits_for_the_runtime_without_touching_its_grants(tmp_path: Path) -> None:
    """While the runtime still runs another room-grant policy, the API's newer one answers 503 and refreshes nothing."""
    room = _room(tmp_path, _agents())
    saved = Config(
        agents={
            **room.config.agents,
            "coder": AgentConfig(display_name="Coder", tools=["file"], access={"current_room_members": True}),
        },
    )
    room.serve(saved)

    assert await _status(room.agents(ALICE)) == (503, "Agent access is unavailable; try again.")
    room.client.joined_rooms.assert_not_called()

    room.router.config = saved
    assert _access(await room.agents(CAROL)) == [("coder", "write", "none"), ("notes", "read", "none")]


@pytest.mark.asyncio
async def test_a_config_save_that_removes_access_shows_on_the_next_request(tmp_path: Path) -> None:
    """Each request answers under the config the API serves when it arrives, and its workspaces carry that config."""
    room = _room(tmp_path, _agents())
    assert [workspace.name for workspace in await room.agents(ALICE)] == ["coder", "notes"]

    agents = _agents()
    agents["notes"].access = ResponderAccessConfig(users=[BOB])
    saved = Config(agents=agents)
    room.serve(saved)

    (coder,) = await room.agents(ALICE)
    assert (coder.name, coder.config) == ("coder", saved)
    assert await _status(room.agent(ALICE, room.ids["notes"])) == (403, "Requester cannot use this agent.")


@pytest.mark.asyncio
async def test_a_room_without_a_running_router_answers_503(tmp_path: Path) -> None:
    """The access check refreshes grants through the router, so without it nobody is admitted or refused."""
    room = _room(tmp_path, _agents())
    room.router.running = False

    assert await _status(room.agents(ALICE)) == (503, "Agent access is unavailable; try again.")


@pytest.mark.asyncio
async def test_a_contact_room_opens_no_workspace(tmp_path: Path) -> None:
    """A contact room runs its agent's turns as the agent's owner, so nobody opens that agent's workspace through it."""
    data = _with_template()
    data["personal_agents"]["mind"]["agent"] |= {"contacts": {}, "tools": ["file"]}
    room = _Room(_load(tmp_path, data, entries=[_personal(ALICE)]), test_runtime_paths(tmp_path), ("mind_alice",))
    agent_id = room.ids["mind_alice"]
    assert _access(await room.agents(ALICE)) == [("mind_alice", "write", "type")]

    record = ContactRecord(
        id="c1",
        status="active",
        requester=ContactParty(kind="agent", id="mind_alice", owner=ALICE),
        target=ContactParty(kind="person", id=BOB, owner=BOB),
        reason="Plan the dinner",
        requester_side=ContactSide(),
        target_side=ContactSide(),
        origin_room_id="!origin:localhost",
        origin_thread_id=None,
        room_id=ROOM,
    )
    update_contacts(room.paths, lambda book: book.contacts.append(record))

    assert await room.agents(ALICE) == []
    assert await _status(room.agent(ALICE, agent_id)) == (403, "No workspace opens through a contact room.")
