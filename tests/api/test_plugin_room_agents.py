"""Plugin routes learn which of a room's agents their caller may use there, and the workspace each gives them.

They ask the worker Computer's question through the same check, and each workspace's root is the one a turn of that
agent for that caller resolves.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio
import pytest
from fastapi import FastAPI, HTTPException, Request
from structlog.testing import capture_logs

from mindroom.api import config_lifecycle
from mindroom.api.app_state import ensure_app_state
from mindroom.api.client_sessions import client_session_store
from mindroom.api.main import bind_embedded_runtime
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME, tracking_dir
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.orchestration.plugin_http_runtime import bind_room_members
from mindroom.plugin_http import AgentWorkspace, RoomReader, require_room_reader
from mindroom.room_agent_access import _WRITERS
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.worker_routing import build_tool_execution_identity
from tests.access_schema_support import joined_members_response
from tests.conftest import bind_runtime_paths, publish_test_plugins, test_runtime_paths
from tests.identity_helpers import entity_ids
from tests.runtime_fakes import ready_runtime
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


@dataclass
class _Router:
    """The running router, recording what each grant refresh's follow-up asks of it."""

    client: SimpleNamespace
    config: Config
    running: bool = True
    follow_ups: list[str] = field(default_factory=list)

    def schedule_pending_invite_reconciliation(self) -> None:
        self.follow_ups.append("invites")

    async def revoke_reply_authorized_calls(self) -> None:
        self.follow_ups.append("revoke_calls")

    def schedule_reply_authorized_call_reconciliation(self) -> None:
        self.follow_ups.append("reconcile_calls")

    def schedule_reply_authorized_call_revocation(self) -> None:
        self.follow_ups.append("scheduled_revoke")

    def release_pending_turn_journal_replay(self) -> None:
        pass

    async def recover_pending_turn_journal_events(self) -> None:
        self.follow_ups.append("turns")


class _Room:
    """A running router, Alice, Bob, Carol, and the ``joined`` agents in ``ROOM``, which the API serves ``config`` for.

    The runtime is the production one, so each access check refreshes grants through the runtime's own refresh.
    """

    def __init__(self, config: Config, paths: RuntimePaths, joined: tuple[str, ...]) -> None:
        self.paths = paths
        self.config = config
        self.ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(config, paths).items()}
        members = [ALICE, BOB, CAROL, self.ids[ROUTER_AGENT_NAME], *(self.ids[name] for name in joined)]
        self.client = SimpleNamespace(
            rooms={ROOM: nio.MatrixRoom(ROOM, self.ids[ROUTER_AGENT_NAME])},
            joined_members=AsyncMock(return_value=joined_members_response(ROOM, *members)),
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
        self.router = _Router(self.client, config)
        self.runtime = ready_runtime(config, paths)
        self.runtime.fleet.add(ROUTER_AGENT_NAME, self.router)  # type: ignore[arg-type]
        self.app = FastAPI()
        state = ensure_app_state(self.app)
        state.api_state = config_lifecycle.ApiState(
            threading.Lock(),
            config_lifecycle.ApiSnapshot(1, self.paths, runtime_config=self.config),
        )
        bind_embedded_runtime(self.app, self.runtime.embedded_runtime())
        # Another running bot in the room answers room reads while the router is down.
        bots = {ROUTER_AGENT_NAME: self.router, "other": SimpleNamespace(client=self.client, running=True)}
        bind_room_members(self.app, bots)  # type: ignore[arg-type]

    def serve(self, config: Config) -> None:
        """Serve ``config`` from the API, as a config save does."""
        state = config_lifecycle.require_api_state(self.app)
        state.snapshot = config_lifecycle.ApiSnapshot(state.snapshot.generation + 1, self.paths, runtime_config=config)

    def apply(self, config: Config) -> None:
        """Run ``config`` in the runtime, as a reload does once the API serves it."""
        self.runtime.config = self.router.config = config

    def refreshes(self) -> int:
        """How many grant refreshes the router's client has served."""
        return self.client.joined_rooms.await_count

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
async def test_a_bridge_alias_opens_the_workspace_its_person_s_turns_use(tmp_path: Path) -> None:
    """A caller signed in as a bridge alias of Alice gets the private root Alice's turns resolve, not one of its own."""
    alias = "@telegram_alice:localhost"
    config = Config(agents=_agents(), authorization={"aliases": {ALICE: [alias]}})
    room = _Room(config, test_runtime_paths(tmp_path), ("coder", "notes"))
    room.client.joined_members.return_value = joined_members_response(ROOM, alias, *room.ids.values())

    (coder, notes) = await room.agents(alias)

    assert (coder.name, notes.name, notes.root) == ("coder", "notes", _turn_root(room, "notes", ALICE))


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
    ("tools", "files"),
    [
        ([{"file": {"enable_save_file": False}}], "write"),
        ([{"file": {"all": True, **READ_ONLY_FILE["file"]}}], "write"),
        ([{"file": {"exclude_tools": ["save_file", "replace_file_chunk"]}}], "read"),
        ([{"file": {"enable_delete_file": True, **READ_ONLY_FILE["file"]}}], "read"),
        ([{"file": {"include_tools": ["delete_file"]}}], "none"),
        (["coding"], "write"),
        ([{"coding": {"include_tools": ["read_file", "grep"]}}], "read"),
        (["python"], "write"),
        ([{"python": {"include_tools": ["read_file", "list_files"]}}], "read"),
        (["script"], "write"),
        ([{"shell": {"enable_run_shell_command": False}}], "none"),
        (["matrix_message"], "read"),
        (["calculator"], "none"),
    ],
)
@pytest.mark.asyncio
async def test_files_follow_what_each_toolkit_offers_by_its_own_settings(
    tmp_path: Path,
    tools: list[object],
    files: str,
) -> None:
    """Toolkits that take local paths give read; those that edit files or run code give write."""
    room = _room(tmp_path, {"helper": AgentConfig(display_name="Helper", tools=tools, access={"users": [ALICE]})})

    assert _access(await room.agents(ALICE)) == [("helper", files, "none")]


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

    for name, functions in _WRITERS.items():
        assert functions <= set(tools.entries[name].function_names), name


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
async def test_pending_membership_policy_answers_503_until_matrix_confirms_it(tmp_path: Path) -> None:
    """While the router's rooms are unknown, a check that waits on a room grant answers 503, never a guess.

    A caller the agent's own rules admit is still listed.
    """
    room = _room(
        tmp_path,
        {
            "helper": AgentConfig(
                display_name="Helper",
                tools=["file"],
                access={"current_room_members": True, "users": [ALICE]},
            ),
        },
    )
    room.client.joined_rooms.return_value = nio.JoinedRoomsError("unavailable")

    pending = (503, "Responder membership policy is unresolved.")
    assert await _status(room.agents(BOB)) == pending
    assert await _status(room.agent(BOB, room.ids["helper"])) == pending
    assert _access(await room.agents(ALICE)) == [("helper", "write", "none")]

    room.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[ROOM])
    assert _access(await room.agents(BOB)) == [("helper", "read", "none")]


@pytest.mark.asyncio
async def test_a_member_who_joined_after_the_last_refresh_is_admitted_after_one_refresh(tmp_path: Path) -> None:
    """The router applies joins from its own sync, which can trail the room's live roster, so one refresh catches up."""
    dave = "@dave:localhost"
    room = _room(
        tmp_path,
        {"helper": AgentConfig(display_name="Helper", tools=["file"], access={"current_room_members": True})},
    )
    assert [workspace.name for workspace in await room.agents(ALICE)] == ["helper"]
    assert room.refreshes() == 1

    room.client.joined_members.return_value = joined_members_response(ROOM, ALICE, BOB, CAROL, dave, *room.ids.values())
    assert [workspace.name for workspace in await room.agents(dave)] == ["helper"]
    assert room.refreshes() == 2

    for _ in range(2):
        assert (await room.agent(dave, room.ids["helper"])).name == "helper"
        assert [workspace.name for workspace in await room.agents(ALICE)] == ["helper"]
    assert room.refreshes() == 2


def _broken(room: _Room, kind: str) -> str:
    """Add an agent ``kind`` breaks, joined to the room unless it has no account, and return its user ID."""
    agents = _agents()
    if kind == "unprepared":
        agents["broken"] = AgentConfig(display_name="Broken", tools=["file"], access={"users": [ALICE]})
        config = Config(agents=agents)
        room.serve(config)
        room.apply(config)
        return "@mindroom_broken:localhost"
    if kind == "pending":
        # A grant room MindRoom has not created stays unresolved.
        agents["broken"] = AgentConfig(display_name="Broken", tools=["file"], access={"members_of_rooms": ["lobby"]})
    else:
        agents["broken"] = AgentConfig(display_name="Broken", tools=["coding"], access={"users": [ALICE]})
        get_runtime_credentials_manager(room.paths).save_credentials("coding", {"exclude_tools": 5})
    config = Config(agents=agents, rooms={"lobby": {}})
    ids = entity_ids(config, room.paths)
    room.ids["broken"] = ids["broken"].full_id
    members = [ALICE, BOB, CAROL, *(matrix_id.full_id for matrix_id in ids.values())]
    room.client.joined_members.return_value = joined_members_response(ROOM, *members)
    room.serve(config)
    room.apply(config)
    return room.ids["broken"]


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        ("unprepared", (503, "Agent identities are not ready.")),
        ("pending", (503, "Responder membership policy is unresolved.")),
        ("tool_setting", TypeError),
    ],
)
@pytest.mark.asyncio
async def test_one_agent_that_cannot_be_resolved_is_left_out_and_the_rest_are_listed(
    tmp_path: Path,
    kind: str,
    error: tuple[int, str] | type[Exception],
) -> None:
    """An agent without its account, with an unresolved grant room, or with a corrupt stored setting fails only itself."""
    room = _room(tmp_path, _agents())
    broken = _broken(room, kind)

    with capture_logs() as logs:
        listed = await room.agents(ALICE)

    assert _access(listed) == [("coder", "write", "type"), ("notes", "read", "none")]
    assert [log["agent_name"] for log in logs if log["event"] == "room_agent_left_out"] == ["broken"]
    if isinstance(error, tuple):
        assert await _status(room.agent(ALICE, broken)) == error
    else:
        with pytest.raises(error):
            await room.agent(ALICE, broken)


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

    room.apply(saved)
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


@pytest.mark.asyncio
async def test_a_corrupt_contacts_store_leaves_out_only_the_personal_agent(tmp_path: Path) -> None:
    """Only a personal agent reads the contacts store, so a corrupt one leaves out that agent and lists the rest."""
    data = _with_template()
    data["personal_agents"]["mind"]["agent"] |= {"contacts": {}, "tools": ["file"]}
    data["agents"]["creator"]["access"] = {"users": [ALICE]}
    room = _Room(
        _load(tmp_path, data, entries=[_personal(ALICE)]),
        test_runtime_paths(tmp_path),
        ("mind_alice", "creator"),
    )
    tracking_dir(room.paths).mkdir(parents=True, exist_ok=True)
    (tracking_dir(room.paths) / "contacts.json").write_text("{not json", encoding="utf-8")

    assert [workspace.name for workspace in await room.agents(ALICE)] == ["creator"]
    with pytest.raises(ValueError, match="Contacts file"):
        await room.agent(ALICE, room.ids["mind_alice"])


@pytest.mark.asyncio
async def test_requests_refresh_grants_only_when_they_need_it_and_share_one_refresh(tmp_path: Path) -> None:
    """With the router in many rooms, a burst of requests makes one refresh, and once grants are current none makes one.

    Each request still reads the room's own members once.
    """
    room = _room(tmp_path, _agents())
    others = [f"!other{index}:localhost" for index in range(60)]
    room.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[ROOM, *others])

    def grant_room_reads() -> int:
        return sum(call.args[0] != ROOM for call in room.client.joined_members.await_args_list)

    burst = await asyncio.gather(*(room.agents(ALICE) for _ in range(5)))

    assert [_access(listed) for listed in burst] == [[("coder", "write", "type"), ("notes", "read", "none")]] * 5
    assert (room.refreshes(), grant_room_reads()) == (1, len(others))
    for _ in range(3):
        before = room.client.joined_members.await_count
        assert len(await room.agents(BOB)) == 2
        assert (await room.agent(CAROL, room.ids["notes"])).name == "notes"
        assert room.client.joined_members.await_count - before == 2
    assert (room.refreshes(), grant_room_reads()) == (1, len(others))


@pytest.mark.asyncio
async def test_a_member_who_joined_while_a_refresh_read_the_room_is_admitted(tmp_path: Path) -> None:
    """A refresh already reading the room may predate a join, so grants that still lack the joined caller refresh again."""
    dave = "@dave:localhost"
    room = _room(
        tmp_path,
        {"helper": AgentConfig(display_name="Helper", tools=["file"], access={"current_room_members": True})},
    )
    assert [workspace.name for workspace in await room.agents(ALICE)] == ["helper"]
    before = room.client.joined_members.return_value
    after = joined_members_response(ROOM, ALICE, BOB, CAROL, dave, *room.ids.values())
    reading = asyncio.Event()
    looked_up = asyncio.Event()
    release = asyncio.Event()

    async def joined_members(_room_id: str) -> nio.JoinedMembersResponse:
        if not reading.is_set():
            # The router's refresh reads the room before Dave joins, and answers once he has.
            reading.set()
            await release.wait()
            return before
        looked_up.set()
        return after

    room.runtime.agent_reply_membership_sync.invalidate(room.runtime.config, reason="test")
    room.client.joined_members.side_effect = joined_members
    frame = asyncio.create_task(
        room.runtime.agent_reply_membership_sync.refresh_if_needed(
            room.runtime.config,
            room.runtime.refresh_agent_reply_memberships,
        ),
    )
    await reading.wait()
    listing = asyncio.create_task(room.agents(dave))
    await looked_up.wait()
    # Dave's check, which reads the room's roster itself, then waits on the refresh in flight.
    for _ in range(20):
        await asyncio.sleep(0)
    release.set()

    assert [workspace.name for workspace in await listing] == ["helper"]
    await frame
    assert room.refreshes() == 3


@pytest.mark.asyncio
async def test_a_room_the_grants_do_not_track_never_refreshes_them(tmp_path: Path) -> None:
    """Grants hold no view of a room outside the router's rooms, so its callers refresh only when grants need it."""
    room = _room(tmp_path, _agents())
    room.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=["!other:localhost"])

    for _ in range(3):
        assert len(await room.agents(ALICE)) == 2

    assert room.refreshes() == 1


@pytest.mark.asyncio
async def test_a_refresh_a_request_starts_while_the_router_backs_off_runs_the_runtime_follow_up_once(
    tmp_path: Path,
) -> None:
    """A request refreshes grants the router's backoff left stale, and turns, calls, and invites resume once."""
    room = _room(tmp_path, _agents())
    membership_sync = room.runtime.agent_reply_membership_sync

    async def router_frame() -> None:
        await membership_sync.refresh_if_needed(room.runtime.config, room.runtime.refresh_agent_reply_memberships)
        assert await wait_for_background_tasks(5, owner=room.runtime.turn_recovery.task_owner)

    room.client.joined_rooms.return_value = nio.JoinedRoomsError("unavailable")
    await router_frame()
    assert room.runtime.agent_reply_memberships.needs_refresh(room.config)
    room.client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[ROOM])
    await router_frame()
    assert room.refreshes() == 1, "the router backs off"
    room.router.follow_ups.clear()

    assert len(await room.agents(ALICE)) == 2
    await router_frame()

    assert room.refreshes() == 2
    assert not room.runtime.agent_reply_memberships.needs_refresh(room.config)
    assert sorted(room.router.follow_ups) == ["invites", "reconcile_calls", "revoke_calls", "turns"]
