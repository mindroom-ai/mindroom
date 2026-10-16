"""Tests for the tool that lets an agent share a directory with other agents."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.shared_directories import SharedDirectoryTools
from mindroom.message_target import MessageTarget
from mindroom.shared_directories.paths import agent_shared_directory_views
from mindroom.shared_directories.store import create_shared_directory, load_shared_directories
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

_ROOM = "!room:localhost"
_USER = "@user:localhost"


_DOCKER_WORKERS = {"MINDROOM_WORKER_BACKEND": "docker", "MINDROOM_DOCKER_WORKER_IMAGE": "worker:test"}


def _config(tmp_path: Path, *, dedicated_workers: bool = False, **bob_overrides: object) -> Config:
    allowed = ResponderAccessConfig(users=[_USER])
    runtime_paths = test_runtime_paths(tmp_path)
    if dedicated_workers:
        runtime_paths = resolve_runtime_paths(
            config_path=runtime_paths.config_path,
            storage_path=runtime_paths.storage_root,
            process_env={**runtime_paths.process_env, **_DOCKER_WORKERS},
        )
    return bind_runtime_paths(
        Config(
            agents={
                "alice": AgentConfig(display_name="Alice", access=allowed),
                "bob": AgentConfig(**{"display_name": "Bob", "access": allowed, **bob_overrides}),
                "carol": AgentConfig(display_name="Carol", access=allowed, private=AgentPrivateConfig(per="user")),
            },
            teams={"crew": TeamConfig(display_name="Crew", role="Crew", agents=["alice", "bob"])},
        ),
        runtime_paths,
    )


def _context(config: Config, agent_name: str = "alice") -> ToolRuntimeContext:
    runtime_paths = runtime_paths_for(config)
    persist_entity_accounts(config, runtime_paths)
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = entity_ids(config, runtime_paths)[agent_name].full_id
    return make_test_tool_runtime_context(
        agent_name=agent_name,
        target=MessageTarget.resolve(room_id=_ROOM, thread_id=None, reply_to_event_id=None),
        requester_id=_USER,
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
    )


def _call(context: ToolRuntimeContext, function: str, *args: str, acting_agent: str | None = None) -> dict[str, object]:
    tools = SharedDirectoryTools(agent_name=acting_agent or context.agent_name)
    with tool_runtime_context(context):
        return json.loads(getattr(tools, function)(*args))


def test_owner_creates_invites_and_lists(tmp_path: Path) -> None:
    """The owner creates a directory, invites an agent, and lists it as read-write by its worker path."""
    context = _context(_config(tmp_path, dedicated_workers=True))
    worker_path = "/app/worker/shared_directories/alice/report"

    created = _call(context, "create_shared_directory", "report")
    invited = _call(context, "invite_to_shared_directory", "report", "bob")
    listed = _call(context, "list_shared_directories")

    assert created["status"] == "ok"
    assert created["path"] == worker_path
    assert "alice: worker restarts on the next tool call" in str(created["message"])
    assert invited["status"] == "ok"
    assert invited["members"] == ["bob"]
    assert "bob: worker restarts on the next tool call" in str(invited["message"])
    assert listed["directories"] == [
        {
            "owner": "alice",
            "name": "report",
            "members": ["bob"],
            "access": "read-write",
            "path": worker_path,
        },
    ]


def test_member_sees_the_directory_read_only(tmp_path: Path) -> None:
    """An invited agent lists the directory it joined as read-only; others see nothing."""
    config = _config(tmp_path)
    _call(_context(config), "create_shared_directory", "report")
    _call(_context(config), "invite_to_shared_directory", "report", "bob")

    bob_listing = _call(_context(config, "bob"), "list_shared_directories")

    assert [(entry["owner"], entry["access"]) for entry in bob_listing["directories"]] == [("alice", "read-only")]


@pytest.mark.parametrize(
    ("target", "fragment"),
    [
        ("alice", "cannot invite yourself"),
        ("carol", "carol is private"),
        ("crew", "'crew' is not an agent"),
        ("ghost", "'ghost' is not an agent"),
    ],
)
def test_invite_refuses_invalid_targets(tmp_path: Path, target: str, fragment: str) -> None:
    """Only other configured, non-private agents can be invited."""
    context = _context(_config(tmp_path))
    _call(context, "create_shared_directory", "report")

    payload = _call(context, "invite_to_shared_directory", "report", target)

    assert payload["status"] == "error"
    assert fragment in str(payload["message"])
    assert load_shared_directories(context.runtime_paths)[0].members == ()


def test_invite_refuses_requester_without_access_to_the_invitee(tmp_path: Path) -> None:
    """A requester cannot expose a directory to an agent they may not use themselves."""
    context = _context(_config(tmp_path, access=ResponderAccessConfig(users=["@other:localhost"])))
    _call(context, "create_shared_directory", "report")

    payload = _call(context, "invite_to_shared_directory", "report", "bob")

    assert payload["status"] == "error"
    assert "no access to bob outside the current room" in str(payload["message"])
    assert load_shared_directories(context.runtime_paths)[0].members == ()


def test_invite_refuses_when_no_user_is_acting(tmp_path: Path) -> None:
    """An agent acting in its own name cannot invite, as for any user-only action."""
    context = _context(_config(tmp_path))
    _call(context, "create_shared_directory", "report")
    context = replace(context, requester_id=context.client.user_id)

    payload = _call(context, "invite_to_shared_directory", "report", "bob")

    assert payload["status"] == "error"
    assert "on behalf of a user" in str(payload["message"])


def test_private_agents_cannot_share(tmp_path: Path) -> None:
    """A private agent cannot create a shared directory."""
    context = _context(_config(tmp_path), "carol")

    payload = _call(context, "create_shared_directory", "report")

    assert payload["status"] == "error"
    assert "Private agents cannot share directories" in str(payload["message"])
    assert load_shared_directories(context.runtime_paths) == ()


@pytest.mark.parametrize(
    ("function", "args"),
    [
        ("invite_to_shared_directory", ("report", "alice")),
        ("remove_from_shared_directory", ("report", "alice")),
        ("delete_shared_directory", ("report",)),
    ],
)
def test_only_the_owner_changes_a_directory(tmp_path: Path, function: str, args: tuple[str, ...]) -> None:
    """Another agent cannot change or delete a directory it does not own."""
    config = _config(tmp_path)
    _call(_context(config), "create_shared_directory", "report")

    payload = _call(_context(config, "bob"), function, *args)

    assert payload["status"] == "error"
    assert "bob has no shared directory named 'report'" in str(payload["message"])
    assert len(load_shared_directories(runtime_paths_for(config))) == 1


def test_invite_and_remove_are_idempotent(tmp_path: Path) -> None:
    """Repeating an invitation or removal reports the state without restarting anything."""
    context = _context(_config(tmp_path, dedicated_workers=True))
    _call(context, "create_shared_directory", "report")
    _call(context, "invite_to_shared_directory", "report", "bob")

    again = _call(context, "invite_to_shared_directory", "report", "bob")
    removed = _call(context, "remove_from_shared_directory", "report", "bob")
    removed_again = _call(context, "remove_from_shared_directory", "report", "bob")

    assert again["status"] == "ok"
    assert "bob is already a member" in str(again["message"])
    assert "restart" not in str(again["message"])
    assert "bob: worker restarts" in str(removed["message"])
    assert removed_again["status"] == "ok"
    assert "bob is not a member" in str(removed_again["message"])
    assert "restart" not in str(removed_again["message"])


def test_delete_empties_the_directory_and_names_every_restart(tmp_path: Path) -> None:
    """Deleting forgets the directory, removes every file in it, and names the owner and members."""
    context = _context(_config(tmp_path))
    created = _call(context, "create_shared_directory", "report")
    context = _context(_config(tmp_path, dedicated_workers=True))
    _call(context, "invite_to_shared_directory", "report", "bob")

    payload = _call(context, "delete_shared_directory", "report")

    assert payload["status"] == "ok"
    assert "alice and bob: worker restarts" in str(payload["message"])
    assert list(Path(str(created["path"])).iterdir()) == []
    assert load_shared_directories(context.runtime_paths) == ()


def test_invalid_name_is_reported_to_the_model(tmp_path: Path) -> None:
    """A refused name comes back as a tool error, not an exception."""
    payload = _call(_context(_config(tmp_path)), "create_shared_directory", "../x")

    assert payload["status"] == "error"
    assert "Shared directory names" in str(payload["message"])


@pytest.mark.parametrize(
    ("backend_env", "expected_root"),
    [
        ({"MINDROOM_WORKER_BACKEND": "docker", "MINDROOM_DOCKER_WORKER_IMAGE": "worker:test"}, Path("/app/worker")),
        (
            {
                "MINDROOM_WORKER_BACKEND": "docker",
                "MINDROOM_DOCKER_WORKER_IMAGE": "worker:test",
                "MINDROOM_DOCKER_WORKER_STORAGE_MOUNT_PATH": "/srv/worker",
            },
            Path("/srv/worker"),
        ),
    ],
)
def test_paths_name_the_worker_mount_when_tools_run_in_a_dedicated_worker(
    tmp_path: Path,
    backend_env: dict[str, str],
    expected_root: Path,
) -> None:
    """An agent whose file tools run in a dedicated worker is told the path its worker mounts."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", **backend_env},
    )
    config = bind_runtime_paths(
        Config(agents={"alice": AgentConfig(display_name="Alice", worker_tools=["shell"])}),
        runtime_paths,
    )
    create_shared_directory(runtime_paths, owner="alice", name="report")
    in_primary = bind_runtime_paths(
        Config(agents={"alice": AgentConfig(display_name="Alice", worker_tools=[])}),
        runtime_paths,
    )

    assert [view.path for view in agent_shared_directory_views(config, runtime_paths, "alice")] == [
        expected_root / "shared_directories" / "alice" / "report",
    ]
    assert [view.path for view in agent_shared_directory_views(in_primary, runtime_paths, "alice")] == [
        runtime_paths.storage_root / "shared_directories" / "alice" / "report",
    ]


def test_restart_notice_appears_only_with_dedicated_workers(tmp_path: Path) -> None:
    """Without dedicated workers nothing restarts, so the result does not claim it."""
    context = _context(_config(tmp_path))

    created = _call(context, "create_shared_directory", "report")
    invited = _call(context, "invite_to_shared_directory", "report", "bob")

    assert "restart" not in str(created["message"])
    assert invited["message"] == "bob can now read it."


def test_mcp_gateway_never_offers_the_tool() -> None:
    """Gateway calls run without the requester and room the tool authorizes against, so the gateway leaves it out."""
    assert TOOL_METADATA["shared_directories"].mcp_available is False


def test_a_team_member_shares_as_itself(tmp_path: Path) -> None:
    """In a team run the tool acts for the member that calls it, not for the team."""
    config = _config(tmp_path)
    team_context = _context(config, "crew")

    created = _call(team_context, "create_shared_directory", "report", acting_agent="alice")
    invited = _call(team_context, "invite_to_shared_directory", "report", "bob", acting_agent="alice")

    assert created["status"] == "ok"
    assert invited["status"] == "ok"
    assert [
        (directory.owner, directory.members) for directory in load_shared_directories(runtime_paths_for(config))
    ] == [
        ("alice", ("bob",)),
    ]


@pytest.mark.parametrize("function", ["remove_from_shared_directory", "delete_shared_directory"])
def test_changes_succeed_after_a_member_left_the_config(tmp_path: Path, function: str) -> None:
    """A member removed from the config can still be removed, and the directory deleted, with dedicated workers."""
    config = _config(tmp_path, dedicated_workers=True)
    _call(_context(config), "create_shared_directory", "report")
    _call(_context(config), "invite_to_shared_directory", "report", "bob")
    without_bob = bind_runtime_paths(
        config.model_copy(update={"agents": {k: v for k, v in config.agents.items() if k != "bob"}, "teams": {}}),
        runtime_paths_for(config),
    )

    args = ("report", "bob") if function == "remove_from_shared_directory" else ("report",)
    payload = _call(_context(without_bob), function, *args)

    assert payload["status"] == "ok"
    assert "bob:" not in str(payload["message"])
    assert "and bob" not in str(payload["message"])
