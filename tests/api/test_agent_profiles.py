"""Chat clients read the role, room model and toolkits of an agent they may use in a room, and nothing else of it.

Alice and Bob may use ``coder``, Carol may not, and Dave is not in the room.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from mindroom.api import agent_profiles
from mindroom.api.client_sessions import client_session_store
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.contacts.store import ContactParty, ContactRecord, ContactSide, update_contacts
from mindroom.mcp.config import MCPServerConfig
from mindroom.room_agent_access import room_execution_identity
from mindroom.tool_system.agent_tools import resolve_agent_tools, turn_keeps_room_tools
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.registry import builtin_tool_registry
from tests.api.test_plugin_room_agents import ALICE, BOB, CAROL, READ_ONLY_FILE, ROOM, _paths, _Room
from tests.test_generated_agents import _load, _personal, _with_template

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import Response

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

DAVE = "@dave:localhost"
MCP_URL = "https://mcp.example.org/private-path"
MCP_TOKEN = "Bearer mcp-secret-token"  # noqa: S105


def _config(**changes: object) -> Config:
    return Config(
        agents={
            "coder": AgentConfig(
                display_name="Coder",
                role="  Writes and reviews code.  ",
                model="luna",
                # A calculator whose settings switch off every function offers nothing.
                tools=[READ_ONLY_FILE, "shell", "mcp_notes", {"calculator": {"include_tools": []}}],
                access=ResponderAccessConfig(users=[ALICE, BOB]),
            ),
            "helper": AgentConfig(display_name="Helper", access=ResponderAccessConfig(users=[ALICE])),
        },
        models={
            "default": ModelConfig(provider="openai", id="gpt-default"),
            "luna": ModelConfig(provider="openai", id="gpt-luna", display_name="Luna"),
            "sol": ModelConfig(provider="anthropic", id="claude-sol"),
        },
        mcp_servers={
            "notes": MCPServerConfig(
                transport="streamable-http",
                url=MCP_URL,
                headers={"Authorization": MCP_TOKEN},
                display_name="Notes app",
                summary="Read and write notes.",
            ),
        },
        **changes,
    )


class _Client:
    def __init__(self, room: _Room) -> None:
        self.room = room
        room.app.include_router(agent_profiles.router)
        self.http = TestClient(room.app)

    def profile(self, user_id: str | None, agent_user_id: str) -> Response:
        headers = {}
        if user_id is not None:
            headers["Authorization"] = f"Bearer {client_session_store(self.room.app).create(user_id).token}"
        return self.http.get(f"/api/rooms/{ROOM}/agents/{agent_user_id}/profile", headers=headers)


def _registry_tool(name: str) -> dict[str, object]:
    metadata = builtin_tool_registry().entries[name]
    return {
        "name": name,
        "display_name": metadata.display_name,
        "description": metadata.description,
        "category": metadata.category.value,
        "mcp_server": None,
    }


def test_a_member_reads_the_role_model_and_toolkits_and_no_tool_settings(tmp_path: Path) -> None:
    """The toolkits come in build order with their registry text; an MCP server shows its ID, never its address."""
    client = _Client(_Room(_config(), _paths(tmp_path), ("coder", "helper")))

    response = client.profile(ALICE, client.room.ids["coder"])

    assert response.status_code == 200
    assert response.json() == {
        "role": "Writes and reviews code.",
        "model": {"config": "luna", "display_name": "Luna", "id": "gpt-luna", "provider": "openai"},
        "tools": [
            _registry_tool("file"),
            _registry_tool("shell"),
            {
                "name": "mcp_notes",
                "display_name": "Notes app",
                "description": "Read and write notes.",
                "category": "development",
                "mcp_server": "notes",
            },
            _registry_tool("scheduler"),  # Every agent gets it by default.
        ],
    }
    # The toolkits a Matrix room turn of the agent may build, as its turn picks them, less the switched-off calculator.
    config, paths = client.room.config, client.room.paths
    room_tools = turn_keeps_room_tools(room_execution_identity("coder", ALICE, ROOM, config, paths))
    candidates = resolve_agent_tools(config, "coder", config_plugins(config, paths).tools).candidates(
        room_tools=room_tools,
    )
    turn_toolkits = [entry.name for entry in candidates if entry.name != "calculator"]
    assert [tool["name"] for tool in response.json()["tools"]] == turn_toolkits
    for secret in (MCP_URL, "mcp-secret-token", "enable_save_file"):
        assert secret not in response.text
    assert client.profile(BOB, client.room.ids["coder"]).json() == response.json()


def test_the_model_is_the_one_the_room_runs_and_a_role_left_blank_is_null(tmp_path: Path) -> None:
    """A room's ``room_models`` entry wins over the agent's own model, as its turns there resolve it."""
    client = _Client(_Room(_config(room_models={ROOM: "sol"}), _paths(tmp_path), ("coder", "helper")))

    profile = client.profile(ALICE, client.room.ids["helper"]).json()

    assert profile["role"] is None
    assert profile["model"] == {"config": "sol", "id": "claude-sol", "provider": "anthropic"}
    assert client.profile(ALICE, client.room.ids["coder"]).json()["model"]["config"] == "sol"


def test_only_a_member_who_may_use_an_agent_joined_to_the_room_reads_it(tmp_path: Path) -> None:
    """No session answers 401; a non-member, a refused member, an unknown ID and an agent not joined answer 403."""
    client = _Client(_Room(_config(), _paths(tmp_path), ("coder",)))
    coder, helper = client.room.ids["coder"], client.room.ids["helper"]

    assert client.profile(None, coder).status_code == 401
    assert client.profile(DAVE, coder).status_code == 403
    assert client.profile(CAROL, coder).status_code == 403
    assert client.profile(ALICE, "@nobody:localhost").status_code == 403
    assert client.profile(ALICE, ALICE).status_code == 403
    assert client.profile(ALICE, helper).status_code == 403
    assert client.profile(ALICE, coder).status_code == 200


def test_a_contact_room_opens_no_profile(tmp_path: Path) -> None:
    """A contact room runs its agent's turns as the agent's owner, so nobody reads that agent through it."""
    data = _with_template()
    data["personal_agents"]["mind"]["agent"] |= {"contacts": {}}
    client = _Client(_Room(_load(tmp_path, data, entries=[_personal(ALICE)]), _paths(tmp_path), ("mind_alice",)))
    agent_id = client.room.ids["mind_alice"]
    assert client.profile(ALICE, agent_id).status_code == 200

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
    update_contacts(client.room.paths, lambda book: book.contacts.append(record))

    assert client.profile(ALICE, agent_id).status_code == 403
