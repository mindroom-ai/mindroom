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
from mindroom.mcp.config import MCPServerConfig
from mindroom.tool_system.registry import builtin_tool_registry
from tests.api.test_plugin_room_agents import ALICE, BOB, CAROL, READ_ONLY_FILE, ROOM, _paths, _Room

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
                tools=[READ_ONLY_FILE, "shell", "mcp_notes"],
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
    def __init__(self, tmp_path: Path, config: Config, joined: tuple[str, ...] = ("coder", "helper")) -> None:
        self.room = _Room(config, _paths(tmp_path), joined)
        self.room.app.include_router(agent_profiles.router)
        self.http = TestClient(self.room.app)

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
    client = _Client(tmp_path, _config())

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
    for secret in (MCP_URL, "mcp-secret-token", "enable_save_file"):
        assert secret not in response.text
    assert client.profile(BOB, client.room.ids["coder"]).json() == response.json()


def test_the_model_is_the_one_the_room_runs_and_a_role_left_blank_is_null(tmp_path: Path) -> None:
    """A room's ``room_models`` entry wins over the agent's own model, as its turns there resolve it."""
    client = _Client(tmp_path, _config(room_models={ROOM: "sol"}))

    profile = client.profile(ALICE, client.room.ids["helper"]).json()

    assert profile["role"] is None
    assert profile["model"] == {"config": "sol", "id": "claude-sol", "provider": "anthropic"}
    assert client.profile(ALICE, client.room.ids["coder"]).json()["model"]["config"] == "sol"


def test_only_a_member_who_may_use_an_agent_joined_to_the_room_reads_it(tmp_path: Path) -> None:
    """No session answers 401; a non-member, a refused member, an unknown ID and an agent not joined answer 403."""
    client = _Client(tmp_path, _config(), joined=("coder",))
    coder, helper = client.room.ids["coder"], client.room.ids["helper"]

    assert client.profile(None, coder).status_code == 401
    assert client.profile(DAVE, coder).status_code == 403
    assert client.profile(CAROL, coder).status_code == 403
    assert client.profile(ALICE, "@nobody:localhost").status_code == 403
    assert client.profile(ALICE, ALICE).status_code == 403
    assert client.profile(ALICE, helper).status_code == 403
    assert client.profile(ALICE, coder).status_code == 200
